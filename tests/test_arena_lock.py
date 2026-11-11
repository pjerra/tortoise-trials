"""Exclusive per-arena file locks between parallel runs on one machine.

Two slots never hold the same arena: the slot walks its rotated candidates,
skips a locked arena WITHOUT probing it, takes the first unlocked arena that
is also quiet, and holds the lock until the repeat's teardown is done. No
server is contacted: the ArenaLock tests use real flock files in a temp dir,
the Situation tests use the FakeAgent rig from test_live.
"""

import os
import sys
import tempfile
import threading
import unittest
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import test_live as tl  # noqa: E402
from trl.live import arena_lock, gm_guard, run, situations  # noqa: E402


def trial():
    return {"id": "t", "player": {"level": 35},
            "situation": {"mobs": {"role": "melee", "count": 1}}}


def pool():
    return [
        {"id": "a", "map": 0, "x": 1, "y": 2, "z": 3, "level_min": 1, "level_max": 60},
        {"id": "b", "map": 0, "x": 11, "y": 12, "z": 13, "level_min": 1, "level_max": 60},
        {"id": "c", "map": 0, "x": 21, "y": 22, "z": 23, "level_min": 1, "level_max": 60},
        {"id": "d", "map": 0, "x": 31, "y": 32, "z": 33, "level_min": 1, "level_max": 60},
    ]


def sit(agent, tmp, state_dir, arenas, slot_index=0, server="gamma", quiet=True, noisy=None):
    s = situations.Situation(
        agent, trial(), gm_guard.GmGuard(agent, tl.new_ledger(tmp), tl.ctx_for(
            arena_coords=frozenset({(0, 1, 2, 3), (0, 11, 12, 13),
                                    (0, 21, 22, 23), (0, 31, 32, 33)}))),
        tl.new_ledger(tmp), tl.FakeMarks(), tl.StubCollector(),
        situations.SitContext(player_guid=tl.PLAYER),
        arenas=arenas, mobs={}, slot_index=slot_index, server=server,
        arena_state_dir=state_dir)
    if noisy is not None:
        s.quiet_ok = lambda: noisy.pop(0) if noisy else False
    else:
        s.quiet_ok = lambda: quiet
    return s


class ArenaLockTest(unittest.TestCase):
    def test_second_acquire_fails_while_the_first_is_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = arena_lock.ArenaLock("gamma", "a", base_dir=tmp)
            self.assertTrue(first.acquire())
            self.assertTrue(first.held)
            second = arena_lock.ArenaLock("gamma", "a", base_dir=tmp)
            self.assertFalse(second.acquire())
            self.assertFalse(second.held)
            first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_lock_files_live_in_state_dir(self):
        self.assertEqual(arena_lock.lock_path("gamma", "unga-ridge"),
                         os.path.join(arena_lock.STATE_DIR, "arena-gamma-unga-ridge.lock"))

    def test_release_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = arena_lock.ArenaLock("gamma", "a", base_dir=tmp)
            lock.release()
            self.assertTrue(lock.acquire())
            lock.release()
            lock.release()


class ArenaFileLockTest(unittest.TestCase):
    """Two simulated slots and 4 arenas, one arena not quiet."""

    def test_two_slots_never_hold_the_same_arena(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state")
            held = {}
            lock = threading.Lock()

            def worker(index, out):
                agent = tl.FakeAgent()
                # arena a is not quiet (every probe of it fails); the rest are quiet
                s = sit(agent, tmp, state, pool(), slot_index=index)
                probes = {"a": False}
                s.quiet_ok = lambda: probes.get(s._probing, True)
                orig_send = s.guard.send

                def send(kind, target=None, *args, **kwargs):
                    if kind == "go_xyz" and args:
                        for arena in pool():
                            if int(arena["x"]) == int(args[0]) and int(arena["y"]) == int(args[1]):
                                s._probing = arena["id"]
                    return orig_send(kind, target, *args, **kwargs)
                s.guard.send = send
                with unittest.mock.patch.object(situations.time, "sleep", lambda *_: None):
                    with unittest.mock.patch.object(situations, "ARENA_WAIT_S", 0.01), \
                         unittest.mock.patch.object(situations, "ARENA_GIVE_UP_S", 30.0):
                        arena = s.arena()
                with lock:
                    held.setdefault(arena["id"], []).append(index)
                out[index] = (arena["id"], s)
                # hold the arena until both slots hold one, like a real repeat;
                # bounded so a stuck slot fails instead of hanging the suite
                import time as _time
                give_up = _time.time() + 60.0
                while True:
                    with lock:
                        if len(held) >= 2:
                            break
                    if _time.time() >= give_up:
                        out[index] = (arena["id"], s, "STUCK: other slot never took an arena")
                        s.release_arena()
                        return
                    _time.sleep(0.01)
                s.release_arena()

            out = {}
            threads = [threading.Thread(target=worker, args=(i, out)) for i in (0, 1)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=60)
            alive = [t.is_alive() for t in threads]
            self.assertEqual(alive, [False, False],
                             "slots still running after 60 s join: %s (held=%s)" % (alive, held))
            self.assertNotEqual(out[0][0], out[1][0])
            self.assertNotIn("a", held, "the not-quiet arena is never taken")

    def test_lock_is_released_after_an_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state")
            agent = tl.FakeAgent()
            s = sit(agent, tmp, state, pool()[:1])
            s.quiet_ok = lambda: True

            def boom(*args, **kwargs):
                raise RuntimeError("probe exploded")
            s.guard.send = boom
            with unittest.mock.patch.object(situations.time, "sleep", lambda *_: None):
                with self.assertRaises(RuntimeError):
                    s.arena()
            self.assertTrue(arena_lock.ArenaLock("gamma", "a", base_dir=state).acquire(),
                            "the lock is free again after the exception")

    def test_a_locked_arena_is_skipped_without_probing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state")
            holder = arena_lock.ArenaLock("gamma", "a", base_dir=state)
            self.assertTrue(holder.acquire())
            try:
                agent = tl.FakeAgent()
                s = sit(agent, tmp, state, pool()[:2])
                s.quiet_ok = lambda: True
                with unittest.mock.patch.object(situations.time, "sleep", lambda *_: None):
                    self.assertEqual(s.arena()["id"], "b")
                goes = [c for c in agent.gm if c.startswith(".go xyz")]
                self.assertNotIn(".go xyz 1 2 3 0", goes)
                self.assertIn(".go xyz 11 12 13 0", goes)
            finally:
                holder.release()

    def test_all_locked_gives_up_with_no_quiet_arena(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state")
            holders = [arena_lock.ArenaLock("gamma", aid, base_dir=state)
                       for aid in ("a", "b", "c", "d")]
            for h in holders:
                self.assertTrue(h.acquire())
            try:
                agent = tl.FakeAgent()
                s = sit(agent, tmp, state, pool())
                s.quiet_ok = lambda: True
                sleeps = []
                with unittest.mock.patch.object(situations, "ARENA_WAIT_S", 0.01), \
                     unittest.mock.patch.object(situations, "ARENA_GIVE_UP_S", 0.05), \
                     unittest.mock.patch.object(situations.time, "sleep", lambda v: sleeps.append(v)):
                    self.assertIsNone(s.acquire_arena())
            finally:
                for h in holders:
                    h.release()

if __name__ == "__main__":
    unittest.main()
