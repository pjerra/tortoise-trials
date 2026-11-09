"""player.gear: "tier": the armor and weapon proficiencies a kit needs are learned before the equip, a kit left
by an earlier trial is stripped before this trial's own kit, and a slow server (a grant or an equip that
shows late) no longer leaves one piece "unworn"."""

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_live as tl  # noqa: E402
from test_gear import GearAgent, player  # noqa: E402
from trl import gear  # noqa: E402
from trl.live import gm_guard, setup  # noqa: E402

PLATE_MAIL, MAIL_ARMOR = 750, 8737


def fake_kit(item_class, subclass, base, n=3, back_cloth=False):
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
    refused, and a spell the player has is listed by `spells`. `need` maps entry -> proficiency spell."""

    def __init__(self, need, known=(), grant_lag=0, equip_lag=0, **kw):
        super().__init__(**kw)
        self.need = dict(need)
        self.spells_known = set(known)
        self.events = []                       # ("learn", id) / ("grant", id) / ("equip", id) in order
        self.grant_lag, self.equip_lag = grant_lag, equip_lag
        self._hidden = {}                      # entry -> inventory reads left before a grant shows
        self._pending = {}                     # entry -> equipment reads left before an equip shows

    def rpc(self, method, params=None, timeout=None):
        if method == "gm":
            text = (params or {}).get("text", "")
            if text.startswith(".learn"):
                self.spells_known.add(int(text.split()[1]))
                self.events.append(("learn", int(text.split()[1])))
                return super().rpc("gm", params, timeout)
            if text.startswith(".additem"):
                entry, count = int(text.split()[1]), int(text.split()[2])
                self.events.append(("grant" if count > 0 else "strip", entry))
                if count > 0 and self.grant_lag:
                    self._hidden[entry] = self.grant_lag
        if method == "inventory":
            out = super().rpc(method, params, timeout)
            if isinstance(out, list):
                for entry in list(self._hidden):
                    self._hidden[entry] -= 1
                shown = [r for r in out if self._hidden.get(r["item_entry"], 0) <= 0]
                for entry in [e for e, n in self._hidden.items() if n <= 0]:
                    del self._hidden[entry]
                return shown
            return out
        if method == "equipment":
            for entry in list(self._pending):
                self._pending[entry] -= 1
                if self._pending[entry] < 0:
                    self.worn.add(entry)
                    del self._pending[entry]
            return super().rpc(method, params, timeout)
        if method == "equip":
            entry = int(params["item_entry"])
            if entry in self._hidden:                          # the bag has not shown the grant yet
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


def run_gear(agent, kit_player, kits, gear_ids=None, **kw):
    items = gear.pick_kit(kit_player, kits)[0]
    ids = frozenset(i["entry"] for i in items) if gear_ids is None else frozenset(gear_ids)
    ctx = tl.ctx_for(gear_ids=ids, strip_ids=frozenset(gear.all_entries(kits)))
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

    def test_a_kit_entry_without_class_data_falls_back_to_its_slot(self):
        kit = [{"slot": "head", "entry": 7400, "name": "Old Helm", "armor": 100, "required_level": 40},
               {"slot": "back", "entry": 7401, "name": "Old Cloak", "armor": 10, "required_level": 40}]
        self.assertEqual(gear.proficiency_spells(kit), [PLATE_MAIL])

    def test_the_shipped_warrior_kits_carry_class_data_and_need_plate_and_their_weapon(self):
        for role, levels in gear.load_kits()["warrior"].items():
            for level, items in levels.items():
                for item in items:
                    self.assertIn("item_class", item, (role, level, item["entry"]))
                    self.assertIn("subclass", item, (role, level, item["entry"]))
                self.assertIn(PLATE_MAIL, gear.proficiency_spells(items), (role, level))

    def test_the_guard_learns_a_proficiency_for_the_player_only_and_nothing_else(self):
        agent = tl.FakeAgent()
        with tempfile.TemporaryDirectory() as tmp:
            g = gm_guard.GmGuard(agent, tl.new_ledger(tmp), tl.ctx_for())
            g.send("learn", tl.PLAYER, PLATE_MAIL)
            with self.assertRaises(gm_guard.GmRefused):
                g.send("learn", tl.PLAYER, 12345)            # not a trial spell, not a proficiency
            with self.assertRaises(gm_guard.GmRefused):
                g.send("learn", tl.HIRE, PLATE_MAIL)         # never the hire
        self.assertEqual(agent.gm, [".learn 750"])

    def test_a_proficiency_that_will_not_learn_fails_the_row_and_says_so(self):
        kit = fake_kit(4, 4, 7000)
        agent = ProfAgent(plate_need(kit))
        orig = agent.rpc

        def never(method, params=None, timeout=None):
            if method == "gm" and (params or {}).get("text", "").startswith(".learn"):
                return {}                                    # the server ignores it
            return orig(method, params, timeout)
        agent.rpc = never
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

    def test_the_read_back_waits_for_the_equip_to_show_on_the_body(self):
        kit = fake_kit(4, 4, 7000)
        agent = ProfAgent(plate_need(kit), equip_lag=2)
        row = run_gear(agent, player(level=40, role="dps"), kits_of(kit), wait=1.0)
        self.assertTrue(row["ok"], row["evidence"])
        self.assertEqual(len(agent.worn), len(kit))

    def test_an_equip_the_server_dropped_is_sent_again(self):
        kit = fake_kit(4, 4, 7000)
        agent = ProfAgent(plate_need(kit))
        orig, dropped = agent.rpc, []

        def drop_first(method, params=None, timeout=None):
            if method == "equip" and not dropped:
                dropped.append(params["item_entry"])
                return {"entry": params["item_entry"]}      # accepted, never applied
            return orig(method, params, timeout)
        agent.rpc = drop_first
        row = run_gear(agent, player(level=40, role="dps"), kits_of(kit))
        self.assertTrue(row["ok"], row["evidence"])
        self.assertEqual(len(agent.worn), len(kit))
        self.assertEqual(agent.equips.count(dropped[0]), 1)


class CarryOverTest(unittest.TestCase):
    """A level-40 kit left on the slot character is stripped before a level-50 kit goes on, whatever the gear mode."""

    def setUp(self):
        self.kit40 = fake_kit(4, 4, 7000)
        self.kit50 = fake_kit(4, 4, 8000)
        self.kits = kits_of(self.kit40, self.kit40, self.kit50)      # 40, 50 (own kit), 60 -> pick by level below
        self.kits = {"warrior": {"dps": {"40": self.kit40, "50": self.kit50}}}

    def strip_then_gear(self, agent, level):
        pl = player(level=level, role="dps")
        keep = frozenset(i["entry"] for i in gear.pick_kit(pl, self.kits)[0])
        ctx = tl.ctx_for(gear_ids=keep, strip_ids=frozenset(gear.all_entries(self.kits)))
        with tempfile.TemporaryDirectory() as tmp:
            guard = gm_guard.GmGuard(agent, tl.new_ledger(tmp), ctx)
            strip = setup.strip_player_gear(agent, guard, ctx, keep=set(keep), row_id="player_gear_carryover")
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

    def test_the_strip_goes_to_the_player_only(self):
        agent = ProfAgent(plate_need(self.kit40), worn={i["entry"] for i in self.kit40})
        self.strip_then_gear(agent, 50)
        additem_targets = [t for text, t in agent.gm_at if text.startswith(".additem") and text.endswith("-1")]
        self.assertEqual(len(additem_targets), len(self.kit40))
        self.assertTrue(all(t == agent_norm(tl.PLAYER) for t in additem_targets), additem_targets)

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

    def test_run_setup_strips_the_old_kit_in_a_gear_trial_before_player_gear(self):
        import unittest.mock
        pl = dict(player(level=50, role="dps"), stance=71)
        trial = {"id": "g", "player": pl, "bot": {"class": "mage"}, "situation": {"kind": "tank_damage"},
                 "setup_checks": []}
        agent = ProfAgent(plate_need(self.kit40 + self.kit50), worn={i["entry"] for i in self.kit40})
        keep = frozenset(i["entry"] for i in self.kit50)
        ctx = tl.ctx_for(gear_ids=keep, strip_ids=frozenset(gear.all_entries(self.kits)))
        real_gear = setup.check_player_gear
        with tempfile.TemporaryDirectory() as tmp:
            guard = gm_guard.GmGuard(agent, tl.new_ledger(tmp), ctx)
            with unittest.mock.patch.multiple(
                    setup, check_level=lambda *a, **k: setup.row("player_level", True, 50, "ok"),
                    check_talents=lambda *a, **k: setup.row("player_talents", True, [], "ok"),
                    check_stance=lambda *a, **k: setup.row("player_stance", True, 71, "ok"),
                    check_forced_role=lambda *a, **k: setup.row("player_forced_role", True, "x", "ok"),
                    check_player_gear=lambda a, t, g, c, **k: real_gear(a, t, g, c, kits=self.kits, settle=0.0,
                                                                        wait=0.0, poll=0.0),
                    read_offhand=lambda a: {"readable": True, "item_entry": None, "evidence": "empty"}):
                result = setup.run_setup(agent, trial, guard, tl.new_ledger(tmp), ctx, first=False)
        ids = [c["id"] for c in result["checks"]]
        self.assertLess(ids.index("player_gear_carryover"), ids.index("player_gear"))
        self.assertTrue(all(c["ok"] for c in result["checks"] if c["id"].startswith("player_gear")), result["checks"])
        self.assertEqual(agent.worn, {i["entry"] for i in self.kit50})


def agent_norm(guid):
    from trl.live.agent import norm_guid
    return norm_guid(guid)


if __name__ == "__main__":
    unittest.main()
