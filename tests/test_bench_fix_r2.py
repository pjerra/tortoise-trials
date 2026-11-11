"""Bench fix round 2 (bench-fix-1111): one test class per fix, from the TRIAGE.

Each section names the case that fails first on 4180cde, with the evidence line it comes from.
"""

import datetime
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import test_live as tl  # noqa: E402
from trl import rules as rules_mod  # noqa: E402
from trl import scenario as scenario_mod  # noqa: E402
from trl import score as score_mod  # noqa: E402
from trl.live import gm_guard, setup, situations  # noqa: E402

MOB = "0xF13000000000A001"
MOB2 = "0xF13000000000A002"


def unit(guid, distance, entry=1234):
    return {"guid": guid, "entry": entry, "distance": distance, "hostile": True, "dead": False,
            "level": 36, "health": 500, "max_health": 500}


# ---- 1. the summon claim: the only new unit is claimed at any distance ----
class SummonClaimAnyDistanceTest(unittest.TestCase):
    def test_the_only_new_unit_at_nine_yards_is_claimed(self):
        # bench-fix-1111: 39 unclaimed summons in 14 trials, each one unit of the right entry at
        # 3.27-14.7 yd, and every miss stopped the refills for the rest of the rep
        with tempfile.TemporaryDirectory() as tmp:
            a = tl.FakeAgent()
            a.units_queue = [[], [unit(MOB, 9.0)]]
            led = tl.new_ledger(tmp)
            g = gm_guard.GmGuard(a, led, tl.ctx_for())
            guid = situations.summon_mob(a, g, led, 1234, timeout=1.0, poll=0.01)
            self.assertEqual(guid, MOB.lower())
            self.assertEqual(led.summon_guids(), [MOB.lower()])

    def test_two_new_units_beyond_the_claim_radius_keep_todays_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = tl.FakeAgent()
            a.units_queue = [[], [unit(MOB, 6.0), unit(MOB2, 9.0)]]
            led = tl.new_ledger(tmp)
            g = gm_guard.GmGuard(a, led, tl.ctx_for())
            logs = []
            self.assertIsNone(situations.summon_mob(a, g, led, 1234, timeout=0.2, poll=0.01,
                                                    log=logs.append))
            self.assertEqual(led.summon_guids(), [])
            self.assertEqual(len(led.of_kind("summon_unclaimed")), 1)
            self.assertTrue(any("no unit claimed" in line for line in logs))

    def test_two_new_units_still_claim_the_one_within_three_yards(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = tl.FakeAgent()
            a.units_queue = [[], [unit(MOB, 2.0), unit(MOB2, 9.0)]]
            led = tl.new_ledger(tmp)
            g = gm_guard.GmGuard(a, led, tl.ctx_for())
            self.assertEqual(situations.summon_mob(a, g, led, 1234, timeout=0.2, poll=0.01),
                             MOB.lower())


if __name__ == "__main__":
    unittest.main()
