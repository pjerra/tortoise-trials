"""Tests for the follow-up fixes (card from `run`, teardown naming, level_delta, pacer line, ...)."""

import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_live as tl  # noqa: E402
from trl import scenario  # noqa: E402
from trl.live import agent as agent_mod, gm_guard, ladder, run, situations, teardown  # noqa: E402


def slot_row(sid):
    return {"server": "s", "slot": {"id": str(sid), "character": "Trldefault%s" % sid, "port": 0},
            "state": "free"}


class RunWritesTheCardTest(unittest.TestCase):
    """`trials run` used to stop after RUN.json: card.md/card.json needed a separate `score`."""

    def run_main(self, tmp, execute_rc=0, writes_run_json=True):
        args = tl.run_args(tmp)
        holder = {}

        def fake_execute(self_, trials):
            holder["run"] = self_
            os.makedirs(self_.run_dir, exist_ok=True)
            if writes_run_json:
                with open(os.path.join(self_.run_dir, "RUN.json"), "w") as f:
                    json.dump({"trials": []}, f)
            return execute_rc

        with unittest.mock.patch.object(run.lease, "place_allowed", lambda s, b=None: (True, "alpha")), \
             unittest.mock.patch.object(run.lease, "pick_free", lambda b, s, n: [slot_row(1)]), \
             unittest.mock.patch.object(run.Run, "execute", fake_execute), \
             unittest.mock.patch.object(run, "_install_signals", lambda: None):
            rc = run.main(args)
        return rc, holder["run"].run_dir

    def test_run_writes_card_md_and_json_at_the_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, run_dir = self.run_main(tmp)
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isfile(os.path.join(run_dir, "card.md")))
            self.assertTrue(os.path.isfile(os.path.join(run_dir, "card.json")))

    def test_the_run_exit_code_survives_scoring(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, run_dir = self.run_main(tmp, execute_rc=5)
            self.assertEqual(rc, 5)
            self.assertTrue(os.path.isfile(os.path.join(run_dir, "card.md")))

    def test_a_scoring_error_is_logged_and_does_not_change_the_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch("trl.score.score_run", side_effect=ValueError("boom")):
                rc, run_dir = self.run_main(tmp, execute_rc=1)
            self.assertEqual(rc, 1)
            self.assertFalse(os.path.isfile(os.path.join(run_dir, "card.md")))

    def test_a_run_that_never_wrote_run_json_is_not_scored(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, run_dir = self.run_main(tmp, execute_rc=4, writes_run_json=False)
            self.assertEqual(rc, 4)
            self.assertFalse(os.path.isfile(os.path.join(run_dir, "card.md")))


OLD_MOB, NEW_MOB = "0xF130000000000D31", "0xF130000000000D47"


class RepeatCleanupScopeTest(unittest.TestCase):
    """The slot ledger holds every earlier repeat's summons; a despawned old corpse answers
    "not in view", which made the whole check UNKNOWN: nothing was killed and one summon stayed."""

    def setup_agent(self, tmp):
        a = tl.FakeAgent(units=[{"guid": NEW_MOB, "entry": 2320, "dead": False}])
        a.unit_rows[agent_mod.norm_guid(OLD_MOB)] = {"_err": "error: -32003 unit %s is not in view" % OLD_MOB}
        led = tl.new_ledger(tmp)
        led.add({"kind": "summon", "guid": OLD_MOB, "entry": 2320})     # an earlier repeat's, despawned
        led.add({"kind": "summon", "guid": NEW_MOB, "entry": 2320})     # this repeat's, still alive
        return a, led

    def test_old_repeats_despawned_summon_no_longer_blocks_the_kill(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, led = self.setup_agent(tmp)
            guard = gm_guard.GmGuard(a, led, tl.ctx_for())
            rows = teardown.repeat_cleanup(a, guard, led, "Helper", tl.PLAYER, wait_s=0.1,
                                           only=[NEW_MOB], absent_is_gone=True)
            dead = [r for r in rows if r["id"] == "mobs_dead"][0]
            self.assertTrue(dead["ok"], dead["evidence"])
            self.assertEqual([agent_mod.norm_guid(g) for g in a.kills], [agent_mod.norm_guid(NEW_MOB)])
            self.assertFalse([r for r in led.rows if r.get("kind") == "mob_leftover"])

    def test_without_the_scope_the_old_summon_still_reads_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, led = self.setup_agent(tmp)
            guard = gm_guard.GmGuard(a, led, tl.ctx_for())
            rows = teardown.repeat_cleanup(a, guard, led, "Helper", tl.PLAYER, wait_s=0.1)
            dead = [r for r in rows if r["id"] == "mobs_dead"][0]
            self.assertFalse(dead["ok"])
            self.assertIn("UNKNOWN", dead["evidence"])
            self.assertEqual(a.kills, [])


class SlotTeardownNamesFailedStepsTest(unittest.TestCase):
    REPORT = """teardown testserver/1 (Someone, port 8000):
ok   rpc            done
FAIL hires          2 hires did not leave
ok   agent_exit     exited
FAIL lease          kept: game-side teardown failed; check the slot
teardown: 2 ok, 0 skipped, 2 failed
"""

    def fake_binary(self, tmp, rc=14, body=None):
        path = os.path.join(tmp, "turtle-agent")
        with open(path, "w") as f:
            f.write("#!/bin/sh\ncat <<'EOF'\n%sEOF\nexit %d\n" % (body or self.REPORT, rc))
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def test_failing_steps_are_named_in_the_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = teardown.slot_teardown(tmp, self.fake_binary(tmp))
            self.assertEqual(out["rc"], 14)
            self.assertTrue(out["teardown_failed"])
            self.assertEqual([x.split(":")[0] for x in out["failed_steps"]], ["hires", "lease"])
            self.assertIn("2 failed", out["detail"])
            self.assertIn("hires: 2 hires did not leave", out["detail"])
            self.assertIn("lease: kept", out["detail"])

    def test_a_clean_teardown_has_no_failed_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = teardown.slot_teardown(tmp, self.fake_binary(tmp, rc=0, body="ok   rpc  done\nteardown: 1 ok, 0 skipped, 0 failed\n"))
            self.assertTrue(out["ok"])
            self.assertEqual(out["failed_steps"], [])
            self.assertEqual(out["detail"], "teardown: 1 ok, 0 skipped, 0 failed")


class LevelDeltaZeroTest(unittest.TestCase):
    """`level_delta: 0` read as +1 (`int(x or 1)`); only an absent value should default to +1."""

    def window(self, mobs):
        trial = tl.load_trial()
        trial["situation"]["mobs"] = mobs
        sit = situations.Situation.__new__(situations.Situation)
        sit.trial, sit.sit = trial, trial["situation"]
        return sit.level_window(), int(trial["player"]["level"])

    def test_an_explicit_zero_is_zero(self):
        (lo, hi), level = self.window({"role": "melee", "count": 1, "level_delta": 0, "refill": True})
        self.assertEqual((lo, hi), (level - 1, level + 1))

    def test_a_missing_or_null_value_is_still_plus_one(self):
        for mobs in ({"role": "melee", "count": 1}, {"role": "melee", "count": 1, "level_delta": None}):
            (lo, hi), level = self.window(mobs)
            self.assertEqual((lo, hi), (level, level + 2))

    def test_other_values_are_unchanged(self):
        for delta in (-2, -1, 1, 2):
            (lo, hi), level = self.window({"role": "melee", "count": 1, "level_delta": delta})
            self.assertEqual((lo, hi), (level + delta - 1, level + delta + 1))

    def test_the_ladder_starts_from_the_explicit_zero(self):
        trial = tl.load_trial()
        trial["situation"]["mobs"] = {"role": "melee", "count": 1, "level_delta": 0, "refill": True}
        self.assertEqual([ladder.params(trial, s)["level_delta"] for s in range(4)], [0, -1, -1, -1])


class PaladinHealLatencySituationTest(unittest.TestCase):
    """It hit the rescue cap on every rep (even the ladder's step 3), so it never scored."""

    def load(self, name):
        return scenario.load_trial(str(ROOT / "trials" / "healers" / (name + ".json")))

    def test_it_is_lighter_than_the_priest_twin_that_scores(self):
        pal = self.load("paladin-holy-heal-latency")["situation"]
        pri = self.load("priest-holy-heal-latency")["situation"]
        self.assertEqual(scenario.validate(self.load("paladin-holy-heal-latency")), [])
        self.assertLessEqual(pal["mobs"]["level_delta"], pri["mobs"]["level_delta"] - 4)   # weaker mob
        self.assertEqual(pal["mobs"]["count"], 1)
        self.assertLess(pal["floor_pct"], pri["floor_pct"])                                # lower rescue floor
        self.assertLessEqual(pal["hp_band"][0], pri["hp_band"][0] - 10)                    # wider band
        self.assertGreaterEqual(pal["max_rescues_per_min"], 12)                            # as the other capped trials

    def test_the_ladder_never_makes_it_harder_and_the_window_has_mobs(self):
        trial = self.load("paladin-holy-heal-latency")
        self.assertEqual({ladder.params(trial, s)["level_delta"] for s in range(4)}, {-4})
        level = trial["player"]["level"]
        lo, hi = level - 4 - 1, level - 4 + 1
        mobs = situations.load_mobs()["melee"]
        inside = [m for m in mobs if m["level_min"] >= lo and m["level_max"] <= hi]
        self.assertGreaterEqual(len(inside), 3)


if __name__ == "__main__":
    unittest.main()
