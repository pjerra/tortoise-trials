"""Tests for the follow-up fixes (card from `run`, teardown naming, level_delta, pacer line, ...)."""

import json
import os
import pathlib
import sys
import tempfile
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_live as tl  # noqa: E402
from trl.live import run  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
