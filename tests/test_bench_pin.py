"""P0: runs use a pinned, detached worktree.

Failing-first: bench_state does not exist yet, and main() has no pin gate.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import test_live as tl
import test_followups as tf  # noqa: E402
from trl.live import run  # noqa: E402


def git(repo, *args):
    subprocess.run(["git", "-C", repo] + list(args), check=True,
                   capture_output=True, text=True)


def make_repo(detached=False, dirty=False):
    tmp = tempfile.mkdtemp()
    git(tmp, "init", "-q")
    git(tmp, "config", "user.email", "t@t.t")
    git(tmp, "config", "user.name", "t")
    git(tmp, "config", "commit.gpgsign", "false")
    with open(os.path.join(tmp, "f.txt"), "w") as f:
        f.write("x\n")
    git(tmp, "add", "f.txt")
    git(tmp, "commit", "-qm", "init")
    git(tmp, "branch", "-M", "main")
    if detached:
        git(tmp, "checkout", "--detach", "HEAD", "-q")
    if dirty:
        with open(os.path.join(tmp, "f.txt"), "a") as f:
            f.write("edit\n")
    return tmp


class BenchStateTest(unittest.TestCase):
    def test_branch_checkout_is_not_detached(self):
        repo = make_repo(detached=False)
        st = run.bench_state(repo)
        self.assertFalse(st["detached"])
        self.assertFalse(st["dirty"])
        self.assertNotIn("error", {k for k in st if st[k] is None} if False else set())
        self.assertIsNone(st.get("error"))
        self.assertEqual(len(st["sha"]), 40)

    def test_detached_clean_checkout(self):
        repo = make_repo(detached=True)
        st = run.bench_state(repo)
        self.assertTrue(st["detached"])
        self.assertFalse(st["dirty"])
        self.assertIsNone(st.get("error"))

    def test_detached_dirty_checkout(self):
        repo = make_repo(detached=True, dirty=True)
        st = run.bench_state(repo)
        self.assertTrue(st["detached"])
        self.assertTrue(st["dirty"])

    def test_not_a_git_repo_counts_as_unpinned(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = run.bench_state(tmp)
        self.assertFalse(st["detached"])
        self.assertIsNotNone(st.get("error"))


class PinGateTest(unittest.TestCase):
    def args(self, tmp):
        return tl.run_args(tmp)

    def test_run_refuses_a_branch_checkout(self):
        repo = make_repo(detached=False)
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(run, "TOOL_ROOT", repo), \
                 unittest.mock.patch.object(run.lease, "place_allowed",
                                             side_effect=AssertionError("lease contacted")):
                self.assertEqual(run.main(self.args(tmp)), 4)

    def test_run_refuses_modified_tracked_file(self):
        repo = make_repo(detached=True, dirty=True)
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(run, "TOOL_ROOT", repo), \
                 unittest.mock.patch.object(run.lease, "place_allowed",
                                             side_effect=AssertionError("lease contacted")):
                self.assertEqual(run.main(self.args(tmp)), 4)

    def test_override_records_unpinned(self):
        repo = make_repo(detached=False)
        with tempfile.TemporaryDirectory() as tmp:
            args = self.args(tmp)
            holder = {}

            def fake_execute(self_, trials):
                holder["run"] = self_
                os.makedirs(self_.run_dir, exist_ok=True)
                self_.write_run_json([])
                return 0

            with unittest.mock.patch.object(run, "TOOL_ROOT", repo), \
                 unittest.mock.patch.object(run.lease, "place_allowed", lambda s, b=None: (True, "alpha")), \
                 unittest.mock.patch.object(run.lease, "pick_free", lambda b, s, n: [tf.slot_row(1)]), \
                 unittest.mock.patch.object(run.Run, "execute", fake_execute), \
                 unittest.mock.patch.object(run, "_install_signals", lambda: None), \
                 unittest.mock.patch.dict(os.environ, {"TRIALS_ALLOW_UNPINNED": "1"}):
                self.assertEqual(run.main(args), 0)
            with open(os.path.join(holder["run"].run_dir, "RUN.json")) as f:
                doc = json.load(f)
        self.assertTrue(doc.get("bench_unpinned"))
        self.assertFalse(doc.get("bench_detached"))

    def test_pinned_run_records_sha_and_flags(self):
        repo = make_repo(detached=True)
        with tempfile.TemporaryDirectory() as tmp:
            args = self.args(tmp)
            holder = {}

            def fake_execute(self_, trials):
                holder["run"] = self_
                os.makedirs(self_.run_dir, exist_ok=True)
                self_.write_run_json([])
                return 0

            with unittest.mock.patch.object(run, "TOOL_ROOT", repo), \
                 unittest.mock.patch.object(run.lease, "place_allowed", lambda s, b=None: (True, "alpha")), \
                 unittest.mock.patch.object(run.lease, "pick_free", lambda b, s, n: [tf.slot_row(1)]), \
                 unittest.mock.patch.object(run.Run, "execute", fake_execute), \
                 unittest.mock.patch.object(run, "_install_signals", lambda: None), \
                 unittest.mock.patch.dict(os.environ, {}, clear=False):
                env = {k: v for k, v in os.environ.items() if k != "TRIALS_ALLOW_UNPINNED"}
                with unittest.mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(run.main(args), 0)
            with open(os.path.join(holder["run"].run_dir, "RUN.json")) as f:
                doc = json.load(f)
        self.assertEqual(len(doc.get("bench_sha") or ""), 40)
        self.assertTrue(doc.get("bench_detached"))
        self.assertFalse(doc.get("bench_dirty"))
        self.assertNotIn("bench_unpinned", doc)

    def test_dry_run_skips_the_pin_check(self):
        repo = make_repo(detached=False)
        with tempfile.TemporaryDirectory() as tmp:
            args = self.args(tmp)
            args.dry_run = True
            with unittest.mock.patch.object(run, "TOOL_ROOT", repo), \
                 unittest.mock.patch.object(run.lease, "place_allowed", lambda s, b=None: (True, "alpha")), \
                 unittest.mock.patch.object(run.lease, "pick_free", lambda b, s, n: [tf.slot_row(1)]):
                self.assertEqual(run.main(args), 0)


if __name__ == "__main__":
    unittest.main()
