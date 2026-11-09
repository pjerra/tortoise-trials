"""Orchestration: leases, slot setup, login, one thread per slot, signals.

`trials.py run` calls `main(args)`; `trials.py cleanup` calls `cleanup(args)`.
args carries server, trials (resolved paths), label, repeats, parallel, out,
bind, dry_run (and binary, the walk binary `slot setup` installs).
"""

if __package__ in (None, ""):
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
    __package__ = "trl.live"

import glob
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .. import gear as gear_mod
from .. import scenario
from . import collect, ladder, lease, pacing, situations, teardown
from .agent import Agent, RpcError, norm_guid
from .gm_guard import GmAborted, GmContext, GmGuard, GmRefused, GmServerError
from .ledger import Ledger
from .setup import REASON_NOT_VISIBLE, check_player_visible, run_setup

TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SLUG = "tortoise-trials"
DEFAULT_OUT = os.environ.get("TRIALS_OUT") or os.path.join(TOOL_ROOT, "runs")
MAX_PARALLEL = 3
MAX_SLOT_TRIES = 3         # slots tried when an account is full (the first one counts)

STOP = threading.Event()


@dataclass
class Args:
    server: str
    trials: list = field(default_factory=list)
    label: str = ""
    repeats: int = 0
    parallel: int = 1
    out: str = ""
    bind: bool = False
    dry_run: bool = False
    binary: str = None
    slot: list = field(default_factory=list)
    mobs_file: str = None


class SetupInvalid(Exception):
    pass


class LoginRefused(Exception):
    pass


class AccountFull(LoginRefused):
    """The create was refused because the account has no free character slot."""


class RepeatInvalid(Exception):
    """A repeat ended INVALID for a run-level reason (lease lost, disconnect)."""


class Disconnected(RepeatInvalid):
    pass


class LeaseLost(RepeatInvalid):
    pass


# -- helpers ---------------------------------------------------------------

def stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def iso_now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def resolve_binary(args):
    for candidate in (getattr(args, "binary", None), os.environ.get("TRIALS_BINARY")):
        if candidate:
            return os.path.abspath(os.path.expanduser(candidate))
    import shutil
    found = shutil.which("turtle-agent")
    return os.path.abspath(found) if found else ""


def binary_commit(binary):
    try:
        res = subprocess.run(["strings", "-a", binary], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"vcs\.revision=([0-9a-f]{7,40})", res.stdout or "")
    return m.group(1) if m else ""


def binary_version(binary):
    try:
        res = subprocess.run([binary, "version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (res.stdout or "").strip().splitlines()[0] if res.stdout else ""


def load_trial(path):
    # `path` is a trial file, or `path@variant` for one of its variants (the merged trial, id `<id>@<variant>`)
    if not os.path.exists(path) and scenario.VARIANT_SEP in path:
        return scenario.load_selected(path)
    with open(path) as f:
        return json.load(f)


def load_trials(paths):
    out = []
    for path in paths or []:
        try:
            out.append(load_trial(path))
        except (OSError, ValueError) as exc:
            print("trial %s: %s" % (path, exc), file=sys.stderr)
    return out


def data_json(name):
    try:
        with open(os.path.join(TOOL_ROOT, "data", name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def characters():
    return data_json("characters.json")


def next_name(name, attempt):
    if attempt <= 0:
        return name
    letters = "abcdefghijklmnopqrstuvwxyz"
    stem = name[:-1]
    for letter in letters[attempt:]:
        candidate = stem + letter
        if len(candidate) <= 12:
            return candidate
    return name[:11] + letters[attempt % len(letters)]


# -- one slot --------------------------------------------------------------

class SlotSession:
    """One slot dir, one login at a time; switches the player character per class."""

    def __init__(self, row, slot_dir, binary, holder_name, run, log):
        self.row = row
        self.slot_id = str((row.get("slot") or {}).get("id"))
        self.slot_dir = slot_dir
        self.binary = binary
        self.holder_name = holder_name
        self.run = run
        self.log = log
        self.agent = Agent(slot_dir, log=log)
        self.port = int((row.get("slot") or {}).get("port") or 0)
        self.cfg = None
        self.pidfile = None
        self.proc = None
        self.character = ""
        self.account = ""
        self.team = ""
        self.klass = ""
        self.name_attempts = {}
        self.out = None
        self.last_bot = ""
        self.last_extras = []
        self.ledger = None
        self.lease_id = ""
        self.keeper = None
        self.abort_reason = ""
        self.teardown_done = False
        self.tried_slots = [self.slot_id]      # slots whose account turned out full (carried over a swap)
        self.ladder_step = 0                   # the difficulty step of the rep about to run
        self.last_rep = None                   # what run_repeat found out about the rep that just ran

    def rebind(self, row, slot_dir, holder_name):
        """Point this session at another slot after an account-full swap (the old one is torn down)."""
        self.row = row
        self.slot_id = str((row.get("slot") or {}).get("id"))
        self.slot_dir = slot_dir
        self.holder_name = holder_name
        self.agent = Agent(slot_dir, log=self.log)
        self.port = int((row.get("slot") or {}).get("port") or 0)
        self.cfg = None
        self.pidfile = None
        self.proc = None
        self.character = ""
        self.account = ""
        self.team = ""
        self.klass = ""
        self.name_attempts = {}
        self.out = None
        self.last_bot = ""
        self.last_extras = []
        self.ledger = None
        self.lease_id = ""
        self.keeper = None
        self.abort_reason = ""
        self.teardown_done = False

    # -- slot setup --------------------------------------------------------

    def setup_slot(self):
        argv = [self.binary, "slot", "setup", self.run.server, self.slot_id,
                "--holder", self.holder_name, "--dir", self.slot_dir, "--binary", self.binary]
        res = lease.run_cmd(argv, timeout=180)
        line = ""
        for raw in res["out"].splitlines():
            if raw.startswith("start: "):
                line = raw[len("start: "):]
        if res["rc"] or not line:
            raise SetupInvalid("slot setup %s/%s failed: %s" % (self.run.server, self.slot_id, res["out"].strip()[-300:]))
        tokens = shlex.split(line.replace("nohup ", "").split(" > ")[0])
        for i, tok in enumerate(tokens):
            if tok == "--config" and i + 1 < len(tokens):
                self.cfg = tokens[i + 1]
            if tok == "--pid-file" and i + 1 < len(tokens):
                self.pidfile = tokens[i + 1]
        if not self.cfg:
            raise SetupInvalid("slot setup printed no --config in its start line")
        return line

    # -- login -------------------------------------------------------------

    def wait_in_world(self, timeout=90.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.agent.rpc("status")
            if isinstance(st, dict) and st.get("state") == "in_world":
                return st
            if self.proc is not None and self.proc.poll() is not None:
                return None
            time.sleep(1.0)
        return None

    def log_tail(self, n=12):
        if not self.out or not os.path.isfile(self.out):
            return ""
        with open(self.out, errors="replace") as f:
            return "\n".join(f.read().splitlines()[-n:])

    def login(self, name, create_spec=None):
        self.stop_agent()
        # realmd refuses the 15th account that logs in from this IP within its window (AuthSocket.cpp:439-457).
        # trl/live/pacing.py keeps distinct accounts under 12 per 180 s and holds logins back at the top of the
        # UTC hour, where realmd's count is wrong. The account is passed, so another login by an account that
        # is already in the window does not count again.
        pace = getattr(self.run, "pace", None)
        if pace is not None:
            pace("login %s" % name, account=self.account)
        else:
            pacer = getattr(self.run, "pacer", None)
            if pacer is not None:
                pacer.acquire("login %s" % name, account=self.account)
        slot_binary = os.path.join(self.slot_dir, "turtle-agent")
        argv = [slot_binary if os.path.isfile(slot_binary) else self.binary, "login",
                "--config", self.cfg, "--pid-file", self.pidfile or os.path.join(self.slot_dir, "agent.pid")]
        if self.port:
            argv += ["--rpc", "127.0.0.1:%d" % self.port]
        if create_spec:
            argv += ["--create", create_spec]
        argv.append(name)
        self.out = os.path.join(self.slot_dir, "agent-%s.out" % name)
        handle = open(self.out, "ab")
        self.log("login %s (%s)" % (name, create_spec or "existing"))
        self.proc = subprocess.Popen(argv, cwd=self.slot_dir, stdout=handle, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        handle.close()
        st = self.wait_in_world()
        if not st:
            tail = self.log_tail()
            # turtle-agent: "SMSG_CHAR_CREATE: create failed: too many characters on this account|realm"
            if re.search(r"too many characters|character limit|character slots", tail, re.I):
                raise AccountFull("no free character slot: %s" % tail.splitlines()[-1])
            if re.search(r"name (?:already )?in use|reserved|invalid name", tail, re.I):
                raise LoginRefused("name in use: %s" % tail.splitlines()[-1])
            raise LoginRefused("login as %s failed: %s" % (name, tail.splitlines()[-1] if tail else "no output"))
        self.character = name
        self.team = (st.get("team") or "").lower()
        self.agent.cursor = 0
        note = {"kind": "session", "server": self.run.server, "slot": self.slot_id,
                "account": self.account, "character": self.character, "team": self.team}
        if self.ledger is not None:
            self.ledger.add(dict(note))
        run_ledger = getattr(self.run, "ledger", None)
        if run_ledger is not None and run_ledger is not self.ledger:
            run_ledger.add(note)
        return st

    def ensure_character(self, klass):
        if not self.cfg:
            raise SetupInvalid("slot not set up")
        if not self.character:
            default = (self.row.get("slot") or {}).get("character")
            if not default:
                raise SetupInvalid("slot %s has no character" % self.slot_id)
            self.login(default)
        if self.klass == klass:
            return
        table = characters().get(self.team or "alliance") or {}
        entry = table.get(klass)
        if not entry:
            raise SetupInvalid("no character for %s/%s in data/characters.json" % (self.team, klass))
        attempt = self.name_attempts.get(klass, 0)
        while True:
            name = next_name(entry["name"], attempt)
            try:
                self.login(name, create_spec="%d,%d" % (int(entry["race"]), int(entry["class"])))
                break
            except LoginRefused as exc:
                if "name in use" in str(exc) and attempt < 20:
                    attempt += 1
                    continue
                self.name_attempts[klass] = attempt
                raise
        self.name_attempts[klass] = attempt
        self.klass = klass
        self.run.character_note(self.slot_id, self.team, klass, self.character)

    def version(self):
        since = self.agent.tbm("version")
        line, _rows = self.agent.wait_reply(since, r"TBM:VERSION\||TortoiseBots \S+", timeout=10)
        return line or ""

    def abort(self, reason):
        """End this slot's repeat/trial now (a lost lease or a dropped session)."""
        if not self.abort_reason:
            self.abort_reason = str(reason or "aborted")

    def relogin(self):
        """Fresh login on the same character after a disconnect (best effort)."""
        if not self.cfg or not self.character:
            return False
        try:
            self.login(self.character)
            self.abort_reason = ""
            return True
        except (LoginRefused, SetupInvalid, RpcError):
            return False

    def stop_agent(self):
        if not self.proc:
            return
        try:
            self.agent.rpc("logout", {}, timeout="20s")
        except Exception:  # noqa: BLE001 - the agent may already be gone
            pass
        for _ in range(10):
            if self.proc.poll() is not None:
                self.proc = None
                return
            time.sleep(1.0)
        try:
            self.proc.terminate()
        except OSError:
            pass
        self.proc = None


# -- the run ---------------------------------------------------------------

class Run:
    def __init__(self, args, binary, run_dir, slots):
        self.args = args
        self.binary = binary
        self.run_dir = run_dir
        self.slots = slots
        self.ledger = Ledger(os.path.join(run_dir, "ledger.jsonl"))
        self.run_stamp = stamp()
        self.started = iso_now()
        self.warnings = []
        self.unverified = []          # summons not proved dead or gone at the run end
        self.characters_used = {}
        self.tb_version = ""
        self.ta_commit = binary_commit(binary) if binary else ""
        self.ta_version = binary_version(binary) if binary else ""
        self.config = {}
        self.refused = False
        self.server_error = False
        self.errors = []
        self.sessions = []
        self.pacer = pacing.LoginPacer(log=self.log, debug=self.debug)
        self.rep_log = []              # one row per rep: slot, step, valid (RUN.json "reps")
        self.slot_history = []         # every slot a session held, in order (RUN.json "slots")
        self.slot_swaps = []           # account-full moves
        self.arena_claims = {}         # arena id -> slot id (parallel slots never share an arena)
        self._lock = threading.Lock()
        self._swap_lock = threading.Lock()

    @property
    def server(self):
        return self.args.server

    def pace(self, what, account=None):
        """Wait until one more realmd login fits (the pacer logs the wait and the ledger keeps it)."""
        pacer = getattr(self, "pacer", None)
        if pacer is None:
            return 0.0
        waited = pacer.acquire(what, account=account)
        if waited > 0:
            self.ledger.add({"kind": "login_wait", "seconds": round(waited, 1), "before": what})
        return waited

    def character_note(self, slot, team, klass, name):
        self.characters_used.setdefault(slot, []).append({"team": team, "class": klass, "name": name})

    def log(self, line):
        print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), line), flush=True)

    def debug(self, line):
        """Debug-level lines are printed only when TRIALS_DEBUG is set (any non-empty value except 0)."""
        if os.environ.get("TRIALS_DEBUG", "0") not in ("", "0"):
            self.log("debug: " + line)

    # -- setup of the slots ------------------------------------------------

    def prepare_slots(self, trials):
        for index, row in enumerate(self.slots):
            holder_name = lease.holder(self.run_stamp, index)
            session = self.open_session(row, holder_name)
            if session is None:
                self.teardown_sessions()
                return False
            self.sessions.append(session)
            with open(os.path.join(self.run_dir, "summary.txt"), "a") as f:
                f.write("server %s slot %s\n" % (self.args.server, session.slot_id))
        return True

    def open_session(self, row, holder_name, session=None):
        """Lease one slot row, start its keeper and run slot setup; None (lease released) on failure.

        With `session` the existing session object is pointed at the new slot (an account-full swap).
        """
        sid = str((row.get("slot") or {}).get("id"))
        got = lease.acquire(self.binary, self.args.server, sid, holder_name, ttl=lease.TTL)
        if not got["ok"] or not got["lease_id"]:
            self.log("lease acquire %s/%s failed: %s" % (self.args.server, sid, got["raw"].strip()[-200:]))
            return None
        slot_dir = os.path.join(self.run_dir, "slots", "%s-%s" % (self.args.server, sid))
        # one ledger per slot: a kill only ever touches this slot's own summons
        ledger_path = os.path.join(self.run_dir, "ledgers", "%s-%s.jsonl" % (self.args.server, sid))
        self.ledger.add({"kind": "lease", "server": self.args.server, "slot": sid,
                         "holder": holder_name, "lease_id": got["lease_id"], "dir": slot_dir,
                         "account": got.get("account") or "",
                         "character": got.get("character") or "",
                         "port": got.get("port") or 0, "ledger": ledger_path})
        if session is None:
            session = SlotSession(row, slot_dir, self.binary, holder_name, self, self.log)
        else:
            session.rebind(row, slot_dir, holder_name)
        session.lease_id = got["lease_id"]
        session.account = got.get("account") or ""
        session.ledger = Ledger(ledger_path)
        # the keeper starts right after acquire: a long setup must not outlive the TTL
        keeper = lease.LeaseKeeper(self.binary, self.args.server, sid, holder_name, got["lease_id"],
                                   self.log, ttl=lease.TTL,
                                   on_lost=lambda reason, s=session: s.abort(reason))
        keeper.start()
        session.keeper = keeper
        try:
            session.setup_slot()
        except Exception as exc:  # noqa: BLE001 - any failure releases this lease
            self.log("slot setup %s/%s failed: %s" % (self.args.server, sid, exc))
            lease.release(self.binary, self.args.server, sid, holder_name, got["lease_id"])
            keeper.stop()
            return None
        self.slot_history.append({"slot": sid, "account": session.account, "holder": holder_name})
        return session

    # -- a full account: move to the next free slot ------------------------

    def ensure_character(self, session, klass):
        """session.ensure_character, and on an account-full create move to another slot (max 3 tried)."""
        while True:
            try:
                return session.ensure_character(klass)
            except AccountFull as exc:
                self.swap_slot(session, str(exc))

    def swap_slot(self, session, reason):
        """Release this session's lease and take the next free slot (never 3, never one another run holds).

        Raises SetupInvalid("no free character slot on slots 1,2,4") after MAX_SLOT_TRIES slots or when
        no further slot is free. No character is deleted.
        """
        with self._swap_lock:
            tried = session.tried_slots
            if session.slot_id not in tried:
                tried.append(session.slot_id)
            fail = "no free character slot on slots %s" % ",".join(tried)
            if len(tried) >= MAX_SLOT_TRIES:
                raise SetupInvalid(fail)
            held = {s.slot_id for s in self.sessions if s is not session}
            free = lease.pick_free(self.binary, self.args.server, 20)
            # pick_free already drops slot 3; the check is repeated here because slot 3 is the default reserved
            candidates = [r for r in free
                          if str((r.get("slot") or {}).get("id")) not in tried
                          and str((r.get("slot") or {}).get("id")) not in held
                          and str((r.get("slot") or {}).get("id")) not in lease.NEVER_SLOTS]
            if not candidates:
                raise SetupInvalid(fail)
            old_id, old_account = session.slot_id, session.account
            self.log("slot %s: account %s is full (%s); looking for another slot"
                     % (old_id, old_account or "?", reason[:120]))
            index = self.sessions.index(session) if session in self.sessions else 0
            self.stop_session(session)            # logout, .gm off and the lease release of the old slot
            session.cfg = None                    # this session holds no slot until one is bound
            for row in candidates:
                sid = str((row.get("slot") or {}).get("id"))
                holder_name = "%s-s%d" % (lease.holder(self.run_stamp, index), len(tried))
                if self.open_session(row, holder_name, session=session) is None:
                    self.log("slot %s: could not be taken, trying the next one" % sid)
                    continue
                tried.append(sid)
                self.slot_swaps.append({"from": old_id, "to": sid, "from_account": old_account,
                                        "to_account": session.account, "reason": reason[:200]})
                self.ledger.add({"kind": "slot_swap", "from": old_id, "to": sid,
                                 "account": session.account})
                self.log("slot %s -> %s (account %s)" % (old_id, sid, session.account or "?"))
                return
            raise SetupInvalid(fail)

    # -- parallel slots: arenas and summons --------------------------------

    def claim_arena(self, session, arena):
        """Hold an arena for this slot; False when another slot holds it."""
        with self._lock:
            owner = self.arena_claims.get(arena.get("id"))
            if owner is not None and owner != session.slot_id:
                return False
            self.arena_claims[arena.get("id")] = session.slot_id
            return True

    def free_arena(self, session, arena):
        with self._lock:
            if self.arena_claims.get(arena.get("id")) == session.slot_id:
                del self.arena_claims[arena.get("id")]

    def free_arenas(self, session):
        with self._lock:
            for key in [k for k, v in self.arena_claims.items() if v == session.slot_id]:
                del self.arena_claims[key]

    def foreign_summons(self, session):
        """Every guid the OTHER slots summoned (their ledgers): never claimed or touched by this slot."""
        out = set()
        for other in list(self.sessions):
            if other is session:
                continue
            ledger = getattr(other, "ledger", None)
            if ledger is not None:
                out.update(g for g in ledger.summon_guids() if g)
        return out

    def teardown_sessions(self):
        """Full cleanup for every session opened so far (a later slot failed)."""
        for session in list(self.sessions):
            self.stop_session(session)

    def execute(self, trials):
        self.run_stamp = stamp()
        os.makedirs(self.run_dir, exist_ok=True)
        with open(os.path.join(self.run_dir, "START"), "w") as f:
            f.write(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")
        open(os.path.join(self.run_dir, "summary.txt"), "w").close()
        if not self.prepare_slots(trials):
            return 4
        threads = []
        buckets = [[] for _ in self.sessions]
        for i, trial in enumerate(trials):
            buckets[i % len(buckets)].append(trial)
        errors = self.errors
        for session, bucket in zip(self.sessions, buckets):
            th = threading.Thread(target=self.worker, args=(session, bucket, errors), daemon=True)
            th.start()
            threads.append(th)
        for th in threads:
            th.join()
        self.write_run_json(trials)
        if errors:
            self.log("errors: %s" % errors)
        return self.exit_code()

    def note_teardown(self, rows):
        """A teardown row that could not prove the summons dead is a server/RPC problem."""
        for row in rows or []:
            if row.get("id") == "mobs_dead" and not row.get("ok"):
                self.server_error = True

    def exit_code(self):
        """5 = server/RPC error or a leftover summon, 4 = safety refusal, 1 = other problems."""
        if self.server_error:
            return 5
        if self.refused:
            return 4
        return 1 if self.errors else 0

    def record_invalid(self, session, trial, rep, reason):
        """One INVALID row in the run ledger and in the slot's own ledger."""
        row = {"kind": "invalid", "trial": (trial or {}).get("id"), "rep": int(rep or 0),
               "reason": str(reason)}
        self.ledger.add(row)
        if getattr(session, "ledger", None) is not None:
            session.ledger.add(dict(row))

    # -- error taxonomy (M4) and aborts (H3) -------------------------------

    def note_exception(self, exc):
        """A safety refusal is exit 4, a server/RPC reject is exit 5; count them everywhere."""
        if isinstance(exc, GmRefused):
            self.refused = True
        elif isinstance(exc, GmServerError):
            self.server_error = True

    def drain_guard(self, guard):
        if getattr(guard, "refused", False):
            self.refused = True
        if getattr(guard, "server_error", False):
            self.server_error = True

    def aborted_exception(self, session):
        """The exception a session's abort_reason means, or None when nothing was lost."""
        reason = str(getattr(session, "abort_reason", "") or "")
        if not reason:
            return None
        if "lease" in reason.lower():
            return LeaseLost("lease lost")
        return Disconnected(reason if "disconnect" in reason.lower() else "disconnected: %s" % reason)

    def abort_text(self, exc):
        return "lease lost" if isinstance(exc, LeaseLost) else str(exc)

    def reacquire(self, session):
        """A NEW lease after a loss; None when the slot cannot be taken back."""
        index = self.sessions.index(session) if session in self.sessions else 0
        holder_name = "%s-r" % lease.holder(self.run_stamp, index)
        got = lease.acquire(self.binary, self.args.server, session.slot_id, holder_name, ttl=lease.TTL)
        if not got.get("ok") or not got.get("lease_id"):
            self.log("lease re-acquire %s/%s failed: %s" % (self.args.server, session.slot_id,
                                                            str(got.get("raw", "")).strip()[-160:]))
            return None
        return {"holder": holder_name, "lease_id": got["lease_id"]}

    def record_cleanup_leftover(self, session, trial, check, reason):
        payload = {"kind": "leftover", "check": check, "evidence": reason,
                   "trial": (trial or {}).get("id")}
        self.ledger.add(payload)
        if getattr(session, "ledger", None) is not None:
            session.ledger.add(dict(payload))
        self.errors.append("%s %s: %s" % (payload["trial"], check, reason))

    def note_cleanup_rows(self, session, trial, rows):
        """Every failed cleanup row is a leftover in the ledger and a non-zero exit."""
        for row in rows or []:
            if row.get("ok"):
                continue
            self.record_cleanup_leftover(session, trial, row.get("id"), row.get("evidence") or "failed")

    @staticmethod
    def unverified_summons(session, ledger):
        """Ledger summons no cleanup has proved dead or gone yet (guids as the ledger wrote them)."""
        verified = getattr(session, "verified_summons", None) or set()
        return [g for g in ledger.summon_guids() if g and norm_guid(g) not in verified]

    @staticmethod
    def note_verified(session, rows):
        """Remember the summons a cleanup's mobs_dead row proved dead or gone: never re-checked."""
        verified = getattr(session, "verified_summons", None)
        if verified is None:
            verified = session.verified_summons = set()
        for row in rows or []:
            if row.get("id") == "mobs_dead":
                verified.update(norm_guid(g) for g in row.get("verified") or [])

    def final_summon_check(self, session):
        """Run end: re-check every summon still unverified once; a leftover is a warning and exit 5.

        Not-in-view stays UNKNOWN here (the player is not necessarily at the mob's arena); `trials
        cleanup <run-dir>` goes back to the arena and settles it.
        """
        agent = getattr(session, "agent", None)
        ledger = getattr(session, "ledger", None) or self.ledger
        pending = self.unverified_summons(session, ledger)
        if not pending:
            return
        if agent is not None and not session.abort_reason:
            try:
                # unknown_ok: one summon that is not in view must not make every other one unverified
                states = situations.summon_states(agent, ledger, only=pending, unknown_ok=True)
            except Exception as exc:  # noqa: BLE001 - the unit list itself failed: all stay unverified
                self.log("final summon check slot %s: %s" % (session.slot_id, exc))
                states = {}
            self.note_verified(session, [{"id": "mobs_dead", "verified": [
                g for g, state in states.items() if state in ("dead", "gone")]}])
        # an aborted session (lost lease, disconnect) is not asked: its leftovers are named, not queried
        left = self.unverified_summons(session, ledger)
        if left:
            self.unverified.extend(norm_guid(g) for g in left)
            self.warnings.append({"summons_unverified": [norm_guid(g) for g in left],
                                  "slot": session.slot_id,
                                  "detail": "not proved dead or gone at the run end: `trials cleanup <run-dir>`"})
            ledger.add({"kind": "mob_leftover", "guids": [norm_guid(g) for g in left],
                        "reason": "unverified at run end"})
            self.log("slot %s: %d summon(s) not proved dead or gone at the run end: %s"
                     % (session.slot_id, len(left), ", ".join(norm_guid(g) for g in left)))
            self.server_error = True

    def repeat_cleanup_now(self, session, bot_name):
        """M8: after a relogin, kill the slot ledger's summons with a fresh guard."""
        agent = session.agent
        ledger = getattr(session, "ledger", None) or self.ledger
        st = agent.rpc("status", {}, timeout="10s")
        player_guid = norm_guid(st.get("guid") or "") if isinstance(st, dict) else ""
        guard = GmGuard(agent, ledger, GmContext(player_guid=player_guid),
                        abort=lambda s=session: s.abort_reason)
        try:
            extras = [n for n in (getattr(session, "last_extras", None) or []) if n]
            kw = {"only": self.unverified_summons(session, ledger)}   # the player may be anywhere: UNKNOWN stays
            if extras:
                kw["extra_names"] = extras
            rows = teardown.repeat_cleanup(agent, guard, ledger, bot_name, player_guid, **kw)
            self.note_verified(session, rows)
            return rows
        finally:
            self.drain_guard(guard)

    def note_arena(self, session, trial, rep, arena):
        row = {"kind": "arena", "id": arena.get("id"), "server": self.args.server,
               "slot": session.slot_id, "account": session.account,
               "character": session.character, "map": int(arena.get("map", 0)),
               "x": arena.get("x"), "y": arena.get("y"), "z": arena.get("z"),
               "trial": (trial or {}).get("id"), "rep": int(rep or 0)}
        self.ledger.add(row)
        if getattr(session, "ledger", None) is not None:
            session.ledger.add(dict(row))

    def trial_cleanup_after(self, session, trial, bot_name, lease_lost):
        """M7: trial cleanup reaches the ledger and the exit code; a lost lease first."""
        fresh = self.reacquire(session) if lease_lost else None
        if lease_lost and not fresh:
            # the slot may already be someone else's: no GM line on it; record it
            self.log("trial cleanup %s: lease not re-acquired, leftovers recorded" % trial["id"])
            checks = ["forced_role_clear"] + (["hire_removed"] if bot_name else [])
            checks += ["hire_removed_extra%d" % i
                       for i, name in enumerate(getattr(session, "last_extras", None) or [], start=1) if name]
            for check in checks:
                self.record_cleanup_leftover(session, trial, check,
                                             "lease lost: not re-acquired")
            return
        try:
            extras = list(getattr(session, "last_extras", None) or [])
            if any(extras):     # index-aligned: removal keeps each hire's original extra index
                rows = teardown.trial_cleanup(session.agent, session.ledger or self.ledger, bot_name,
                                              extras=extras)
            else:
                rows = teardown.trial_cleanup(session.agent, session.ledger or self.ledger, bot_name)
            session.last_extras = []
            self.note_cleanup_rows(session, trial, rows)
        except Exception as exc:  # noqa: BLE001
            self.note_exception(exc)
            self.log("trial cleanup %s: %s" % (trial["id"], exc))
        finally:
            if fresh:
                lease.release(self.binary, self.args.server, session.slot_id,
                              fresh["holder"], fresh["lease_id"])

    def worker(self, session, bucket, errors):
        try:
            if not bucket:
                return
            try:
                self.ensure_character(session, bucket[0]["player"]["class"])
            except Exception as exc:  # noqa: BLE001 - a full account is an INVALID, not a crash
                self.log("character for %s: %s" % (bucket[0]["id"], exc))
                self.note_exception(exc)
                self.record_invalid(session, bucket[0], 0, "error: %s" % type(exc).__name__)
                errors.append("%s: %s" % (bucket[0]["id"], exc))
                return
            if not self.tb_version:
                self.tb_version = session.version()
            for trial in bucket:
                if STOP.is_set():
                    break
                aborted = self.aborted_exception(session)
                if aborted:
                    # H3: the lease/session went away outside the fight loop
                    self.log("%s: %s" % (trial["id"], aborted))
                    self.record_invalid(session, trial, 0, self.abort_text(aborted))
                    errors.append("%s: %s" % (trial["id"], aborted))
                    self.server_error = True     # the exit code from M4, not a safety refusal
                    if isinstance(aborted, LeaseLost):
                        self.trial_cleanup_after(session, trial, session.last_bot, True)
                    else:
                        session.relogin()
                    break
                try:
                    self.ensure_character(session, trial["player"]["class"])
                except Exception as exc:  # noqa: BLE001
                    self.log("character for %s: %s" % (trial["id"], exc))
                    self.note_exception(exc)
                    self.record_invalid(session, trial, 0, "error: %s" % type(exc).__name__)
                    errors.append("%s: %s" % (trial["id"], exc))
                    continue
                repeat_count = int(self.args.repeats or trial.get("repeats") or 1)
                # keep the hire name across repeats/trials: `.bot remove` must always go out,
                # and a stale name is harmless (hire_bot checks the party list)
                bot_name = session.last_bot
                windows = []
                lease_lost = [False]
                steps = ladder.Ladder(trial, repeat_count)
                rep = 0
                try:
                    while True:
                        rep += 1
                        # the planned reps, then (adaptive) up to 2 extra while too few were valid
                        if rep > repeat_count and not steps.want_extra():
                            break
                        if STOP.is_set():
                            break
                        aborted = self.aborted_exception(session)
                        if aborted:
                            self.log("%s rep%d: %s" % (trial["id"], rep, aborted))
                            self.record_invalid(session, trial, rep, self.abort_text(aborted))
                            errors.append("%s rep%d: %s" % (trial["id"], rep, aborted))
                            lease_lost[0] = isinstance(aborted, LeaseLost)
                            self.server_error = True
                            break
                        session.ladder_step = steps.step
                        session.last_rep = None
                        try:
                            rep_bot, window = self.run_repeat(session, trial, rep)
                        except Exception as exc:  # noqa: BLE001 - log it, clean up, INVALID
                            if isinstance(exc, GmAborted):
                                # the guard refused after the session was lost: no GM line went out
                                exc = self.aborted_exception(session) or LeaseLost("lease lost")
                            self.log("%s rep%d: %s" % (trial["id"], rep, exc))
                            self.note_exception(exc)
                            bot_name = session.last_bot or bot_name
                            reason = "error: %s" % type(exc).__name__
                            if isinstance(exc, (Disconnected, LeaseLost)) and str(exc):
                                reason = self.abort_text(exc)
                            self.record_invalid(session, trial, rep, reason)
                            errors.append("%s rep%d: %s" % (trial["id"], rep, exc))
                            if isinstance(exc, LeaseLost):
                                lease_lost[0] = True
                                self.server_error = True
                                break
                            if isinstance(exc, Disconnected):
                                self.server_error = True
                                # M8: after a successful relogin, kill this slot's summons again
                                if session.relogin():
                                    try:
                                        rows = self.repeat_cleanup_now(session, bot_name)
                                        self.note_cleanup_rows(session, trial, rows)
                                    except Exception as rexc:  # noqa: BLE001
                                        self.note_exception(rexc)
                                        self.log("repeat cleanup after relogin %s: %s"
                                                 % (trial["id"], rexc))
                                break
                            if isinstance(exc, RpcError):
                                self.server_error = True
                                break
                            if session.abort_reason:
                                aborted = self.aborted_exception(session)
                                if aborted:
                                    self.record_invalid(session, trial, rep, self.abort_text(aborted))
                                    errors.append("%s rep%d: %s" % (trial["id"], rep, aborted))
                                    lease_lost[0] = isinstance(aborted, LeaseLost)
                                    self.server_error = True
                                break
                            continue
                        if rep_bot:
                            bot_name = rep_bot
                        elif session.last_bot:
                            bot_name = session.last_bot
                        if window:
                            windows.append(window)
                        info = session.last_rep or {}
                        steps.record(bool(info.get("valid", bool(window))), bool(info.get("capped")))
                        if info.get("capped") and steps.on and steps.step > info.get("step", 0):
                            self.log("%s rep%d ended on the rescue cap: the next rep runs at difficulty step %d"
                                     % (trial["id"], rep, steps.step))
                finally:
                    try:
                        self.pull_evpull(trial, bot_name, windows)
                    except Exception as exc:  # noqa: BLE001 - cleanup must still run
                        self.log("evpull %s: %s" % (trial["id"], exc))
                    self.trial_cleanup_after(session, trial, bot_name, lease_lost[0])
            aborted = self.aborted_exception(session)
            if aborted:
                # a lease lost after the last repeat still ends the run non-zero
                errors.append("session: %s" % aborted)
                self.server_error = True
        finally:
            self.stop_session(session)

    def stop_session(self, session):
        if getattr(session, "teardown_done", False):
            return
        session.teardown_done = True
        try:
            # `turtle-agent teardown` logs in again (to say `.gm off`): it counts for realmd too
            try:
                self.final_summon_check(session)
            except Exception as exc:  # noqa: BLE001 - the teardown must still run
                self.log("final summon check slot %s: %s" % (session.slot_id, exc))
            self.pace("slot teardown %s" % session.slot_id, account=getattr(session, "account", ""))
            out = teardown.slot_teardown(session.slot_dir, self.binary)
            if not out.get("ok"):
                # the graceful teardown (logout, .gm off, lease) failed: end the agent,
                # then release the lease anyway so the slot never stays blocked
                session.stop_agent()
                released = lease.release(self.binary, self.args.server, session.slot_id,
                                         session.holder_name, getattr(session, "lease_id", ""))
                out["released"] = released
                self.log("lease release %s/%s after a failed teardown: %s"
                         % (self.args.server, session.slot_id, "ok" if released else "FAILED"))
            self.ledger.add({"kind": "slot_teardown", "dir": session.slot_dir,
                             "rc": out.get("rc"), "detail": out.get("detail"), "ok": out.get("ok"),
                             "failed_steps": out.get("failed_steps") or []})
            self.log("teardown %s: rc=%s %s" % (session.slot_dir, out.get("rc"), out.get("detail")))
            if out.get("teardown_failed"):
                self.warnings.append({"teardown_failed": session.slot_dir, "detail": out.get("detail")})
        finally:
            # the keeper outlives setup and runs until the slot teardown and the release
            if getattr(session, "keeper", None):
                session.keeper.stop()

    # -- one repeat --------------------------------------------------------

    def run_repeat(self, session, trial, rep_index):
        agent = session.agent
        ledger = getattr(session, "ledger", None) or self.ledger
        # summons an earlier repeat's cleanup already proved dead or gone are not checked again: a
        # despawned corpse answers "not in view", which used to make the whole check UNKNOWN
        if getattr(session, "verified_summons", None) is None:
            session.verified_summons = set()
        before_rep = {norm_guid(g) for g in ledger.summon_guids() if g}      # earlier repeats' summons
        rep_dir = os.path.join(self.run_dir, trial["id"], "rep%d" % rep_index)
        os.makedirs(rep_dir, exist_ok=True)
        marks = collect.Marks(os.path.join(rep_dir, "marks.jsonl"), self.log)
        marks.start()
        st = agent.status()
        state = str(st.get("state") or "")
        if state != "in_world":
            raise Disconnected("disconnected: agent state %s" % (state or "unknown"))
        player_guid = norm_guid(st.get("guid"))
        sit = trial.get("situation") or {}
        sit_mobs = sit.get("mobs") or {}
        # the ladder step this rep runs at (0 = as written); only the Situation sees the easier trial
        step = int(getattr(session, "ladder_step", 0) or 0)
        eff_trial = ladder.apply(trial, step)
        difficulty = eff_trial["_difficulty"]
        ctx = GmContext(
            player_guid=player_guid,
            band_low=int((sit.get("hp_band") or [40, 90])[0]),
            floor_pct=int(sit.get("floor_pct") or 30),
            learn_ids=frozenset(self.learn_ids(trial)),
            gear_ids=frozenset(gear_mod.kit_entries(trial["player"]) if trial["player"].get("gear") == "tier" else ()),
            proficiency_ids=frozenset(gear_mod.proficiency_spells(gear_mod.pick_kit(trial["player"])[0])
                                      if trial["player"].get("gear") == "tier" else ()),
            strip_ids=frozenset(gear_mod.all_entries()),
            aura_ids=frozenset(self.aura_ids(trial)),
            arena_coords=frozenset((int(a.get("map", 0)), int(a["x"]), int(a["y"]),
                                    int(round(float(a.get("z", 0)))))
                                   for a in situations.load_arenas()),
            mob_entries=frozenset(self.mob_entries()),
            trial_id=trial["id"],
        )
        guard = GmGuard(agent, ledger, ctx, dry_run=self.args.dry_run,
                        abort=lambda s=session: s.abort_reason,
                        on_gm=lambda kind, text, target: marks.add(
                            {"kind": "gm", "line": text, "kind_detail": kind,
                             "target": norm_guid(target) if target else None}))
        collector = collect.Collector(agent, rep_dir, marks, {}, ledger, trial, self.log)
        collector.on_lost = lambda reason, s=session: s.abort(reason)
        extra_kw = {}
        if trial.get("extra_bots"):
            extra_kw["previous_extras"] = list(session.last_extras or [])
        try:
            setup_doc = run_setup(agent, trial, guard, ledger, ctx, first=(rep_index == 1),
                                  previous=session.last_bot, **extra_kw)
        finally:
            # a setup that dies after hiring still leaves its extra names for the trial teardown
            slots = list((ctx.extra or {}).get("extra_slots") or [])
            if any(slots):
                # index-aligned (None = that slot never got a hire): the next repeat reuses a
                # name only at its own extra index, and the teardown keeps the original indices
                session.last_extras = slots
        self.drain_guard(guard)
        for check in setup_doc.get("checks") or []:
            self.log("  %s %s: %s" % (check.get("id"), "ok" if check.get("ok") else "FAIL",
                                     (check.get("evidence") or "")[:160]))
        setup_doc["difficulty"] = dict(difficulty)
        setup_doc["slot"] = session.slot_id
        setup_doc["account"] = session.account
        self.write_setup(rep_dir, setup_doc)
        bot_name = setup_doc.get("bot_name") or ""
        if bot_name:
            session.last_bot = bot_name
        extra_names = [n for n in (setup_doc.get("extra_names") or []) if n]
        extra_slots = list(setup_doc.get("extra_slots") or [])
        if any(extra_slots):
            session.last_extras = extra_slots
        if not setup_doc.get("ok"):
            marks.add({"kind": "setup_failed", "reason": setup_doc.get("invalid_reason")})
            try:
                kw = {"extra_names": extra_names} if extra_names else {}
                # the unverified summons only, and no "not in view = gone": the whole ledger read UNKNOWN
                # (exit 5) from the second repeat on
                rows = teardown.repeat_cleanup(agent, guard, ledger, setup_doc.get("bot_name"),
                                               player_guid, wait_s=5,
                                               only=self.unverified_summons(session, ledger), **kw)
                self.note_verified(session, rows)
                self.note_teardown(rows)
            except Exception as exc:  # noqa: BLE001 - the repeat still ends INVALID
                self.note_exception(exc)
                self.log("setup-fail cleanup %s: %s" % (trial["id"], exc))
            self.drain_guard(guard)
            self.note_rep(session, trial, rep_index, difficulty, False, False, "setup_failed")
            aborted = self.aborted_exception(session)
            if aborted:
                raise aborted
            return bot_name, None
        if session.abort_reason:
            raise self.aborted_exception(session) or LeaseLost("lease lost")

        cfg = setup_doc.get("config") or {}
        if cfg.get("ok"):
            self.config = {"critical_health": cfg["critical"], "low_health": cfg["low"],
                           "medium_health": cfg["medium"]}
        marks.start()
        marks.add({"kind": "setup_ok", "guids": setup_doc["guids"]})
        if self.args.bind and bot_name:
            collect.bind_commander(agent, bot_name, self.log)
        sit_ctx = situations.SitContext(player_guid=player_guid, bot_name=bot_name,
                                        hire_guid=ctx.hire_guid, guids=setup_doc["guids"],
                                        extra_names=tuple(extra_names))
        collector.guids = setup_doc["guids"]
        slot_index = self.sessions.index(session) if session in self.sessions else 0
        situation = situations.Situation(agent, eff_trial, guard, ledger, marks, collector,
                                         sit_ctx, mobs=situations.load_mobs(getattr(self.args, "mobs_file", None)),
                                         log=self.log, slot_index=slot_index,
                                         abort=lambda s=session: s.abort_reason,
                                         on_arena=lambda arena, s=session, r=rep_index:
                                         self.note_arena(s, trial, r, arena),
                                         arena_claim=lambda arena, s=session: self.claim_arena(s, arena),
                                         arena_free=lambda arena, s=session: self.free_arena(s, arena),
                                         foreign_summons=lambda s=session: self.foreign_summons(s))
        collector.start()
        # cleanup runs on every path out of the situation window, exceptions included
        try:
            outcome = situation.run()
        finally:
            collector.stop()
            # every summon no cleanup has verified yet; "not in view" counts as gone only for this
            # repeat's own (made where the player stands), an earlier leftover may sit in another arena
            own = [g for g in ledger.summon_guids() if g and norm_guid(g) not in before_rep]
            kw = {"only": self.unverified_summons(session, ledger), "absent_is_gone": own}
            if extra_names:
                kw["extra_names"] = extra_names
            rows = teardown.repeat_cleanup(agent, guard, ledger, bot_name, player_guid, **kw)
            self.note_verified(session, rows)
            marks.add({"kind": "teardown", "rows": rows})
            self.note_teardown(rows)
            self.free_arenas(session)         # the arena goes back once this slot's summons are cleaned up
        self.drain_guard(guard)
        aborted = self.aborted_exception(session)
        if aborted:
            raise aborted
        for check in sit_ctx.checks:
            setup_doc["checks"].append(check)
        # mobs.count 0 (e.g. ooc_hurt) summons nothing, so there is no mobs_ready to check; a missing
        # count still means "has mobs" and keeps the fallback
        has_mobs = sit_mobs.get("count") != 0
        if has_mobs and not any(c.get("id") == "mobs_ready" for c in setup_doc["checks"]):
            window = self.mobs_window(agent, situation)
            if window:
                setup_doc["checks"].append(window)
        if sit_mobs.get("role") == "caster" and situation.summoned:
            cast = collector.mob_casts(situation.summoned)
            setup_doc["checks"].append({"id": "mob_caster_casts", "ok": bool(cast),
                                        "value": (cast or {}).get("spell_id"),
                                        "evidence": ("spell_start by a summoned mob" if cast
                                                     else "no spell_start by the summoned mob in the window")})
        setup_doc["mob_source"] = situation.mob_source
        setup_doc["outcome"] = outcome
        failed = [c for c in setup_doc["checks"] if not c.get("ok") and c.get("id") != "config_read"]
        # no arena / no mobs = no fight happened: the failed mobs_ready and player_visible rows are only
        # its consequence, so the rep says WHY there was no fight (sweep-1b read "player not visible" and
        # "0/0 summons" for 7 reps whose real cause was a polluted arena)
        no_fight = str(outcome.get("ended") or "") in ("no arena", "no mobs")
        setup_doc["ok"] = not failed and not outcome.get("invalid_reason")
        if no_fight and outcome.get("invalid_reason"):
            setup_doc["invalid_reason"] = outcome["invalid_reason"]
        elif failed:
            setup_doc["invalid_reason"] = failed[0].get("evidence") or failed[0].get("id")
        elif outcome.get("invalid_reason"):
            setup_doc["invalid_reason"] = outcome["invalid_reason"]
        self.write_setup(rep_dir, setup_doc)

        if self.args.bind:
            collect.pull_fights(self.run_dir, self.args.server, session.character, rep_dir)
            collect.unbind_commander(agent, bot_name or "")
        collect.normalise(rep_dir, trial, self.log)
        # REQUIRED: the player must have been visible to the bots (events.jsonl exists now)
        if not no_fight:
            self.apply_player_visible(rep_dir, trial, setup_doc)
        capped = ladder.capped(outcome)
        self.note_rep(session, trial, rep_index, difficulty, bool(setup_doc.get("ok")) and not capped,
                      capped, outcome.get("ended"))
        reason = str(outcome.get("invalid_reason") or "")
        if "disconnect" in reason.lower():
            raise Disconnected(reason)
        if "lease lost" in reason.lower():
            raise LeaseLost(reason)
        return bot_name, window_of(marks)

    def note_rep(self, session, trial, rep_index, difficulty, valid, capped, ended):
        """What the worker needs after a rep (valid? cut by the rescue cap?) and the RUN.json row."""
        session.last_rep = {"valid": bool(valid), "capped": bool(capped), "step": difficulty["step"]}
        row = {"trial": trial.get("id"), "rep": int(rep_index), "slot": session.slot_id,
               "account": session.account, "step": difficulty["step"],
               "level_delta": difficulty["level_delta"], "count": difficulty["count"],
               "refill": difficulty["refill"], "adaptive": difficulty["adaptive"],
               "valid": bool(valid), "capped": bool(capped), "ended": str(ended or "")}
        with self._lock:
            self.rep_log.append(row)

    def apply_player_visible(self, rep_dir, trial, setup_doc):
        """Append the in-fight `player_visible` row to setup.json; a failing one makes the rep INVALID."""
        visible = check_player_visible(rep_dir, trial)
        if visible is None:
            return None
        setup_doc["checks"].append(visible)
        self.log("  %s %s: %s" % (visible["id"], "ok" if visible["ok"] else "FAIL", visible["evidence"][:160]))
        if not visible["ok"]:
            setup_doc["ok"] = False
            setup_doc["invalid_reason"] = REASON_NOT_VISIBLE
        self.write_setup(rep_dir, setup_doc)
        return visible

    def learn_ids(self, trial):
        """Only the trial's own talent/spell ids: talents, the stance and its threat cast."""
        ids = set(int(x) for x in ((trial["player"].get("talents") or {}).get("spells") or []))
        stance = trial["player"].get("stance")
        if stance:
            ids.add(int(stance))
            threat = situations.THREAT_CAST.get(int(stance))
            if threat:
                ids.add(int(threat))
        return ids

    def aura_ids(self, trial):
        """The aura ids the trial file lists: player auras and the situation debuff."""
        ids = set(int(x) for x in (trial["player"].get("auras") or []))
        debuff = ((trial.get("situation") or {}).get("debuff") or {}).get("spell_id")
        if debuff:
            ids.add(int(debuff))
        return ids

    def mob_entries(self):
        mobs = situations.load_mobs(getattr(self.args, "mobs_file", None))
        out = set()
        for role in (mobs or {}).values():
            for row in role or []:
                if isinstance(row, dict) and row.get("entry"):
                    out.add(int(row["entry"]))
        return out

    def mobs_window(self, agent, situation):
        lo, hi = situation.level_window()
        return situations.mobs_ready_row(agent, self.ledger, (lo, hi), situation.summoned)

    def write_setup(self, rep_dir, doc):
        with open(os.path.join(rep_dir, "setup.json"), "w") as f:
            json.dump(doc, f, indent=2, sort_keys=True)

    def pull_evpull(self, trial, bot_name, windows):
        if not bot_name:
            return
        got = collect.run_evpull(self.run_dir, self.args.server, bot_name, self.log)
        if not got.get("ok"):
            self.warnings.append({"evpull": self.args.server, "detail": got.get("detail")})
            return
        for index, window in enumerate(windows, start=1):
            rep_dir = os.path.join(self.run_dir, trial["id"], "rep%d" % index)
            if os.path.isdir(rep_dir) and window:
                collect.split_botev(self.run_dir, self.args.server, rep_dir, window, self.log)

    def write_run_json(self, trials):
        doc = {
            "name": SLUG,
            "label": self.args.label,
            "server": self.args.server,
            # every slot a session held, in order (a full account moves the session to the next free slot)
            "slots": list(dict.fromkeys(h["slot"] for h in self.slot_history)) or [s.slot_id for s in self.sessions],
            "slot_accounts": {h["slot"]: h["account"] for h in self.slot_history},
            "slot_swaps": self.slot_swaps,
            "parallel": len(self.sessions),
            "reps": self.rep_log,
            "started": self.started,
            "ended": iso_now(),
            "binary": self.binary,
            "tb_version": self.tb_version,
            "ta_commit": self.ta_commit,
            "ta_version": self.ta_version,
            "critical_health": self.config.get("critical_health"),
            "low_health": self.config.get("low_health"),
            "medium_health": self.config.get("medium_health"),
            "trials": [t.get("id") for t in trials],
            # --repeats 0 means "each trial's own count": record what actually ran
            "repeats_run": {t.get("id"): len(glob.glob(os.path.join(self.run_dir, t.get("id") or "-", "rep*")))
                            for t in trials},
            "characters": self.characters_used,
            "warnings": self.warnings,
            "unverified_summons": sorted(set(self.unverified)),
            "refused": self.refused,
            "args": {k: getattr(self.args, k) for k in
                     ("server", "label", "repeats", "parallel", "out", "bind", "dry_run")},
        }
        with open(os.path.join(self.run_dir, "RUN.json"), "w") as f:
            json.dump(doc, f, indent=2, sort_keys=True)


def window_of(marks):
    start = end = None
    for row in marks.rows:
        if row.get("kind") == "setup_ok" and start is None:
            start = row.get("wall")
        if row.get("kind") == "fight_end":
            end = row.get("wall")
    if start and end:
        return (start, end)
    return None


# -- entry points ----------------------------------------------------------

def score_and_card(run_dir, log=print):
    """Score a finished run and write card.md/card.json (what `trials.py score <run-dir>` does).

    The live runner used to stop after RUN.json, so every run needed a separate `score`. A run
    that never got as far as RUN.json (setup refused) has nothing to score. A scoring error is
    logged and never changes the run's exit code: the run dir stays re-scorable by hand.
    """
    if not os.path.isfile(os.path.join(run_dir, "RUN.json")):
        return None
    try:
        from trl import score
        scored = score.score_run(run_dir)
    except Exception as exc:  # noqa: BLE001 - the run itself already finished
        log("scoring failed (%s: %s); run `trials.py score %s` by hand" % (type(exc).__name__, exc, run_dir))
        return None
    verdicts = [v.get("verdict") for t in scored for v in (t.get("verdicts") or [])]
    log("scored: %d FAIL, %d INVALID; card at %s" % (
        verdicts.count("FAIL"), verdicts.count("INVALID"), os.path.join(run_dir, "card.md")))
    return scored


def _install_signals():
    def handler(signum, frame):
        STOP.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def main(args):
    binary = resolve_binary(args)
    allowed, detail = lease.place_allowed(args.server, binary)
    if not allowed:
        print("not a test server: %s" % detail, file=sys.stderr)
        return 4
    trials = load_trials(args.trials)
    if not trials:
        print("no trials resolved", file=sys.stderr)
        return 2
    wanted = max(1, min(MAX_PARALLEL, int(getattr(args, "parallel", 1) or 1)))
    slots = lease.pick_free(binary, args.server, wanted + 4)
    wanted_ids = [str(x) for x in (getattr(args, "slot", None) or [])]
    if wanted_ids:
        slots = [r for r in slots if str((r.get("slot") or {}).get("id")) in wanted_ids]
    slots = slots[:wanted]
    if not slots:
        print("no free slot on %s" % args.server, file=sys.stderr)
        return 4
    if len(slots) < wanted:
        print("only %d free slot(s) on %s; running with them" % (len(slots), args.server), file=sys.stderr)
    label = getattr(args, "label", "") or "run"
    run_dir = os.path.join(os.path.abspath(os.path.expanduser(getattr(args, "out", "") or DEFAULT_OUT)),
                           "%s-%s" % (stamp(), label))
    if getattr(args, "dry_run", False):
        print("dry run: would use %s with slots %s" % (args.server, ", ".join(
            str((r.get("slot") or {}).get("id")) for r in slots)))
        for trial in trials:
            print("  %s (%s repeats)" % (trial.get("id"),
                                        getattr(args, "repeats", 0) or trial.get("repeats") or 1))
        print("run dir would be %s" % run_dir)
        return 0
    _install_signals()
    run = Run(args, binary, run_dir, slots)
    run.run_stamp = stamp()
    # a real run shares realmd's login window with every other trials process on this machine
    run.pacer = pacing.LoginPacer(log=run.log, debug=run.debug, state_path=pacing.DEFAULT_STATE)
    try:
        rc = run.execute(trials)
    finally:
        STOP.set()
        for session in run.sessions:
            run.stop_session(session)
    score_and_card(run.run_dir, run.log)
    return rc


def cleanup(args):
    run_dir = getattr(args, "run_dir", None) or getattr(args, "dir", None)
    if not run_dir:
        print("cleanup needs a run dir", file=sys.stderr)
        return 2
    binary = resolve_binary(args) or None
    return teardown.cleanup(os.path.abspath(os.path.expanduser(run_dir)), binary=binary)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--trial", action="append", default=[], required=True)
    ap.add_argument("--label", default="run")
    ap.add_argument("--repeats", type=int, default=0)
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument("--out", default="")
    ap.add_argument("--bind", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--binary", default=None)
    ap.add_argument("--slot", action="append", default=[])
    ap.add_argument("--mobs-file", default=None)
    ns = ap.parse_args()
    ns.trials = ns.trial
    sys.exit(main(ns))
