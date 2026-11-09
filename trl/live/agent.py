"""The turtle-agent RPC client for one slot dir.

Shape reused from tests/live-1553/before/run.py: every call shells out to
`<slot>/turtle-agent rpc --raw . <method> <json>` with the slot dir as cwd.
Nothing here reads the slot's settings, token or pass files.
"""

import json
import os
import re
import subprocess
import threading
import time

DEFAULT_TIMEOUT = "20s"
# a seq no log reaches: the events rpc answers `next` = the newest seq and no rows
HEAD_PROBE = 1 << 62


class RpcError(Exception):
    pass


class UnitGone(RpcError):
    """The agent answered that the unit is no longer there (not an RPC failure)."""


_COLOR = re.compile(r"\|c[0-9a-fA-F]{8}|\|r")
_LINK = re.compile(r"\|H[^|]*\|h(\[[^\]]*\])?\|h")
_CHAT_KINDS = ("chat", "whisper", "system", "party", "channel_notify")
# the agent's explicit "no such unit" answers; a visibility/range refusal ("outside",
# "too far", "not in view") is an RPC failure: UNKNOWN is not gone until death or
# absence is confirmed, so the cleanup stays non-zero
_GONE = re.compile(r"no unit|not found|no such", re.I)


def clean_text(text):
    if not text:
        return ""
    return _LINK.sub(lambda m: m.group(1) or "", _COLOR.sub("", str(text))).strip()


def _side(row, side):
    """The name or guid of an event row's source/target, both JSON shapes."""
    flat = row.get(side + "_guid")
    value = row.get(side)
    if isinstance(value, dict):
        return value
    out = {}
    if isinstance(value, str):
        out["name"] = value
    if flat:
        out["guid"] = flat
    return out


def event_name(row, side="source"):
    return _side(row, side).get("name") or ""


def event_guid(row, side="source"):
    return norm_guid(_side(row, side).get("guid") or "")


def norm_guid(value):
    if not value:
        return ""
    text = str(value).strip().lower()
    if not text.startswith("0x"):
        try:
            text = "0x%016x" % int(text)
        except ValueError:
            return text
    try:
        return "0x%016x" % int(text, 16)
    except ValueError:
        return text


def event_text(row):
    text = row.get("text")
    if text:
        return clean_text(text)
    data = row.get("data")
    if isinstance(data, dict):
        for key in ("raw", "text", "msg", "message"):
            if data.get(key):
                return clean_text(data[key])
    return ""


def _duration_seconds(spec):
    spec = str(spec or "20s").strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m)", spec)
    if not m:
        return 20.0
    value = float(m.group(1))
    return value / 1000.0 if m.group(2) == "ms" else value * 60.0 if m.group(2) == "m" else value


class Agent:
    def __init__(self, slot_dir, binary=None, log=None):
        self.slot_dir = slot_dir
        self.binary = binary or os.path.join(slot_dir, "turtle-agent")
        self.log = log or (lambda line: None)
        self.cursor = 0
        self._lock = threading.Lock()
        # one lock for every action that depends on the server's selection: a
        # select+send pair in the guard and target+attack in the situation loop
        self.action_lock = threading.RLock()
        # bumped by every Situation selection move; the guard restores the previous
        # target only when this is unchanged since it captured it
        self.selection_gen = 0
        self.proc = None

    # -- transport ---------------------------------------------------------

    def rpc(self, method, params=None, timeout=DEFAULT_TIMEOUT, grace=15.0):
        """One agent RPC. `grace` is how long past the RPC's own timeout the subprocess may live before it is
        killed (a call under a deadline passes a short one)."""
        argv = [self.binary, "rpc", "--timeout", str(timeout), "--raw", ".", method]
        if params is not None:
            argv.append(json.dumps(params))
        try:
            res = subprocess.run(argv, cwd=self.slot_dir, capture_output=True, text=True,
                                 timeout=_duration_seconds(timeout) + grace)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"_err": "%s: %s" % (method, exc)}
        if res.returncode:
            return {"_err": (res.stdout + res.stderr).strip(), "_rc": res.returncode}
        text = (res.stdout or "").strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except ValueError:
            return {"_raw": text}

    def dl_rpc(self, method, params=None, deadline=None, default_s=20.0):
        """An RPC under a deadline: not started once the deadline is spent (`{"_err": ..., "_deadline": True}`),
        else its own timeout and its subprocess kill grace are capped by the time left (1 s floor).
        Without a deadline it is a plain `rpc`."""
        if deadline is None or not deadline.finite():
            return self.rpc(method, params)
        if deadline.expired():
            return {"_err": "%s: setup deadline spent, not sent" % method, "_deadline": True}
        secs = max(1, int(deadline.cap(default_s)))
        kw = {"timeout": "%ds" % secs}
        if getattr(self.rpc, "__func__", None) is Agent.rpc:      # the real transport; test fakes take no grace
            kw["grace"] = 1.0
        return self.rpc(method, params, **kw)

    def rpc_must(self, method, params=None, timeout=DEFAULT_TIMEOUT):
        out = self.rpc(method, params, timeout)
        if isinstance(out, dict) and "_err" in out:
            raise RpcError("%s: %s" % (method, out["_err"]))
        return out

    # -- observation -------------------------------------------------------

    def status(self):
        return self.rpc_must("status")

    def units(self, within=None, entry=None, name=None, hostile=None, limit=None):
        out = self.rpc("units", self._units_params(within, entry, name, hostile, limit))
        return out if isinstance(out, list) else []

    def units_must(self, within=None, entry=None, name=None, hostile=None, limit=None):
        """The same list, but an rpc error raises instead of reading as an empty world."""
        out = self.rpc("units", self._units_params(within, entry, name, hostile, limit))
        if isinstance(out, dict) and "_err" in out:
            raise RpcError("units: %s" % out["_err"])
        if not isinstance(out, list):
            raise RpcError("units: not a list (%r)" % (out,))
        return out

    @staticmethod
    def _units_params(within=None, entry=None, name=None, hostile=None, limit=None):
        params = {}
        if within is not None:
            params["within"] = within
        if entry is not None:
            params["entry"] = entry
        if name is not None:
            params["name"] = name
        if hostile is not None:
            params["hostile"] = hostile
        if limit is not None:
            params["limit"] = limit
        return params

    def unit(self, guid=None, name=None):
        out = self.rpc("unit", self._unit_params(guid, name))
        return out if isinstance(out, dict) and "_err" not in out else None

    def unit_must(self, guid=None, name=None):
        """One unit row; raises UnitGone when the agent says it is not there, RpcError otherwise."""
        out = self.rpc("unit", self._unit_params(guid, name))
        if isinstance(out, dict) and "_err" in out:
            err = str(out["_err"])
            if _GONE.search(err):
                raise UnitGone("unit %s: %s" % (guid or name, err))
            raise RpcError("unit %s: %s" % (guid or name, err))
        if not isinstance(out, dict) or not out.get("guid"):
            raise UnitGone("unit %s: no row" % (guid or name))
        return out

    @staticmethod
    def _unit_params(guid=None, name=None):
        params = {}
        if guid:
            params["guid"] = guid
        if name:
            params["name"] = name
        return params

    def own_row(self):
        st = self.status()
        guid = norm_guid(st.get("guid"))
        row = self.unit(guid=guid) if guid else None
        return row or {"guid": guid, "health": st.get("health", 0), "max_health": st.get("max_health", 1),
                       "power": st.get("power", 0), "max_power": st.get("max_power", 0),
                       "auras": [], "level": st.get("level", 0)}

    def spell_ids(self, deadline=None):
        rows = self.dl_rpc("spells", {"passive": True}, deadline, 10.0)
        if isinstance(rows, dict):
            rows = rows.get("spells") or []
        return {int(r["spell_id"]) for r in rows if isinstance(r, dict) and r.get("spell_id")}

    def head(self, deadline=None):
        """The event log's newest seq, asked of the agent (None when it cannot answer).

        A `since` past the end answers `next` = the newest seq and no events, so the
        probe costs one empty page and reads nothing.
        """
        out = self.dl_rpc("events", {"since": HEAD_PROBE, "limit": 1, "wait_ms": 0}, deadline, 5.0)
        if isinstance(out, dict) and "_err" not in out:
            try:
                return int(out.get("next"))
            except (TypeError, ValueError):
                return None
        return None

    def snapshot(self, deadline=None):
        """The cursor a reply read starts from: the log head NOW.

        `self.cursor` only moves while the collector's event pump runs; in setup the
        pump is off, so it still sits at the login (0) and a reply read from it matches
        lines of earlier commands (live1: a `co ?` read returned the previous hire's
        Strategies line). The head is exact; the cursor is the fallback.
        """
        head = self.head(deadline)
        if head is not None:
            return head
        with self._lock:
            return self.cursor

    def party(self, deadline=None):
        out = self.dl_rpc("party", None, deadline, 10.0)
        return out if isinstance(out, dict) and "_err" not in out else {}

    def members(self, deadline=None):
        return list(self.party(deadline).get("members") or [])

    # -- actions -----------------------------------------------------------

    def chat(self, type_, text, to=None, deadline=None):
        params = {"type": type_, "text": text}
        if to:
            params["to"] = to
        return self.dl_rpc("chat", params, deadline, 10.0)

    def tbm(self, command, deadline=None):
        out = self.dl_rpc("tbm", {"command": command}, deadline, 10.0)
        return int(out.get("since", 0)) if isinstance(out, dict) else 0

    def say_to_bot(self, bot, command, deadline=None):
        """Playerbot chat order: '<bot> <command>' in party chat."""
        return self.chat("party", "%s %s" % (bot, command), deadline=deadline)

    def cast(self, spell_id, target=None):
        params = {"spell_id": int(spell_id)}
        if target:
            params["target"] = str(target)
        return self.rpc("cast", params)

    # -- events ------------------------------------------------------------

    def events_since(self, since=None, kinds=None, wait_ms=900, limit=1000, cap_s=None, deadline=None):
        """One events page. `cap_s` bounds this RPC's own timeout (a caller under a deadline never lets one
        RPC outlive what is left of it)."""
        with self._lock:
            start = self.cursor if since is None else int(since)
        params = {"since": start, "limit": limit, "wait_ms": wait_ms}
        if kinds:
            params["kinds"] = list(kinds)
        rpc_ms = wait_ms + 20000
        if cap_s is not None:
            rpc_ms = min(rpc_ms, max(1000, int(cap_s * 1000)))
        if deadline is not None and deadline.finite():
            if deadline.expired():
                return {"events": [], "next": start, "oldest": start, "dropped": False, "more": False}
            rpc_ms = min(rpc_ms, max(1000, int(deadline.left() * 1000)))
        kw = {}
        if deadline is not None and deadline.finite() and getattr(self.rpc, "__func__", None) is Agent.rpc:
            kw["grace"] = 1.0
        page = self.rpc("events", params, timeout="%dms" % rpc_ms, **kw)
        if not isinstance(page, dict) or "_err" in page:
            return {"events": [], "next": start, "oldest": start, "dropped": False, "more": False}
        page.setdefault("events", [])
        if since is None:
            with self._lock:
                self.cursor = int(page.get("next", start))
        return page

    def lines(self, rows):
        out = []
        for row in rows:
            text = event_text(row)
            if text:
                out.append(text)
        return out

    def raw_lines(self, rows):
        out = []
        for row in rows:
            text = row.get("text")
            if not text and isinstance(row.get("data"), dict):
                text = row["data"].get("raw") or row["data"].get("text")
            if text:
                out.append(str(text))
        return out

    def read_raw_lines(self, since, timeout=10.0, kinds=_CHAT_KINDS, stop=None):
        rows = []
        deadline = time.time() + timeout
        page_from = since
        while True:
            page = self.events_since(page_from, kinds=kinds, wait_ms=500)
            rows.extend(page.get("events") or [])
            page_from = int(page.get("next", page_from))
            texts = self.raw_lines(rows)
            if stop and any(stop(t) for t in texts):
                return texts, rows
            if time.time() >= deadline:
                return texts, rows

    def read_rows(self, since, timeout=10.0, kinds=_CHAT_KINDS, stop=None, deadline=None):
        """Poll the event log from `since` and return the event ROWS (sender and all), until `stop(rows)` is
        true or the timeout is up. Every page RPC is capped by the time left."""
        rows = []
        end = time.time() + max(0.0, timeout)
        page_from = since
        while True:
            page = self.events_since(page_from, kinds=kinds, wait_ms=500,
                                     cap_s=max(1.0, end - time.time()), deadline=deadline)
            rows.extend(page.get("events") or [])
            page_from = int(page.get("next", page_from))
            if stop and stop(rows):
                return rows
            if time.time() >= end or (deadline is not None and deadline.expired()):
                return rows

    def read_lines(self, since, timeout=10.0, kinds=_CHAT_KINDS, stop=None, deadline=None):
        """Poll the event log from `since` and return (lines, rows)."""
        stop_rows = (lambda rows: any(stop(t) for t in self.lines(rows))) if stop else None
        rows = self.read_rows(since, timeout=timeout, kinds=kinds, stop=stop_rows, deadline=deadline)
        return self.lines(rows), rows

    def wait_reply(self, since, pattern, timeout=10.0, kinds=_CHAT_KINDS):
        rx = re.compile(pattern, re.I)

        def hit(text):
            return bool(rx.search(text))

        texts, rows = self.read_lines(since, timeout=timeout, kinds=kinds, stop=hit)
        for text in texts:
            if rx.search(text):
                return text, rows
        return None, rows


def wait_until(fn, timeout=10.0, poll=0.5):
    deadline = time.time() + timeout
    while True:
        value = fn()
        if value:
            return value
        if time.time() >= deadline:
            return value
        time.sleep(poll)
