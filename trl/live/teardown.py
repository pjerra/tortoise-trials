"""Repeat, trial and run teardown, and `trials cleanup <run-dir>`.

Per repeat: clear the forced role, kill every alive ledger summon and verify
with unit_must, `.unaura all` on self, wait out of combat, heal self and hire
full. A summons status that cannot be read is UNKNOWN, never "none alive".
Per trial: `.bot remove` the hire and every extra_bots hire.
Per run: `turtle-agent teardown <slot-dir>` (logout, `.gm off`, lease release);
exit 14 is reported and the run carries on.

`trials cleanup` takes a FRESH lease on each run slot before any login (it
never logs into a slot someone else holds), logs in as the character that made
the summons (the ledger's account/character/arena), returns to the arena, kills
that slot's own ledger summons by guid, verifies each with unit_must, clears the
forced role, removes the extra_bots hires the ledger never saw removed, then
releases the fresh lease. It is idempotent and returns
non-zero while anything is left.
"""

import os
import re
import subprocess
import tempfile
import time
from datetime import datetime

from .agent import RpcError, UnitGone, norm_guid, wait_until
from .gm_guard import GmContext, GmGuard
from .ledger import Ledger
from .situations import summon_states


def _row(cid, ok, value=None, evidence=""):
    return {"id": cid, "ok": bool(ok), "value": value, "evidence": evidence}


def _kill_error(exc):
    return str(exc)[:120]


def repeat_cleanup(agent, guard, ledger, bot_name, player_guid, wait_s=20.0, extra_names=(),
                   only=None, absent_is_gone=False):
    """Per-repeat cleanup rows.

    `only` limits the summon check and the kills to this repeat's own mobs (the slot ledger also
    holds every earlier repeat's, and each of those was already verified by its own cleanup).
    Without it an earlier repeat's despawned corpse answers "unit ... is not in view", which read
    as UNKNOWN for the whole ledger: mobs_dead failed, nothing was killed, one summon stayed alive.
    `absent_is_gone` is for the caller that stands in the arena (see situations.summon_states): True, or
    the guids it holds for (this repeat's own mobs; an earlier repeat's leftover may sit in another arena
    and stays UNKNOWN when it is not in view).
    """
    rows = []
    try:
        agent.tbm("role self clear")
    except Exception:  # noqa: BLE001 - the agent may already be gone
        pass
    try:
        agent.rpc("stop_attack", {})
    except Exception:  # noqa: BLE001
        pass
    ids = [norm_guid(g) for g in ledger.summon_guids() if g]
    if only is not None:
        want = {norm_guid(g) for g in only if g}
        ids = [g for g in ids if g in want]
    unknown = ""
    states = None
    try:
        states = summon_states(agent, ledger, only=only, absent_is_gone=absent_is_gone, unknown_ok=True)
    except RpcError as exc:
        # an RPC error is UNKNOWN, not "none alive"
        unknown = str(exc)[:160]
    had_alive = False
    killed = []
    kill_errors = []
    for _attempt in range(4 if states is not None else 0):
        if str(getattr(guard, "abort", lambda: "")() or ""):
            break
        pending = [g for g, state in (states or {}).items() if state == "alive"]
        if not pending:
            break
        had_alive = True
        for guid in pending:
            try:
                guard.kill(guid)
                killed.append(guid)
            except Exception as exc:  # noqa: BLE001 - recorded, the retry loop decides
                kill_errors.append(_kill_error(exc))
        time.sleep(1.5)
        try:
            states = summon_states(agent, ledger, only=only, absent_is_gone=absent_is_gone, unknown_ok=True)
        except RpcError as exc:
            states = None
            unknown = str(exc)[:160]
            break
    if unknown:
        rows.append(_row("mobs_dead", False, None,
                         "summons status UNKNOWN (RPC error): %s" % unknown))
        ledger.add({"kind": "mob_leftover", "guids": ids, "reason": "unknown: %s" % unknown})
    else:
        still = [g for g, state in (states or {}).items() if state == "alive"]
        # an earlier repeat's leftover that is not in view (the player may stand in another arena) is
        # UNKNOWN, never gone; it does not stop the other summons being checked and killed
        not_proved = [g for g, state in (states or {}).items() if state == "unknown"]
        evidence = "killed %d, still alive: %s" % (len(killed), ", ".join(still) or "none")
        if not_proved:
            evidence += "; UNKNOWN (not in view, not proved dead): %s" % ", ".join(not_proved)
        if kill_errors:
            evidence += "; errors: " + " | ".join(kill_errors[-2:])
        rows.append(_row("mobs_dead", not (still or not_proved), killed,
                         evidence if (still or not_proved or kill_errors or had_alive)
                         else "every ledger summon confirmed dead or gone"))
        # per guid, so the runner stops re-checking exactly the summons that are proved dead or gone
        rows[-1]["verified"] = sorted(g for g, state in (states or {}).items() if state in ("dead", "gone"))
        if still:
            ledger.add({"kind": "mob_leftover", "guids": still, "reason": "still alive"})
        if not_proved:
            ledger.add({"kind": "mob_leftover", "guids": not_proved, "reason": "unknown: not in view"})
    try:
        guard.send("unaura", player_guid, "all")
        rows.append(_row("player_unaura", True, "all", ".unaura all on self"))
    except Exception as exc:  # noqa: BLE001
        rows.append(_row("player_unaura", False, None, str(exc)))
    try:
        wait_until(lambda: not any(u.get("in_combat") for u in agent.units(within=40)),
                   timeout=wait_s, poll=1.0)
    except Exception:  # noqa: BLE001
        pass
    try:
        own = agent.unit(guid=player_guid) or {}
        maxhp = int(own.get("max_health") or 0)
        if maxhp:
            guard.send("modify_hp", player_guid, maxhp, maxhp)
        hire = agent.unit(name=bot_name) if bot_name else None
        if hire:
            hire_max = int(hire.get("max_health") or 0)
            if hire_max:
                guard.send("modify_hp", hire.get("guid"), hire_max, hire_max)
        for extra_name in extra_names or ():
            extra_unit = agent.unit(name=extra_name) if extra_name else None
            extra_max = int((extra_unit or {}).get("max_health") or 0)
            if extra_max:
                guard.send("modify_hp", extra_unit.get("guid"), extra_max, extra_max)
        rows.append(_row("healed_full", True, maxhp, ".modify hp full on self and hire"))
    except Exception as exc:  # noqa: BLE001
        rows.append(_row("healed_full", False, None, str(exc)))
    return rows


def remove_hire(agent, ledger, name, check_id="hire_removed", timeout=15.0, extra=None):
    """`.bot remove <name>`; one row. The ledger gets a hire_removed row (extra index when set)."""
    rows = []
    try:
        since = agent.tbm("remove %s" % name)
        line, _rows = agent.wait_reply(
            since, r"Removal requested|Hire ended|not found|not removable", timeout=timeout)
        entry = {"kind": "hire_removed", "name": name, "reply": line or ""}
        if extra:
            # an unanswered removal is recorded but does not count as removed: cleanup retries it
            entry["extra"] = int(extra)
            entry["ok"] = bool(line)
        ledger.add(entry)
        rows.append(_row(check_id, bool(line), line, line or "no remove reply"))
    except Exception as exc:  # noqa: BLE001
        rows.append(_row(check_id, False, None, str(exc)))
    return rows


def trial_cleanup(agent, ledger, bot_name, timeout=15.0, extras=()):
    rows = []
    try:
        since = agent.tbm("role self clear")
        line, _rows = agent.wait_reply(since, r"Your role set to", timeout=timeout)
        ledger.add({"kind": "role", "role": "clear", "reply": line or "", "phase": "trial_end"})
        rows.append(_row("forced_role_clear", bool(line), line, line or "no reply"))
    except Exception as exc:  # noqa: BLE001
        rows.append(_row("forced_role_clear", False, None, str(exc)))
    if bot_name:
        try:
            since = agent.tbm("remove %s" % bot_name)
            line, _rows = agent.wait_reply(
                since, r"Removal requested|Hire ended|not found|not removable", timeout=timeout)
            ledger.add({"kind": "hire_removed", "name": bot_name, "reply": line or ""})
            rows.append(_row("hire_removed", bool(line), line, line or "no remove reply"))
        except Exception as exc:  # noqa: BLE001
            rows.append(_row("hire_removed", False, None, str(exc)))
    # every extra_bots hire goes too (the extras' spec roles die with the hire)
    for index, name in enumerate(extras or (), start=1):
        if name:
            rows.extend(remove_hire(agent, ledger, name, "hire_removed_extra%d" % index,
                                    timeout=timeout, extra=index))
    return rows


def pending_extra_hires(ledger):
    """Extra hires the ledger never saw removed: [(name, extra index)], oldest first, one per name."""
    last_hire, last_removed = {}, {}
    for pos, rec in enumerate(ledger.rows):
        name = rec.get("name")
        if not name:
            continue
        if rec.get("kind") == "hire" and rec.get("extra"):
            last_hire[name] = (pos, int(rec.get("extra")))
        elif rec.get("kind") == "hire_removed" and rec.get("ok", True):
            last_removed[name] = pos
    return sorted(((n, idx) for n, (pos, idx) in last_hire.items() if last_removed.get(n, -1) < pos),
                  key=lambda x: last_hire[x[0]][0])


_FAILED_STEP = re.compile(r"^(?:FAIL|ending)\s+(\S+)\s*(.*)$")


def failed_steps(output):
    """["step: detail", ...] for every step line `turtle-agent teardown` marks FAIL or ending.

    The text report prints one line per step (`FAIL lease  kept: game-side teardown failed ...`)
    and a summary line (`teardown: 5 ok, 9 skipped, 2 failed`); the summary alone says how many
    steps failed, never which.
    """
    out = []
    for line in (output or "").splitlines():
        m = _FAILED_STEP.match(line.strip())
        if m:
            step, detail = m.group(1), m.group(2).strip()
            out.append("%s: %s" % (step, detail[:160]) if detail else step)
    return out


def slot_teardown(slot_dir, binary=None, timeout=180):
    binary = binary or os.path.join(slot_dir, "turtle-agent")
    try:
        res = subprocess.run([binary, "teardown", slot_dir], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "rc": None, "detail": str(exc)}
    lines = (res.stdout + res.stderr).strip().splitlines()
    detail = lines[-1] if lines else ""
    failed = failed_steps(res.stdout)
    if failed:
        detail += "; failed steps: " + " | ".join(failed)
    return {"ok": res.returncode == 0, "rc": res.returncode, "detail": detail,
            "failed_steps": failed, "teardown_failed": res.returncode == 14}


# -- `trials cleanup` ------------------------------------------------------

class _CleanupRun:
    """The little of a Run that SlotSession reads (server, warnings, character notes)."""

    def __init__(self, server, log):
        self.server = server
        self.warnings = []
        self.log = log
        # the cleanup login counts against realmd's limit like any other
        from .pacing import LoginPacer
        from .pacing import DEFAULT_STATE
        self.pacer = LoginPacer(log=log, state_path=DEFAULT_STATE)

    def character_note(self, *args, **kwargs):
        pass


def _stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _ok_rows(result):
    """list_slots now answers (ok, rows); older patched callers may still give a list."""
    if isinstance(result, tuple):
        return result
    return True, list(result or [])


def _slot_row(server, slot_id, binary):
    from . import lease as lease_mod
    ok, rows = _ok_rows(lease_mod.list_slots(binary, server) if binary else (False, []))
    if ok:
        for row in rows:
            if str((row.get("slot") or {}).get("id")) == str(slot_id):
                return row
    return {"server": server, "slot": {"id": str(slot_id)}, "state": "unknown"}


def _slot_free(server, slot_id, binary):
    """True ONLY for a successfully parsed row for that slot whose state is free."""
    from . import lease as lease_mod
    ok, rows = _ok_rows(lease_mod.list_slots(binary, server) if binary else (False, []))
    if not ok:
        return False
    for row in rows:
        if str((row.get("slot") or {}).get("id")) == str(slot_id):
            return row.get("state") == "free"
    return False


ARENA_NEAR = 60.0     # yards: the agent lists units within 60, a summon made there is in view


def _at_arena(agent, arena, log=None):
    """True when the logged-in character stands within ARENA_NEAR yd of the ledger arena."""
    if not arena or arena.get("x") is None or arena.get("y") is None:
        return False
    try:
        st = agent.status()
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(st, dict) or st.get("x") is None or st.get("y") is None:
        return False
    dist = ((float(st["x"]) - float(arena["x"])) ** 2 + (float(st["y"]) - float(arena["y"])) ** 2) ** 0.5
    if log and dist > ARENA_NEAR:
        log("cleanup: the character is %.0f yd from the arena; a summon that is not in view stays UNKNOWN"
            % dist)
    return dist <= ARENA_NEAR


def _last_row(ledger, kind, server, slot):
    found = None
    for row in ledger.of_kind(kind):
        if str(row.get("server")) == str(server) and str(row.get("slot")) == str(slot):
            found = row
    return found


def _clean_dir(rec):
    """A slot dir slot setup may claim: an existing non-empty dir is never overwritten."""
    slot_dir = rec.get("dir") or ""
    if not slot_dir or not os.path.isdir(slot_dir):
        return slot_dir or tempfile.mkdtemp(prefix="trl-cleanup-")
    try:
        if os.listdir(slot_dir):
            return "%s-cleanup-%s" % (slot_dir, _stamp())
    except OSError:
        return "%s-cleanup-%s" % (slot_dir, _stamp())
    return slot_dir


def _fresh_login(rec, ledger, binary, log, login_factory=None):
    """(session) on the run's slot through the normal login path; None when impossible."""
    if login_factory is not None:
        return login_factory(rec, ledger)
    from . import run as run_mod
    slot_dir = _clean_dir(rec)
    if not binary or not slot_dir:
        return None
    row = _slot_row(rec.get("server"), rec.get("slot"), binary)
    session = run_mod.SlotSession(row, slot_dir, binary, rec.get("holder") or "",
                                  _CleanupRun(rec.get("server"), log), log)
    session.account = rec.get("account") or ""
    try:
        session.setup_slot()
        name = rec.get("character") or (row.get("slot") or {}).get("character")
        if not name:
            return None
        session.login(name)
    except Exception as exc:  # noqa: BLE001 - no login, no kills; the lease is still released
        log("cleanup: fresh login on %s/%s failed: %s" % (rec.get("server"), rec.get("slot"), exc))
        return None
    return session


def cleanup(run_dir, log=None, binary=None, login_factory=None):
    """Replay teardown from ledger.jsonl. Idempotent: dead summons and released leases are fine."""
    log = log or (lambda line: print(line))
    path = os.path.join(run_dir, "ledger.jsonl")
    if not os.path.isfile(path):
        log("cleanup: no ledger.jsonl in %s" % run_dir)
        return 2
    from . import lease as lease_mod
    ledger = Ledger(path)
    reports = []
    problems = []
    slots = []
    seen = set()
    for rec in ledger.of_kind("lease"):
        key = (rec.get("server"), str(rec.get("slot")), rec.get("dir"))
        if key in seen:
            continue
        seen.add(key)
        slots.append(rec)
    for rec in slots:
        server, sid = rec.get("server"), str(rec.get("slot"))
        slot_dir = rec.get("dir")
        if not binary and slot_dir:
            local = os.path.join(slot_dir, "turtle-agent")
            if os.path.isfile(local):
                binary = local
        if not binary:
            problems.append("no turtle-agent binary: cannot check or release %s/%s" % (server, sid))
            continue
        # M5: a fresh lease (a new holder) before ANY login; a held slot is never touched
        holder_name = "tortoise-trials-%s-clean" % _stamp()
        if not _slot_free(server, sid, binary):
            problems.append("cleanup refused: %s/%s is not free (held or unreadable); no login"
                            % (server, sid))
            log("cleanup REFUSED %s/%s: slot is not free; never logging into a held slot"
                % (server, sid))
            continue
        got = lease_mod.acquire(binary, server, sid, holder_name, ttl=lease_mod.TTL)
        if not got.get("ok") or not got.get("lease_id"):
            problems.append("cleanup refused: fresh lease on %s/%s failed: %s"
                            % (server, sid, str(got.get("raw", "")).strip()[-160:]))
            log("cleanup REFUSED %s/%s: fresh lease failed; no login" % (server, sid))
            continue
        fresh_lease = {"holder": holder_name, "lease_id": got["lease_id"]}
        # everything after the acquire is inside the try: a failure is one recorded
        # problem, and the finally hands back exactly the holder/lease id acquired here
        try:
            slot_ledger_path = rec.get("ledger") or os.path.join(run_dir, "%s-%s.jsonl" % (server, sid))
            slot_ledger = Ledger(slot_ledger_path) if os.path.isfile(slot_ledger_path) else ledger
            guids = [g for g in slot_ledger.summon_guids() if g]
            session_row = _last_row(ledger, "session", server, sid) or {}
            arena = _last_row(ledger, "arena", server, sid)
            character = session_row.get("character") or rec.get("character") or ""
            account = session_row.get("account") or rec.get("account") or ""
            log("cleanup: %s/%s account %s character %s arena %s (fresh lease %s)"
                % (server, sid, account or "?", character or "?", (arena or {}).get("id") or "-",
                   got["lease_id"]))
            session = None
            if guids or (slot_dir and os.path.isdir(slot_dir)):
                rec_in = dict(rec)
                rec_in["character"] = character
                rec_in["holder"] = holder_name
                session = _fresh_login(rec_in, slot_ledger, binary, log, login_factory)
            leftover = []
            kill_errors = []
            if session is not None:
                agent = session.agent
                try:
                    st = agent.status()
                except Exception:  # noqa: BLE001
                    st = {}
                ctx = GmContext(trial_id="cleanup", player_guid=norm_guid(st.get("guid") or ""))
                if arena:
                    ctx.arena_coords = frozenset({(int(arena.get("map", 0)), int(arena["x"]),
                                                   int(arena["y"]), int(round(float(arena.get("z", 0)))))}) \
                        if arena.get("x") is not None and arena.get("y") is not None else frozenset()
                guard = GmGuard(agent, slot_ledger, ctx)
                if arena and ctx.arena_coords:
                    try:
                        guard.send("go_xyz", None, int(arena["x"]), int(arena["y"]),
                                   int(arena["z"]), int(arena.get("map", 0)))
                    except Exception as exc:  # noqa: BLE001 - the character may already be there
                        log("cleanup: go_xyz to the ledger arena skipped: %s" % exc)
                for guid in guids:
                    try:
                        guard.kill(guid)
                    except (UnitGone, RpcError) as exc:
                        kill_errors.append((guid, _kill_error(exc)))
                    except Exception as exc:  # noqa: BLE001 - recorded as a leftover
                        kill_errors.append((guid, _kill_error(exc)))
                try:
                    # back at the arena a corpse that despawned ("unit ... is not in view") is
                    # gone, not UNKNOWN; anywhere else the agent's answer proves nothing
                    states = summon_states(agent, slot_ledger,
                                           absent_is_gone=_at_arena(agent, arena, log))
                    leftover = [g for g, state in states.items() if state == "alive"]
                except RpcError as exc:
                    leftover = [norm_guid(g) for g in guids]
                    log("cleanup: summons status unknown on %s/%s: %s" % (server, sid, exc))
                for guid, err in kill_errors:
                    slot_ledger.add({"kind": "mob_leftover", "guids": [norm_guid(guid)],
                                     "reason": "gm_kill error: %s" % err})
                    problems.append("gm_kill error on %s/%s %s: %s" % (server, sid, guid, err))
                try:
                    agent.tbm("role self clear")
                except Exception:  # noqa: BLE001
                    pass
                # replay the extra_bots hires the ledger never saw removed
                for name, index in pending_extra_hires(slot_ledger):
                    for row in remove_hire(agent, slot_ledger, name, "hire_removed_extra%d" % index,
                                           extra=index):
                        if row["ok"]:
                            log("cleanup: removed extra hire %s on %s/%s" % (name, server, sid))
                        else:
                            problems.append("extra hire %s left on %s/%s: %s"
                                            % (name, server, sid, row["evidence"]))
                            slot_ledger.add({"kind": "leftover", "check": row["id"],
                                             "evidence": row["evidence"], "name": name})
                try:
                    session.stop_agent()
                except Exception:  # noqa: BLE001
                    pass
                reports.append({"slot": slot_dir, "killed": guids, "leftover": leftover})
                if leftover:
                    problems.append("summons left on %s/%s: %s" % (server, sid, ", ".join(leftover)))
                    slot_ledger.add({"kind": "mob_leftover", "guids": leftover, "reason": "cleanup"})
            elif pending_extra_hires(slot_ledger):
                left = pending_extra_hires(slot_ledger)
                problems.append("no agent to remove %d extra hire(s) on %s/%s: %s"
                                % (len(left), server, sid, ", ".join(n for n, _i in left)))
                if guids:
                    problems.append("no agent to kill %d summon(s) on %s/%s" % (len(guids), server, sid))
            elif guids:
                problems.append("no agent to kill %d summon(s) on %s/%s" % (len(guids), server, sid))
                log("cleanup: no live agent for %s/%s; %d ledger summon(s) unknown"
                    % (server, sid, len(guids)))
            if slot_dir and os.path.isdir(slot_dir):
                out = slot_teardown(slot_dir, binary)
                log("cleanup: teardown %s: rc=%s %s"
                    % (slot_dir, out.get("rc"), out.get("detail", "")))
                reports.append({"slot": slot_dir, **out})
            clean_dir = getattr(session, "slot_dir", "") if session is not None else ""
            if clean_dir and clean_dir != slot_dir and os.path.isdir(clean_dir):
                # a fresh dir slot setup had to claim: remove its secrets after the logout
                out = slot_teardown(session.slot_dir, binary)
                log("cleanup: teardown %s: rc=%s %s"
                    % (session.slot_dir, out.get("rc"), out.get("detail", "")))
                reports.append({"slot": session.slot_dir, **out})
            if not slot_dir:
                log("cleanup: slot dir gone for %s/%s; only the fresh lease was touched"
                    % (server, sid))
        except Exception as exc:  # noqa: BLE001 - one slot's failure must not skip the rest
            problems.append("cleanup failed on %s/%s: %s" % (server, sid, _kill_error(exc)))
            log("cleanup FAILED %s/%s: %s" % (server, sid, exc))
        finally:
            # the fresh lease goes back last, whatever happened above, with the exact
            # holder and lease id that were acquired
            try:
                released = lease_mod.release(binary, server, sid, fresh_lease["holder"],
                                             fresh_lease["lease_id"])
            except Exception as exc:  # noqa: BLE001
                released = False
                log("cleanup: fresh lease release %s/%s raised: %s" % (server, sid, exc))
            if not released and _slot_free(server, sid, binary):
                # The clean slot dir holds the FRESH lease (slot setup adopts it) and its
                # `turtle-agent teardown` already gave it back, so this second release fails
                # on a lease that is no longer held (live2: FAILED on every cleanup while
                # `lease list` showed the slot free). The slot table is the word on it.
                released = True
                log("cleanup: fresh lease %s/%s was already released by the slot teardown (slot free)"
                    % (server, sid))
            log("cleanup: fresh lease release %s/%s: %s"
                % (server, sid, "ok" if released else "FAILED"))
            if not released:
                problems.append("fresh lease still held on %s/%s" % (server, sid))
    if not slots:
        log("cleanup: no lease rows in the ledger")
    for problem in problems:
        log("cleanup LEFT: %s" % problem)
    return 1 if problems else 0
