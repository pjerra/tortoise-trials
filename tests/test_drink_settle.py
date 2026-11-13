"""Bench-fix-1112 item 3: the ooc window must not open on a stripped setup drink.

A low-mana hire starts drinking during setup; the window's `.unaura all` strips that drink and the
strip can leave the AI asleep in the meal. When a Drink aura was on the hire, the runner waits
(bounded) for the healthy immediate re-drink and opens the window on it; with no setup drink the
window opens at once. Offline: a scripted agent, a recording guard and a fake clock drive
Situation.run_ooc on the real drinks trial. No server is touched.

Run from the tool directory: python3 -I -m unittest discover -s tests
"""

import copy
import os
import pathlib
import sys
import types
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trl import scenario as scenario_mod  # noqa: E402
from trl.live import situations as situations_mod  # noqa: E402

TRIAL_PATH = os.path.join(ROOT, "trials", "edge", "priest-holy-low-mana-drinks.json")
PLAYER = "0x0000000000000001"
HIRE = "0x0000000000000002"
DRINK = 24355


def trial():
    return scenario_mod.load_trial(TRIAL_PATH)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def time(self):
        return 1000.0 + self.t

    def sleep(self, secs):
        self.t += secs


class ScriptedAgent:
    """The hire's aura read: a setup drink until the guard strips it, then a re-drink at `redrink_at`.

    `stripped` flips when the test guard sends the unaura, modelling the server removing the aura.
    """

    def __init__(self, clock, setup_drink=False, redrink_at=None):
        self.clock = clock
        self.setup_drink = setup_drink
        self.redrink_at = redrink_at
        self.stripped = False
        self.reads = 0

    def unit(self, guid=None, **kw):
        self.reads += 1
        ids = []
        if self.setup_drink and not self.stripped:
            ids = [DRINK]
        elif self.redrink_at is not None and self.clock.t >= self.redrink_at:
            ids = [DRINK]
        return {"guid": guid, "auras": [{"spell_id": i} for i in ids]}


class RecordingGuard:
    def __init__(self, agent):
        self.agent = agent
        self.sent = []

    def send(self, kind, target=None, *args):
        self.sent.append((kind, target) + tuple(args))
        if kind == "unaura":
            self.agent.stripped = True


class FakeMarks:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(dict(row))

    def kinds(self, kind):
        return [r for r in self.rows if r.get("kind") == kind]


class FakeCollector:
    def __init__(self):
        self.tick_hooks = []
        self.last_sample = None
        self.lost = ""


def make(setup_drink=False, redrink_at=None, sit_over=None):
    t = copy.deepcopy(trial())
    t["situation"].update(sit_over or {})
    clock = FakeClock()
    agent = ScriptedAgent(clock, setup_drink=setup_drink, redrink_at=redrink_at)
    guard = RecordingGuard(agent)
    marks = FakeMarks()
    logs = []
    sit = situations_mod.Situation(agent, t, guard, None, marks, FakeCollector(),
                                  situations_mod.SitContext(player_guid=PLAYER, bot_name="Helper",
                                                            hire_guid=HIRE),
                                  arenas=[], mobs={}, log=logs.append,
                                  abort=lambda: "")
    return sit, clock, guard, marks, logs


def run(sit, clock):
    fake_time = types.SimpleNamespace(time=clock.time, sleep=clock.sleep)
    with unittest.mock.patch.object(situations_mod, "time", fake_time):
        return sit.run_ooc()


class DrinkSettleTest(unittest.TestCase):
    def test_no_setup_drink_opens_the_window_at_once(self):
        sit, clock, guard, marks, _logs = make(sit_over={"ooc_wait_s": 20})
        out = run(sit, clock)
        self.assertTrue(out["ok"])
        # the unaura went out, the window opened with no settle delay
        self.assertIn(("unaura", PLAYER, "all"), guard.sent)
        self.assertIn(("unaura", HIRE, "all"), guard.sent)
        self.assertEqual(len(marks.kinds("fight_start")), 1)
        self.assertEqual(marks.kinds("ooc_drink_settle_timeout"), [])
        self.assertGreaterEqual(clock.t, 20.0)
        self.assertLess(clock.t, 25.0)

    def test_a_stripped_drink_waits_for_the_redrink(self):
        # Drink during setup, stripped at t=0, back 1.5 s later (the healthy rep2/rep4 shape):
        # the window opens on the re-drink, with no timeout mark.
        sit, clock, guard, marks, logs = make(setup_drink=True, redrink_at=1.5,
                                              sit_over={"ooc_wait_s": 20})
        out = run(sit, clock)
        self.assertTrue(out["ok"])
        self.assertTrue(any("re-drank" in line for line in logs), logs)
        self.assertEqual(marks.kinds("ooc_drink_settle_timeout"), [])
        # opened on the re-drink (~1.5 s), not after the full grace
        self.assertLess(clock.t, situations_mod.DRINK_SETTLE_S + 20.0)
        self.assertGreaterEqual(clock.t, 21.5)

    def test_no_redrink_opens_after_the_grace_with_a_mark(self):
        # the stripped-drink sleep shape: nothing comes back, the window still opens, marked
        sit, clock, guard, marks, logs = make(setup_drink=True, sit_over={"ooc_wait_s": 20})
        out = run(sit, clock)
        self.assertTrue(out["ok"])
        timeouts = marks.kinds("ooc_drink_settle_timeout")
        self.assertEqual(len(timeouts), 1)
        self.assertGreaterEqual(clock.t, situations_mod.DRINK_SETTLE_S + 20.0)
        starts = marks.kinds("fight_start")
        ends = marks.kinds("fight_end")
        self.assertEqual((len(starts), len(ends)), (1, 1))

    def test_the_drink_ids_match_the_trials_uptime_list(self):
        rule = [r for r in trial()["rules"] if r["id"] == "PRI-LOWMANA-DRINK-UPTIME"][0]
        self.assertEqual(set(rule["check"]["aura"]), set(situations_mod.DRINK_AURA_IDS))

    def test_unknown_aura_reads_settle_like_a_drink(self):
        # codex MEDIUM (round 1): `hire_aura_ids()` returning None (a missing/malformed
        # unit read) is unknown, not "no drink". The runner settles (bounded) anyway
        # instead of opening the window at once; the unknown pre-cleanup state retries
        # the read once.
        for broken in ({"guid": HIRE}, {"guid": HIRE, "auras": None},
                       {"guid": HIRE, "auras": [{"nope": DRINK}]},
                       {"guid": HIRE, "auras": [{"spell_id": DRINK}, {"nope": 1}]}):
            clock = FakeClock()
            agent = ScriptedAgent(clock)
            orig = lambda guid=None, _b=dict(broken), **kw: dict(_b)
            calls = {"n": 0}
            def counting(guid=None, **kw):
                calls["n"] += 1
                return orig(guid, **kw)
            agent.unit = counting
            guard = RecordingGuard(agent)
            marks = FakeMarks()
            logs = []
            t = copy.deepcopy(trial())
            t["situation"].update({"ooc_wait_s": 20})
            sit = situations_mod.Situation(agent, t, guard, None, marks, FakeCollector(),
                                         situations_mod.SitContext(player_guid=PLAYER,
                                                                   bot_name="Helper",
                                                                   hire_guid=HIRE),
                                         arenas=[], mobs={}, log=logs.append,
                                         abort=lambda: "")
            out = run(sit, clock)
            self.assertTrue(out["ok"], broken)
            self.assertEqual(len(marks.kinds("ooc_drink_settle_timeout")), 1, broken)
            self.assertGreaterEqual(calls["n"], 2, broken)  # pre-cleanup read + retry
            self.assertGreaterEqual(clock.t, situations_mod.DRINK_SETTLE_S + 20.0, broken)

    def test_a_failing_aura_read_settles_instead_of_opening_at_once(self):
        # an `agent.unit()` RPC exception is unknown too (the runner logs nothing fatal
        # here); the window still opens after the bounded settle, marked.
        clock = FakeClock()
        agent = ScriptedAgent(clock)
        calls = {"n": 0}
        def exploding(guid=None, **kw):
            calls["n"] += 1
            raise RuntimeError("boom")
        agent.unit = exploding
        guard = RecordingGuard(agent)
        marks = FakeMarks()
        logs = []
        t = copy.deepcopy(trial())
        t["situation"].update({"ooc_wait_s": 20})
        sit = situations_mod.Situation(agent, t, guard, None, marks, FakeCollector(),
                                       situations_mod.SitContext(player_guid=PLAYER,
                                                                 bot_name="Helper",
                                                                 hire_guid=HIRE),
                                       arenas=[], mobs={}, log=logs.append,
                                       abort=lambda: "")
        out = run(sit, clock)
        self.assertTrue(out["ok"])
        self.assertEqual(len(marks.kinds("ooc_drink_settle_timeout")), 1)
        self.assertGreaterEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
