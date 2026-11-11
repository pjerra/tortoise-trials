"""P4: the marked mob is taunted into combat before the fight counts.

Failing-first: no engage_marked (no 355 cast), mobs_ready ignores the mark,
opportunity_seconds knows no has_aura, and zero-hit count rules print "0 s".
"""

import json
import os
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_live as tl  # noqa: E402
import test_trials as tt  # noqa: E402
from trl import rules as rules_mod  # noqa: E402
from trl.live import agent as agent_mod, situations  # noqa: E402

norm = agent_mod.norm_guid
A, B, C = (norm(g) for g in ("0xF130000000000C01", "0xF130000000000C02", "0xF130000000000C03"))
MARKED = B


def make(in_combat=(A, B, C)):
    with open(str(ROOT / "trials" / "dps" / "dps-mage-cc-respect.json")) as f:
        trial = json.load(f)
    agent = tl.FakeAgent()
    ctx = situations.SitContext(player_guid=tl.PLAYER)
    ctx.guids["marked"] = MARKED
    sit = situations.Situation(agent, trial, None, None, None, None, ctx, arenas=[], mobs={})
    level = sit.level_window()[0] + 1
    for g in (A, B, C):
        agent.unit_rows[norm(g)] = {"guid": norm(g), "hostile": True, "dead": False, "level": level,
                                    "in_combat": g in in_combat}
    sit.summoned = [A, B, C]
    ctx.checks.append({"id": "mobs_ready", "ok": True, "value": [], "evidence": "before the pull"})
    return sit, agent


class EngageMarkedTest(unittest.TestCase):
    def calls(self, agent):
        """(targets selected, casts, attacks) in order."""
        targets, casts, attacks = [], [], []
        current = None
        for method, params in agent.calls:
            if method == "target":
                current = norm((params or {}).get("guid"))
                targets.append(current)
            elif method == "cast":
                casts.append(((params or {}).get("spell_id"), norm((params or {}).get("target"))))
            elif method == "attack":
                attacks.append(current)
        return targets, casts, attacks

    def test_marked_mob_is_taunted_into_combat_first(self):
        sit, agent = make(in_combat=(A, B, C))
        sit.engage_marked(timeout=0.0)
        targets, casts, attacks = self.calls(agent)
        taunts = [c for c in casts if c[0] == 355]
        self.assertTrue(taunts, casts)
        self.assertEqual(taunts[0][1], norm(MARKED))
        # no attack on the marked guid, and the last selection is unmarked
        self.assertNotIn(norm(MARKED), attacks)
        self.assertNotEqual(targets[-1], norm(MARKED))

    def test_engage_order_is_mark_then_cc_then_unmarked(self):
        sit, agent = make(in_combat=(A, B, C))
        seen = []
        orig_mark = sit.mark_and_cc
        sit.walk_near = lambda guid, want=25.0: seen.append(("near", norm(guid))) or True
        sit.mark_and_cc = lambda guid: seen.append(("mark", norm(guid))) or {"id": "cc_mark_set", "ok": True,
                                                                              "value": guid, "evidence": ""}
        sit.engage_marked = lambda timeout=5.0: seen.append(("engage_marked",)) or None
        orig_engage = situations.Situation.engage_cc_targets
        sit.engage_cc_targets = lambda timeout=10.0: seen.append(("engage_cc",)) or orig_engage(sit, timeout=0.0)
        sit.run_cc_pull()
        self.assertEqual([s[0] for s in seen][:3], ["mark", "engage_marked", "engage_cc"])

    def test_mobs_ready_fails_when_the_mark_stays_idle(self):
        sit, agent = make(in_combat=(A, C))   # the marked mob never entered combat
        row = situations.mobs_ready_row(agent, agent_ledger(agent), sit.level_window(),
                                        sit.summoned, engaged=[MARKED], marked=MARKED)
        self.assertFalse(row["ok"])
        self.assertIn(MARKED[-4:], row["evidence"])


def agent_ledger(agent):
    class Led:
        def summon_guids(self):
            return []
    return Led()


class HasAuraOpportunityTest(unittest.TestCase):
    def series(self, secs, aura):
        out = []
        for t in range(0, secs + 1):
            out.append(tt_Sample(t, MARKED, auras=(aura,) if 10 <= t <= 40 else ()))
        return out

    def test_has_aura_opportunity(self):
        trial = {"opportunity": {"unit": "$marked", "has_aura": [12825]}}
        samples = self.series(60, 12825)
        ctx = {"guids": {"marked": MARKED}, "thresholds": {}}
        opp = rules_mod.opportunity_seconds(trial, samples, [], ctx)
        self.assertAlmostEqual(opp, 30.0, delta=2.0)

    def test_respect_with_zero_hits_passes_and_missing_aura_is_inconclusive(self):
        import tempfile
        from trl import score as score_mod
        with open(str(ROOT / "trials" / "dps" / "dps-mage-cc-respect.json")) as f:
            trial = json.load(f)
        rule = [r for r in trial["rules"] if r["id"] == "DPS-MAG-CC-RESPECT"][0]
        rule["min_opportunity_s"] = 10
        trial["min_valid_s"] = 30
        samples = self.series(60, 12825)
        res = rules_mod.evaluate(rule, [], samples, [],
                                 {"guids": {"marked": MARKED}, "thresholds": {}, "trial": trial})
        self.assertEqual(res["verdict"], "PASS", res)
        bare = self.series(60, None)
        tmp = tempfile.mkdtemp()
        rep = os.path.join(tmp, "rep1")
        os.makedirs(rep)
        import json as _json
        with open(os.path.join(rep, "setup.json"), "w") as f:
            _json.dump({"ok": True, "guids": {"player": "p", "bot": "b", "marked": MARKED},
                        "config": {"critical": 20, "low": 50, "medium": 70}}, f)
        with open(os.path.join(rep, "samples.jsonl"), "w") as f:
            for row in bare:
                f.write(_json.dumps(row) + "\n")
        with open(os.path.join(rep, "events.jsonl"), "w") as f:
            pass
        with open(os.path.join(rep, "marks.jsonl"), "w") as f:
            f.write(_json.dumps({"kind": "fight_start", "t": 0}) + "\n")
            f.write(_json.dumps({"kind": "fight_end", "t": 60, "ended": "duration"}) + "\n")
        out = score_mod.score_rep(rep, trial)
        got = [r for r in out["rules"] if r["id"] == "DPS-MAG-CC-RESPECT"][0]
        self.assertEqual(got["verdict"], "INCONCLUSIVE", got)
        self.assertIn("opportunity 0 s < 10 s", got.get("reason") or "")


def tt_Sample(t, guid, auras=()):
    return {"t": float(t), "guid": guid, "hp": 600, "max_hp": 1000, "auras": list(auras or ()),
            "in_combat": True}


class ZeroHitEvidenceTest(unittest.TestCase):
    def test_zero_hit_count_evidence_names_other_targets(self):
        with open(str(ROOT / "trials" / "dps" / "dps-mage-cc-respect.json")) as f:
            trial = json.load(f)
        # a trial WITHOUT an opportunity block: no "opportunity 0 s" anywhere
        trial.pop("opportunity", None)
        rule = [r for r in trial["rules"] if r["id"] == "DPS-MAG-CC-POLYMORPH"][0]
        other = "0xF130000000000C99"
        rows = [{"t": float(10 + i), "kind": "cast", "src": "Botty", "dst": "Other Mob", "src_guid": tt.BOT.lower(),
                 "dst_guid": other.lower(), "src_unit": "$bot", "dst_unit": "$mob",
                 "spell_id": 118, "spell": "Polymorph", "amount": 0, "src_role": "bot",
                 "dst_role": "mob", "origin": "ta"}
                for i in range(2)]
        samples = [tt_Sample(float(t), other) for t in range(0, 60)]
        ctx = {"guids": {"player": tt.PLAYER, "bot": tt.BOT, "marked": MARKED,
                          "mobs": [other.lower()]},
               "thresholds": {}, "trial": trial}
        res = rules_mod.evaluate(rule, rows, samples, [], ctx)
        self.assertEqual(res["verdict"], "FAIL", res)
        ev = " ".join(res.get("evidence") or [])
        self.assertIn("2 same-spell casts on other targets", ev)
        self.assertNotIn("opportunity 0 s", ev)


if __name__ == "__main__":
    unittest.main()
