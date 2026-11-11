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


# ---- 4. the tea's 300 s cooldown is carried across reps ----------------------------------

TEA_TRIAL = os.path.join(ROOT, "trials", "edge", "dps-rogue-thistle-tea.json")


def load_tea():
    with open(TEA_TRIAL) as f:
        return json.load(f)


def tea_rule(trial):
    return [r for r in trial["rules"] if r["id"] == "DPS-ROG-TEA"][0]


def wall(base_seconds, offset):
    return datetime.datetime.fromtimestamp(base_seconds + offset,
                                           datetime.timezone.utc).isoformat(timespec="milliseconds")


def write_rep(trial_dir, name, base_seconds, duration, drains, teas):
    rep = os.path.join(trial_dir, name)
    os.makedirs(rep)
    setup = {"ok": True, "guids": {"player": PLAYER, "bot": BOT, "mobs": [MOBG]}, "config": {}}
    with open(os.path.join(rep, "setup.json"), "w") as f:
        json.dump(setup, f)
    marks = [{"kind": "fight_start", "wall": wall(base_seconds, 0)}]
    marks += [{"kind": "energy_drain", "wall": wall(base_seconds, t), "to": 0} for t in drains]
    marks.append({"kind": "fight_end", "wall": wall(base_seconds, duration), "ended": "duration"})
    events = [{"t": float(t), "wall": None, "kind": "cast", "src": "b", "src_guid": BOT,
               "dst": "b", "dst_guid": BOT, "src_unit": "$bot", "dst_unit": "$bot",
               "spell_id": 9512, "spell": "Restore Energy", "amount": 0, "dst_hp_pct": 100,
               "src_role": "bot", "dst_role": "bot", "origin": "ta"} for t in teas]
    for fname, rows in (("marks.jsonl", marks), ("events.jsonl", events), ("samples.jsonl", [])):
        with open(os.path.join(rep, fname), "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
    return rep


class TeaCrossRepCooldownTest(unittest.TestCase):
    def trial(self, across=True):
        rule = {"id": "DPS-ROG-TEA", "expect_today": "?",
                "check": {"metric": "latency",
                          "trigger": {"kind": "mark", "mark": "energy_drain"},
                          "response": {"kind": "cast", "src": "$bot", "spell": "Restore Energy"},
                          "within_s": 5, "debounce_responder_s": 300,
                          "debounce_responder_spells": ["Restore Energy"]},
                "pass_if": ">= 0.5", "min_n": 1}
        if across:
            rule["check"]["debounce_responder_across_reps"] = True
        return {"id": "tea", "group": "tanks-melee", "class": "rogue", "spec": "combat",
                "player": {"class": "warrior", "level": 60,
                           "talents": {"tree": "arms", "spells": [16462, 12286]}},
                "bot": {"class": "rogue", "spec": "combat", "role": "dps", "level": 60},
                "situation": {"kind": "tank_damage", "mobs": {"role": "melee", "count": 1,
                                                              "level_delta": 0},
                              "hp_band": [40, 90], "floor_pct": 12},
                "opportunity": {"unit": "$player", "in_combat": True},
                "rules": [rule]}

    def score(self, across=True):
        """rep1 drinks at t=95; rep2 starts 100 s later, so its two early drains see the 300 s."""
        with tempfile.TemporaryDirectory() as tmp:
            tdir = os.path.join(tmp, "run", "tea")
            write_rep(tdir, "rep1", 1_800_000_000.0, 120, [20], [20.1, 95.0])
            write_rep(tdir, "rep2", 1_800_000_100.0, 400, [50, 100, 350], [350.1])
            out = score_mod.score_trial(tdir, self.trial(across))
        return out["verdicts"][0]

    def test_a_drain_inside_the_prior_reps_tea_cooldown_is_no_chance(self):
        # bench-fix-1111: rep2's drains 241 s and 282 s after the rep1 tea were scored as misses
        got = self.score(across=True)
        self.assertEqual((got["verdict"], got["k"], got["n"]), ("PASS", 2, 2), got)
        rep2 = [d for d in got["detail"] if d["rep"] == "rep2"][0]
        self.assertEqual((rep2["k"], rep2["n"]), (1, 1), rep2)

    def test_without_the_option_the_same_reps_score_the_misses(self):
        got = self.score(across=False)
        self.assertEqual((got["verdict"], got["k"], got["n"]), ("FAIL", 2, 4), got)
        rep2 = [d for d in got["detail"] if d["rep"] == "rep2"][0]
        self.assertEqual((rep2["k"], rep2["n"]), (1, 3), rep2)

    def test_the_trial_carries_the_option_and_says_why(self):
        rule = tea_rule(load_tea())
        self.assertTrue(rule["check"]["debounce_responder_across_reps"])
        self.assertIn("item cooldown", rule["why_expect"])

    def test_the_option_is_validated(self):
        bad = self.trial()
        bad["rules"][0]["check"]["debounce_responder_across_reps"] = False
        self.assertTrue(any("debounce_responder_across_reps" in e
                            for e in scenario_mod.validate(bad)))
        missing = self.trial()
        del missing["rules"][0]["check"]["debounce_responder_s"]
        self.assertTrue(any("debounce_responder_across_reps" in e
                            for e in scenario_mod.validate(missing)))


# ---- 5. Shadowform is stripped off the hire before every rep -----------------------------

SHADOW_TRIAL = os.path.join(ROOT, "trials", "healers", "priest-shadow-form-prepull.json")


def load_shadow():
    with open(SHADOW_TRIAL) as f:
        return json.load(f)


class PrepullUnauraTest(unittest.TestCase):
    def situation(self, sit, guard=None, log=None):
        agent = tl.FakeAgent()
        return situations.Situation(agent, {"id": "t", "player": {"level": 40}, "situation": sit},
                                    guard or SpendGuard(Clock()), tl.new_ledger(tempfile.mkdtemp()),
                                    tl.FakeMarks(), tl.StubCollector(),
                                    situations.SitContext(player_guid=PLAYER, bot_name="Helper",
                                                          hire_guid=BOT),
                                    arenas=[], mobs={}, log=log)

    def test_the_hire_is_stripped_before_the_summon(self):
        # reps 2-3 of priest-shadow-form-prepull "passed" only because the form carried over
        order = []

        class Guard:
            def send(self, kind, target=None, *args):
                order.append((kind, target) + tuple(args))
                if kind == "cooldown":
                    return ".cooldown"
                return ".unaura %s" % args[0]

        sit = self.situation({"kind": "tank_damage", "prepull_unaura": [15473],
                              "mobs": {"role": "melee", "count": 1}}, guard=Guard())
        sit.acquire_arena = lambda: {"id": "a1"}
        sit.summon = lambda role, count, near_player=True: order.append(("summon", role, count)) or []
        sit.world_pull = lambda: None
        sit.run()
        self.assertEqual(order, [("unaura", BOT, 15473), ("summon", "melee", 1)])

    def test_a_refused_prepull_line_is_logged_and_not_fatal(self):
        class Refusing:
            def send(self, kind, target=None, *args):
                raise RuntimeError("not in view")

        logs = []
        sit = self.situation({"kind": "tank_damage", "prepull_unaura": [15473]},
                             guard=Refusing(), log=logs.append)
        sit.clear_prepull_auras()
        self.assertTrue(any("refused" in line for line in logs))

    def test_the_trial_strips_shadowform_and_validates(self):
        trial = load_shadow()
        self.assertEqual(trial["situation"]["prepull_unaura"], [15473])
        self.assertEqual(scenario_mod.validate(trial), [])
        for bad in ([], "15473", [0], [True]):
            trial["situation"]["prepull_unaura"] = bad
            self.assertTrue(any("prepull_unaura" in e for e in scenario_mod.validate(trial)), bad)


# ---- 6. the two second-tank PWS trials are regression guards now -------------------------

class TwoTankRegressionGuardTest(unittest.TestCase):
    TRIALS = {"priest-holy-pws-def-arms-2tank": "PRI-PWS-DEF-ARMS-2TANK",
              "priest-holy-pws-bear-bal-2tank": "PRI-PWS-BEAR-BAL-2TANK"}

    def test_the_stale_fail_expectation_is_gone(self):
        for tid, rid in self.TRIALS.items():
            trial = scenario_mod.load_trial(os.path.join(ROOT, "trials", "healers", tid + ".json"))
            rule = [r for r in trial["rules"] if r["id"] == rid][0]
            self.assertEqual(rule["expect_today"], "PASS", tid)
            self.assertEqual(rule["min_reps"], 3, tid)
            self.assertIn("Regression guard", rule["desc"], tid)
            self.assertIn("#1557", rule["why_expect"], tid)
            self.assertEqual(scenario_mod.validate(trial), [], tid)


# ---- 7. a bot tank's nearby fight proves the player visible ------------------------------

class PlayerVisibleNearbyFightTest(unittest.TestCase):
    """bench-fix-1111: a bot tank holds the mob, the player is never touched, and the first 30 s
    are empty -> 5 reps INVALID for nothing (threat cap @lvl50 reps 1/3/4, taunt-off-healer rep1 x2)."""

    def rep(self, tmp, events, samples):
        rep = os.path.join(tmp, "rep1")
        os.makedirs(rep, exist_ok=True)
        for fname, rows in (("events.jsonl", events), ("samples.jsonl", samples)):
            with open(os.path.join(rep, fname), "w") as f:
                for row in rows:
                    f.write(json.dumps(row) + "\n")
        return rep

    def trial(self, role="dps", victim="player"):
        return {"id": "t", "group": "dps", "bot": {"role": role},
                "situation": {"kind": "tank_damage", "victim": victim}}

    def bot_swing(self, t):
        return {"t": float(t), "wall": None, "kind": "damage", "src": "bot", "src_guid": BOT,
                "dst": "mob", "dst_guid": MOBG, "src_unit": "$bot", "dst_unit": "mob:" + MOBG,
                "spell_id": None, "spell": None, "amount": 90, "dst_hp_pct": 95,
                "src_role": "bot", "dst_role": "mob", "origin": "ta"}

    def pos(self, t, unit, guid, x, y, z=0.0):
        return {"t": float(t), "wall": None, "unit": unit, "guid": guid, "hp": 100, "max_hp": 100,
                "power": 0, "max_power": 0, "auras": [], "casting": None, "in_combat": True,
                "target": "", "x": float(x), "y": float(y), "z": float(z)}

    def samples(self, mob_x):
        rows = []
        for t in range(0, 31):
            rows.append(self.pos(t, "player", PLAYER, 0.0, 0.0))
            rows.append(self.pos(t, "bot", BOT, 1.0, 1.0))
            rows.append(self.pos(t, "mob:" + MOBG, MOBG, float(mob_x), 0.0))
        return rows

    def test_a_swing_at_a_mob_beside_the_player_makes_it_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            rep = self.rep(tmp, [self.bot_swing(6.0)], self.samples(2.0))
            got = setup.check_player_visible(rep, self.trial())
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["value"]["bot_attacks_near_player"], 1)
        self.assertEqual(got["value"]["mob_hits_on_player"], 0)

    def test_a_swing_at_a_distant_mob_still_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            rep = self.rep(tmp, [self.bot_swing(6.0)], self.samples(30.0))
            got = setup.check_player_visible(rep, self.trial())
        self.assertFalse(got["ok"], got)
        self.assertEqual(got["value"]["bot_attacks_near_player"], 0)
        self.assertIn(REASON, got["evidence"])

    def test_a_hired_healer_still_needs_a_cast_at_the_player(self):
        # live3: the hidden GM was hit by mobs and healed in none; the new branch must not mask that
        with tempfile.TemporaryDirectory() as tmp:
            rep = self.rep(tmp, [self.bot_swing(6.0)], self.samples(2.0))
            got = setup.check_player_visible(rep, self.trial(role="healer"))
        self.assertFalse(got["ok"], got)

    def test_a_rep_without_samples_keeps_the_old_answer(self):
        # the bench-fix fixtures carry events only: no positions, no new evidence
        with tempfile.TemporaryDirectory() as tmp:
            rep = os.path.join(tmp, "rep1")
            os.makedirs(rep)
            with open(os.path.join(rep, "events.jsonl"), "w") as f:
                f.write(json.dumps(self.bot_swing(6.0)) + "\n")
            got = setup.check_player_visible(rep, self.trial())
        self.assertFalse(got["ok"], got)


if __name__ == "__main__":
    unittest.main()
