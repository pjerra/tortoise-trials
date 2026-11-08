"""Rule options added after the first review round of the tank/DPS trials:
check.debounce_responder_s (the responder's own cooldown) and where.not_within_s_after_mark."""

import copy
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_trials as tt  # noqa: E402
from trl import rules as rules_mod  # noqa: E402
from trl import scenario  # noqa: E402

PLAYER, BOT, MOB = tt.PLAYER, tt.BOT, tt.MOB


def mob_damage(t):
    return {"t": t, "kind": "damage", "src": "m", "src_guid": MOB, "dst": "p", "dst_guid": PLAYER,
            "src_unit": "x", "dst_unit": "$player", "spell_id": None, "spell": None, "amount": 1,
            "dst_hp_pct": 40.0, "src_role": "mob", "dst_role": "player", "origin": "ta"}


def bot_heal(t):
    return {"t": t, "kind": "heal", "src": "b", "src_guid": BOT, "dst": "p", "dst_guid": PLAYER,
            "src_unit": "$bot", "dst_unit": "$player", "spell_id": 1, "spell": "x", "amount": 1,
            "dst_hp_pct": 40.0, "src_role": "bot", "dst_role": "player", "origin": "ta"}


def latency_rule(**check_over):
    check = {"metric": "latency", "trigger": {"kind": "damage"},
             "response": {"kind": "heal", "src": "$bot", "dst": "$player"}, "within_s": 3}
    check.update(check_over)
    return {"id": "LAT", "desc": "x", "source": "s", "expect_today": "?", "why_expect": "x",
            "check": check, "pass_if": ">= 0.8", "min_opportunity_s": 0, "min_n": 0}


class DebounceResponderTest(unittest.TestCase):
    # triggers at 10, 16 and 45; the bot answers only the first (at 11): its ability is on a 30 s cooldown
    TRIGGERS = [mob_damage(10.0), mob_damage(16.0), mob_damage(45.0)]
    ROWS = TRIGGERS + [bot_heal(11.0)]

    def run_rule(self, **check_over):
        trial = tt._trial()
        trial["rules"] = [latency_rule(**check_over)]
        ctx = {"guids": {"player": PLAYER, "bot": BOT, "mobs": [MOB]}, "thresholds": {}, "trial": trial}
        return rules_mod.evaluate(trial["rules"][0], list(self.ROWS), [], [], ctx)

    def test_off_by_default_every_trigger_counts(self):
        res = self.run_rule()
        self.assertEqual((res["k"], res["n"]), (1, 3))

    def test_a_trigger_inside_the_responders_cooldown_is_skipped_not_missed(self):
        # the failing case before the change: the option did not exist, 16 s counted as a miss
        res = self.run_rule(debounce_responder_s=30)
        self.assertEqual((res["k"], res["n"]), (1, 2))        # 10 answered, 16 skipped, 45 missed

    def test_the_cooldown_ends_after_n_seconds(self):
        res = self.run_rule(debounce_responder_s=5)           # 16 is 5 s after the answer at 11: counted
        self.assertEqual((res["k"], res["n"]), (1, 3))
        res = self.run_rule(debounce_responder_s=5.5)
        self.assertEqual((res["k"], res["n"]), (1, 2))

    def test_only_the_responders_own_responses_start_a_cooldown(self):
        other = dict(bot_heal(11.0), src_guid="0xF13000000000FFFF", src_unit="", src_role="other")
        self.ROWS = self.TRIGGERS + [other]
        res = self.run_rule(debounce_responder_s=30)
        self.assertEqual((res["k"], res["n"]), (0, 3))

    def test_the_cooldown_is_on_the_ability_not_the_target(self):
        # the answer at 11 went to another unit; a response to a different dst still starts the cooldown
        elsewhere = dict(bot_heal(11.0), dst_guid="0x0000000000000009", dst_unit="")
        self.ROWS = self.TRIGGERS + [elsewhere]
        res = self.run_rule(response={"kind": "heal", "src": "$bot"}, debounce_responder_s=30)
        self.assertEqual(res["n"], 2)

    def test_a_cooldown_skipped_trigger_does_not_start_its_mobs_debounce_window(self):
        # the reviewer's repro (counterspell): mob B casts at 10 and is answered at 11; mob A casts at 20
        # (inside the 30 s cooldown: skipped) and again at 45 (the bot is free: a miss that must count).
        # Before the fix the skipped t=20 trigger started mob A's 30 s debounce window and hid the t=45 miss.
        mob_a, mob_b = MOB, "0xF13000000000BEEF"

        def cast_start(t, guid):
            return dict(mob_damage(t), src_guid=guid)
        self.ROWS = [cast_start(10.0, mob_b), bot_heal(11.0), cast_start(20.0, mob_a), cast_start(45.0, mob_a)]
        res = self.run_rule(debounce_responder_s=30, trigger_debounce_s=30)
        self.assertEqual((res["k"], res["n"]), (1, 2))

    def test_only_the_listed_response_spells_start_the_cooldown(self):
        from trl import spells
        taunt, mocking = min(spells.ids_for("Taunt")), min(spells.ids_for("Mocking Blow"))
        rows = [mob_damage(10.0), dict(bot_heal(11.0), spell_id=mocking, spell="Mocking Blow"), mob_damage(16.0)]
        resp = {"kind": "heal", "src": "$bot", "spell": ["Taunt", "Mocking Blow"]}
        self.ROWS = rows
        # every response spell starts the cooldown (the default): 16 is skipped
        res = self.run_rule(response=resp, debounce_responder_s=10)
        self.assertEqual(res["n"], 1)
        # only Taunt does: the Mocking Blow at 11 does not, so 16 counts (a miss)
        res = self.run_rule(response=resp, debounce_responder_s=10, debounce_responder_spells=["Taunt"])
        self.assertEqual((res["k"], res["n"]), (1, 2))
        # a Taunt at 11 does start it
        self.ROWS = [mob_damage(10.0), dict(bot_heal(11.0), spell_id=taunt, spell="Taunt"), mob_damage(16.0)]
        res = self.run_rule(response=resp, debounce_responder_s=10, debounce_responder_spells=["Taunt"])
        self.assertEqual(res["n"], 1)

    def test_spells_validator(self):
        def errs(spells, cd=10):
            trial = tt._trial()
            rule = latency_rule(response={"kind": "heal", "src": "$bot", "spell": ["Taunt", "Mocking Blow"]},
                                debounce_responder_spells=spells)
            if cd is not None:
                rule["check"]["debounce_responder_s"] = cd
            trial["rules"] = [rule]
            return [e for e in scenario.validate(trial) if "debounce_responder_spells" in e]
        self.assertEqual(errs(["Taunt"]), [])
        for bad in ([], "Taunt", [""], [3], ["Kick"]):
            self.assertTrue(errs(bad), bad)
        self.assertTrue(errs(["Taunt"], cd=None))             # needs debounce_responder_s

    def test_validator(self):
        def errs(value, metric="latency"):
            trial = tt._trial()
            rule = latency_rule(debounce_responder_s=value)
            rule["check"]["metric"] = metric
            trial["rules"] = [rule]
            return [e for e in scenario.validate(trial) if "debounce_responder_s" in e]
        self.assertEqual(errs(30), [])
        self.assertEqual(errs(2.5), [])
        for bad in (0, -1, "x", True, None.__class__):
            self.assertTrue(errs(bad), bad)
        self.assertTrue(errs(30, metric="count"))             # latency only


class NotWithinAfterMarkTest(unittest.TestCase):
    def count_rule(self, spec=None):
        where = {"kind": "heal", "src": "$bot"}
        if spec is not None:
            where["not_within_s_after_mark"] = spec
        return {"id": "C", "desc": "x", "source": "s", "expect_today": "?", "why_expect": "x",
                "check": {"metric": "count", "where": where}, "pass_if": ">= 0", "min_opportunity_s": 0, "min_n": 0}

    def count(self, spec, marks, wall=False):
        trial = tt._trial()
        trial["rules"] = [self.count_rule(spec)]
        ctx = {"guids": {"player": PLAYER, "bot": BOT}, "thresholds": {}, "trial": trial}
        rows = [bot_heal(5.0), bot_heal(12.0), bot_heal(30.0)]
        return rules_mod.evaluate(trial["rules"][0], rows, [], marks, ctx)["value"]

    def test_events_within_s_after_each_refill_mark_are_dropped(self):
        marks = [{"kind": "refill", "t": 10.0}]
        self.assertEqual(self.count(None, marks), 3)
        # the failing case before the change: the key was unknown and changed nothing
        self.assertEqual(self.count({"kind": "refill", "s": 5}, marks), 2)           # 12 s dropped
        self.assertEqual(self.count({"kind": "refill", "s": 1}, marks), 3)           # 12 s is outside
        self.assertEqual(self.count({"kind": "other_kind", "s": 5}, marks), 3)       # a different mark kind

    def test_every_mark_of_the_kind_counts(self):
        marks = [{"kind": "refill", "t": 4.0}, {"kind": "refill", "t": 29.0}]
        self.assertEqual(self.count({"kind": "refill", "s": 2}, marks), 1)           # 5 and 30 dropped

    def test_marks_are_put_on_the_events_time_base_through_their_wall_clock(self):
        # marks.jsonl times start at setup_ok; events and samples count from fight_start
        marks = [{"kind": "setup_ok", "t": 0.0, "wall": "2026-11-08T10:00:00.000+00:00"},
                 {"kind": "fight_start", "t": 10.0, "wall": "2026-11-08T10:00:10.000+00:00"},
                 {"kind": "refill", "t": 22.0, "wall": "2026-11-08T10:00:22.000+00:00"}]   # = 12 s after fight_start
        self.assertEqual(self.count({"kind": "refill", "s": 5}, marks), 2)           # the heal at 12 is dropped
        self.assertEqual(rules_mod.mark_spans(marks, "refill", 5), [(12.0, 17.0)])

    def test_samples_in_the_span_are_dropped_too(self):
        trial = tt._trial()
        rule = {"id": "U", "desc": "x", "source": "s", "expect_today": "?", "why_expect": "x",
                "check": {"metric": "uptime", "unit": "$player", "aura": [71], "phase": "in_combat",
                          "where": {"not_within_s_after_mark": {"kind": "refill", "s": 9}}},
                "pass_if": ">= 0.5", "min_opportunity_s": 0, "min_n": 0}
        samples = [tt._sample(float(t), PLAYER, 500, combat=True, auras=[71] if t < 15 else [])
                   for t in range(20)]
        ctx = {"guids": {"player": PLAYER}, "trial": trial}
        marks = [{"kind": "refill", "t": 10.0}]
        self.assertAlmostEqual(rules_mod.evaluate(rule, [], samples, marks, ctx)["value"], 1.0)
        self.assertAlmostEqual(rules_mod.evaluate(rule, [], samples, [], ctx)["value"], 0.75)

    def test_excluded_span_rows_are_kept(self):
        trial = tt._trial()
        trial["rules"] = [self.count_rule({"kind": "refill", "s": 20})]
        ctx = {"guids": {"player": PLAYER, "bot": BOT}, "thresholds": {}, "trial": trial}
        rows = [bot_heal(12.0), tt._excluded(11.0, 14.0)]
        res = rules_mod.evaluate(trial["rules"][0], rows, [], [{"kind": "refill", "t": 10.0}], ctx)
        self.assertEqual(res["value"], 0)
        # the excluded row survived the filter, so it still excludes (a hit inside it would not score)
        rows2 = [bot_heal(12.0), tt._excluded(11.0, 14.0)]
        trial["rules"] = [self.count_rule(None)]
        self.assertEqual(rules_mod.evaluate(trial["rules"][0], rows2, [], [], ctx)["value"], 0)

    def test_validator(self):
        def errs(spec, label="where"):
            trial = tt._trial()
            rule = self.count_rule(None)
            if label == "where":
                rule["check"]["where"]["not_within_s_after_mark"] = spec
            else:
                rule["check"] = {"metric": "ratio", "num": {"kind": "heal", "not_within_s_after_mark": spec},
                                 "den": {"kind": "heal"}}
            trial["rules"] = [rule]
            return [e for e in scenario.validate(trial) if "not_within_s_after_mark" in e]
        self.assertEqual(errs({"kind": "refill", "s": 5}), [])
        self.assertEqual(errs({"kind": "refill", "s": 2.5}), [])
        for bad in ({"kind": "refill"}, {"s": 5}, {"kind": "", "s": 5}, {"kind": "refill", "s": 0},
                    {"kind": "refill", "s": -1}, {"kind": "refill", "s": True}, {"kind": 3, "s": 5},
                    {"kind": "refill", "s": 5, "extra": 1}, "refill", 5):
            self.assertTrue(errs(bad), bad)
        self.assertTrue(errs({"kind": "refill", "s": 5}, label="num"))     # only valid in check.where

    def test_a_latency_rule_may_carry_it_in_check_where(self):
        trial = tt._trial()
        rule = latency_rule(where={"not_within_s_after_mark": {"kind": "refill", "s": 5}})
        trial["rules"] = [rule]
        self.assertEqual([e for e in scenario.validate(trial) if "not_within" in e or "needs a kind" in e], [])


class RatePerMobAliveTest(unittest.TestCase):
    """DPS-MAG-THREAT-DAMAGE counted nukes per repeat: a mob that dies after 2-3 nukes made it unpassable."""

    NUKE = {"kind": "cast", "src": "$bot", "dst": "$mob", "spell": ["Fireball"]}

    def rule(self, per="mob_alive", min_alive_s=15, pass_if=">= 8"):
        check = {"metric": "rate", "where": dict(self.NUKE)}
        if per:
            check["per"] = per
        if min_alive_s:
            check["min_alive_s"] = min_alive_s
        return {"id": "R", "desc": "x", "source": "s", "expect_today": "?", "why_expect": "x", "check": check,
                "pass_if": pass_if, "min_opportunity_s": 0, "min_n": 0}

    @staticmethod
    def nuke(t):
        return {"t": t, "kind": "cast", "src": "b", "src_guid": BOT, "dst": "m", "dst_guid": MOB,
                "src_unit": "$bot", "dst_unit": "$mob", "spell_id": 133, "spell": "Fireball", "amount": None,
                "dst_hp_pct": 90.0, "src_role": "bot", "dst_role": "mob", "origin": "ta"}

    @staticmethod
    def samples(alive_s, total_s=180):
        out = []
        for t in range(total_s):
            out.append(tt._sample(float(t), PLAYER, 800))
            out.append({"t": float(t), "unit": "mob:" + MOB.lower(), "guid": MOB.lower(),
                        "hp": 500 if t < alive_s else 0, "max_hp": 2000, "in_combat": t < alive_s, "auras": []})
        return out

    def evaluate(self, rule, rows, samples):
        trial = tt._trial()
        trial["rules"] = [rule]
        ctx = {"guids": {"player": PLAYER, "bot": BOT, "mobs": [MOB]}, "thresholds": {}, "trial": trial}
        return rules_mod.evaluate(rule, rows, samples, [], ctx)

    def test_a_short_lived_mob_does_not_sink_an_active_mage(self):
        # 6 nukes while the mob lives for 20 s of a 180 s repeat: 18 a minute alive, 2 a minute scored
        rows = [self.nuke(2.0 + 3.3 * i) for i in range(6)]
        res = self.evaluate(self.rule(), rows, self.samples(20))
        self.assertEqual(res["verdict"], "PASS")
        self.assertAlmostEqual(res["value"], 18.0, places=0)
        # the failing case before the change: the per-repeat rate over the scored span reads 2 a minute
        res = self.evaluate(self.rule(per=None, min_alive_s=None), rows, self.samples(20))
        self.assertLess(res["value"], 8)

    def test_a_blocked_mage_misses_it_with_a_long_lived_mob(self):
        rows = [self.nuke(10.0), self.nuke(60.0), self.nuke(120.0)]      # 3 nukes in 180 s alive: 1 a minute
        res = self.evaluate(self.rule(), rows, self.samples(180))
        self.assertEqual(res["verdict"], "FAIL")
        self.assertAlmostEqual(res["value"], 1.0, places=1)

    def test_a_mob_alive_too_briefly_is_inconclusive(self):
        res = self.evaluate(self.rule(), [self.nuke(1.0)], self.samples(8))
        self.assertEqual(res["verdict"], "INCONCLUSIVE")

    def test_the_union_of_mobs_counts_each_second_once(self):
        samples = self.samples(20)
        samples += [{"t": float(t), "unit": "mob:0xf130000000000bbb", "guid": "x", "hp": 400, "max_hp": 900,
                     "in_combat": True, "auras": []} for t in range(10, 30)]
        self.assertEqual(rules_mod.mob_alive_seconds(samples, []), 30.0)

    def test_validator(self):
        def errs(**over):
            trial = tt._trial()
            rule = self.rule()
            rule["check"].update(over)
            trial["rules"] = [rule]
            return [e for e in scenario.validate(trial) if "per" in e or "min_alive_s" in e]
        self.assertEqual(errs(), [])
        self.assertTrue(errs(per="alive"))
        self.assertTrue(errs(min_alive_s=0))
        self.assertTrue(errs(min_alive_s="x"))
        trial = tt._trial()
        rule = self.rule()
        rule["check"]["metric"] = "count"
        trial["rules"] = [rule]
        self.assertTrue([e for e in scenario.validate(trial) if "bad per" in e])      # rate rules only


if __name__ == "__main__":
    unittest.main()
