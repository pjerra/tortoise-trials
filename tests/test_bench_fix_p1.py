"""P1: the mage threat cap is measured against a hired bot tank.

Failing-first: the trial still has a player tank (stance 71, forced tank role),
extra_bots has no gear field, and run_extras sends no autogear.
"""

import copy
import json
import os
import sys
import tempfile
import unittest
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import test_live as tl  # noqa: E402
import test_rules_tanks_dps as td  # noqa: E402
from trl import gear as gear_mod  # noqa: E402
from trl import scenario as scenario_mod  # noqa: E402
from trl.live import gm_guard, setup  # noqa: E402

TRIAL_PATH = os.path.join(ROOT, "trials", "dps", "dps-mage-threat-cap.json")


def load():
    with open(TRIAL_PATH) as f:
        return json.load(f)


class ExtraBotGearFieldTest(unittest.TestCase):
    def test_extra_bot_gear_field_validates(self):
        trial = load()
        trial["extra_bots"] = [dict(trial["extra_bots"][0], gear="blue")]
        self.assertEqual(scenario_mod.validate(trial), [])

    def test_extra_bot_gear_rejects_non_word(self):
        trial = load()
        trial["extra_bots"] = [dict(trial["extra_bots"][0], gear=3)]
        errs = scenario_mod.validate(trial)
        self.assertTrue(any("gear" in e for e in errs), errs)


class RunExtrasAutogearTest(unittest.TestCase):
    def run_extras(self, extras, reply="Re-geared to blue"):
        agent = tl.FakeAgent()
        sent = []

        def fake_tbm(command, via=None, deadline=None):
            sent.append(command)
            return 0

        agent.tbm = fake_tbm
        agent.wait_reply = lambda since, pattern, timeout=20: (reply, [])
        trial = {"id": "t", "extra_bots": extras}
        with tempfile.TemporaryDirectory() as tmp:
            led = tl.new_ledger(tmp)
            ctx = gm_guard.GmContext(player_guid=tl.PLAYER, hire_guid=tl.HIRE)
            ctx.extra = {"extra_names": [], "extra_slots": []}
            ctx.extra_hire_guids = frozenset()
            guard = gm_guard.GmGuard(agent, led, ctx)
            with unittest.mock.patch.object(setup, "_hire",
                                            lambda a, eb, l, t, prev=None, extra=None:
                                            ({"ok": True, "evidence": "hired"}, "Helper%d" % (extra or 1))), \
                 unittest.mock.patch.object(setup, "party_guid", lambda a, n: "0x0000000000000003"), \
                 unittest.mock.patch.object(setup, "bring_hire", lambda *a, **k: True), \
                 unittest.mock.patch.object(setup, "check_bot_talents", lambda *a, **k: None), \
                 unittest.mock.patch.object(setup, "_apply_role",
                                             lambda a, spec, name, ledger, *rest, **kw: {"ok": True, "evidence": "role"}), \
                 unittest.mock.patch.object(setup, "_check_strategy",
                                             lambda a, spec, name, *rest, **kw: {"ok": True, "evidence": "strat"}), \
                 unittest.mock.patch.object(setup, "check_extras_grouped",
                                             lambda a, names: {"id": "extra_bots_in_group", "ok": True,
                                                               "evidence": "grouped"}):
                rows, names, guids = setup.run_extras(agent, trial, guard, led, ctx)
        return sent, rows, led

    def test_autogear_sent_after_role(self):
        sent, rows, led = self.run_extras(
            [{"class": "warrior", "spec": "protection", "role": "tank", "level": 40, "gear": "blue",
              "items": []}])
        self.assertTrue(any(c.startswith("autogear Helper1 blue") for c in sent), sent)
        role_rows = [r for r in rows if r["id"] == "extra_bots_role"]
        self.assertTrue(role_rows and role_rows[0]["ok"], rows)
        kinds = [r.get("kind") for r in led.rows]
        self.assertIn("autogear", kinds)

    def test_autogear_refusal_fails_role(self):
        sent, rows, led = self.run_extras(
            [{"class": "warrior", "spec": "protection", "role": "tank", "level": 40, "gear": "blue",
              "items": []}], reply="refused: no such set")
        role_rows = [r for r in rows if r["id"] == "extra_bots_role"]
        self.assertTrue(role_rows and not role_rows[0]["ok"], rows)
        self.assertIn("refused", role_rows[0]["evidence"], rows)

    def test_no_gear_sends_no_autogear(self):
        sent, rows, led = self.run_extras(
            [{"class": "priest", "spec": "holy", "role": "healer", "level": 40, "items": []}])
        self.assertFalse([c for c in sent if c.startswith("autogear")], sent)


class ThreatCapBotTankTest(unittest.TestCase):
    def test_threat_cap_uses_a_bot_tank(self):
        trial = load()
        merged = {"base": trial}
        merged.update({n: m for n, m in scenario_mod.expand_variants(trial)})
        for name, m in merged.items():
            extra = m["extra_bots"][0]
            self.assertEqual((extra["class"], extra["spec"], extra["role"], extra.get("gear")),
                             ("warrior", "protection", "tank", "blue"), name)
            self.assertEqual(m["player"]["forced_role"], "dps", name)
            self.assertEqual(m["player"]["stance"], 2457, name)
            self.assertIn("player_no_shield", m["setup_checks"], name)
            ids = [r["id"] for r in m["rules"]]
            self.assertIn("DPS-MAG-THREAT-TANK-HELD", ids, name)
            self.assertIn("DPS-MAG-THREAT-TANK-FOUGHT", ids, name)
            self.assertEqual(scenario_mod.validate(dict(m, id=trial["id"])), [], name)
            for rule in m["rules"]:
                for key in ("desc", "why_expect"):
                    self.assertNotIn("player tank", rule.get(key) or "", "%s/%s" % (name, rule["id"]))
        lvl60 = merged["lvl60"]
        self.assertIsNone(lvl60["player"].get("gear"))
        self.assertEqual(lvl60["situation"]["mobs"]["level_delta"], -2)
        self.assertEqual(trial["repeats"], 4)

    def test_tank_held_ratio(self):
        trial = load()
        merged = dict(scenario_mod.expand_variants(trial))
        base = dict(trial, id=trial["id"])
        for m in [base] + list(merged.values()):
            ids = [r["id"] for r in m["rules"]]
            if "DPS-MAG-THREAT-TANK-HELD" not in ids:
                continue
        held = [r for r in trial["rules"] if r["id"] == "DPS-MAG-THREAT-TANK-HELD"]
        self.assertTrue(held, "trial needs the TANK-HELD rule first")
        held = held[0]
        self.assertEqual(held["check"]["num"], {"kind": "damage", "src": "$mob", "dst": "$extra1"})
        self.assertEqual(held["check"]["den"], {"kind": "damage", "src": "$mob"})
        self.assertEqual(held["pass_if"], ">= 0.6")
        self.assertEqual(held["check"].get("min_n"), 20)

    def test_no_level_60_kit(self):
        items, lvl, why = gear_mod.pick_kit({"class": "warrior", "level": 60, "forced_role": "dps"})
        self.assertIsNone(items)
        self.assertIn("within 9 levels", why)

    def test_kits_are_vanilla_entries(self):
        import build_data
        self.assertEqual(build_data.GEAR_MAX_ENTRY, 20000)
        kits = gear_mod.load_kits()
        for klass, roles in kits.items():
            for role, levels in (roles or {}).items():
                for lvl, items in (levels or {}).items():
                    for item in items or []:
                        self.assertLess(int(item["entry"]), 20000, item)


class ThreatCapHeldScoringTest(td.RepMixin, unittest.TestCase):
    def run_rules(self, tid, events, samples):
        return self.rules(tid, events, samples)

    def test_tank_held_pass_and_no_aggro_fail(self):
        tid = "dps-mage-threat-cap"
        samples = td.series(200, player=(2457,))
        rows = ([td.event(10 + i, "damage", td.MOB, td.EXTRA1, None) for i in range(30)]
                + [td.event(50 + i, "damage", td.MOB, td.PLAYER, None) for i in range(5)]
                + [td.event(70 + i, "damage", td.MOB, td.BOT, None) for i in range(2)])
        # 30 mob hits on extra1, 5 on the player, 2 on the mage
        got = self.rules(tid, rows, samples)
        self.assertIn("DPS-MAG-THREAT-TANK-HELD", got, got.keys())
        self.assertEqual(got["DPS-MAG-THREAT-TANK-HELD"].get("verdict"), "PASS", got)
        self.assertEqual(got["DPS-MAG-THREAT-NO-AGGRO"].get("verdict"), "FAIL", got)

    def test_tank_held_inconclusive_below_min_n(self):
        tid = "dps-mage-threat-cap"
        samples = td.series(200, player=(2457,))
        rows = [td.event(10 + i, "damage", td.MOB, td.EXTRA1, None) for i in range(12)]
        got = self.rules(tid, rows, samples)
        self.assertEqual(got["DPS-MAG-THREAT-TANK-HELD"].get("verdict"), "INCONCLUSIVE", got)


if __name__ == "__main__":
    unittest.main()
