"""player.gear: "tier": the armor and weapon proficiencies a kit needs are learned before the equip, a kit left
by an earlier trial is stripped (only the items foreign to this trial's kit) before the trial's own kit, a slow
server (a grant, an equip or a removal that shows late) no longer leaves a piece "unworn", and the whole gear
setup runs under one deadline."""

import pathlib
import sys
import tempfile
import time
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_live as tl  # noqa: E402
from test_gear import GearAgent, player  # noqa: E402
from trl import gear  # noqa: E402
from trl.live import gm_guard, setup  # noqa: E402
from trl.live.agent import norm_guid  # noqa: E402

PLATE_MAIL, MAIL_ARMOR = 750, 8737


def fake_kit(item_class, subclass, base, n=3):
    """One kit of n items (entries base..), all of one armor kind."""
    items = []
    for i, slot in enumerate(("head", "chest", "legs", "feet", "hands")[:n]):
        items.append({"slot": slot, "entry": base + i, "name": "Item %d" % (base + i), "armor": 100,
                      "required_level": 40, "item_class": item_class, "subclass": subclass})
    return items


def kits_of(*kits):
    """{'warrior': {'dps': {'40': kit, '50': kit2}}}: level 40, 50 ... from the given kits."""
    return {"warrior": {"dps": {str(40 + 10 * i): kit for i, kit in enumerate(kits)}}}


class ProfAgent(GearAgent):
    """A server that enforces proficiency: `.learn N` adds a spell, `equip` of an item whose spell is not known is
    refused, and a spell the player has is listed by `spells`. `need` maps entry -> proficiency spell.
    Knobs for a slow or flaky server: grant_lag / equip_lag / strip_lag (reads before the change shows),
    learn_drops (the first N `.learn` lines are ignored), slow (seconds every RPC takes)."""

    def __init__(self, need, known=(), grant_lag=0, equip_lag=0, strip_lag=0, learn_drops=0, slow=0.0, **kw):
        super().__init__(**kw)
        self.need = dict(need)
        self.spells_known = set(known)
        self.events = []                       # ("learn" | "grant" | "strip" | "equip", entry) in order
        self.grant_lag, self.equip_lag, self.strip_lag = grant_lag, equip_lag, strip_lag
        self.learn_drops, self.slow = learn_drops, slow
        self.learn_sends = []
        self.equip_sends = {}                  # entry -> how many times an equip was sent
        self._hidden = {}                      # entry -> inventory reads left before a grant shows
        self._pending = {}                     # entry -> equipment reads left before an equip shows
        self._lingering = {}                   # entry -> [reads left, "worn" | "bag"] for a removed item
        self.refuse_all_equips = False

    def rpc(self, method, params=None, timeout=None):
        if self.slow:
            time.sleep(self.slow)
        if method == "gm":
            text = (params or {}).get("text", "")
            if text.startswith(".learn"):
                spell = int(text.split()[1])
                self.learn_sends.append(spell)
                if self.learn_drops > 0:
                    self.learn_drops -= 1
                else:
                    self.spells_known.add(spell)
                    self.events.append(("learn", spell))
                return super().rpc("gm", params, timeout)
            if text.startswith(".additem"):
                entry, count = int(text.split()[1]), int(text.split()[2])
                self.events.append(("grant" if count > 0 else "strip", entry))
                if count > 0 and self.grant_lag:
                    self._hidden[entry] = self.grant_lag
                if count < 0 and self.strip_lag:
                    where = "worn" if entry in self.worn else "bag" if self.bags.get(entry) else None
                    if where:
                        self._lingering[entry] = [self.strip_lag, where]
        if method == "inventory":
            out = super().rpc(method, params, timeout)
            if isinstance(out, list):
                for entry in list(self._hidden):
                    self._hidden[entry] -= 1
                out = [r for r in out if self._hidden.get(r["item_entry"], 0) <= 0]
                for entry in [e for e, n in self._hidden.items() if n <= 0]:
                    del self._hidden[entry]
                for entry, (left, where) in list(self._lingering.items()):
                    if where == "bag":
                        out.append({"bag": 255, "slot": 99, "item_entry": entry, "count": 1})
                        self._lingering[entry][0] -= 1
                        if self._lingering[entry][0] <= 0:
                            del self._lingering[entry]
            return out
        if method == "equipment":
            for entry in list(self._pending):
                self._pending[entry] -= 1
                if self._pending[entry] < 0:
                    self.worn.add(entry)
                    del self._pending[entry]
            out = super().rpc(method, params, timeout)
            if isinstance(out, list):
                for entry, (left, where) in list(self._lingering.items()):
                    if where == "worn":
                        out.append({"slot": 30, "item_entry": entry})
                        self._lingering[entry][0] -= 1
                        if self._lingering[entry][0] <= 0:
                            del self._lingering[entry]
            return out
        if method == "equip":
            entry = int(params["item_entry"])
            self.equip_sends[entry] = self.equip_sends.get(entry, 0) + 1
            if self.refuse_all_equips:
                return {"_err": "cannot equip item %d: wrong slot" % entry}
            if entry in self._hidden or not self.bags.get(entry):
                return {"_err": "item %d is not in the bags" % entry}
            need = self.need.get(entry)
            if need and need not in self.spells_known:
                return {"_err": "cannot equip item %d: no proficiency" % entry}
            self.events.append(("equip", entry))
            if self.equip_lag:
                self.bags[entry] -= 1
                if not self.bags[entry]:
                    del self.bags[entry]
                self._pending[entry] = self.equip_lag
                return {"entry": entry}
        return super().rpc(method, params, timeout)

    def lines(self, prefix):
        return [e for kind, e in self.events if kind == prefix]


def kit_ctx(items, kits):
    return tl.ctx_for(gear_ids=frozenset(i["entry"] for i in items),
                      proficiency_ids=frozenset(gear.proficiency_spells(items)),
                      strip_ids=frozenset(gear.all_entries(kits)))


def run_gear(agent, kit_player, kits, **kw):
    items = gear.pick_kit(kit_player, kits)[0]
    ctx = kit_ctx(items, kits)
    with tempfile.TemporaryDirectory() as tmp:
        guard = gm_guard.GmGuard(agent, tl.new_ledger(tmp), ctx)
        kw.setdefault("settle", 0.0)
        kw.setdefault("wait", 0.0)
        kw.setdefault("poll", 0.0)
        return setup.check_player_gear(agent, {"id": "t", "player": kit_player}, guard, ctx, kits=kits, **kw)


def plate_need(items):
    return {i["entry"]: PLATE_MAIL for i in items}


class ProficiencyTest(unittest.TestCase):
    def test_a_plate_kit_on_a_warrior_without_plate_mail_learns_750_before_the_equip(self):
        kit = fake_kit(4, 4, 7000)
        agent = ProfAgent(plate_need(kit))
        row = run_gear(agent, player(level=40, role="dps"), kits_of(kit))
        self.assertTrue(row["ok"], row["evidence"])
        self.assertIn(PLATE_MAIL, agent.spells_known)
        order = [k for k, _e in agent.events]
        self.assertLess(order.index("learn"), order.index("equip"))
        self.assertEqual(agent.lines("learn"), [PLATE_MAIL])
        self.assertIn("learned proficiency spell(s) 750", row["evidence"])
        self.assertEqual(len(agent.worn), len(kit))

    def test_a_known_proficiency_is_not_learned_again(self):
        kit = fake_kit(4, 4, 7000)
        agent = ProfAgent(plate_need(kit), known={PLATE_MAIL})
        row = run_gear(agent, player(level=40, role="dps"), kits_of(kit))
        self.assertTrue(row["ok"], row["evidence"])
        self.assertEqual(agent.lines("learn"), [])

    def test_a_mail_kit_learns_8737(self):
        kit = fake_kit(4, 3, 7100)
        agent = ProfAgent({i["entry"]: MAIL_ARMOR for i in kit})
        row = run_gear(agent, player(level=40, role="dps"), kits_of(kit))
        self.assertTrue(row["ok"], row["evidence"])
        self.assertEqual(agent.lines("learn"), [MAIL_ARMOR])

    def test_a_cloth_kit_learns_nothing(self):
        kit = fake_kit(4, 1, 7200)
        agent = ProfAgent({})
        row = run_gear(agent, player(level=40, role="dps"), kits_of(kit))
        self.assertTrue(row["ok"], row["evidence"])
        self.assertEqual(agent.lines("learn"), [])
        self.assertFalse([g for g in agent.gm if g.startswith(".learn")])

    def test_weapon_skills_the_kit_needs_are_learned_too(self):
        kit = fake_kit(4, 4, 7300, n=2) + [{"slot": "twohand", "entry": 7399, "name": "Greatsword", "armor": 0,
                                           "required_level": 40, "item_class": 2, "subclass": 8}]
        need = plate_need(kit[:2])
        need[7399] = 202
        agent = ProfAgent(need)
        row = run_gear(agent, player(level=40, role="dps"), kits_of(kit))
        self.assertTrue(row["ok"], row["evidence"])
        self.assertEqual(sorted(agent.lines("learn")), [202, PLATE_MAIL])

    def test_the_second_learn_attempt_catches_a_line_the_server_dropped(self):
        kit = fake_kit(4, 4, 7000)
        agent = ProfAgent(plate_need(kit), learn_drops=1)
        row = run_gear(agent, player(level=40, role="dps"), kits_of(kit))
        self.assertTrue(row["ok"], row["evidence"])
        self.assertEqual(agent.learn_sends, [PLATE_MAIL, PLATE_MAIL])         # sent, dropped, sent again
        self.assertIn("learned proficiency spell(s) 750", row["evidence"])

    def test_a_kit_entry_without_class_data_fails_closed_and_sends_nothing(self):
        for broken in ({"slot": "head", "entry": 7400, "name": "Old Helm", "armor": 100, "required_level": 40},
                       {"slot": "head", "entry": 7400, "name": "Old Helm", "armor": 100, "required_level": 40,
                        "item_class": None, "subclass": None},
                       {"slot": "head", "entry": 7400, "name": "Old Helm", "armor": 100, "required_level": 40,
                        "item_class": 0, "subclass": 0}):
            kit = [broken] + fake_kit(4, 4, 7401, n=2)
            agent = ProfAgent(plate_need(kit))
            row = run_gear(agent, player(level=40, role="dps"), kits_of(kit))
            self.assertFalse(row["ok"])
            self.assertIn("kit data lacks item class", row["evidence"])
            self.assertIn("Old Helm", row["evidence"])
            self.assertEqual(agent.gm, [])                                      # no plate guess, no GM line
            self.assertEqual(gear.proficiency_spells([broken]), [])

    def test_a_missing_item_table_column_is_written_as_none_not_zero(self):
        import build_data
        self.assertIsNone(build_data._opt_int(None))
        self.assertIsNone(build_data._opt_int(""))
        self.assertEqual(build_data._opt_int("0"), 0)
        self.assertEqual(build_data._opt_int("4"), 4)

    def test_the_shipped_warrior_kits_carry_class_data_and_need_plate_and_their_weapon(self):
        for role, levels in gear.load_kits()["warrior"].items():
            for level, items in levels.items():
                self.assertEqual(gear.missing_class(items), [], (role, level))
                self.assertIn(PLATE_MAIL, gear.proficiency_spells(items), (role, level))

    def test_the_guard_learns_only_this_trials_kit_proficiencies_for_the_player_only(self):
        agent = tl.FakeAgent()
        with tempfile.TemporaryDirectory() as tmp:
            g = gm_guard.GmGuard(agent, tl.new_ledger(tmp),
                                 tl.ctx_for(proficiency_ids=frozenset({PLATE_MAIL})))
            g.send("learn", tl.PLAYER, PLATE_MAIL)
            with self.assertRaises(gm_guard.GmRefused):
                g.send("learn", tl.PLAYER, MAIL_ARMOR)       # a real proficiency, but not this trial's kit
            with self.assertRaises(gm_guard.GmRefused):
                g.send("learn", tl.PLAYER, 12345)            # not a trial spell
            with self.assertRaises(gm_guard.GmRefused):
                g.send("learn", tl.HIRE, PLATE_MAIL)         # never the hire
            g2 = gm_guard.GmGuard(agent, tl.new_ledger(tmp), tl.ctx_for())
            with self.assertRaises(gm_guard.GmRefused):
                g2.send("learn", tl.PLAYER, PLATE_MAIL)      # a no-gear trial learns no proficiency
        self.assertEqual(agent.gm, [".learn 750"])

    def test_a_proficiency_that_will_not_learn_fails_the_row_and_says_so(self):
        kit = fake_kit(4, 4, 7000)
        agent = ProfAgent(plate_need(kit), learn_drops=99)
        row = run_gear(agent, player(level=40, role="dps"), kits_of(kit))
        self.assertFalse(row["ok"])
        self.assertIn("proficiency NOT learned: 750", row["evidence"])


class SlowServerTest(unittest.TestCase):
    """One piece 'stays unworn at random': the equip went out before the bag read showed the grant, or the worn
    read came before the equip landed."""

    def test_an_equip_waits_for_the_grant_to_show_in_the_bags(self):
        kit = fake_kit(4, 4, 7000)
        agent = ProfAgent(plate_need(kit), grant_lag=3)
        row = run_gear(agent, player(level=40, role="dps"), kits_of(kit), wait=1.0)
        self.assertTrue(row["ok"], row["evidence"])
        self.assertEqual(len(agent.worn), len(kit))
        self.assertEqual(set(agent.equip_sends.values()), {1})                  # no equip sent before the bags had it

    def test_the_read_back_waits_for_the_equip_to_show_on_the_body(self):
        kit = fake_kit(4, 4, 7000)
        agent = ProfAgent(plate_need(kit), equip_lag=2)
        row = run_gear(agent, player(level=40, role="dps"), kits_of(kit), wait=1.0)
        self.assertTrue(row["ok"], row["evidence"])
        self.assertEqual(len(agent.worn), len(kit))
        self.assertEqual(set(agent.equip_sends.values()), {1})                  # waited, did not resend

    def test_an_equip_the_server_dropped_is_sent_again_once(self):
        kit = fake_kit(4, 4, 7000)
        agent = ProfAgent(plate_need(kit))
        orig, dropped = agent.rpc, []

        def drop_first(method, params=None, timeout=None):
            if method == "equip" and not dropped:
                dropped.append(params["item_entry"])
                agent.equip_sends[params["item_entry"]] = agent.equip_sends.get(params["item_entry"], 0) + 1
                return {"entry": params["item_entry"]}      # accepted, never applied
            return orig(method, params, timeout)
        agent.rpc = drop_first
        row = run_gear(agent, player(level=40, role="dps"), kits_of(kit))
        self.assertTrue(row["ok"], row["evidence"])
        self.assertEqual(len(agent.worn), len(kit))
        self.assertEqual(agent.equip_sends[dropped[0]], 2)                      # the drop and exactly one resend
        self.assertEqual(sorted(v for k, v in agent.equip_sends.items() if k != dropped[0]), [1] * (len(kit) - 1))

    def test_a_refused_equip_is_not_retried_or_waited_for(self):
        kit = fake_kit(4, 4, 7000)
        agent = ProfAgent(plate_need(kit))
        agent.refuse_all_equips = True
        started = time.monotonic()
        row = run_gear(agent, player(level=40, role="dps"), kits_of(kit), wait=30.0, poll=0.0)
        self.assertLess(time.monotonic() - started, 5.0)                        # no 30 s read-back waits
        self.assertFalse(row["ok"])
        self.assertEqual(set(agent.equip_sends.values()), {1})                  # one try each, no more rounds
        self.assertIn("equip errors", row["evidence"])


class DeadlineTest(unittest.TestCase):
    """The whole gear setup is one budget: waits are capped by what is left, both `_retry` passes share it."""

    def test_a_degraded_server_ends_within_the_deadline_across_both_retry_passes(self):
        kit = fake_kit(4, 4, 7000)
        agent = ProfAgent(plate_need(kit), slow=0.02, learn_drops=99)
        agent.refuse_all_equips = True
        kits = kits_of(kit)
        pl = player(level=40, role="dps")
        ctx = kit_ctx(kit, kits)
        budget = 1.5
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            guard = gm_guard.GmGuard(agent, tl.new_ledger(tmp), ctx)
            dl = setup.Deadline(budget)
            calls = []

            def one():
                calls.append(1)
                return setup.check_player_gear(agent, {"id": "t", "player": pl}, guard, ctx, kits=kits,
                                               settle=2.0, wait=30.0, poll=0.01, deadline=dl)
            row = setup._retry(one, deadline=dl)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, budget + 1.5)                                  # never the 2 x (2 + 30 + ...) s
        self.assertFalse(row["ok"])
        self.assertLessEqual(len(calls), 2)

    def test_a_spent_deadline_stops_the_retry_and_names_the_phase(self):
        kit = fake_kit(4, 4, 7000)
        agent = ProfAgent(plate_need(kit), learn_drops=99)
        kits = kits_of(kit)
        pl = player(level=40, role="dps")
        ctx = kit_ctx(kit, kits)
        clock = [0.0]
        dl = setup.Deadline(10.0, clock=lambda: clock[0])
        with tempfile.TemporaryDirectory() as tmp:
            guard = gm_guard.GmGuard(agent, tl.new_ledger(tmp), ctx)
            calls = []

            def one():
                calls.append(1)
                clock[0] += 11.0                                                # the first pass spends the budget
                return setup.check_player_gear(agent, {"id": "t", "player": pl}, guard, ctx, kits=kits,
                                               settle=0.0, wait=0.0, poll=0.0, deadline=dl)
            row = setup._retry(one, deadline=dl)
        self.assertEqual(len(calls), 1)                                         # no second pass
        self.assertFalse(row["ok"])
        self.assertIn("setup deadline (10 s) ran out in: proficiency learn", row["evidence"])
        self.assertEqual(agent.gm, [])                                          # nothing sent past the deadline

    def test_run_setup_shares_one_deadline_between_the_strip_and_the_kit(self):
        seen = []

        def strip(*a, **k):
            seen.append(k.get("deadline"))
            return setup.row("player_gear_carryover", True, None, "stub")

        def gear_step(*a, **k):
            seen.append(k.get("deadline"))
            return setup.row("player_gear", True, None, "stub")
        pl = dict(player(level=50, role="dps"), stance=71)
        trial = {"id": "g", "player": pl, "bot": {"class": "mage"}, "situation": {"kind": "tank_damage"},
                 "setup_checks": []}
        agent = ProfAgent({})
        ctx = tl.ctx_for(gear_ids=frozenset(gear.kit_entries(pl)), strip_ids=frozenset(gear.all_entries()))
        with tempfile.TemporaryDirectory() as tmp:
            guard = gm_guard.GmGuard(agent, tl.new_ledger(tmp), ctx)
            with unittest.mock.patch.multiple(
                    setup, check_level=lambda *a, **k: setup.row("player_level", True, 50, "ok"),
                    check_talents=lambda *a, **k: setup.row("player_talents", True, [], "ok"),
                    check_stance=lambda *a, **k: setup.row("player_stance", True, 71, "ok"),
                    check_forced_role=lambda *a, **k: setup.row("player_forced_role", True, "x", "ok"),
                    strip_player_gear=strip, check_player_gear=gear_step,
                    read_offhand=lambda a: {"readable": True, "item_entry": None, "evidence": "empty"}):
                setup.run_setup(agent, trial, guard, tl.new_ledger(tmp), ctx, first=False)
        self.assertEqual(len(seen), 2)
        self.assertIsInstance(seen[0], setup.Deadline)
        self.assertIs(seen[0], seen[1])                                         # one budget for both steps


class RpcCapTest(unittest.TestCase):
    def test_the_gear_rpcs_carry_a_timeout_capped_by_the_deadline(self):
        kit = fake_kit(4, 4, 7000)
        agent = ProfAgent(plate_need(kit))
        seen = []
        orig = agent.rpc

        def spy(method, params=None, timeout=None):
            if method in ("equip", "equipment", "inventory", "gm", "status", "spells"):
                seen.append((method, timeout))
            return orig(method, params, timeout)
        agent.rpc = spy
        kits = kits_of(kit)
        pl = player(level=40, role="dps")
        ctx = kit_ctx(kit, kits)
        with tempfile.TemporaryDirectory() as tmp:
            guard = gm_guard.GmGuard(agent, tl.new_ledger(tmp), ctx)
            setup.check_player_gear(agent, {"id": "t", "player": pl}, guard, ctx, kits=kits, settle=0.0,
                                    wait=0.0, poll=0.0, deadline=setup.Deadline(7.0))
        self.assertTrue({"equip", "equipment", "inventory", "gm", "status", "spells"} <= {m for m, _t in seen})
        # the guard's own selection RPCs (10 s by default) and the spell read are capped by the deadline too
        self.assertTrue(all(t is not None and int(t.rstrip("s")) <= 7 for _m, t in seen), seen)


class CarryOverTest(unittest.TestCase):
    """Only the kit items foreign to this trial's kit are stripped, whatever the gear mode."""

    def setUp(self):
        self.kit40 = fake_kit(4, 4, 7000)
        self.kit50 = fake_kit(4, 4, 8000)
        self.kits = {"warrior": {"dps": {"40": self.kit40, "50": self.kit50}}}

    def strip_then_gear(self, agent, level, wait=0.0):
        pl = player(level=level, role="dps")
        items = gear.pick_kit(pl, self.kits)[0]
        ctx = kit_ctx(items, self.kits)
        keep = set(ctx.gear_ids)
        with tempfile.TemporaryDirectory() as tmp:
            guard = gm_guard.GmGuard(agent, tl.new_ledger(tmp), ctx)
            strip = setup.strip_player_gear(agent, guard, ctx, keep=keep, row_id="player_gear_carryover",
                                            wait=wait, poll=0.0)
            order_after_strip = len(agent.events)
            gear_row = setup.check_player_gear(agent, {"id": "t", "player": pl}, guard, ctx, kits=self.kits,
                                               settle=0.0, wait=0.0, poll=0.0)
        return strip, gear_row, order_after_strip

    def test_a_leftover_level_40_kit_is_stripped_before_a_level_50_kit(self):
        agent = ProfAgent(plate_need(self.kit40 + self.kit50), worn={i["entry"] for i in self.kit40})
        agent.bags[7000] = 1                                          # a stray copy in the bags too
        strip, gear_row, n = self.strip_then_gear(agent, 50)
        self.assertTrue(strip["ok"], strip["evidence"])
        self.assertEqual(strip["id"], "player_gear_carryover")
        self.assertTrue(gear_row["ok"], gear_row["evidence"])
        self.assertEqual(agent.worn, {i["entry"] for i in self.kit50})
        self.assertEqual(agent.bags, {})
        stripped = [e for k, e in agent.events[:n] if k == "strip"]
        self.assertEqual(sorted(stripped), sorted([i["entry"] for i in self.kit40] + [7000]))
        self.assertTrue(all(k == "strip" for k, _e in agent.events[:n]))     # nothing granted before the strip ended

    def test_a_player_with_the_current_kit_and_one_foreign_piece_loses_only_the_foreign_piece(self):
        foreign = self.kit40[0]["entry"]
        agent = ProfAgent(plate_need(self.kit40 + self.kit50),
                          worn={i["entry"] for i in self.kit50} | {foreign, 999001})
        agent.bags[self.kit50[0]["entry"]] = 1                         # a spare copy of a current piece in the bags
        strip, _gear_row, n = self.strip_then_gear(agent, 50)
        self.assertTrue(strip["ok"], strip["evidence"])
        self.assertEqual([e for k, e in agent.events[:n] if k == "strip"], [foreign])    # exactly the foreign one
        self.assertEqual(agent.worn, {i["entry"] for i in self.kit50} | {999001})        # the kit stays, others too
        self.assertEqual(agent.bags, {self.kit50[0]["entry"]: 1})                        # the spare copy stays

    def test_every_copy_of_a_foreign_entry_goes_one_minus_one_per_copy(self):
        agent = ProfAgent(plate_need(self.kit40), worn={self.kit40[0]["entry"]})
        agent.bags[self.kit40[1]["entry"]] = 3
        strip, _g, n = self.strip_then_gear(agent, 50)
        self.assertTrue(strip["ok"], strip["evidence"])
        self.assertEqual(sorted(e for k, e in agent.events[:n] if k == "strip"),
                         sorted([self.kit40[0]["entry"]] + [self.kit40[1]["entry"]] * 3))
        self.assertEqual(agent.additem_lines().count(".additem %d -1" % self.kit40[1]["entry"]), 3)

    def test_the_strip_goes_to_the_player_only(self):
        agent = ProfAgent(plate_need(self.kit40), worn={i["entry"] for i in self.kit40})
        self.strip_then_gear(agent, 50)
        additem_targets = [t for text, t in agent.gm_at if text.startswith(".additem") and text.endswith("-1")]
        self.assertEqual(len(additem_targets), len(self.kit40))
        self.assertTrue(all(t == norm_guid(tl.PLAYER) for t in additem_targets), additem_targets)

    def test_the_strip_refuses_the_hire_and_anything_that_is_not_a_kit_item(self):
        agent = tl.FakeAgent()
        ctx = tl.ctx_for(strip_ids=frozenset(gear.all_entries(self.kits)))
        with tempfile.TemporaryDirectory() as tmp:
            g = gm_guard.GmGuard(agent, tl.new_ledger(tmp), ctx)
            with self.assertRaises(gm_guard.GmRefused):
                g.send("additem_self", tl.HIRE, 7000, -1)
            with self.assertRaises(gm_guard.GmRefused):
                g.send("additem_self", tl.PLAYER, 999999, -1)
        self.assertEqual(agent.gm, [])

    def test_a_player_with_only_this_trials_kit_or_nothing_gets_no_gm_line(self):
        for worn in ((), {i["entry"] for i in self.kit50}):
            agent = ProfAgent(plate_need(self.kit50), worn=worn)
            strip, _gear_row, _n = self.strip_then_gear(agent, 50)
            self.assertTrue(strip["ok"])
            self.assertEqual([e for k, e in agent.events if k == "strip"], [], worn)

    def test_the_foreign_items_of_a_gear_trial_do_not_touch_items_that_belong_to_no_kit(self):
        agent = ProfAgent(plate_need(self.kit40), worn={999001} | {i["entry"] for i in self.kit40})
        self.strip_then_gear(agent, 50)
        self.assertIn(999001, agent.worn)

    def test_the_strip_read_back_waits_for_the_removal_to_show(self):
        agent = ProfAgent(plate_need(self.kit40), worn={i["entry"] for i in self.kit40}, strip_lag=3)
        agent.bags[7000] = 1
        # a single read right after the removals would still see the items (lag 3 reads): the wait must not
        strip, _gear_row, _n = self.strip_then_gear(agent, 50, wait=1.0)
        self.assertTrue(strip["ok"], strip["evidence"])
        self.assertEqual(strip["value"]["left"], 0)

    def test_run_setup_strips_the_old_kit_in_a_gear_trial_before_player_gear(self):
        pl = dict(player(level=50, role="dps"), stance=71)
        trial = {"id": "g", "player": pl, "bot": {"class": "mage"}, "situation": {"kind": "tank_damage"},
                 "setup_checks": []}
        agent = ProfAgent(plate_need(self.kit40 + self.kit50), worn={i["entry"] for i in self.kit40})
        ctx = kit_ctx(self.kit50, self.kits)
        real_gear = setup.check_player_gear
        real_strip = setup.strip_player_gear
        with tempfile.TemporaryDirectory() as tmp:
            guard = gm_guard.GmGuard(agent, tl.new_ledger(tmp), ctx)
            with unittest.mock.patch.multiple(
                    setup, check_level=lambda *a, **k: setup.row("player_level", True, 50, "ok"),
                    check_talents=lambda *a, **k: setup.row("player_talents", True, [], "ok"),
                    check_stance=lambda *a, **k: setup.row("player_stance", True, 71, "ok"),
                    check_forced_role=lambda *a, **k: setup.row("player_forced_role", True, "x", "ok"),
                    strip_player_gear=lambda *a, **k: real_strip(*a, wait=0.0, poll=0.0, **k),
                    check_player_gear=lambda a, t, g, c, **k: real_gear(a, t, g, c, kits=self.kits, settle=0.0,
                                                                        wait=0.0, poll=0.0, **k),
                    read_offhand=lambda a: {"readable": True, "item_entry": None, "evidence": "empty"}):
                result = setup.run_setup(agent, trial, guard, tl.new_ledger(tmp), ctx, first=False)
        ids = [c["id"] for c in result["checks"]]
        self.assertLess(ids.index("player_gear_carryover"), ids.index("player_gear"))
        self.assertTrue(all(c["ok"] for c in result["checks"] if c["id"].startswith("player_gear")), result["checks"])
        self.assertEqual(agent.worn, {i["entry"] for i in self.kit50})


if __name__ == "__main__":
    unittest.main()
