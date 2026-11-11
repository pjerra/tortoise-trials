"""P2: the test player is too weak for its arena.

Failing-first: the bear-bal pair still fights at delta 1 with no cap,
and shadow-form-prepull still has a gearless player with no healer.
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from trl import gear as gear_mod  # noqa: E402
from trl import scenario as scenario_mod  # noqa: E402


def load(name):
    with open(os.path.join(ROOT, "trials", "healers", name + ".json")) as f:
        return json.load(f)


class BearBalTwinsTest(unittest.TestCase):
    def test_bear_bal_twins_are_lighter_and_match(self):
        a = load("priest-holy-pws-bear-bal")
        b = load("priest-holy-pws-bear-bal-2tank")
        for t in (a, b):
            self.assertEqual(t["situation"]["mobs"]["level_delta"], -4, t["id"])
            self.assertEqual(t["situation"].get("max_rescues_per_min"), 12, t["id"])
            self.assertEqual(scenario_mod.validate(t), [], t["id"])
        self.assertEqual(a["situation"], b["situation"])


class ShadowFormHealerTest(unittest.TestCase):
    def test_shadow_form_has_a_healer_and_a_tank_kit(self):
        t = load("priest-shadow-form-prepull")
        self.assertEqual(t["player"]["forced_role"], "tank")
        self.assertEqual(t["player"]["gear"], "tier")
        items, lvl, why = gear_mod.pick_kit(t["player"])
        self.assertTrue(items, why)
        self.assertEqual(lvl, 40)
        self.assertEqual(t["player"].get("stance"), 71)
        extra = (t.get("extra_bots") or [None])[0]
        self.assertIsNotNone(extra)
        self.assertEqual((extra["class"], extra["spec"], extra["role"]), ("priest", "holy", "healer"))
        self.assertEqual(t["situation"]["mobs"]["level_delta"], -1)
        self.assertEqual(t["situation"]["floor_pct"], 12)
        self.assertEqual(t["situation"].get("max_rescues_per_min"), 12)
        for cid in ("player_gear", "extra_bots_present", "extra_bots_role", "extra_bots_in_group"):
            self.assertIn(cid, t["setup_checks"], cid)
        self.assertEqual(scenario_mod.validate(t), [], t["id"])


if __name__ == "__main__":
    unittest.main()
