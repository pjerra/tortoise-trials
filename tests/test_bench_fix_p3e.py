"""P3e: paladin defensives runs at 50 with a per-rep cooldown reset."""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from trl import scenario as scenario_mod  # noqa: E402


def load():
    with open(os.path.join(ROOT, "trials", "tanks", "tank-paladin-defensives.json")) as f:
        return json.load(f)


class PaladinDefensivesLevelTest(unittest.TestCase):
    def test_paladin_defensives_runs_at_50_with_reset(self):
        trial = load()
        self.assertEqual(trial["bot"]["level"], 50)
        self.assertEqual(trial["player"]["level"], 50)
        self.assertTrue(trial["situation"].get("reset_hire_cooldowns"))
        bulwark = [r for r in trial["rules"] if r["id"] == "TNK-PAL-BULWARK"][0]
        self.assertEqual(bulwark.get("min_reps"), 3)
        self.assertEqual(scenario_mod.validate(trial), [])


if __name__ == "__main__":
    unittest.main()
