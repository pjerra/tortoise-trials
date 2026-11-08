"""Tests for the out-of-combat hurt situation (kind ooc_hurt) and the two #1553 round-3 trials.

Offline: a fake clock, a recording guard and a stub collector drive Situation.run_ooc_hurt; the
trial rules are scored through score.score_rep on synthetic repeats. No server is touched.

Run from the tool directory: python3 -I -m unittest discover -s tests
"""

import copy
import math
import os
import pathlib
import sys
import types
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_rules_healers as trh  # noqa: E402
from trl import scenario  # noqa: E402
from trl.live import situations  # noqa: E402

TRIAL_DIR = ROOT / "trials" / "healers"
PLAYER = "0x0000000000000001"
HIRE = "0x0000000000000002"
DEF_ID = "priest-holy-pws-def-arms-ooc"
BATTLE_ID = "priest-holy-pws-battle-ooc-control"
MAX_HP = 1000


def trial(tid):
    return scenario.load_trial(str(TRIAL_DIR / (tid + ".json")))


def hurt_cadence():
    """(hurt_every_s, ooc_wait_s) of the DEF trial, read from its file so a retune does not break the pins."""
    sit = trial(DEF_ID)["situation"]
    return float(sit["hurt_every_s"]), float(sit["ooc_wait_s"])


def grid_ticks(every, wait):
    """The hurt ticks run_ooc_hurt lands in a window of `wait` s: 0, every, 2 * every, ... below `wait`."""
    return [k * every for k in range(int(math.ceil(wait / every)))]


class FakeClock:
    def __init__(self, world):
        self.t = 0.0
        self.world = world

    def time(self):
        return 1000.0 + self.t

    def sleep(self, secs):
        self.t += secs
        self.world.advance(self)


class RecordingGuard:
    def __init__(self, world, clock):
        self.world = world
        self.clock = clock
        self.sent = []

    def send(self, kind, target=None, *args):
        self.sent.append((round(self.clock.t, 1), kind, target) + tuple(args))
        if kind == "modify_hp":
            self.world.victim_hp = int(args[0])
            self.world.healed_at = self.clock.t + self.world.heal_after
        return ".modify hp"


class FakeMarks:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(dict(row))

    def excluded(self, start, end, why):
        self.add({"kind": "excluded", "from": start, "to": end, "why": why})

    def kinds(self, kind):
        return [r for r in self.rows if r.get("kind") == kind]


class FakeCollector:
    def __init__(self):
        self.tick_hooks = []
        self.last_sample = None
        self.lost = ""


class FakeAgent:
    def __init__(self):
        self.casts = []
        self.action_lock = __import__("threading").RLock()

    def cast(self, spell_id, target=None):
        self.casts.append((int(spell_id), target))
        return {}


class World:
    """The sample the collector would hold each second: the priest heals the victim back `heal_after` s later."""

    def __init__(self, collector, heal_after=4.0):
        self.collector = collector
        self.heal_after = heal_after
        self.victim_hp = MAX_HP
        self.healed_at = None
        self.hire_in_combat = lambda t: False
        self.player_in_combat = lambda t: False
        self.auras = lambda t: [71]
        self.hire_present = True

    def advance(self, clock):
        if self.healed_at is not None and clock.t >= self.healed_at:
            self.victim_hp = MAX_HP
            self.healed_at = None
        sample = {"player": {"hp": self.victim_hp, "max_hp": MAX_HP, "pct": 100.0 * self.victim_hp / MAX_HP,
                             "auras": self.auras(clock.t), "in_combat": self.player_in_combat(clock.t),
                             "dead": False},
                  "bot": ({"hp": MAX_HP, "max_hp": MAX_HP, "pct": 100.0, "auras": [],
                           "in_combat": self.hire_in_combat(clock.t)} if self.hire_present else None)}
        self.collector.last_sample = sample
        for hook in list(self.collector.tick_hooks):
            hook(sample)


def make(tid=DEF_ID, **sit_over):
    t = copy.deepcopy(trial(tid))
    t["situation"].update(sit_over)
    collector = FakeCollector()
    world = World(collector)
    clock = FakeClock(world)
    world.advance(clock)                    # a first sample exists before the window opens
    guard = RecordingGuard(world, clock)
    logs = []
    marks = FakeMarks()
    agent = FakeAgent()
    sit = situations.Situation(agent, t, guard, None, marks, collector,
                               situations.SitContext(player_guid=PLAYER, bot_name="Helper", hire_guid=HIRE),
                               arenas=[], mobs={}, log=logs.append)
    sit.arena = lambda: {"id": "a"}
    return sit, world, clock, guard, marks, agent, logs


def run_window(sit, clock):
    fake_time = types.SimpleNamespace(time=clock.time, sleep=clock.sleep)
    sit.collector.last_sample = None
    clock.world.advance(clock)              # the sample the collector holds when the window opens
    with unittest.mock.patch.object(situations, "time", fake_time):
        return sit.run()


class HurtLoopTest(unittest.TestCase):
    def test_the_hurt_loop_sets_health_on_schedule_and_marks_each_set(self):
        # fails without the change: Situation.run has no ooc_hurt branch (it would summon mobs and fight)
        every, wait = hurt_cadence()
        sit, world, clock, guard, marks, _agent, _logs = make()
        out = run_window(sit, clock)
        self.assertEqual(out, {"ok": True, "ended": "ooc"})
        sets = [row for row in guard.sent if row[1] == "modify_hp"]
        # window start, then every hurt_every_s across the ooc_wait_s window (both read from the trial)
        ticks = grid_ticks(every, wait)
        self.assertEqual([row[0] for row in sets], ticks)
        for _t, kind, target, hp, max_hp in sets:
            self.assertEqual((target, max_hp), (PLAYER, MAX_HP))
            self.assertTrue(300 <= hp <= 400, hp)              # inside hp_band [30, 40]
        hurts = marks.kinds("hurt")
        self.assertEqual(len(hurts), len(ticks))
        self.assertTrue(all(30 <= row["hp"] <= 40 for row in hurts))
        kinds = [row["kind"] for row in marks.rows]
        self.assertEqual(kinds[0], "fight_start")
        self.assertTrue(marks.rows[0]["ooc"])
        self.assertEqual(marks.rows[0]["mobs"], [])
        self.assertEqual(kinds[-1], "fight_end")
        self.assertEqual(marks.rows[-1]["ended"], "ooc")
        self.assertEqual([row for row in guard.sent if row[1] == "unaura"], [])    # the stance stays up
        self.assertEqual(sit.collector.tick_hooks, [])                             # the governor hook is gone

    def test_a_tick_is_skipped_and_logged_while_the_hire_is_in_combat(self):
        every, wait = hurt_cadence()
        ticks = grid_ticks(every, wait)
        skip_ticks = ticks[1:3]                               # the second and third ticks: every, 2 * every
        sit, world, clock, guard, marks, _agent, logs = make()
        world.hire_in_combat = lambda t: skip_ticks[0] - every / 2 <= t <= skip_ticks[-1] + every / 2   # those two only
        run_window(sit, clock)
        times = [row[0] for row in guard.sent if row[1] == "modify_hp"]
        self.assertEqual(times, [t for t in ticks if t not in skip_ticks])
        self.assertEqual(len(marks.kinds("hurt")), len(ticks) - 2)
        skipped = [line for line in logs if "hurt tick skipped" in line and "hire in combat" in line]
        self.assertEqual(len(skipped), 2)

    def test_a_tick_is_skipped_while_the_victim_is_in_combat(self):
        every, wait = hurt_cadence()
        ticks = grid_ticks(every, wait)
        sit, world, clock, guard, marks, _agent, logs = make()
        world.player_in_combat = lambda t: t < ticks[2]       # covers the first two ticks: 0 and every
        run_window(sit, clock)
        times = [row[0] for row in guard.sent if row[1] == "modify_hp"]
        self.assertEqual(times[0], ticks[2])
        self.assertTrue([line for line in logs if "victim in combat" in line])

    def test_no_hire_sample_waits_and_retries_next_second(self):
        sit, world, clock, guard, marks, _agent, logs = make(ooc_wait_s=30)
        world.hire_present = False
        clock.sleep(0)                      # refresh the sample without the hire
        original = world.advance

        def appear(c):
            world.hire_present = c.t >= 5
            original(c)
        world.advance = appear
        run_window(sit, clock)
        times = [row[0] for row in guard.sent if row[1] == "modify_hp"]
        self.assertEqual(times[0], 5.0)     # not 20: the wait did not spend the tick
        self.assertTrue([line for line in logs if "hurt tick waits" in line])

    def test_a_victim_already_in_band_is_left_alone(self):
        sit, world, clock, guard, marks, _agent, logs = make(ooc_wait_s=10)
        world.victim_hp = 350
        run_window(sit, clock)
        self.assertEqual([row for row in guard.sent if row[1] == "modify_hp"], [])
        self.assertEqual(marks.kinds("hurt"), [])

    def test_a_rescue_out_of_combat_fires_the_floor_and_is_logged_loudly(self):
        sit, world, clock, guard, marks, _agent, logs = make(ooc_wait_s=10)
        original = world.advance

        def struck(c):                      # something damages the victim to 5 % (floor 12 %) out of combat
            if c.t == 3:
                world.victim_hp = 50
            original(c)
        world.advance = struck
        run_window(sit, clock)
        rescues = [row for row in guard.sent if row[1] == "modify_hp" and row[3] > 50]
        self.assertTrue(rescues)
        self.assertTrue([line for line in logs if "OOC RESCUE FIRED" in line], logs)

    def test_the_stance_is_recast_when_it_reads_down(self):
        sit, world, clock, guard, marks, agent, logs = make(ooc_wait_s=20)
        world.auras = lambda t: [] if t < 8 else [71]
        run_window(sit, clock)
        self.assertEqual(agent.casts[0], (71, PLAYER))
        self.assertLessEqual(len(agent.casts), 2)
        self.assertTrue([line for line in logs if "re-cast" in line])

    def test_a_stance_that_holds_is_not_recast(self):
        sit, world, clock, guard, marks, agent, logs = make(ooc_wait_s=20)
        run_window(sit, clock)
        self.assertEqual(agent.casts, [])

    def test_the_battle_control_recasts_its_own_stance(self):
        sit, world, clock, guard, marks, agent, logs = make(BATTLE_ID, ooc_wait_s=10)
        world.auras = lambda t: []
        run_window(sit, clock)
        self.assertEqual(agent.casts[0][0], 2457)

    def test_a_lost_session_ends_the_window_invalid(self):
        sit, world, clock, guard, marks, _agent, _logs = make()
        sit.collector.lost = "disconnected: test"
        out = run_window(sit, clock)
        self.assertFalse(out["ok"])
        self.assertIn("disconnected", out["invalid_reason"])
        self.assertEqual(marks.rows[-1]["kind"], "fight_end")


class ValidationTest(unittest.TestCase):
    def test_both_new_trials_validate(self):
        for tid in (DEF_ID, BATTLE_ID):
            t = trial(tid)
            self.assertEqual(scenario.validate(t), [], tid)
            self.assertEqual(t["situation"]["kind"], "ooc_hurt")
            # the cadence is read from each file, not pinned: a retune must not break this
            self.assertTrue(8 <= t["situation"]["hurt_every_s"] <= 120, tid)      # the scenario range
            self.assertEqual(t["situation"]["mobs"]["count"], 0)

    def errors(self, **over):
        t = copy.deepcopy(trial(DEF_ID))
        t["situation"].update(over)
        return scenario.validate(t)

    def test_a_bad_hurt_every_s_is_rejected(self):
        for bad in (7, 121, 0, -20, "20", True, None.__class__):
            errs = self.errors(hurt_every_s=bad)
            self.assertTrue([e for e in errs if "hurt_every_s" in e], (bad, errs))

    def test_the_range_edges_are_accepted(self):
        for ok in (8, 120, 20.5):
            self.assertEqual(self.errors(hurt_every_s=ok), [], ok)

    def test_hp_band_is_required_and_checked(self):
        t = copy.deepcopy(trial(DEF_ID))
        del t["situation"]["hp_band"]
        self.assertTrue([e for e in scenario.validate(t) if "hp_band" in e])
        self.assertTrue([e for e in self.errors(hp_band=[0, 40]) if "hp_band" in e])
        self.assertTrue([e for e in self.errors(hp_band=[40, 100]) if "hp_band" in e])

    def test_an_ooc_hurt_with_mobs_or_no_window_is_rejected(self):
        self.assertTrue([e for e in self.errors(mobs={"role": "melee", "count": 1, "level_delta": 0})
                         if "mobs.count 0" in e])
        self.assertTrue([e for e in self.errors(ooc_wait_s=0) if "ooc_wait_s" in e])

    def test_the_kind_is_still_checked(self):
        self.assertTrue([e for e in self.errors(kind="ooc_hurtx") if "situation.kind" in e])


# ---- scoring: the three rules on a synthetic repeat ------------------------------------------

class OocHurtScoringTest(trh.RepMixin, unittest.TestCase):
    def window(self, secs=150, shield_at=(), heal_at=(), auras=(71,), hurt_every=20, in_band_s=12):
        """The player sits at 35 % for in_band_s s after each hurt (and is full otherwise), all out of combat."""
        def hp(t):
            return 350 if (t % hurt_every) < in_band_s else 1000
        samples = trh.series(secs, player_hp=hp, player_auras=list(auras), combat=False)
        events = []
        for t in shield_at:
            events.append(trh.event(t, "cast", "bot", "player", trh.sid("Power Word: Shield"), hp=35))
        for t in heal_at:
            events.append(trh.event(t, "cast", "bot", "player", trh.sid("Heal"), hp=35))
        return events, samples

    def test_a_shield_between_pulls_above_critical_fails_the_rule_and_the_controls_pass(self):
        events, samples = self.window(shield_at=(2.0, 22.0), heal_at=(4.0, 24.0))
        rules, _ = self.score(DEF_ID, events, samples, secs=150)
        self.assertEqual(rules["PRI-PWS-DEF-ARMS-OOC"]["verdict"], "FAIL")
        self.assertEqual(rules["PRI-PWS-DEF-ARMS-OOC"]["value"], 2)
        self.assertEqual(rules["PRI-PWS-DEF-ARMS-OOC-HEALS"]["verdict"], "PASS")
        self.assertEqual(rules["PRI-PWS-DEF-ARMS-OOC-STANCE"]["verdict"], "PASS")

    def test_no_shield_but_heals_passes_after_the_fix(self):
        events, samples = self.window(heal_at=(4.0, 24.0))
        rules, _ = self.score(DEF_ID, events, samples, secs=150)
        self.assertEqual(rules["PRI-PWS-DEF-ARMS-OOC"]["verdict"], "PASS")

    def test_a_priest_that_never_heals_fails_the_heals_control(self):
        events, samples = self.window()
        rules, _ = self.score(DEF_ID, events, samples, secs=150)
        self.assertEqual(rules["PRI-PWS-DEF-ARMS-OOC-HEALS"]["verdict"], "FAIL")

    def test_too_little_in_band_time_is_inconclusive_not_a_pass(self):
        events, samples = self.window(in_band_s=2)          # 8 hurts x ~2 s = 16 s < min_opportunity_s 60
        rules, _ = self.score(DEF_ID, events, samples, secs=150)
        self.assertEqual(rules["PRI-PWS-DEF-ARMS-OOC"]["verdict"], "INCONCLUSIVE")
        self.assertIn("opportunity", rules["PRI-PWS-DEF-ARMS-OOC"]["reason"])

    def test_opportunity_counts_only_out_of_combat_in_band_seconds(self):
        t = trial(DEF_ID)
        min_s = float(trh.rule_of(t, "PRI-PWS-DEF-ARMS-OOC")["min_opportunity_s"])
        events, samples = self.window()                       # 35 %: inside the critical-to-low band, out of combat
        # "$low" and "$critical" resolve through ctx["thresholds"], the way score.py's _ctx builds it
        ctx = {"guids": {"player": trh.PLAYER, "bot": trh.BOT},
               "thresholds": {"critical": 20, "low": 50, "medium": 70}}
        from trl import rules as rules_mod
        got = rules_mod.opportunity_seconds(t, samples, [], ctx)
        self.assertGreaterEqual(got, min_s)
        fighting = [dict(s, in_combat=True) for s in samples]
        self.assertEqual(rules_mod.opportunity_seconds(t, fighting, [], ctx), 0.0)
        high = trh.series(150, player_hp=lambda _t: 600, player_auras=[71], combat=False)   # 60 %: above the low band
        self.assertEqual(rules_mod.opportunity_seconds(t, high, [], ctx), 0.0)

    def test_a_shield_in_combat_does_not_count_for_the_out_of_combat_rule(self):
        events, samples = self.window()
        events.append(trh.event(30.0, "cast", "bot", "player", trh.sid("Power Word: Shield"), hp=35))
        samples = [dict(s, in_combat=True) if (s["guid"] == trh.BOT and 29 <= s["t"] <= 31) else s for s in samples]
        rules, _ = self.score(DEF_ID, events, samples, secs=150)
        self.assertEqual(rules["PRI-PWS-DEF-ARMS-OOC"]["value"], 0)

    def test_a_stance_that_drops_fails_the_stance_precondition(self):
        events, samples = self.window(auras=())
        rules, _ = self.score(DEF_ID, events, samples, secs=150)
        self.assertEqual(rules["PRI-PWS-DEF-ARMS-OOC-STANCE"]["verdict"], "FAIL")

    def test_battle_control_needs_one_out_of_combat_shield(self):
        events, samples = self.window(auras=(2457,), shield_at=(2.0,))
        rules, _ = self.score(BATTLE_ID, events, samples, secs=150)
        self.assertEqual(rules["PRI-PWS-BATTLE-OOC-CONTROL"]["verdict"], "PASS")
        self.assertEqual(rules["PRI-PWS-BATTLE-OOC-CONTROL-STANCE"]["verdict"], "PASS")
        events, samples = self.window(auras=(2457,))
        rules, _ = self.score(BATTLE_ID, events, samples, secs=150)
        self.assertEqual(rules["PRI-PWS-BATTLE-OOC-CONTROL"]["verdict"], "FAIL")

    def test_expectations_are_the_before_after_pair(self):
        self.assertEqual(trh.rule_of(trial(DEF_ID), "PRI-PWS-DEF-ARMS-OOC")["expect_today"], "FAIL")
        self.assertEqual(trh.rule_of(trial(DEF_ID), "PRI-PWS-DEF-ARMS-OOC-HEALS")["expect_today"], "PASS")
        self.assertEqual(trh.rule_of(trial(BATTLE_ID), "PRI-PWS-BATTLE-OOC-CONTROL")["expect_today"], "PASS")


if __name__ == "__main__":
    unittest.main()
