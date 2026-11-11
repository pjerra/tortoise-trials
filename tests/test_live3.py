"""Regression tests for the problems of the third live round (live3, 2026-11-08).

The fixtures in tests/fixtures-live3/ are slices of the real files of the runs under the run output
folder: 20261108-044040-before (def-arms-2tank), -045710-before (def-arms; reps
2-4 failed the stance), -050235-before (realmd refused the login: 15 per 300 s) and
-050750-before (battle-control). Root cause of the first four items, from the read-only
investigation: the player was logged in as a hidden GM (VISIBILITY_OFF; `.gm off` does not
unhide), so the bots skipped it (PartyMemberToHeal::Check / PartyMemberValue::Check) and the
priest cast nothing at it in 16 reps. No server is touched.
"""

from datetime import datetime, timezone
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_extra_bots as teb  # noqa: E402
import test_live1 as tl1  # noqa: E402
import test_rules_healers as trh  # noqa: E402
from trl import scenario  # noqa: E402
from trl.live import agent as agent_mod  # noqa: E402
from trl.live import collect, gm_guard, pacing, run, setup, situations  # noqa: E402
from trl.live.ledger import Ledger  # noqa: E402

FIX = ROOT / "tests" / "fixtures-live3"
TRIAL_DIR = ROOT / "trials" / "healers"
PLAYER = tl1.PLAYER
BOT = tl1.BOT
EXTRA = tl1.EXTRA
STALE_MOB = "0xf130000aae3d0e65"        # the mob the player had selected when repeat 1 of run 045710 ended
REASON = "player not visible to bots"


def fx(name):
    with open(FIX / name) as f:
        return json.load(f)


def fx_rows(name):
    with open(FIX / name) as f:
        return [json.loads(line) for line in f if line.strip()]


def trial(tid):
    return scenario.load_trial(str(TRIAL_DIR / (tid + ".json")))


def rep_dir_with(tmp, events_name, extra_events=()):
    rep = os.path.join(tmp, "rep1")
    os.makedirs(rep, exist_ok=True)
    rows = fx_rows(events_name) + list(extra_events)
    with open(os.path.join(rep, "events.jsonl"), "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return rep


# (events fixture, trial, bot casts on the player in the first 30 s, mob hits on the player in the first 30 s)
REAL_REPS = [
    ("ev_045710_def_arms_rep1.jsonl", "priest-holy-pws-def-arms", 0, 17),
    ("ev_050750_battle_rep1.jsonl", "priest-holy-pws-battle-control", 0, 4),
    ("ev_044040_2tank_rep2.jsonl", "priest-holy-pws-def-arms-2tank", 0, 3),
    ("ev_044040_2tank_rep1.jsonl", "priest-holy-pws-def-arms-2tank", 0, 0),
]


# ---------------------------------------------------------------------------------------------
# 1. the player is visible to the bots
# ---------------------------------------------------------------------------------------------

class HiddenPlayerFixtureTest(unittest.TestCase):
    def test_the_real_reps_show_no_cast_at_the_player_and_still_mob_hits(self):
        for name, _tid, casts, hits in REAL_REPS:
            rows = [r for r in fx_rows(name) if 0 <= r["t"] <= 30]
            got_casts = [r for r in rows if r["kind"] in ("cast", "cast_start")
                         and r["src_role"] == "bot" and r["dst_role"] == "player"]
            got_hits = [r for r in rows if r["kind"] == "damage" and r["src_role"] == "mob"
                        and r["dst_role"] == "player"]
            self.assertEqual((len(got_casts), len(got_hits)), (casts, hits), name)

    def test_the_greater_invisibility_aura_is_the_core_s_gm_hide(self):
        gone = [r for r in fx_rows("ev_045710_def_arms_rep1.jsonl")
                if r["kind"] == "aura_off" and r["spell_id"] == 16380]
        self.assertEqual(gone[0]["spell"], "Greater Invisibility")
        self.assertLess(gone[0]["t"], 0)           # removed by the setup `.unaura all`, not by the fight


class GmVisibleSetupTest(unittest.TestCase):
    def agent(self, tmp, replies=(), hidden=True, unhides=True):
        a = tl1.LogAgent(unit_rows={PLAYER: {"guid": PLAYER, "health": 1189, "max_health": 1189, "level": 40,
                                             "auras": [{"spell_id": 16380}] if hidden else []}})

        def on_gm(text):
            if text == ".gm visible on":
                for line in replies:
                    a.push(line)
                if unhides:
                    a.unit_rows[PLAYER]["auras"] = []
        a.on_gm = on_gm
        return a

    def guard(self, a, tmp):
        return gm_guard.GmGuard(a, Ledger(os.path.join(tmp, "l.jsonl")), tl1.ctx_for())

    def test_visible_on_is_in_the_closed_set_and_visible_off_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self.agent(tmp)
            g = self.guard(a, tmp)
            self.assertEqual(g.send("gm_visible_on", None), ".gm visible on")
            self.assertEqual(g.send_text(".gm visible on"), ".gm visible on")
            with self.assertRaises(gm_guard.GmRefused):
                g.send_text(".gm visible off")
            self.assertEqual(a.gm, [".gm visible on", ".gm visible on"])

    def test_visible_on_goes_out_before_gm_off_and_the_row_proves_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self.agent(tmp, replies=["You are now visible to all."])
            row = setup.check_gm_visible(a, self.guard(a, tmp), reply_wait=0.2, aura_wait=0.2, poll=0.05)
            self.assertEqual(a.gm, [".gm visible on", ".gm off"])
            self.assertTrue(row["ok"], row)
            self.assertEqual(row["id"], "gm_visible")
            self.assertEqual(row["value"], {"reply": "You are now visible to all.", "aura_16380": False})

    def test_a_player_still_holding_aura_16380_fails_the_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self.agent(tmp, replies=["You are now visible to all."], unhides=False)
            row = setup.check_gm_visible(a, self.guard(a, tmp), reply_wait=0.2, aura_wait=0.2, poll=0.05)
            self.assertFalse(row["ok"])
            self.assertIn("STILL HELD", row["evidence"])
            self.assertTrue(row["value"]["aura_16380"])

    def test_a_refusal_reply_fails_the_row_even_if_the_aura_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self.agent(tmp, replies=["There is no such command."])
            row = setup.check_gm_visible(a, self.guard(a, tmp), reply_wait=0.2, aura_wait=0.2, poll=0.05)
            self.assertFalse(row["ok"])
            self.assertIn("refusal", row["evidence"])

    def test_no_reply_line_with_the_aura_gone_passes_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self.agent(tmp)
            row = setup.check_gm_visible(a, self.guard(a, tmp), reply_wait=0.2, aura_wait=0.2, poll=0.05)
            self.assertTrue(row["ok"])
            self.assertIn("no reply line", row["evidence"])

    def test_run_setup_sends_visible_on_first_and_lists_the_row_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = teb.RunSetupTest("test_no_extra_bots_output_is_unchanged")
            doc, agent, _led, _ctx = case.run_it(teb.minimal(teb.load("priest-holy-pws-def-arms")), tmp)
            self.assertEqual(agent.gm[:2], [".gm visible on", ".gm off"])
            self.assertEqual(doc["checks"][0]["id"], "gm_visible")

    def test_setup_stops_when_the_player_stays_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self.agent(tmp, unhides=False)
            doc = setup.run_setup(a, trial("priest-holy-pws-def-arms"), self.guard(a, tmp),
                                  Ledger(os.path.join(tmp, "x.jsonl")), tl1.ctx_for())
            self.assertFalse(doc["ok"])
            self.assertIn("STILL HELD", doc["invalid_reason"])


class PlayerVisibleCheckTest(unittest.TestCase):
    def test_every_real_hidden_rep_is_invalid_for_a_healer(self):
        for name, tid, _casts, hits in REAL_REPS:
            with tempfile.TemporaryDirectory() as tmp:
                row = setup.check_player_visible(rep_dir_with(tmp, name), trial(tid))
            self.assertFalse(row["ok"], name)
            self.assertEqual(row["id"], "player_visible")
            self.assertIn(REASON, row["evidence"])
            self.assertEqual(row["value"]["mob_hits_on_player"], hits)

    def test_mob_hits_alone_do_not_make_a_hired_healer_visible(self):
        # def-arms rep1 of run 045710: 17 mob hits on the player in 30 s and not one heal or shield
        with tempfile.TemporaryDirectory() as tmp:
            row = setup.check_player_visible(rep_dir_with(tmp, "ev_045710_def_arms_rep1.jsonl"),
                                             trial("priest-holy-pws-def-arms"))
        self.assertFalse(row["ok"])
        self.assertIn("a healer must cast at the player", row["evidence"])

    def test_a_cast_at_the_player_in_the_first_30_s_makes_it_visible(self):
        real = [r for r in fx_rows("ev_045710_def_arms_rep1.jsonl")
                if r["kind"] == "cast" and r["src_role"] == "bot" and r["dst_role"] == "bot"][0]
        cast = dict(real, dst="Trlwarriora", dst_guid=PLAYER, dst_role="player", dst_unit="$player", t=12.0)
        with tempfile.TemporaryDirectory() as tmp:
            row = setup.check_player_visible(rep_dir_with(tmp, "ev_045710_def_arms_rep1.jsonl", [cast]),
                                             trial("priest-holy-pws-def-arms"))
            self.assertTrue(row["ok"], row)
            self.assertEqual(row["value"]["bot_casts_on_player"], 1)
            late = dict(cast, t=45.0)       # after the 30 s window: not enough
            row = setup.check_player_visible(rep_dir_with(tmp, "ev_045710_def_arms_rep1.jsonl", [late]),
                                             trial("priest-holy-pws-def-arms"))
            self.assertFalse(row["ok"])

    def test_a_bot_that_is_not_a_healer_may_prove_it_by_a_mob_attack(self):
        shadow = trial("priest-shadow-form-prepull")
        self.assertEqual(shadow["bot"]["role"], "dps")
        with tempfile.TemporaryDirectory() as tmp:
            hit = setup.check_player_visible(rep_dir_with(tmp, "ev_045710_def_arms_rep1.jsonl"), shadow)
            none = setup.check_player_visible(rep_dir_with(tmp, "ev_044040_2tank_rep1.jsonl"), shadow)
        self.assertTrue(hit["ok"])
        self.assertFalse(none["ok"])

    def test_with_an_extra_bots_victim_the_player_is_a_bystander_and_a_mob_hit_counts(self):
        control = trial("priest-holy-pws-bot-warrior-control")
        self.assertEqual(control["bot"]["role"], "healer")
        with tempfile.TemporaryDirectory() as tmp:
            hit = setup.check_player_visible(rep_dir_with(tmp, "ev_045710_def_arms_rep1.jsonl"), control)
            none = setup.check_player_visible(rep_dir_with(tmp, "ev_044040_2tank_rep1.jsonl"), control)
        self.assertTrue(hit["ok"])
        self.assertFalse(none["ok"])

    def test_an_ooc_trial_looks_at_its_whole_window(self):
        fort = trial("priest-holy-fort-ooc")
        buff = {"t": 50.0, "kind": "cast", "src_role": "bot", "dst_role": "player", "origin": "ta",
                "spell": "Power Word: Fortitude"}
        with tempfile.TemporaryDirectory() as tmp:
            rep = os.path.join(tmp, "rep")
            os.makedirs(rep)
            with open(os.path.join(rep, "events.jsonl"), "w") as f:
                f.write(json.dumps(buff) + "\n")
            self.assertTrue(setup.check_player_visible(rep, fort)["ok"])

    def test_a_raid_trial_has_no_row_and_a_missing_events_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(setup.check_player_visible(tmp, {"group": "raid"}))
            row = setup.check_player_visible(tmp, trial("priest-holy-pws-def-arms"))
            self.assertFalse(row["ok"])
            self.assertIn(REASON, row["evidence"])

    def test_the_runner_marks_the_rep_invalid_with_the_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run.Run(types.SimpleNamespace(server="s", bind=False, dry_run=False), "", tmp, [])
            rep = rep_dir_with(tmp, "ev_045710_def_arms_rep1.jsonl")
            doc = {"ok": True, "invalid_reason": "", "checks": []}
            r.apply_player_visible(rep, trial("priest-holy-pws-def-arms"), doc)
            self.assertFalse(doc["ok"])
            self.assertEqual(doc["invalid_reason"], REASON)
            with open(os.path.join(rep, "setup.json")) as f:
                saved = json.load(f)
            self.assertEqual(saved["invalid_reason"], REASON)
            self.assertEqual(saved["checks"][-1]["id"], "player_visible")

    def test_every_non_raid_trial_lists_the_check_and_the_validator_requires_it(self):
        for path in sorted(TRIAL_DIR.glob("*.json")):
            t = scenario.load_trial(str(path))
            self.assertIn("player_visible", t["setup_checks"], path.name)
            self.assertEqual(scenario.validate(t), [], path.name)
        t = trial("priest-holy-pws-def-arms")
        t["setup_checks"].remove("player_visible")
        self.assertTrue([e for e in scenario.validate(t) if "player_visible" in e])
        t["setup_checks"].append("player_visible")
        t["setup_checks"].append("player_invisible")
        self.assertTrue([e for e in scenario.validate(t) if "unknown setup check" in e])


# ---------------------------------------------------------------------------------------------
# 2. the rescue floor
# ---------------------------------------------------------------------------------------------

class GuardStub:
    def __init__(self):
        self.sent = []

    def send(self, kind, target, *args):
        self.sent.append((kind, target) + args)


class MarksStub:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)

    def excluded(self, *args):
        pass


def governor(floor, band=(40, 90), victim="player", guid=PLAYER, **kw):
    guard = GuardStub()
    clock = [1000.0]
    gov = situations.Governor(None, guard, MarksStub(), victim=victim, band=band, floor=floor,
                              victim_guid=guid, now=lambda: clock[0], **kw)
    return gov, guard, clock


def feed(gov, clock, pcts, max_hp=1189, combat=True, key=None):
    """Tick the governor with one sample per percent; the index of the first rescue, or None."""
    for i, pct in enumerate(pcts):
        clock[0] += 1.0
        sample = {"hp": int(pct * max_hp / 100.0), "max_hp": max_hp, "in_combat": combat}
        before = len(gov.guard.sent)
        gov.tick({key: sample} if key else sample)
        if len(gov.guard.sent) > before and gov.guard.sent[-1][0] == "modify_hp":
            return i
    return None


class FloorTest(unittest.TestCase):
    FOUR = ["priest-holy-pws-bear-bal", "priest-holy-pws-bear-bal-2tank",
            "priest-holy-pws-def-arms", "priest-holy-pws-def-arms-2tank"]

    def test_the_real_rescue_fired_at_26_to_29_percent_so_the_critical_band_was_never_reached(self):
        # run 045710 rep1: the floor-30 governor healed at 25.7, 27.8, 21.3 and 25.7 % (marks.jsonl gm_heal rows);
        # critical is 20, and the player never went under it
        for name in ("ev_045710_def_arms_rep1.jsonl",):
            lows = [r["dst_hp_pct"] for r in fx_rows(name)
                    if r["dst_role"] == "player" and r.get("dst_hp_pct") is not None and r["t"] >= 0]
            self.assertTrue(lows)
            self.assertGreater(min(lows), 20.0)

    def test_the_old_floor_30_rescues_the_real_fall_at_25_percent(self):
        series = [r for r in fx("player_050750_battle_rep3.json")]
        pcts = [100.0 * r["hp"] / r["max_hp"] for r in series]
        self.assertAlmostEqual(pcts[-1], 25.1, 1)
        gov, guard, clock = governor(30, band=(35, 60))
        self.assertEqual(feed(gov, clock, pcts), len(pcts) - 1)

    def test_floor_12_lets_the_same_fall_reach_the_critical_band(self):
        pcts = [100.0 * r["hp"] / r["max_hp"] for r in fx("player_050750_battle_rep3.json")]
        fall = pcts[-2] - pcts[-1]                       # 5.7 points a second, as recorded
        tail = [pcts[-1] - fall * k for k in range(1, 4)]    # 19.4, 13.7, 8.0: the same fall, going on
        gov, guard, clock = governor(12, band=(35, 60))
        at = feed(gov, clock, pcts + tail)
        self.assertEqual(at, len(pcts) + 2)              # only the sample under 12 % is rescued
        self.assertTrue(any(p <= 20 for p in (pcts + tail)[:at]), "the critical band was not reached")

    def test_the_four_shield_trials_set_floor_12_and_the_rest_keep_the_default(self):
        for path in sorted(TRIAL_DIR.glob("*.json")):
            t = scenario.load_trial(str(path))
            want = 12 if t["id"] in self.FOUR else (12 if t["id"] in ("priest-holy-pws-bot-warrior-control", "priest-holy-pws-def-arms-ooc",
                                                         "priest-holy-pws-battle-ooc-control", "priest-shadow-form-prepull",
                                                         "paladin-holy-heal-latency") else 15 if t["id"] == "priest-holy-heal-latency" else 30)
            self.assertEqual(t["situation"]["floor_pct"], want, t["id"])
            self.assertEqual(scenario.validate(t), [], t["id"])

    def test_the_floor_is_read_by_the_situation_and_the_guard_context(self):
        t = trial("priest-holy-pws-def-arms")
        sit = situations.Situation(None, t, None, None, None, None, situations.SitContext(), arenas=[], mobs={})
        self.assertEqual(sit.floor, 12)
        src = (ROOT / "trl" / "live" / "run.py").read_text()
        self.assertIn('floor_pct=int(sit.get("floor_pct") or 30)', src)

    def test_validator_rejects_a_floor_at_or_above_the_band_low(self):
        t = trial("priest-holy-pws-def-arms")
        for bad in (40, 55, 0, -3):
            t["situation"]["floor_pct"] = bad
            self.assertTrue([e for e in scenario.validate(t) if "floor_pct" in e], bad)

    # -- the player must not die ---------------------------------------------------------------

    def test_the_real_falls_between_two_samples_stay_under_the_floor_of_12(self):
        doc = fx("mob_falls_real.json")
        self.assertEqual(doc["pairs"], 2934)
        self.assertEqual(len(doc["falls_pct_points"]), 153)
        self.assertLess(max(doc["falls_pct_points"]), 12.0)       # 11.94, def-arms rep1 of run 045710
        self.assertGreater(max(doc["falls_pct_points"]), 11.0)    # ... so the margin over floor 12 is thin

    def test_a_bigger_fall_than_ever_seen_is_still_rescued_before_death_once_one_was_seen(self):
        # falls of 11.9 points (the biggest real one) take 60 -> 12.4: above the floor (12) but one more
        # such fall from death; the guard rescues it there, the plain floor would wait for the next
        # sample and die to any hit over 12.4
        pcts = [60.0, 48.1, 36.2, 24.3, 12.4]
        gov, guard, clock = governor(12)
        self.assertEqual(feed(gov, clock, pcts), 4)
        self.assertEqual(gov.early_rescues, 1)
        slow, _g, c2 = governor(12)
        self.assertIsNone(feed(slow, c2, [13.4, 13.3, 13.2, 13.1]))       # no big fall seen: the floor alone waits

    def test_typical_falls_leave_the_critical_band_open(self):
        gov, guard, clock = governor(12)
        pcts = [50, 43, 36, 29, 22, 15, 8]
        self.assertEqual(feed(gov, clock, pcts), 6)
        self.assertEqual(gov.early_rescues, 0)

    def spike_then_small_hits(self, cycles=3):
        """One 12-point fall, then 1-point hits down to the floor, three times (heal to 65 after a rescue)."""
        gov, guard, clock = governor(12, band=(40, 90))
        gov.max_rescues = 10 ** 6
        pct, rescues = 70.0, []
        for cycle in range(cycles):
            seq = [58.0] + [58.0 - k for k in range(1, 60)]          # the spike, then 1 point a second
            for p in seq:
                pct = p
                before = len(guard.sent)
                clock[0] += 1.0
                gov.tick({"hp": int(pct * 1189 / 100.0), "max_hp": 1189, "in_combat": True})
                if len(guard.sent) > before and guard.sent[-1][0] == "modify_hp":
                    rescues.append(round(pct, 1))
                    break
            for _ in range(4):                                  # healed up, quiet for a few seconds
                clock[0] += 1.0
                gov.tick({"hp": int(70 * 1189 / 100.0), "max_hp": 1189, "in_combat": True})
        return gov, rescues

    def test_one_early_spike_does_not_drive_early_rescues_for_the_rest_of_the_rep(self):
        gov, rescues = self.spike_then_small_hits()
        self.assertEqual(gov.early_rescues, 0)
        self.assertEqual(gov.ended, "")                    # the rescue budget is untouched
        self.assertEqual(len(rescues), 3)                  # one floor rescue per cycle, none of them early
        self.assertTrue(all(p < 12 for p in rescues), rescues)

    def test_a_spike_that_never_expires_is_caught_by_this_test(self):
        # the mutation: the old unbounded max_drop (window of ever) rescues at 15 % every cycle
        with unittest.mock.patch.object(situations, "DROP_WINDOW_S", 10 ** 9):
            gov, rescues = self.spike_then_small_hits()
        self.assertGreater(gov.early_rescues, 0)
        self.assertTrue(any(p >= 12 for p in rescues), rescues)

    def test_a_recent_spike_still_rescues_early_and_a_rescue_keeps_the_falls_until_they_age_out(self):
        gov, guard, clock = governor(12)
        self.assertEqual(feed(gov, clock, [60.0, 48.1, 36.2, 24.3, 12.4]), 4)     # inside the 20 s window
        self.assertGreater(gov.max_drop, 11.0)               # the rescue does not clear the window
        clock[0] += situations.DROP_WINDOW_S + 1
        gov.tick({"hp": int(65 * 1189 / 100.0), "max_hp": 1189, "in_combat": True})
        self.assertEqual(gov.max_drop, 0.0)                  # ... it ages out like any other fall

    # -- a hit right after a rescue (Codex: the old skip ignored it) ---------------------------

    HIT_AFTER_RESCUE = [11.0, 65.0, 53.1] + [53.1 - 3.0 * k for k in range(1, 14)]    # rescue, heal, 11.9 hit, 3-point falls

    def run_hit_after_rescue(self, mutate=None):
        gov, guard, clock = governor(12)
        gov.max_rescues = 10 ** 6
        if mutate:
            mutate(gov)
        early_at = None
        for i, pct in enumerate(self.HIT_AFTER_RESCUE):
            clock[0] += 1.0
            before = gov.early_rescues
            gov.tick({"hp": int(pct * 1189 / 100.0), "max_hp": 1189, "in_combat": True})
            if gov.early_rescues > before and early_at is None:
                early_at = i
        return gov, early_at

    def test_a_big_hit_in_the_samples_right_after_a_rescue_is_recorded_and_drives_the_next_early_rescue(self):
        gov, early_at = self.run_hit_after_rescue()
        self.assertEqual(self.HIT_AFTER_RESCUE[early_at] > 12.0, True)      # above the floor: an EARLY rescue
        self.assertAlmostEqual(self.HIT_AFTER_RESCUE[early_at], 14.1, 1)
        self.assertGreater(gov.early_rescues, 0)

    def test_mutation_skipping_samples_after_a_rescue_is_caught(self):
        def skip_after_rescue(gov):               # the old behaviour: skip_drop = 2 after every rescue
            real_floor, real_tick, st = gov._floor, gov.tick, {"skip": 0}

            def floor(*a, **k):
                real_floor(*a, **k)
                st["skip"] = 3                 # this tick + the next two samples (the old skip_drop = 2)

            def tick(sample):
                kept = list(gov.falls)
                out = real_tick(sample)
                if st["skip"] > 0:
                    st["skip"] -= 1
                    gov.falls = kept
                    gov.max_drop = max([f for (_t, f) in kept] or [0.0])
                return out
            gov._floor, gov.tick = floor, tick
        gov, early_at = self.run_hit_after_rescue(skip_after_rescue)
        # the 11.9-point hit is swallowed: no early rescue at 14.1, the plain floor waits for the next sample
        self.assertLess(gov.max_drop, 11.0)
        self.assertNotAlmostEqual(self.HIT_AFTER_RESCUE[early_at] if early_at is not None else 0, 14.1, 1)

    def test_mutation_clearing_the_window_at_a_rescue_is_caught(self):
        def clear_at_rescue(gov):
            real = gov._floor

            def floor(*a, **k):
                real(*a, **k)
                gov.falls = []
            gov._floor = floor
        # the rescue at the head of HIT_AFTER_RESCUE has no earlier falls, so build one that does
        gov, guard, clock = governor(12)
        clear_at_rescue(gov)
        feed(gov, clock, [60.0, 48.1, 36.2, 24.3, 12.4])                      # early rescue: window cleared by the mutant
        self.assertEqual(gov.falls, [])
        clean, _g, c2 = governor(12)
        feed(clean, c2, [60.0, 48.1, 36.2, 24.3, 12.4])
        self.assertNotEqual(clean.falls, [])                                  # the real governor keeps them

    def test_a_fall_older_than_the_window_is_forgotten(self):
        gov, guard, clock = governor(12)
        feed(gov, clock, [60.0, 48.1])
        self.assertAlmostEqual(gov.max_drop, 11.9, 1)
        clock[0] += situations.DROP_WINDOW_S + 1
        gov.tick({"hp": int(47 * 1189 / 100.0), "max_hp": 1189, "in_combat": True})
        self.assertLess(gov.max_drop, 2.0)                 # the 11.9-point fall is gone

    def test_the_governor_s_own_damage_is_not_counted_as_a_mob_fall(self):
        gov, guard, clock = governor(12, band=(40, 90))
        clock[0] += 10
        gov.tick({"hp": 1189, "max_hp": 1189, "in_combat": True})        # full life: a .damage to the middle
        self.assertEqual(guard.sent[0][0], "damage")
        clock[0] += 1
        gov.tick({"hp": 773, "max_hp": 1189, "in_combat": True})         # the 35-point fall that line made
        self.assertEqual(gov.max_drop, 0.0)

    def test_the_real_fall_distribution_never_kills_a_floor_12_player(self):
        import random
        falls = fx("mob_falls_real.json")["falls_pct_points"]
        rng = random.Random(1553)
        for seed in range(20):
            rng = random.Random(seed)
            gov, guard, clock = governor(12, band=(40, 90))
            gov.max_rescues = 10 ** 6
            pct, lowest = 65.0, 100.0
            for _ in range(3000):
                pct = max(0.0, pct - rng.choice(falls) * rng.choice((0, 0, 1, 1, 1)))
                lowest = min(lowest, pct)
                before = len(guard.sent)
                clock[0] += 1.0
                gov.tick({"hp": int(pct * 1189 / 100.0), "max_hp": 1189, "in_combat": True})
                if len(guard.sent) > before and guard.sent[-1][0] == "modify_hp":
                    pct = guard.sent[-1][2] * 100.0 / 1189.0
            self.assertGreater(lowest, 0.0, "seed %d: the player died" % seed)


class RescueSimulationTest(unittest.TestCase):
    """20 seeds x 3000 steps with the 153 real falls; the variant lands a big hit right after every rescue."""

    def simulate(self, seed, hit_after_rescue):
        import random
        falls = fx("mob_falls_real.json")["falls_pct_points"]
        biggest = sorted(falls)[-15:]
        rng = random.Random(seed)
        gov, guard, clock = governor(12, band=(40, 90))
        gov.max_rescues = 10 ** 6
        pct, lowest, since_rescue = 65.0, 100.0, 99
        for _ in range(3000):
            if hit_after_rescue and since_rescue in (1, 2):
                pct -= rng.choice(biggest)                 # a real big hit in the old skip window
            else:
                pct -= rng.choice(falls) * rng.choice((0, 0, 1, 1, 1))
            pct = max(0.0, pct)
            lowest = min(lowest, pct)
            since_rescue += 1
            before = len(guard.sent)
            clock[0] += 1.0
            gov.tick({"hp": int(pct * 1189 / 100.0), "max_hp": 1189, "in_combat": True})
            if len(guard.sent) > before and guard.sent[-1][0] == "modify_hp":
                pct = guard.sent[-1][2] * 100.0 / 1189.0
                since_rescue = 0
        return lowest, gov.early_rescues

    def test_no_death_in_20_seeds_plain_and_with_hits_right_after_rescues(self):
        for hit in (False, True):
            for seed in range(20):
                lowest, _early = self.simulate(seed, hit)
                self.assertGreater(lowest, 0.0, "seed %d hit_after_rescue=%s: the player died" % (seed, hit))


# ---------------------------------------------------------------------------------------------
# 3. the player's off-hand
# ---------------------------------------------------------------------------------------------

class OffhandTest(unittest.TestCase):
    ARMS = ["priest-holy-pws-def-arms", "priest-holy-pws-def-arms-2tank", "priest-holy-pws-forced-role",
            "priest-holy-pws-battle-control", "priest-holy-pws-bot-warrior-control"]

    def test_the_agent_has_no_equipment_read_so_the_off_hand_is_unreadable_never_empty(self):
        a = tl1.LogAgent()          # answers an unknown method with an error, like the real agent
        info = setup.read_offhand(a)
        self.assertEqual((info["readable"], info["item_entry"]), (False, None))
        row = setup.check_player_no_shield(info)
        self.assertTrue(row["ok"])
        self.assertEqual(row["value"], "unreadable")
        self.assertTrue(row["evidence"].startswith("UNVERIFIED"))

    def test_a_shield_in_the_off_hand_fails_the_check(self):
        class Eq(tl1.LogAgent):
            def rpc(self, method, params=None, timeout=None):
                if method == "equipment":
                    return [{"slot": 15, "item_entry": 2025}, {"slot": 16, "item_entry": 2362}]
                return tl1.LogAgent.rpc(self, method, params, timeout)
        info = setup.read_offhand(Eq())
        self.assertEqual((info["readable"], info["item_entry"]), (True, 2362))
        row = setup.check_player_no_shield(info)
        self.assertFalse(row["ok"])
        self.assertIn("2362", row["evidence"])

    def test_an_empty_off_hand_passes(self):
        class Eq(tl1.LogAgent):
            def rpc(self, method, params=None, timeout=None):
                if method == "equipment":
                    return [{"slot": 15, "item_entry": 2025}]
                return tl1.LogAgent.rpc(self, method, params, timeout)
        row = setup.check_player_no_shield(setup.read_offhand(Eq()))
        self.assertTrue(row["ok"])
        self.assertIsNone(row["value"])

    def test_the_off_hand_is_recorded_in_setup_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = teb.RunSetupTest("test_no_extra_bots_output_is_unchanged")
            doc, _a, _l, _c = case.run_it(teb.minimal(teb.load("priest-holy-pws-def-arms")), tmp)
        self.assertIn("player_offhand", doc)
        self.assertEqual(doc["player_offhand"]["slot"], 16)

    def test_a_bot_s_off_hand_is_read_from_its_real_inv_listing(self):
        with open(ROOT / "tests" / "fixtures-live1" / "solo_rep1_inv_and_lookup.jsonl") as f:
            rows = [json.loads(line) for line in f]
        a = tl1.LogAgent()
        begin = [r["text"] for r in rows if r["text"].startswith("TBM:INV_BEGIN")][0]
        name = begin.split("|")[1]

        def on_tbm(command):
            if command.startswith("inv "):
                for r in rows:
                    if r["text"].startswith("TBM:INV"):
                        a.push(r["text"])
        a.on_tbm = on_tbm
        info = setup.read_bot_offhand(a, name)
        self.assertEqual((info["readable"], info["item_entry"]), (True, 55336))     # TBM:INV_EQ|16|55336
        self.assertFalse(setup.check_extras_no_shield(a, [name])["ok"])

    def test_every_arms_warrior_trial_lists_player_no_shield(self):
        for tid in self.ARMS:
            self.assertIn("player_no_shield", trial(tid)["setup_checks"], tid)
        self.assertNotIn("player_no_shield", trial("priest-holy-pws-bear-bal")["setup_checks"])


# ---------------------------------------------------------------------------------------------
# 4. the control with a hired bot warrior
# ---------------------------------------------------------------------------------------------

EXTRA_SETUP = {"guids": {"player": trh.PLAYER, "bot": trh.BOT, "extras": [EXTRA], "mobs": [trh.MOB]}}


def extra_event(t, hp):
    return {"t": float(t), "wall": None, "kind": "cast", "src": "bot", "src_guid": trh.BOT, "dst": "extra",
            "dst_guid": EXTRA, "src_unit": "$bot", "dst_unit": "$extra1", "spell_id": trh.PWS,
            "spell": "Power Word: Shield", "amount": None, "dst_hp_pct": hp, "src_role": "bot",
            "dst_role": "extra", "origin": "ta"}


def extra_series(secs, hp_pct=35, aura=(2457,)):
    out = []
    for t in range(secs + 1):
        out.append(trh.sample(t, trh.PLAYER, hp=800))
        out.append(trh.sample(t, trh.BOT, hp=1000))
        out.append(trh.sample(t, EXTRA, hp=hp_pct * 10, auras=aura))
    return out


class BotWarriorControlTest(trh.RepMixin, unittest.TestCase):
    TID = "priest-holy-pws-bot-warrior-control"

    def test_the_trial_is_the_battle_control_plus_a_forced_dps_arms_warrior(self):
        t = trial(self.TID)
        self.assertEqual(scenario.validate(t), [])
        self.assertEqual(t["extra_bots"], [{"class": "warrior", "spec": "arms", "role": "dps", "level": 40, "items": []}])
        self.assertEqual((t["player"]["stance"], t["player"]["talents"]["tree"]), (2457, "arms"))
        self.assertEqual(t["situation"]["victim"], "extra1")
        self.assertEqual(t["situation"]["hp_band"], [21, 50])
        self.assertEqual(t["opportunity"], {"unit": "$extra1", "hp_lt": "$low", "hp_gt": "$critical", "in_combat": True})
        rule = trh.rule_of(t, "PRI-PWS-BOT-WARRIOR-CONTROL")
        self.assertEqual((rule["expect_today"], rule["pass_if"]), ("PASS", ">= 1"))
        self.assertEqual(rule["check"]["where"]["dst"], "$extra1")
        for cid in ("player_visible", "player_no_shield", "extra_bots_no_shield"):
            self.assertIn(cid, t["setup_checks"])

    def test_one_shield_on_the_bot_in_the_band_passes_and_none_fails(self):
        samples = extra_series(200)
        good = self.verdict(self.TID, "PRI-PWS-BOT-WARRIOR-CONTROL", [extra_event(30, 35)], samples, setup=EXTRA_SETUP)
        bad = self.verdict(self.TID, "PRI-PWS-BOT-WARRIOR-CONTROL", [], samples, setup=EXTRA_SETUP)
        above = self.verdict(self.TID, "PRI-PWS-BOT-WARRIOR-CONTROL", [extra_event(30, 80)], samples, setup=EXTRA_SETUP)
        self.assertEqual((good["verdict"], good["value"]), ("PASS", 1))
        self.assertEqual(bad["verdict"], "FAIL")
        self.assertEqual(above["verdict"], "FAIL")         # a shield above the low band is not the control's case

    def test_the_battle_stance_precondition_reads_the_bot_s_own_aura(self):
        held = self.verdict(self.TID, "PRI-PWS-BOT-WARRIOR-CONTROL-STANCE", [], extra_series(200), setup=EXTRA_SETUP)
        lost = self.verdict(self.TID, "PRI-PWS-BOT-WARRIOR-CONTROL-STANCE", [], extra_series(200, aura=(71,)),
                            setup=EXTRA_SETUP)
        self.assertEqual((held["verdict"], lost["verdict"]), ("PASS", "FAIL"))

    def test_victim_extra1_needs_an_extra_bot_and_is_the_only_new_victim(self):
        t = trial(self.TID)
        t["situation"]["victim"] = "extra2"
        self.assertTrue([e for e in scenario.validate(t) if "victim" in e])
        t2 = trial("priest-holy-pws-def-arms")
        t2["situation"]["victim"] = "extra1"
        self.assertTrue([e for e in scenario.validate(t2) if "victim" in e])

    def test_the_situation_governs_extra1_and_still_pulls_with_the_player(self):
        t = trial(self.TID)
        ctx = situations.SitContext(player_guid=PLAYER, bot_name="Tira", hire_guid=BOT,
                                    guids={"extras": [EXTRA]}, extra_names=("Wally",))
        sit = situations.Situation(None, t, None, None, None, None, ctx, arenas=[], mobs={})
        self.assertEqual(sit.victim_target_guid(), EXTRA)
        self.assertTrue(sit.player_pulls())
        self.assertEqual(sit.floor, 12)
        solo = situations.Situation(None, trial("priest-holy-pws-def-arms"), None, None, None, None,
                                    situations.SitContext(player_guid=PLAYER), arenas=[], mobs={})
        self.assertEqual(solo.victim_target_guid(), PLAYER)

    def test_the_governor_drops_the_bot_into_the_band_and_rescues_it_under_the_floor(self):
        gov, guard, clock = governor(12, band=(21, 50), victim="extra1", guid=EXTRA)
        clock[0] += 10
        gov.tick({"extra1": {"hp": 1500, "max_hp": 1500, "in_combat": True}})       # 100 %: down to the middle
        self.assertEqual(guard.sent[-1], ("modify_hp", EXTRA, 532, 1500))
        gov.tick({"extra1": {"hp": 100, "max_hp": 1500, "in_combat": True}})        # 6.7 %: rescue
        self.assertEqual(guard.sent[-1][:2], ("modify_hp", EXTRA))
        self.assertGreater(guard.sent[-1][2], 0.21 * 1500)

    def test_the_sampler_reports_each_extra_for_the_governor(self):
        with tempfile.TemporaryDirectory() as tmp:
            unit = {"guid": EXTRA, "health": 700, "max_health": 1400, "power": 0, "max_power": 0,
                    "auras": [{"spell_id": 2457}], "in_combat": True, "x": 1, "y": 2, "z": 3, "level": 40}
            status = {"state": "in_world", "guid": PLAYER, "level": 40, "health": 1000, "max_health": 1189,
                      "power": 0, "max_power": 1000, "target": "", "x": 0, "y": 0, "z": 0}
            agent = tl1.LogAgent(status=status, units=[unit], unit_rows={PLAYER: {"guid": PLAYER, "auras": []}})
            marks = collect.Marks(os.path.join(tmp, "m.jsonl"))
            marks.start()
            col = collect.Collector(agent, tmp, marks, {"player": PLAYER, "bot": BOT, "extras": [EXTRA], "mobs": []},
                                    None, {})
            col.tick_hooks.append(lambda sample: col._stop.set())
            col._sample()
            self.assertEqual(col.last_sample["extra1"]["pct"], 50.0)
            self.assertEqual(col.last_sample["extra1"]["auras"], [2457])
            self.assertTrue(col.last_sample["extra1"]["in_combat"])


# ---------------------------------------------------------------------------------------------
# 5. the solo Defensive Stance trial is not a before case
# ---------------------------------------------------------------------------------------------

class DefArmsExpectationTest(unittest.TestCase):
    def test_def_arms_expects_pass_for_both_builds_and_says_why(self):
        rule = trh.rule_of(trial("priest-holy-pws-def-arms"), "PRI-PWS-DEF-ARMS")
        self.assertEqual(rule["expect_today"], "PASS")
        why = rule["why_expect"]
        for needle in ("NOT a before-case", "t1106g", "PASS for both builds", "HasOtherTank", "AiFactory.cpp:503"):
            self.assertIn(needle, why)

    def test_the_two_tank_trial_is_a_regression_guard_now(self):
        # #1553 is fixed (TortoiseBots 5f57e84f2, PR #1557): the stale FAIL was what bench-fix-1111
        # reported as "expected FAIL, got PASS"
        rule = trh.rule_of(trial("priest-holy-pws-def-arms-2tank"), "PRI-PWS-DEF-ARMS-2TANK")
        self.assertEqual(rule["expect_today"], "PASS")
        self.assertIn("#1557", rule["why_expect"])
        self.assertIn("Regression guard", rule["desc"])


# ---------------------------------------------------------------------------------------------
# 6. login pacing
# ---------------------------------------------------------------------------------------------

def utc_epoch(year, month, day, hour, minute, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).timestamp()


T_1410 = utc_epoch(2026, 11, 8, 14, 10)      # mid-hour, so the hour-start guard does not apply


class FakeTime:
    def __init__(self, now=T_1410):
        self.now = now

    def clock(self):
        return self.now

    def sleep(self, secs):
        self.now += secs


class LoginPacingTest(unittest.TestCase):
    def test_the_real_refusal_is_realmd_s_15_per_300_s(self):
        text = (FIX / "realmd_rate_limit.out").read_text()
        self.assertIn("15 per 300 s", text)
        self.assertEqual(pacing.LOGIN_LIMIT, 12)
        self.assertEqual(pacing.LOGIN_WINDOW_S, 180.0)
        self.assertEqual(pacing.HOUR_GUARD_END_S, 185.0)

    def test_the_real_runs_logged_in_more_often_than_their_sessions_show(self):
        rows = fx("ledger_logins.json")
        self.assertEqual(len([r for r in rows if r["kind"] == "session"]), 6)    # 3 runs x 2 session rows
        self.assertTrue([r for r in rows if r["reason"] == "error: LoginRefused"])
        # run 050235 was refused 5 minutes after 045710 began; teardown logs in too (3 per run in the .out files)

    def burst(self, n, pacer, ft):
        stamps = []
        for i in range(n):
            pacer.acquire("login %d" % i)
            stamps.append(ft.now)
        return stamps

    def test_a_burst_never_puts_more_than_12_distinct_accounts_in_any_180_s_window(self):
        ft = FakeTime()
        logs = []
        pacer = pacing.LoginPacer(clock=ft.clock, sleep=ft.sleep, log=logs.append)
        stamps = self.burst(30, pacer, ft)       # 13 trials x 2 logins, and a few relogins
        for i, t in enumerate(stamps):
            window = [s for s in stamps if t - 180.0 < s <= t]
            self.assertLessEqual(len(window), 12, (i, len(window)))
        self.assertTrue(logs)
        self.assertIn("waiting", logs[0])
        self.assertEqual(stamps[11], stamps[0])           # the first 12 go straight through
        self.assertGreater(stamps[12] - stamps[0], 180.0)

    def test_twelve_distinct_accounts_never_wait_and_the_thirteenth_does(self):
        ft = FakeTime()
        pacer = pacing.LoginPacer(clock=ft.clock, sleep=ft.sleep)
        self.burst(12, pacer, ft)
        self.assertEqual(pacer.waits, [])
        pacer.acquire("login 13")
        self.assertEqual(len(pacer.waits), 1)
        self.assertEqual(pacer.waits[0][0], "login 13")
        self.assertEqual(pacer.waits[0][1], 182.0)     # the 12 all came at once: the first ages out 182 s later

    def test_logins_spaced_out_over_the_window_never_wait(self):
        ft = FakeTime()
        pacer = pacing.LoginPacer(clock=ft.clock, sleep=ft.sleep)
        for _ in range(40):
            pacer.acquire("x")
            ft.now += 30.0                      # 10 a window
        self.assertEqual(pacer.waits, [])

    def test_two_processes_share_the_window_through_the_state_file(self):
        ft = FakeTime()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "logins.json")
            first = pacing.LoginPacer(clock=ft.clock, sleep=ft.sleep, state_path=path)
            self.burst(12, first, ft)
            second = pacing.LoginPacer(clock=ft.clock, sleep=ft.sleep, state_path=path)
            waited = second.acquire("login of another run")
            self.assertAlmostEqual(waited, 182.0)      # the first 12 are 182 s from ageing out
            self.assertEqual(len(second.waits), 1)

    def test_a_slot_login_and_a_slot_teardown_both_go_through_the_run_s_pacer(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            stub = types.SimpleNamespace()
            stub.server = "s"
            stub.warnings = []
            stub.log = lambda line: None
            stub.character_note = lambda *a, **k: None
            stub.pace = lambda what, account=None: order.append(("pace", what, account))
            session = run.SlotSession({"slot": {"id": "1", "port": 0}}, os.path.join(tmp, "slot1"), "bin", "holder",
                                      stub, lambda line: None)
            session.cfg, session.pidfile = "cfg", "pid"
            session.account = "ACC1"
            os.makedirs(session.slot_dir)
            with unittest.mock.patch.object(run.subprocess, "Popen",
                                            lambda *a, **k: order.append(("popen",)) or types.SimpleNamespace(poll=lambda: None)), \
                    unittest.mock.patch.object(run.SlotSession, "wait_in_world", lambda self, timeout=90.0: {"team": "alliance"}):
                session.login("Gbmage")
            self.assertEqual(order[0], ("pace", "login Gbmage", "ACC1"))
            self.assertEqual(order[1], ("popen",))
            r = run.Run(types.SimpleNamespace(server="s", bind=False, dry_run=False), "", tmp, [])
            r.pacer = pacing.LoginPacer(clock=lambda: T_1410, sleep=lambda s: None)
            sess = types.SimpleNamespace(slot_dir=tmp, slot_id="1", holder_name="h", lease_id="", keeper=None,
                                         teardown_done=False, stop_agent=lambda: None)
            with unittest.mock.patch.object(run.teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                r.stop_session(sess)
            self.assertEqual(len(r.pacer.stamps), 1)

    def test_the_run_ledger_keeps_the_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            ft = FakeTime()
            r = run.Run(types.SimpleNamespace(server="s", bind=False, dry_run=False), "", tmp, [])
            r.pacer = pacing.LoginPacer(clock=ft.clock, sleep=ft.sleep)
            for i in range(13):
                r.pace("login %d" % i)
            waits = [x for x in r.ledger.rows if x.get("kind") == "login_wait"]
            self.assertEqual(len(waits), 1)
            self.assertEqual(waits[0]["before"], "login 12")
            self.assertAlmostEqual(waits[0]["seconds"], 182.0, places=1)

    def test_run_pace_passes_the_account_to_the_pacer(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run.Run(types.SimpleNamespace(server="s", bind=False, dry_run=False), "", tmp, [])
            r.pacer = pacing.LoginPacer(clock=lambda: T_1410, sleep=lambda s: None)
            r.pace("login Gbmage", account="ACC1")
            r.pace("login Gbmage", account="acc1")          # the same account, another case: one account
            self.assertEqual([acct for _t, acct in r.pacer.stamps], ["ACC1", "ACC1"])
            self.assertEqual(r.pacer.waits, [])

    def test_regression_a_login_at_minute_01_waits_for_the_hour_start_window(self):
        # the failing case: the pre-change pacer let a login at 14:01:30 UTC straight through
        ft = FakeTime(utc_epoch(2026, 11, 8, 14, 1, 30))
        logs = []
        pacer = pacing.LoginPacer(clock=ft.clock, sleep=ft.sleep, log=logs.append)
        pacer.acquire("login Gbmage")
        self.assertEqual(pacer.waits, [("login Gbmage", 95.0)])
        self.assertEqual(ft.now, utc_epoch(2026, 11, 8, 14, 3, 5))
        self.assertIn("login pacer: waiting 95 s for the hour-start realmd window", logs)

    def test_a_login_at_1403_10_does_not_wait(self):
        ft = FakeTime(utc_epoch(2026, 11, 8, 14, 3, 10))
        pacer = pacing.LoginPacer(clock=ft.clock, sleep=ft.sleep)
        pacer.acquire("login Gbmage")
        self.assertEqual(pacer.waits, [])
        self.assertEqual(ft.now, utc_epoch(2026, 11, 8, 14, 3, 10))

    def test_the_hour_start_guard_runs_from_1400_to_1403_05_exactly(self):
        ft = FakeTime(utc_epoch(2026, 11, 8, 14, 0, 0))
        pacer = pacing.LoginPacer(clock=ft.clock, sleep=ft.sleep)
        pacer.acquire("login at 14:00:00")
        self.assertEqual(pacer.waits, [("login at 14:00:00", 185.0)])
        ft = FakeTime(utc_epoch(2026, 11, 8, 14, 3, 5))
        pacer = pacing.LoginPacer(clock=ft.clock, sleep=ft.sleep)
        pacer.acquire("login at 14:03:05")
        self.assertEqual(pacer.waits, [])

    def test_twenty_logins_of_one_account_inside_180_s_never_wait(self):
        ft = FakeTime()
        pacer = pacing.LoginPacer(clock=ft.clock, sleep=ft.sleep)
        for _ in range(20):
            pacer.acquire("login ACC1", account="ACC1")
            ft.now += 9.0                                   # 20 logins span 171 s: all inside one window
        self.assertEqual(pacer.waits, [])

    def test_thirteen_distinct_accounts_inside_180_s_make_the_thirteenth_wait(self):
        ft = FakeTime()
        pacer = pacing.LoginPacer(clock=ft.clock, sleep=ft.sleep)
        for i in range(13):
            name = "ACC%02d" % (i + 1)
            pacer.acquire("login " + name, account=name)
            if i < 12:
                self.assertEqual(pacer.waits, [])
            ft.now += 10.0
        self.assertEqual(pacer.waits, [("login ACC13", 62.0)])   # the 13th, at +120 s, waits for +182 s

    def test_a_repeat_by_an_account_in_the_window_does_not_wait_even_at_the_limit(self):
        ft = FakeTime()
        pacer = pacing.LoginPacer(clock=ft.clock, sleep=ft.sleep)
        for i in range(12):
            name = "ACC%02d" % (i + 1)
            pacer.acquire("login " + name, account=name)
        ft.now += 5.0
        pacer.acquire("relogin acc03", account="acc03")     # the same account in another case: no new account
        self.assertEqual(pacer.waits, [])

    def test_an_old_state_file_of_bare_times_counts_as_unknown_accounts(self):
        ft = FakeTime()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "logins.json")
            with open(path, "w") as f:
                json.dump([ft.now] * 12, f)
            pacer = pacing.LoginPacer(clock=ft.clock, sleep=ft.sleep, state_path=path)
            waited = pacer.acquire("login ACC1", account="ACC1")
            self.assertAlmostEqual(waited, 182.0)

    def test_the_shared_state_file_keeps_the_account_of_each_login(self):
        ft = FakeTime()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "logins.json")
            pacer = pacing.LoginPacer(clock=ft.clock, sleep=ft.sleep, state_path=path)
            pacer.acquire("a", account="ACC1")
            pacer.acquire("b", account="acc2")
            with open(path) as f:
                rows = json.load(f)
        self.assertEqual(rows, [[ft.now, "ACC1"], [ft.now, "ACC2"]])


# ---------------------------------------------------------------------------------------------
# 8. the "duplicate" sample series: one sampler, a host clock that steps back
# ---------------------------------------------------------------------------------------------

class SteppingDatetime:
    """datetime.now() of a host whose wall clock steps back 2.93 s every 3rd call (as WSL2's does)."""
    calls = 0

    @classmethod
    def now(cls, tz=None):
        import datetime as real
        cls.calls += 1
        steps = cls.calls // 3
        return real.datetime(2026, 11, 8, 5, 0, 0, tzinfo=real.timezone.utc).astimezone(tz) + \
            real.timedelta(seconds=cls.calls - 2.93 * steps)


class ClockStepTest(unittest.TestCase):
    def test_the_real_run_shows_a_clock_that_steps_back_not_two_samplers(self):
        doc = fx("clock_steps_050750_rep3.json")
        from datetime import datetime
        walls = [datetime.fromisoformat(w).timestamp() for w in doc["player_sample_walls"]]
        backsteps = [a - b for a, b in zip(walls, walls[1:]) if b < a]
        self.assertEqual(len(backsteps), 6)
        self.assertTrue(all(1.8 < x < 2.1 for x in backsteps), backsteps)       # 1.93 s, every ~31 s
        offs = [datetime.fromisoformat(t).timestamp() - m / 1000.0 for _s, t, m in doc["events"]]
        jumps = [round(b - a, 2) for a, b in zip(offs, offs[1:]) if abs(b - a) > 0.3]
        self.assertEqual(len(jumps), 8)
        self.assertTrue(all(-3.0 <= j <= -2.89 for j in jumps), jumps)          # the agent's `time` steps, mono_ms does not
        monos = [m for _s, _t, m in doc["events"]]
        self.assertEqual(monos, sorted(monos))
        self.assertEqual(len({s for s, _t, _m in doc["events"]}), len(doc["events"]))    # one pump: no duplicate seq

    def run_sampler(self, ticks=9):
        with tempfile.TemporaryDirectory() as tmp:
            status = {"state": "in_world", "guid": PLAYER, "level": 40, "health": 1000, "max_health": 1189,
                      "power": 0, "max_power": 1000, "target": "", "x": 0, "y": 0, "z": 0}
            agent = tl1.LogAgent(status=status, units=[], unit_rows={PLAYER: {"guid": PLAYER, "auras": []}})
            mono = [0]
            real_rpc = agent.rpc

            def rpc(method, params=None, timeout=None):
                if method == "status":
                    mono[0] += 1000
                    return dict(agent.status_row, mono_ms=mono[0])
                return real_rpc(method, params, timeout)
            agent.rpc = rpc
            marks = collect.Marks(os.path.join(tmp, "m.jsonl"))
            marks.start()
            col = collect.Collector(agent, tmp, marks, {"player": PLAYER, "bot": BOT, "extras": [], "mobs": []}, None, {})
            count = [0]

            class Stop:
                def is_set(self):
                    return count[0] >= ticks

                def wait(self, secs):
                    count[0] += 1
                    clock[0] += 1.0
                    return False

                def set(self):
                    count[0] = ticks
            clock = [0.0]
            col._stop = Stop()
            SteppingDatetime.calls = 0
            with unittest.mock.patch("trl.live.ledger.datetime", SteppingDatetime), \
                    unittest.mock.patch.object(collect.time, "monotonic", lambda: clock[0]), \
                    unittest.mock.patch.object(collect.time, "time", lambda: 1_700_000_000.0 + clock[0] - 2.93 * (int(clock[0]) // 3)):
                marks2 = collect.Marks(os.path.join(tmp, "m2.jsonl"))
                marks2.start()
                col.marks = marks2
                col._sample()
            with open(os.path.join(tmp, "samples.jsonl")) as f:
                return [json.loads(line) for line in f]

    def test_one_rep_has_exactly_one_player_row_per_tick_and_the_clock_never_runs_backwards(self):
        rows = [r for r in self.run_sampler(9) if r["unit"] == "player"]
        self.assertEqual(len(rows), 9)
        walls = [r["wall"] for r in rows]
        self.assertEqual(walls, sorted(walls))
        self.assertEqual(len(set(walls)), 9)
        ts = [r["t"] for r in rows]
        self.assertEqual(ts, sorted(ts))
        self.assertEqual([r["mono_ms"] for r in rows], [1000 * k for k in range(1, 10)])

    def test_normalise_puts_events_on_the_monotonic_axis(self):
        from trl import events as normaliser
        from datetime import datetime, timedelta, timezone
        base = datetime(2026, 11, 8, 5, 0, 0, tzinfo=timezone.utc)

        def iso(secs):
            return (base + timedelta(seconds=secs)).astimezone().isoformat(timespec="milliseconds")
        with tempfile.TemporaryDirectory() as tmp:
            samples = [{"t": 0, "wall": iso(k), "unit": "player", "guid": PLAYER, "hp": 1000 - 10 * k, "max_hp": 1000,
                        "auras": [], "in_combat": True, "mono_ms": 5000 + 1000 * k} for k in range(0, 20)]
            # the host clock stepped back 2.93 s before event 12: the agent's `time` steps, mono_ms does not
            raw = [{"kind": "melee", "seq": k, "mono_ms": 5000 + 1000 * k + 500,
                    "time": iso(k + 0.5 - (2.93 if k >= 12 else 0.0)),
                    "source": "mob", "source_guid": "0xf130000000000001", "target": "p", "target_guid": PLAYER,
                    "amount": 5} for k in range(0, 18)]
            marks = [{"kind": "fight_start", "wall": iso(0.0), "t": 0.0}]
            for name, rows in (("samples.jsonl", samples), ("raw-ta.jsonl", raw), ("marks.jsonl", marks)):
                with open(os.path.join(tmp, name), "w") as f:
                    for r in rows:
                        f.write(json.dumps(r) + "\n")
            with open(os.path.join(tmp, "setup.json"), "w") as f:
                json.dump({"guids": {"player": PLAYER, "bot": BOT, "mobs": ["0xf130000000000001"]}}, f)
            out = [e for e in normaliser.normalise(tmp, {}) if e["kind"] == "damage"]
        self.assertEqual([e["t"] for e in out], [k + 0.5 for k in range(18)])
        self.assertEqual(out[15]["dst_hp_pct"], 100.0 * (1000 - 10 * 15) / 1000.0)      # the sample of its own second


# ---------------------------------------------------------------------------------------------
# 7. the stance cast failing with line_of_sight on reps 2-4
# ---------------------------------------------------------------------------------------------

class SelectionAgent(tl1.LogAgent):
    """The server side of a self cast: `cast` with no target sends the client's SELECTION
    (Session.Cast: t == 0 -> s.target); a selection that is a dead mob out of sight fails the
    cast with reason 42 line_of_sight; a self target succeeds and the aura lands."""

    def __init__(self, selection, **kw):
        tl1.LogAgent.__init__(self, **kw)
        self.status_row["target"] = selection
        self.unit_rows[PLAYER] = {"guid": PLAYER, "health": 1189, "max_health": 1189, "level": 40, "auras": []}
        self.casts = []
        self.last_failure = None

    def rpc(self, method, params=None, timeout=None):
        params = params or {}
        if method == "cast":
            self.casts.append(dict(params))
            target = agent_mod.norm_guid(params.get("target") or self.status_row.get("target") or "")
            if target and target != agent_mod.norm_guid(PLAYER):
                self.last_failure = {"spell_id": params["spell_id"], "reason": 42, "reason_name": "line_of_sight"}
            else:
                self.last_failure = None
                self.unit_rows[PLAYER]["auras"] = [{"spell_id": params["spell_id"]}]
            return {}
        if method == "cast_state":
            return {"last_failure": self.last_failure}
        return tl1.LogAgent.rpc(self, method, params, timeout)


class StanceSelectionTest(unittest.TestCase):
    def ctx(self):
        return tl1.ctx_for(learn_ids=frozenset({71, 355}))

    def setup_for(self, selection, tmp):
        agent = SelectionAgent(selection)
        guard = gm_guard.GmGuard(agent, Ledger(os.path.join(tmp, "l.jsonl")), self.ctx())
        return agent, guard

    def test_the_real_failure_and_the_state_repeat_1_left_behind(self):
        failed = fx("stance_045710_rep2.json")["failed_row_rep2"]
        self.assertFalse(failed["ok"])
        self.assertIn("'reason_name': 'line_of_sight'", failed["evidence"])
        end = fx("rep1_end_045710.json")
        player = [r for r in end["last_samples"] if r["unit"] == "player"][0]
        mob = [r for r in end["last_samples"] if r["guid"] == STALE_MOB][0]
        self.assertEqual(player["target"], STALE_MOB)            # still targeting the last mob
        dist = ((player["x"] - mob["x"]) ** 2 + (player["y"] - mob["y"]) ** 2) ** 0.5
        self.assertGreater(dist, 15.0)                             # 17 yd away, on higher ground (z 85.77 vs 85)
        lines = [m["line"] for m in end["marks_tail"] if m.get("kind") == "gm"]
        self.assertEqual(lines[0], ".die")                         # the teardown ends on GM lines, each
        self.assertEqual(lines[1], ".unaura all")                  # restoring the previous selection

    def test_a_cast_without_a_target_with_the_stale_mob_selected_fails_like_the_real_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _guard = self.setup_for(STALE_MOB, tmp)
            agent.cast(71, target=0)
            self.assertEqual(agent.last_failure["reason_name"], "line_of_sight")

    def test_the_stance_cast_names_the_player_and_holds_with_the_stale_mob_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, guard = self.setup_for(STALE_MOB, tmp)
            row = setup.check_stance(agent, trial("priest-holy-pws-def-arms"), guard, guard.ctx, timeout=0.3, poll=0.05)
            self.assertTrue(row["ok"], row)
            self.assertEqual(agent.casts[0], {"spell_id": 71, "target": agent_mod.norm_guid(PLAYER)})

    def test_every_guarded_line_puts_the_stale_selection_back_so_clearing_it_once_is_not_enough(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, guard = self.setup_for(STALE_MOB, tmp)
            guard.send("unaura", PLAYER, "all")
            self.assertEqual(agent.status_row["target"], STALE_MOB)

    def test_a_failing_stance_row_names_the_selection_it_was_cast_at(self):
        class Never(SelectionAgent):
            def rpc(self, method, params=None, timeout=None):
                if method == "cast":
                    self.casts.append(dict(params))
                    self.last_failure = {"spell_id": 71, "reason": 42, "reason_name": "line_of_sight"}
                    return {}
                return SelectionAgent.rpc(self, method, params, timeout)
        with tempfile.TemporaryDirectory() as tmp:
            agent = Never(STALE_MOB)
            guard = gm_guard.GmGuard(agent, Ledger(os.path.join(tmp, "l.jsonl")), self.ctx())
            row = setup.check_stance(agent, trial("priest-holy-pws-def-arms"), guard, guard.ctx, timeout=0.2, poll=0.05)
            self.assertFalse(row["ok"])
            self.assertIn("selected " + agent_mod.norm_guid(STALE_MOB), row["evidence"])


if __name__ == "__main__":
    unittest.main()
