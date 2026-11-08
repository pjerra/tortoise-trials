"""JSON safety: a no-match `first` rule writes strict JSON.

Before the fix, a `first` metric with no matching event stored float("inf"),
which json.dump wrote as the token `Infinity` (invalid JSON). Now the stored
value is null with no_match true; pass_if still decides on +inf internally.
Every JSON writer in trl/ uses allow_nan=False, so a NaN/inf fails loudly.

Failing case this guards: a `first` rule "< 30" with no Power Word: Shield
row wrote value Infinity into verdicts.json and card.json.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from trl import report as report_mod  # noqa: E402
from trl import rules as rules_mod  # noqa: E402
from trl import score as score_mod  # noqa: E402

PWS = "Power Word: Shield"
PLAYER = "0x0000000000000001"
BOT = "0x0000000000000002"


def _reject_constant(name):
    raise ValueError("non-standard JSON token %s" % name)


def _strict_loads(text):
    return json.loads(text, parse_constant=_reject_constant)


def _first_rule(rid, pass_if):
    return {"id": rid, "desc": "first shield", "source": "test", "expect_today": "?",
            "why_expect": "test",
            "check": {"metric": "first", "where": {"kind": "cast", "spell": PWS}},
            "pass_if": pass_if, "min_opportunity_s": 0, "min_n": 0}


def _trial(rules):
    return {"id": "json-safe-trial", "title": "json safe", "group": "t",
            "class": "priest", "spec": "holy", "player": {}, "bot": {},
            "situation": {}, "setup_checks": [], "duration_s": 60, "repeats": 1,
            "min_valid_s": 0, "opportunity": {}, "rules": rules}


def _ctx():
    return {"guids": {}, "thresholds": {}, "thresholds_assumed": False,
            "trial": {}}


def _renew(t):
    # A cast of another spell: the `first` where (spell = Power Word: Shield)
    # matches nothing in this rep.
    return {"t": t, "wall": None, "kind": "cast", "src": "bot", "dst": "player",
            "src_guid": BOT, "dst_guid": PLAYER, "src_unit": "$bot",
            "dst_unit": "$player", "spell_id": 139, "spell": "Renew",
            "amount": None, "dst_hp_pct": 50.0, "src_role": "bot",
            "dst_role": "player", "origin": "ta"}


class NoMatchFirstRuleTest(unittest.TestCase):
    def test_value_null_no_match_true_and_verdict_kept(self):
        for pass_if, verdict in (("< 30", "FAIL"), ("> 30", "PASS")):
            res = rules_mod.evaluate(_first_rule("F", pass_if), [], [], [], _ctx())
            self.assertIsNone(res["value"], pass_if)
            self.assertIs(res.get("no_match"), True, pass_if)
            self.assertEqual(res["verdict"], verdict, pass_if)
            self.assertEqual(res["evidence"], ["no match"])

    def test_match_still_has_numeric_value(self):
        shield = {"t": 12.0, "kind": "cast", "src": "bot", "dst": "player",
                  "src_guid": BOT, "dst_guid": PLAYER, "src_unit": "$bot",
                  "dst_unit": "$player", "spell_id": 6065, "spell": PWS,
                  "dst_hp_pct": 80.0, "origin": "ta"}
        res = rules_mod.evaluate(_first_rule("F", "< 30"), [shield], [], [], _ctx())
        self.assertEqual(res["value"], 12.0)
        self.assertNotIn("no_match", res)
        self.assertEqual(res["verdict"], "PASS")

    def test_result_dumps_strictly(self):
        res = rules_mod.evaluate(_first_rule("F", "< 30"), [], [], [], _ctx())
        text = json.dumps(res, allow_nan=False)
        self.assertIsNone(_strict_loads(text)["value"])


class NoMatchWritesStrictJsonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rep(self, run_dir, trial_id, name="rep1"):
        rep = os.path.join(run_dir, trial_id, name)
        os.makedirs(rep)
        with open(os.path.join(rep, "setup.json"), "w") as f:
            json.dump({"ok": True, "checks": [], "guids": {}}, f)
        with open(os.path.join(rep, "events.jsonl"), "w") as f:
            f.write(json.dumps(_renew(5.0), sort_keys=True) + "\n")
        return rep

    def test_verdicts_and_trial_json_are_strict(self):
        trial = _trial([_first_rule("F-LT", "< 30"), _first_rule("F-GT", "> 30")])
        rep = self._rep(self.tmp, trial["id"])
        out = score_mod.score_rep(rep, trial)
        by_id = {r["id"]: r for r in out["rules"]}
        self.assertEqual(by_id["F-LT"]["verdict"], "FAIL")
        self.assertEqual(by_id["F-GT"]["verdict"], "PASS")
        with open(os.path.join(rep, "verdicts.json")) as f:
            doc = _strict_loads(f.read())
        rules = {r["id"]: r for r in doc["rules"]}
        for rid, verdict in (("F-LT", "FAIL"), ("F-GT", "PASS")):
            self.assertIsNone(rules[rid]["value"])
            self.assertIs(rules[rid]["no_match"], True)
            self.assertEqual(rules[rid]["verdict"], verdict)

        trial_dir = os.path.join(self.tmp, trial["id"])
        score_mod.score_trial(trial_dir, trial)
        with open(os.path.join(trial_dir, "trial.json")) as f:
            tdoc = _strict_loads(f.read())
        for row in tdoc["verdicts"]:
            self.assertIsNone(row["detail"][0]["value"])
            self.assertIs(row["detail"][0]["no_match"], True)
            self.assertEqual(row["verdict"], "FAIL" if row["id"] == "F-LT" else "PASS")

    def test_card_is_strict_and_says_no_match(self):
        trial = _trial([_first_rule("F-LT", "< 30")])
        run_dir = self.tmp
        self._rep(run_dir, trial["id"])
        trial_dir = os.path.join(run_dir, trial["id"])
        run_trials = score_mod.score_trial(trial_dir, trial)
        report_mod.write_card(run_dir, [run_trials])
        with open(os.path.join(run_dir, "card.json")) as f:
            card = _strict_loads(f.read())
        row = card["rows"][0]
        self.assertIsNone(row["value"])
        self.assertIs(row["no_match"], True)
        with open(os.path.join(run_dir, "card.md")) as f:
            md = f.read()
        self.assertIn("no match", md)
        self.assertNotIn("Infinity", md)
        self.assertNotIn("inf", md.split("| trial |", 1)[-1])

    def test_agg_value_all_no_match_is_none_not_inf(self):
        details = [{"verdict": "FAIL", "value": None, "no_match": True}]
        agg, vals = report_mod._agg_value(details, "first")
        self.assertIsNone(agg)
        self.assertEqual(vals, [])


class AllowNanGuardTest(unittest.TestCase):
    def test_nan_and_inf_fail_loudly_on_write(self):
        tmp = tempfile.mkdtemp()
        try:
            for bad in (float("inf"), float("-inf"), float("nan")):
                path = os.path.join(tmp, "x.json")
                with self.assertRaises(ValueError):
                    score_mod._write(path, {"value": bad})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
