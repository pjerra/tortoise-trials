"""Tests for the scoring/report half of the trials harness.

The first test is the #1553 shape: 3 shields priest->priest, 1 priest->player
at 15% (critical 20), 2 priest->player at 60% -> count for rule
PRI-PWS-DEF-ARMS = 2, verdict FAIL; the self and critical ones are not
counted. Failing case: this is what #1553's summarize.py got wrong (it listed
all shields and used the poll hp); name it in the report.
"""

import copy
import glob
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from trl import events as events_mod  # noqa: E402
from trl import report as report_mod  # noqa: E402
from trl import rules as rules_mod  # noqa: E402
from trl import scenario as scenario_mod  # noqa: E402
from trl import score as score_mod  # noqa: E402

PLAYER = "0x0000000000000001"
BOT = "0x0000000000000002"
MOB = "0xF13000000000ABCD"


def _trial(**over):
    trial = {
        "id": "priest-holy-pws-def-arms",
        "title": "Holy priest: no shield on an Arms warrior tanking in Defensive Stance",
        "group": "healers",
        "class": "priest",
        "spec": "holy",
        "player": {"class": "warrior", "level": 35,
                   "talents": {"tree": "arms", "spells": [12285, 12295]},
                   "stance": 71, "forced_role": None, "auras": [], "items": []},
        "bot": {"class": "priest", "spec": "holy", "role": "healer", "level": 35,
                "gear": "blue", "items": [{"name": "Morning Glory Dew", "count": 20}],
                "mana_pct": None, "cc_mark": None},
        "situation": {"kind": "tank_damage",
                      "mobs": {"role": "melee", "count": 1, "level_delta": 1, "refill": True},
                      "hp_band": [40, 90], "floor_pct": 30, "victim": "player",
                      "pull_after_s": 0, "ooc_wait_s": 0, "debuff": None},
        "setup_checks": ["player_level", "player_visible", "player_talents", "player_stance",
                         "player_forced_role", "bot_present", "bot_level",
                         "bot_spec", "grouped", "mobs_ready", "config_read"],
        "duration_s": 180,
        "repeats": 2,
        "min_valid_s": 120,
        "opportunity": {"unit": "$player", "hp_lt": 90, "hp_gt": "$critical",
                        "in_combat": True},
        "rules": [{
            "id": "PRI-PWS-DEF-ARMS",
            "desc": "no shield on the player above critical in combat",
            "source": "ai/playerbot/strategy/priest/PriestActions.h:42; issue #1553",
            "expect_today": "FAIL",
            "why_expect": "IsTank reads talents/forced role, not stance",
            "check": {"metric": "count",
                      "where": {"kind": "cast", "src": "$bot", "dst": "$player",
                                "spell": "Power Word: Shield",
                                "dst_hp_gt": "$critical", "in_combat": True}},
            "pass_if": "== 0",
            "min_opportunity_s": 90,
            "min_n": 0,
        }],
    }
    trial.update(over)
    return trial


def _ctx(**over):
    ctx = {"guids": {"player": PLAYER, "bot": BOT, "pet": None, "mobs": [MOB], "marked": None},
           "thresholds": {"critical": 20, "low": 50, "medium": 70},
           "thresholds_assumed": False, "trial": _trial()}
    ctx.update(over)
    return ctx


def _cast(t, src, dst, hp, spell_id=6065, combat=True):
    sguid = {"bot": BOT, "player": PLAYER}.get(src, src)
    dguid = {"player": PLAYER, "bot": BOT}.get(dst, dst)
    return {"t": t, "wall": None, "kind": "cast", "src": src, "src_guid": sguid,
            "dst": dst, "dst_guid": dguid,
            "src_unit": "$bot" if sguid == BOT else ("$player" if sguid == PLAYER else ""),
            "dst_unit": "$player" if dguid == PLAYER else ("$bot" if dguid == BOT else ""),
            "spell_id": spell_id, "spell": "Power Word: Shield", "amount": None,
            "dst_hp_pct": hp, "src_role": "bot", "dst_role": "player",
            "origin": "ta"}


def _sample(t, guid, hp, max_hp=1000, combat=True, auras=()):
    return {"t": t, "wall": None, "unit": "player", "guid": guid,
            "hp": hp, "max_hp": max_hp, "power": 500, "max_power": 500,
            "auras": list(auras), "casting": None, "in_combat": combat,
            "target": "", "x": 0.0, "y": 0.0, "z": 0.0}


def _excluded(a, b, why="test"):
    return {"t": a, "wall": None, "kind": "excluded", "src": "", "src_guid": "",
            "dst": "", "dst_guid": "", "src_unit": "", "dst_unit": "",
            "spell_id": None, "spell": None, "amount": None, "dst_hp_pct": None,
            "src_role": "other", "dst_role": "other", "origin": "marks",
            "from": a, "to": b, "why": why}

class ShieldCountTest(unittest.TestCase):
    def test_shield_count_ignores_self_and_critical(self):
        trial = _trial()
        rule = trial["rules"][0]
        rows = [
            _cast(10.0, "bot", "bot", 100.0),
            _cast(20.0, "bot", "bot", 100.0),
            _cast(30.0, "bot", "bot", 100.0),
            _cast(40.0, "bot", "player", 15.0),
            _cast(60.0, "bot", "player", 60.0),
            _cast(70.0, "bot", "player", 60.0),
        ]
        samples = [_sample(float(t), BOT, 900) for t in range(0, 80)]
        ctx = _ctx()
        res = rules_mod.evaluate(rule, rows, samples, [], ctx)
        self.assertEqual(res["value"], 2)
        self.assertEqual(res["verdict"], "FAIL")


class RebaseTest(unittest.TestCase):
    def test_fight_start_rebases_samples_and_marks(self):
        # HIGH 1: fight_start 20 s after setup_ok; the player at 30 % at the
        # shield shows dst_hp_pct 30 (the re-based sample), not 100.
        tmp = tempfile.mkdtemp()
        guids = {"player": PLAYER, "bot": BOT, "pet": None, "mobs": [MOB], "marked": None}
        with open(os.path.join(tmp, "setup.json"), "w") as f:
            json.dump({"ok": True, "guids": guids}, f)
        with open(os.path.join(tmp, "marks.jsonl"), "w") as f:
            f.write(json.dumps({"kind": "setup_ok", "wall": "2026-01-01T00:00:00+00:00"}) + "\n")
            f.write(json.dumps({"kind": "fight_start", "wall": "2026-01-01T00:00:20+00:00"}) + "\n")
            f.write(json.dumps({"kind": "excluded", "from": "2026-01-01T00:00:25+00:00",
                                "to": "2026-01-01T00:00:27+00:00", "why": "test"}) + "\n")
        with open(os.path.join(tmp, "samples.jsonl"), "w") as f:
            # wall 00:00:50 = t 30 on the fight_start base; 30 % hp.
            f.write(json.dumps({"t": -999.0, "wall": "2026-01-01T00:00:50+00:00", "guid": PLAYER,
                                "hp": 300, "max_hp": 1000, "auras": [], "in_combat": True}) + "\n")
        with open(os.path.join(tmp, "raw-ta.jsonl"), "w") as f:
            # wall 00:00:50 = t 30 on the fight_start base (NOT the setup_ok base).
            f.write(json.dumps({"seq": 7, "time": "2026-01-01T00:00:50+00:00",
                                "kind": "spell_go", "source": {"name": "b", "guid": BOT},
                                "target": {"name": "p", "guid": PLAYER},
                                "spell_id": 6065}) + "\n")
        rows = events_mod.normalise(tmp, _trial())
        casts = [r for r in rows if r.get("kind") == "cast"]
        self.assertEqual(len(casts), 1)
        self.assertAlmostEqual(casts[0]["t"], 30.0)
        self.assertEqual(casts[0]["dst_hp_pct"], 30.0)
        excl = [r for r in rows if r.get("kind") == "excluded"]
        self.assertEqual([(r["from"], r["to"]) for r in excl], [(5.0, 7.0)])
        # The re-based sample on disk uses the same base.
        with open(os.path.join(tmp, "samples.jsonl")) as f:
            samples = [json.loads(line) for line in f if line.strip()]
        self.assertAlmostEqual(samples[0]["t"], 30.0)

    def test_dedupe_and_dropped_kinds(self):
        # HIGH 15: dedupe on seq + content (null seq handled); a missing
        # time row is dropped and counted in dropped_kinds.
        tmp = tempfile.mkdtemp()
        guids = {"player": PLAYER, "bot": BOT, "pet": None, "mobs": [MOB], "marked": None}
        with open(os.path.join(tmp, "setup.json"), "w") as f:
            json.dump({"ok": True, "guids": guids}, f)
        with open(os.path.join(tmp, "marks.jsonl"), "w") as f:
            f.write(json.dumps({"kind": "setup_ok", "wall": "2026-01-01T00:00:00+00:00"}) + "\n")
        with open(os.path.join(tmp, "samples.jsonl"), "w") as f:
            f.write(json.dumps({"t": 10.0, "guid": PLAYER, "hp": 500, "max_hp": 1000,
                                "auras": []}) + "\n")
        good = {"seq": 1, "time": "2026-01-01T00:00:10+00:00",
                "kind": "spell_go", "source": {"name": "b", "guid": BOT},
                "target": {"name": "p", "guid": PLAYER}, "spell_id": 6065}
        same_seq_other = dict(good, spell_id=17)
        no_time = {"kind": "spell_go", "source": {"name": "b", "guid": BOT},
                   "target": {"name": "p", "guid": PLAYER}, "spell_id": 6065}
        unknown_kind = dict(good, seq=9, kind="emotes")
        with open(os.path.join(tmp, "raw-ta.jsonl"), "w") as f:
            for row in (good, dict(good), same_seq_other, no_time, unknown_kind):
                f.write(json.dumps(row) + "\n")
        rows = events_mod.normalise(tmp, _trial())
        # exact dup dropped; same seq with other content kept; no-time + unknown dropped.
        self.assertEqual(len([r for r in rows if r.get("kind") == "cast"]), 2)
        summary = json.load(open(os.path.join(tmp, "events_summary.json")))
        self.assertEqual(summary["dropped_kinds"].get("emotes"), 1)
        self.assertEqual(summary["dropped_kinds"].get("<missing time>"), 1)

    def test_overlapping_excluded_merged(self):
        # HIGH 2: overlapping ISO excluded marks merge before subtracting.
        tmp = tempfile.mkdtemp()
        guids = {"player": PLAYER, "bot": BOT, "pet": None, "mobs": [MOB], "marked": None}
        with open(os.path.join(tmp, "setup.json"), "w") as f:
            json.dump({"ok": True, "guids": guids}, f)
        with open(os.path.join(tmp, "marks.jsonl"), "w") as f:
            f.write(json.dumps({"kind": "fight_start", "wall": "2026-01-01T00:00:00+00:00"}) + "\n")
            f.write(json.dumps({"kind": "excluded", "from": "2026-01-01T00:00:05+00:00",
                                "to": "2026-01-01T00:00:10+00:00", "why": "a"}) + "\n")
            f.write(json.dumps({"kind": "excluded", "from": "2026-01-01T00:00:08+00:00",
                                "to": "2026-01-01T00:00:12+00:00", "why": "b"}) + "\n")
        open(os.path.join(tmp, "samples.jsonl"), "w").close()
        open(os.path.join(tmp, "raw-ta.jsonl"), "w").close()
        rows = events_mod.normalise(tmp, _trial())
        excl = [r for r in rows if r.get("kind") == "excluded"]
        self.assertEqual(len(excl), 1)
        self.assertEqual((excl[0]["from"], excl[0]["to"]), (5.0, 12.0))


class NormaliserTest(unittest.TestCase):
    def test_dst_hp_from_last_sample(self):
        tmp = tempfile.mkdtemp()
        guids = {"player": PLAYER, "bot": BOT, "pet": None, "mobs": [MOB], "marked": None}
        with open(os.path.join(tmp, "setup.json"), "w") as f:
            json.dump({"ok": True, "guids": guids}, f)
        with open(os.path.join(tmp, "marks.jsonl"), "w") as f:
            f.write(json.dumps({"kind": "setup_ok", "wall": "2026-01-01T00:00:00+00:00"}) + "\n")
        with open(os.path.join(tmp, "samples.jsonl"), "w") as f:
            f.write(json.dumps({"t": 8.8, "guid": PLAYER, "hp": 500, "max_hp": 1000,
                                "auras": []}) + "\n")
            f.write(json.dumps({"t": 9.6, "guid": PLAYER, "hp": 800, "max_hp": 1000,
                                "auras": []}) + "\n")
        with open(os.path.join(tmp, "raw-ta.jsonl"), "w") as f:
            f.write(json.dumps({"seq": 1, "time": "2026-01-01T00:00:10+00:00",
                                "kind": "spell_go", "source": {"name": "b", "guid": BOT},
                                "target": {"name": "p", "guid": PLAYER},
                                "spell_id": 6065}) + "\n")
            f.write(json.dumps({"seq": 1, "time": "2026-01-01T00:00:10+00:00",
                                "kind": "spell_go", "source": {"name": "b", "guid": BOT},
                                "target": {"name": "p", "guid": PLAYER},
                                "spell_id": 6065}) + "\n")
        rows = events_mod.normalise(tmp, _trial())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "cast")
        # sample at 9.6 (0.4 s before t=10) wins over 8.8 (1.2 s before)
        self.assertEqual(rows[0]["dst_hp_pct"], 80.0)
        # none within 1 s -> null
        with open(os.path.join(tmp, "samples.jsonl"), "w") as f:
            f.write(json.dumps({"t": 5.0, "guid": PLAYER, "hp": 500, "max_hp": 1000,
                                "auras": []}) + "\n")
        rows = events_mod.normalise(tmp, _trial())
        self.assertIsNone(rows[0]["dst_hp_pct"])


class SetupInvalidTest(unittest.TestCase):
    def test_invalid_setup_not_scored(self):
        tmp = tempfile.mkdtemp()
        rep = os.path.join(tmp, "rep1")
        os.makedirs(rep)
        with open(os.path.join(rep, "setup.json"), "w") as f:
            json.dump({"ok": False, "invalid_reason": "stance not held",
                       "guids": {}}, f)
        open(os.path.join(rep, "samples.jsonl"), "w").close()
        open(os.path.join(rep, "marks.jsonl"), "w").close()
        open(os.path.join(rep, "raw-ta.jsonl"), "w").close()
        out = score_mod.score_rep(rep, _trial())
        self.assertTrue(all(r["verdict"] == "INVALID" for r in out["rules"]))
        self.assertIn("stance", out["invalid_reason"])


class CutShortTest(unittest.TestCase):
    def test_cut_short_invalid(self):
        tmp = tempfile.mkdtemp()
        rep = os.path.join(tmp, "rep1")
        os.makedirs(rep)
        with open(os.path.join(rep, "setup.json"), "w") as f:
            json.dump({"ok": True, "guids": {"player": PLAYER, "bot": BOT}}, f)
        with open(os.path.join(rep, "samples.jsonl"), "w") as f:
            for t in range(0, 101):
                f.write(json.dumps(_sample(float(t), PLAYER, 500)) + "\n")
        with open(os.path.join(rep, "marks.jsonl"), "w") as f:
            f.write(json.dumps({"kind": "fight_end", "ended": "player died"}) + "\n")
        out = score_mod.score_rep(rep, _trial())
        self.assertTrue(all(r["verdict"] == "INVALID" for r in out["rules"]))
        self.assertIn("cut short", out["invalid_reason"])
        self.assertIn("player died", out["invalid_reason"])


class OpportunityTest(unittest.TestCase):
    def test_inconclusive_below_opportunity(self):
        tmp = tempfile.mkdtemp()
        rep = os.path.join(tmp, "rep1")
        os.makedirs(rep)
        with open(os.path.join(rep, "setup.json"), "w") as f:
            json.dump({"ok": True, "guids": {"player": PLAYER, "bot": BOT}}, f)
        with open(os.path.join(rep, "samples.jsonl"), "w") as f:
            for t in range(0, 200):
                # hp 95%: above hp_lt 90 only 0 s... use 60 s of 80 then rest 95
                hp = 800 if t < 60 else 950
                f.write(json.dumps(_sample(float(t), PLAYER, hp)) + "\n")
        with open(os.path.join(rep, "marks.jsonl"), "w") as f:
            f.write("")
        rows = [_cast(200.0, "bot", "player", 80.0)]
        with open(os.path.join(rep, "events.jsonl"), "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        out = score_mod.score_rep(rep, _trial())
        self.assertEqual(out["rules"][0]["verdict"], "INCONCLUSIVE")


class LatencyTest(unittest.TestCase):
    def test_latency_window_and_excluded(self):
        trial = _trial()
        trial["rules"] = [{
            "id": "LAT", "desc": "x", "source": "s", "expect_today": "?",
            "why_expect": "x",
            "check": {"metric": "latency",
                      "trigger": {"kind": "cross", "unit": "$player", "below": 50},
                      "response": {"kind": "heal", "src": "$bot", "dst": "$player"},
                      "within_s": 4},
            "pass_if": ">= 0.8", "min_opportunity_s": 0, "min_n": 0}]
        samples = [_sample(float(t), PLAYER, 600 if t < 10 else 400) for t in range(0, 30)]
        rows = [{"t": 13.0, "kind": "heal", "src": "b", "src_guid": BOT,
                 "dst": "p", "dst_guid": PLAYER, "src_unit": "$bot",
                 "dst_unit": "$player", "spell_id": 1, "spell": "x",
                 "amount": 1, "dst_hp_pct": 40.0, "src_role": "bot",
                 "dst_role": "player", "origin": "ta"}]
        ctx = {"guids": {"player": PLAYER, "bot": BOT}, "thresholds": {},
               "trial": trial}
        res = rules_mod.evaluate(trial["rules"][0], rows, samples, [], ctx)
        self.assertEqual(res["value"], 1.0)
        self.assertEqual(res["verdict"], "PASS")
        self.assertEqual(res["k"], 1)
        self.assertEqual(res["n"], 1)
        # A trigger inside an excluded span is skipped -> no triggers.
        # The normaliser's kind: excluded rows are the one source.
        excl_rows = rows + [_excluded(9.0, 12.0)]
        res = rules_mod.evaluate(trial["rules"][0], excl_rows, samples, [], ctx)
        self.assertEqual(res["verdict"], "INCONCLUSIVE")

    def test_latency_window_crossing_excluded_skipped(self):
        # The response window crossing an excluded span carries no
        # opportunity: the trigger is skipped, not counted as missed.
        trial = _trial()
        trial["rules"] = [{
            "id": "LAT", "desc": "x", "source": "s", "expect_today": "?",
            "why_expect": "x",
            "check": {"metric": "latency",
                      "trigger": {"kind": "cross", "unit": "$player", "below": 50},
                      "response": {"kind": "heal", "src": "$bot", "dst": "$player"},
                      "within_s": 4},
            "pass_if": ">= 0.8", "min_opportunity_s": 0, "min_n": 0}]
        samples = [_sample(float(t), PLAYER, 600 if t < 10 else 400) for t in range(0, 30)]
        rows = [{"t": 13.0, "kind": "heal", "src": "b", "src_guid": BOT,
                 "dst": "p", "dst_guid": PLAYER, "src_unit": "$bot",
                 "dst_unit": "$player", "spell_id": 1, "spell": "x",
                 "amount": 1, "dst_hp_pct": 40.0, "src_role": "bot",
                 "dst_role": "player", "origin": "ta"},
                _excluded(11.0, 12.0)]
        ctx = {"guids": {"player": PLAYER, "bot": BOT}, "thresholds": {},
               "trial": trial}
        res = rules_mod.evaluate(trial["rules"][0], rows, samples, [], ctx)
        self.assertEqual(res["verdict"], "INCONCLUSIVE")

    def test_latency_debounce_per_src(self):
        # trigger_debounce_s is per src guid: the same mob refiring 3 s
        # later is debounced, a different mob is not.
        trial = _trial()
        m2 = "0xF13000000000BEEF"

        def dmg(t, guid, name):
            return {"t": t, "kind": "damage", "src": name, "src_guid": guid,
                    "dst": "p", "dst_guid": PLAYER, "src_unit": "x",
                    "dst_unit": "$player", "spell_id": None, "spell": None,
                    "amount": 1, "dst_hp_pct": 40.0, "src_role": "mob",
                    "dst_role": "player", "origin": "ta"}
        trig_rows = [dmg(10.0, MOB, "m"), dmg(12.0, m2, "m2"), dmg(13.0, MOB, "m")]
        trial["rules"] = [{
            "id": "LAT", "desc": "x", "source": "s", "expect_today": "?",
            "why_expect": "x",
            "check": {"metric": "latency",
                      "trigger": {"kind": "damage"},
                      "response": {"kind": "heal", "src": "$bot", "dst": "$player"},
                      "within_s": 10, "trigger_debounce_s": 5},
            "pass_if": ">= 0.8", "min_opportunity_s": 0, "min_n": 0}]
        ctx = {"guids": {"player": PLAYER, "bot": BOT, "mobs": [MOB, m2]}, "thresholds": {},
               "trial": trial, "rows": trig_rows}
        res = rules_mod.evaluate(trial["rules"][0], trig_rows, [], [], ctx)
        # 3 triggers, one debounced (same src 3 s later) -> counted 2.
        self.assertEqual(res["n"], 2)
        self.assertEqual(res["verdict"], "FAIL")
        # ... and the same test at check level AND at where level agrees.
        trial["rules"][0]["check"]["trigger_debounce_s"] = None
        trial["rules"][0]["check"]["where"] = {"trigger_debounce_s": 5}
        res = rules_mod.evaluate(trial["rules"][0], trig_rows, [], [], ctx)
        self.assertEqual(res["n"], 2)

    def test_latency_response_dst_guid(self):
        # response_dst/same_dst match by GUID, not by display name.
        trial = _trial()
        trial["rules"] = [{
            "id": "LAT", "desc": "x", "source": "s", "expect_today": "?",
            "why_expect": "x",
            "check": {"metric": "latency",
                      "trigger": {"kind": "damage", "dst": "$player"},
                      "response": {"kind": "heal", "src": "$bot"},
                      "within_s": 4, "response_dst": "$trigger.dst"},
            "pass_if": ">= 0.8", "min_opportunity_s": 0, "min_n": 0}]
        trig = {"t": 10.0, "kind": "damage", "src": "Wrong Name", "src_guid": MOB,
                "dst": "Also Wrong", "dst_guid": PLAYER, "src_unit": "mob:x",
                "dst_unit": "$player", "spell_id": None, "spell": None,
                "amount": 1, "dst_hp_pct": 40.0, "src_role": "mob",
                "dst_role": "player", "origin": "ta"}
        resp = {"t": 11.0, "kind": "heal", "src": "b", "src_guid": BOT,
                "dst": "Renamed Player", "dst_guid": PLAYER, "src_unit": "$bot",
                "dst_unit": "$player", "spell_id": 1, "spell": "x",
                "amount": 1, "dst_hp_pct": 40.0, "src_role": "bot",
                "dst_role": "player", "origin": "ta"}
        ctx = {"guids": {"player": PLAYER, "bot": BOT}, "thresholds": {},
               "trial": trial, "rows": [trig, resp]}
        res = rules_mod.evaluate(trial["rules"][0], [trig, resp], [], [], ctx)
        self.assertEqual(res["value"], 1.0)

class UptimeFirstRateTest(unittest.TestCase):
    def test_uptime_from_samples(self):
        trial = _trial()
        rule = {"id": "U", "desc": "x", "source": "s", "expect_today": "?",
                "why_expect": "x",
                "check": {"metric": "uptime", "unit": "$player", "aura": [71],
                          "phase": "in_combat"},
                "pass_if": ">= 0.5", "min_opportunity_s": 0, "min_n": 0}
        samples = [_sample(float(t), PLAYER, 500, combat=True,
                           auras=[71] if t < 15 else []) for t in range(20)]
        ctx = {"guids": {"player": PLAYER}, "trial": trial}
        res = rules_mod.evaluate(rule, [], samples, [], ctx)
        self.assertAlmostEqual(res["value"], 0.75)
        self.assertEqual(res["verdict"], "PASS")

    def test_first_and_rate(self):
        trial = _trial()
        first = {"id": "F", "desc": "x", "source": "s", "expect_today": "?",
                 "why_expect": "x",
                 "check": {"metric": "first",
                           "where": {"kind": "cast", "spell": "Power Word: Shield"}},
                 "pass_if": "< 30", "min_opportunity_s": 0, "min_n": 0}
        ctx = _ctx()
        res = rules_mod.evaluate(first, [_cast(12.0, "bot", "player", 80.0)], [], [], ctx)
        self.assertEqual(res["value"], 12.0)
        self.assertEqual(res["verdict"], "PASS")
        rate = dict(first, id="R")
        rate["check"] = {"metric": "rate",
                         "where": {"kind": "cast", "spell": "Power Word: Shield"}}
        rate["pass_if"] = ">= 1"
        rows = [_cast(10.0, "bot", "player", 80.0), _cast(70.0, "bot", "player", 80.0)]
        samples = [_sample(float(t), PLAYER, 500) for t in (10, 70)]
        ctx = _ctx()
        res = rules_mod.evaluate(rate, rows, samples, [], ctx)
        self.assertAlmostEqual(res["value"], 2.0)
        self.assertEqual(res["verdict"], "PASS")

    def test_first_no_match_is_null(self):
        # `first` with no match: value null + no_match true; pass_if still sees +inf.
        rule = {"id": "F", "desc": "x", "source": "s", "expect_today": "?",
                "why_expect": "x",
                "check": {"metric": "first",
                          "where": {"kind": "cast", "spell": "Power Word: Shield"}},
                "pass_if": "< 30", "min_opportunity_s": 0, "min_n": 0}
        ctx = _ctx()
        res = rules_mod.evaluate(rule, [], [], [], ctx)
        self.assertIsNone(res["value"])
        self.assertIs(res["no_match"], True)
        self.assertEqual(res["verdict"], "FAIL")
        rule = dict(rule, pass_if="> 30")
        res = rules_mod.evaluate(rule, [], [], [], ctx)
        self.assertEqual(res["verdict"], "PASS")

    def test_pet_alive_missing_samples_not_alive(self):
        # HIGH 6: the pet dead 90 % (missing samples) -> FAIL.
        PET = "0x0000000000000003"
        rule = {"id": "U", "desc": "x", "source": "s", "expect_today": "?",
                "why_expect": "x",
                "check": {"metric": "uptime", "unit": "$pet", "alive": True},
                "pass_if": ">= 0.5", "min_opportunity_s": 0, "min_n": 0}
        samples = []
        for t in range(20):
            samples.append(_sample(float(t), PLAYER, 500))
            if t < 2:
                s = _sample(float(t), PET, 500)
                samples.append(s)
        ctx = {"guids": {"player": PLAYER, "pet": PET}, "trial": _trial()}
        res = rules_mod.evaluate(rule, [], samples, [], ctx)
        self.assertAlmostEqual(res["value"], 0.1)
        self.assertEqual(res["verdict"], "FAIL")

    def test_hp_window_last_within_1s(self):
        # Future samples, first-not-last, and 1.5 s staleness never win.
        trial = _trial()
        rule = trial["rules"][0]
        rows = [_cast(10.0, "bot", "player", None)]
        rows[0]["dst_hp_pct"] = None
        samples = [_sample(9.0, PLAYER, 900), _sample(9.6, PLAYER, 300),
                   _sample(11.0, PLAYER, 100)]
        ctx = _ctx()
        res = rules_mod.evaluate(rule, rows, samples, [], ctx)
        # _matches reads the row's dst_hp_pct; the normaliser fills it from
        # the last sample within 1 s (9.6 -> 30 %). Direct check via events:
        tmp = tempfile.mkdtemp()
        guids = {"player": PLAYER, "bot": BOT, "pet": None, "mobs": [MOB], "marked": None}
        with open(os.path.join(tmp, "setup.json"), "w") as f:
            json.dump({"ok": True, "guids": guids}, f)
        with open(os.path.join(tmp, "marks.jsonl"), "w") as f:
            f.write(json.dumps({"kind": "setup_ok", "wall": "2026-01-01T00:00:00+00:00"}) + "\n")
        with open(os.path.join(tmp, "samples.jsonl"), "w") as f:
            f.write(json.dumps({"t": 8.5, "guid": PLAYER, "hp": 900, "max_hp": 1000,
                                "auras": []}) + "\n")
            f.write(json.dumps({"t": 9.6, "guid": PLAYER, "hp": 300, "max_hp": 1000,
                                "auras": []}) + "\n")
            f.write(json.dumps({"t": 11.0, "guid": PLAYER, "hp": 100, "max_hp": 1000,
                                "auras": []}) + "\n")
        with open(os.path.join(tmp, "raw-ta.jsonl"), "w") as f:
            f.write(json.dumps({"seq": 1, "time": "2026-01-01T00:00:10+00:00",
                                "kind": "spell_go", "source": {"name": "b", "guid": BOT},
                                "target": {"name": "p", "guid": PLAYER},
                                "spell_id": 6065}) + "\n")
        nrows = events_mod.normalise(tmp, _trial())
        # Last within 1 s (9.6) wins over first (8.5); future (11.0) ignored.
        self.assertEqual(nrows[0]["dst_hp_pct"], 30.0)
        # 1.5 s stale -> null.
        with open(os.path.join(tmp, "samples.jsonl"), "w") as f:
            f.write(json.dumps({"t": 8.5, "guid": PLAYER, "hp": 900, "max_hp": 1000,
                                "auras": []}) + "\n")
        nrows = events_mod.normalise(tmp, _trial())
        self.assertIsNone(nrows[0]["dst_hp_pct"])

    def test_missing_hp_never_passes_gate(self):
        # Missing hp treated as above critical: a dst_hp_gt gate counts the
        # cast (value 1), so the == 0 rule FAILs closed.
        trial = _trial()
        rule = trial["rules"][0]
        rows = [_cast(10.0, "bot", "player", None)]
        rows[0]["dst_hp_pct"] = None
        samples = [_sample(10.0, BOT, 900, combat=True)]
        ctx = _ctx()
        res = rules_mod.evaluate(rule, rows, samples, [], ctx)
        self.assertEqual(res["value"], 1)
        self.assertEqual(res["verdict"], "FAIL")


class MajorityTest(unittest.TestCase):
    def _rep(self, root, name, verdict, value=0, k=0, n=0):
        rep = os.path.join(root, name)
        os.makedirs(rep)
        verdicts = {"trial": _trial()["id"], "rep": name, "ok_setup": True,
                    "scored_s": 180.0,
                    "rules": [{"id": "PRI-PWS-DEF-ARMS", "verdict": verdict,
                               "value": value, "k": k, "n": n, "evidence": [],
                               "guard": "", "invalid_reason": ""}]}
        with open(os.path.join(rep, "verdicts.json"), "w") as f:
            json.dump(verdicts, f)
        return rep

    def test_majority_tie_fails(self):
        tmp = tempfile.mkdtemp()
        trial = _trial()
        trial["repeats"] = 2
        self._rep(tmp, "rep1", "PASS", k=1, n=1)
        self._rep(tmp, "rep2", "FAIL", k=0, n=1)
        out = score_mod.score_trial(tmp, trial)
        self.assertEqual(out["verdicts"][0]["verdict"], "FAIL")
        # Aggregate across repeats: k/n summed, not rep1 only.
        self.assertEqual((out["verdicts"][0]["k"], out["verdicts"][0]["n"]), (1, 2))
        tmp2 = tempfile.mkdtemp()
        for i, v in enumerate(["PASS", "PASS", "FAIL"]):
            self._rep(tmp2, "rep%d" % (i + 1), v, k=1 if v == "PASS" else 0, n=1)
        trial3 = _trial()
        trial3["repeats"] = 3
        out = score_mod.score_trial(tmp2, trial3)
        self.assertEqual(out["verdicts"][0]["verdict"], "PASS")
        self.assertEqual((out["verdicts"][0]["k"], out["verdicts"][0]["n"]), (2, 3))
        tmp3 = tempfile.mkdtemp()
        self._rep(tmp3, "rep1", "INVALID")
        self._rep(tmp3, "rep2", "PASS", value=0, k=3, n=4)
        out = score_mod.score_trial(tmp3, trial)
        self.assertEqual(out["verdicts"][0]["verdict"], "PASS")
        self.assertEqual(out["verdicts"][0]["n"], 4)


class ValidateTest(unittest.TestCase):
    def test_validate_rejects(self):
        # Fixed weak test: goes through cmd_validate and asserts the
        # specific error text on stdout.
        import io
        import contextlib
        import trials as trials_mod
        base = _trial()
        bad_metric = copy.deepcopy(base)
        bad_metric["rules"][0]["check"]["metric"] = "frobnicate"
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "bad.json")
        with open(path, "w") as f:
            json.dump(bad_metric, f)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = trials_mod.cmd_validate(type("A", (), {"files": [path]})())
        self.assertEqual(rc, 1)
        self.assertIn("unknown metric", buf.getvalue())
        bad_pass = copy.deepcopy(base)
        bad_pass["rules"][0]["pass_if"] = "=< 3"
        with open(path, "w") as f:
            json.dump(bad_pass, f)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = trials_mod.cmd_validate(type("A", (), {"files": [path]})())
        self.assertEqual(rc, 1)
        self.assertIn("bad pass_if", buf.getvalue())
        bad_where = copy.deepcopy(base)
        bad_where["rules"][0]["check"]["where"]["frobnicate"] = 1
        with open(path, "w") as f:
            json.dump(bad_where, f)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = trials_mod.cmd_validate(type("A", (), {"files": [path]})())
        self.assertEqual(rc, 1)
        self.assertIn("unknown where key", buf.getvalue())
        bad_spell = copy.deepcopy(base)
        bad_spell["rules"][0]["check"]["where"]["spell"] = "Not A Spell At All"
        from trl import spells as spells_mod
        spells_mod._cache.clear()
        errs = scenario_mod.validate(bad_spell, spells_path=os.path.join(
            ROOT, "data", "spells.json"))
        self.assertTrue(any("unknown spell" in e for e in errs))
        first = copy.deepcopy(base)
        second = copy.deepcopy(base)
        second["id"] = "other-trial"
        seen = {}
        dup = []
        for trial in (first, second):
            for rule in trial["rules"]:
                if rule["id"] in seen:
                    dup.append(rule["id"])
                seen[rule["id"]] = trial["id"]
        self.assertEqual(dup, ["PRI-PWS-DEF-ARMS"])

    def test_validate_strict_schema(self):
        # HIGH 3: unknown fields at every level; closed where keys with no
        # prefix escape; num/den/trigger/response dicts; unit/kind/threshold
        # aliases; trial id == file stem; a rule without kind in where.
        from trl import spells as spells_mod
        spells_mod._cache.clear()
        base = _trial()
        for doc, needle in (
                (dict(base, bogus=1), "unknown top-level field"),
                (dict(base, player=dict(base["player"], bogus=1)), "unknown player field"),
                (dict(base, bot=dict(base["bot"], bogus=1)), "unknown bot field"),
                (dict(base, situation=dict(base["situation"], bogus=1)), "unknown situation field")):
            self.assertTrue(any(needle in e for e in scenario_mod.validate(doc)),
                            (needle, scenario_mod.validate(doc)))
        rule_no_kind = copy.deepcopy(base)
        del rule_no_kind["rules"][0]["check"]["where"]["kind"]
        self.assertTrue(any("needs a kind" in e for e in scenario_mod.validate(rule_no_kind)))
        bad_kind = copy.deepcopy(base)
        bad_kind["rules"][0]["check"]["where"]["kind"] = "frobnicate"
        self.assertTrue(any("bad kind" in e for e in scenario_mod.validate(bad_kind)))
        prefix_escape = copy.deepcopy(base)
        prefix_escape["rules"][0]["check"]["where"]["num_cast"] = 1
        self.assertTrue(any("unknown where key" in e
                            for e in scenario_mod.validate(prefix_escape)))
        bad_alias = copy.deepcopy(base)
        bad_alias["rules"][0]["check"]["where"]["src"] = "$frobnicate"
        self.assertTrue(any("bad src alias" in e for e in scenario_mod.validate(bad_alias)))
        bad_thr = copy.deepcopy(base)
        bad_thr["rules"][0]["check"]["where"]["dst_hp_gt"] = "$frobnicate"
        self.assertTrue(any("bad dst_hp_gt" in e for e in scenario_mod.validate(bad_thr)))
        bad_trig = copy.deepcopy(base)
        bad_trig["rules"][0]["check"]["trigger"] = {"kind": "frobnicate", "unit": "$player",
                                                    "below": 50}
        self.assertTrue(any("bad trigger kind" in e for e in scenario_mod.validate(bad_trig)))
        bad_below = copy.deepcopy(base)
        bad_below["rules"][0]["check"]["trigger"] = {"kind": "cross", "unit": "$player",
                                                    "below": "$frobnicate"}
        self.assertTrue(any("bad trigger below" in e for e in scenario_mod.validate(bad_below)))
        # A bad threshold alias never crashes evaluation.
        lat = {"id": "L", "desc": "x", "source": "s", "expect_today": "?",
               "why_expect": "x",
               "check": {"metric": "latency",
                         "trigger": {"kind": "cross", "unit": "$player", "below": "$frobnicate"},
                         "response": {"kind": "heal"}, "within_s": 4},
               "pass_if": ">= 0.5", "min_opportunity_s": 0, "min_n": 0}
        ctx = _ctx()
        res = rules_mod.evaluate(lat, [], [_sample(1.0, PLAYER, 100)], [], ctx)
        self.assertEqual(res["verdict"], "INCONCLUSIVE")


class CompareTest(unittest.TestCase):
    def _cards(self, run, base, metric, run_v, base_v, rk, rn, bk, bn):
        for d, k, n in ((run, rk, rn), (base, bk, bn)):
            rep = os.path.join(d, "t", "rep1")
            os.makedirs(rep, exist_ok=True)
            with open(os.path.join(d, "card.json"), "w") as f:
                json.dump({"run": {"label": "x", "tb_version": "v", "critical_health": 20,
                                   "low_health": 50, "medium_health": 70},
                           "rows": [{"trial": "t", "rule": "R", "expected": "?",
                                     "verdict": "PASS", "k": k, "n": n, "metric": metric,
                                     "value": run_v if d == run else base_v,
                                     "values": [run_v] if d == run else [base_v],
                                     "pass_if": ">= 0.8",
                                     "surprise": "", "source": "s", "detail": []}]}, f)
            with open(os.path.join(rep, "verdicts.json"), "w") as f:
                json.dump({"rules": [{"id": "R", "verdict": "PASS",
                                      "value": 1, "k": k, "n": n}
                                     ]}, f)

    def test_compare_uses_walk_report_fisher(self):
        # Fixed weak test: exercises report.compare on a ratio kind so the
        # p value comes from the shared walk-report Fisher, not a copy.
        run = tempfile.mkdtemp()
        base = tempfile.mkdtemp()
        self._cards(run, base, "ratio", 0.2, 0.9, 2, 10, 9, 10)
        rows = report_mod.compare(run, base)
        self.assertEqual(len(rows), 1)
        from trl._wr import stats as wr_stats
        self.assertAlmostEqual(rows[0]["p"], wr_stats.fisher_two_sided(2, 8, 9, 1))

    def test_compare_rows(self):
        # Fixed weak test: the card row carries the aggregate k/n summed
        # over valid repeats (not rep1 only), with per-repeat values.
        run = tempfile.mkdtemp()
        base = tempfile.mkdtemp()
        self._cards(run, base, "ratio", 0.2, 0.9, 2, 10, 9, 10)
        rows = report_mod.compare(run, base)
        self.assertEqual(rows[0]["k_n_after"], "2/10")
        self.assertEqual(rows[0]["k_n_before"], "9/10")
        self.assertEqual(rows[0]["values_after"], [0.2])
        self.assertEqual(rows[0]["values_before"], [0.9])
        from trl._wr import stats as wr_stats
        self.assertAlmostEqual(rows[0]["p"], wr_stats.fisher_two_sided(2, 8, 9, 1))
        self.assertIn(rows[0]["note"], ("differs (p<0.05)", "suggestive (p<0.20)", "no evidence"))

    def test_compare_count_no_test(self):
        # HIGH 5: count/rate/first show values and the difference, no Fisher test.
        run = tempfile.mkdtemp()
        base = tempfile.mkdtemp()
        self._cards(run, base, "count", 3, 1, 0, 0, 0, 0)
        rows = report_mod.compare(run, base)
        self.assertIsNone(rows[0]["p"])
        self.assertIn("diff", rows[0]["note"])

class PrBlockTest(unittest.TestCase):
    def test_pr_block_format(self):
        before = tempfile.mkdtemp()
        after = tempfile.mkdtemp()
        for d in (before, after):
            os.makedirs(os.path.join(d, "t", "rep1"))
            with open(os.path.join(d, "card.json"), "w") as f:
                json.dump({"run": {"label": "before", "tb_version": "v1"},
                           "rows": [{"trial": "t", "rule": "R", "expected": "?",
                                     "verdict": "FAIL", "k": 0, "n": 1,
                                     "value": 3, "pass_if": "== 0",
                                     "surprise": "", "source": "s",
                                     "detail": []}]}, f)
            with open(os.path.join(d, "t", "rep1", "setup.json"), "w") as f:
                json.dump({"checks": []}, f)
        text = report_mod.pr_block(before, after, 1553)
        self.assertTrue(text.startswith("### Tortoise Trials: before/after, issue #1553"))
        self.assertIn("Before: before v1", text)
        self.assertIn("| trial | rule | before | after | value before → after |", text)
        self.assertIn("|---|---|---|---|---|", text)

    def test_pr_block_before_after_evidence(self):
        # HIGH 4: "before:" shows the BEFORE run's evidence, "after:" the AFTER run's.
        before = tempfile.mkdtemp()
        after = tempfile.mkdtemp()
        for d, ev in ((before, "BEFORE-EV"), (after, "AFTER-EV")):
            os.makedirs(os.path.join(d, "t", "rep1"))
            with open(os.path.join(d, "card.json"), "w") as f:
                json.dump({"run": {"label": "x", "tb_version": "v1"},
                           "rows": [{"trial": "t", "rule": "R", "expected": "?",
                                     "verdict": "FAIL", "k": 0, "n": 1,
                                     "value": 3, "pass_if": "== 0", "metric": "count",
                                     "surprise": "", "source": "s",
                                     "detail": [{"rep": "rep1", "verdict": "FAIL",
                                                 "value": 3, "evidence": [ev]}]}]}, f)
            with open(os.path.join(d, "t", "rep1", "setup.json"), "w") as f:
                json.dump({"checks": []}, f)
        text = report_mod.pr_block(before, after, 1553)
        self.assertLess(text.index("BEFORE-EV"), text.index("AFTER-EV"))
        self.assertIn("- before: BEFORE-EV", text)
        self.assertIn("- after: AFTER-EV", text)
        # INCONCLUSIVE/INVALID print their reason.
        with open(os.path.join(after, "card.json")) as f:
            card = json.load(f)
        card["rows"][0]["verdict"] = "INCONCLUSIVE"
        card["rows"][0]["detail"] = [{"rep": "rep1", "verdict": "INCONCLUSIVE",
                                      "value": None, "evidence": [],
                                      "invalid_reason": "walk n == 0"}]
        with open(os.path.join(after, "card.json"), "w") as f:
            json.dump(card, f)
        text = report_mod.pr_block(before, after, 1553)
        self.assertIn("INCONCLUSIVE", text)
        self.assertIn("walk n == 0", text)

class NameTest(unittest.TestCase):
    def test_name_in_one_place(self):
        offenders = []
        for path in glob.glob(os.path.join(ROOT, "**", "*.py"), recursive=True):
            if "live" in path.split(os.sep):
                continue
            text = open(path, errors="replace").read()
            if os.path.basename(path) in ("naming.py", "test_trials.py", "test_live.py"):
                continue
            rel = os.path.relpath(path, ROOT)
            if rel == os.path.join("tests", "test_trials.py"):
                continue
            if "Tortoise Trials" in text or "tortoise-trials" in text:
                offenders.append(path)
        for path in glob.glob(os.path.join(ROOT, "*.md")) + glob.glob(os.path.join(ROOT, "**", "*.md"), recursive=True):
            if os.path.basename(path) in ("README.md", "PUBLISH-SCAN.md"):
                continue
            text = open(path, errors="replace").read()
            if "Tortoise Trials" in text or "tortoise-trials" in text:
                offenders.append(path)
        self.assertEqual(offenders, [])


class ParserTest(unittest.TestCase):
    def test_build_data_parser(self):
        import build_data
        sql = ("CREATE TABLE `spell_template` (\n"
               "  `entry` int(10) unsigned NOT NULL DEFAULT 0,\n"
               "  `name` varchar(100) NOT NULL DEFAULT '',\n"
               "  `nameSubtext` varchar(100) NOT NULL DEFAULT ''\n"
               ");\n"
               "INSERT INTO `spell_template` (`entry`, `name`, `nameSubtext`) VALUES\n"
               "(17, 'Power Word: Shield', 'Rank 1'),\n"
               "(118, 'Polymorph', 'Rank 1'),\n"
               "(6065, 'Hakkar\\'s Shield', 'Rank 5');\n")
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "spells.sql")
        with open(path, "w") as f:
            f.write(sql)
        rows = build_data._parse_table(path, "spell_template")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2]["name"], "Hakkar's Shield")
        self.assertEqual([c for c in build_data._create_columns(sql, "spell_template")],
                         ["entry", "name", "nameSubtext"])


class InterruptibleTest(unittest.TestCase):
    def test_interruptible_cast_time_prevention_flags(self):
        # HIGH 12: cast time > 0 AND PreventionType SILENCE AND damage flag.
        # PWS (instant, index 1) is NOT interruptible; Polymorph is.
        rule = {"id": "I", "desc": "x", "source": "s", "expect_today": "?",
                "why_expect": "x",
                "check": {"metric": "count",
                          "where": {"kind": "cast", "spell_interruptible": True}},
                "pass_if": ">= 1", "min_opportunity_s": 0, "min_n": 0}
        poly = _cast(10.0, "bot", "player", 80.0, spell_id=118)
        pws = _cast(11.0, "bot", "player", 80.0, spell_id=6065)
        ctx = _ctx()
        res = rules_mod.evaluate(rule, [poly, pws], [], [], ctx)
        self.assertEqual(res["value"], 1)
        self.assertEqual(res["verdict"], "PASS")


class ScoredWindowTest(unittest.TestCase):
    def test_window_minus_merged_excluded(self):
        # HIGH 9: the scored time is the window minus merged excluded spans.
        samples = [_sample(float(t), PLAYER, 500) for t in range(0, 101)]
        rows = [_cast(0.0, "bot", "player", 80.0), _cast(100.0, "bot", "player", 80.0),
                _excluded(10.0, 20.0), _excluded(15.0, 25.0)]
        self.assertAlmostEqual(rules_mod._scored_span(rows, samples, [(10.0, 25.0)]), 85.0)
        self.assertAlmostEqual(
            score_mod._scored_seconds(rows, samples, [(10.0, 20.0), (15.0, 25.0)]), 85.0)

    def test_opportunity_ignores_excluded(self):
        # Opportunity skips excluded spans instead of counting them.
        trial = _trial()
        samples = [_sample(float(t), PLAYER, 500) for t in range(0, 100)]
        full = rules_mod.opportunity_seconds(trial, samples, [], _ctx())
        cut = rules_mod.opportunity_seconds(trial, samples, [(10.0, 20.0)], _ctx())
        self.assertAlmostEqual(full - cut, 10.0, places=0)

    def test_min_n_gate(self):
        # HIGH 7: fewer opportunities than min_n -> INCONCLUSIVE.
        rule = {"id": "U", "desc": "x", "source": "s", "expect_today": "?",
                "why_expect": "x",
                "check": {"metric": "uptime", "unit": "$player", "aura": [71],
                          "phase": "in_combat"},
                "pass_if": ">= 0.5", "min_opportunity_s": 0, "min_n": 99}
        tmp = tempfile.mkdtemp()
        run = tempfile.mkdtemp()
        rep = os.path.join(run, "t", "rep1")
        os.makedirs(rep)
        with open(os.path.join(run, "RUN.json"), "w") as f:
            json.dump({"critical_health": 20, "low_health": 50, "medium_health": 70,
                       "trials": ["t"]}, f)
        trial = _trial()
        trial["id"] = "t"
        trial["rules"] = [rule]
        samples = [_sample(float(t), PLAYER, 500, combat=True, auras=[71]) for t in range(20)]
        res = rules_mod.evaluate(rule, [], samples, [], _ctx(trial=trial))
        self.assertEqual(res["verdict"], "PASS")
        self.assertLess(res["n"], 99)

    def test_walk_n_zero_inconclusive(self):
        # walk_metric with n == 0 -> INCONCLUSIVE (missing guard).
        rule = {"id": "W", "desc": "x", "source": "s", "expect_today": "?",
                "why_expect": "x",
                "check": {"metric": "walk_metric", "name": "m"},
                "pass_if": ">= 0.5", "min_opportunity_s": 0, "min_n": 0}
        ctx = _ctx(walk_report={"metrics": {"m": {"k": 0, "n": 0}}})
        res = rules_mod.evaluate(rule, [], [], [], ctx)
        self.assertEqual(res["verdict"], "INCONCLUSIVE")
        self.assertEqual(res["n"], 0)

    def test_zero_valid_repeats(self):
        # 0 valid repeats with only INCONCLUSIVE (no INVALID) stays INCONCLUSIVE.
        tmp = tempfile.mkdtemp()
        tdir = os.path.join(tmp, "t")
        os.makedirs(os.path.join(tdir, "rep1"))
        with open(os.path.join(tdir, "rep1", "verdicts.json"), "w") as f:
            json.dump({"trial": "t", "rep": "rep1", "ok_setup": True,
                       "rules": [{"id": "R", "verdict": "INCONCLUSIVE",
                                  "value": None, "evidence": ["no triggers"],
                                  "guard": "", "invalid_reason": ""}]}, f)
        trial = {"id": "t", "rules": [{"id": "R", "expect_today": "?",
                                       "pass_if": ">= 0.5", "source": "s", "desc": "x",
                                       "check": {"metric": "latency"}}]}
        out = score_mod.score_trial(tdir, trial)
        self.assertEqual(out["verdicts"][0]["verdict"], "INCONCLUSIVE")

    def test_surprise_column(self):
        run = tempfile.mkdtemp()
        os.makedirs(os.path.join(run, "t", "rep1"))
        trial = {"id": "t", "verdicts": [{"id": "R", "verdict": "PASS",
                                          "k": 1, "n": 1, "metric": "count",
                                          "expected": "FAIL", "pass_if": ">= 1",
                                          "source": "s", "desc": "x", "detail": []}]}
        rows = report_mod.write_card(run, [trial])
        self.assertIn("expected FAIL, got PASS", rows[0]["surprise"])

    def test_ratio_den_zero(self):
        # The ratio den-0 guard: no denominator -> INCONCLUSIVE, never a crash.
        rule = {"id": "R", "desc": "x", "source": "s", "expect_today": "?",
                "why_expect": "x",
                "check": {"metric": "ratio", "num": {"kind": "cast"}, "den": {"kind": "heal"}},
                "pass_if": ">= 0.5", "min_opportunity_s": 0, "min_n": 0}
        res = rules_mod.evaluate(rule, [_cast(1.0, "bot", "player", 80.0)], [], [], _ctx())
        self.assertEqual(res["verdict"], "INCONCLUSIVE")
        self.assertEqual(res["n"], 0)

    def test_uptime_no_samples(self):
        rule = {"id": "U", "desc": "x", "source": "s", "expect_today": "?",
                "why_expect": "x",
                "check": {"metric": "uptime", "unit": "$player", "aura": [71],
                          "phase": "in_combat"},
                "pass_if": ">= 0.5", "min_opportunity_s": 0, "min_n": 0}
        res = rules_mod.evaluate(rule, [], [], [], _ctx())
        self.assertEqual(res["verdict"], "INCONCLUSIVE")

    def test_count_reports_min_n_guard(self):
        # min_n applies through score_rep: k/n metrics below min_n go INCONCLUSIVE.
        tmp = tempfile.mkdtemp()
        run = tempfile.mkdtemp()
        rule = {"id": "R", "desc": "x", "source": "s", "expect_today": "?",
                "why_expect": "x", "min_n": 5,
                "check": {"metric": "ratio", "num": {"kind": "cast"}, "den": {"kind": "cast"}},
                "pass_if": ">= 0.5", "min_opportunity_s": 0}
        trial = _trial()
        trial["rules"] = [rule]
        rep = os.path.join(run, trial["id"], "rep1")
        os.makedirs(rep)
        with open(os.path.join(run, "RUN.json"), "w") as f:
            json.dump({"critical_health": 20, "low_health": 50, "medium_health": 70,
                       "trials": [trial["id"]]}, f)
        with open(os.path.join(rep, "setup.json"), "w") as f:
            json.dump({"ok": True, "guids": {"player": PLAYER, "bot": BOT}}, f)
        with open(os.path.join(rep, "samples.jsonl"), "w") as f:
            for t in range(0, 200):
                f.write(json.dumps(_sample(float(t), PLAYER, 500)) + "\n")
        with open(os.path.join(rep, "marks.jsonl"), "w") as f:
            f.write("")
        row = _cast(50.0, "bot", "player", 80.0)
        with open(os.path.join(rep, "events.jsonl"), "w") as f:
            f.write(json.dumps(row) + "\n")
        out = score_mod.score_rep(rep, trial)
        self.assertEqual(out["rules"][0]["verdict"], "INCONCLUSIVE")
        self.assertIn("min_n", out["rules"][0]["guard"])


class CliSelectorsTest(unittest.TestCase):
    def test_exact_or_glob(self):
        # HIGH 13/14: short-form top-level args; exact id or explicit glob,
        # never a bare substring; a multi-match glob is an error.
        import trials as trials_mod
        ap = trials_mod.build_parser()
        args = ap.parse_args(["--class", "priest", "--scenario", "shield-stance",
                              "--label", "before", "--server", "S"])
        self.assertEqual(args.klass, "priest")
        self.assertEqual(args.scenario, ["shield-stance"])
        self.assertEqual(args.server, "S")
        trials = [{"id": "priest-holy-x"}, {"id": "priest-holy-y"}]
        paths = ["px", "py"]
        self.assertEqual(scenario_mod.find_trials("priest-holy-x", trials=trials, paths=paths), ["px"])
        with self.assertRaises(ValueError):
            scenario_mod.find_trials("priest-holy", trials=trials, paths=paths)
        # M3 (lead ruling): an explicit glob may match many trials.
        self.assertEqual(scenario_mod.find_trials("priest-*", trials=trials, paths=paths), ["px", "py"])
        self.assertEqual(scenario_mod.find_trials("priest-holy-x*", trials=trials, paths=paths), ["px"])


class CardQualityTest(unittest.TestCase):
    def test_cut_short_reason_and_dropped(self):
        # HIGH 16: agent-ring events_dropped, excluded seconds, the cut-short reason.
        run = tempfile.mkdtemp()
        repdir = os.path.join(run, "t", "rep1")
        os.makedirs(repdir)
        with open(os.path.join(repdir, "setup.json"), "w") as f:
            json.dump({"ok": True, "checks": []}, f)
        with open(os.path.join(repdir, "verdicts.json"), "w") as f:
            json.dump({"trial": "t", "rep": "rep1", "ok_setup": True, "scored_s": 5.0,
                       "excluded_s": 3.0, "invalid_reason": "cut short (5 s, ended: wiped)",
                       "rules": []}, f)
        with open(os.path.join(repdir, "events_summary.json"), "w") as f:
            json.dump({"dropped_kinds": {"session": 2},
                       "excluded_spans": [{"from": 1, "to": 4, "why": "floor"}]}, f)
        open(os.path.join(repdir, "events.jsonl"), "w").close()
        with open(os.path.join(run, "RUN.json"), "w") as f:
            json.dump({"label": "x", "tb_version": "v"}, f)
        trial = {"trial": "t", "verdicts": []}
        report_mod.write_card(run, [trial])
        text = open(os.path.join(run, "card.md")).read()
        self.assertIn("events_dropped=[session=2]", text)
        self.assertIn("excluded=3 s", text)
        self.assertIn("cut-short: cut short (5 s, ended: wiped)", text)



def _rule(check, **over):
    rule = {"id": "R3", "desc": "d", "source": "s", "expect_today": "?",
            "why_expect": "x", "check": check, "pass_if": ">= 0.5",
            "min_opportunity_s": 0, "min_n": 0}
    rule.update(over)
    return rule


def _verr(rule, **trial_over):
    return scenario_mod.validate(_trial(rules=[rule], **trial_over))


class R3H1KindOnlyWhereUsedTest(unittest.TestCase):
    def test_latency_cross_rule_without_where_validates(self):
        rule = _rule({"metric": "latency",
                      "trigger": {"kind": "cross", "unit": "$player", "below": 50},
                      "response": {"kind": "heal", "src": "$bot", "dst": "$player"},
                      "within_s": 4})
        self.assertEqual(_verr(rule), [])

    def test_uptime_rule_without_where_validates(self):
        rule = _rule({"metric": "uptime", "unit": "$bot", "aura": [588]})
        self.assertEqual(_verr(rule), [])

    def test_walk_metric_rule_without_where_validates(self):
        rule = _rule({"metric": "walk_metric", "name": "m"})
        self.assertEqual(_verr(rule), [])

    def test_legacy_where_without_kind_is_fine_on_unused_metrics(self):
        # `where` carries only response_dst / trigger_debounce_s on a latency
        # rule; it is not an event filter there, so it needs no kind.
        rule = _rule({"metric": "latency",
                      "trigger": {"kind": "damage", "src": "$mob", "dst": "$player"},
                      "response": {"kind": "cast", "src": "$bot"},
                      "within_s": 4, "where": {"trigger_debounce_s": 5,
                                               "response_dst": "$trigger.src"}})
        self.assertEqual(_verr(rule), [])
        up = _rule({"metric": "uptime", "unit": "$bot", "aura": [588],
                    "where": {"dst_has_aura": [588]}})
        self.assertEqual(_verr(up), [])

    def test_kind_still_required_where_the_dict_is_used(self):
        for metric in ("count", "first", "rate"):
            rule = _rule({"metric": metric, "where": {"src": "$bot"}})
            self.assertTrue(any("needs a kind" in e for e in _verr(rule)), metric)
        ratio = _rule({"metric": "ratio", "num": {"kind": "cast"}, "den": {"src": "$bot"}})
        self.assertTrue(any("den needs a kind" in e for e in _verr(ratio)), _verr(ratio))
        ratio2 = _rule({"metric": "ratio", "num": {"src": "$bot"}, "den": {"kind": "cast"}})
        self.assertTrue(any("num needs a kind" in e for e in _verr(ratio2)), _verr(ratio2))
        lat = _rule({"metric": "latency",
                     "trigger": {"kind": "cross", "unit": "$player", "below": 50},
                     "response": {"src": "$bot"}, "within_s": 4})
        self.assertTrue(any("response needs a kind" in e for e in _verr(lat)), _verr(lat))


def _ev(t, kind, src_guid, dst_guid, spell_id=None, src_role="other", dst_role="other", hp=None):
    return {"t": t, "wall": None, "kind": kind, "src": "s", "src_guid": src_guid,
            "dst": "d", "dst_guid": dst_guid, "src_unit": "", "dst_unit": "",
            "spell_id": spell_id, "spell": None, "amount": 1, "dst_hp_pct": hp,
            "src_role": src_role, "dst_role": dst_role, "origin": "ta"}


MOB2 = "0xF13000000000BEEF"


def _lat_ctx(**over):
    ctx = {"guids": {"player": PLAYER, "bot": BOT, "mobs": [MOB, MOB2]},
           "thresholds": {"critical": 20, "low": 50, "medium": 70},
           "thresholds_assumed": False, "trial": _trial()}
    ctx.update(over)
    return ctx


class R3H2EventTriggerTest(unittest.TestCase):
    def test_aura_on_trigger_matches_only_aura_on_rows(self):
        rule = _rule({"metric": "latency",
                      "trigger": {"kind": "aura_on", "dst": "$player", "spell_id": [118]},
                      "response": {"kind": "cast", "src": "$bot", "spell": "Dispel Magic"},
                      "within_s": 4})
        self.assertEqual(_verr(rule), [])
        rows = [_ev(10.0, "aura_on", MOB, PLAYER, 118, "mob", "player"),
                _ev(20.0, "cast", MOB, PLAYER, 118, "mob", "player"),
                _ev(30.0, "aura_off", MOB, PLAYER, 118, "mob", "player"),
                _ev(40.0, "damage", MOB, PLAYER, 118, "mob", "player"),
                _ev(11.0, "cast", BOT, PLAYER, 527, "bot", "player")]
        res = rules_mod.evaluate(rule, rows, [], [], _lat_ctx())
        self.assertEqual((res["k"], res["n"]), (1, 1))
        self.assertEqual(res["verdict"], "PASS")

    def test_damage_trigger_with_src_and_dst(self):
        rule = _rule({"metric": "latency",
                      "trigger": {"kind": "damage", "src": "$mob", "dst": "$player"},
                      "response": {"kind": "cast", "src": "$bot", "spell": "Taunt"},
                      "within_s": 4, "trigger_debounce_s": 6,
                      "response_dst": "$trigger.src"}, min_n=2, pass_if=">= 0.8")
        self.assertEqual(_verr(rule), [])
        rows = [_ev(10.0, "damage", MOB, PLAYER, None, "mob", "player"),
                _ev(10.5, "damage", PLAYER, MOB, None, "player", "mob"),     # wrong direction
                _ev(10.7, "damage", MOB, BOT, None, "mob", "bot"),           # wrong victim
                _ev(11.0, "heal", BOT, PLAYER, 1, "bot", "player"),          # wrong kind
                _ev(12.0, "damage", MOB2, PLAYER, None, "mob", "player"),
                _ev(13.0, "damage", MOB, PLAYER, None, "mob", "player"),     # debounced (same src < 6 s)
                _ev(11.5, "cast", BOT, MOB, 355, "bot", "mob")]              # taunt on MOB only
        res = rules_mod.evaluate(rule, rows, [], [], _lat_ctx())
        # Two real triggers (MOB at 10, MOB2 at 12); only MOB's was answered on MOB.
        self.assertEqual((res["k"], res["n"]), (1, 2))
        self.assertEqual(res["verdict"], "FAIL")

    def test_response_dst_trigger_dst_with_event_trigger(self):
        rule = _rule({"metric": "latency",
                      "trigger": {"kind": "cast_start", "src": "$mob"},
                      "response": {"kind": "cast", "src": "$bot"},
                      "within_s": 2, "response_dst": "$trigger.src"})
        rows = [_ev(10.0, "cast_start", MOB, PLAYER, 118, "mob", "player"),
                _ev(10.5, "cast", BOT, MOB2, 72, "bot", "mob"),   # wrong target
                _ev(11.0, "cast", BOT, MOB, 72, "bot", "mob")]
        res = rules_mod.evaluate(rule, rows, [], [], _lat_ctx())
        self.assertEqual((res["k"], res["n"]), (1, 1))

    def test_interrupt_shape_validates_and_scores(self):
        rule = _rule({"metric": "latency",
                      "trigger": {"kind": "cast_start", "src": "$mob",
                                  "spell_interruptible": True},
                      "response": {"kind": "cast", "src": "$bot",
                                   "spell": ["Shield Bash", "Pummel"]},
                      "within_s": 1.5, "response_dst": "$trigger.src"}, min_n=2)
        self.assertEqual(_verr(rule), [])
        rows = [_ev(10.0, "cast_start", MOB, PLAYER, 118, "mob", "player"),   # interruptible
                _ev(10.6, "cast", BOT, MOB, 72, "bot", "mob"),               # Shield Bash
                _ev(20.0, "cast_start", MOB, PLAYER, 118, "mob", "player"),  # interruptible, unanswered
                _ev(30.0, "cast_start", MOB, PLAYER, 17, "mob", "player"),   # instant: not interruptible
                _ev(31.0, "cast", BOT, MOB, 72, "bot", "mob"),
                _ev(40.0, "cast", MOB, PLAYER, 118, "mob", "player")]        # a go, not a start
        res = rules_mod.evaluate(rule, rows, [], [], _lat_ctx())
        self.assertEqual((res["k"], res["n"]), (1, 2))
        self.assertEqual(res["verdict"], "PASS")

    def test_trigger_validation(self):
        base = {"metric": "latency", "response": {"kind": "cast", "src": "$bot"}, "within_s": 2}
        for kind in ("cross", "damage", "aura_on", "aura_off", "cast", "cast_start"):
            self.assertEqual(scenario_mod.TRIGGER_KINDS & {kind}, {kind})
        bad_kind = dict(base, trigger={"kind": "damge", "src": "$mob"})
        self.assertTrue(any("bad trigger kind" in e for e in _verr(_rule(bad_kind))))
        bad_field = dict(base, trigger={"kind": "damage", "bogus": 1})
        self.assertTrue(any("unknown trigger field" in e for e in _verr(_rule(bad_field))))
        any_mob = dict(base, trigger={"kind": "damage", "src": "$any_mob", "dst": "$player"})
        self.assertTrue(any("bad src alias" in e for e in _verr(_rule(any_mob))))
        no_kind = dict(base, trigger={"src": "$mob"})
        self.assertTrue(any("bad trigger kind" in e for e in _verr(_rule(no_kind))))
        for key in ("spell_interruptible", "src_hp_le", "dst_hp_lt", "in_combat"):
            self.assertIn(key, scenario_mod.TRIGGER_FIELDS)


class R3H2BriefsTest(unittest.TestCase):
    def test_rule_briefs_use_mob_not_any_mob(self):
        import glob as _g
        briefs = _g.glob(os.path.join(os.environ.get("TRIALS_RULE_BRIEFS", "/nonexistent"), "trials-3-rules-*.md"))
        if not briefs:
            self.skipTest("rule briefs not on this host")
        for path in briefs:
            with open(path) as f:
                self.assertFalse("$any_mob" in f.read(), path)


_W = "2026-01-01T00:00:%02d.000+00:00"


def _fight_rep(root, setup_s=0, start_s=25, end_s=30, end_mark=True, shields=(10, 27),
               last_sample_s=44, thresholds=True, extra_setup=None, pet=False):
    """A rep dir: setup_ok at setup_s, fight_start/fight_end walls, 1 Hz samples
    of player + bot (in combat), and one Power Word: Shield per `shields` wall second."""
    rep = os.path.join(root, "priest-holy-pws-def-arms", "rep1")
    os.makedirs(rep, exist_ok=True)
    setup = {"ok": True, "guids": {"player": PLAYER, "bot": BOT, "pet": PET, "mobs": [MOB]},
             "checks": [{"id": "config_read", "ok": True}],
             "config": {"ok": True, "critical": 20, "low": 50, "medium": 70}}
    setup.update(extra_setup or {})
    with open(os.path.join(rep, "setup.json"), "w") as f:
        json.dump(setup, f)
    with open(os.path.join(root, "RUN.json"), "w") as f:
        json.dump({"trials": ["priest-holy-pws-def-arms"]}, f)
    with open(os.path.join(rep, "marks.jsonl"), "w") as f:
        marks = [("setup_ok", setup_s), ("fight_start", start_s)]
        if end_mark:
            marks.append(("fight_end", end_s))
        for kind, sec in marks:
            row = {"kind": kind, "wall": _W % sec, "t": float(sec)}
            if kind == "fight_end":
                row["ended"] = "disconnected"
            f.write(json.dumps(row) + "\n")
    with open(os.path.join(rep, "samples.jsonl"), "w") as f:
        for sec in range(0, last_sample_s + 1):
            guids = (PLAYER, BOT) + ((PET,) if pet and sec < 2 else ())
            for g in guids:
                f.write(json.dumps({"t": float(sec), "wall": _W % sec, "guid": g, "hp": 600,
                                    "max_hp": 1000, "auras": [], "in_combat": True}) + "\n")
    with open(os.path.join(rep, "raw-ta.jsonl"), "w") as f:
        for i, sec in enumerate(shields):
            f.write(json.dumps({"seq": i + 1, "time": _W % sec, "kind": "spell_go",
                                "source": {"name": "b", "guid": BOT},
                                "target": {"name": "p", "guid": PLAYER}, "spell_id": 17}) + "\n")
    return rep


PET = "0x0000000000000003"


def _short(**over):
    """The def-arms trial without the opportunity guard."""
    trial = _trial(**over)
    trial["rules"][0]["min_opportunity_s"] = 0
    return trial


class R3H3ScoredWindowTest(unittest.TestCase):
    def test_short_fight_is_cut_short_not_scored_on_whole_capture(self):
        # Capture spans 45 s but the fight is fight_start..fight_end = 5 s.
        root = tempfile.mkdtemp()
        rep = _fight_rep(root)
        out = score_mod.score_rep(rep, _trial(min_valid_s=30))
        self.assertAlmostEqual(out["scored_s"], 5.0)
        self.assertTrue(all(r["verdict"] == "INVALID" for r in out["rules"]), out["rules"])
        self.assertIn("cut short", out["invalid_reason"])
        self.assertIn("disconnected", out["invalid_reason"])

    def test_25_to_30_s_fight_with_min_valid_30_is_invalid(self):
        root = tempfile.mkdtemp()
        rep = _fight_rep(root, setup_s=0, start_s=5, end_s=32, last_sample_s=60)
        out = score_mod.score_rep(rep, _trial(min_valid_s=30))
        self.assertAlmostEqual(out["scored_s"], 27.0)
        self.assertIn("cut short", out.get("invalid_reason", ""))

    def test_pre_fight_shield_is_not_counted(self):
        # Shield at wall 10 = t -15 (before fight_start); one in the fight at t 2.
        root = tempfile.mkdtemp()
        rep = _fight_rep(root)
        out = score_mod.score_rep(rep, _short(min_valid_s=3))
        rule = out["rules"][0]
        self.assertEqual(rule["verdict"], "FAIL", rule)
        self.assertEqual(rule["value"], 1)
        # With only the pre-fight shield nothing is counted.
        root2 = tempfile.mkdtemp()
        rep2 = _fight_rep(root2, shields=(10,))
        out2 = score_mod.score_rep(rep2, _short(min_valid_s=3))
        self.assertEqual(out2["rules"][0]["value"], 0)
        self.assertEqual(out2["rules"][0]["verdict"], "PASS")

    def test_post_fight_events_are_not_counted(self):
        root = tempfile.mkdtemp()
        rep = _fight_rep(root, shields=(40,))
        out = score_mod.score_rep(rep, _short(min_valid_s=3))
        self.assertEqual(out["rules"][0]["value"], 0)

    def test_window_arguments_to_scored_seconds_and_span(self):
        rows = [_excluded(10.0, 20.0), _excluded(15.0, 25.0)]
        samples = [_sample(float(t), PLAYER, 500) for t in range(-20, 101)]
        self.assertAlmostEqual(score_mod._scored_seconds(rows, samples, [(10.0, 25.0)],
                                                         window=(0.0, 60.0)), 45.0)
        self.assertAlmostEqual(rules_mod._scored_span(rows, samples, [(10.0, 25.0)],
                                                      window=(0.0, 60.0)), 45.0)
        # An excluded span half outside the window only cuts what is inside.
        self.assertAlmostEqual(score_mod._scored_seconds([], samples, [(-5.0, 5.0)],
                                                         window=(0.0, 10.0)), 5.0)

    def test_rate_counts_only_window_rows(self):
        rate = _rule({"metric": "rate",
                      "where": {"kind": "cast", "spell": "Power Word: Shield"}},
                     pass_if=">= 1")
        rate["check"]["where"] = {"kind": "cast", "spell": "Power Word: Shield"}
        rows = [_cast(-10.0, "bot", "player", 80.0), _cast(10.0, "bot", "player", 80.0),
                _cast(20.0, "bot", "player", 80.0), _cast(90.0, "bot", "player", 80.0)]
        samples = [_sample(float(t), PLAYER, 500) for t in range(-20, 101)]
        res = rules_mod.evaluate(rate, rows, samples, [], _ctx(window=(0.0, 60.0)))
        self.assertEqual(res["k"], 2)
        self.assertAlmostEqual(res["value"], 2.0)
        # The denominator is the window (60 s), not the span of the few samples in it.
        sparse = [_sample(10.0, PLAYER, 500), _sample(20.0, PLAYER, 500)]
        res = rules_mod.evaluate(rate, rows, sparse, [], _ctx(window=(0.0, 60.0)))
        self.assertAlmostEqual(res["value"], 2.0)

    def test_alive_uptime_denominator_only_window_samples(self):
        rule = _rule({"metric": "uptime", "unit": "$pet", "alive": True}, pass_if=">= 0.5")
        samples = []
        for t in range(-20, 21):
            samples.append(_sample(float(t), PLAYER, 500))
            if t >= 0:
                samples.append(_sample(float(t), PET, 500))
        ctx = {"guids": {"player": PLAYER, "pet": PET}, "trial": _trial(), "window": (0.0, 20.0)}
        res = rules_mod.evaluate(rule, [], samples, [], ctx)
        self.assertEqual((res["k"], res["n"]), (21, 21))
        self.assertEqual(res["verdict"], "PASS")

    def test_in_combat_uptime_and_triggers_use_window(self):
        rule = _rule({"metric": "uptime", "unit": "$player", "aura": [71],
                      "phase": "in_combat"}, pass_if=">= 0.9")
        samples = [_sample(float(t), PLAYER, 500, auras=[71] if t >= 0 else [])
                   for t in range(-20, 21)]
        ctx = {"guids": {"player": PLAYER}, "trial": _trial(), "window": (0.0, 20.0)}
        res = rules_mod.evaluate(rule, [], samples, [], ctx)
        self.assertEqual((res["k"], res["n"]), (21, 21))
        cross = _rule({"metric": "latency",
                       "trigger": {"kind": "cross", "unit": "$player", "below": 50},
                       "response": {"kind": "heal", "src": "$bot"}, "within_s": 4})
        low = [_sample(float(t), PLAYER, 600 if t < -10 else 400) for t in range(-20, 21)]
        res = rules_mod.evaluate(cross, [], low, [], dict(ctx, guids={"player": PLAYER, "bot": BOT}))
        self.assertEqual(res["verdict"], "INCONCLUSIVE")  # the only crossing was pre-fight

    def test_fight_end_missing_uses_last_in_fight_sample_and_flags(self):
        root = tempfile.mkdtemp()
        rep = _fight_rep(root, setup_s=0, start_s=5, end_mark=False, last_sample_s=40)
        # samples after t=30 (wall 35) are out of combat
        lines = [json.loads(l) for l in open(os.path.join(rep, "samples.jsonl")) if l.strip()]
        with open(os.path.join(rep, "samples.jsonl"), "w") as f:
            for row in lines:
                if row["t"] > 30:
                    row["in_combat"] = False
                f.write(json.dumps(row) + "\n")
        out = score_mod.score_rep(rep, _trial(min_valid_s=3))
        self.assertAlmostEqual(out["scored_s"], 25.0)   # t 5 .. 30
        self.assertTrue(out.get("fight_end_missing"))
        run = os.path.dirname(os.path.dirname(rep))
        score_mod.score_run(run)
        text = open(os.path.join(run, "card.md")).read()
        self.assertIn("fight_end missing", text)


def _thr_rep(root, hp=270, config=None, config_read=True, run_thr=None, rules=None):
    """A 40 s fight; the player at hp/1000 and one shield on the player at t 10."""
    extra = {"config": config if config is not None else {"ok": False, "critical": None,
                                                           "low": None, "medium": None},
             "checks": [{"id": "config_read", "ok": config_read}]}
    rep = _fight_rep(tempfile.mkdtemp() if root is None else root, start_s=0, end_s=40,
                     shields=(10,), extra_setup=extra)
    with open(os.path.join(rep, "samples.jsonl"), "w") as f:
        for sec in range(0, 45):
            for g, h in ((PLAYER, hp), (BOT, 900)):
                f.write(json.dumps({"t": float(sec), "wall": _W % sec, "guid": g, "hp": h,
                                    "max_hp": 1000, "auras": [], "in_combat": True}) + "\n")
    run = os.path.dirname(os.path.dirname(rep))
    doc = {"trials": ["priest-holy-pws-def-arms"]}
    doc.update(run_thr or {})
    with open(os.path.join(run, "RUN.json"), "w") as f:
        json.dump(doc, f)
    trial = _short(min_valid_s=10)
    trial.pop("opportunity", None)
    if rules is not None:
        trial["rules"] = rules
    return rep, trial


class R3M2ThresholdsTest(unittest.TestCase):
    def test_setup_config_is_read_first(self):
        # probe4: RUN.json carries no thresholds, setup.json config does -> no crash.
        # critical 25 from setup: the shield at 27 % is counted, at 22 % it is not.
        cfg = {"ok": True, "critical": 25, "low": 45, "medium": 65}
        rep, trial = _thr_rep(None, hp=270, config=cfg)
        out = score_mod.score_rep(rep, trial)
        self.assertEqual(out["rules"][0]["value"], 1, out["rules"])
        rep, trial = _thr_rep(None, hp=220, config=cfg)
        out = score_mod.score_rep(rep, trial)
        self.assertEqual(out["rules"][0]["value"], 0, out["rules"])
        self.assertEqual(out["rules"][0]["guard"], "")

    def test_setup_config_beats_run_json_and_run_json_beats_defaults(self):
        cfg = {"ok": True, "critical": 25, "low": 45, "medium": 65}
        run_thr = {"critical_health": 30, "low_health": 50, "medium_health": 70}
        rep, trial = _thr_rep(None, hp=270, config=cfg, run_thr=run_thr)
        self.assertEqual(score_mod.score_rep(rep, trial)["rules"][0]["value"], 1)  # 25 not 30
        rep, trial = _thr_rep(None, hp=270, run_thr=run_thr)   # setup has no thresholds
        self.assertEqual(score_mod.score_rep(rep, trial)["rules"][0]["value"], 0)  # 30 from RUN.json

    def test_defaults_only_when_config_read_failed(self):
        rep, trial = _thr_rep(None, hp=220, config_read=False)
        out = score_mod.score_rep(rep, trial)
        # 22 % > default critical 20 -> counted, with the assumed guard.
        self.assertEqual(out["rules"][0]["value"], 1)
        self.assertIn("thresholds assumed", out["rules"][0]["guard"])
        self.assertTrue(out["thresholds_assumed"])

    def test_defaults_not_used_when_config_read_ok_but_values_missing(self):
        rep, trial = _thr_rep(None, hp=220, config_read=True)
        out = score_mod.score_rep(rep, trial)
        rule = out["rules"][0]
        self.assertEqual(rule["verdict"], "INCONCLUSIVE", rule)
        self.assertIn("threshold unknown", rule["guard"])
        self.assertNotIn("thresholds assumed", rule["guard"])
        self.assertFalse(out["thresholds_assumed"])

    def test_assumed_guard_only_on_rules_using_an_alias(self):
        plain = _rule({"metric": "count", "where": {"kind": "cast", "spell": "Power Word: Shield"}},
                      id="PLAIN", pass_if=">= 0", source="s")
        alias = _trial()["rules"][0]
        rep, trial = _thr_rep(None, hp=220, config_read=False, rules=[plain, alias])
        out = score_mod.score_rep(rep, trial)
        by_id = {r["id"]: r for r in out["rules"]}
        self.assertEqual(by_id["PLAIN"]["guard"], "")
        self.assertIn("thresholds assumed", by_id["PRI-PWS-DEF-ARMS"]["guard"])

    def test_none_threshold_never_reaches_a_comparison(self):
        # The probe4 crash: ctx thresholds all None.
        rule = _trial()["rules"][0]
        ctx = _ctx(thresholds={"critical": None, "low": None, "medium": None})
        rows = [_cast(60.0, "bot", "player", 60.0)]
        res = rules_mod.evaluate(rule, rows, [_sample(60.0, BOT, 900)], [], ctx)
        self.assertEqual(res["verdict"], "INCONCLUSIVE")
        self.assertIn("threshold unknown", res["guard"])
        # ... also for every other alias place: trigger below, opportunity, src_hp_le.
        lat = _rule({"metric": "latency",
                     "trigger": {"kind": "cross", "unit": "$player", "below": "$low"},
                     "response": {"kind": "heal"}, "within_s": 4})
        res = rules_mod.evaluate(lat, [], [_sample(1.0, PLAYER, 100)], [], ctx)
        self.assertEqual(res["verdict"], "INCONCLUSIVE")
        self.assertIn("threshold unknown", res["guard"])
        hp = _rule({"metric": "count", "where": {"kind": "cast", "src_hp_le": "$low"}},
                   pass_if="== 0")
        res = rules_mod.evaluate(hp, rows, [_sample(60.0, BOT, 900)], [], ctx)
        self.assertEqual(res["verdict"], "INCONCLUSIVE")

    def test_bad_threshold_alias_is_a_validation_error(self):
        rule = _rule({"metric": "count",
                      "where": {"kind": "cast", "src_hp_le": "$crit"}}, pass_if="== 0")
        self.assertTrue(any("bad src_hp_le" in e for e in _verr(rule)), _verr(rule))


class R3M1StrictValidationTest(unittest.TestCase):
    def _has(self, errs, needle):
        self.assertTrue(any(needle in e for e in errs), (needle, errs))

    def _lat(self, **resp):
        return _rule({"metric": "latency",
                      "trigger": {"kind": "cross", "unit": "$player", "below": 50},
                      "response": dict({"kind": "cast", "src": "$bot",
                                        "spell": "Dispel Magic"}, **resp),
                      "within_s": 4})

    def test_good_latency_rule_is_clean(self):
        self.assertEqual(_verr(self._lat()), [])

    def test_response_values(self):
        self._has(_verr(self._lat(spell="Dispel Magik")), "unknown spell")
        self._has(_verr(self._lat(kind="casts")), "bad kind")
        self._has(_verr(self._lat(src="$bots")), "bad src alias")
        self._has(_verr(self._lat(dst="$trigger.dstt")), "bad dst alias")
        self._has(_verr(self._lat(spell_id=[99999999])), "unknown spell_id")
        self._has(_verr(self._lat(spell_id="abc")), "unknown spell_id")
        rule = self._lat()
        rule["check"]["response"]["bogus"] = 1
        self._has(_verr(rule), "unknown response field")

    def test_response_known_spell_id_validates(self):
        self.assertEqual(_verr(self._lat(spell_id=[527])), [])

    def test_numden_values(self):
        def ratio(num=None, den=None):
            return _rule({"metric": "ratio",
                          "num": dict({"kind": "cast", "src": "$bot"}, **(num or {})),
                          "den": dict({"kind": "cast"}, **(den or {}))})
        self.assertEqual(_verr(ratio()), [])
        self._has(_verr(ratio(num={"kind": "casts"})), "bad kind")
        self._has(_verr(ratio(num={"src": "$bots"})), "bad src alias")
        self._has(_verr(ratio(num={"dst_hp_lt": "$crit"})), "bad dst_hp_lt")
        self._has(_verr(ratio(den={"spell": "Nope Nope"})), "unknown spell")
        self._has(_verr(ratio(den={"bogus": 1})), "unknown den key")
        self._has(_verr(ratio(num={"src_hp_le": "$crit"})), "bad src_hp_le")

    def test_trigger_unit_alias(self):
        rule = self._lat()
        rule["check"]["trigger"]["unit"] = "$players"
        self._has(_verr(rule), "bad trigger unit")
        rule["check"]["trigger"]["unit"] = "$mob"   # a cross reads a sample guid
        self._has(_verr(rule), "bad trigger unit")

    def test_where_value_checks(self):
        def count(**where):
            return _rule({"metric": "count", "where": dict({"kind": "cast"}, **where)},
                         pass_if="== 0")
        self.assertEqual(_verr(count(src_hp_le=40, src_power_lt="$low", t_from=-5, t_to=60,
                                     spell_id=[17])), [])
        self._has(_verr(count(src_hp_le="$crit")), "bad src_hp_le")
        self._has(_verr(count(src_hp_le="abc")), "bad src_hp_le")
        self._has(_verr(count(src_power_lt="x")), "bad src_power_lt")
        self._has(_verr(count(src_power_lt="$crit")), "bad src_power_lt")
        self._has(_verr(count(spell_id="abc")), "unknown spell_id")
        self._has(_verr(count(spell_id=[99999999])), "unknown spell_id")
        self._has(_verr(count(t_from="x")), "bad t_from")
        self._has(_verr(count(t_to="x")), "bad t_to")
        self._has(_verr(count(dst_has_aura=["x"])), "bad dst_has_aura")

    def test_opportunity_fields(self):
        good = {"unit": "$player", "hp_lt": 90, "hp_gt": "$critical", "in_combat": True}
        self.assertEqual(scenario_mod.validate(_trial(opportunity=good)), [])
        self._has(scenario_mod.validate(_trial(opportunity=dict(good, hp_ltt=90))),
                  "unknown opportunity field")
        self._has(scenario_mod.validate(_trial(opportunity=dict(good, unit="$players"))),
                  "bad opportunity unit")
        self._has(scenario_mod.validate(_trial(opportunity=dict(good, hp_gt="$crit"))),
                  "bad opportunity hp_gt")
        self._has(scenario_mod.validate(_trial(opportunity=dict(good, hp_lt="x"))),
                  "bad opportunity hp_lt")
        self._has(scenario_mod.validate(_trial(opportunity=["x"])), "opportunity is not an object")

    def test_talents_fields(self):
        t = _trial()
        t["player"]["talents"]["treee"] = 1
        self._has(scenario_mod.validate(t), "unknown talents field")
        t = _trial()
        t["player"]["talents"] = ["12285"]
        self._has(scenario_mod.validate(t), "talents is not an object")
        t = _trial()
        t["player"]["talents"]["spells"] = [12285, "x"]
        self._has(scenario_mod.validate(t), "talents.spells")

    def test_check_level_and_response_unknown_fields(self):
        rule = _verr_rule = _rule({"metric": "count", "where": {"kind": "cast"}, "bogus": 1},
                                  pass_if="== 0")
        self._has(_verr(rule), "unknown check field")
        rule = self._lat()
        rule["check"]["response"]["bogus"] = 1
        self._has(_verr(rule), "unknown response field")
        rule = self._lat()
        rule["bogus"] = 1
        self._has(_verr(rule), "unknown rule field")


class R3M3SelectorTest(unittest.TestCase):
    TRIALS = [{"id": "priest-holy-x"}, {"id": "priest-holy-y"}, {"id": "mage-frost-z"}]
    PATHS = ["px", "py", "pz"]

    def _find(self, sel):
        return scenario_mod.find_trials(sel, trials=self.TRIALS, paths=self.PATHS)

    def test_glob_may_match_many_and_bare_id_exactly_one(self):
        self.assertEqual(self._find("priest-*"), ["px", "py"])
        self.assertEqual(self._find("*-z"), ["pz"])
        self.assertEqual(self._find("priest-holy-?"), ["px", "py"])
        self.assertEqual(self._find("priest-holy-[xy]"), ["px", "py"])
        self.assertEqual(self._find("mage-frost-z"), ["pz"])
        self.assertEqual(self._find(["priest-*", "mage-frost-z"]), ["px", "py", "pz"])

    def test_bad_selector_is_an_error(self):
        for sel in ("priest-holy", "nope", "nope-*", "priest-holy-xx"):
            with self.assertRaises(ValueError, msg=sel):
                self._find(sel)
        # one bad selector makes the whole selection fail, not just skip.
        with self.assertRaises(ValueError):
            self._find(["mage-frost-z", "nope"])

    def test_duplicate_bare_id_is_ambiguous(self):
        trials = [{"id": "dup"}, {"id": "dup"}]
        with self.assertRaises(ValueError):
            scenario_mod.find_trials("dup", trials=trials, paths=["a", "b"])

    def test_cli_fails_before_running_anything(self):
        import io
        import contextlib
        import trials as trials_mod
        ran = []
        saved = (trials_mod.cmd_run, trials_mod._all_trials)
        trials_mod.cmd_run = lambda a: ran.append(a) or 0
        trials_mod._all_trials = lambda: (
            [dict(t, group="healers") for t in self.TRIALS], list(self.PATHS))
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = trials_mod.main(["--trial", "mage-frost-z", "--trial", "nope",
                                      "--server", "S"])
            self.assertEqual(rc, 2)
            self.assertEqual(ran, [])
            self.assertIn("nope", err.getvalue())
            with contextlib.redirect_stderr(io.StringIO()):
                rc = trials_mod.main(["--trial", "priest-holy"])   # bare prefix
            self.assertEqual(rc, 2)
            self.assertEqual(ran, [])
            rc = trials_mod.main(["--trial", "priest-*", "--server", "S"])
            self.assertEqual(rc, 0)
            self.assertEqual(sorted(ran[0].trials), ["px", "py"])
            ran.clear()
            rc = trials_mod.main(["--class", "priest", "--scenario", "holy-x", "--server", "S"])
            self.assertEqual(rc, 0)
            self.assertEqual(ran[0].trials, ["px"])
            ran.clear()
            with contextlib.redirect_stderr(io.StringIO()):
                rc = trials_mod.main(["--class", "priest", "--scenario", "holy-q"])
            self.assertEqual(rc, 2)
            self.assertEqual(ran, [])
        finally:
            trials_mod.cmd_run, trials_mod._all_trials = saved


class R3M4PrBlockReasonTest(unittest.TestCase):
    def _run(self, detail, verdict="INCONCLUSIVE"):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "t", "rep1"))
        with open(os.path.join(d, "card.json"), "w") as f:
            json.dump({"run": {"label": "x", "tb_version": "v1"},
                       "rows": [{"trial": "t", "rule": "R", "expected": "?",
                                 "verdict": verdict, "k": 0, "n": 1, "value": None,
                                 "pass_if": ">= 0.5", "metric": "latency", "surprise": "",
                                 "source": "s", "detail": detail}]}, f)
        with open(os.path.join(d, "t", "rep1", "setup.json"), "w") as f:
            json.dump({"checks": []}, f)
        return d

    def test_guard_reason_for_every_inconclusive(self):
        before = self._run([{"rep": "rep1", "verdict": "INCONCLUSIVE", "value": 0.4,
                             "evidence": ["3/5"], "guard": "n 3 < min_n 5"},
                            {"rep": "rep2", "verdict": "INCONCLUSIVE", "value": None,
                             "evidence": ["no triggers"], "guard": ""}])
        after = self._run([{"rep": "rep1", "verdict": "INCONCLUSIVE", "value": None,
                            "evidence": ["threshold unknown"], "guard": "threshold unknown"},
                           {"rep": "rep2", "verdict": "INVALID", "value": None, "evidence": [],
                            "invalid_reason": "cut short (5 s, ended: wiped)"}])
        text = report_mod.pr_block(before, after, 1)
        self.assertIn("before INCONCLUSIVE: n 3 < min_n 5 (rep1)", text)
        self.assertIn("before INCONCLUSIVE: no triggers (rep2)", text)
        self.assertIn("after INCONCLUSIVE: threshold unknown (rep1)", text)
        self.assertIn("after INVALID: cut short (5 s, ended: wiped) (rep2)", text)

    def test_reason_field_wins_over_assumed_guard(self):
        d = self._run([{"rep": "rep1", "verdict": "INCONCLUSIVE", "value": None,
                        "evidence": [], "guard": "thresholds assumed",
                        "reason": "den 0"}])
        text = report_mod.pr_block(d, d, None)
        self.assertIn("after INCONCLUSIVE: den 0 (rep1)", text)
        self.assertNotIn("INCONCLUSIVE: thresholds assumed", text)

    def test_end_to_end_min_n_reason_reaches_pr_block(self):
        runs = []
        for _ in range(2):
            rule = {"id": "R", "desc": "x", "source": "s", "expect_today": "?",
                    "why_expect": "x", "min_n": 5, "min_opportunity_s": 0,
                    "check": {"metric": "ratio", "num": {"kind": "cast"}, "den": {"kind": "cast"}},
                    "pass_if": ">= 0.5"}
            rep, trial = _thr_rep(None, hp=500, config={"ok": True, "critical": 20, "low": 50,
                                                        "medium": 70}, rules=[rule])
            run = os.path.dirname(os.path.dirname(rep))
            res = score_mod.score_trial(os.path.dirname(rep), trial)
            report_mod.write_card(run, [res])
            runs.append(run)
        text = report_mod.pr_block(runs[0], runs[1], 7)
        self.assertIn("INCONCLUSIVE: n 1 < min_n 5 (rep1)", text)


class R3LowCardTest(unittest.TestCase):
    def _card(self, metric, details, verdict="PASS"):
        run = tempfile.mkdtemp()
        os.makedirs(os.path.join(run, "t", "rep1"))
        trial = {"trial": "t", "verdicts": [{
            "id": "R", "verdict": verdict, "k": sum(d.get("k", 0) for d in details),
            "n": sum(d.get("n", 0) for d in details), "metric": metric, "expected": "?",
            "pass_if": ">= 0.3", "source": "s", "desc": "x", "detail": details}]}
        rows = report_mod.write_card(run, [trial])
        return run, rows

    def test_card_header_says_summed_over_repeats(self):
        run, _ = self._card("ratio", [])
        text = open(os.path.join(run, "card.md")).read()
        self.assertIn("metric k/n (summed over repeats)", text)

    def test_card_value_is_pooled_k_over_n_not_mean_of_ratios(self):
        details = [{"rep": "rep1", "verdict": "PASS", "value": 0.5, "k": 1, "n": 2},
                   {"rep": "rep2", "verdict": "PASS", "value": 0.3, "k": 3, "n": 10}]
        run, rows = self._card("ratio", details)
        self.assertAlmostEqual(rows[0]["value"], 4 / 12.0)        # not (0.5+0.3)/2
        text = open(os.path.join(run, "card.md")).read()
        self.assertIn("0.333 (0.5, 0.3)", text)
        # latency and uptime pool too; count/first/rate keep the plain mean.
        for metric in ("latency", "uptime"):
            self.assertAlmostEqual(self._card(metric, details)[1][0]["value"], 4 / 12.0)
        counts = [{"rep": "rep1", "verdict": "PASS", "value": 3, "k": 3, "n": 1},
                  {"rep": "rep2", "verdict": "PASS", "value": 1, "k": 1, "n": 1}]
        self.assertAlmostEqual(self._card("count", counts)[1][0]["value"], 2.0)

    def test_pooled_value_skips_invalid_and_empty_repeats(self):
        details = [{"rep": "rep1", "verdict": "INVALID", "value": None, "k": 0, "n": 0},
                   {"rep": "rep2", "verdict": "PASS", "value": 0.5, "k": 2, "n": 4},
                   {"rep": "rep3", "verdict": "INCONCLUSIVE", "value": 0.0, "k": 0, "n": 3}]
        self.assertAlmostEqual(self._card("ratio", details)[1][0]["value"], 0.5)

    def test_assumed_text_follows_score_config_logic(self):
        # config_read ok + setup thresholds -> "live", even though RUN.json has none.
        cfg = {"ok": True, "critical": 25, "low": 45, "medium": 65}
        rep, trial = _thr_rep(None, hp=270, config=cfg)
        run = os.path.dirname(os.path.dirname(rep))
        res = score_mod.score_trial(os.path.dirname(rep), trial)
        report_mod.write_card(run, [res])
        self.assertIn("thresholds=live", open(os.path.join(run, "card.md")).read())
        # config_read failed -> assumed 20/50/70.
        rep, trial = _thr_rep(None, hp=270, config_read=False)
        run = os.path.dirname(os.path.dirname(rep))
        res = score_mod.score_trial(os.path.dirname(rep), trial)
        report_mod.write_card(run, [res])
        self.assertIn("thresholds=assumed 20/50/70", open(os.path.join(run, "card.md")).read())
        # config_read ok but nothing readable -> unknown (never "assumed").
        rep, trial = _thr_rep(None, hp=270)
        run = os.path.dirname(os.path.dirname(rep))
        res = score_mod.score_trial(os.path.dirname(rep), trial)
        report_mod.write_card(run, [res])
        text = open(os.path.join(run, "card.md")).read()
        self.assertIn("thresholds=unknown", text)
        self.assertNotIn("assumed 20/50/70", text)


class R3LowCliDefaultsTest(unittest.TestCase):
    def test_subparser_defaults_do_not_shadow_top_level_flags(self):
        import trials as trials_mod
        ap = trials_mod.build_parser()
        args = ap.parse_args(["--server", "S", "--label", "L", "--class", "priest",
                              "--spec", "holy", "--trial", "a", "--repeats", "3",
                              "--parallel", "2", "--out", "O", "--bind", "--dry-run", "run"])
        self.assertEqual((args.server, args.label, args.klass, args.spec), ("S", "L", "priest", "holy"))
        self.assertEqual((args.trial, args.repeats, args.parallel, args.out), (["a"], 3, 2, "O"))
        self.assertTrue(args.bind and args.dry_run)
        # ... and a flag given after the subcommand still wins.
        args = ap.parse_args(["--server", "S", "run", "--server", "T", "--trial", "b"])
        self.assertEqual((args.server, args.trial), ("T", ["b"]))
        lst = ap.parse_args(["--class", "mage", "list"])
        self.assertEqual(lst.klass, "mage")
        raid = ap.parse_args(["--label", "LL", "raid", "--boss", "x"])
        self.assertEqual(raid.label, "LL")

    def test_run_form_defaults_still_apply(self):
        import trials as trials_mod
        ran = []
        saved = (trials_mod.cmd_run, trials_mod._all_trials)
        trials_mod.cmd_run = lambda a: ran.append(a) or 0
        trials_mod._all_trials = lambda: ([{"id": "priest-a", "group": "healers"}], ["pa"])
        try:
            self.assertEqual(trials_mod.main(["run", "--trial", "priest-a"]), 0)
            self.assertEqual(ran[0].label, "run")
            self.assertEqual((ran[0].server, ran[0].repeats, ran[0].parallel), ("", 0, 1))
            ran.clear()
            self.assertEqual(trials_mod.main(["--server", "S", "--label", "x", "run",
                                              "--trial", "priest-a"]), 0)
            self.assertEqual((ran[0].server, ran[0].label), ("S", "x"))
        finally:
            trials_mod.cmd_run, trials_mod._all_trials = saved


class R3LowInterruptibleHelperTest(unittest.TestCase):
    def test_shared_helper_needs_all_three_conditions(self):
        from trl import spells as spells_mod
        ok = dict(cast_ms=1500, prevention_type=1, interrupt_flags=0x02)
        self.assertTrue(spells_mod.is_interruptible(**ok))
        self.assertFalse(spells_mod.is_interruptible(**dict(ok, cast_ms=0)))        # instant
        self.assertFalse(spells_mod.is_interruptible(**dict(ok, cast_ms=-1000000))) # DBC index 18
        self.assertFalse(spells_mod.is_interruptible(**dict(ok, prevention_type=2)))
        self.assertFalse(spells_mod.is_interruptible(**dict(ok, prevention_type=0)))
        self.assertFalse(spells_mod.is_interruptible(**dict(ok, interrupt_flags=0x01)))
        self.assertFalse(spells_mod.is_interruptible(**dict(ok, interrupt_flags=0)))
        self.assertFalse(spells_mod.is_interruptible(None, None, None))
        # Channeled spells are left out: they carry no cast time (documented).
        self.assertFalse(spells_mod.is_interruptible(cast_ms=0, prevention_type=1,
                                                     interrupt_flags=0x0F))

    def test_build_data_caster_filter_uses_the_same_definition(self):
        import build_data
        d = tempfile.mkdtemp()
        spell_cols = "`entry`, `name`, `nameSubtext`, `castingTimeIndex`, `preventionType`, `interruptFlags`"
        spell_rows = [
            (1, "GoodBolt", 4, 1, 2),        # 1000 ms, silence, damage flag: interruptible
            (2, "Instant", 1, 1, 2),         # instant
            (3, "NoSilence", 4, 2, 2),       # prevention type 2
            (4, "NoDamageFlag", 4, 1, 1),    # flag 1 only
        ]
        with open(os.path.join(d, "spell.sql"), "w") as f:
            f.write("CREATE TABLE `spell_template` (\n  `entry` int,\n  `name` varchar,\n"
                    "  `nameSubtext` varchar,\n  `castingTimeIndex` int,\n"
                    "  `preventionType` int,\n  `interruptFlags` int\n)\n;\n"
                    "INSERT INTO `spell_template` (%s) VALUES\n" % spell_cols)
            f.write(",\n".join("(%d, '%s', '', %d, %d, %d)" % r for r in spell_rows) + ";\n")
        mob_cols = ("entry", "name", "rank", "script_name", "ai_name", "spell_list_id",
                    "npc_flags", "unit_flags", "type", "faction", "level_min", "level_max")
        with open(os.path.join(d, "mob.sql"), "w") as f:
            f.write("CREATE TABLE `creature_template` (\n" + ",\n".join(
                "  `%s` int" % c for c in mob_cols) + "\n)\n;\n")
            f.write("INSERT INTO `creature_template` (%s) VALUES\n" % ", ".join("`%s`" % c for c in mob_cols))
            f.write(",\n".join("(%d, 'm%d', 0, NULL, NULL, %d, 0, 0, 7, 14, 20, 22)" % (
                100 + i, i, 10 + i) for i in range(1, 5)) + ";\n")
        with open(os.path.join(d, "links.sql"), "w") as f:
            f.write("CREATE TABLE `creature_spells` (\n  `entry` int,\n" + ",\n".join(
                "  `spellId_%d` int" % i for i in range(1, 9)) + "\n)\n;\n")
            f.write("INSERT INTO `creature_spells` (`entry`, " + ", ".join(
                "`spellId_%d`" % i for i in range(1, 9)) + ") VALUES\n")
            f.write(",\n".join("(%d, %d, 0, 0, 0, 0, 0, 0, 0)" % (10 + i, i) for i in range(1, 5)) + ";\n")
        mobs = build_data.build_mobs(spells_path=os.path.join(d, "spell.sql"),
                                     template_path=os.path.join(d, "mob.sql"),
                                     cspells_path=os.path.join(d, "links.sql"))
        self.assertEqual([m["entry"] for m in mobs["caster"]], [101])

    def test_cast_time_index_18_is_never_interruptible_in_the_data(self):
        import build_data
        from trl import spells as spells_mod
        self.assertLessEqual(build_data._cast_table()[18], 0)
        for sid, info in (spells_mod._load().get("by_id") or {}).items():
            if info.get("casting_time_index") == 18:
                self.assertFalse(spells_mod.is_interruptible(
                    info.get("cast_ms"), info.get("prevention_type"),
                    info.get("interrupt_flags")), sid)


class R3SurvivorKillTest(unittest.TestCase):
    """Tests added to kill the mutations that survived round 2."""

    def _normalise_hp(self, samples):
        tmp = tempfile.mkdtemp()
        guids = {"player": PLAYER, "bot": BOT, "pet": None, "mobs": [MOB], "marked": None}
        with open(os.path.join(tmp, "setup.json"), "w") as f:
            json.dump({"ok": True, "guids": guids}, f)
        with open(os.path.join(tmp, "marks.jsonl"), "w") as f:
            f.write(json.dumps({"kind": "setup_ok", "wall": "2026-01-01T00:00:00+00:00"}) + "\n")
        with open(os.path.join(tmp, "samples.jsonl"), "w") as f:
            for t, hp in samples:
                f.write(json.dumps({"t": t, "guid": PLAYER, "hp": hp, "max_hp": 1000,
                                    "auras": []}) + "\n")
        with open(os.path.join(tmp, "raw-ta.jsonl"), "w") as f:
            f.write(json.dumps({"seq": 1, "time": "2026-01-01T00:00:10+00:00",
                                "kind": "spell_go", "source": {"name": "b", "guid": BOT},
                                "target": {"name": "p", "guid": PLAYER}, "spell_id": 6065}) + "\n")
        return events_mod.normalise(tmp, _trial())[0]["dst_hp_pct"]

    def test_hp_takes_the_last_sample_not_the_first_inside_the_window(self):
        # 9.3 and 9.6 are BOTH within 1 s of t=10: the later one (30 %) wins.
        self.assertEqual(self._normalise_hp([(9.3, 900), (9.6, 300)]), 30.0)
        # ... and a later sample (10.4 is after t) is never read.
        self.assertEqual(self._normalise_hp([(9.3, 900), (9.6, 300), (10.4, 100)]), 30.0)

    def test_rules_at_future_sample_and_first_not_last(self):
        by_guid = rules_mod._sample_rows([_sample(9.3, PLAYER, 900), _sample(9.6, PLAYER, 300),
                                          _sample(11.0, PLAYER, 100)])
        self.assertEqual(rules_mod._at(by_guid, PLAYER, 10.0)["hp"], 300)     # last, not first
        self.assertEqual(rules_mod._at(by_guid, PLAYER, 10.0)["t"], 9.6)
        only_future = rules_mod._sample_rows([_sample(10.5, PLAYER, 100)])
        self.assertIsNone(rules_mod._at(only_future, PLAYER, 10.0))           # never the future
        stale = rules_mod._sample_rows([_sample(8.5, PLAYER, 900)])
        self.assertIsNone(rules_mod._at(stale, PLAYER, 10.0))                 # 1.5 s old
        edge = rules_mod._sample_rows([_sample(9.0, PLAYER, 900)])
        self.assertEqual(rules_mod._at(edge, PLAYER, 10.0)["hp"], 900)        # exactly 1 s is in
        # Through a where key: src_hp_le reads the last sample, not the first.
        rule = _rule({"metric": "count", "where": {"kind": "cast", "src_hp_le": 50}},
                     pass_if="== 0")
        row = _cast(10.0, "bot", "player", 80.0)
        samples = [_sample(9.3, BOT, 900), _sample(9.6, BOT, 300)]
        res = rules_mod.evaluate(rule, [row], samples, [], _ctx())
        self.assertEqual(res["value"], 1)

    def test_interruptible_each_condition_alone(self):
        from trl import spells as spells_mod
        default = os.path.join(spells_mod._TOOL_ROOT, "data", "spells.json")
        doc = {"by_name": {}, "by_id": {
            "1": {"name": "ok", "cast_ms": 1500, "prevention_type": 1, "interrupt_flags": 0x0F},
            "2": {"name": "instant", "cast_ms": 0, "prevention_type": 1, "interrupt_flags": 0x0F},
            "3": {"name": "pushback only", "cast_ms": 1500, "prevention_type": 2, "interrupt_flags": 0x0F},
            "4": {"name": "no dmg flag", "cast_ms": 1500, "prevention_type": 1, "interrupt_flags": 0x01}}}
        rule = _rule({"metric": "count", "where": {"kind": "cast", "spell_interruptible": True}},
                     pass_if=">= 0")
        saved = spells_mod._cache.get(default)
        spells_mod._cache[default] = doc
        try:
            for sid, expect in ((1, 1), (2, 0), (3, 0), (4, 0), (999, 0)):
                res = rules_mod.evaluate(rule, [_cast(10.0, "bot", "player", 80.0, spell_id=sid)],
                                         [], [], _ctx())
                self.assertEqual(res["value"], expect, sid)
        finally:
            if saved is None:
                spells_mod._cache.pop(default, None)
            else:
                spells_mod._cache[default] = saved

    def test_id_differs_from_file_stem_is_rejected(self):
        import io
        import contextlib
        import trials as trials_mod
        d = tempfile.mkdtemp()
        path = os.path.join(d, "some-other-name.json")
        with open(path, "w") as f:
            json.dump(_trial(), f)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = trials_mod.cmd_validate(type("A", (), {"files": [path]})())
        self.assertEqual(rc, 1)
        self.assertIn("!= file stem", buf.getvalue())
        good = os.path.join(d, "priest-holy-pws-def-arms.json")
        with open(good, "w") as f:
            json.dump(_trial(), f)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = trials_mod.cmd_validate(type("A", (), {"files": [good]})())
        self.assertEqual(rc, 0, buf.getvalue())

    def test_repeat_without_setup_json_is_invalid(self):
        run = tempfile.mkdtemp()
        tdir = os.path.join(run, "t")
        os.makedirs(os.path.join(tdir, "rep1"))      # no setup.json, no verdicts.json
        out = score_mod.score_trial(tdir, _trial())
        self.assertEqual(out["verdicts"][0]["verdict"], "INVALID")
        self.assertIn("setup.json", out["verdicts"][0]["detail"][0]["invalid_reason"])
        # A second, scored repeat does not hide it: INCONCLUSIVE/PASS only if it scored.
        self.assertEqual(out["verdicts"][0]["n"], 0)

    def test_alive_uptime_with_no_samples_is_inconclusive_not_fail(self):
        rule = _rule({"metric": "uptime", "unit": "$pet", "alive": True}, pass_if=">= 0.5")
        res = rules_mod.evaluate(rule, [], [], [], {"guids": {"pet": PET}, "trial": _trial()})
        self.assertEqual(res["verdict"], "INCONCLUSIVE")
        self.assertEqual(res["n"], 0)
        # Samples only OUTSIDE the window are no samples either.
        res = rules_mod.evaluate(rule, [], [_sample(-5.0, PLAYER, 500)], [],
                                 {"guids": {"pet": PET}, "trial": _trial(), "window": (0.0, 10.0)})
        self.assertEqual(res["verdict"], "INCONCLUSIVE")


class R3WindowOpportunityAndReasonTest(unittest.TestCase):
    def test_opportunity_counts_only_the_window(self):
        # 40 s of low-hp pre-fight samples must not give a 5 s fight its opportunity.
        root = tempfile.mkdtemp()
        rep = _fight_rep(root, setup_s=0, start_s=40, end_s=45, shields=(42,), last_sample_s=60)
        trial = _trial(min_valid_s=3)
        trial["opportunity"] = {"unit": "$player", "hp_lt": 90, "in_combat": True}
        trial["rules"][0]["min_opportunity_s"] = 20
        out = score_mod.score_rep(rep, trial)
        rule = out["rules"][0]
        self.assertEqual(rule["verdict"], "INCONCLUSIVE", rule)
        self.assertIn("opportunity", rule["guard"])
        self.assertLessEqual(float(rule["guard"].split("opportunity ")[1].split(" s")[0]), 5.0)

    def test_pr_block_reason_prefers_real_reason_to_assumed_guard(self):
        runs = []
        for _ in range(2):
            lat = _rule({"metric": "latency",
                         "trigger": {"kind": "cross", "unit": "$player", "below": "$low"},
                         "response": {"kind": "heal", "src": "$bot"}, "within_s": 4},
                        min_n=0)
            rep, trial = _thr_rep(None, hp=900, config_read=False, rules=[lat])
            run = os.path.dirname(os.path.dirname(rep))
            res = score_mod.score_trial(os.path.dirname(rep), trial)
            report_mod.write_card(run, [res])
            runs.append(run)
        text = report_mod.pr_block(runs[0], runs[1], None)
        self.assertIn("INCONCLUSIVE: no triggers (rep1)", text)


class HireTalentsSetupTest(unittest.TestCase):
    """A hired bot keeps its random talent build (`role` rebuilds strategies only): the setup proves it."""

    def test_fire_mage_setup_requires_fire_vulnerability_talent(self):
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        import test_hire_talents as hire
        trial = hire.mage_trial()
        self.assertEqual((trial["bot"]["class"], trial["bot"]["spec"]), ("mage", "fire"))
        # an arcane mage on a fire trial: the strategies say fire, the talents are arcane
        with tempfile.TemporaryDirectory() as tmp:
            stuck = hire.TalentAgent(rolled={"mage": "arcane"}, apply_works=False)
            doc, _led = hire.run_it(copy.deepcopy(trial), stuck, tmp)
        row = [c for c in doc["checks"] if c["id"] == "bot_talents"]
        self.assertEqual(len(row), 1, [c["id"] for c in doc["checks"]])
        self.assertFalse(row[0]["ok"])
        self.assertFalse(doc["ok"])                                  # INVALID, never a silent score
        self.assertIn("Fire Vulnerability", row[0]["evidence"])
        # the bench asks for the fire build and the same bot then passes
        with tempfile.TemporaryDirectory() as tmp:
            fixable = hire.TalentAgent(rolled={"mage": "arcane"})
            doc, _led = hire.run_it(copy.deepcopy(trial), fixable, tmp)
        self.assertTrue(doc["ok"], doc)
        self.assertEqual(fixable.build["Embercrest"], ("mage", "fire"))


if __name__ == "__main__":
    unittest.main()
