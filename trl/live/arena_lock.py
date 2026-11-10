"""Exclusive per-arena locks between parallel runs on one machine.

Lock file: `~/.local/state/tortoise-trials/arena-<server>-<arena id>.lock`,
taken with `fcntl.flock(LOCK_EX | LOCK_NB)`. The fd stays open (the lock is
held) until `release()`; the OS drops the lock on close or on a crash, so a
lock never goes stale. Two runs on one machine never share an arena; two
threads of one run exclude each other the same way.

Standard library only.
"""

import fcntl
import os
import re

STATE_DIR = os.path.join(os.path.expanduser("~"), ".local", "state", "tortoise-trials")

_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def _safe(part):
    """A filename-safe word (server and arena ids are already plain, this is belt and braces)."""
    return _SAFE.sub("_", str(part or ""))


def lock_path(server, arena_id, base_dir=None):
    """The lock file for one (server, arena). `base_dir` is for the tests (else STATE_DIR)."""
    base = base_dir or STATE_DIR
    return os.path.join(base, "arena-%s-%s.lock" % (_safe(server), _safe(arena_id)))


class ArenaLocked(Exception):
    pass


class ArenaLock:
    """One arena's flock lock. A context manager; the repeat-held locks use
    acquire()/release() paired with the repeat teardown's `finally` (every exit
    path, exceptions included)."""

    def __init__(self, server, arena_id, base_dir=None):
        self.path = lock_path(server, arena_id, base_dir)
        self._f = None

    @property
    def held(self):
        return self._f is not None

    def acquire(self):
        """Take the lock without blocking; False when another slot holds it."""
        if self._f is not None:
            return True
        parent = os.path.dirname(self.path)
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError:
                return False
        try:
            f = open(self.path, "a+b")
        except OSError:
            return False
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                f.close()
            except OSError:
                pass
            return False
        self._f = f
        return True

    def release(self):
        f, self._f = self._f, None
        if f is None:
            return
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                f.close()
            except OSError:
                pass

    def __enter__(self):
        if not self.acquire():
            raise ArenaLocked(self.path)
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    def __del__(self):
        try:
            self.release()
        except Exception:  # noqa: BLE001 - a destructor must not raise
            pass
