"""Tests for trial variants (`variants` in a trial file): merge semantics, refusals, selection, ids in the
run folder, the report and the baseline diff. No server is contacted.

Run from the tool directory: python3 -I -m unittest discover -s tests
"""

import copy
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import trials as cli  # noqa: E402
from trl import report as report_mod  # noqa: E402
from trl import scenario  # noqa: E402
from trl import score as score_mod  # noqa: E402
from trl.live import run as live_run  # noqa: E402

HEAL = os.path.join(ROOT, "trials", "healers", "priest-holy-heal-latency.json")
TANK = os.path.join(ROOT, "trials", "tanks", "tank-warrior-taunt-off-healer.json")

PLAYER = "0x0000000000000001"
BOT = "0x0000000000000002"
MOB = "0xf13000000000abcd"


def base_trial():
    """A tank trial with a gear kit, an extra bot and two rules (a small, valid trial to vary)."""
    trial = scenario.load_trial(TANK)
    trial.pop("variants", None)
    return trial


def with_variants(*variants):
    trial = base_trial()
    trial["variants"] = list(variants)
    return trial


def var(name, **override):
    return {"name": name, "override": override}


class MergeTest(unittest.TestCase):
    def test_deep_merge_dicts_merge_lists_and_scalars_replace(self):
        base = {"a": {"b": 1, "c": [1, 2]}, "d": 5}
        out = scenario.deep_merge(base, {"a": {"c": [9]}, "d": None, "e": {"f": 1}})
        self.assertEqual(out, {"a": {"b": 1, "c": [9]}, "d": None, "e": {"f": 1}})
        self.assertEqual(base, {"a": {"b": 1, "c": [1, 2]}, "d": 5}, "the base is never changed")

    def test_level_sets_player_bot_and_every_extra(self):
        trial = with_variants(var("l50", level=50))
        merged, errs = scenario.apply_override(trial, trial["variants"][0]["override"])
        self.assertEqual(errs, [])
        self.assertEqual(merged["player"]["level"], 50)
        self.assertEqual(merged["bot"]["level"], 50)
        self.assertEqual([b["level"] for b in merged["extra_bots"]], [50])
        self.assertNotIn("variants", merged)
        self.assertEqual(trial["player"]["level"], 40, "the base trial is untouched")

    def test_explicit_fields_win_over_the_level_shortcut(self):
        trial = with_variants(var("x", level=50, bot={"level": 45}))
        merged, errs = scenario.apply_override(trial, trial["variants"][0]["override"])
        self.assertEqual(errs, [])
        self.assertEqual(merged["bot"]["level"], 45)
        self.assertEqual(merged["player"]["level"], 50)

    def test_nested_situation_field_merges_and_null_clears_gear(self):
        trial = with_variants(var("x", situation={"mobs": {"level_delta": -3}}, player={"gear": None}))
        merged, errs = scenario.apply_override(trial, trial["variants"][0]["override"])
        self.assertEqual(errs, [])
        self.assertEqual(merged["situation"]["mobs"]["level_delta"], -3)
        self.assertEqual(merged["situation"]["mobs"]["count"], 3, "the rest of mobs stays")
        self.assertIsNone(merged["player"]["gear"])
        self.assertEqual(merged["player"]["talents"], trial["player"]["talents"])

    def test_extra_bots_list_is_replaced_whole(self):
        extra = [{"class": "priest", "spec": "holy", "role": "healer", "level": 40, "items": []},
                 {"class": "paladin", "spec": "protection", "role": "tank", "level": 40, "items": []}]
        trial = with_variants(var("x", extra_bots=extra))
        merged, errs = scenario.apply_override(trial, trial["variants"][0]["override"])
        self.assertEqual(errs, [])
        self.assertEqual(len(merged["extra_bots"]), 2)

    def test_rule_thresholds_change_by_id_and_nothing_else(self):
        trial = with_variants(var("x", rules={"TNK-WAR-TAUNT": {"pass_if": ">= 0.5", "min_n": 3}}))
        merged, errs = scenario.apply_override(trial, trial["variants"][0]["override"])
        self.assertEqual(errs, [])
        rule = [r for r in merged["rules"] if r["id"] == "TNK-WAR-TAUNT"][0]
        self.assertEqual((rule["pass_if"], rule["min_n"]), (">= 0.5", 3))
        before = [r for r in trial["rules"] if r["id"] == "TNK-WAR-TAUNT"][0]
        self.assertEqual(rule["check"], before["check"])
        self.assertEqual([r["id"] for r in merged["rules"]], [r["id"] for r in trial["rules"]])


class ValidationTest(unittest.TestCase):
    def errs(self, *variants, mutate=None):
        trial = with_variants(*variants)
        if mutate:
            mutate(trial)
        return scenario.validate(trial)

    def has(self, errs, text):
        self.assertTrue(any(text in e for e in errs), "%r not in %r" % (text, errs))

    def test_a_good_variant_validates(self):
        self.assertEqual(self.errs(var("l50", level=50)), [])

    def test_merged_trial_goes_through_ordinary_validation(self):
        # level 50 with the gear kit and a player level that has no kit
        self.has(self.errs(var("bad", player={"level": 60})), "player.gear tier")
        self.has(self.errs(var("bad", situation={"victim": "extra9"})), "bad victim")
        self.has(self.errs(var("bad", situation={"mobs": {"count": "x"}})), "mobs.count must be int")
        self.has(self.errs(var("bad", player={"stance": "x"})), "player.stance")

    def test_unknown_override_keys_are_refused(self):
        self.has(self.errs(var("bad", rules_list=[])), "may not touch 'rules_list'")
        self.has(self.errs(var("bad", setup_checks=[])), "may not touch 'setup_checks'")
        self.has(self.errs(var("bad", duration_s=10)), "may not touch 'duration_s'")
        self.has(self.errs(var("bad", situation={"nope": 1})), "unknown situation field 'nope'")

    def test_class_spec_and_role_cannot_change(self):
        self.has(self.errs(var("bad", bot={"role": "dps"})), "may not change bot.role")
        self.has(self.errs(var("bad", bot={"class": "mage"})), "may not change bot.class")
        self.has(self.errs(var("bad", player={"class": "mage"})), "may not change player.class")

    def test_a_bot_spec_variant_may_rewrite_a_rules_own_texts(self):
        rule0 = [r for r in base_trial()["rules"] if r["id"] == "TNK-WAR-TAUNT"][0]
        trial = with_variants(var("fr", bot={"spec": "frost"},
                                  rules={"TNK-WAR-TAUNT": {"desc": "frost text",
                                                          "check": dict(rule0["check"])}}))
        self.assertEqual(scenario.validate(trial), [])
        [(name, merged)] = scenario.expand_variants(trial)
        rule = [r for r in merged["rules"] if r["id"] == "TNK-WAR-TAUNT"][0]
        self.assertEqual(rule["desc"], "frost text")
        self.assertEqual(merged["spec"], "frost")
        # without the spec change the same rewrite is refused
        self.has(self.errs(var("bad", rules={"TNK-WAR-TAUNT": {"desc": "x"}})), "only with bot.spec")

    def test_rules_cannot_change_except_three_thresholds(self):
        self.has(self.errs(var("bad", rules={"TNK-WAR-TAUNT": {"check": {"within_s": 9}}})),
                 "only with bot.spec")
        self.has(self.errs(var("bad", rules={"TNK-WAR-TAUNT": {"expect_today": "FAIL"}})),
                 "may not change rule field 'expect_today'")
        self.has(self.errs(var("bad", rules={"TNK-WAR-TAUNT": {"pass_if": "maybe"}})), "bad variant pass_if")
        self.has(self.errs(var("bad", rules={"TNK-WAR-TAUNT": {"min_n": 0}})), "min_n must be an int >= 1")
        self.has(self.errs(var("bad", rules={"TNK-WAR-TAUNT": {"min_opportunity_s": -1}})),
                 "min_opportunity_s must be a number >= 0")
        self.has(self.errs(var("bad", rules=[{"id": "X"}])), "rules must be {rule id")

    def test_bad_level_and_shape(self):
        self.has(self.errs(var("bad", level=61)), "level must be an int 1-60")
        self.has(self.errs(var("bad", level=True)), "level must be an int 1-60")
        self.has(self.errs({"name": "Bad Name", "override": {"level": 50}}), "bad name")
        self.has(self.errs({"name": "x", "override": {}}), "non-empty object")
        self.has(self.errs({"name": "x"}), "non-empty object")
        self.has(self.errs({"name": "x", "override": {"level": 50}, "extra": 1}), "unknown field 'extra'")
        self.has(self.errs(var("a", level=50), var("a", level=45)), "duplicate variant name")
        self.has(self.errs(mutate=lambda t: t.update(variants=[])), "non-empty list")
        self.has(self.errs(mutate=lambda t: t.update(variants="x")), "non-empty list")
        many = [var("v%d" % i, level=45) for i in range(scenario.MAX_VARIANTS + 1)]
        self.has(self.errs(*many), "at most %d" % scenario.MAX_VARIANTS)

    def test_level_below_the_talent_proof_is_refused(self):
        # a Protection warrior hire at level 20: the premade link of that level stops short of the marker talent
        errs = self.errs(var("l20", level=20, player={"gear": None}))
        self.has(errs, "below level 35")

    def test_raid_trials_take_no_variants(self):
        raid = {"id": "r", "group": "raid", "boss": {"entry": 1}, "live": "practice", "variants": [var("x", level=50)]}
        self.has(scenario.validate(raid), "raid trials take no variants")

    def test_a_trial_without_variants_is_unchanged(self):
        trial = base_trial()
        self.assertEqual(scenario.validate(trial), [])
        self.assertEqual(scenario.expand_variants(trial), [])

    def test_every_shipped_variant_validates(self):
        seen = 0
        for path in scenario.trial_files():
            trial = scenario.load_trial(path)
            if "variants" not in trial:
                continue
            self.assertEqual(scenario.validate(trial), [], path)
            expanded = scenario.expand_variants(trial)
            self.assertEqual(len(expanded), len(trial["variants"]), path)
            for name, merged in expanded:
                seen += 1
                self.assertEqual(merged["id"], "%s@%s" % (trial["id"], name))
                self.assertEqual([r["id"] for r in merged["rules"]], [r["id"] for r in trial["rules"]], path)
                same_spec = merged.get("spec") == trial.get("spec")
                if same_spec:
                    self.assertEqual([r["check"] for r in merged["rules"]], [r["check"] for r in trial["rules"]], path)

class ExpandTest(unittest.TestCase):
    def test_ids_and_titles(self):
        trial = with_variants({"name": "l50", "title": "level 50", "override": {"level": 50}})
        [(name, merged)] = scenario.expand_variants(trial)
        self.assertEqual(name, "l50")
        self.assertEqual(merged["id"], trial["id"] + "@l50")
        self.assertEqual(scenario.split_variant_id(merged["id"]), (trial["id"], "l50"))
        self.assertEqual(scenario.split_variant_id(trial["id"]), (trial["id"], None))

    def test_an_invalid_variant_is_not_expanded(self):
        trial = with_variants(var("good", level=50), var("bad", level=20, player={"gear": None}))
        self.assertEqual([n for n, _ in scenario.expand_variants(trial)], ["good"])


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.trials, self.paths = cli._all_trials()

    def resolve(self, *sels):
        class Args:
            pass
        args = Args()
        args.trial = list(sels)
        args.scenario = []
        args.klass = None
        return cli.resolve_trials(args, self.trials, self.paths)

    def test_bare_id_is_the_base_trial_only(self):
        self.assertEqual(self.resolve("priest-holy-heal-latency"), [HEAL])

    def test_variant_id_selects_the_variant(self):
        self.assertEqual(self.resolve("priest-holy-heal-latency@lvl50"), [HEAL + "@lvl50"])

    def test_variant_glob_selects_every_variant_and_not_the_base(self):
        self.assertEqual(self.resolve("priest-holy-heal-latency@*"), [HEAL + "@lvl50", HEAL + "@lvl60"])

    def test_plain_glob_and_class_never_start_variants(self):
        got = self.resolve("priest-holy-heal-*")
        self.assertIn(HEAL, got)
        self.assertFalse([p for p in got if scenario.VARIANT_SEP in os.path.basename(p)], got)
        picked = scenario.find_trials(trials=self.trials, paths=self.paths, bot_class="priest")
        self.assertFalse([p for p in picked if scenario.VARIANT_SEP in os.path.basename(p)], picked)

    def test_unknown_variant_matches_nothing(self):
        with self.assertRaises(ValueError):
            self.resolve("priest-holy-heal-latency@nope")

    def test_runner_loads_the_merged_variant(self):
        trial = live_run.load_trial(HEAL + "@lvl60")
        self.assertEqual(trial["id"], "priest-holy-heal-latency@lvl60")
        self.assertEqual(trial["player"]["level"], 60)
        self.assertEqual(trial["bot"]["level"], 60)
        self.assertEqual(trial["situation"]["mobs"]["level_delta"], -2)
        plain = live_run.load_trial(HEAL)
        self.assertEqual(plain["id"], "priest-holy-heal-latency")
        self.assertEqual(plain["player"]["level"], 40)
        self.assertEqual([r["id"] for r in trial["rules"]], [r["id"] for r in plain["rules"]])
        with self.assertRaises(ValueError):
            live_run.load_trial(HEAL + "@nope")


class ReportTest(unittest.TestCase):
    """The variant is its own trial id in the run folder, the card and the baseline diff."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make_run(self, name, ids, heal_events):
        run_dir = os.path.join(self.tmp, name)
        for tid in ids:
            rep = os.path.join(run_dir, tid, "rep1")
            os.makedirs(rep)
            with open(os.path.join(rep, "setup.json"), "w") as f:
                json.dump({"ok": True, "guids": {"player": PLAYER, "bot": BOT, "extras": [], "mobs": [MOB]},
                           "config": {"critical": 20, "low": 50, "medium": 70}}, f)
            samples = []
            for t in range(0, 201):
                hp = 900 if t < 10 else 400
                samples.append({"t": float(t), "wall": None, "unit": "player", "guid": PLAYER, "hp": hp,
                                "max_hp": 1000, "power": 1, "max_power": 1, "auras": [], "casting": None,
                                "in_combat": True, "target": MOB, "x": 0.0, "y": 0.0, "z": 0.0})
            events = []
            if heal_events.get(tid):
                events.append({"t": 11.0, "wall": None, "kind": "cast", "src": "bot", "src_guid": BOT,
                               "dst": "player", "dst_guid": PLAYER, "src_unit": "bot", "dst_unit": "player",
                               "spell_id": 2060, "spell": "Greater Heal", "amount": 0, "dst_hp_pct": 40,
                               "src_role": "bot", "dst_role": "player", "origin": "ta"})
            for fname, rows in (("samples.jsonl", samples), ("events.jsonl", events),
                                ("marks.jsonl", [{"kind": "fight_start", "t": 0},
                                                 {"kind": "fight_end", "t": 200, "ended": "duration"}])):
                with open(os.path.join(rep, fname), "w") as f:
                    for row in rows:
                        f.write(json.dumps(row) + "\n")
        with open(os.path.join(run_dir, "RUN.json"), "w") as f:
            json.dump({"trials": list(ids)}, f)
        return run_dir

    def rows(self, run_dir):
        with open(os.path.join(run_dir, "card.json")) as f:
            doc = json.load(f)
        return {(r["trial"], r["rule"]): r for r in doc["rows"]}

    def test_card_rows_and_baseline_key_on_the_variant_id(self):
        base_id = "priest-holy-heal-latency"
        ids = [base_id, base_id + "@lvl50"]
        before = self.make_run("before", ids, {base_id: False, base_id + "@lvl50": False})
        after = self.make_run("after", ids, {base_id: True, base_id + "@lvl50": False})
        for run_dir in (before, after):
            scored = score_mod.score_run(run_dir)
            self.assertEqual([t["trial"] for t in scored], ids)
        rows = self.rows(after)
        self.assertEqual(set(t for (t, _r) in rows), set(ids))
        self.assertEqual(rows[(base_id, "PRI-HEAL-LATENCY")]["verdict"], "INCONCLUSIVE")
        diff = {(r["trial"], r["rule"]): r for r in report_mod.compare(after, before)}
        self.assertIn((base_id + "@lvl50", "PRI-HEAL-LATENCY"), diff)
        self.assertIn((base_id, "PRI-HEAL-LATENCY"), diff)
        self.assertEqual(len(diff), 2, "one row per trial id, the variant is not folded into its base")

    def test_a_variant_missing_from_the_baseline_is_its_own_new_row(self):
        base_id = "priest-holy-heal-latency"
        before = self.make_run("before", [base_id], {})
        after = self.make_run("after", [base_id, base_id + "@lvl60"], {})
        score_mod.score_run(before)
        score_mod.score_run(after)
        diff = {(r["trial"], r["rule"]): r for r in report_mod.compare(after, before)}
        new = diff[(base_id + "@lvl60", "PRI-HEAL-LATENCY")]
        self.assertIsNone(new["before"])
        self.assertIsNotNone(diff[(base_id, "PRI-HEAL-LATENCY")]["before"])

    def test_score_run_finds_the_merged_variant(self):
        trial = scenario.find_trial_by_id("priest-holy-heal-latency@lvl60")
        self.assertEqual(trial["player"]["level"], 60)
        self.assertIsNone(scenario.find_trial_by_id("priest-holy-heal-latency@nope"))
        self.assertEqual(scenario.find_trial_by_id("priest-holy-heal-latency")["player"]["level"], 40)


class SnapshotTest(ReportTest):
    """A run scores against the trial it froze before it ran, never against today's trial files."""

    BASE = "priest-holy-heal-latency"

    def definition(self, min_n):
        trial = scenario.load_trial(HEAL)
        trial["variants"] = [{"name": "frozen", "override": {"level": 50, "rules": {"PRI-HEAL-LATENCY": {"min_n": min_n}}}}]
        return trial

    def run_with_snapshots(self, name, min_n, marker=True):
        trial = self.definition(min_n)
        [(_n, merged)] = scenario.expand_variants(trial)
        run_dir = self.make_run(name, [merged["id"]], {})
        live_run.snapshot_trial(run_dir, merged)
        with open(os.path.join(run_dir, "RUN.json"), "w") as f:
            json.dump({"trials": [merged["id"]], "trial_snapshots": marker}, f)
        return run_dir, merged["id"]

    def verdict(self, run_dir, tid):
        return self.rows(run_dir)[(tid, "PRI-HEAL-LATENCY")]["verdict"]

    def test_the_snapshot_is_the_merged_trial(self):
        run_dir, tid = self.run_with_snapshots("snap", 1)
        path = os.path.join(run_dir, tid, scenario.SNAPSHOT_NAME)
        with open(path) as f:
            snap = json.load(f)
        self.assertEqual(snap["id"], tid)
        self.assertEqual(snap["player"]["level"], 50)
        self.assertEqual([r["min_n"] for r in snap["rules"]], [1])
        self.assertNotIn("variants", snap)

    def test_rescore_uses_the_frozen_threshold_not_the_current_definition(self):
        run_dir, tid = self.run_with_snapshots("snap", 1)
        score_mod.score_run(run_dir)
        self.assertEqual(self.verdict(run_dir, tid), "INCONCLUSIVE")  # one trigger, min_n 1, but min_reps 3 with 1 rep
        # the definition now asks for 5 triggers: a rescore from today's definition would say INCONCLUSIVE
        [(_n, today)] = scenario.expand_variants(self.definition(5))
        self.assertEqual(today["rules"][0]["min_n"], 5)
        score_mod.score_run(run_dir)
        self.assertEqual(self.verdict(run_dir, tid), "INCONCLUSIVE")

    def test_rescore_after_the_variant_is_removed_keeps_the_rows(self):
        run_dir, tid = self.run_with_snapshots("snap", 1)
        original = scenario.find_trial_by_id
        scenario.find_trial_by_id = lambda *a, **k: self.fail("a snapshot run must not look the trial up")
        self.addCleanup(setattr, scenario, "find_trial_by_id", original)
        self.assertIsNone(original(tid), "the shipped files carry no such variant: it is gone")
        scored = score_mod.score_run(run_dir)
        self.assertEqual([t["trial"] for t in scored], [tid])
        self.assertEqual(self.verdict(run_dir, tid), "INCONCLUSIVE")

    def test_a_missing_snapshot_in_a_snapshot_run_fails_loudly(self):
        run_dir, tid = self.run_with_snapshots("snap", 1)
        os.remove(os.path.join(run_dir, tid, scenario.SNAPSHOT_NAME))
        with self.assertRaises(ValueError) as ctx:
            score_mod.score_run(run_dir)
        self.assertIn(scenario.SNAPSHOT_NAME, str(ctx.exception))

    def test_an_unreadable_or_foreign_snapshot_fails_loudly(self):
        run_dir, tid = self.run_with_snapshots("snap", 1)
        path = os.path.join(run_dir, tid, scenario.SNAPSHOT_NAME)
        with open(path, "w") as f:
            f.write("{not json")
        with self.assertRaises(ValueError):
            score_mod.score_run(run_dir)
        with open(path, "w") as f:
            json.dump({"id": "other-trial"}, f)
        with self.assertRaises(ValueError):
            score_mod.score_run(run_dir)

    def test_a_legacy_run_without_the_marker_falls_back_and_says_so(self):
        import contextlib
        import io
        run_dir = self.make_run("legacy", [self.BASE], {})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            scored = score_mod.score_run(run_dir)
        self.assertEqual([t["trial"] for t in scored], [self.BASE])
        self.assertIn("no trial snapshots", err.getvalue())
        self.assertIn("CURRENT trial files", err.getvalue())

    def test_the_runner_marks_its_runs_and_writes_a_snapshot_per_trial(self):
        trial = live_run.load_trial(HEAL + "@lvl60")
        run_dir = os.path.join(self.tmp, "r")
        path = live_run.snapshot_trial(run_dir, trial)
        self.assertEqual(path, os.path.join(run_dir, "priest-holy-heal-latency@lvl60", scenario.SNAPSHOT_NAME))
        with open(path) as f:
            self.assertEqual(json.load(f), trial)
        import inspect
        self.assertIn('"trial_snapshots": True', inspect.getsource(live_run.Run.write_run_json))
        self.assertIn("snapshot_trial(self.run_dir, trial)", inspect.getsource(live_run.Run.execute))


if __name__ == "__main__":
    unittest.main()
