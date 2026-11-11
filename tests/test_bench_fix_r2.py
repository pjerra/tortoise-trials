"""Bench fix round 2 (bench-fix-1111): one test class per fix, from the TRIAGE.

Each section names the case that fails first on 4180cde, with the evidence line it comes from.
"""

import datetime
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import test_live as tl  # noqa: E402
from trl import rules as rules_mod  # noqa: E402
from trl import scenario as scenario_mod  # noqa: E402
from trl import score as score_mod  # noqa: E402
from trl.live import gm_guard, setup, situations  # noqa: E402

MOB = "0xF13000000000A001"
MOB2 = "0xF13000000000A002"


def unit(guid, distance, entry=1234):
    return {"guid": guid, "entry": entry, "distance": distance, "hostile": True, "dead": False,
            "level": 36, "health": 500, "max_health": 500}


# ---- 1. the summon claim: the only new unit is claimed at any distance ----
class SummonClaimAnyDistanceTest(unittest.TestCase):
    def test_the_only_new_unit_at_nine_yards_is_claimed(self):
        # bench-fix-1111: 39 unclaimed summons in 14 trials, each one unit of the right entry at
        # 3.27-14.7 yd, and every miss stopped the refills for the rest of the rep
        with tempfile.TemporaryDirectory() as tmp:
            a = tl.FakeAgent()
            a.units_queue = [[], [unit(MOB, 9.0)]]
            led = tl.new_ledger(tmp)
            g = gm_guard.GmGuard(a, led, tl.ctx_for())
            guid = situations.summon_mob(a, g, led, 1234, timeout=1.0, poll=0.01)
            self.assertEqual(guid, MOB.lower())
            self.assertEqual(led.summon_guids(), [MOB.lower()])

    def test_two_new_units_beyond_the_claim_radius_keep_todays_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = tl.FakeAgent()
            a.units_queue = [[], [unit(MOB, 6.0), unit(MOB2, 9.0)]]
            led = tl.new_ledger(tmp)
            g = gm_guard.GmGuard(a, led, tl.ctx_for())
            logs = []
            self.assertIsNone(situations.summon_mob(a, g, led, 1234, timeout=0.2, poll=0.01,
                                                    log=logs.append))
            self.assertEqual(led.summon_guids(), [])
            self.assertEqual(len(led.of_kind("summon_unclaimed")), 1)
            self.assertTrue(any("no unit claimed" in line for line in logs))

    def test_two_new_units_still_claim_the_one_within_three_yards(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = tl.FakeAgent()
            a.units_queue = [[], [unit(MOB, 2.0), unit(MOB2, 9.0)]]
            led = tl.new_ledger(tmp)
            g = gm_guard.GmGuard(a, led, tl.ctx_for())
            self.assertEqual(situations.summon_mob(a, g, led, 1234, timeout=0.2, poll=0.01),
                             MOB.lower())


# ---- 2. the refill and energy_drain markers are stamped before the GM line ----
class Clock:
    """A shared fake wall clock: a GM line spends real time, the runner's marks do not."""

    def __init__(self, start=1_800_000_000.0):
        self.now = float(start)
        self.sends = []

    def iso(self, offset=0.0):
        return datetime.datetime.fromtimestamp(self.now + float(offset),
                                               datetime.timezone.utc).isoformat(timespec="milliseconds")

    def spend(self, seconds):
        self.now += float(seconds)


class ClockMarks:
    """Marks with the steady clock and a recording `add` (no file)."""

    def __init__(self, clock):
        self.clock = clock
        self.rows = []

    def now_iso(self, offset=0.0):
        return self.clock.iso(offset)

    def add(self, row):
        row = dict(row)
        row.setdefault("wall", self.now_iso())
        self.rows.append(row)
        return row

    def excluded(self, start, end, why):
        return self.add({"kind": "excluded", "from": start, "to": end, "why": why})

    def kinds(self, kind):
        return [r for r in self.rows if r.get("kind") == kind]


class SpendGuard:
    """A guard double whose GM line spends `cost` seconds of clock time."""

    def __init__(self, clock, cost=2.0):
        self.clock = clock
        self.cost = cost
        self.sent = []

    def send(self, kind, target=None, *args):
        self.sent.append({"kind": kind, "target": target, "args": args, "before": self.clock.iso()})
        self.clock.spend(self.cost)
        return "sent"


class MarkerBeforeCommandTest(unittest.TestCase):
    def test_the_energy_drain_mark_precedes_the_line(self):
        # rep4 of dps-rogue-thistle-tea: the tea came 64 ms before a mark stamped after the send, and
        # the 300 s responder debounce then ate every later drain ("no triggers")
        trial = {"id": "t", "player": {"level": 60},
                 "situation": {"kind": "tank_damage", "mobs": {"role": "melee", "count": 1},
                               "energy_drain": {"first_at_s": 20, "every_s": 40, "to": 0}}}
        clock = Clock()
        marks = ClockMarks(clock)
        guard = SpendGuard(clock)
        sit = situations.Situation(tl.FakeAgent(), trial, guard, tl.new_ledger(tempfile.mkdtemp()),
                                   marks, tl.StubCollector(),
                                   situations.SitContext(player_guid=tl.PLAYER, bot_name="Helper",
                                                         hire_guid=tl.HIRE),
                                   arenas=[], mobs={})
        sit.fight_start = clock.now
        sit.collector.last_sample = {"bot": {"max_power": 100}}
        sit.drain_tick(clock.now + 20.0)
        self.assertEqual(len(guard.sent), 1)
        drain = marks.kinds("energy_drain")[0]
        self.assertEqual(drain["wall"], guard.sent[0]["before"])   # the pre-send stamp
        self.assertLess(drain["wall"], clock.iso())                # strictly before the line returned

    def test_the_summon_send_wall_is_stamped_before_the_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            agent = tl.FakeAgent()
            agent.units_queue = [[], [unit(MOB, 9.0)]]
            led = tl.new_ledger(tmp)
            guard = gm_guard.GmGuard(agent, led, tl.ctx_for())
            sent = {}
            orig = guard.send

            def send(kind, target=None, *args):
                if kind == "npc_summon":
                    guard.at = clock.iso()
                    clock.spend(5.0)
                return orig(kind, target, *args)

            guard.send = send
            situations.summon_mob(agent, guard, led, 1234, timeout=1.0, poll=0.01,
                                  now_iso=clock.iso, sent=sent)
            self.assertEqual(sent["wall"], guard.at)          # the pre-send stamp
            self.assertLess(sent["wall"], clock.iso())        # and before the claim wait

    def test_the_refill_mark_carries_the_summon_send_wall(self):
        clock = Clock()
        marks = ClockMarks(clock)
        sit = situations.Situation(tl.FakeAgent(), {"id": "t", "player": {"level": 60}},
                                   SpendGuard(clock), tl.new_ledger(tempfile.mkdtemp()), marks,
                                   tl.StubCollector(), situations.SitContext(), arenas=[], mobs={})
        clock.spend(4.0)                     # the "claim" takes 4 s after the send
        sit.last_summon_wall = clock.iso(-4.0)
        row = sit.add_refill_mark(MOB.lower(), every=40)
        self.assertEqual(row["wall"], clock.iso(-4.0))
        self.assertLess(row["wall"], clock.iso())
        self.assertEqual(row["every"], 40)


# ---- 3. the threat cap's own windows, opportunity and min_reps ----------------------------

THREAT_TRIAL = os.path.join(ROOT, "trials", "dps", "dps-mage-threat-cap.json")
PLAYER = tl.PLAYER
BOT = tl.HIRE
MOBG = "0xf13000000000abcd"
REASON = "player not visible to bots"


def load_threat():
    with open(THREAT_TRIAL) as f:
        return json.load(f)


def no_aggro(trial):
    return [r for r in trial["rules"] if r["id"] == "DPS-MAG-THREAT-NO-AGGRO"][0]


def sample(t, guid, hp=1000, max_hp=1000, combat=True):
    return {"t": float(t), "wall": None, "unit": "x", "guid": guid, "hp": hp, "max_hp": max_hp,
            "power": 500, "max_power": 500, "auras": [], "casting": None,
            "in_combat": combat, "target": "", "x": 0.0, "y": 0.0, "z": 0.0}


def hit(t):
    return {"t": float(t), "wall": None, "kind": "damage", "src": "m", "src_guid": MOBG,
            "dst": "b", "dst_guid": BOT, "src_unit": "$mob", "dst_unit": "$bot",
            "spell_id": None, "spell": None, "amount": 10, "dst_hp_pct": 90,
            "src_role": "mob", "dst_role": "bot", "origin": "ta"}


def score_one(tmp, trial, events, samples, marks, name="rep1"):
    rep = os.path.join(tmp, "run", "trial", name)
    os.makedirs(rep, exist_ok=True)
    setup = {"ok": True, "guids": {"player": PLAYER, "bot": BOT, "mobs": [MOBG]},
             "config": {"critical": 20, "low": 50, "medium": 70}}
    with open(os.path.join(rep, "setup.json"), "w") as f:
        json.dump(setup, f)
    for fname, rows in (("samples.jsonl", samples), ("events.jsonl", events),
                        ("marks.jsonl", marks)):
        with open(os.path.join(rep, fname), "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
    return {r["id"]: r for r in score_mod.score_rep(rep, trial)["rules"]}


class ThreatCapTrialShapeTest(unittest.TestCase):
    def test_the_rule_leaves_out_the_opening_and_every_refill(self):
        self.assertEqual(no_aggro(load_threat())["check"]["where"]["not_within_s_after_mark"],
                         [{"kind": "fight_start", "s": 12}, {"kind": "refill", "s": 12}])

    def test_the_opportunity_is_the_mage_and_three_reps_are_required(self):
        trial = load_threat()
        self.assertEqual(trial["opportunity"], {"in_combat": True, "unit": "$bot"})
        self.assertEqual(no_aggro(trial)["min_reps"], 3)

    def test_the_frost_variant_keeps_the_same_windows(self):
        frost = [v for v in load_threat()["variants"] if v["name"] == "frost"][0]
        self.assertNotIn("check", frost["override"]["rules"]["DPS-MAG-THREAT-NO-AGGRO"])
        self.assertIn("min_reps", no_aggro(load_threat()))


class ThreatCapWindowsTest(unittest.TestCase):
    def trial(self):
        return {"id": "t",
                "opportunity": {"in_combat": True, "unit": "$bot"},
                "rules": [{"id": "NO-AGGRO", "expect_today": "?",
                           "check": {"metric": "count",
                                     "where": {"kind": "damage", "src": "$mob", "dst": "$bot",
                                               "not_within_s_after_mark": [
                                                   {"kind": "fight_start", "s": 12},
                                                   {"kind": "refill", "s": 12}]}},
                           "pass_if": "== 0", "min_opportunity_s": 90}]}

    def marks(self):
        return [{"kind": "fight_start", "t": 0},
                {"kind": "refill", "t": 60},
                {"kind": "fight_end", "t": 140, "ended": "duration"}]

    def test_a_hit_inside_the_opening_twelve_seconds_is_not_counted(self):
        # bench-fix-1111 rep1: 3 hits at t=0.5, 1.2 and 2.1 from the opening mob, which had no refill mark
        samples = [sample(t, BOT) for t in range(0, 141)]
        with tempfile.TemporaryDirectory() as tmp:
            got = score_one(tmp, self.trial(), [hit(5.0)], samples, self.marks())
        self.assertEqual(got["NO-AGGRO"]["verdict"], "PASS", got)
        self.assertEqual(got["NO-AGGRO"]["k"], 0)

    def test_a_hit_after_the_refill_window_still_fails(self):
        samples = [sample(t, BOT) for t in range(0, 141)]
        with tempfile.TemporaryDirectory() as tmp:
            got = score_one(tmp, self.trial(), [hit(100.0)], samples, self.marks())
        self.assertEqual(got["NO-AGGRO"]["verdict"], "FAIL", got)
        self.assertEqual(got["NO-AGGRO"]["k"], 1)

    def test_the_guard_counts_the_opportunity_without_the_dropped_windows(self):
        # 100 s in combat minus the opening 12 s = 88 s < 90: the rule cannot PASS on the rest
        samples = [sample(t, BOT) for t in range(0, 101)]
        with tempfile.TemporaryDirectory() as tmp:
            got = score_one(tmp, self.trial(), [], samples, self.marks())
        self.assertEqual(got["NO-AGGRO"]["verdict"], "INCONCLUSIVE", got)
        self.assertIn("opportunity", got["NO-AGGRO"]["reason"])


if __name__ == "__main__":
    unittest.main()
