"""One repeat's collectors: the event pump, the 1 Hz sampler, marks, evpull, fights.

Two threads per slot: the pump pages `events` and appends raw rows; the sampler
writes status + a units snapshot every second. Together they stay under 4 RPC
calls per second per slot.

marks.jsonl carries the runner's phase rows; `t` is seconds since fight_start
(or setup_ok for trials without a pull).
"""

import csv
import io
import json
import math
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from .agent import event_text, norm_guid
from .ledger import now_iso

TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EVPULL = os.environ.get("TRIALS_EVPULL", "")   # optional script: evpull.sh <run_dir> <server>
SAMPLE_UNITS_YARDS = 60
DISCONNECT = re.compile(r"disconnect|logged out|session (?:lost|ended|closed)|left the world", re.I)


def _append(path, row):
    with open(path, "a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
        f.flush()


class Marks:
    """The rep's phase rows, and the ONE clock of its files.

    live3: the host's wall clock (WSL2) steps BACK about 2-3 s every ~32 s, so rows stamped with
    datetime.now() ran backwards in blocks of ~32 rows (the "duplicate series" of run
    20261108-050750: one sampler, a clock that repeats itself). now_iso() is the wall clock read
    ONCE at construction plus the monotonic time since: it never steps, and the sampler, the
    governor's marks and the situation all stamp with it.
    """

    def __init__(self, path, log=None):
        self.path = path
        self.rows = []
        self.base_wall = None
        self._lock = threading.Lock()
        self.log = log or (lambda line: None)
        self._anchor_wall = time.time()
        self._anchor_mono = time.monotonic()

    def now_iso(self, offset=0.0):
        secs = self._anchor_wall + (time.monotonic() - self._anchor_mono) + float(offset)
        return datetime.fromtimestamp(secs, timezone.utc).astimezone().isoformat(timespec="milliseconds")

    def start(self, wall=None):
        with self._lock:
            self.base_wall = wall or self.now_iso()

    def rel(self, wall=None):
        wall = wall or self.now_iso()
        if not self.base_wall:
            return 0.0
        try:
            a = datetime.fromisoformat(wall)
            b = datetime.fromisoformat(self.base_wall)
        except ValueError:
            return 0.0
        return round((a - b).total_seconds(), 2)

    def add(self, row):
        row = dict(row)
        row.setdefault("wall", self.now_iso())
        row.setdefault("t", self.rel(row["wall"]))
        with self._lock:
            self.rows.append(row)
            _append(self.path, row)
        return row

    def excluded(self, start, end, why):
        return self.add({"kind": "excluded", "from": start, "to": end, "why": why})

    def kinds(self, kind):
        with self._lock:
            return [r for r in self.rows if r.get("kind") == kind]


class Collector:
    def __init__(self, agent, rep_dir, marks, guids, ledger, trial, log=None):
        self.agent = agent
        self.rep_dir = rep_dir
        self.marks = marks
        self.guids = guids
        self.ledger = ledger
        self.trial = trial
        self.log = log or (lambda line: None)
        self.raw_path = os.path.join(rep_dir, "raw-ta.jsonl")
        self.samples_path = os.path.join(rep_dir, "samples.jsonl")
        self._stop = threading.Event()
        self._threads = []
        self._seq = set()
        self._lock = threading.Lock()
        self.tick_hooks = []
        self.lost = ""
        self.on_lost = None
        self.samples = 0
        self.raw_rows = 0
        self.raw = []
        self.last_sample = None

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        for target in (self._pump, self._sample):
            th = threading.Thread(target=target, daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self):
        self._stop.set()
        # one governor tick may be inside a GM send (10 s per RPC): wait for it,
        # bounded so a wedged agent cannot hang the run
        deadline = time.time() + 40.0
        for th in self._threads:
            th.join(timeout=max(0.1, deadline - time.time()))
        self._threads = []

    def mark_lost(self, reason):
        """The session is gone: no more samples, and the situation loop ends the repeat."""
        if self.lost:
            return
        self.lost = str(reason or "disconnected")
        self._stop.set()
        hook = self.on_lost
        if hook:
            try:
                hook(self.lost)
            except Exception as exc:  # noqa: BLE001 - a bad hook must not kill the sampler
                self.log("on_lost error: %s" % exc)

    # -- event pump --------------------------------------------------------

    def _pump(self):
        while not self._stop.is_set():
            page = self.agent.events_since(wait_ms=900, limit=1000)
            rows = page.get("events") or []
            if page.get("dropped"):
                self.marks.add({"kind": "events_dropped", "dropped": True,
                                "oldest": page.get("oldest"), "next": page.get("next")})
            for row in rows:
                seq = row.get("seq")
                with self._lock:
                    if seq in self._seq:
                        continue
                    self._seq.add(seq)
                _append(self.raw_path, row)
                with self._lock:
                    self.raw_rows += 1
                    self.raw.append(row)
                if row.get("kind") == "session" and DISCONNECT.search(event_text(row)):
                    self.mark_lost("disconnected: %s" % event_text(row)[:120])
            if not rows:
                self._stop.wait(0.1)

    # -- sampler -----------------------------------------------------------

    def _sample(self):
        while not self._stop.is_set():
            begin = time.time()
            try:
                st = self.agent.status()
                units = self.agent.units(within=SAMPLE_UNITS_YARDS)
            except Exception as exc:  # noqa: BLE001 - a lost session must not kill the repeat
                self.log("sample error: %s" % exc)
                self.mark_lost("disconnected: %s" % str(exc)[:120])
                return
            state = str(st.get("state") or "")
            if state != "in_world":
                self.mark_lost("disconnected: agent state %s" % (state or "unknown"))
                return
            own = self._own_row(st, units)
            rows = self._rows(st, units, own=own)
            for row in rows:
                _append(self.samples_path, row)
            with self._lock:
                self.samples += len(rows)
            sample = {"player": None, "bot": None}
            by_guid = {norm_guid(u.get("guid")): u for u in units}
            # status is the authority for the player's own hp (a corpse drops out of units)
            sample["player"] = {"hp": st.get("health"), "max_hp": st.get("max_health"),
                                "pct": pct(st.get("health"), st.get("max_health")),
                                "auras": sorted(int(a.get("spell_id", 0)) for a in own.get("auras") or []),
                                "in_combat": _own_in_combat(st, own, units),
                                "dead": bool(st.get("dead"))}
            bot_guid = norm_guid(self.guids.get("bot") or "")
            if bot_guid and bot_guid in by_guid:
                b = by_guid[bot_guid]
                sample["bot"] = {"hp": b.get("health"), "max_hp": b.get("max_health"),
                                 "pct": pct(b.get("health"), b.get("max_health")),
                                 "auras": sorted(int(a.get("spell_id", 0)) for a in b.get("auras") or []),
                                 "in_combat": bool(b.get("in_combat"))}
            for index, guid in enumerate(self.guids.get("extras") or [], start=1):
                unit = by_guid.get(norm_guid(guid))
                if unit:
                    sample["extra%d" % index] = {
                        "hp": unit.get("health"), "max_hp": unit.get("max_health"),
                        "pct": pct(unit.get("health"), unit.get("max_health")),
                        "auras": sorted(int(a.get("spell_id", 0)) for a in unit.get("auras") or []),
                        "in_combat": bool(unit.get("in_combat"))}
            self.last_sample = sample
            for hook in list(self.tick_hooks):
                try:
                    hook(sample)
                except Exception as exc:  # noqa: BLE001
                    self.log("tick hook error: %s" % exc)
            spent = time.time() - begin
            self._stop.wait(max(0.05, 1.0 - spent))

    def mob_casts(self, guids):
        """First spell_start/spell_go by any of these guids in the raw rows (caster proof)."""
        want = {norm_guid(g) for g in guids}
        with self._lock:
            rows = list(self.raw)
        for row in rows:
            if row.get("kind") not in ("spell_start", "spell_go"):
                continue
            if _row_guid(row, "source") in want:
                return row
        return None

    def _own_row(self, st, units):
        """The player's own unit row (auras, in_combat). `units` never lists the player
        (Session.Units skips self), so it is asked of the `unit` rpc by guid, as setup's
        own_row does; {} when the agent cannot answer."""
        guid = norm_guid(st.get("guid"))
        if not guid:
            return {}
        for u in units:
            if norm_guid(u.get("guid")) == guid:
                return u
        try:
            return self.agent.unit(guid=guid) or {}
        except Exception as exc:  # noqa: BLE001 - one missed sample must not end the repeat
            self.log("own unit read failed: %s" % exc)
            return {}

    def _rows(self, st, units, own=None):
        wall = self.marks.now_iso()
        t = self.marks.rel(wall)
        mono = st.get("mono_ms")          # the agent's monotonic axis, the one its events carry
        out = []

        def row(label, guid, unit, in_combat=None, target=None, x=None, y=None, z=None):
            out.append({
                "t": t, "wall": wall, "unit": label, "guid": norm_guid(guid),
                "hp": int(unit.get("health") or 0), "max_hp": int(unit.get("max_health") or 0),
                "power": int(unit.get("power") or 0), "max_power": int(unit.get("max_power") or 0),
                "auras": sorted(int(a.get("spell_id", 0)) for a in unit.get("auras") or []),
                "casting": unit.get("casting"),
                "in_combat": bool(unit.get("in_combat") if in_combat is None else in_combat),
                "target": norm_guid(target if target is not None else unit.get("target") or ""),
                "x": x if x is not None else unit.get("x"),
                "y": y if y is not None else unit.get("y"),
                "z": z if z is not None else unit.get("z"),
                **({"mono_ms": mono} if mono is not None else {}),
            })

        by_guid = {norm_guid(u.get("guid")): u for u in units}
        own_guid = norm_guid(st.get("guid"))
        if own is None:
            own = by_guid.get(own_guid, {})
        row("player", own_guid, {**own, "health": st.get("health"), "max_health": st.get("max_health"),
                                 "power": st.get("power"), "max_power": st.get("max_power")},
            in_combat=_own_in_combat(st, own, units),
            target=st.get("target"), x=st.get("x"), y=st.get("y"), z=st.get("z"))
        for label, key in (("bot", "bot"), ("pet", "pet")):
            guid = norm_guid(self.guids.get(key) or "")
            if not guid:
                continue
            unit = by_guid.get(guid)
            if unit:
                row(label, guid, unit)
        for index, guid in enumerate(self.guids.get("extras") or [], start=1):
            unit = by_guid.get(norm_guid(guid))
            if unit:
                row("extra%d" % index, norm_guid(guid), unit)
        for guid in self.guids.get("mobs") or []:
            unit = by_guid.get(norm_guid(guid))
            if unit:
                row("mob:%s" % norm_guid(guid), guid, unit)
        return out


def _own_in_combat(st, own, units):
    """The player's combat flag: status when it carries one, else the unit row.

    With no own row at all (the `unit` rpc failed) the flag falls back to what the
    world shows: a hostile unit in combat that the player has targeted.
    """
    if "in_combat" in st:
        return bool(st.get("in_combat"))
    if "in_combat" in (own or {}):
        return bool(own.get("in_combat"))
    target = norm_guid(st.get("target") or "")
    if target:
        for u in units or []:
            if norm_guid(u.get("guid")) == target and u.get("hostile") and u.get("in_combat") \
                    and not u.get("dead"):
                return True
    return False


def _row_guid(row, side):
    value = row.get(side + "_guid")
    if value:
        return norm_guid(value)
    flat = row.get(side)
    if isinstance(flat, dict):
        return norm_guid(flat.get("guid"))
    return ""


def pct(hp, maxhp):
    try:
        return round(100.0 * float(hp) / max(1, float(maxhp)), 1)
    except (TypeError, ValueError):
        return 0.0


# -- evpull ----------------------------------------------------------------

def run_evpull(run_dir, server, pattern, log=None):
    log = log or (lambda line: None)
    if not EVPULL or not os.path.isfile(EVPULL):
        return {"ok": False, "detail": "no event-pull script (set TRIALS_EVPULL); bot events skipped"}
    env = dict(os.environ)
    if pattern:
        env["EVPULL_PATTERN"] = pattern
    try:
        res = subprocess.run(["bash", EVPULL, run_dir, server], capture_output=True, text=True, timeout=180, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": str(exc)}
    detail = (res.stdout + res.stderr).strip().splitlines()
    log("evpull %s: %s" % (server, detail[-1] if detail else "no output"))
    return {"ok": res.returncode == 0, "detail": " ".join(detail)}


def split_botev(run_dir, server, rep_dir, window, log=None):
    """Split ev-<server>.csv rows into this repeat's wall window as botev.csv."""
    src = os.path.join(run_dir, "ev-%s.csv" % server)
    if not os.path.isfile(src):
        return {"ok": False, "detail": "no ev-%s.csv" % server, "rows": 0}
    try:
        lo = datetime.fromisoformat(window[0]).replace(tzinfo=None)
        hi = datetime.fromisoformat(window[1]).replace(tzinfo=None)
    except (TypeError, ValueError):
        return {"ok": False, "detail": "bad window %r" % (window,), "rows": 0}
    kept = []
    with open(src, newline="", errors="replace") as f:
        for line in f:
            ts = line.split(",", 1)[0].strip()
            try:
                when = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if lo <= when <= hi:
                kept.append(line)
    with open(os.path.join(rep_dir, "botev.csv"), "w", newline="") as f:
        f.writelines(kept)
    if log:
        log("botev: %d rows in %s..%s" % (len(kept), window[0], window[1]))
    return {"ok": True, "rows": len(kept), "detail": "%d rows" % len(kept)}


# -- dashboard fights (--bind only) ----------------------------------------

def bind_commander(agent, bot_name, log=None):
    agent.tbm("commander bind %s" % bot_name)
    return {"ok": True}


def unbind_commander(agent, bot_name, log=None):
    agent.tbm("commander unbind %s" % bot_name)
    return {"ok": True}


def pull_fights(run_dir, server, player, rep_dir, timeout=60):
    """`--bind`: the dashboard's fights for this commander, then each fight detail."""
    fights_dir = os.path.join(rep_dir, "fights")
    os.makedirs(fights_dir, exist_ok=True)
    try:
        res = subprocess.run(["tbdash-get", server,
                              "/api/v1/combat/fights?commander=%s" % player],
                             capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": str(exc)}
    if res.returncode:
        return {"ok": False, "detail": (res.stdout + res.stderr).strip()[:300]}
    try:
        listing = json.loads(res.stdout or "[]")
    except ValueError:
        return {"ok": False, "detail": "fights listing is not json"}
    fights = listing.get("fights", listing) if isinstance(listing, dict) else listing
    n = 0
    for fight in fights if isinstance(fights, list) else []:
        fid = fight.get("id") if isinstance(fight, dict) else None
        if not fid:
            continue
        try:
            got = subprocess.run(["tbdash-get", server, "/api/v1/combat/fight/%s" % fid],
                                 capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if got.returncode == 0 and got.stdout.strip():
            with open(os.path.join(fights_dir, "fight-%s.json" % fid), "w") as f:
                f.write(got.stdout)
            n += 1
    return {"ok": True, "detail": "%d fight files" % n, "count": n}


def normalise(rep_dir, trial, log=None):
    """Call brief 2's normaliser when it exists; the raw files stand alone otherwise."""
    try:
        from trl import events as normalizer
    except Exception:  # noqa: BLE001 - brief 2 may not be in yet
        return {"ok": False, "detail": "trl.events not installed"}
    try:
        normalizer.normalise(rep_dir, trial)
        return {"ok": True, "detail": "events.jsonl"}
    except Exception as exc:  # noqa: BLE001
        if log:
            log("normalise failed: %s" % exc)
        return {"ok": False, "detail": str(exc)}
