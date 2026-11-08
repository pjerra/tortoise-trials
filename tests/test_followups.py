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
from trl.live import agent as agent_mod, gm_guard, run, teardown  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
