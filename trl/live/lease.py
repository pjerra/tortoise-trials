"""Server allow-list, slot picking and leases.

The allow-list probe runs the turtle-agent binary with a bogus server and parses the
refusal's `test servers only (<list>)`.

Reserved slots (default 3, override: TRIALS_RESERVED_SLOTS="3,5" or "") are refused outright (`NEVER_SLOTS`), whatever the slot table says; a
slot whose port already has a listener is dropped too.
"""

import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time

TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NEVER_SLOTS = {s.strip() for s in os.environ.get("TRIALS_RESERVED_SLOTS", "3").split(",") if s.strip()}   # slots never taken (default: 3)
TTL = 90


def bin_dir_for(binary):
    """A directory holding the binary as `turtle-agent` (the probe joins that name)."""
    binary = os.path.abspath(os.path.expanduser(binary))
    if os.path.basename(binary) == "turtle-agent":
        return os.path.dirname(binary)
    keep = tempfile.mkdtemp(prefix="trl-bin-")
    link = os.path.join(keep, "turtle-agent")
    os.symlink(binary, link)
    return keep


def check_place_allow(server, bin_dir):
    """[(status, check, server, detail)]: is `server` in the binary's test-server list?"""
    binary = os.path.join(bin_dir, "turtle-agent")
    probe = [binary, "raid-walk", "--server", "trials-probe-none",
             "--from", "step 1", "--place", "zul_gurub"]
    try:
        p = subprocess.run(probe, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return [("FAIL", "place-allow", server, "cannot read the binary's place list: probe timeout")]
    except OSError as exc:
        return [("FAIL", "place-allow", server, "cannot read the binary's place list: %s" % exc)]
    text = (p.stdout or "") + "\n" + (p.stderr or "")
    m = re.search(r"test servers only \(([^)]*)\)", text)
    if not m:
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        return [("FAIL", "place-allow", server, "cannot read the binary's place list: %s" % first[:200])]
    servers = [x.strip() for x in m.group(1).split(",") if x.strip()]
    if server not in servers:
        return [("FAIL", "place-allow", server, "not in the binary's place list: %s" % ", ".join(servers))]
    return [("PASS", "place-allow", server, "place list: %s" % ", ".join(servers))]


def place_allowed(server, binary):
    """(allowed, detail) from the place probe."""
    if not binary or not os.path.isfile(os.path.expanduser(binary)):
        return False, "no turtle-agent binary (pass --binary, set TRIALS_BINARY, or put turtle-agent on PATH)"
    rows = check_place_allow(server, bin_dir_for(binary))
    status, _check, _server, detail = rows[0]
    return status == "PASS", detail


def list_slots(binary, server):
    """(ok, rows): ok is False on every error (no binary, timeout, non-zero rc, bad json).

    An unreadable slot table is never `no free slots`: callers must fail closed.
    """
    if not binary:
        return False, []
    try:
        res = subprocess.run([binary, "lease", "list", server, "--json"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False, []
    if res.returncode:
        return False, []
    try:
        rows = json.loads(res.stdout or "[]")
    except ValueError:
        return False, []
    if not isinstance(rows, list):
        return False, []
    return True, rows


def port_open(port):
    if not port:
        return True
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.3):
            return True
    except OSError:
        return False


def pick_slots(rows, n, port_open=port_open):
    """Free, non-reserved, listener-free slot rows, nearest id first; at most n."""
    out = []
    for row in rows:
        if row.get("state") != "free":
            continue
        slot = row.get("slot") or {}
        sid = str(slot.get("id", ""))
        if not sid:
            continue
        if sid in NEVER_SLOTS or slot.get("reserved"):
            continue
        if port_open(slot.get("port")):
            continue
        out.append(row)
    out.sort(key=lambda r: int((r.get("slot") or {}).get("id", 0)))
    return out[:max(0, int(n))]


def pick_free(binary, server, n):
    ok, rows = list_slots(binary, server)
    return pick_slots(rows, n) if ok else []


def holder(stamp, index=0):
    return "tortoise-trials-%s-%d" % (stamp, index)


def acquire(binary, server, slot, holder_name, ttl=TTL):
    argv = [binary, "lease", "acquire", server, "--slot", str(slot), "--holder", holder_name, "--ttl", str(ttl)]
    res = run_cmd(argv)
    out = res.get("out", "")
    m = re.search(r"lease\s+([0-9A-Za-z_.-]+)\s*$", out, re.M)
    account = re.search(r"account ([^,\s]+)", out)
    character = re.search(r"character ([^,\s]+)", out)
    port = re.search(r"port (\d+)", out)
    return {"ok": res["rc"] == 0, "lease_id": m.group(1) if m else "",
            "account": account.group(1) if account else "",
            "character": character.group(1) if character else "",
            "port": int(port.group(1)) if port else 0,
            "raw": out, "rc": res["rc"]}


def renew(binary, server, slot, holder_name, lease_id, ttl=TTL):
    argv = [binary, "lease", "renew", server, str(slot), "--holder", holder_name,
            "--lease-id", str(lease_id), "--ttl", str(ttl)]
    return run_cmd(argv)["rc"] == 0


def release(binary, server, slot, holder_name, lease_id):
    argv = [binary, "lease", "release", server, str(slot), "--holder", holder_name]
    if lease_id:
        argv += ["--lease-id", str(lease_id)]
    return run_cmd(argv)["rc"] == 0


def run_cmd(argv, timeout=60):
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"rc": 15, "out": str(exc)}
    return {"rc": res.returncode, "out": (res.stdout or "") + (res.stderr or "")}


class LeaseKeeper(threading.Thread):
    """Renews one lease every ttl/3 (30 s for the default 90 s lease).

    A failed renewal stops the keeper and calls `on_lost` once: the run must end
    the trial at once rather than hold a lease the server may have dropped.
    """

    def __init__(self, binary, server, slot, holder_name, lease_id, log=None, ttl=TTL,
                 interval=None, renew_fn=None, on_lost=None):
        threading.Thread.__init__(self, daemon=True)
        self.args = (binary, server, slot, holder_name, lease_id)
        self.log = log or (lambda line: None)
        self.ttl = max(0.01, float(ttl))
        self.interval = max(0.001, float(interval if interval is not None else self.ttl / 3.0))
        self.renew_fn = renew_fn or renew
        self.on_lost = on_lost or (lambda reason: None)
        self.failed = False
        self.lost_reason = ""
        self.renewals = 0
        self.stopped = threading.Event()

    def run(self):
        while not self.stopped.wait(self.interval):
            try:
                ok = bool(self.renew_fn(*self.args))
            except Exception as exc:  # noqa: BLE001 - a broken renew is a lost lease
                ok = False
                detail = str(exc)
            else:
                detail = ""
            self.log("lease renew %s/%s: %s" % (self.args[1], self.args[2], "ok" if ok else "FAILED"))
            if not ok:
                self.failed = True
                self.lost_reason = "lease lost%s" % ((": " + detail[:120]) if detail else "")
                self.stopped.set()
                try:
                    self.on_lost(self.lost_reason)
                except Exception:  # noqa: BLE001 - a bad callback must not keep the keeper alive
                    pass
                return
            self.renewals += 1

    def stop(self):
        self.stopped.set()
