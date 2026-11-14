"""Pooled scoring for DPS-MAG-COUNTERSPELL (t1113 scoring flip, not a regression).

The t1113 card (bench 8bc775f, `dps-mage-counterspell-caster`) read FAIL 6/10 only
because two repeats fell under `min_n` 5 by one trigger and the tie among the two
valid repeats scored FAIL. The t1112-PASS was the same rate (12/21 = 0.571). Pooled,
both images FAIL at the same rate (10/18 = 0.556), so the rule never held. This
test pins the pooled verdict at the t1113 numbers (2/5, 2/4, 4/5, 2/4 -> FAIL at
10/18) and checks the flip on a real synthetic repeat: pooled, a 4-trigger repeat
is scored 4/4 and PASS; without `pool` the same repeat is INCONCLUSIVE (`n 4 <
min_n 5`), which is what the pre-pool JSON still does.
"""

import copy
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_rules_tanks_dps import (BOT, EXTRA1, LIGHTNING_BOLT, MARKED, MOB,  # noqa: E402
                                  PLAYER, event, series)
from trl import scenario as scenario_mod  # noqa: E402
from trl import score as score_mod  # noqa: E402

TRIAL_ID = "dps-mage-counterspell-caster"
RULE_ID = "DPS-MAG-COUNTERSPELL"
# the t1113 per-rep k/n from COUNTERSPELL-FAIL.md (card 20261113-141949-t1113)
T1113 = ((2, 5), (2, 4), (4, 5), (2, 4))


def load_rule():
    trial = scenario_mod.load_trial(os.path.join(ROOT, "trials", "dps", TRIAL_ID + ".json"))
    rule = [r for r in trial["rules"] if r["id"] == RULE_ID][0]
    return trial, rule


def write_reps(root, pairs):
    for i, (k, n) in enumerate(pairs):
        rep = os.path.join(root, "rep%d" % (i + 1))
        os.makedirs(rep)
        with open(os.path.join(rep, "verdicts.json"), "w") as f:
            json.dump({"trial": TRIAL_ID, "rep": "rep%d" % (i + 1), "ok_setup": True,
                       "scored_s": 180.0,
                       "rules": [{"id": RULE_ID, "verdict": "PASS" if k / n >= 0.6 else "FAIL",
                                  "value": k / n, "k": k, "n": n, "evidence": [],
                                  "guard": "", "reason": "", "invalid_reason": ""}]}, f)
    return root


def write_casting_rep(root, trial, answers, triggers=4):
    """One synthetic repeat: `triggers` mob casts, `answers` Counterspells; return the rule row."""
    rep = os.path.join(root, "rep")
    os.makedirs(rep)
    setup = {"ok": True,
             "guids": {"player": PLAYER, "bot": BOT, "extras": [EXTRA1],
                       "mobs": [MOB, MARKED], "marked": MARKED},
             "config": {"critical": 20, "low": 50, "medium": 70}}
    rows = {"setup.json": setup,
            "samples.jsonl": series(200, player=(71,)),
            "events.jsonl": ([event(10 + 40 * i, "cast_start", MOB, PLAYER, LIGHTNING_BOLT)
                              for i in range(triggers)]
                             + [event(11.5 + 40 * i, "cast", BOT, MOB, 2139)
                                for i in range(answers)]),
            "marks.jsonl": [{"kind": "fight_start", "t": 0},
                            {"kind": "fight_end", "t": 200, "ended": "duration"}]}
    for name, doc in rows.items():
        with open(os.path.join(rep, name), "w") as f:
            if name.endswith(".jsonl"):
                for row in doc:
                    f.write(json.dumps(row) + "\n")
            else:
                json.dump(doc, f)
    out = score_mod.score_rep(rep, trial)
    return [r for r in out["rules"] if r["id"] == RULE_ID][0]


class CounterspellPooledTest(unittest.TestCase):
    def test_rule_is_pooled_and_min_n_reads_the_pooled_total(self):
        trial, rule = load_rule()
        self.assertIs(rule.get("pool"), True)
        self.assertGreaterEqual(sum(n for _, n in T1113), int(rule.get("min_n") or 0))

    def test_t1113_scores_a_pooled_fail_at_10_of_18(self):
        trial, _ = load_rule()
        tmp = tempfile.mkdtemp()
        out = score_mod.score_trial(write_reps(tmp, T1113), trial)
        row = out["verdicts"][0]
        self.assertEqual((row["verdict"], row["k"], row["n"]), ("FAIL", 10, 18))

    def test_a_four_trigger_repeat_is_scored_only_when_pooled(self):
        # t1113 rep2/rep4: 4 triggers, under min_n 5. Pooled, the per-repeat guard does not
        # run and the repeat contributes 4/4; without `pool` it is INCONCLUSIVE (the old JSON).
        trial, _ = load_rule()
        tmp = tempfile.mkdtemp()
        row = write_casting_rep(os.path.join(tmp, "pooled"), trial, answers=4)
        self.assertEqual((row["verdict"], row["k"], row["n"]), ("PASS", 4, 4))
        legacy = copy.deepcopy(trial)
        for rule in legacy["rules"]:
            if rule["id"] == RULE_ID:
                rule.pop("pool", None)
        row = write_casting_rep(os.path.join(tmp, "legacy"), legacy, answers=4)
        self.assertEqual(row["verdict"], "INCONCLUSIVE")
        self.assertIn("n 4 < min_n 5", row["reason"])


if __name__ == "__main__":
    unittest.main()
