"""player.gear: "tier": the guarded self-only grant, the kit pick by class, role and level, the setup check and
its failures, the validator, and the trials that turn it on."""

import json
import pathlib
import sys
import tempfile
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_live as tl  # noqa: E402
from trl import gear, scenario  # noqa: E402
from trl.live import gm_guard, setup  # noqa: E402


def player(klass="warrior", level=40, role="tank"):
    return {"class": klass, "level": level, "forced_role": role, "gear": "tier"}


class KitPickTest(unittest.TestCase):
    def test_the_kit_follows_level_class_and_role(self):
        for level in (40, 45, 50):
            items, kit_level, why = gear.pick_kit(player(level=level))
            self.assertEqual((kit_level, why), (level, ""))
            self.assertGreaterEqual(len(items), 10)
            self.assertTrue(all(i["required_level"] <= level for i in items), level)
        tank = {i["slot"] for i in gear.pick_kit(player(role="tank"))[0]}
        dps = {i["slot"] for i in gear.pick_kit(player(role="dps"))[0]}
        self.assertIn("offhand", tank)                       # a shield for the tank
        self.assertNotIn("offhand", dps)                     # none for a DPS player (it would read as a tank)
        self.assertIn("twohand", dps)

    def test_a_level_between_kits_takes_the_highest_kit_below_it_within_the_gap(self):
        self.assertEqual(gear.pick_kit(player(level=47))[1], 45)
        self.assertEqual(gear.pick_kit(player(level=49))[1], 45)

    def test_no_kit_is_a_clear_reason_not_an_empty_list(self):
        for pl, text in ((player(klass="mage"), "no mage tank gear kit"),
                         (player(level=39), "no warrior tank gear kit for level 39"),
                         (player(level=60), "within 9 levels of 60")):
            items, kit_level, why = gear.pick_kit(pl)
            self.assertIsNone(items)
            self.assertIn(text, why)
        self.assertIsNone(gear.pick_kit(player(), kits={})[0])

    def test_every_kit_is_one_item_per_slot_with_armor_and_no_duplicates(self):
        kits = gear.load_kits()
        for klass, roles in kits.items():
            for role, levels in roles.items():
                for level, items in levels.items():
                    slots = [i["slot"] for i in items]
                    self.assertEqual(len(slots), len(set(slots)), (klass, role, level))
                    self.assertEqual(len({i["entry"] for i in items}), len(items))
                    self.assertGreater(sum(i["armor"] for i in items), 2000, (klass, role, level))


class GuardTest(unittest.TestCase):
    def guard(self, tmp, kit):
        agent = tl.FakeAgent()
        ctx = tl.ctx_for(gear_ids=frozenset(kit))
        return agent, gm_guard.GmGuard(agent, tl.new_ledger(tmp), ctx)

    def test_a_kit_item_is_granted_to_the_player_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, g = self.guard(tmp, {7922})
            self.assertEqual(g.send("additem_self", tl.PLAYER, 7922, 1), ".additem 7922 1")
            self.assertEqual(agent.gm, [".additem 7922 1"])

    def test_a_kit_item_is_taken_away_one_at_a_time_from_the_player_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = tl.FakeAgent()
            g = gm_guard.GmGuard(agent, tl.new_ledger(tmp), tl.ctx_for(strip_ids=frozenset({7922})))
            self.assertEqual(g.send("additem_self", tl.PLAYER, 7922, -1), ".additem 7922 -1")
            for args in ((7922, -2), (12345, -1), (7922, 0)):
                with self.assertRaises(gm_guard.GmRefused):
                    g.send("additem_self", tl.PLAYER, *args)
            with self.assertRaises(gm_guard.GmRefused):
                g.send("additem_self", tl.HIRE, 7922, -1)
            with self.assertRaises(gm_guard.GmRefused):
                g.send_text(".additem 7922 -1", target=tl.PLAYER)       # the raw line is the hire kind: refused
            self.assertEqual(agent.gm, [".additem 7922 -1"])

    def test_anything_else_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, g = self.guard(tmp, {7922})
            with self.assertRaises(gm_guard.GmRefused):
                g.send("additem_self", tl.PLAYER, 12345, 1)         # not a kit entry
            with self.assertRaises(gm_guard.GmRefused):
                g.send("additem_self", tl.PLAYER, 7922, 2)          # once only
            with self.assertRaises(gm_guard.GmRefused):
                g.send("additem_self", tl.HIRE, 7922, 1)            # self only
            with self.assertRaises(gm_guard.GmRefused):
                g.send_text(".additem 7922 1", target=tl.PLAYER)    # the raw line is still the hire kind
            self.assertEqual(agent.gm, [])

    def test_the_hire_additem_still_refuses_the_player(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = tl.FakeAgent()
            g = gm_guard.GmGuard(agent, tl.new_ledger(tmp), tl.ctx_for(item_ids=frozenset({1708})))
            with self.assertRaises(gm_guard.GmRefused):
                g.send("additem", tl.PLAYER, 1708, 1)


class GearAgent(tl.FakeAgent):
    """A player's items: `.additem N` puts a copy in the bags, `.additem N -1` takes one away (worn first, like
    the core's DestroyItemCount), `equip` wears one from the bags (an entry in refuse_equip is refused)."""

    def __init__(self, worn=(), bags=(), refuse_equip=(), equipment_error=False, inventory_error=False):
        super().__init__()
        self.worn, self.bags = set(worn), {int(e): 1 for e in bags}
        self.refuse_equip, self.equipment_error = set(refuse_equip), equipment_error
        self.inventory_error = inventory_error
        self.equips = []

    def rpc(self, method, params=None, timeout=None):
        if method == "gm" and (params or {}).get("text", "").startswith(".additem"):
            _cmd, entry, count = params["text"].split()
            entry, count = int(entry), int(count)
            if count > 0:
                self.bags[entry] = self.bags.get(entry, 0) + count
            elif entry in self.worn:
                self.worn.discard(entry)
            elif self.bags.get(entry):
                self.bags[entry] -= 1
                if not self.bags[entry]:
                    del self.bags[entry]
        elif method == "equipment":
            if self.equipment_error:
                return {"_err": "unknown method: equipment"}
            return [{"slot": i, "item_entry": e} for i, e in enumerate(sorted(self.worn))]
        elif method == "inventory":
            if self.inventory_error:
                return {"_err": "unknown method: inventory"}
            return [{"bag": 255, "slot": 23 + i, "item_entry": e, "count": c}
                    for i, (e, c) in enumerate(sorted(self.bags.items()))]
        elif method == "equip":
            entry = int(params["item_entry"])
            self.equips.append(entry)
            if entry in self.refuse_equip or not self.bags.get(entry):
                return {"_err": "cannot equip item %d" % entry}
            self.bags[entry] -= 1
            if not self.bags[entry]:
                del self.bags[entry]
            self.worn.add(entry)
            return {"entry": entry}
        return super().rpc(method, params, timeout)

    def additem_lines(self):
        return [g for g in self.gm if g.startswith(".additem")]


class SetupCheckTest(unittest.TestCase):
    TRIAL = {"id": "t", "player": player(level=50, role="tank")}

    def ctx(self, trial=None, kits=None):
        kit = [i["entry"] for i in (gear.pick_kit((trial or self.TRIAL)["player"], kits)[0] or [])]
        return tl.ctx_for(gear_ids=frozenset(kit), strip_ids=frozenset(gear.all_entries()))

    def run_check(self, agent, trial=None, kits=None):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.ctx(trial, kits)
            return setup.check_player_gear(agent, trial or self.TRIAL,
                                           gm_guard.GmGuard(agent, tl.new_ledger(tmp), ctx), ctx, kits=kits, settle=0.0, wait=0.0, poll=0.0)

    def test_the_kit_is_granted_equipped_and_verified(self):
        agent = GearAgent()
        row = self.run_check(agent)
        items = gear.pick_kit(self.TRIAL["player"])[0]
        self.assertTrue(row["ok"], row["evidence"])
        self.assertEqual(row["value"]["worn"], len(items))
        self.assertEqual(row["value"]["armor"], sum(i["armor"] for i in items))
        self.assertIn("%d/%d items worn" % (len(items), len(items)), row["evidence"])
        self.assertEqual(len(agent.additem_lines()), len(items))

    def test_an_item_already_worn_is_not_granted_again(self):
        items = gear.pick_kit(self.TRIAL["player"])[0]
        agent = GearAgent(worn={items[0]["entry"]})
        row = self.run_check(agent)
        self.assertTrue(row["ok"])
        self.assertEqual(len(agent.additem_lines()), len(items) - 1)

    def test_a_kit_already_in_the_bags_is_equipped_without_a_new_grant(self):
        # a repeat (or an earlier gear trial) left the kit in the bags: equip it, grant nothing, no duplicates
        items = gear.pick_kit(self.TRIAL["player"])[0]
        agent = GearAgent(bags=[i["entry"] for i in items])
        row = self.run_check(agent)
        self.assertTrue(row["ok"], row["evidence"])
        self.assertEqual(agent.additem_lines(), [])
        self.assertEqual(agent.bags, {})
        self.assertIn("0 granted, %d equipped from the bags" % len(items), row["evidence"])

    def test_a_second_run_of_the_same_check_grants_nothing(self):
        agent = GearAgent()
        self.run_check(agent)
        granted = len(agent.additem_lines())
        row = self.run_check(agent)
        self.assertTrue(row["ok"])
        self.assertEqual(len(agent.additem_lines()), granted)

    def test_a_missing_kit_fails_clearly_and_grants_nothing(self):
        agent = GearAgent()
        trial = {"id": "t", "player": player(klass="mage", level=50)}
        row = self.run_check(agent, trial)
        self.assertFalse(row["ok"])
        self.assertIn("no mage tank gear kit", row["evidence"])
        self.assertEqual(agent.additem_lines(), [])

    def test_an_item_that_will_not_equip_is_named(self):
        items = gear.pick_kit(self.TRIAL["player"])[0]
        agent = GearAgent(refuse_equip={items[3]["entry"]})
        row = self.run_check(agent)
        self.assertFalse(row["ok"])
        self.assertIn(items[3]["name"], row["evidence"])
        self.assertIn("equip errors", row["evidence"])

    def test_an_unreadable_equipment_or_inventory_list_fails_instead_of_passing(self):
        row = self.run_check(GearAgent(equipment_error=True))
        self.assertFalse(row["ok"])
        self.assertIn("cannot verify the gear", row["evidence"])
        row = self.run_check(GearAgent(inventory_error=True))
        self.assertFalse(row["ok"])
        self.assertIn("cannot read the bags", row["evidence"])


class StripTest(unittest.TestCase):
    """A no-gear trial after a gear trial on the same slot character starts with no kit on the player."""

    def strip(self, agent):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = tl.ctx_for(strip_ids=frozenset(gear.all_entries()))
            return setup.strip_player_gear(agent, gm_guard.GmGuard(agent, tl.new_ledger(tmp), ctx), ctx)

    def test_a_gear_trial_then_a_no_gear_trial_ends_with_nothing_of_the_kit_worn(self):
        agent = GearAgent()
        with tempfile.TemporaryDirectory() as tmp:
            ctx = tl.ctx_for(gear_ids=frozenset(gear.kit_entries(player(level=50))),
                             strip_ids=frozenset(gear.all_entries()))
            guard = gm_guard.GmGuard(agent, tl.new_ledger(tmp), ctx)
            row = setup.check_player_gear(agent, {"id": "g", "player": player(level=50)}, guard, ctx, settle=0.0, wait=0.0, poll=0.0)
            self.assertTrue(row["ok"])
            self.assertGreaterEqual(len(agent.worn), 10)
            agent.bags[7922] = agent.bags.get(7922, 0) + 1          # a leftover copy in the bags too
            row = setup.strip_player_gear(agent, guard, ctx)         # the next trial has no gear option
        self.assertTrue(row["ok"], row["evidence"])
        self.assertEqual(agent.worn & gear.all_entries(), set())
        self.assertEqual(agent.bags, {})
        self.assertEqual(row["value"]["left"], 0)
        self.assertGreaterEqual(row["value"]["removed"], 11)

    def test_run_setup_strips_a_no_gear_trial_and_equips_a_gear_trial(self):
        # a gear trial, then a no-gear trial, through run_setup on one character
        def run_one(agent, tmp, trial):
            ctx = tl.ctx_for(gear_ids=frozenset(gear.kit_entries(trial["player"])
                                                if trial["player"].get("gear") == "tier" else ()),
                             strip_ids=frozenset(gear.all_entries()))
            guard = gm_guard.GmGuard(agent, tl.new_ledger(tmp), ctx)
            with unittest.mock.patch.multiple(
                    setup, check_level=lambda *a, **k: setup.row("player_level", True, 40, "ok"),
                    check_talents=lambda *a, **k: setup.row("player_talents", True, [], "ok"),
                    check_stance=lambda *a, **k: setup.row("player_stance", True, 71, "ok"),
                    check_forced_role=lambda *a, **k: setup.row("player_forced_role", True, "x", "ok"),
                    read_offhand=lambda a: {"readable": True, "item_entry": None, "evidence": "empty"}):
                return setup.run_setup(agent, trial, guard, tl.new_ledger(tmp), ctx, first=False)

        agent = GearAgent()
        gear_trial = {"id": "g", "player": dict(player(level=40, role="tank"), stance=71),
                      "bot": {"class": "mage"}, "situation": {"kind": "tank_damage"}, "setup_checks": []}
        plain_trial = {"id": "p", "player": {"class": "warrior", "level": 40, "forced_role": "dps", "stance": 2457},
                       "bot": {"class": "priest"}, "situation": {"kind": "tank_damage"}, "setup_checks": []}
        with tempfile.TemporaryDirectory() as tmp:
            first = run_one(agent, tmp, gear_trial)
            ids = [c["id"] for c in first["checks"]]
            self.assertIn("player_gear", ids)
            self.assertNotIn("player_gear_strip", ids)
            self.assertGreaterEqual(len(agent.worn), 10)
            second = run_one(agent, tmp, plain_trial)
        ids = [c["id"] for c in second["checks"]]
        self.assertIn("player_gear_strip", ids)          # recorded in setup.json
        self.assertNotIn("player_gear", ids)
        strip = [c for c in second["checks"] if c["id"] == "player_gear_strip"][0]
        self.assertTrue(strip["ok"], strip["evidence"])
        self.assertEqual(agent.worn & gear.all_entries(), set())     # nothing of the kit on before the healer's checks

    def test_other_worn_items_are_not_touched_and_are_counted(self):
        agent = GearAgent(worn={999001, 999002, 7922})
        row = self.strip(agent)
        self.assertTrue(row["ok"])
        self.assertEqual(agent.worn, {999001, 999002})
        self.assertEqual(row["value"]["other_worn"], 2)
        self.assertIn("2 other item(s) worn", row["evidence"])

    def test_a_clean_player_gets_no_gm_line(self):
        agent = GearAgent()
        row = self.strip(agent)
        self.assertTrue(row["ok"])
        self.assertEqual(agent.additem_lines(), [])

    def test_an_unreadable_agent_is_unverified_not_failed(self):
        row = self.strip(GearAgent(equipment_error=True))
        self.assertTrue(row["ok"])
        self.assertIn("UNVERIFIED", row["evidence"])

    def test_a_kit_item_that_will_not_go_fails_the_row(self):
        agent = GearAgent(worn={7922})
        orig = agent.rpc

        def stubborn(method, params=None, timeout=None):
            if method == "gm" and (params or {}).get("text", "").startswith(".additem 7922 -1"):
                return {}
            return orig(method, params, timeout)
        agent.rpc = stubborn
        row = self.strip(agent)
        self.assertFalse(row["ok"])
        self.assertIn("7922", row["evidence"])


class ValidatorAndTrialsTest(unittest.TestCase):
    def errors(self, **over):
        with open(str(ROOT / "trials" / "dps" / "dps-mage-threat-cap.json")) as f:
            trial = json.load(f)
        trial["player"].update(over)
        return [e for e in scenario.validate(trial) if "gear" in e]

    def test_the_validator(self):
        self.assertEqual(self.errors(gear="tier"), [])
        self.assertEqual(self.errors(gear=None), [])
        self.assertTrue(self.errors(gear="blue"))
        self.assertTrue(self.errors(gear="tier", **{"class": "mage"}))        # no mage kit: the trial fails validation
        self.assertTrue(self.errors(gear="tier", level=30))

    def test_every_tank_and_dps_trial_turns_it_on_with_a_kit_that_fits(self):
        files = sorted(ROOT.glob("trials/dps/*.json")) + sorted(ROOT.glob("trials/tanks/*.json"))
        self.assertGreaterEqual(len(files), 6)
        for path in files:
            trial = json.loads(path.read_text())
            self.assertEqual(trial["player"].get("gear"), "tier", path.name)
            self.assertIn("player_gear", trial["setup_checks"], path.name)
            items, _lvl, why = gear.pick_kit(trial["player"])
            self.assertTrue(items, (path.name, why))
            # a DPS-role player (the tank-* trials) gets no shield: it would read as a tank
            if trial["player"].get("forced_role") != "tank":
                self.assertNotIn("offhand", [i["slot"] for i in items], path.name)


class RunnerWiringTest(unittest.TestCase):
    def test_run_repeat_hands_the_kit_entries_to_the_guard_context(self):
        import test_cleanup_wiring as cw
        from trl.live import run
        captured = []

        def fake_setup(agent, trial, guard, ledger, ctx, **kw):
            captured.append(ctx)
            return {"ok": False, "checks": [], "invalid_reason": "stop here", "guids": {}, "bot_name": None,
                    "config": {}}

        with tempfile.TemporaryDirectory() as tmp:
            h = cw.Harness(tmp)
            with open(str(ROOT / "trials" / "dps" / "dps-rogue-kick-caster.json")) as f:
                h.trial = json.load(f)
            with unittest.mock.patch.object(run, "run_setup", fake_setup), \
                 unittest.mock.patch.object(run.Run, "pull_evpull", lambda *a, **k: None):
                h.r.run_repeat(h.session, h.trial, 1)
        self.assertEqual(captured[0].gear_ids, frozenset(gear.kit_entries(h.trial["player"])))
        self.assertGreaterEqual(len(captured[0].gear_ids), 10)

    def test_a_trial_without_the_option_has_no_gear_ids(self):
        import test_cleanup_wiring as cw
        from trl.live import run
        captured = []
        with tempfile.TemporaryDirectory() as tmp:
            h = cw.Harness(tmp)
            with unittest.mock.patch.object(run, "run_setup", lambda a, t, g, l, ctx, **kw: captured.append(ctx) or
                                            {"ok": False, "checks": [], "invalid_reason": "x", "guids": {},
                                             "bot_name": None, "config": {}}), \
                 unittest.mock.patch.object(run.Run, "pull_evpull", lambda *a, **k: None):
                h.r.run_repeat(h.session, h.trial, 1)
        self.assertEqual(captured[0].gear_ids, frozenset())


if __name__ == "__main__":
    unittest.main()
