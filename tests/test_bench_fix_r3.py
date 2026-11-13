"""Bench fix round 3: a cross-rep cooldown removes only the chance it actually covers.

The old `_window_overlaps_cooldown` returned INCONCLUSIVE for any overlap: a potion cooldown
that ran out 10 s into a 30 s window still hid the bot's remaining 20 s, so a real miss read as
no opportunity. The rule's whole response window must be covered before the rep is INCONCLUSIVE;
a partial overlap falls through to the normal no-match scoring.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from trl import rules  # noqa: E402

BOT = "0x0000000000000B01"
POTION = 2023  # Restore Mana, rank 1
WINDOW = (0.0, 60.0)  # fight window; the potion's t_from -2 widens it, pass_if "<= 30" clips it


def potion_rule():
    return {"id": "PRI-LOWMANA-POTION", "expect_today": "PASS",
            "check": {"metric": "first",
                      "where": {"kind": "cast", "src": "$bot", "spell": "Restore Mana",
                                "t_from": -2},
                      "debounce_responder_s": 120,
                      "debounce_responder_spells": ["Restore Mana"],
                      "debounce_responder_across_reps": True},
            "pass_if": "<= 30"}


def cast(t):
    return {"t": float(t), "kind": "cast", "src": "Helper", "src_guid": BOT,
            "dst": "Helper", "dst_guid": BOT, "src_unit": "$bot", "dst_unit": "$bot",
            "spell_id": POTION, "spell": "Restore Mana", "amount": 0,
            "dst_hp_pct": 100, "src_role": "bot", "dst_role": "bot", "origin": "ta"}


def cooldown(a, b):
    return {"kind": "excluded", "from": float(a), "to": float(b), "t": float(a),
            "why": "prior rep responder cooldown"}


def score(rows):
    return rules.evaluate(potion_rule(), rows, [], [],
                          {"window": WINDOW, "guids": {"bot": BOT}, "trial": {}})


class CrossRepCooldownCoverageTest(unittest.TestCase):
    def test_a_cooldown_over_the_whole_window_is_no_opportunity(self):
        got = score([cooldown(-2, 118)])
        self.assertEqual(got["verdict"], "INCONCLUSIVE", got)
        self.assertEqual(got["reason"], "no opportunity (item on cooldown from an earlier rep)")

    def test_a_cooldown_that_ends_mid_window_leaves_a_late_cast_to_pass(self):
        # the span (-2, 10) overlaps the window but leaves 20 s; the t=20 cast is the chance
        got = score([cooldown(-2, 10), cast(20)])
        self.assertEqual((got["verdict"], got["value"]), ("PASS", 20.0), got)

    def test_a_cooldown_that_ends_mid_window_with_no_cast_is_a_miss(self):
        got = score([cooldown(-2, 10)])
        self.assertEqual(got["verdict"], "FAIL", got)
        self.assertEqual(got["evidence"], ["no match"], got)

    def test_a_cooldown_that_ends_before_the_window_scores_normally(self):
        got = score([cooldown(-200, -100), cast(5)])
        self.assertEqual((got["verdict"], got["value"]), ("PASS", 5.0), got)
        self.assertEqual(score([cooldown(-200, -100)])["verdict"], "FAIL")

    def test_adjacent_spans_merging_to_the_whole_window_are_no_opportunity(self):
        # neither span alone covers (-2, 30); merged they do, so it is still no chance
        got = score([cooldown(-2, 10), cooldown(10, 30)])
        self.assertEqual(got["verdict"], "INCONCLUSIVE", got)


if __name__ == "__main__":
    unittest.main()
