"""P3a: a minimum-sample rule (min_reps).

Failing-first: score_trial has no min_reps gate; RULE_OVERRIDE_FIELDS lacks it.
"""

import copy
import glob
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from trl import scenario as scenario_mod  # noqa: E402
from trl import score as score_mod  # noqa: E402
from trl import report as report_mod  # noqa: E402


def write_rep(trial_dir, rep, rule_id, verdict, k=0, n=0, value=None):
    rep_dir = os.path.join(trial_dir, rep)
    os.makedirs(rep_dir, exist_ok=True)
    with open(os.path.join(rep_dir, "verdicts.json"), "w") as f:
        json.dump({"trial": "t", "rules": [{"id": rule_id, "verdict": verdict, "k": k, "n": n,
                                            "value": value, "evidence": [], "guard": "",
                                            "no_match": False, "reason": "",
                                            "invalid_reason": ""}]}, f)


def trial_with(rule):
    return {"id": "t", "rules": [rule]}


class MinRepsTest(unittest.TestCase):
    def test_one_valid_rep_is_inconclusive_under_min_reps(self):
        rule = {"id": "R", "check": {"metric": "ratio", "num": {}, "den": {}},
                "pass_if": ">= 0.5", "min_reps": 3}
        with tempfile.TemporaryDirectory() as tmp:
            tdir = os.path.join(tmp, "t")
            os.makedirs(tdir)
            write_rep(tdir, "rep1", "R", "FAIL", k=3, n=5, value=0.6)
            for i in (2, 3, 4):
                write_rep(tdir, "rep%d" % i, "R", "INCONCLUSIVE")
            out = score_mod.score_trial(tdir, trial_with(rule))
        row = out["verdicts"][0]
        self.assertEqual(row["verdict"], "INCONCLUSIVE")
        self.assertIn("valid reps 1 < min_reps 3", row.get("reason") or "")
        self.assertEqual((row["k"], row["n"]), (3, 5))

    def test_min_reps_default_keeps_todays_verdict(self):
        rule = {"id": "R", "check": {"metric": "ratio", "num": {}, "den": {}},
                "pass_if": ">= 0.5"}
        with tempfile.TemporaryDirectory() as tmp:
            tdir = os.path.join(tmp, "t")
            os.makedirs(tdir)
            write_rep(tdir, "rep1", "R", "FAIL", k=3, n=5, value=0.6)
            for i in (2, 3, 4):
                write_rep(tdir, "rep%d" % i, "R", "INCONCLUSIVE")
            out = score_mod.score_trial(tdir, trial_with(rule))
        self.assertEqual(out["verdicts"][0]["verdict"], "FAIL")

    def test_variant_may_override_min_reps(self):
        base = {"id": "t", "rules": [{"id": "R", "expect_today": "PASS",
                                      "check": {"metric": "count", "where": {"kind": "cast"}},
                                      "pass_if": ">= 1", "min_n": 1}]}
        merged, errs = scenario_mod.apply_override(base, {"rules": {"R": {"min_reps": 3}}})
        self.assertEqual(errs, [])
        self.assertEqual(merged["rules"][0]["min_reps"], 3)
        merged2, errs2 = scenario_mod.apply_override(base, {"rules": {"R": {"min_reps": 0}}})
        self.assertTrue(errs2)

    def test_compare_notes_too_few_reps(self):
        with tempfile.TemporaryDirectory() as tmp:
            a_run, b_run = os.path.join(tmp, "after"), os.path.join(tmp, "before")
            for d in (a_run, b_run):
                rep = os.path.join(d, "t", "rep1")
                os.makedirs(rep, exist_ok=True)
                with open(os.path.join(rep, "verdicts.json"), "w") as f:
                    json.dump({"rules": [{"id": "R", "verdict": "PASS",
                                          "value": 1, "k": 1, "n": 1}]}, f)
            with open(os.path.join(a_run, "card.json"), "w") as f:
                json.dump({"run": {}, "rows": [{"trial": "t", "rule": "R", "expected": "?",
                                                "verdict": "INCONCLUSIVE", "k": 3, "n": 5,
                                                "metric": "latency", "value": 0.6, "values": [0.6],
                                                "no_match": False, "pass_if": ">= 0.5",
                                                "surprise": "", "source": "s", "detail": [],
                                                "reason": "valid reps 1 < min_reps 3"}]}, f)
            with open(os.path.join(b_run, "card.json"), "w") as f:
                json.dump({"run": {}, "rows": [{"trial": "t", "rule": "R", "expected": "?",
                                                "verdict": "PASS", "k": 4, "n": 5,
                                                "metric": "latency", "value": 0.8, "values": [0.8],
                                                "no_match": False, "pass_if": ">= 0.5",
                                                "surprise": "", "source": "s", "detail": [],
                                                "reason": ""}]}, f)
            rows = report_mod.compare(a_run, b_run)
        row = [r for r in rows if r["rule"] == "R"][0]
        self.assertIsNone(row.get("p"))
        self.assertIn("too few valid reps", row.get("note") or "")


if __name__ == "__main__":
    unittest.main()
