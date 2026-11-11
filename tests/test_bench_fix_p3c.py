"""P3c: taunt-off-healer gets its chances (count 3, 240 s, min_reps 3)."""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from trl import scenario as scenario_mod  # noqa: E402


def load():
    with open(os.path.join(ROOT, "trials", "tanks", "tank-warrior-taunt-off-healer.json")) as f:
        return json.load(f)


class TauntOffHealerChancesTest(unittest.TestCase):
    def test_taunt_off_healer_has_chances(self):
        trial = load()
        merged = {"base": trial}
        merged.update({n: m for n, m in scenario_mod.expand_variants(trial)})
        for name, m in merged.items():
            self.assertEqual(m["situation"]["mobs"]["count"], 3, name)
            self.assertEqual(m["duration_s"], 240, name)
            self.assertEqual(m["repeats"], 4, name)
            taunt = [r for r in m["rules"] if r["id"] == "TNK-WAR-TAUNT"][0]
            self.assertEqual((taunt["check"].get("min_n") or taunt.get("min_n")), 5, name)
            self.assertEqual(taunt.get("min_reps"), 3, name)
            self.assertEqual(scenario_mod.validate(dict(m, id=trial["id"])), [], name)


if __name__ == "__main__":
    unittest.main()
