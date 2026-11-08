"""Login pacing for the trials harness, against realmd's per-IP login throttle.

realmd refuses a logon once the accounts counted from that IP, plus this connection, reach 15
(src/realmd/AuthSocket.cpp:439-457 in tortoise-wow-main4; the count is the query at :444 and the
test at :447-449). The count is of DIFFERENT accounts, not of logins, and it is wrong at the top of
the hour. The bound is `NOW() - '300'`, and MySQL does that subtraction on NOW() as a number
(YYYYMMDDhhmmss minus 300). In minutes 03-59 the result is a valid datetime three minutes back.
In minutes 00-02 the subtraction borrows into the hour (14:01:30 gives 13:97:30), the bound is not
a valid datetime, and the count covers every account whose last_ip is that address, however long
ago. Behind docker NAT every outside client shares one address (172.18.0.1), so on such a server that is 17+
accounts. All 12 refusals in the realmd log came at minutes 00-02 UTC.

LoginPacer keeps a run under that limit:
* hour-start guard: no login starts in UTC minutes 00-02 or before 03:05 (HOUR_GUARD_END_S);
* distinct-account window: at most LOGIN_LIMIT accounts in LOGIN_WINDOW_S (plus WINDOW_MARGIN_S).
  A login by an account already in the window adds no account to realmd's count, so it never
  waits. A login without a known account name counts as an account of its own.

With a state file the window is shared by every trials process on the machine, so a run that
starts right after another one counts the other's logins too.
"""

import fcntl
import json
import logging
import os
import threading
import time
import uuid

LOGIN_LIMIT = 12           # distinct accounts per window; realmd refuses the 15th
LOGIN_WINDOW_S = 180.0     # realmd's effective window in minutes 03-59 (NOW() - '300' is three minutes)
WINDOW_MARGIN_S = 2.0      # a login counts for window + margin: the client's and realmd's clocks differ
HOUR_GUARD_END_S = 185.0   # no login starts before this many seconds past the UTC hour (03:05)
DEFAULT_STATE = os.path.join(os.path.expanduser("~"), ".local", "state", "tortoise-trials-logins.json")


def hour_start_wait(now):
    """Seconds a login that would start at `now` (Unix time) waits for the hour-start guard, else 0.

    Unix time has no time zone and every UTC hour starts at a multiple of 3600 s, so `now % 3600`
    is the number of seconds into the UTC hour.
    """
    into_hour = now % 3600.0
    if into_hour < HOUR_GUARD_END_S:
        return max(0.01, HOUR_GUARD_END_S - into_hour)
    return 0.0


def account_key(account):
    """The account as realmd compares it (its usernames are upper case).

    A login with no account name gets a key of its own, so it counts as a distinct account.
    """
    name = str(account or "").strip().upper()
    return name or "?" + uuid.uuid4().hex


class LoginPacer:
    def __init__(self, limit=LOGIN_LIMIT, window=LOGIN_WINDOW_S, margin=WINDOW_MARGIN_S,
                 clock=time.time, sleep=time.sleep, log=None, state_path=None, debug=None):
        self.limit = int(limit)
        self.window = float(window)
        self.margin = float(margin)
        self.clock = clock
        self.sleep = sleep
        self.log = log or (lambda line: None)
        # one debug line per login, waited or not (default: the stdlib logger "trl.live.pacing" at DEBUG)
        self.debug = debug or logging.getLogger(__name__).debug
        self.state_path = state_path
        self.stamps = []          # (time, account) of the logins let through (this process, or the shared file's)
        self.waits = []           # (what, seconds) of every wait, for the ledger and the tests
        self._lock = threading.Lock()

    # -- the window --------------------------------------------------------

    def _horizon(self, now):
        return now - (self.window + self.margin)

    def _read_state(self):
        try:
            with open(self.state_path) as f:
                rows = json.load(f)
        except (OSError, ValueError):
            return []
        stamps = []
        for row in rows:
            if isinstance(row, list) and len(row) == 2 and isinstance(row[0], (int, float)):
                stamps.append((float(row[0]), account_key(row[1])))
            elif isinstance(row, (int, float)):      # the old format: a bare time, no account name
                stamps.append((float(row), account_key(None)))
        return stamps

    def _write_state(self, stamps):
        tmp = "%s.%d.tmp" % (self.state_path, os.getpid())
        with open(tmp, "w") as f:
            json.dump([[t, acct] for t, acct in stamps], f)
        os.replace(tmp, self.state_path)

    def _try(self, account):
        """(need, why, distinct): need is 0 when the login may start now and is counted, else the wait."""
        now = self.clock()
        guard = hour_start_wait(now)
        if guard > 0:
            return guard, "hour", 0
        key = account_key(account)
        if self.state_path:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            with open(self.state_path + ".lock", "w") as lockf:
                fcntl.flock(lockf, fcntl.LOCK_EX)
                try:
                    stamps = [s for s in self._read_state() if s[0] > self._horizon(now)]
                    need, distinct = self._need(stamps, now, key)
                    if need <= 0:
                        stamps.append((now, key))
                        self._write_state(stamps)
                    self.stamps = stamps
                    return need, "window", distinct
                finally:
                    fcntl.flock(lockf, fcntl.LOCK_UN)
        stamps = [s for s in self.stamps if s[0] > self._horizon(now)]
        need, distinct = self._need(stamps, now, key)
        if need <= 0:
            stamps.append((now, key))
        self.stamps = stamps
        return need, "window", distinct

    def _need(self, stamps, now, key):
        """(seconds until this login fits, distinct accounts in the window); 0 when it fits now."""
        latest = {}                                  # account -> its newest login in the window
        for t, acct in stamps:
            if t > latest.get(acct, float("-inf")):
                latest[acct] = t
        distinct = len(latest)
        if key in latest or distinct < self.limit:   # a repeat adds no account to realmd's count
            return 0.0, distinct
        # the login that must age out for this one to fit: the (distinct - limit + 1)th oldest account
        ready = sorted(latest.values())[distinct - self.limit] + self.window + self.margin
        return max(0.01, ready - now), distinct

    def acquire(self, what="", account=None):
        """Block until this login may start, count it, return the seconds waited.

        The in-process lock is held while waiting, so parallel slots queue in order.
        """
        with self._lock:
            total = 0.0
            while True:
                need, why, distinct = self._try(account)
                if need <= 0:
                    # logins that were not held back used to leave no trace, so a pacer shared by
                    # parallel workers (or by other processes through the state file) was invisible
                    self.debug("login pacer: %s let through after %.0f s wait (%d distinct accounts in the "
                               "last %.0f s before it, limit %d, %s, pid %d, thread %s)"
                               % (what or "login", total, distinct, self.window, self.limit,
                                  "shared state file" if self.state_path else "this process only",
                                  os.getpid(), threading.current_thread().name))
                    return total
                if why == "hour":
                    self.log("login pacer: waiting %.0f s for the hour-start realmd window" % need)
                else:
                    self.log("login pacing: %d distinct accounts in the last %.0f s (limit %d); "
                             "waiting %.0f s before %s"
                             % (distinct, self.window, self.limit, need, what or "the next login"))
                self.waits.append((what, need))
                self.sleep(need)
                total += need
