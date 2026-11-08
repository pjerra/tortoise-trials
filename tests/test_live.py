"""Tests for the Tortoise Trials live runner (trl/live).

Failing-first case: test_never_slot_3 and every other test here failed with
ModuleNotFoundError before trl/live existed; test_never_slot_3 is the safety
case the whole runner hangs on (slot 3 is the default reserved slot and
must never be taken, even when the slot table lists it free).

No server is contacted: FakeAgent records the RPC calls and returns canned rows.
"""

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trl.live import agent as agent_mod  # noqa: E402
from trl.live import collect, gm_guard, lease, run, situations, setup, teardown  # noqa: E402
from trl.live.ledger import Ledger  # noqa: E402

PLAYER = "0x0000000000000001"
HIRE = "0x0000000000000002"
MOB = "0xF13000000000ABCD"
MOB_DEAD = "0xF13000000000ABCE"
TRIAL = str(ROOT / "trials" / "_fixtures" / "smoke-priest-tank.json")


class FakeMarks:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(dict(row))

    def excluded(self, start, end, why):
        self.add({"kind": "excluded", "from": start, "to": end, "why": why})

    def kinds(self, kind):
        return [r for r in self.rows if r.get("kind") == kind]


class FakeAgent(agent_mod.Agent):
    """Records RPC calls; returns canned rows. Never starts a subprocess."""

    def __init__(self, status=None, units=None, spells=(), party=None, unit_rows=None,
                 events=None, tt_reply=None, status_seq=None):
        agent_mod.Agent.__init__(self, "/nonexistent-slot")
        self.status_row = status or {"state": "in_world", "guid": PLAYER, "level": 35,
                                     "health": 1000, "max_health": 1000, "power": 500,
                                     "max_power": 500, "team": "alliance", "target": PLAYER}
        self.status_seq = list(status_seq or [])
        self.unit_list = list(units or [])
        self.spells_known = set(spells)
        self.party_row = party or {"in_group": True, "members": [{"name": "Helper", "guid": HIRE, "level": 35}]}
        self.unit_rows = dict(unit_rows or {})
        self.event_rows = list(events or [])
        self.tt_reply = tt_reply or {"replies": [{"ok": True}]}
        self.mark_row = {"icon": "moon", "guid": ""}      # the agent's real MarkResult
        self.calls = []
        self.gm = []
        self.gm_at = []
        self.kills = []
        self.tbm_calls = []
        self.casts = []
        self.attacks = 0
        self.targets = []
        self.units_queue = []
        self.units_raise = False
        self.units_err = False
        self.gm_err = False
        self.kill_raise = False
        self.ignore_select = False
        self.cursor = 0

    # --- the one override that matters: like the real Agent, an unknown method is an error ---
    def rpc(self, method, params=None, timeout=None):
        self.calls.append((method, params))
        if method == "status":
            if self.status_seq:
                row = self.status_seq.pop(0) if len(self.status_seq) > 1 else self.status_seq[0]
                if isinstance(row, dict):
                    self.status_row = dict(row)
            return dict(self.status_row)
        if method == "units":
            if self.units_raise:
                raise agent_mod.RpcError("units: rpc failed")
            if self.units_err:
                return {"_err": "units: rpc failed"}
            if self.units_queue:
                return list(self.units_queue.pop(0))
            return list(self.unit_list)
        if method == "unit":
            return self._unit(params or {})
        if method == "spells":
            return [{"spell_id": i} for i in sorted(self.spells_known)]
        if method == "party":
            return dict(self.party_row)
        if method == "gm":
            if self.gm_err:
                return {"_err": "game refusal"}
            self.gm.append(params["text"])
            self.gm_at.append((params["text"],
                               agent_mod.norm_guid(self.status_row.get("target") or "")))
            return {}
        if method == "gm_kill":
            if self.kill_raise:
                raise agent_mod.RpcError("gm_kill: rpc failed")
            self.kills.append(params["guid"])
            for row in self.unit_list:
                if agent_mod.norm_guid(row.get("guid")) == agent_mod.norm_guid(params["guid"]):
                    row["dead"] = True
            return {}
        if method == "target":
            self.targets.append(params.get("guid"))
            time.sleep(0.005)
            if not self.ignore_select:
                self.status_row["target"] = params.get("guid")
            return {"guid": params.get("guid")}
        if method == "cast":
            self.casts.append(params)
            return {}
        if method == "chat":
            return {}
        if method == "mark":
            return dict(self.mark_row)
        if method == "stop_attack":
            return {}
        if method == "tbm":
            self.tbm_calls.append(params["command"])
            return {"since": self.cursor}
        if method == "events":
            params = params or {}
            kinds = params.get("kinds")
            rows = [r for r in self.event_rows if not kinds or r.get("kind") in kinds]
            self.cursor = len(rows)
            return {"events": rows, "next": self.cursor, "oldest": 0, "dropped": False}
        if method == "tt":
            return dict(self.tt_reply)
        if method == "attack":
            self.attacks += 1
            return {}
        # the real Agent answers {"_err": ...} for a failed rpc; tests use this path
        return {"_err": "unknown method: %s" % method}

    def _unit(self, params):
        out = self._unit_row(params)
        if params.get("guid") is not None and agent_mod.norm_guid(params.get("guid")) == agent_mod.norm_guid(PLAYER):
            # the player's own object always exists and shows the selection the (instant) fake
            # server holds; the guard fails closed without it
            if not isinstance(out, dict) or "_err" in out:
                out = {"guid": PLAYER}
            out.setdefault("target", self.status_row.get("target") or "")
        return out

    def _unit_row(self, params):
        guid = params.get("guid")
        name = params.get("name")
        if guid in self.unit_rows:
            return dict(self.unit_rows[guid])
        for row in self.unit_list:
            if guid is not None and agent_mod.norm_guid(row.get("guid")) == agent_mod.norm_guid(guid):
                return dict(row)
            if guid is None and name is not None and (row.get("name") or "").lower() == str(name).lower():
                return dict(row)
        return {"_err": "no unit"}

    def tbm(self, command, via=None):
        return int(self.rpc("tbm", {"command": command}).get("since", 0))


class StubCollector:
    """The bits of collect.Collector the situation loop reads, without threads."""

    def __init__(self, *args, **kwargs):
        self.tick_hooks = []
        self.last_sample = None
        self.lost = ""
        self.on_lost = None
        self.guids = {}
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def mob_casts(self, guids):
        return None


def ctx_for(**over):
    base = dict(
        player_guid=PLAYER,
        band_low=40,
        floor_pct=30,
        hire_name="Helper",
        hire_guid=HIRE,
        arena_coords=frozenset({(0, -12790, -400, 18)}),
        mob_entries=frozenset({1234}),
        learn_ids=frozenset({12285, 12295}),
        aura_ids=frozenset({12345}),
        item_ids=frozenset({8766}),
        trial_id="t",
    )
    base.update(over)
    return gm_guard.GmContext(**base)


def new_ledger(tmp):
    return Ledger(os.path.join(tmp, "ledger.jsonl"))


def run_args(tmp, server="gamma"):
    return run.Args(server=server, trials=[TRIAL], label="t", repeats=1, parallel=1, out=tmp,
                    bind=False, dry_run=False, binary=None)


def load_trial():
    with open(TRIAL) as f:
        return json.load(f)


class LeaseTest(unittest.TestCase):
    def test_never_slot_3(self):
        self.assertEqual(lease.pick_slots([
            {"server": "s", "slot": {"id": "3", "character": "C", "port": 1113}, "state": "free"},
        ], 1, port_open=lambda p: False), [])

        with tempfile.TemporaryDirectory() as tmp:
            args = run_args(tmp, server="gamma")
            rows = [
                {"server": "s", "slot": {"id": "1", "character": "A", "port": 1111}, "state": "held"},
                {"server": "s", "slot": {"id": "2", "character": "B", "port": 1112}, "state": "held"},
                {"server": "s", "slot": {"id": "3", "character": "C", "port": 1113}, "state": "free"},
            ]
            with unittest.mock.patch.object(run.lease, "place_allowed", lambda s, b=None: (True, "alpha, gamma")), \
                 unittest.mock.patch.object(run.lease, "list_slots", lambda b, s: (True, rows)):
                self.assertEqual(run.main(args), 4)

    def test_server_not_allowed_exits_4(self):
        with tempfile.TemporaryDirectory() as tmp:
            bindir = pathlib.Path(tmp) / "bin"
            bindir.mkdir()
            fake = bindir / "turtle-agent"
            fake.write_text("#!/bin/sh\necho 'raid-walk: test servers only (alpha, beta)' >&2\nexit 1\n")
            fake.chmod(0o755)
            ok, detail = lease.place_allowed("alpha", str(fake))
            self.assertTrue(ok)
            self.assertIn("alpha", detail)
            ok, detail = lease.place_allowed("gamma", str(fake))
            self.assertFalse(ok)
            self.assertIn("alpha, beta", detail)

            args = run_args(tmp, server="gamma")
            with unittest.mock.patch.object(run.lease, "place_allowed", lambda s, b=None: (False, "alpha, beta")):
                self.assertEqual(run.main(args), 4)

    def test_renewal_interval_is_a_third_of_the_ttl(self):
        keeper = lease.LeaseKeeper("b", "s", 1, "h", "L", ttl=90)
        self.assertEqual(keeper.interval, 30.0)

    def test_short_ttl_renews_several_times(self):
        calls = []

        def renew(*args):
            calls.append(args)
            return True

        keeper = lease.LeaseKeeper("b", "s", 1, "h", "L", ttl=0.06, renew_fn=renew)
        self.assertAlmostEqual(keeper.interval, 0.02)
        keeper.start()
        time.sleep(0.15)
        keeper.stop()
        keeper.join(timeout=1)
        self.assertGreaterEqual(len(calls), 3)
        self.assertFalse(keeper.failed)

    def test_failed_renewal_stops_the_trial_at_once(self):
        calls = []
        lost = []

        def renew(*args):
            calls.append(args)
            return False

        keeper = lease.LeaseKeeper("b", "s", 1, "h", "L", ttl=0.09, renew_fn=renew,
                                   on_lost=lost.append)
        keeper.start()
        keeper.join(timeout=2)
        self.assertTrue(keeper.stopped.is_set())
        self.assertTrue(keeper.failed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(lost, ["lease lost"])
        time.sleep(0.1)
        self.assertEqual(len(calls), 1)

    def test_partial_setup_releases_the_first_lease(self):
        released = []

        def fake_release(binary, server, slot, holder, lease_id):
            released.append((str(slot), str(lease_id)))
            return True

        def fake_setup(session):
            if session.slot_id == "2":
                raise run.SetupInvalid("slot setup 2 failed")

        class KeeperStub:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

            def stop(self):
                pass

        rows = [{"server": "s", "slot": {"id": "1", "character": "A", "port": 0}, "state": "free"},
                {"server": "s", "slot": {"id": "2", "character": "B", "port": 0}, "state": "free"}]
        got = iter([{"ok": True, "lease_id": "L1", "raw": ""},
                    {"ok": True, "lease_id": "L2", "raw": ""}])
        with tempfile.TemporaryDirectory() as tmp:
            args = run_args(tmp)
            args.parallel = 2
            with unittest.mock.patch.object(run.lease, "place_allowed", lambda s, b=None: (True, "alpha")), \
                 unittest.mock.patch.object(run.lease, "pick_free", lambda b, s, n: rows), \
                 unittest.mock.patch.object(run.lease, "acquire", lambda *a, **k: next(got)), \
                 unittest.mock.patch.object(run.lease, "release", fake_release), \
                 unittest.mock.patch.object(run.lease, "LeaseKeeper", KeeperStub), \
                 unittest.mock.patch.object(run.SlotSession, "setup_slot", fake_setup), \
                 unittest.mock.patch.object(run.teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": False, "rc": 1, "detail": "down"}):
                self.assertEqual(run.main(args), 4)
        self.assertIn(("1", "L1"), released)
        self.assertIn(("2", "L2"), released)

    def test_list_slots_errors_are_never_ok(self):
        class Res:
            def __init__(self, rc, out):
                self.returncode = rc
                self.stdout = out
                self.stderr = ""

        self.assertEqual(lease.list_slots("", "s"), (False, []))
        with unittest.mock.patch.object(lease.subprocess, "run", lambda *a, **k: Res(0, "[]")):
            self.assertEqual(lease.list_slots("/bin/true", "s"), (True, []))
        with unittest.mock.patch.object(lease.subprocess, "run", lambda *a, **k: Res(1, "[]")):
            self.assertEqual(lease.list_slots("/bin/true", "s"), (False, []))
        with unittest.mock.patch.object(lease.subprocess, "run", lambda *a, **k: Res(0, "not json")):
            self.assertEqual(lease.list_slots("/bin/true", "s"), (False, []))
        with unittest.mock.patch.object(lease.subprocess, "run", lambda *a, **k: Res(0, '{"slot": 1}')):
            self.assertEqual(lease.list_slots("/bin/true", "s"), (False, []))
        with unittest.mock.patch.object(lease.subprocess, "run",
                                        side_effect=subprocess.TimeoutExpired("x", 1)):
            self.assertEqual(lease.list_slots("/bin/true", "s"), (False, []))

    def test_slot_free_needs_a_parsed_free_row_for_that_slot(self):
        free = [{"server": "s", "slot": {"id": "1"}, "state": "free"}]
        held = [{"server": "s", "slot": {"id": "1"}, "state": "held by walker"}]
        other = [{"server": "s", "slot": {"id": "2"}, "state": "free"}]
        for rows, want in ((free, True), (held, False), (other, False), ([], False)):
            with unittest.mock.patch.object(lease, "list_slots", lambda b, s, rows=rows: (True, rows)):
                self.assertEqual(teardown._slot_free("s", "1", "/bin/true"), want, rows)
        with unittest.mock.patch.object(lease, "list_slots", lambda b, s: (False, [])):
            self.assertFalse(teardown._slot_free("s", "1", "/bin/true"))

    def test_acquire_records_account_character_and_port(self):
        out = ("leased s/1 to h until 12:00: account ACC, character Trlwarriora, "
               "port 1234, lease L7\n")
        with unittest.mock.patch.object(lease.subprocess, "run",
                                        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": out,
                                                                      "stderr": ""})()):
            got = lease.acquire("/bin/true", "s", "1", "h")
        self.assertEqual(got["lease_id"], "L7")
        self.assertEqual(got["account"], "ACC")
        self.assertEqual(got["character"], "Trlwarriora")
        self.assertEqual(got["port"], 1234)

    def test_keeper_starts_before_setup_and_stops_after_teardown(self):
        events = []

        class Keeper:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                events.append("keeper.start")

            def stop(self):
                events.append("keeper.stop")

        rows = [{"server": "s", "slot": {"id": "1", "character": "A", "port": 0}, "state": "free"}]
        with tempfile.TemporaryDirectory() as tmp:
            args = run_args(tmp)
            args.parallel = 1
            with unittest.mock.patch.object(run.lease, "place_allowed", lambda s, b=None: (True, "alpha")), \
                 unittest.mock.patch.object(run.lease, "pick_free", lambda b, s, n: rows), \
                 unittest.mock.patch.object(run.lease, "acquire",
                                            lambda *a, **k: {"ok": True, "lease_id": "L1",
                                                             "account": "ACC", "character": "A",
                                                             "port": 0, "raw": ""}), \
                 unittest.mock.patch.object(run.lease, "LeaseKeeper", Keeper), \
                 unittest.mock.patch.object(run.SlotSession, "setup_slot",
                                            lambda self: events.append("setup")), \
                 unittest.mock.patch.object(run.teardown, "slot_teardown",
                                            lambda d, binary=None: (events.append("teardown")
                                                                    or {"ok": True, "rc": 0, "detail": ""})):
                r = run.Run(args, "", tmp, rows)
                self.assertTrue(r.prepare_slots([]))
                r.teardown_sessions()
        self.assertEqual(events, ["keeper.start", "setup", "teardown", "keeper.stop"])

    def test_a_long_setup_with_a_short_ttl_keeps_the_lease(self):
        renewals = []
        real = lease.LeaseKeeper

        def factory(*args, **kwargs):
            kwargs["ttl"] = 0.04
            kwargs["interval"] = 0.01
            kwargs["renew_fn"] = lambda *a: (renewals.append(a) or True)
            return real(*args, **kwargs)

        rows = [{"server": "s", "slot": {"id": "1", "character": "A", "port": 0}, "state": "free"}]
        with tempfile.TemporaryDirectory() as tmp:
            args = run_args(tmp)
            with unittest.mock.patch.object(run.lease, "place_allowed", lambda s, b=None: (True, "alpha")), \
                 unittest.mock.patch.object(run.lease, "pick_free", lambda b, s, n: rows), \
                 unittest.mock.patch.object(run.lease, "acquire",
                                            lambda *a, **k: {"ok": True, "lease_id": "L1",
                                                             "account": "ACC", "character": "A",
                                                             "port": 0, "raw": ""}), \
                 unittest.mock.patch.object(run.lease, "LeaseKeeper", factory), \
                 unittest.mock.patch.object(run.SlotSession, "setup_slot",
                                            lambda self: time.sleep(0.12)), \
                 unittest.mock.patch.object(run.lease, "release", lambda *a, **k: True), \
                 unittest.mock.patch.object(run.teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                r = run.Run(args, "", tmp, rows)
                self.assertTrue(r.prepare_slots([]))
                keeper = r.sessions[0].keeper
                self.assertGreaterEqual(len(renewals), 2)
                self.assertFalse(keeper.failed)
                self.assertFalse(keeper.stopped.is_set())
                r.teardown_sessions()
                self.assertTrue(keeper.stopped.is_set())

    def test_any_setup_exception_releases_the_lease(self):
        released = []

        def fake_release(binary, server, slot, holder, lease_id):
            released.append((str(slot), str(lease_id)))
            return True

        def fake_setup(session):
            raise RuntimeError("unexpected")

        class KeeperStub:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

            def stop(self):
                pass

        rows = [{"server": "s", "slot": {"id": "1", "character": "A", "port": 0}, "state": "free"}]
        with tempfile.TemporaryDirectory() as tmp:
            args = run_args(tmp)
            with unittest.mock.patch.object(run.lease, "place_allowed", lambda s, b=None: (True, "alpha")), \
                 unittest.mock.patch.object(run.lease, "pick_free", lambda b, s, n: rows), \
                 unittest.mock.patch.object(run.lease, "acquire",
                                            lambda *a, **k: {"ok": True, "lease_id": "L1", "raw": ""}), \
                 unittest.mock.patch.object(run.lease, "release", fake_release), \
                 unittest.mock.patch.object(run.lease, "LeaseKeeper", KeeperStub), \
                 unittest.mock.patch.object(run.SlotSession, "setup_slot", fake_setup):
                r = run.Run(args, "", tmp, rows)
                self.assertFalse(r.prepare_slots([]))
        self.assertEqual(released, [("1", "L1")])


class GmGuardTest(unittest.TestCase):
    def test_gm_guard_closed_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            led = new_ledger(tmp)
            g = gm_guard.GmGuard(a, led, ctx_for())
            exact = {
                "gm_off": (None, (), ".gm off"),
                "levelup": (PLAYER, (2,), ".levelup 2"),
                "reset_talents": (PLAYER, (), ".reset talents"),
                "learn": (PLAYER, (12285,), ".learn 12285"),
                "unaura": (PLAYER, ("all",), ".unaura all"),
                "aura": (PLAYER, (12345,), ".aura 12345"),
                "cooldown": (PLAYER, (), ".cooldown"),
                "go_xyz": (None, (-12790, -400, 18, 0), ".go xyz -12790 -400 18 0"),
                "npc_summon": (None, (1234,), ".npc summon 1234"),
                "lookup_item": (None, ("Morning Glory Dew",), ".lookup item Morning Glory Dew"),
            }
            for kind, (target, args, text) in exact.items():
                got = g.send(kind, target, *args)
                self.assertEqual(got, text, kind)
            for line in (".npc add 123", ".reset all", ".bot run stop", ".respawn"):
                before = len(a.gm)
                with self.assertRaises(gm_guard.GmRefused):
                    g.send_text(line)
                self.assertEqual(len(a.gm), before)
            # `.die` is allowed only on a ledger summon and only through the selection
            led.add({"kind": "summon", "guid": MOB, "entry": 1234})
            self.assertEqual(g.send("die", MOB), ".die")
            with self.assertRaises(gm_guard.GmRefused):
                g.send("die", "0xF13000000000FFFF")

    def test_levelup_and_reset_talents_are_self_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            g = gm_guard.GmGuard(a, new_ledger(tmp), ctx_for())
            for kind, args in (("reset_talents", ()), ("levelup", (2,))):
                with self.assertRaises(gm_guard.GmRefused):
                    g.send(kind, HIRE, *args)
            self.assertEqual(a.gm, [])
            self.assertEqual(g.send("reset_talents", PLAYER), ".reset talents")
            self.assertEqual(a.gm, [".reset talents"])

    def test_gm_guard_selection_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(status={"state": "in_world", "guid": PLAYER, "health": 900, "max_health": 1000,
                                  "target": "0x0000000000000009"})
            g = gm_guard.GmGuard(a, new_ledger(tmp), ctx_for())
            a.ignore_select = True
            with self.assertRaises(gm_guard.GmServerError):
                g.send("damage", PLAYER, 100)
            self.assertEqual(a.gm, [])
            a.ignore_select = False
            a.status_row["target"] = PLAYER
            self.assertEqual(g.send("damage", PLAYER, 100), ".damage 100")
            self.assertEqual(a.gm, [".damage 100"])

    def test_selection_race_two_threads(self):
        """Each GM line lands on its own confirmed unit; the per-agent lock is the proof."""
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(unit_rows={
                PLAYER: {"guid": PLAYER, "max_health": 1000, "max_power": 500},
                HIRE: {"guid": HIRE, "max_health": 1000, "max_power": 500},
            })
            g = gm_guard.GmGuard(a, new_ledger(tmp), ctx_for())

            def one():
                g.send("modify_hp", PLAYER, 800, 1000)

            def two():
                g.send("modify_mana", HIRE, 250, 500)

            threads = [threading.Thread(target=one), threading.Thread(target=two)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            landed = dict(a.gm_at)
            self.assertEqual(landed[".modify hp 800 1000"], PLAYER)
            self.assertEqual(landed[".modify mana 250 500"], HIRE)

    def test_guard_restores_the_previous_target_when_nobody_moved_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(unit_rows={PLAYER: {"guid": PLAYER, "max_health": 1000, "max_power": 500}})
            a.status_row["target"] = MOB
            led = new_ledger(tmp)
            g = gm_guard.GmGuard(a, led, ctx_for())
            self.assertEqual(g.send("damage", PLAYER, 100), ".damage 100")
            self.assertEqual([agent_mod.norm_guid(g) for g in a.targets],
                             [agent_mod.norm_guid(PLAYER), agent_mod.norm_guid(MOB)])
            self.assertEqual(agent_mod.norm_guid(a.status_row["target"]), agent_mod.norm_guid(MOB))
            self.assertFalse([r for r in led.rows if r.get("kind") in ("retarget", "retarget_skipped")])

    def test_an_interleaved_attack_blocks_the_stale_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(unit_rows={PLAYER: {"guid": PLAYER, "max_health": 1000, "max_power": 500}})
            a.status_row["target"] = MOB
            led = new_ledger(tmp)
            g = gm_guard.GmGuard(a, led, ctx_for())
            s = situations.Situation(a, {"id": "t", "player": {"level": 35}}, g, led,
                                     FakeMarks(), StubCollector(),
                                     situations.SitContext(player_guid=PLAYER), arenas=[], mobs={})
            original = a.rpc
            attacked = []

            def rpc(method, params=None, timeout=None):
                out = original(method, params, timeout)
                if method == "gm" and not attacked:
                    attacked.append(params["text"])
                    s.attack(HIRE)      # another mover lands between the send and the restore
                return out

            a.rpc = rpc
            self.assertEqual(g.send("damage", PLAYER, 100), ".damage 100")
            self.assertEqual(attacked, [".damage 100"])
            self.assertEqual(a.targets[-1], HIRE)
            self.assertEqual(agent_mod.norm_guid(a.status_row["target"]), HIRE)
            self.assertFalse([r for r in led.rows if r.get("kind") == "retarget"])
            self.assertTrue([r for r in led.rows if r.get("kind") == "retarget_skipped"])

    def test_only_guard_calls_gm(self):
        pat = re.compile(r"""rpc\s*\(\s*['"]gm['"]|rpc_must\s*\(\s*['"]gm['"]|\bGM\s*\(""")
        offenders = []
        for path in sorted((ROOT / "trl" / "live").glob("*.py")):
            if path.name == "gm_guard.py":
                continue
            if pat.search(path.read_text()):
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_templates_are_tightened(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(unit_rows={PLAYER: {"guid": PLAYER, "health": 900}})
            g = gm_guard.GmGuard(a, new_ledger(tmp), ctx_for())
            with self.assertRaises(gm_guard.GmRefused):   # .aura id not in the trial file
                g.send("aura", PLAYER, 999)
            with self.assertRaises(gm_guard.GmRefused):   # .learn id not a trial talent
                g.send("learn", PLAYER, 999)
            with self.assertRaises(gm_guard.GmRefused):   # z outside the arena bounds
                g.send("go_xyz", None, -12790, -400, 80, 0)
            with self.assertRaises(gm_guard.GmRefused):   # tt pattern anchored
                g.tt("please quiet 40")
            self.assertEqual(g.tt("quiet 40 w=2"), {"replies": [{"ok": True}]})
            with self.assertRaises(gm_guard.GmRefused):   # max health unknown
                g.send("modify_hp", PLAYER, 800, 1000)

    def test_modify_hp_refused_when_the_max_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(unit_rows={PLAYER: {"guid": PLAYER, "health": 900}})
            g = gm_guard.GmGuard(a, new_ledger(tmp), ctx_for())
            with self.assertRaises(gm_guard.GmRefused):
                g.send("modify_hp", PLAYER, 900, 1000)
            self.assertEqual(a.gm, [])

    def test_modify_mana_refused_when_the_max_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(unit_rows={HIRE: {"guid": HIRE, "power": 10}})
            g = gm_guard.GmGuard(a, new_ledger(tmp), ctx_for())
            with self.assertRaises(gm_guard.GmRefused):
                g.send("modify_mana", HIRE, 100, 500)
            self.assertEqual(a.gm, [])

    def test_tt_allows_only_the_exact_quiet_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            g = gm_guard.GmGuard(a, new_ledger(tmp), ctx_for())
            for bad in ("quiet 45 w=2", "quiet 40", "quiet 40 w=3"):
                with self.assertRaises(gm_guard.GmRefused):
                    g.tt(bad)
            self.assertEqual(g.tt("quiet 40 w=2"), {"replies": [{"ok": True}]})

    def test_unit_not_visible_is_a_server_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            g = gm_guard.GmGuard(a, new_ledger(tmp), ctx_for())
            # the hire's role is allowed, but the unit is not in view
            with self.assertRaises(gm_guard.GmServerError):
                g.send("modify_hp", HIRE, 100, 100)
            self.assertTrue(g.server_error)
            self.assertFalse(g.refused)

    def test_gm_rpc_reject_is_a_server_error_not_a_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            a.gm_err = True
            g = gm_guard.GmGuard(a, new_ledger(tmp), ctx_for())
            with self.assertRaises(gm_guard.GmServerError):
                g.send("gm_off", None)
            self.assertTrue(g.server_error)
            self.assertFalse(g.refused)

    def test_hire_guid_has_no_name_lookup_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(units=[{"guid": HIRE, "name": "Helper", "max_health": 1000}])
            g = gm_guard.GmGuard(a, new_ledger(tmp),
                                 ctx_for(hire_guid="", hire_name="Helper"))
            with self.assertRaises(gm_guard.GmRefused):
                g.send("modify_hp", HIRE, 800, 1000)
            self.assertEqual(g._hire_guid(), "")

    def test_abort_hook_refuses_every_line_once_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            reason = [""]
            g = gm_guard.GmGuard(a, new_ledger(tmp), ctx_for(), abort=lambda: reason[0])
            self.assertEqual(g.send("gm_off", None), ".gm off")
            reason[0] = "lease lost"
            with self.assertRaises(gm_guard.GmAborted):
                g.send("gm_off", None)
            with self.assertRaises(gm_guard.GmAborted):
                g.send_text(".gm off")
            self.assertEqual(a.gm, [".gm off"])


class GovernorTest(unittest.TestCase):
    def test_damage_never_below_band_low(self):
        self.assertEqual(situations.damage_step(900, 1000, 40, 90), 0)   # exactly at the top: not above it
        self.assertEqual(situations.damage_step(950, 1000, 40, 90), 300)  # 950 -> the 65% middle
        self.assertLessEqual(situations.damage_step(990, 1000, 40, 90), 990 - 400)
        self.assertEqual(situations.damage_step(450, 1000, 40, 90), 0)

    def test_floor_rescue_marks_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(status={"state": "in_world", "guid": PLAYER, "health": 250, "max_health": 1000,
                                  "target": PLAYER},
                          unit_rows={PLAYER: {"guid": PLAYER, "health": 250, "max_health": 1000}})
            marks = FakeMarks()
            g = gm_guard.GmGuard(a, new_ledger(tmp), ctx_for())
            gov = situations.Governor(a, g, marks, victim="player", band=(40, 90), floor=30,
                                      victim_guid=PLAYER)
            gov.tick({"hp": 250, "max_hp": 1000, "pct": 25.0})
            # live1: the rescue heals to the band middle (65%), not to the floor itself (30%)
            self.assertIn(".modify hp 650 1000", a.gm)
            self.assertEqual(len(marks.kinds("gm_heal")), 1)
            self.assertEqual(len(marks.kinds("excluded")), 1)
            self.assertEqual(marks.kinds("excluded")[0]["why"], "floor")

    def test_a_stopped_governor_sends_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(status={"state": "in_world", "guid": PLAYER, "health": 950,
                                  "max_health": 1000, "target": PLAYER},
                          unit_rows={PLAYER: {"guid": PLAYER, "health": 950, "max_health": 1000}})
            stopped = threading.Event()
            stopped.set()
            marks = FakeMarks()
            gov = situations.Governor(a, gm_guard.GmGuard(a, new_ledger(tmp), ctx_for()), marks,
                                      victim="player", band=(40, 90), floor=30,
                                      victim_guid=PLAYER, stopped=stopped)
            gov.tick({"hp": 950, "max_hp": 1000, "pct": 95.0})
            self.assertEqual(a.gm, [])
            self.assertEqual(marks.kinds("gm_heal"), [])

    def test_the_corpse_heal_is_a_skip_not_a_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            g = gm_guard.GmGuard(a, new_ledger(tmp), ctx_for())
            gov = situations.Governor(a, g, FakeMarks(), victim="player", band=(40, 90), floor=30,
                                      victim_guid=PLAYER, log=lambda line: None)
            self.assertEqual(gov.tick({"hp": 0, "max_hp": 1000, "pct": 0.0, "dead": True}),
                             "player_dead")
            self.assertEqual(a.gm, [])
            self.assertFalse(g.refused)
            self.assertFalse(g.server_error)


class SetupTest(unittest.TestCase):
    def test_stance_not_held_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(units=[{"guid": PLAYER, "health": 1000, "max_health": 1000, "auras": []}])
            trial = {"id": "t", "player": {"class": "warrior", "level": 35, "stance": 71}}
            ctx = ctx_for(learn_ids=frozenset({12285, 12295, 71, 355}))
            row = setup.check_stance(a, trial, gm_guard.GmGuard(a, new_ledger(tmp), ctx), ctx, timeout=0.05)
            self.assertFalse(row["ok"])
            self.assertIn("stance not held", row["evidence"])

    def test_talents_proven_by_spells(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(spells={12285})
            trial = {"id": "t", "player": {"level": 35, "talents": {"tree": "arms", "spells": [12285, 12295]}}}
            row = setup.check_talents(a, trial, gm_guard.GmGuard(a, new_ledger(tmp), ctx_for()),
                                      ctx_for())
            self.assertFalse(row["ok"])
            self.assertIn("talents not learned: 12295", row["evidence"])

    def test_hire_identity_from_system_reply_and_party(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(events=[{"kind": "system", "text": "You have hired Helper for 30 minutes."}],
                          party={"in_group": True,
                                 "members": [{"name": "Helper", "guid": HIRE, "level": 35}]})
            led = new_ledger(tmp)
            row, name = setup.hire_bot(a, load_trial(), None, led, timeout=0.2)
            self.assertTrue(row["ok"], row)
            self.assertEqual(name, "Helper")
            hires = [r for r in led.rows if r.get("kind") == "hire"]
            self.assertEqual(hires[0]["guid"], agent_mod.norm_guid(HIRE))

    def test_whisper_hire_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(events=[{"kind": "whisper", "text": "hired Helper for 100g"},
                                  {"kind": "channel_notify", "text": "hired Helper for 5g"}])
            led = new_ledger(tmp)
            row, name = setup.hire_bot(a, load_trial(), None, led, timeout=0.1)
            self.assertFalse(row["ok"])
            self.assertIsNone(name)
            self.assertFalse([r for r in led.rows if r.get("kind") == "hire"])

    def test_hire_named_but_not_a_party_member_is_no_hire(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(events=[{"kind": "system", "text": "You have hired Helper for 30 minutes."}],
                          party={"in_group": False, "members": []})
            row, name = setup.hire_bot(a, load_trial(), None, new_ledger(tmp), timeout=0.05)
            self.assertFalse(row["ok"])
            self.assertIsNone(name)

    def test_lookup_item_takes_the_exact_name_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(events=[{"kind": "system",
                                   "text": "|Hitem:222:0:0:0|h[Morning Glory Dew (test)]|h |Hitem:111:0:0:0|h[Morning Glory Dew]|h"}])
            led = new_ledger(tmp)
            g = gm_guard.GmGuard(a, led, ctx_for())
            item_id, line = setup.lookup_item(a, g, "Morning Glory Dew", timeout=0.5)
            self.assertEqual(item_id, 111, line)


class RepeatTest(unittest.TestCase):
    def test_summon_ledgered_before_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            a.units_queue = [[], [{"guid": MOB, "entry": 1234, "distance": 2.0, "hostile": True,
                                   "dead": False, "level": 36, "health": 500, "max_health": 500}]]
            led = new_ledger(tmp)
            g = gm_guard.GmGuard(a, led, ctx_for())
            guid = situations.summon_mob(a, g, led, 1234, timeout=1.0, poll=0.01)
            self.assertEqual(guid, agent_mod.norm_guid(MOB))
            self.assertEqual(a.attacks, 0)
            self.assertIn({"kind": "summon", "guid": agent_mod.norm_guid(MOB)},
                          [{"kind": r.get("kind"), "guid": r.get("guid")} for r in led.rows])

    def test_summon_claim_ignores_units_beyond_three_yards(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            a.units_queue = [[], [{"guid": MOB, "entry": 1234, "distance": 5.0, "hostile": True,
                                   "dead": False, "level": 36, "health": 500, "max_health": 500}]]
            led = new_ledger(tmp)
            g = gm_guard.GmGuard(a, led, ctx_for())
            self.assertIsNone(situations.summon_mob(a, g, led, 1234, timeout=0.2, poll=0.01))
            self.assertEqual(led.summon_guids(), [])

    def test_summon_claim_by_the_reply_guid(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(events=[{"kind": "system", "text": "Summoned creature 0xF13000000000ABCD"}])
            a.units_queue = [[], [{"guid": MOB, "entry": 1234, "distance": 20.0, "hostile": True,
                                   "dead": False, "level": 36, "health": 500, "max_health": 500}]]
            led = new_ledger(tmp)
            g = gm_guard.GmGuard(a, led, ctx_for())
            guid = situations.summon_mob(a, g, led, 1234, timeout=0.5, poll=0.01)
            self.assertEqual(guid, agent_mod.norm_guid(MOB))

    def test_unknown_summons_are_leftover_not_dead(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(units=[{"guid": MOB, "entry": 1234, "dead": False}])
            a.units_raise = True
            led = new_ledger(tmp)
            led.add({"kind": "summon", "guid": MOB, "entry": 1234})
            rows = teardown.repeat_cleanup(a, gm_guard.GmGuard(a, led, ctx_for()), led, "Helper", PLAYER)
            dead = [r for r in rows if r["id"] == "mobs_dead"]
            self.assertFalse(dead[0]["ok"])
            self.assertIn("UNKNOWN", dead[0]["evidence"])
            self.assertTrue([r for r in led.rows if r.get("kind") == "mob_leftover"])

    def test_kill_touches_only_this_slots_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            led_a = new_ledger(os.path.join(tmp, "a") + os.sep + "ledger.jsonl")
            led_b = new_ledger(os.path.join(tmp, "b") + os.sep + "ledger.jsonl")
            led_a.add({"kind": "summon", "guid": MOB, "entry": 1234})
            led_b.add({"kind": "summon", "guid": MOB_DEAD, "entry": 1234})
            g = gm_guard.GmGuard(a, led_a, ctx_for())
            with self.assertRaises(gm_guard.GmRefused):
                g.kill(MOB_DEAD)
            self.assertEqual(a.kills, [])

    def test_parallel_slots_take_different_arenas(self):
        trial = {"id": "t", "player": {"level": 35}, "situation": {"mobs": {"role": "melee", "count": 1}}}
        arenas = [
            {"id": "a", "map": 0, "x": 1, "y": 2, "z": 3, "level_min": 1, "level_max": 60},
            {"id": "b", "map": 0, "x": 11, "y": 12, "z": 13, "level_min": 1, "level_max": 60},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            ctx = ctx_for(arena_coords=frozenset({(0, 1, 2, 3), (0, 11, 12, 13)}))
            picked = []
            for index in (0, 1):
                s = situations.Situation(a, trial, gm_guard.GmGuard(a, new_ledger(tmp), ctx),
                                         new_ledger(tmp), FakeMarks(), StubCollector(),
                                         situations.SitContext(player_guid=PLAYER),
                                         arenas=arenas, mobs={}, slot_index=index)
                s.quiet_ok = lambda: True
                with unittest.mock.patch.object(situations.time, "sleep", lambda *_: None):
                    picked.append(s.arena()["id"])
            self.assertEqual(picked, ["a", "b"])

    def test_refill_uses_the_trials_mob_kind(self):
        trial = {"id": "t", "player": {"level": 35}, "duration_s": 5,
                 "situation": {"kind": "tank_damage", "victim": "player",
                               "mobs": {"role": "caster", "count": 1, "refill": True}}}
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            collector = StubCollector()
            s = situations.Situation(a, trial, gm_guard.GmGuard(a, new_ledger(tmp), ctx_for()),
                                     new_ledger(tmp), FakeMarks(), collector, situations.SitContext(
                                         player_guid=PLAYER), arenas=[], mobs={})
            calls = []
            s.arena = lambda: {"id": "a"}
            s.summon = lambda role, count: (calls.append((role, count)) or [MOB])
            s.summoned = []

            def fake_sleep(_seconds):
                collector.lost = "disconnected: test"

            with unittest.mock.patch.object(situations.time, "sleep", fake_sleep):
                out = s.run()
            self.assertEqual(calls[0], ("caster", 1))
            self.assertIn(("caster", 1), calls[1:])
            self.assertIn("disconnected", out.get("invalid_reason", ""))

    def test_disconnect_mid_repeat_is_invalid_and_samples_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(status_seq=[
                {"state": "in_world", "guid": PLAYER, "health": 900, "max_health": 1000,
                 "target": PLAYER},
                {"state": "disconnected", "guid": PLAYER, "health": 900, "max_health": 1000},
            ])
            marks = collect.Marks(os.path.join(tmp, "marks.jsonl"))
            marks.start()
            collector = collect.Collector(a, tmp, marks, {}, new_ledger(tmp), load_trial())
            th = threading.Thread(target=collector._sample, daemon=True)
            th.start()
            th.join(timeout=5)
            self.assertTrue(collector.lost.startswith("disconnected"), collector.lost)
            samples_before = collector.samples
            self.assertGreater(samples_before, 0)
            time.sleep(0.3)
            self.assertEqual(collector.samples, samples_before)
            self.assertTrue(collector._stop.is_set())

    def test_exit_codes_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run.Run(run_args(tmp), "", tmp, [])
            self.assertEqual(r.exit_code(), 0)
            r.errors.append("x")
            self.assertEqual(r.exit_code(), 1)
            r.refused = True
            self.assertEqual(r.exit_code(), 4)
            r.server_error = True
            self.assertEqual(r.exit_code(), 5)


class WorkerCleanupTest(unittest.TestCase):
    def setUp(self):
        run.STOP.clear()   # run.main() sets the module STOP on its way out

    def make_run(self, tmp):
        r = run.Run(run_args(tmp), "", tmp, [])
        r.tb_version = "test"
        return r

    def make_session(self, tmp, agent):
        row = {"slot": {"id": "1", "port": 0, "character": "Trlwarriora"}}
        session = run.SlotSession(row, os.path.join(tmp, "slot1"), "", "holder", r_stub(), lambda line: None)
        session.agent = agent
        session.ledger = new_ledger(tmp)
        session.ensure_character = lambda klass: setattr(session, "klass", klass)
        return session

    def test_exception_mid_repeat_cleans_up_and_records_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(units=[{"guid": MOB, "entry": 1234, "dead": False}],
                          events=[{"kind": "system", "text": "Your role set to clear"},
                                  {"kind": "system", "text": "Removal requested."}])
            session = self.make_session(tmp, a)
            session.ledger.add({"kind": "summon", "guid": MOB, "entry": 1234})
            r = self.make_run(tmp)

            class Boom:
                def __init__(self, *args, **kwargs):
                    self.summoned = []
                    self.mob_source = "summon"

                def run(self):
                    raise RuntimeError("boom mid-repeat")

            with unittest.mock.patch.object(run, "run_setup", lambda *a, **k: {
                    "ok": True, "checks": [], "guids": {"player": PLAYER, "bot": None, "pet": None,
                                                        "mobs": [], "marked": None},
                    "bot_name": "Helper", "config": {}}), \
                 unittest.mock.patch.object(run.collect, "Collector", StubCollector), \
                 unittest.mock.patch.object(run.situations, "Situation", Boom), \
                 unittest.mock.patch.object(run.Run, "pull_evpull", lambda *a, **k: None), \
                 unittest.mock.patch.object(run.teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                r.worker(session, [load_trial()], r.errors)

            self.assertIn(agent_mod.norm_guid(MOB), [agent_mod.norm_guid(g) for g in a.kills])
            self.assertIn("role self clear", a.tbm_calls)
            self.assertIn("remove Helper", a.tbm_calls)
            invalids = [x for x in r.ledger.rows if x.get("kind") == "invalid"]
            self.assertEqual(invalids[-1]["reason"], "error: RuntimeError")
            self.assertEqual(r.exit_code(), 1)

    def test_ensure_character_failure_is_invalid_and_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            session = self.make_session(tmp, a)

            def refuse(klass):
                raise run.LoginRefused("no free character slot")

            session.ensure_character = refuse
            r = self.make_run(tmp)
            with unittest.mock.patch.object(run.teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                r.worker(session, [load_trial()], r.errors)
            invalids = [x for x in r.ledger.rows if x.get("kind") == "invalid"]
            self.assertEqual(invalids[-1]["reason"], "error: LoginRefused")
            self.assertEqual(r.exit_code(), 1)

    def test_disconnect_repeat_is_invalid_and_exit_5(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(events=[{"kind": "system", "text": "Your role set to clear"},
                                  {"kind": "system", "text": "Removal requested."}])
            session = self.make_session(tmp, a)
            r = self.make_run(tmp)

            class Gone:
                def __init__(self, *args, **kwargs):
                    self.summoned = []
                    self.mob_source = "summon"

                def level_window(self):
                    return (34, 36)

                def run(self):
                    return {"ok": False, "ended": "disconnected",
                            "invalid_reason": "disconnected: agent state disconnected"}

            with unittest.mock.patch.object(run, "run_setup", lambda *a, **k: {
                    "ok": True, "checks": [], "guids": {"player": PLAYER, "bot": None, "pet": None,
                                                        "mobs": [], "marked": None},
                    "bot_name": "", "config": {}}), \
                 unittest.mock.patch.object(run.collect, "Collector", StubCollector), \
                 unittest.mock.patch.object(run.situations, "Situation", Gone), \
                 unittest.mock.patch.object(run.Run, "pull_evpull", lambda *a, **k: None), \
                 unittest.mock.patch.object(run.SlotSession, "relogin", lambda self: False), \
                 unittest.mock.patch.object(run.teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                r.worker(session, [load_trial()], r.errors)
            invalids = [x for x in r.ledger.rows if x.get("kind") == "invalid"]
            self.assertTrue(invalids[-1]["reason"].startswith("disconnected"), invalids[-1])
            self.assertEqual(r.exit_code(), 5)

    def test_lease_lost_records_leftovers_when_it_cannot_be_reacquired(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            session = self.make_session(tmp, a)
            r = self.make_run(tmp)

            class Lost:
                def __init__(self, *args, **kwargs):
                    self.summoned = []
                    self.mob_source = "summon"

                def level_window(self):
                    return (34, 36)

                def run(self):
                    session.abort("lease lost")
                    return {"ok": False, "ended": "lease lost", "invalid_reason": "lease lost"}

            with unittest.mock.patch.object(run, "run_setup", lambda *a, **k: {
                    "ok": True, "checks": [], "guids": {"player": PLAYER, "bot": None, "pet": None,
                                                        "mobs": [], "marked": None},
                    "bot_name": "Helper", "config": {}}), \
                 unittest.mock.patch.object(run.collect, "Collector", StubCollector), \
                 unittest.mock.patch.object(run.situations, "Situation", Lost), \
                 unittest.mock.patch.object(run.Run, "pull_evpull", lambda *a, **k: None), \
                 unittest.mock.patch.object(run.lease, "acquire",
                                            lambda *a, **k: {"ok": False, "lease_id": "", "raw": "held"}), \
                 unittest.mock.patch.object(run.teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                r.worker(session, [load_trial()], r.errors)
            invalids = [x for x in r.ledger.rows if x.get("kind") == "invalid"]
            self.assertEqual(invalids[-1]["reason"], "lease lost")
            # the lease could not be re-acquired: no GM line on the slot, both cleanups recorded
            self.assertNotIn("remove Helper", a.tbm_calls)
            leftovers = [x for x in r.ledger.rows if x.get("kind") == "leftover"]
            self.assertEqual({x["check"] for x in leftovers},
                             {"forced_role_clear", "hire_removed"})
            self.assertEqual(r.exit_code(), 5)   # M4's exit code for a lost lease (not 4)

    def test_lease_lost_trial_cleanup_runs_after_reacquire(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(events=[{"kind": "system", "text": "Your role set to clear"},
                                  {"kind": "system", "text": "Removal requested."}])
            session = self.make_session(tmp, a)
            r = self.make_run(tmp)
            releases = []

            class Lost:
                def __init__(self, *args, **kwargs):
                    self.summoned = []
                    self.mob_source = "summon"

                def level_window(self):
                    return (34, 36)

                def run(self):
                    session.abort("lease lost")
                    return {"ok": False, "ended": "lease lost", "invalid_reason": "lease lost"}

            with unittest.mock.patch.object(run, "run_setup", lambda *a, **k: {
                    "ok": True, "checks": [], "guids": {"player": PLAYER, "bot": None, "pet": None,
                                                        "mobs": [], "marked": None},
                    "bot_name": "Helper", "config": {}}), \
                 unittest.mock.patch.object(run.collect, "Collector", StubCollector), \
                 unittest.mock.patch.object(run.situations, "Situation", Lost), \
                 unittest.mock.patch.object(run.Run, "pull_evpull", lambda *a, **k: None), \
                 unittest.mock.patch.object(run.lease, "acquire",
                                            lambda *a, **k: {"ok": True, "lease_id": "L9", "raw": ""}), \
                 unittest.mock.patch.object(run.lease, "release",
                                            lambda *a, **k: (releases.append(a) or True)), \
                 unittest.mock.patch.object(run.teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                r.worker(session, [load_trial()], r.errors)
            self.assertIn("role self clear", a.tbm_calls)
            self.assertIn("remove Helper", a.tbm_calls)
            self.assertTrue(releases)
            self.assertEqual(r.exit_code(), 5)

    def test_bot_name_survives_into_the_next_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(events=[{"kind": "system", "text": "Your role set to clear"},
                                  {"kind": "system", "text": "Removal requested."}])
            session = self.make_session(tmp, a)
            r = self.make_run(tmp)
            r.args.repeats = 2
            calls = []

            def fake_repeat(self, session_, trial, rep):
                calls.append(rep)
                if rep == 1:
                    return "Helper", None
                raise RuntimeError("second repeat dies")

            with unittest.mock.patch.object(run.Run, "run_repeat", fake_repeat), \
                 unittest.mock.patch.object(run.Run, "pull_evpull", lambda *a, **k: None), \
                 unittest.mock.patch.object(run.teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                r.worker(session, [load_trial()], r.errors)
            self.assertIn("remove Helper", a.tbm_calls)
            self.assertEqual(calls, [1, 2])

    def _fake_setup(self, fn):
        def setup(agent, trial, guard, ledger, ctx, first=True, previous=None):
            return fn(agent, trial, guard)
        return setup

    def test_safety_refusal_in_setup_is_invalid_and_exit_4(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            session = self.make_session(tmp, a)
            r = self.make_run(tmp)

            def boom(agent, trial, guard):
                guard.send_text(".npc add 123")   # the real closed-set refusal

            with unittest.mock.patch.object(run, "run_setup", self._fake_setup(boom)), \
                 unittest.mock.patch.object(run.collect, "Collector", StubCollector), \
                 unittest.mock.patch.object(run.Run, "pull_evpull", lambda *a, **k: None), \
                 unittest.mock.patch.object(run.teardown, "trial_cleanup", lambda *a, **k: []), \
                 unittest.mock.patch.object(run.teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                r.worker(session, [load_trial()], r.errors)
            invalids = [x for x in r.ledger.rows if x.get("kind") == "invalid"]
            self.assertEqual(invalids[-1]["reason"], "error: GmRefused")
            self.assertEqual(a.gm, [])
            self.assertEqual(r.exit_code(), 4)

    def test_gm_server_error_in_setup_is_invalid_and_exit_5(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            a.gm_err = True
            session = self.make_session(tmp, a)
            r = self.make_run(tmp)

            def reject(agent, trial, guard):
                guard.send("gm_off", None)       # the real gm RPC reject path

            with unittest.mock.patch.object(run, "run_setup", self._fake_setup(reject)), \
                 unittest.mock.patch.object(run.collect, "Collector", StubCollector), \
                 unittest.mock.patch.object(run.Run, "pull_evpull", lambda *a, **k: None), \
                 unittest.mock.patch.object(run.teardown, "trial_cleanup", lambda *a, **k: []), \
                 unittest.mock.patch.object(run.teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                r.worker(session, [load_trial()], r.errors)
            invalids = [x for x in r.ledger.rows if x.get("kind") == "invalid"]
            self.assertEqual(invalids[-1]["reason"], "error: GmServerError")
            self.assertEqual(r.exit_code(), 5)

    def test_setup_fail_cleanup_exception_still_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            session = self.make_session(tmp, a)
            r = self.make_run(tmp)

            def refuse(agent, guard, ledger, bot_name, player_guid, wait_s=0, **kw):
                raise gm_guard.GmServerError("popup")

            with unittest.mock.patch.object(run, "run_setup", lambda *a, **k: {
                    "ok": False, "checks": [], "invalid_reason": "stance not held",
                    "guids": {"player": PLAYER}, "bot_name": "Helper", "config": {}}), \
                 unittest.mock.patch.object(run.collect, "Collector", StubCollector), \
                 unittest.mock.patch.object(run.Run, "pull_evpull", lambda *a, **k: None), \
                 unittest.mock.patch.object(run.teardown, "repeat_cleanup", refuse), \
                 unittest.mock.patch.object(run.teardown, "trial_cleanup", lambda *a, **k: []), \
                 unittest.mock.patch.object(run.teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                r.worker(session, [load_trial()], r.errors)
            self.assertEqual(r.exit_code(), 5)

    def test_lease_lost_during_setup_stops_gm_lines_invalid_exit_5(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            session = self.make_session(tmp, a)
            r = self.make_run(tmp)

            def lost(agent, trial, guard):
                guard.send("gm_off", None)
                session.abort("lease lost")
                with self.assertRaises(gm_guard.GmAborted):
                    guard.send("gm_off", None)
                raise gm_guard.GmAborted("guard aborted: lease lost")

            with unittest.mock.patch.object(run, "run_setup", self._fake_setup(lost)), \
                 unittest.mock.patch.object(run.collect, "Collector", StubCollector), \
                 unittest.mock.patch.object(run.Run, "pull_evpull", lambda *a, **k: None), \
                 unittest.mock.patch.object(run.lease, "acquire",
                                            lambda *a, **k: {"ok": False, "lease_id": "", "raw": "held"}), \
                 unittest.mock.patch.object(run.teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                r.worker(session, [load_trial()], r.errors)
            self.assertEqual(a.gm, [".gm off"])    # no GM line after the lease was lost
            invalids = [x for x in r.ledger.rows if x.get("kind") == "invalid"]
            self.assertEqual(invalids[-1]["reason"], "lease lost")
            self.assertEqual(r.exit_code(), 5)

    def test_failed_trial_cleanup_is_a_leftover_and_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            session = self.make_session(tmp, a)
            r = self.make_run(tmp)

            def fake_repeat(self_, session_, trial, rep):
                return "Helper", None

            with unittest.mock.patch.object(run.Run, "run_repeat", fake_repeat), \
                 unittest.mock.patch.object(run.Run, "pull_evpull", lambda *a, **k: None), \
                 unittest.mock.patch.object(run.teardown, "trial_cleanup",
                                            lambda *a, **k: [{"id": "hire_removed", "ok": False,
                                                              "evidence": "no remove reply"}]), \
                 unittest.mock.patch.object(run.teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                r.worker(session, [load_trial()], r.errors)
            leftovers = [x for x in r.ledger.rows if x.get("kind") == "leftover"]
            self.assertEqual([x["check"] for x in leftovers], ["hire_removed"])
            self.assertEqual(r.exit_code(), 1)

    def test_disconnect_relogin_reruns_repeat_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent(events=[{"kind": "system", "text": "Your role set to clear"},
                                  {"kind": "system", "text": "Removal requested."}])
            session = self.make_session(tmp, a)
            r = self.make_run(tmp)
            relogins = []
            calls = []

            class Gone:
                def __init__(self, *args, **kwargs):
                    self.summoned = []
                    self.mob_source = "summon"

                def level_window(self):
                    return (34, 36)

                def run(self):
                    return {"ok": False, "ended": "disconnected",
                            "invalid_reason": "disconnected: agent state disconnected"}

            def fake_repeat_cleanup(*args, **kwargs):
                calls.append(args)
                return []

            with unittest.mock.patch.object(run, "run_setup", lambda *a, **k: {
                    "ok": True, "checks": [], "guids": {"player": PLAYER, "bot": None, "pet": None,
                                                        "mobs": [], "marked": None},
                    "bot_name": "", "config": {}}), \
                 unittest.mock.patch.object(run.collect, "Collector", StubCollector), \
                 unittest.mock.patch.object(run.situations, "Situation", Gone), \
                 unittest.mock.patch.object(run.Run, "pull_evpull", lambda *a, **k: None), \
                 unittest.mock.patch.object(run.SlotSession, "relogin",
                                            lambda self: (relogins.append(1) or True)), \
                 unittest.mock.patch.object(run.teardown, "repeat_cleanup", fake_repeat_cleanup), \
                 unittest.mock.patch.object(run.teardown, "trial_cleanup", lambda *a, **k: []), \
                 unittest.mock.patch.object(run.teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                r.worker(session, [load_trial()], r.errors)
            self.assertEqual(relogins, [1])
            self.assertEqual(len(calls), 2)   # the repeat's own cleanup + M8's after the relogin
            self.assertEqual(r.exit_code(), 5)

    # run_repeat's mobs_ready fallback. An ooc_hurt trial summons nothing (mobs.count 0), so the
    # fallback row always read "0/0 summons hostile, alive, level 40-42" and the rep came out INVALID.
    def repeat_setup_doc(self, count):
        trial = run.load_trial(str(ROOT / "trials" / "healers" / "priest-holy-pws-def-arms-ooc.json"))
        trial["situation"]["mobs"]["count"] = count
        with tempfile.TemporaryDirectory() as tmp:
            session = self.make_session(tmp, FakeAgent())
            r = self.make_run(tmp)

            class Window:
                """Stands in for situations.Situation: the window ends clean and nothing is summoned."""

                def __init__(self, *args, **kwargs):
                    self.summoned = []
                    self.mob_source = "none"

                def level_window(self):
                    return (40, 42)

                def run(self):
                    return {"ok": True, "ended": "window done", "invalid_reason": None}

            # the in-fight visibility check reads a real fight's events.jsonl: not what this test is about
            with unittest.mock.patch.object(run, "run_setup", lambda *a, **k: {
                    "ok": True, "checks": [], "guids": {"player": PLAYER, "bot": None, "pet": None,
                                                        "mobs": [], "marked": None},
                    "bot_name": "Helper", "config": {}}), \
                 unittest.mock.patch.object(run.collect, "Collector", StubCollector), \
                 unittest.mock.patch.object(run.situations, "Situation", Window), \
                 unittest.mock.patch.object(run, "check_player_visible", lambda *a, **k: None):
                r.run_repeat(session, trial, 1)
            with open(os.path.join(tmp, trial["id"], "rep1", "setup.json")) as f:
                return json.load(f)

    def test_zero_mobs_setup_gets_no_mobs_ready_check_and_is_ok(self):
        doc = self.repeat_setup_doc(count=0)
        self.assertEqual([c for c in doc["checks"] if c.get("id") == "mobs_ready"], [], doc["checks"])
        self.assertTrue(doc["ok"], doc.get("invalid_reason"))

    def test_summoning_setup_without_a_mobs_ready_row_still_gets_the_fallback(self):
        doc = self.repeat_setup_doc(count=1)
        rows = [c for c in doc["checks"] if c.get("id") == "mobs_ready"]
        self.assertEqual(len(rows), 1, doc["checks"])
        self.assertFalse(rows[0]["ok"])
        self.assertFalse(doc["ok"])


class UnitsMustTest(unittest.TestCase):
    def test_units_must_raises_on_the_real_err_shape(self):
        a = FakeAgent()
        a.units_err = True
        with self.assertRaises(agent_mod.RpcError):
            a.units_must(within=60)
        self.assertEqual(a.units(within=60), [])    # the lenient reader still reads empty

    def test_unit_must_separates_gone_from_a_broken_rpc(self):
        self.assertRaises(agent_mod.UnitGone, FakeAgent().unit_must, guid=MOB)

        class Broken(FakeAgent):
            def rpc(self, method, params=None, timeout=None):
                if method == "unit":
                    return {"_err": "internal error"}
                return FakeAgent.rpc(self, method, params, timeout)

        with self.assertRaises(agent_mod.RpcError) as caught:
            Broken().unit_must(guid=MOB)
        self.assertNotIsInstance(caught.exception, agent_mod.UnitGone)

    def test_summon_prelist_error_never_claims_a_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            a.units_err = True
            led = new_ledger(tmp)
            g = gm_guard.GmGuard(a, led, ctx_for())
            with self.assertRaises(agent_mod.RpcError):
                situations.summon_mob(a, g, led, 1234, timeout=0.1, poll=0.01)
            self.assertEqual(led.summon_guids(), [])
            self.assertEqual(a.gm, [])    # refused before the summon line went out

    def test_alive_summons_propagates_an_rpc_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            a.units_err = True
            led = new_ledger(tmp)
            led.add({"kind": "summon", "guid": MOB, "entry": 1234})
            with self.assertRaises(agent_mod.RpcError):
                situations.alive_summons(a, led)


class RefillTest(unittest.TestCase):
    def test_an_rpc_error_skips_the_refill_tick(self):
        trial = {"id": "t", "player": {"level": 35}, "duration_s": 5,
                 "situation": {"kind": "tank_damage", "victim": "player",
                               "mobs": {"role": "melee", "count": 2, "refill": True}}}
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            collector = StubCollector()
            s = situations.Situation(a, trial, gm_guard.GmGuard(a, new_ledger(tmp), ctx_for()),
                                     new_ledger(tmp), FakeMarks(), collector,
                                     situations.SitContext(player_guid=PLAYER),
                                     arenas=[], mobs={})
            calls = []
            s.arena = lambda: {"id": "a"}
            s.summon = lambda role, count: (calls.append((role, count))
                                            or ([MOB] if len(calls) == 1 else []))
            s.summoned = []

            def fake_sleep(_seconds):
                collector.lost = "disconnected: test"

            with unittest.mock.patch.object(situations, "alive_summons",
                                            side_effect=agent_mod.RpcError("units: rpc failed")), \
                 unittest.mock.patch.object(situations.time, "sleep", fake_sleep):
                out = s.run()
            self.assertEqual(calls, [("melee", 2)])   # no refill on an unreadable unit list
            self.assertIn("disconnected", out.get("invalid_reason", ""))


class TrackedLock:
    def __init__(self):
        self.depth = 0

    def __enter__(self):
        self.depth += 1
        return self

    def __exit__(self, *exc):
        self.depth -= 1
        return False


class LockScopeTest(unittest.TestCase):
    def test_threat_casts_under_the_action_lock(self):
        lock = TrackedLock()

        class A(FakeAgent):
            def rpc(self, method, params=None, timeout=None):
                if method == "cast":
                    self.cast_locked = lock.depth > 0
                return FakeAgent.rpc(self, method, params, timeout)

        with tempfile.TemporaryDirectory() as tmp:
            a = A()
            a.action_lock = lock
            s = situations.Situation(a, {"id": "t", "player": {"level": 35, "stance": 71}},
                                     gm_guard.GmGuard(a, new_ledger(tmp), ctx_for()),
                                     new_ledger(tmp), FakeMarks(), StubCollector(),
                                     situations.SitContext(player_guid=PLAYER), arenas=[], mobs={})
            s.threat()
            self.assertEqual([c["spell_id"] for c in a.casts], [355])
            self.assertTrue(a.cast_locked)

    def test_mark_runs_under_the_action_lock_and_needs_the_exact_ack(self):
        lock = TrackedLock()

        class A(FakeAgent):
            def rpc(self, method, params=None, timeout=None):
                if method == "mark":
                    self.mark_locked = lock.depth > 0
                return FakeAgent.rpc(self, method, params, timeout)

        with tempfile.TemporaryDirectory() as tmp:
            a = A(events=[{"kind": "system",
                           "text": "TBM:ACTION_ACK|cc moon Helper|party|1|Helper"}])
            a.action_lock = lock
            s = situations.Situation(a, {"id": "t", "player": {"level": 35}},
                                     gm_guard.GmGuard(a, new_ledger(tmp), ctx_for()),
                                     new_ledger(tmp), FakeMarks(), StubCollector(),
                                     situations.SitContext(player_guid=PLAYER, bot_name="Helper"),
                                     arenas=[], mobs={})
            row = s.mark_and_cc(MOB)
            self.assertTrue(a.mark_locked)
            self.assertTrue(row["ok"], row)

            b = A(events=[{"kind": "system", "text": "CC order set for Helper"}])
            s2 = situations.Situation(b, {"id": "t", "player": {"level": 35}},
                                      gm_guard.GmGuard(b, new_ledger(tmp), ctx_for()),
                                      new_ledger(tmp), FakeMarks(), StubCollector(),
                                      situations.SitContext(player_guid=PLAYER, bot_name="Helper"),
                                      arenas=[], mobs={})
            self.assertFalse(s2.mark_and_cc(MOB)["ok"])

    def test_summon_refuses_a_fallback_unit_at_an_unknown_distance(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = FakeAgent()
            a.units_queue = [[], [{"guid": MOB, "entry": 1234, "distance": -1, "hostile": True,
                                   "dead": False, "level": 36}]]
            led = new_ledger(tmp)
            g = gm_guard.GmGuard(a, led, ctx_for())
            self.assertIsNone(situations.summon_mob(a, g, led, 1234, timeout=0.2, poll=0.01))
            self.assertEqual(led.summon_guids(), [])


class CleanupTest(unittest.TestCase):
    def _setup(self, tmp, guids, dir_exists=True, account="ACC", character="Trlwarriora"):
        run_dir = os.path.join(tmp, "run")
        os.makedirs(run_dir, exist_ok=True)
        slot_dir = os.path.join(tmp, "slot1")
        if dir_exists:
            os.makedirs(slot_dir, exist_ok=True)
        else:
            slot_dir = os.path.join(tmp, "gone-slot1")
        slot_ledger_path = os.path.join(run_dir, "s-1.jsonl")
        slab = Ledger(slot_ledger_path)
        for guid in guids:
            slab.add({"kind": "summon", "guid": guid, "entry": 1234})
        ledger = Ledger(os.path.join(run_dir, "ledger.jsonl"))
        ledger.add({"kind": "lease", "server": "s", "slot": "1", "holder": "holder",
                    "lease_id": "L1", "dir": slot_dir, "ledger": slot_ledger_path,
                    "account": account, "character": character})
        ledger.add({"kind": "session", "server": "s", "slot": "1",
                    "account": account, "character": character})
        ledger.add({"kind": "arena", "id": "a1", "server": "s", "slot": "1",
                    "map": 0, "x": -12790, "y": -400, "z": 18})
        return run_dir, slot_dir

    def _session(self, agent):
        class FakeSession:
            def __init__(self, agent_):
                self.agent = agent_
                self.stopped = False

            def stop_agent(self):
                self.stopped = True

        return FakeSession(agent)

    @staticmethod
    def _rows(state="free"):
        return [{"server": "s", "slot": {"id": "1", "character": "Trlwarriora", "port": 0},
                 "state": state}]

    def _env(self, state="free", acquire_ok=True):
        releases = []
        acquired = []

        def acquire(binary, server, slot, holder, ttl=None):
            acquired.append((slot, holder))
            if not acquire_ok:
                return {"ok": False, "lease_id": "", "raw": "held by someone else"}
            return {"ok": True, "lease_id": "L9", "raw": "", "account": "ACC",
                    "character": "Trlwarriora", "port": 0}

        def release(*args):
            releases.append(args)
            return True

        return releases, acquired, acquire, release

    def test_cleanup_takes_a_fresh_lease_logs_in_at_the_arena_and_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, _slot_dir = self._setup(tmp, [MOB])
            a = FakeAgent(units=[{"guid": MOB, "entry": 1234, "dead": False}])
            releases, acquired, acquire, release = self._env()
            seen = []

            def login(rec, led):
                seen.append(rec.get("character"))
                self.assertTrue(acquired, "login before the fresh lease")
                return self._session(a)

            with unittest.mock.patch.object(lease, "list_slots", lambda b, s: (True, self._rows())), \
                 unittest.mock.patch.object(lease, "acquire", acquire), \
                 unittest.mock.patch.object(lease, "release", release), \
                 unittest.mock.patch.object(teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                rc = teardown.cleanup(run_dir, binary="/bin/true", login_factory=login)
            self.assertEqual(rc, 0)
            self.assertEqual(seen, ["Trlwarriora"])   # the ledger character, not the slot default
            self.assertEqual(acquired[0][0], "1")
            self.assertIn(agent_mod.norm_guid(MOB), [agent_mod.norm_guid(g) for g in a.kills])
            self.assertIn(".go xyz -12790 -400 18 0", a.gm)    # the ledger arena
            self.assertIn("role self clear", a.tbm_calls)
            self.assertEqual(releases[0][2], "1")
            self.assertEqual(releases[0][4], "L9")

    def test_cleanup_releases_even_when_the_slot_dir_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, _slot_dir = self._setup(tmp, [MOB], dir_exists=False)
            a = FakeAgent(units=[{"guid": MOB, "entry": 1234, "dead": False}])
            releases, _acquired, acquire, release = self._env()
            with unittest.mock.patch.object(lease, "list_slots", lambda b, s: (True, self._rows())), \
                 unittest.mock.patch.object(lease, "acquire", acquire), \
                 unittest.mock.patch.object(lease, "release", release):
                rc = teardown.cleanup(run_dir, binary="/bin/true",
                                      login_factory=lambda rec, led: self._session(a))
            self.assertEqual(rc, 0)
            self.assertEqual(releases[0][2], "1")

    def test_cleanup_refuses_a_slot_someone_else_holds(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, _slot_dir = self._setup(tmp, [MOB])
            _releases, acquired, acquire, release = self._env()
            logins = []
            with unittest.mock.patch.object(
                    lease, "list_slots",
                    lambda b, s: (True, self._rows("held by walker until 12:00"))), \
                 unittest.mock.patch.object(lease, "acquire", acquire), \
                 unittest.mock.patch.object(lease, "release", release):
                rc = teardown.cleanup(run_dir, binary="/bin/true",
                                      login_factory=lambda rec, led: logins.append(rec))
            self.assertEqual(rc, 1)
            self.assertEqual(acquired, [])
            self.assertEqual(logins, [])     # never logs into a held slot

    def test_cleanup_refuses_when_the_slot_table_is_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, _slot_dir = self._setup(tmp, [MOB])
            _releases, acquired, acquire, release = self._env()
            logins = []
            with unittest.mock.patch.object(lease, "list_slots", lambda b, s: (False, [])), \
                 unittest.mock.patch.object(lease, "acquire", acquire):
                rc = teardown.cleanup(run_dir, binary="/bin/true",
                                      login_factory=lambda rec, led: logins.append(rec))
            self.assertEqual(rc, 1)
            self.assertEqual(acquired, [])
            self.assertEqual(logins, [])

    def test_cleanup_refuses_when_the_fresh_lease_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, _slot_dir = self._setup(tmp, [MOB])
            _releases, acquired, acquire, release = self._env(acquire_ok=False)
            logins = []
            with unittest.mock.patch.object(lease, "list_slots", lambda b, s: (True, self._rows())), \
                 unittest.mock.patch.object(lease, "acquire", acquire):
                rc = teardown.cleanup(run_dir, binary="/bin/true",
                                      login_factory=lambda rec, led: logins.append(rec))
            self.assertEqual(rc, 1)
            self.assertEqual(len(acquired), 1)
            self.assertEqual(logins, [])

    def test_cleanup_nonzero_when_a_summons_status_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, _slot_dir = self._setup(tmp, [MOB])
            a = FakeAgent(units=[{"guid": MOB, "entry": 1234, "dead": False}])
            a.units_err = True
            _releases, _acquired, acquire, release = self._env()
            with unittest.mock.patch.object(lease, "list_slots", lambda b, s: (True, self._rows())), \
                 unittest.mock.patch.object(lease, "acquire", acquire), \
                 unittest.mock.patch.object(lease, "release", release), \
                 unittest.mock.patch.object(teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                rc = teardown.cleanup(run_dir, binary="/bin/true",
                                      login_factory=lambda rec, led: self._session(a))
            self.assertEqual(rc, 1)

    def test_cleanup_keeps_a_too_far_summon_as_a_leftover(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, _slot_dir = self._setup(tmp, [MOB])

            class TooFar(FakeAgent):
                def rpc(self, method, params=None, timeout=None):
                    if method == "unit":
                        return {"_err": "unit is too far away"}
                    return FakeAgent.rpc(self, method, params, timeout)

            a = TooFar(units=[])
            _releases, _acquired, acquire, release = self._env()
            with unittest.mock.patch.object(lease, "list_slots", lambda b, s: (True, self._rows())), \
                 unittest.mock.patch.object(lease, "acquire", acquire), \
                 unittest.mock.patch.object(lease, "release", release), \
                 unittest.mock.patch.object(teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                rc = teardown.cleanup(run_dir, binary="/bin/true",
                                      login_factory=lambda rec, led: self._session(a))
            self.assertEqual(rc, 1)   # "too far" is UNKNOWN, not gone: the summon stays a leftover
            rows = [r for r in Ledger(os.path.join(run_dir, "s-1.jsonl")).rows
                    if r.get("kind") == "mob_leftover"]
            self.assertIn(agent_mod.norm_guid(MOB), [g for r in rows for g in r.get("guids") or []])

    def test_a_failure_after_acquire_releases_and_processes_the_next_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, _slot_dir = self._setup(tmp, [MOB])
            ledger = Ledger(os.path.join(run_dir, "ledger.jsonl"))
            slot2_ledger = os.path.join(run_dir, "s-2.jsonl")
            slab2 = Ledger(slot2_ledger)
            slab2.add({"kind": "summon", "guid": MOB_DEAD, "entry": 1234})
            ledger.add({"kind": "lease", "server": "s", "slot": "2", "holder": "holder2",
                        "lease_id": "L2", "dir": "", "ledger": slot2_ledger})
            rows = [{"server": "s", "slot": {"id": "1", "character": "Trlwarriora", "port": 0},
                     "state": "free"},
                    {"server": "s", "slot": {"id": "2", "character": "Trlwarriora", "port": 0},
                     "state": "free"}]
            releases, acquired = [], []

            def acquire(binary, server, slot, holder, ttl=None):
                acquired.append(slot)
                return {"ok": True, "lease_id": "L%d" % (10 + len(acquired)), "raw": ""}

            def release(*args):
                releases.append(args)
                return True

            calls = []

            def login(rec, led, *args, **kwargs):
                calls.append(rec.get("holder"))
                if len(calls) == 1:
                    raise RuntimeError("boom right after acquire")
                return self._session(FakeAgent(units=[{"guid": MOB_DEAD, "dead": True}]))

            with unittest.mock.patch.object(lease, "list_slots", lambda b, s: (True, rows)), \
                 unittest.mock.patch.object(lease, "acquire", acquire), \
                 unittest.mock.patch.object(lease, "release", release), \
                 unittest.mock.patch.object(teardown, "_fresh_login", login):
                rc = teardown.cleanup(run_dir, binary="/bin/true")
            self.assertEqual(rc, 1)                       # the failure is recorded, not raised
            self.assertEqual(len(calls), 2)               # the next slot was still processed
            self.assertEqual([r[2] for r in releases], ["1", "2"])
            self.assertEqual([r[4] for r in releases], ["L11", "L12"])
            self.assertTrue(releases[0][3].startswith("tortoise-trials-"), releases[0])

    def test_cleanup_verifies_a_missing_summon_with_unit_must(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, _slot_dir = self._setup(tmp, [MOB])
            # not in the 60 yd list, but unit_must confirms it dead
            a = FakeAgent(units=[], unit_rows={MOB: {"guid": MOB, "dead": True}})
            _releases, _acquired, acquire, release = self._env()
            with unittest.mock.patch.object(lease, "list_slots", lambda b, s: (True, self._rows())), \
                 unittest.mock.patch.object(lease, "acquire", acquire), \
                 unittest.mock.patch.object(lease, "release", release), \
                 unittest.mock.patch.object(teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                rc = teardown.cleanup(run_dir, binary="/bin/true",
                                      login_factory=lambda rec, led: self._session(a))
            self.assertEqual(rc, 0)

    def test_cleanup_gm_kill_error_is_a_leftover(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, _slot_dir = self._setup(tmp, [MOB])
            a = FakeAgent(units=[{"guid": MOB, "entry": 1234, "dead": False}])
            a.kill_raise = True
            _releases, _acquired, acquire, release = self._env()
            with unittest.mock.patch.object(lease, "list_slots", lambda b, s: (True, self._rows())), \
                 unittest.mock.patch.object(lease, "acquire", acquire), \
                 unittest.mock.patch.object(lease, "release", release), \
                 unittest.mock.patch.object(teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                rc = teardown.cleanup(run_dir, binary="/bin/true",
                                      login_factory=lambda rec, led: self._session(a))
            self.assertEqual(rc, 1)
            rows = [r for r in Ledger(os.path.join(run_dir, "s-1.jsonl")).rows
                    if r.get("kind") == "mob_leftover"]
            self.assertTrue(any("gm_kill error" in (r.get("reason") or "") for r in rows), rows)

    def test_cleanup_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir, _slot_dir = self._setup(tmp, [MOB])
            a = FakeAgent(units=[{"guid": MOB, "entry": 1234, "dead": True}])
            _releases, _acquired, acquire, release = self._env()
            with unittest.mock.patch.object(lease, "list_slots", lambda b, s: (True, self._rows())), \
                 unittest.mock.patch.object(lease, "acquire", acquire), \
                 unittest.mock.patch.object(lease, "release", release), \
                 unittest.mock.patch.object(teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                first = teardown.cleanup(run_dir, binary="/bin/true",
                                         login_factory=lambda rec, led: self._session(a))
                second = teardown.cleanup(run_dir, binary="/bin/true",
                                          login_factory=lambda rec, led: self._session(a))
            self.assertEqual((first, second), (0, 0))


def r_stub():
    class RunStub:
        server = "s"
        warnings = []

        def character_note(self, *args, **kwargs):
            pass

        def log(self, line):
            pass

    return RunStub()


class EvpullTest(unittest.TestCase):
    """EVPULL_PATTERN overrides the diag-4a pattern; the old literal stays the default."""

    def test_evpull_pattern_env(self):
        if not os.environ.get("TRIALS_EVPULL"):
            self.skipTest("TRIALS_EVPULL (the optional event-pull script) is not set")
        script = pathlib.Path(os.environ["TRIALS_EVPULL"])
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            logs = tmp / "logs"
            logs.mkdir()
            walk = tmp / "run"
            walk.mkdir()
            today = subprocess.check_output(["date", "+%Y-%m-%d"], text=True).strip()
            epoch = subprocess.check_output(["date", "-d", "today 00:00", "+%s"], text=True).strip()
            start = subprocess.check_output(["date", "-u", "-d", "@" + epoch, "+%Y-%m-%dT%H:%M:%SZ"], text=True).strip()
            (walk / "START").write_text(start + "\n")
            (walk / "summary.txt").write_text("server testbox9 slot 4\n")
            rows = [
                '%s 12:00:01,Zzzbot,BossTacticsThreatCap,"POINT(0 0)",1,9,60.00,"",""\n' % today,
                '%s 12:00:02,Otherbot,BossTacticsThreatCap,"POINT(0 0)",1,9,60.00,"",""\n' % today,
            ]
            (logs / "bot_events.csv").write_text("".join(rows))
            env = dict(os.environ, EVPULL_LOCAL_LOGDIR=str(logs))
            res = subprocess.run(["bash", str(script), str(walk), "testbox9"],
                                 capture_output=True, text=True, env=env)
            self.assertEqual(res.returncode, 0, res.stderr)
            out = (walk / "ev-testbox9.csv").read_text()
            self.assertIn("Zzzbot", out)
            self.assertIn("Otherbot", out)

            env["EVPULL_PATTERN"] = "Zzzbot"
            res = subprocess.run(["bash", str(script), str(walk), "testbox9"],
                                 capture_output=True, text=True, env=env)
            self.assertEqual(res.returncode, 0, res.stderr)
            out = (walk / "ev-testbox9.csv").read_text()
            self.assertIn("Zzzbot", out)
            self.assertNotIn("Otherbot", out)


if __name__ == "__main__":
    unittest.main()
