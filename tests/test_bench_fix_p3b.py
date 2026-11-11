"""P3b: heal latency gets its chance via top_up_to_pct.

Failing-first: damage_step takes no to_pct, and the schema rejects top_up_to_pct.
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from trl import scenario as scenario_mod  # noqa: E402
from trl.live import situations as sit_mod  # noqa: E402


def load(group, name):
    with open(os.path.join(ROOT, "trials", group, name + ".json")) as f:
        return json.load(f)


class DamageStepTest(unittest.TestCase):
    def test_damage_step_to_pct(self):
        self.assertEqual(sit_mod.damage_step(2000, 2000, 40, 80, to_pct=45), 1100)

    def test_damage_step_default_unchanged(self):
        # pins tests/test_live.py:709-712
        self.assertEqual(sit_mod.damage_step(900, 1000, 40, 90), 0)
        self.assertEqual(sit_mod.damage_step(950, 1000, 40, 90), 300)
        self.assertLessEqual(sit_mod.damage_step(990, 1000, 40, 90), 990 - 400)
        self.assertEqual(sit_mod.damage_step(450, 1000, 40, 90), 0)


class GovernorTopUpTest(unittest.TestCase):
    def test_governor_top_up_crosses_the_trigger(self):
        import test_live as tl
        with tempfile.TemporaryDirectory() as tmp:
            a = tl.FakeAgent(status={"state": "in_world", "guid": tl.PLAYER,
                                     "health": 1700, "max_health": 2000, "target": tl.PLAYER})
            a.unit_rows[tl.PLAYER] = {"guid": tl.PLAYER, "health": 1700, "max_health": 2000}
            led = tl.new_ledger(tmp)
            ctx = tl.ctx_for(band_low=40)
            from trl.live import gm_guard
            g = gm_guard.GmGuard(a, led, ctx)
            marks = tl.FakeMarks()
            gov = sit_mod.Governor(a, g, marks, victim="player", band=(40, 80), floor=15,
                                   victim_guid=tl.PLAYER, top_up_to=45)
            gov.tick({"hp": 1700, "max_hp": 2000})
            self.assertIn(".damage 800", a.gm)
            a.gm.clear()
            gov2 = sit_mod.Governor(a, g, marks, victim="player", band=(40, 80), floor=15,
                                    victim_guid=tl.PLAYER, top_up_to=45)
            gov2.tick({"hp": 1580, "max_hp": 2000})
            self.assertEqual(a.gm, [])

class TopUpBoundsTest(unittest.TestCase):
    def test_top_up_bounds_validate(self):
        trial = load("healers", "priest-holy-heal-latency")
        trial["situation"]["top_up_to_pct"] = 85
        self.assertTrue(any("top_up_to_pct" in e for e in scenario_mod.validate(trial)))
        trial["situation"]["top_up_to_pct"] = 35
        self.assertTrue(any("top_up_to_pct" in e for e in scenario_mod.validate(trial)))
        trial["situation"]["top_up_to_pct"] = 45
        self.assertEqual(scenario_mod.validate(trial), [])


class HealLatencyChanceTest(unittest.TestCase):
    def test_heal_latency_trials_make_the_chance(self):
        for group, tid in (("healers", "priest-holy-heal-latency"),
                           ("healers", "paladin-holy-heal-latency")):
            trial = load(group, tid)
            sit = trial["situation"]
            trig = trial["rules"][0]["check"]["trigger"]
            self.assertIn("top_up_to_pct", sit)
            self.assertLess(sit["top_up_to_pct"], trig["below"])
            self.assertEqual(trial["rules"][0].get("min_reps"), 3)
            self.assertEqual(scenario_mod.validate(trial), [])


if __name__ == "__main__":
    unittest.main()
