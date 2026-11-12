"""Pooled scoring (`pool: true`) and the drink-under-attack rule (paladin add-pickup).

Why (paladin-addpickup WHY.md, 2026-11-12): with 0-5 triggers per repeat and min_n 3 applied
per repeat, one repeat decided the trial's verdict, and the card that PASSed did so because the
guard dropped the two repeats with the misses. A pooled rule takes ONE verdict from the summed
k/n over the valid repeats and applies min_n to the pooled n.

Second half: TNK-PAL-NO-DRINK-UNDER-ATTACK. The bot's Drink sets a 15 s meal duration, so a
refill mob that opens on the player or the healer during the meal is unanswered until the sleep
runs out. The rule counts Drink casts by the bot while any of those party members has a hostile
mob on it (packets first, the mob's sample target as the fallback).
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import test_trials as tt  # noqa: E402
from trl import rules as rules_mod  # noqa: E402
from trl import scenario  # noqa: E402
from trl import score as score_mod  # noqa: E402
from trl import spells  # noqa: E402

PLAYER, BOT, MOB = tt.PLAYER, tt.BOT, tt.MOB
HEALER = "0x0000000000000003"
DRINK_ID = sorted(spells.ids_for("Drink"))[0]

LATENCY = {"id": "POOLED", "desc": "x", "source": "s", "expect_today": "?",
           "why_expect": "x",
           "check": {"metric": "latency",
                     "trigger": {"kind": "damage", "src": "$mob", "dst": "$player"},
                     "response": {"kind": "cast", "src": "$bot"},
                     "within_s": 5, "response_dst": "$trigger.src"},
           "pass_if": ">= 0.7", "min_n": 3, "pool": True}


def _mob_damage(t):
    return {"t": t, "kind": "damage", "src": "m", "src_guid": MOB, "dst": "p",
            "dst_guid": PLAYER, "src_unit": "mob:" + MOB.lower(), "dst_unit": "$player",
            "spell_id": None, "spell": None, "amount": 1, "dst_hp_pct": 50.0,
            "src_role": "mob", "dst_role": "player", "origin": "ta"}


def _bot_cast(t, spell="Drink", spell_id=None, dst_guid=""):
    return {"t": t, "kind": "cast", "src": "b", "src_guid": BOT, "dst": "", "dst_guid": dst_guid,
            "src_unit": "$bot", "dst_unit": "", "spell_id": spell_id if spell_id is not None else DRINK_ID,
            "spell": spell, "amount": None, "dst_hp_pct": None, "src_role": "bot",
            "dst_role": "other", "origin": "ta"}


def _sample(t, guid, unit, target="", hp=1000):
    return {"t": t, "guid": guid, "unit": unit, "target": target, "hp": hp, "max_hp": hp,
            "power": 100, "max_power": 100, "auras": [], "casting": None, "in_combat": True,
            "x": 0.0, "y": 0.0, "z": 0.0}


def _rep(root, name, verdict, k, n, reason=""):
    rep = os.path.join(root, name)
    os.makedirs(rep)
    with open(os.path.join(rep, "verdicts.json"), "w") as f:
        json.dump({"trial": "t", "rep": name, "ok_setup": True, "scored_s": 180.0,
                   "rules": [{"id": "POOLED", "verdict": verdict, "value": (k / n) if n else None,
                              "k": k, "n": n, "evidence": [], "guard": "", "reason": reason,
                              "invalid_reason": ""}]}, f)
    return rep


class PooledTrialTest(unittest.TestCase):
    def _trial(self, **over):
        trial = tt._trial()
        trial["id"] = "t"
        trial["rules"] = [dict(LATENCY)]
        trial["rules"][0].update(over)
        return trial

    def test_zero_trigger_repeat_does_not_veto_the_five_trigger_repeat(self):
        # the failing case before the change: min_n 3 read PER REPEAT, so the rep with 0
        # triggers was INCONCLUSIVE on its own and the 5-trigger rep carried the only vote;
        # pooled, the summed n is the 3-trigger minimum's denominator.
        tmp = tempfile.mkdtemp()
        _rep(tmp, "rep1", "INCONCLUSIVE", 0, 0)
        _rep(tmp, "rep2", "PASS", 5, 5)
        out = score_mod.score_trial(tmp, self._trial())
        row = out["verdicts"][0]
        self.assertEqual((row["verdict"], row["k"], row["n"]), ("PASS", 5, 5))

    def test_an_unknown_attack_state_makes_the_pooled_verdict_inconclusive(self):
        # round 2: the unknown repeat is INCONCLUSIVE with reason "attack state unknown"; it must not
        # be erased from the pool, so the clean PASS repeat cannot decide the trial.
        tmp = tempfile.mkdtemp()
        _rep(tmp, "rep1", "INCONCLUSIVE", 0, 1, reason="attack state unknown")
        _rep(tmp, "rep2", "PASS", 3, 3)
        out = score_mod.score_trial(tmp, self._trial())
        row = out["verdicts"][0]
        self.assertEqual(row["verdict"], "INCONCLUSIVE")
        self.assertIn("attack state unknown", row["reason"])
        self.assertIn("rep1", row["reason"])

    def test_a_no_trigger_repeat_is_still_erased_from_the_pool(self):
        # benign INCONCLUSIVE reasons (no opportunity, no trigger) keep the old behaviour: they add
        # nothing, and the PASS repeat still decides the pooled verdict.
        tmp = tempfile.mkdtemp()
        _rep(tmp, "rep1", "INCONCLUSIVE", 0, 0, reason="n 0 < min_n 3")
        _rep(tmp, "rep2", "PASS", 3, 3)
        out = score_mod.score_trial(tmp, self._trial())
        self.assertEqual(out["verdicts"][0]["verdict"], "PASS")

    def test_pooled_min_n_reads_the_pooled_total(self):
        # each repeat has n=2, under min_n 3 on its own; the pooled n is 4.
        tmp = tempfile.mkdtemp()
        _rep(tmp, "rep1", "PASS", 1, 2)
        _rep(tmp, "rep2", "PASS", 2, 2)
        out = score_mod.score_trial(tmp, self._trial())
        row = out["verdicts"][0]
        self.assertEqual((row["verdict"], row["k"], row["n"]), ("PASS", 3, 4))

    def test_one_verdict_from_the_summed_k_over_n(self):
        # per-repeat majority says PASS (2 of 3); pooled 2/7 is under 0.7 -> FAIL.
        tmp = tempfile.mkdtemp()
        _rep(tmp, "rep1", "PASS", 1, 1)
        _rep(tmp, "rep2", "PASS", 1, 1)
        _rep(tmp, "rep3", "FAIL", 0, 5)
        out = score_mod.score_trial(tmp, self._trial())
        self.assertEqual(out["verdicts"][0]["verdict"], "FAIL")

    def test_pooled_total_below_min_n_is_inconclusive(self):
        tmp = tempfile.mkdtemp()
        _rep(tmp, "rep1", "PASS", 1, 1)
        _rep(tmp, "rep2", "FAIL", 0, 1)
        out = score_mod.score_trial(tmp, self._trial())
        row = out["verdicts"][0]
        self.assertEqual(row["verdict"], "INCONCLUSIVE")
        self.assertIn("pooled n 2 < min_n 3", row["reason"])

    def test_score_rep_skips_the_per_repeat_min_n_for_a_pooled_rule(self):
        # score_rep must not turn each pooled repeat INCONCLUSIVE before score_trial can pool it.
        run = tempfile.mkdtemp()
        for pooled, expected in ((True, "PASS"), (False, "INCONCLUSIVE")):
            trial = self._trial()
            trial["min_valid_s"] = 0
            trial["rules"][0]["pool"] = pooled
            trial["rules"][0]["min_n"] = 5
            rep = os.path.join(run, trial["id"] + ("_p" if pooled else "_n"), "rep1")
            os.makedirs(rep)
            with open(os.path.join(run, "RUN.json"), "w") as f:
                json.dump({"critical_health": 20, "low_health": 50, "medium_health": 70,
                           "trials": [trial["id"]]}, f)
            with open(os.path.join(rep, "setup.json"), "w") as f:
                json.dump({"ok": True, "guids": {"player": PLAYER, "bot": BOT,
                                                 "mobs": [MOB]}}, f)
            with open(os.path.join(rep, "samples.jsonl"), "w") as f:
                for t in range(0, 60):
                    f.write(json.dumps(_sample(float(t), PLAYER, "player")) + "\n")
            open(os.path.join(rep, "marks.jsonl"), "w").close()
            with open(os.path.join(rep, "events.jsonl"), "w") as f:
                for t in (10.0, 20.0):
                    f.write(json.dumps(_mob_damage(t)) + "\n")
                    f.write(json.dumps(_bot_cast(t + 1.0, spell="Taunt", spell_id=355, dst_guid=MOB)) + "\n")
            out = score_mod.score_rep(rep, trial)
            self.assertEqual(out["rules"][0]["verdict"], expected, pooled)

    def test_pooled_count_of_zero_and_five(self):
        # a count rule's per-repeat n is 1: pooled value is the summed count over the valid
        # repeats (5/2 = 2.5 here), so `pass_if == 0` passes only when every repeat is 0.
        rule = {"id": "POOLED", "desc": "x", "source": "s", "expect_today": "?",
                "why_expect": "x",
                "check": {"metric": "count",
                          "where": {"kind": "cast", "src": "$bot", "spell": "Drink"}},
                "pass_if": "== 0", "pool": True}
        trial = tt._trial()
        trial["rules"] = [rule]
        tmp = tempfile.mkdtemp()
        _rep(tmp, "rep1", "PASS", 0, 1)
        _rep(tmp, "rep2", "FAIL", 5, 1)
        out = score_mod.score_trial(tmp, trial)
        self.assertEqual(out["verdicts"][0]["verdict"], "FAIL")
        tmp2 = tempfile.mkdtemp()
        _rep(tmp2, "rep1", "PASS", 0, 1)
        _rep(tmp2, "rep2", "PASS", 0, 1)
        out = score_mod.score_trial(tmp2, trial)
        self.assertEqual(out["verdicts"][0]["verdict"], "PASS")


class PooledValidatorTest(unittest.TestCase):
    def _errs(self, rule, trial=None):
        trial = trial or tt._trial()
        trial["rules"] = [rule]
        return scenario.validate(trial)

    def test_pool_true_is_valid_on_a_count(self):
        rule = dict(LATENCY, check={"metric": "count", "where": {"kind": "cast"}},
                    pass_if="== 0")
        self.assertEqual([e for e in self._errs(rule) if "pool" in e], [])

    def test_pool_must_be_true_and_only_on_a_k_over_n_metric(self):
        for bad in (False, "yes", 1):
            errs = [e for e in self._errs(dict(LATENCY, pool=bad)) if "pool" in e]
            self.assertTrue(errs, bad)
        uptime = {"id": "POOLED", "desc": "x", "source": "s", "expect_today": "?",
                  "why_expect": "x",
                  "check": {"metric": "uptime", "unit": "$player", "aura": [71]},
                  "pass_if": ">= 0.5", "pool": True}
        self.assertTrue([e for e in self._errs(uptime) if "pool applies only" in e])

    def test_party_under_attack_wants_a_non_empty_list_of_party_units(self):
        def errs(units):
            rule = dict(LATENCY, check={"metric": "count",
                                        "where": {"kind": "cast", "party_under_attack": units}})
            return [e for e in self._errs(rule) if "party_under_attack" in e]
        self.assertEqual(errs(["$player", "$extras"]), [])
        for bad in ([], "$player", ["$nope"], ["$mob"], [3], [""]):
            self.assertTrue(errs(bad), bad)


class DrinkUnderAttackTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "trials", "tanks", "tank-paladin-add-pickup.json")) as f:
            self.trial = json.load(f)
        self.rule = [r for r in self.trial["rules"]
                     if r["id"] == "TNK-PAL-NO-DRINK-UNDER-ATTACK"][0]
        self.guids = {"player": PLAYER, "bot": BOT, "extras": [HEALER], "mobs": [MOB]}
        self.samples = [
            # the mob's own sample target says who it is on; packets decide first where they can
            _sample(9.0, MOB, "mob:" + MOB.lower(), PLAYER, hp=500),
            _sample(29.0, MOB, "mob:" + MOB.lower(), BOT, hp=500),
            _sample(45.0, MOB, "mob:" + MOB.lower(), BOT, hp=500),
        ] + [_sample(float(t), PLAYER, "player") for t in range(0, 60)]

    def _ctx(self, victims=None):
        return {"guids": self.guids, "thresholds": {}, "thresholds_assumed": False,
                "trial": self.trial, "victims_by_guid": victims or {}}

    def test_drink_while_a_mob_is_on_the_player_counts(self):
        # t=10: the packet at 9 has the player as victim -> a counted Drink cast.
        # t=30 and t=50: the mob is on the bot (the tank), which is not in the party list.
        victims = {MOB.lower(): [(None, 9.0, PLAYER), (None, 29.0, BOT), (None, 45.0, BOT)]}
        rows = [_bot_cast(10.0), _bot_cast(30.0), _bot_cast(50.0)]
        res = rules_mod.evaluate(self.rule, rows, self.samples, [], self._ctx(victims))
        self.assertEqual((res["k"], res["n"], res["verdict"]), (1, 1, "FAIL"))

    def test_the_sample_target_is_the_fallback_when_no_packet_is_fresh(self):
        rows = [_bot_cast(10.0)]
        res = rules_mod.evaluate(self.rule, rows, self.samples, [], self._ctx())
        self.assertEqual((res["k"], res["verdict"]), (1, "FAIL"))

    def test_no_drink_while_the_party_is_attacked_passes(self):
        victims = {MOB.lower(): [(None, 9.0, BOT)]}
        rows = [_bot_cast(10.0)]
        res = rules_mod.evaluate(self.rule, rows, self.samples, [], self._ctx(victims))
        self.assertEqual((res["k"], res["verdict"]), (0, "PASS"))

    def test_a_healer_attack_packet_counts_when_the_sample_is_stale(self):
        # t=32: the mob's last sample (29, on the bot) is 3 s old; the packet at 29 has the healer.
        victims = {MOB.lower(): [(None, 29.0, HEALER)]}
        rows = [_bot_cast(32.0)]
        res = rules_mod.evaluate(self.rule, rows, self.samples, [], self._ctx(victims))
        self.assertEqual((res["k"], res["verdict"]), (1, "FAIL"))

    def test_unknown_attack_state_is_inconclusive_not_a_pass(self):
        # t=32: the mob was alive but its last sample is 3 s old (no fresh target) and there is no
        # attack packet at all: missing attack-state evidence must not read as "not under attack".
        rows = [_bot_cast(32.0)]
        res = rules_mod.evaluate(self.rule, rows, self.samples, [], self._ctx())
        self.assertEqual((res["k"], res["verdict"]), (0, "INCONCLUSIVE"))
        self.assertIn("attack state unknown", " ".join(res["evidence"]))
        # score.py's pooled guard keys on this exact reason.
        self.assertEqual(res["reason"], "attack state unknown")

    def test_a_dead_mob_is_not_unknown(self):
        # The mob's freshest sample has hp 0: it was not live at the cast, so nothing was on the
        # party and the Drink is clean (no INCONCLUSIVE from a corpse).
        samples = [_sample(29.0, MOB, "mob:" + MOB.lower(), BOT, hp=0),
                   _sample(29.0, PLAYER, "player")]
        res = rules_mod.evaluate(self.rule, [_bot_cast(32.0)], samples, [], self._ctx())
        self.assertEqual((res["k"], res["verdict"]), (0, "PASS"))


if __name__ == "__main__":
    unittest.main()
