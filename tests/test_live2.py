"""Regression tests for the problems of the second live run (live2, 2026-11-08).

The fixtures in tests/fixtures-live2/ are slices of the real raw-ta lines of the second live
run's run dirs under the run output folder (20261108-035820-before = the
2tank run whose bot_items needed 4 .additem to land one stack; 20261108-040331-before = the
battle-control run): the `.lookup item` row, the `.bot inv` listings with and without the
stack, and the `You changed HP of ...` / `Player not found!` replies of the GM lines.

Root cause shared by bot_items, the governor's floor rescues, the teardown heal and the
revived mob: the core applies CMSG_SET_SELECTION on the map thread and runs a GM chat line on
the world thread, so a line sent right after its selection can run on the selection from
before. RaceAgent below is that server: `target` is applied `lag` rpc calls later, a GM line
acts on the selection in force when it arrives, and only the target field of the player's
own object (the `unit` rpc on the player's guid) shows it. No real server is touched.
"""

import json
import os
import pathlib
import re
import sys
import tempfile
import time
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from trl import scenario, score  # noqa: E402
from trl.live import agent as agent_mod  # noqa: E402
from trl.live import gm_guard, lease, setup, situations, teardown  # noqa: E402
from trl.live.ledger import Ledger  # noqa: E402

FIX = ROOT / "tests" / "fixtures-live2"
PLAYER = "0x000000000000064c"
HIRE = "0x0000000000000655"
MOB = "0xf130000aae3d0df5"
NAME = "Steelbreaker"


def jsonl(name):
    with open(FIX / name) as f:
        return [json.loads(line) for line in f if line.strip()]


class RaceAgent(agent_mod.Agent):
    """A server whose selection lags the client's by `lag` rpc calls."""

    def __init__(self, lag=3, hot=False, selection=PLAYER, hire_stack=0):
        agent_mod.Agent.__init__(self, "/nonexistent-slot")
        self.lag = lag
        self.hot = hot
        self.calls = 0
        self.client_target = selection
        self.server_selection = selection
        self.pending = []
        self.log = []
        self.gm = []                 # (text, the server's selection when the line ran)
        self.stack_on_hire = hire_stack
        self.stack_lost = 0          # what a line gave to whatever else was selected
        self.hp_landed = []          # the unit each .modify hp reached
        self.tbm_calls = []
        self.lookup = jsonl("inv_without_stack.jsonl")[0]
        self.without = jsonl("inv_without_stack.jsonl")[1:]
        self.with_stack = jsonl("inv_with_stack.jsonl")

    # -- the server ------------------------------------------------------------

    def _tick(self):
        self.calls += 1
        due = [p for p in self.pending if p[0] <= self.calls]
        self.pending = [p for p in self.pending if p[0] > self.calls]
        for _when, guid in due:
            self.server_selection = guid

    def push(self, text, kind="system"):
        self.log.append({"seq": len(self.log) + 1, "kind": kind, "text": text})

    def rpc(self, method, params=None, timeout=None):
        params = params or {}
        self._tick()
        if method == "status":
            return {"state": "in_world", "guid": PLAYER, "level": 40, "health": 1189, "max_health": 1189,
                    "power": 0, "max_power": 1000, "target": self.client_target}
        if method == "target":
            self.client_target = params.get("guid")
            self.pending.append((self.calls + self.lag, params.get("guid")))
            return {"guid": params.get("guid")}
        if method == "unit":
            guid = agent_mod.norm_guid(params.get("guid") or "")
            name = params.get("name")
            if guid == PLAYER:
                row = {"guid": PLAYER, "name": "Trlwarriora", "health": 1189, "max_health": 1189,
                       "in_combat": self.hot}
                if self.server_selection:
                    row["target"] = self.server_selection
                return row
            if guid == HIRE or (name or "").lower() == NAME.lower():
                return {"guid": HIRE, "name": NAME, "health": 1567, "max_health": 1567}
            if guid == MOB:
                return {"guid": MOB, "name": "Ridge Stalker Patriarch", "health": 900, "max_health": 2175}
            return {"_err": "no unit"}
        if method == "gm":
            text = params["text"]
            self.gm.append((text, self.server_selection))
            self._line(text)
            return {}
        if method == "tbm":
            command = params["command"]
            self.tbm_calls.append(command)
            since = len(self.log)
            if command.startswith("inv "):
                for row in (self.with_stack if self.stack_on_hire else self.without):
                    self.push(row["text"])
            return {"since": since}
        if method == "events":
            since = int(params.get("since", 0))
            last = len(self.log)
            if since > last:
                return {"events": [], "next": last, "oldest": 1, "dropped": False, "more": False}
            kinds = params.get("kinds")
            rows = [r for r in self.log if r["seq"] > since and (not kinds or r["kind"] in kinds)]
            return {"events": rows, "next": last, "oldest": 1, "dropped": False, "more": False}
        if method == "units":
            return []
        return {}

    def _line(self, text):
        if text.startswith(".lookup item"):
            self.push(self.lookup["text"])
        elif text.startswith(".additem"):
            if self.server_selection == HIRE:
                self.stack_on_hire += int(text.split()[-1])
            else:
                self.stack_lost += int(text.split()[-1])
        elif text.startswith(".modify hp"):
            hp, maxhp = text.split()[-2:]
            landed = self.server_selection
            self.hp_landed.append(landed)
            who = "[Trlwarriora]" if landed == PLAYER else "<creature>"
            self.push("You changed HP of %s to %s/%s." % (who, hp, maxhp))

    def pump(self):
        """Let every select in flight land (what time does)."""
        for _when, guid in self.pending:
            self.server_selection = guid
        self.pending = []

    def adds(self):
        return [t for t, _sel in self.gm if t.startswith(".additem")]


def guard_for(agent, tmp, **ctx):
    base = dict(player_guid=PLAYER, band_low=35, floor_pct=30, hire_name=NAME, hire_guid=HIRE,
                arena_coords=frozenset(), mob_entries=frozenset(), learn_ids=frozenset(),
                aura_ids=frozenset(), item_ids=frozenset(), trial_id="t")
    base.update(ctx)
    guard = gm_guard.GmGuard(agent, Ledger(os.path.join(tmp, "ledger.jsonl")), gm_guard.GmContext(**base))
    clock = [0.0]
    guard.clock = lambda: clock[0]
    guard.sleep = lambda s: clock.__setitem__(0, clock[0] + s)
    return guard


class FixtureShapeTest(unittest.TestCase):
    """The fixtures are the real lines: pin what the tests rely on."""

    def test_the_real_replies_name_the_player_once_and_a_creature_five_times(self):
        texts = [r["text"] for r in jsonl("modify_hp_replies.jsonl") if r["rep"] == "rep1"]
        self.assertEqual(texts[0], "You changed HP of [Trlwarriora] to 564/1189.")
        self.assertEqual(texts[1:], ["You changed HP of <creature> to 564/1189."] * 5)

    def test_the_real_listing_has_the_stack_only_after_a_landed_additem(self):
        self.assertNotIn("|1708|", " ".join(r["text"] for r in jsonl("inv_without_stack.jsonl")))
        self.assertIn("TBM:INV_ITEM|255|34|1708|20|-", [r["text"] for r in jsonl("inv_with_stack.jsonl")])


class SelectionRaceTest(unittest.TestCase):
    def test_bot_items_lands_on_the_hire_not_the_gm(self):
        """live2 bot_items: 'Sweet Nectar x20 short after 2 .additem' -- the lines ran on the GM."""
        with tempfile.TemporaryDirectory() as tmp:
            agent = RaceAgent(lag=3)
            guard = guard_for(agent, tmp, item_ids=frozenset())
            trial = {"bot": {"items": [{"name": "Sweet Nectar", "count": 20}]}}
            out = setup.check_items(agent, trial, guard, guard.ctx, None, NAME, timeout=0.5,
                                    sleep=lambda s: None)
            self.assertTrue(out["ok"], out)
            self.assertEqual(agent.stack_lost, 0, "the stack went to something else than the hire")
            self.assertEqual(agent.stack_on_hire, 20)
            self.assertEqual(agent.adds(), [".additem 1708 20"])
            self.assertIn("added 1x", out["evidence"])

    def test_every_additem_ran_on_the_hire(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = RaceAgent(lag=5)
            guard = guard_for(agent, tmp, item_ids=frozenset({1708}))
            guard.send("additem", HIRE, 1708, 20)
            self.assertEqual(agent.gm[-1], (".additem 1708 20", HIRE))

    def test_a_selection_the_server_never_shows_sends_no_line_and_fails_the_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = RaceAgent(lag=10 ** 6)
            guard = guard_for(agent, tmp)
            trial = {"bot": {"items": [{"name": "Sweet Nectar", "count": 20}]}}
            out = setup.check_items(agent, trial, guard, guard.ctx, None, NAME, timeout=0.5,
                                    sleep=lambda s: None)
            self.assertFalse(out["ok"])
            self.assertIn("selection not confirmed by the server", out["evidence"])
            self.assertEqual(agent.adds(), [], "a line went out on an unconfirmed selection")

    def test_the_rescue_loop_lands_every_heal_on_the_player_in_combat(self):
        """live2 battle-control rep1: 1 heal reached [Trlwarriora], 5 reached <creature>."""
        with tempfile.TemporaryDirectory() as tmp:
            agent = RaceAgent(lag=3, hot=True, selection=MOB)
            guard = guard_for(agent, tmp)
            clock = [100.0]

            class Marks:
                def __init__(self):
                    self.rows = []

                def add(self, row):
                    self.rows.append(row)

                def excluded(self, *args):
                    pass

            marks = Marks()
            gov = situations.Governor(agent, guard, marks, victim="player", band=(35, 60), floor=30,
                                      victim_guid=PLAYER, now=lambda: clock[0])
            for _ in range(6):
                clock[0] += 1.0
                gov.tick({"hp": 325, "max_hp": 1189, "in_combat": True})
            texts = [r["text"] for r in agent.log if r["text"].startswith("You changed HP of")]
            self.assertEqual(len(texts), 6)
            real = jsonl("modify_hp_replies.jsonl")[0]["text"]
            self.assertEqual(texts, [re.sub(r"to \d+/\d+", "to 564/1189", real)] * 6,
                             "the heals did not all name the player: %s" % texts)
            self.assertEqual(agent.hp_landed, [PLAYER] * 6)
            agent.pump()
            self.assertEqual(agent.server_selection, MOB, "the fought mob was not selected again")

    def test_the_teardown_heal_does_not_revive_the_selected_mob(self):
        """live2 rep2 start: 'You changed HP of <creature> to 1189/1189' set the dead mob's life."""
        with tempfile.TemporaryDirectory() as tmp:
            agent = RaceAgent(lag=3, selection=MOB)
            guard = guard_for(agent, tmp)
            ledger = Ledger(os.path.join(tmp, "slot.jsonl"))
            rows = teardown.repeat_cleanup(agent, guard, ledger, NAME, PLAYER, wait_s=0.0)
            heal = [r for r in rows if r["id"] == "healed_full"][0]
            self.assertTrue(heal["ok"], heal)
            self.assertEqual(agent.hp_landed, [PLAYER, HIRE])
            self.assertNotIn(MOB, agent.hp_landed)


class FailClosedTest(unittest.TestCase):
    """The guard sends a selection-based line only after the server showed the selection."""

    class FlakyUnit(RaceAgent):
        """The player's own unit row: `fails` bad answers first (or for ever), `bad` says how."""

        def __init__(self, fails, bad, **kw):
            RaceAgent.__init__(self, **kw)
            self.fails, self.bad, self.reads = fails, bad, 0

        def rpc(self, method, params=None, timeout=None):
            if method == "unit" and agent_mod.norm_guid((params or {}).get("guid") or "") == PLAYER:
                self.reads += 1
                if self.fails is None or self.reads <= self.fails:
                    self._tick()
                    return dict(self.bad)
            return RaceAgent.rpc(self, method, params, timeout)

    def _gm_rpcs(self, agent):
        return [c for c in agent.gm]

    def test_a_missing_player_guid_sends_no_line_even_for_a_self_line(self):
        for kind, target, args in (("additem", HIRE, (1708, 20)), ("unaura", PLAYER, ("all",))):
            with tempfile.TemporaryDirectory() as tmp:
                agent = RaceAgent(lag=0)
                guard = guard_for(agent, tmp, player_guid="", item_ids=frozenset({1708}))
                # a self line is refused earlier (the player cannot be told from "other" without
                # its guid); a hire line reaches the selection and must fail closed there
                with self.assertRaises((gm_guard.GmServerError, gm_guard.GmRefused)):
                    guard.send(kind, target, *args)
                self.assertEqual(agent.gm, [], kind)
                self.assertEqual(agent.client_target, PLAYER, "a select went out: " + kind)

    def test_an_empty_unit_row_sends_no_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.FlakyUnit(None, {}, lag=0)
            guard = guard_for(agent, tmp, item_ids=frozenset({1708}))
            with self.assertRaises(gm_guard.GmServerError):
                guard.send("additem", HIRE, 1708, 20)
            self.assertEqual(agent.gm, [])
            with self.assertRaises(gm_guard.GmServerError):       # a self-targeted line too
                guard.send("unaura", PLAYER, "all")
            self.assertEqual(agent.gm, [])

    def test_a_unit_read_that_fails_twice_then_works_sends_one_line_after_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.FlakyUnit(2, {"_err": "unit: rpc timed out"}, lag=0)
            guard = guard_for(agent, tmp, item_ids=frozenset({1708}))
            guard.send("additem", HIRE, 1708, 20)
            self.assertEqual(agent.gm, [(".additem 1708 20", HIRE)])
            self.assertGreaterEqual(agent.reads, 3)

    def test_a_unit_read_that_fails_for_the_whole_deadline_sends_nothing_and_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.FlakyUnit(None, {"_err": "unit: rpc timed out"}, lag=0)
            guard = guard_for(agent, tmp, item_ids=frozenset({1708}))
            with self.assertRaises(gm_guard.GmServerError) as ctx:
                guard.send("additem", HIRE, 1708, 20)
            self.assertIn("never readable", str(ctx.exception))
            self.assertEqual(agent.gm, [])
            rows = [r for r in guard.ledger.rows if r.get("kind") == "selection_unconfirmed"]
            self.assertEqual(len(rows), 1, rows)
            self.assertIn("never readable", rows[0]["reason"])

    def test_the_next_line_waits_for_its_own_confirmation_after_an_unconfirmed_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            # the previous target is a mob; the restore after each line is not waited for at all
            agent = RaceAgent(lag=3, selection=MOB)
            guard = guard_for(agent, tmp, item_ids=frozenset({1708}))
            guard.restore_wait = 0.0
            guard.send("additem", HIRE, 1708, 20)
            guard.send("additem", HIRE, 1708, 20)
            self.assertEqual([sel for _t, sel in agent.gm], [HIRE, HIRE])


class SummonStateTest(unittest.TestCase):
    def _agent(self, rows, absent):
        class A:
            def units_must(self, **kw):
                return list(rows)

            def unit_must(self, guid=None, name=None):
                if guid in absent:
                    raise agent_mod.RpcError("unit %s: unit %s is not in view" % (guid, guid))
                raise agent_mod.UnitGone("unit %s: no row" % guid)

        return A()

    def _ledger(self, tmp, guids):
        led = Ledger(os.path.join(tmp, "l.jsonl"))
        for g in guids:
            led.add({"kind": "summon", "guid": g, "entry": 2734})
        return led

    def test_a_despawned_summon_is_gone_only_where_the_summons_were_made(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = self._ledger(tmp, [MOB])
            agent = self._agent([], {MOB})
            with self.assertRaises(agent_mod.RpcError):
                situations.summon_states(agent, led)
            self.assertEqual(situations.summon_states(agent, led, absent_is_gone=True), {MOB: "gone"})

    def test_other_rpc_errors_stay_unknown_even_in_the_arena(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = self._ledger(tmp, [MOB])

            class A(self._agent([], set()).__class__):
                def unit_must(self, guid=None, name=None):
                    raise agent_mod.RpcError("unit %s: too far away" % guid)

            with self.assertRaises(agent_mod.RpcError):
                situations.summon_states(A(), led, absent_is_gone=True)

    def test_only_limits_the_states_to_one_repeats_mobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            stale, mine = "0xf130000aae3d0dee", "0xf130000aae3d0df6"
            led = self._ledger(tmp, [stale, mine])
            agent = self._agent([{"guid": stale, "dead": False}, {"guid": mine, "dead": True}], set())
            self.assertEqual(situations.alive_summons(agent, led), [stale])
            self.assertEqual(situations.alive_summons(agent, led, only=[mine]), [])

    def test_refill_ignores_a_leftover_of_an_earlier_repeat(self):
        """live2 rep2: a revived mob of rep1 counted as alive, so nothing was refilled after 20 s."""
        with tempfile.TemporaryDirectory() as tmp:
            stale, mine = "0xf130000aae3d0df5", "0xf130000aae3d0df6"
            led = self._ledger(tmp, [stale, mine])
            agent = self._agent([{"guid": stale, "dead": False}, {"guid": mine, "dead": True}], set())
            sit = situations.Situation.__new__(situations.Situation)
            sit.agent, sit.ledger, sit.summoned = agent, led, [mine]
            self.assertEqual(sit.alive_now(), [], "the stale mob would block the refill")


class CleanupBookkeepingTest(unittest.TestCase):
    def _run(self, tmp, release_ok, slot_state, agent):
        run_dir = os.path.join(tmp, "run")
        os.makedirs(run_dir)
        slab = Ledger(os.path.join(run_dir, "s-1.jsonl"))
        slab.add({"kind": "summon", "guid": MOB, "entry": 2734})
        led = Ledger(os.path.join(run_dir, "ledger.jsonl"))
        led.add({"kind": "lease", "server": "s", "slot": "1", "holder": "h", "lease_id": "L1",
                 "dir": os.path.join(tmp, "gone"), "ledger": os.path.join(run_dir, "s-1.jsonl")})
        led.add({"kind": "session", "server": "s", "slot": "1", "account": "A", "character": "Trlwarriora"})
        led.add({"kind": "arena", "id": "a", "server": "s", "slot": "1", "map": 0,
                 "x": -11926, "y": -1283, "z": 85})
        logs = []

        class Session:
            def __init__(self, a):
                self.agent = a

            def stop_agent(self):
                pass

        # the table is read before the fresh lease (always free) and again after the release
        states = ["free", slot_state]

        def list_slots(binary, server):
            state = states.pop(0) if len(states) > 1 else states[0]
            return True, [{"server": "s", "slot": {"id": "1", "port": 0}, "state": state}]

        with unittest.mock.patch.object(lease, "list_slots", list_slots), \
             unittest.mock.patch.object(lease, "acquire",
                                        lambda *a, **k: {"ok": True, "lease_id": "L9", "raw": ""}), \
             unittest.mock.patch.object(lease, "release", lambda *a, **k: release_ok):
            rc = teardown.cleanup(run_dir, log=logs.append, binary="/bin/true",
                                  login_factory=lambda rec, led_: Session(agent))
        return rc, logs

    def _agent(self, x=-11926.0, y=-1283.0):
        class A(RaceAgent):
            def rpc(self, method, params=None, timeout=None):
                if method == "status":
                    out = RaceAgent.rpc(self, method, params, timeout)
                    out.update({"x": x, "y": y})
                    return out
                if method == "unit" and agent_mod.norm_guid((params or {}).get("guid") or "") == MOB:
                    return {"_err": "unit %s is not in view" % MOB}
                return RaceAgent.rpc(self, method, params, timeout)

        return A(lag=0)

    def test_a_release_that_fails_on_an_already_released_lease_is_not_a_leftover(self):
        """live2: 'fresh lease release: FAILED' / 'LEFT: fresh lease still held' with the slot free."""
        with tempfile.TemporaryDirectory() as tmp:
            rc, logs = self._run(tmp, release_ok=False, slot_state="free", agent=self._agent())
            self.assertEqual(rc, 0, logs)
            self.assertFalse([l for l in logs if "LEFT" in l], logs)
            self.assertTrue([l for l in logs if "already released" in l], logs)

    def test_a_release_that_fails_on_a_slot_still_held_is_a_leftover(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, logs = self._run(tmp, release_ok=False, slot_state="held by x", agent=self._agent())
            self.assertEqual(rc, 1)
            self.assertTrue([l for l in logs if "fresh lease still held" in l], logs)

    def test_a_summon_not_in_view_is_gone_when_cleanup_stands_at_the_arena(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, logs = self._run(tmp, release_ok=True, slot_state="free", agent=self._agent())
            self.assertEqual(rc, 0, logs)
            self.assertFalse([l for l in logs if "summons left" in l or "status unknown" in l], logs)

    def test_a_summon_not_in_view_stays_unknown_away_from_the_arena(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, logs = self._run(tmp, release_ok=True, slot_state="free", agent=self._agent(x=0.0, y=0.0))
            self.assertEqual(rc, 1)
            self.assertTrue([l for l in logs if "summons left" in l], logs)


class FloorReasonTest(unittest.TestCase):
    def test_a_floor_end_names_its_rescues(self):
        """live2 rep1 'cut short (27 s, ended: floor)': six gm_heal rescues, five of them misdirected."""
        if True:
            marks = [{"kind": "setup_ok", "t": 0.0, "wall": "2026-11-08T04:04:09.730+01:00"},
                     {"kind": "fight_start", "t": 11.07, "wall": "2026-11-08T04:04:20.797+01:00"}]
            for i in range(6):
                marks.append({"kind": "gm_heal", "hp": 325, "max_hp": 1189, "pct": 27.3, "to": 564,
                              "t": 37 + i, "wall": "2026-11-08T04:04:%02d.912+01:00" % (46 + i)})
            marks.append({"kind": "fight_end", "ended": "floor", "t": 46.1,
                          "wall": "2026-11-08T04:04:55.828+01:00"})
            reason = score.cut_short_reason(marks, 27.1)
            self.assertEqual(reason, "cut short (27 s, ended: floor after 6 rescues)")
            self.assertEqual(score.cut_short_reason([{"kind": "fight_end", "ended": "wiped"}], 5.0),
                             "cut short (5 s, ended: wiped)")


class RepeatsTest(unittest.TestCase):
    def test_the_four_1553_trials_default_to_four_repeats(self):
        for tid in ("priest-holy-pws-def-arms", "priest-holy-pws-def-arms-2tank",
                    "priest-holy-pws-bear-bal", "priest-holy-pws-bear-bal-2tank"):
            path = ROOT / "trials" / "healers" / (tid + ".json")
            with open(path) as f:
                self.assertEqual(json.load(f).get("repeats"), 4, tid)


if __name__ == "__main__":
    unittest.main()
