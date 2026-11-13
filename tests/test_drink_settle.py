"""Bench-1113 item 3: the ooc window never `.unaura all` over an active Drink/Food.

A low-mana hire can be mid-meal when the window opens; `.unaura all` cuts the meal and a cut meal can
leave the AI asleep in it. When a Drink aura is up the hire keeps its auras: the runner waits (bounded,
MEAL_WAIT_S) for the meal to end and opens the window on it. A meal still up after the wait makes the
repeat INVALID (`meal still active`) and no window is opened: its Drink would still be restoring mana
through the scored seconds. An unreadable aura list is UNKNOWN, not "no meal": it is retried inside the
same wait, and a list that never reads makes the repeat INVALID too (`meal state unknown`). A trial
that starts the window at a mana level gets that level back after the meal. Offline: a scripted agent,
a recording guard and a fake clock drive Situation.run_ooc on the real drinks trial. No server is
touched.

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
MAX_MANA = 2000


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
    """The hire's aura read: a Drink aura until `meal_ends_at` (never when it is None)."""

    def __init__(self, clock, meal_ends_at=None):
        self.clock = clock
        self.meal_ends_at = meal_ends_at
        self.reads = 0

    def unit(self, guid=None, **kw):
        self.reads += 1
        ids = []
        if self.meal_ends_at is None or self.clock.t < self.meal_ends_at:
            ids = [DRINK]
        return {"guid": guid, "max_power": MAX_MANA,
                "auras": [{"spell_id": i} for i in ids]}


class RecordingGuard:
    def __init__(self, agent):
        self.agent = agent
        self.sent = []
        self.refuse = set()

    def send(self, kind, target=None, *args):
        if kind in self.refuse:
            raise RuntimeError("refused %s" % kind)
        self.sent.append((kind, target) + tuple(args))


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


def make(meal_ends_at=None, sit_over=None, agent=None):
    t = copy.deepcopy(trial())
    t["situation"].update(sit_over or {})
    clock = FakeClock()
    agent = agent or ScriptedAgent(clock, meal_ends_at=meal_ends_at)
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


class OocWindowMealTest(unittest.TestCase):
    def test_no_meal_clears_both_and_opens_at_once(self):
        sit, clock, guard, marks, _logs = make(meal_ends_at=0.0, sit_over={"ooc_wait_s": 20})
        out = run(sit, clock)
        self.assertTrue(out["ok"])
        self.assertIn(("unaura", PLAYER, "all"), guard.sent)
        self.assertIn(("unaura", HIRE, "all"), guard.sent)
        self.assertEqual(marks.kinds("ooc_mana_reset"), [])
        self.assertEqual(len(marks.kinds("fight_start")), 1)
        self.assertGreaterEqual(clock.t, 20.0)
        self.assertLess(clock.t, 21.0)

    def test_a_meal_is_waited_out_and_the_hire_is_never_cleared(self):
        # The meal ends at 8 s: the window opens then, the hire never sees `.unaura all`, and the
        # trial's bot.mana_pct is re-applied so the window still starts at the trial's mana.
        sit, clock, guard, marks, logs = make(meal_ends_at=8.0, sit_over={"ooc_wait_s": 20})
        out = run(sit, clock)
        self.assertTrue(out["ok"])
        self.assertNotIn(("unaura", HIRE, "all"), guard.sent)
        self.assertIn(("unaura", PLAYER, "all"), guard.sent)
        self.assertTrue(any("meal ended" in line for line in logs), logs)
        reset = marks.kinds("ooc_mana_reset")
        self.assertEqual(len(reset), 1)
        self.assertEqual(reset[0]["pct"], float(trial()["bot"]["mana_pct"]))
        self.assertIn(("modify_mana", HIRE, int(MAX_MANA * trial()["bot"]["mana_pct"] / 100),
                       MAX_MANA), guard.sent)
        # opened on the meal's end, not after the whole grace
        self.assertGreaterEqual(clock.t, 8.0 + 20.0)
        self.assertLess(clock.t, situations_mod.MEAL_WAIT_S + 20.0)

    def test_a_meal_that_outlasts_the_wait_invalidates_the_repeat(self):
        # Round-1 HIGH: a meal still up after MEAL_WAIT_S must not open the window. The hire's Drink
        # keeps restoring mana, so a window opened on it scores a bot that has nothing to drink for:
        # the repeat is INVALID and no fight_start is recorded (no window, no fight_end).
        sit, clock, guard, marks, logs = make(meal_ends_at=None, sit_over={"ooc_wait_s": 20})
        out = run(sit, clock)
        self.assertFalse(out["ok"])
        self.assertEqual(out["invalid_reason"], "meal still active")
        self.assertEqual(out["ended"], "meal")
        self.assertNotIn(("unaura", HIRE, "all"), guard.sent)
        self.assertEqual(marks.kinds("fight_start"), [])
        self.assertEqual(marks.kinds("fight_end"), [])
        self.assertEqual(marks.kinds("ooc_mana_reset"), [])
        self.assertFalse([row for row in guard.sent if row[0] == "modify_mana"])
        # the whole ooc_wait_s grace is not burned on a window that will not open
        self.assertGreaterEqual(clock.t, situations_mod.MEAL_WAIT_S)
        self.assertLess(clock.t, situations_mod.MEAL_WAIT_S + 20.0)
        self.assertTrue(any("no scoring window" in line for line in logs), logs)

    def test_an_aura_list_that_never_reads_makes_the_repeat_invalid(self):
        # Round-2 HIGH: `hire_aura_ids()` returning None (a missing/malformed unit read) is not
        # "no meal". The hire is not cleared (a strip could cut a drink) and, with no clean read
        # inside the meal wait, no window opens: the meal state was never seen.
        for broken in ({"guid": HIRE}, {"guid": HIRE, "auras": None},
                       {"guid": HIRE, "auras": [{"nope": DRINK}]}):
            clock = FakeClock()

            class Broken(ScriptedAgent):
                def unit(self, guid=None, **kw):
                    self.reads += 1
                    return dict(broken)

            agent = Broken(clock)
            sit, clock, guard, marks, logs = make(agent=agent, sit_over={"ooc_wait_s": 5})
            out = run(sit, clock)
            self.assertFalse(out["ok"], broken)
            self.assertEqual(out["invalid_reason"], "meal state unknown", broken)
            self.assertEqual(out["ended"], "meal", broken)
            self.assertNotIn(("unaura", HIRE, "all"), guard.sent, broken)
            self.assertEqual(marks.kinds("fight_start"), [], broken)
            self.assertEqual(marks.kinds("fight_end"), [], broken)
            self.assertEqual(marks.kinds("ooc_mana_reset"), [], broken)
            self.assertFalse([row for row in guard.sent if row[0] == "modify_mana"], broken)
            # pre-cleanup read + retry, then the meal wait keeps polling for the whole bound
            self.assertGreaterEqual(agent.reads, 3, broken)
            self.assertGreaterEqual(clock.t, situations_mod.MEAL_WAIT_S, broken)
            self.assertLess(clock.t, situations_mod.MEAL_WAIT_S + 5.0, broken)
            self.assertTrue(any("read unknown" in line for line in logs), logs)
            self.assertTrue(any("no readable aura list" in line for line in logs), logs)

    def test_reads_that_fail_and_then_show_no_meal_open_the_window(self):
        # The retry inside the meal wait is what saves the repeat: as soon as a read proves there is
        # no meal, the window opens as usual with the hire's auras left alone.
        clock = FakeClock()

        class Flaky(ScriptedAgent):
            def unit(self, guid=None, **kw):
                self.reads += 1
                if self.reads <= 3:
                    return {"guid": guid, "max_power": MAX_MANA, "auras": None}
                return {"guid": guid, "max_power": MAX_MANA, "auras": []}

        agent = Flaky(clock)
        sit, clock, guard, marks, logs = make(agent=agent, sit_over={"ooc_wait_s": 5})
        out = run(sit, clock)
        self.assertTrue(out["ok"])
        self.assertEqual(out["ended"], "ooc")
        self.assertEqual(len(marks.kinds("fight_start")), 1)
        self.assertEqual(len(marks.kinds("fight_end")), 1)
        self.assertNotIn(("unaura", HIRE, "all"), guard.sent)
        self.assertGreaterEqual(agent.reads, 4)
        # the window still starts at the trial's mana level
        self.assertEqual(len(marks.kinds("ooc_mana_reset")), 1)
        self.assertIn(("modify_mana", HIRE, int(MAX_MANA * trial()["bot"]["mana_pct"] / 100),
                       MAX_MANA), guard.sent)
        self.assertTrue(any("read unknown" in line for line in logs), logs)

    def test_a_refused_cleanup_line_does_not_stop_the_window(self):
        sit, clock, guard, marks, _logs = make(meal_ends_at=0.0, sit_over={"ooc_wait_s": 5})
        guard.refuse = {"unaura"}
        out = run(sit, clock)
        self.assertTrue(out["ok"])
        self.assertEqual(len(marks.kinds("fight_start")), 1)

    def test_the_drink_ids_match_the_trials_uptime_list(self):
        rule = [r for r in trial()["rules"] if r["id"] == "PRI-LOWMANA-DRINK-UPTIME"][0]
        self.assertEqual(set(rule["check"]["aura"]), set(situations_mod.DRINK_AURA_IDS))


if __name__ == "__main__":
    unittest.main()
