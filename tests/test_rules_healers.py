"""Tests for the healer trial files (trials/healers/*.json, briefs 3a).

Every rule is loaded from its trial file and scored through score.score_rep on
a small synthetic repeat, once for PASS and once for FAIL. The #1553 set also
proves its setup contract: Arms talents with no Protection points (checked
against the shipped Talent.dbc when TRIALS_DBC_DIR points at it), the stance aura,
the Holy priest healer, and that a talentless player is INVALID, never scored.

Run from the tool directory: python3 -I -m unittest discover -s tests
"""

import glob
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from trl import scenario as scenario_mod  # noqa: E402
from trl import score as score_mod  # noqa: E402
from trl import spells as spells_mod  # noqa: E402

TRIAL_DIR = os.path.join(ROOT, "trials", "healers")
BOT_SRC = os.path.expanduser(os.environ.get("TRIALS_BOT_SRC", "/nonexistent"))
DBC_DIR = os.path.expanduser(os.environ.get("TRIALS_DBC_DIR", "/nonexistent"))

PLAYER = "0x0000000000000001"
BOT = "0x0000000000000002"
MOB = "0xf13000000000abcd"

REPEATS_4 = {"priest-holy-pws-def-arms", "priest-holy-pws-def-arms-2tank", "priest-holy-pws-def-arms-ooc",
             "priest-holy-pws-bear-bal", "priest-holy-pws-bear-bal-2tank",
             "priest-holy-heal-latency", "paladin-holy-heal-latency"}
IDS = [
    "priest-holy-pws-def-arms", "priest-holy-pws-bear-bal", "priest-holy-pws-battle-control",
    "priest-holy-pws-forced-role", "priest-holy-heal-latency", "priest-holy-dispel-magic",
    "priest-holy-fort-ooc", "priest-shadow-form-prepull", "paladin-holy-heal-latency",
    "paladin-blessing-ooc", "druid-resto-heal-latency",
]
# rule id -> expect_today (brief table; solo-tank PWS trials PASS: AiFactory.cpp:597-611 reads the stance unless HasOtherTank)
EXPECT = {
    "PRI-PWS-DEF-ARMS": "PASS", "PRI-PWS-BEAR-BAL": "PASS", "PRI-PWS-BATTLE-CONTROL": "PASS",
    "PRI-PWS-FORCED-ROLE": "PASS", "PRI-HEAL-LATENCY": "PASS", "PRI-DISPEL-MAGIC": "PASS",
    "PRI-FORT-OOC": "PASS", "PRI-SHADOWFORM-PREPULL": "FAIL", "PAL-HOLY-HEAL-LATENCY": "PASS",
    "PAL-BLESSING-OOC": "PASS", "DRU-RESTO-HEAL-LATENCY": "PASS",
}


def load(tid):
    return scenario_mod.load_trial(os.path.join(TRIAL_DIR, tid + ".json"))


def rule_of(trial, rid):
    return [r for r in trial["rules"] if r["id"] == rid][0]


def sid(name, rank=0):
    return sorted(spells_mod.ids_for(name))[rank]


# ---- synthetic repeat -------------------------------------------------------

def sample(t, guid, hp=1000, max_hp=1000, auras=(), combat=True):
    return {"t": float(t), "wall": None, "unit": "x", "guid": guid, "hp": hp, "max_hp": max_hp,
            "power": 500, "max_power": 500, "auras": list(auras), "casting": None,
            "in_combat": combat, "target": "", "x": 0.0, "y": 0.0, "z": 0.0}


def event(t, kind, src, dst, spell_id, hp=None):
    guid = {"player": PLAYER, "bot": BOT}
    return {"t": float(t), "wall": None, "kind": kind, "src": src, "src_guid": guid[src],
            "dst": dst, "dst_guid": guid[dst], "src_unit": "$" + src, "dst_unit": "$" + dst,
            "spell_id": spell_id, "spell": spells_mod.name_for(spell_id), "amount": None,
            "dst_hp_pct": hp, "src_role": src, "dst_role": dst, "origin": "ta"}


def series(secs, player_hp=lambda t: 600, player_auras=(), bot_auras=(), combat=True):
    out = []
    for t in range(0, secs + 1):
        out.append(sample(t, PLAYER, hp=player_hp(t), auras=player_auras, combat=combat))
        out.append(sample(t, BOT, hp=1000, auras=bot_auras, combat=combat))
    return out


class RepMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.n = 0

    def score(self, tid, events, samples, secs=200, setup=None):
        """Score one synthetic repeat of trial `tid`; return {rule id: entry}."""
        self.n += 1
        rep = os.path.join(self.tmp, "run", tid, "rep%d" % self.n)
        os.makedirs(rep)
        doc = {"ok": True, "guids": {"player": PLAYER, "bot": BOT, "mobs": [MOB]},
               "config": {"critical": 20, "low": 50, "medium": 70}}
        doc.update(setup or {})
        with open(os.path.join(rep, "setup.json"), "w") as f:
            json.dump(doc, f)
        for name, rows in (("samples.jsonl", samples), ("events.jsonl", events),
                           ("marks.jsonl", [{"kind": "fight_start", "t": 0},
                                            {"kind": "fight_end", "t": secs, "ended": "duration"}])):
            with open(os.path.join(rep, name), "w") as f:
                for row in rows:
                    f.write(json.dumps(row) + "\n")
        out = score_mod.score_rep(rep, load(tid))
        return {r["id"]: r for r in out["rules"]}, out

    def verdict(self, tid, rid, events, samples, **kw):
        rules, _ = self.score(tid, events, samples, **kw)
        return rules[rid]


# ---- trial files ------------------------------------------------------------

class TrialFilesTest(unittest.TestCase):
    def test_all_healer_trials_validate(self):
        files = sorted(glob.glob(os.path.join(TRIAL_DIR, "*.json")))
        self.assertEqual([os.path.basename(p)[:-5] for p in files], sorted(IDS, key=lambda i: i + ".json"))
        proc = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "trials.py"), "validate"] + files,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for path in files:
            trial = scenario_mod.load_trial(path)
            self.assertEqual(scenario_mod.validate(trial), [], path)
            self.assertEqual(trial["id"], os.path.basename(path)[:-5])

    def test_rule_ids_unique(self):
        seen = {}
        for tid in IDS:
            for rule in load(tid)["rules"]:
                self.assertNotIn(rule["id"], seen, "%s also in %s" % (rule["id"], seen.get(rule["id"])))
                seen[rule["id"]] = tid

    def test_expect_today_values(self):
        for tid in IDS:
            for rule in load(tid)["rules"]:
                if rule["id"] in EXPECT:
                    self.assertEqual(rule["expect_today"], EXPECT[rule["id"]], rule["id"])
                else:
                    self.assertEqual(rule["expect_today"], "PASS", rule["id"])
                self.assertTrue(rule["why_expect"], rule["id"])

    def test_sources_point_at_real_lines(self):
        if not os.path.isdir(BOT_SRC):
            self.skipTest("bot source tree not present")
        pat = re.compile(r"((?:ai|commands)/[\w/.\-]+\.(?:h|cpp)):([0-9,\-]+)")
        checked = 0
        for tid in IDS:
            for rule in load(tid)["rules"]:
                if rule["source"].startswith("precondition"):
                    continue
                found = pat.findall(rule["source"])
                self.assertTrue(found, rule["id"])
                for path, lines in found:
                    full = os.path.join(BOT_SRC, path)
                    self.assertTrue(os.path.exists(full), "%s: %s" % (rule["id"], path))
                    with open(full, errors="replace") as f:
                        count = sum(1 for _ in f)
                    for part in lines.split(","):
                        last = int(part.split("-")[-1])
                        self.assertLessEqual(last, count, "%s: %s:%s" % (rule["id"], path, part))
                        checked += 1
        self.assertGreater(checked, 20)

    def test_healer_trials_carry_the_common_decisions(self):
        for tid in IDS:
            trial = load(tid)
            self.assertEqual(trial["player"]["level"], 40)
            self.assertEqual(trial["bot"]["level"], 40)
            kind = trial["situation"]["kind"]
            hurt = kind == "ooc_hurt"           # window = ooc_wait_s 150, one stance, no mobs
            ooc = kind in ("ooc_buffs", "ooc_hurt")
            self.assertEqual((trial["duration_s"], trial["min_valid_s"]),
                             (60, 45) if kind == "ooc_buffs" else (150, 120) if hurt else (180, 120))
            # the four #1553 trials run 4 repeats so one flaky rep leaves more than one data point
            self.assertEqual(trial["repeats"], 4 if tid in REPEATS_4 else 2, tid)
            self.assertIn("player_talents", trial["setup_checks"])
            self.assertEqual("player_stance" in trial["setup_checks"], hurt or not ooc)
            self.assertEqual("mobs_ready" in trial["setup_checks"], not ooc)
            if trial["bot"]["role"] == "healer":
                self.assertEqual(trial["bot"]["items"], [{"name": "Sweet Nectar", "count": 20}])
                self.assertIn("bot_items", trial["setup_checks"])
            if not ooc:
                # lighter fights (DECISIONS.md): paladin-heal-latency at -4, the bear-bal pair at -4 and
                # shadow-form-prepull at -1 after the 11-11 bench round; everything else at +1
                self.assertEqual(trial["situation"]["mobs"]["level_delta"],
                                 {"paladin-holy-heal-latency": -4, "priest-holy-pws-bear-bal": -4,
                                  "priest-holy-pws-bear-bal-2tank": -4,
                                  "priest-shadow-form-prepull": -1}.get(tid, 1), tid)


# ---- #1553 setup contract ---------------------------------------------------

def read_dbc(name):
    data = open(os.path.join(DBC_DIR, name), "rb").read()
    _, rec, fields, size, _ = struct.unpack("<4sIIII", data[:20])
    return [struct.unpack("<%di" % fields, data[20 + i * size:20 + (i + 1) * size]) for i in range(rec)]


def points_per_tab(spell_ids, class_id):
    """TB's GetPlayerSpecTabs: ranks held per talent tab page (HasSpell of a rank spell)."""
    tabs = {t[0]: (1 if t[0] == 41 else t[13]) for t in read_dbc("TalentTab.dbc")
            if t[12] & (1 << (class_id - 1))}
    pts = {0: 0, 1: 0, 2: 0}
    for tal in read_dbc("Talent.dbc"):
        if tal[1] not in tabs:
            continue
        rank = max([r + 1 for r in range(5) if tal[4 + r] and tal[4 + r] in spell_ids] or [0])
        pts[tabs[tal[1]]] += rank
    return pts


def spec_tab(pts):
    tab, best = -1, 0
    for i in range(3):          # AiFactory::GetPlayerSpecTab: first tab wins ties
        if tab == -1 or best < pts[i]:
            tab, best = i, pts[i]
    return tab


class SetupContractTest(unittest.TestCase):
    ARMS_TRIALS = ["priest-holy-pws-def-arms", "priest-holy-pws-battle-control", "priest-holy-pws-forced-role"]

    def test_arms_talent_ids_come_from_the_spell_table(self):
        for tid in self.ARMS_TRIALS + IDS:
            trial = load(tid)
            if trial["player"]["class"] != "warrior":
                continue
            ids = trial["player"]["talents"]["spells"]
            self.assertEqual(trial["player"]["talents"]["tree"], "arms")
            self.assertEqual(sorted(spells_mod.name_for(i) for i in ids), ["Deflection", "Improved Rend"])
            for i in ids:
                info = json.load(open(os.path.join(ROOT, "data", "spells.json")))["by_id"][str(i)]
                self.assertEqual(info["rank"], "Rank 1", i)

    def test_arms_trials_have_arms_points_and_no_protection_points(self):
        if not os.path.exists(os.path.join(DBC_DIR, "Talent.dbc")):
            self.skipTest("Talent.dbc not present")
        for tid in IDS:
            trial = load(tid)
            if trial["player"]["class"] != "warrior":
                continue
            pts = points_per_tab(set(trial["player"]["talents"]["spells"]), 1)
            self.assertEqual(pts[2], 0, "%s has Protection points %s" % (tid, pts))
            self.assertGreater(pts[0], 0, tid)
            self.assertEqual(spec_tab(pts), 0, "%s would read tab %s" % (tid, pts))

    def test_balance_druid_has_balance_points_only(self):
        if not os.path.exists(os.path.join(DBC_DIR, "Talent.dbc")):
            self.skipTest("Talent.dbc not present")
        trial = load("priest-holy-pws-bear-bal")
        self.assertEqual(sorted(spells_mod.name_for(i).replace("''", "'")
                                for i in trial["player"]["talents"]["spells"]),
                         ["Improved Wrath", "Nature's Grasp"])
        pts = points_per_tab(set(trial["player"]["talents"]["spells"]), 11)
        self.assertEqual((pts[0], pts[1], pts[2]), (2, 0, 0))

    def test_stance_aura_is_proven_and_held(self):
        want = {"priest-holy-pws-def-arms": (71, "Defensive Stance"),
                "priest-holy-pws-bear-bal": (9634, "Dire Bear Form"),
                "priest-holy-pws-battle-control": (2457, "Battle Stance"),
                "priest-holy-pws-forced-role": (71, "Defensive Stance")}
        for tid, (aura, name) in want.items():
            trial = load(tid)
            self.assertEqual(trial["player"]["stance"], aura)
            self.assertEqual(spells_mod.name_for(aura), name)
            self.assertIn("player_stance", trial["setup_checks"])
            held = [r for r in trial["rules"] if r["check"]["metric"] == "uptime"]
            self.assertEqual(len(held), 1, tid)
            self.assertEqual(held[0]["check"]["aura"], [aura])
            self.assertEqual(held[0]["check"]["unit"], "$player")

    def test_player_is_not_forced_unless_the_trial_says_so(self):
        for tid in self.ARMS_TRIALS + ["priest-holy-pws-bear-bal"]:
            self.assertEqual(load(tid)["player"]["forced_role"],
                             "tank" if tid.endswith("forced-role") else None, tid)
            self.assertIn("player_forced_role", load(tid)["setup_checks"])

    def test_bot_is_a_holy_priest_healer(self):
        for tid in IDS[:7]:
            bot = load(tid)["bot"]
            self.assertEqual((bot["class"], bot["spec"], bot["role"]), ("priest", "holy", "healer"), tid)
            for check in ("bot_present", "bot_level", "bot_role", "bot_spec"):
                self.assertIn(check, load(tid)["setup_checks"], tid)

    def test_talentless_player_is_not_a_valid_setup(self):
        # AiFactory::GetPlayerSpecTab: a level>=10 warrior with 0 talent points reads tab 2 = tank
        for talents in ({"tree": "arms", "spells": []}, {"tree": "none", "spells": []}):
            trial = load("priest-holy-pws-def-arms")
            trial["player"]["talents"] = talents
            errs = scenario_mod.validate(trial)
            self.assertTrue(any("talents.spells" in e for e in errs), errs)


class SetupFailureIsInvalidTest(RepMixin, unittest.TestCase):
    def test_unproven_talents_or_stance_are_invalid_not_scored(self):
        for check, why in (("player_talents", "talents not learned: 16462"),
                           ("player_stance", "stance not held: aura 71 missing")):
            rules, out = self.score(
                "priest-holy-pws-def-arms", [], series(200),
                setup={"ok": False, "invalid_reason": why,
                       "checks": [{"id": check, "ok": False, "value": None, "evidence": why}]})
            self.assertEqual({r["verdict"] for r in rules.values()}, {"INVALID"})
            self.assertEqual(out["invalid_reason"], why)


# ---- #1553 rules ------------------------------------------------------------

PWS = 6065
STANCES = [("priest-holy-pws-def-arms", "PRI-PWS-DEF-ARMS-STANCE", 71),
           ("priest-holy-pws-bear-bal", "PRI-PWS-BEAR-BAL-FORM", 9634),
           ("priest-holy-pws-battle-control", "PRI-PWS-BATTLE-CONTROL-STANCE", 2457),
           ("priest-holy-pws-forced-role", "PRI-PWS-FORCED-ROLE-STANCE", 71)]


def tank_hp(t):
    return 600 if t < 150 else 1000


class ShieldRulesTest(RepMixin, unittest.TestCase):
    def test_def_arms_shield_fails_on_1553_shape(self):
        # the named failing case: 2 shields bot->player at 60 % in combat, 3 bot->bot, 120 s of opportunity
        events = [event(10, "cast", "bot", "bot", PWS, 100), event(40, "cast", "bot", "bot", PWS, 100),
                  event(70, "cast", "bot", "bot", PWS, 100),
                  event(60, "cast", "bot", "player", PWS, 60), event(90, "cast", "bot", "player", PWS, 60)]
        samples = series(200, player_hp=lambda t: 600 if t < 120 else 1000, player_auras=[71])
        entry = self.verdict("priest-holy-pws-def-arms", "PRI-PWS-DEF-ARMS", events, samples)
        self.assertEqual(entry["verdict"], "FAIL")
        self.assertEqual(entry["value"], 2)

    def test_def_arms_pass_when_only_the_critical_shield_lands(self):
        events = [event(10, "cast", "bot", "bot", PWS, 100), event(80, "cast", "bot", "player", PWS, 15)]
        samples = series(200, player_hp=tank_hp, player_auras=[71])
        entry = self.verdict("priest-holy-pws-def-arms", "PRI-PWS-DEF-ARMS", events, samples)
        self.assertEqual((entry["verdict"], entry["value"]), ("PASS", 0))

    def test_def_arms_without_opportunity_is_inconclusive_never_pass(self):
        samples = series(200, player_hp=lambda t: 1000, player_auras=[71])
        entry = self.verdict("priest-holy-pws-def-arms", "PRI-PWS-DEF-ARMS", [], samples)
        self.assertEqual(entry["verdict"], "INCONCLUSIVE")

    def test_bear_bal_fail_and_pass(self):
        samples = series(200, player_hp=tank_hp, player_auras=[9634])
        bad = self.verdict("priest-holy-pws-bear-bal", "PRI-PWS-BEAR-BAL",
                           [event(50, "cast", "bot", "player", PWS, 70)], samples)
        good = self.verdict("priest-holy-pws-bear-bal", "PRI-PWS-BEAR-BAL",
                            [event(10, "cast", "bot", "bot", PWS, 100)], samples)
        self.assertEqual((bad["verdict"], bad["value"]), ("FAIL", 1))
        self.assertEqual((good["verdict"], good["value"]), ("PASS", 0))

    def test_forced_role_trial_passes_with_no_shields(self):
        samples = series(200, player_hp=tank_hp, player_auras=[71])
        same = [event(10, "cast", "bot", "bot", PWS, 100), event(40, "cast", "bot", "bot", PWS, 100)]
        good = self.verdict("priest-holy-pws-forced-role", "PRI-PWS-FORCED-ROLE", same, samples)
        bad = self.verdict("priest-holy-pws-forced-role", "PRI-PWS-FORCED-ROLE",
                           same + [event(60, "cast", "bot", "player", PWS, 60)], samples)
        self.assertEqual((good["verdict"], good["value"]), ("PASS", 0))
        self.assertEqual((bad["verdict"], bad["value"]), ("FAIL", 1))

    def test_forced_role_allows_the_critical_shield(self):
        samples = series(200, player_hp=tank_hp, player_auras=[71])
        entry = self.verdict("priest-holy-pws-forced-role", "PRI-PWS-FORCED-ROLE",
                             [event(80, "cast", "bot", "player", PWS, 15)], samples)
        self.assertEqual((entry["verdict"], entry["value"]), ("PASS", 0))

    def test_battle_control_needs_low_band(self):
        samples = series(200, player_hp=lambda t: 450 if t < 120 else 800, player_auras=[2457])
        high = self.verdict("priest-holy-pws-battle-control", "PRI-PWS-BATTLE-CONTROL",
                            [event(150, "cast", "bot", "player", PWS, 80)], samples)
        low = self.verdict("priest-holy-pws-battle-control", "PRI-PWS-BATTLE-CONTROL",
                           [event(30, "cast", "bot", "player", PWS, 45)], samples)
        self.assertEqual((high["verdict"], high["value"]), ("FAIL", 0))
        self.assertEqual((low["verdict"], low["value"]), ("PASS", 1))

    def test_battle_control_without_low_band_time_is_inconclusive(self):
        samples = series(200, player_hp=lambda t: 800, player_auras=[2457])
        entry = self.verdict("priest-holy-pws-battle-control", "PRI-PWS-BATTLE-CONTROL", [], samples)
        self.assertEqual(entry["verdict"], "INCONCLUSIVE")

    def test_stance_held_rules(self):
        for tid, rid, aura in STANCES:
            hp = (lambda t: 450) if "battle" in tid else tank_hp
            held = self.verdict(tid, rid, [], series(200, player_hp=hp, player_auras=[aura]))
            self.assertEqual(held["verdict"], "PASS", tid)
            lost = series(200, player_hp=hp, player_auras=[])
            self.assertEqual(self.verdict(tid, rid, [], lost)["verdict"], "FAIL", tid)


# ---- latency rules ----------------------------------------------------------

HEALS = {"priest-holy-heal-latency": ("PRI-HEAL-LATENCY", 2061),
         "paladin-holy-heal-latency": ("PAL-HOLY-HEAL-LATENCY", 635),
         "druid-resto-heal-latency": ("DRU-RESTO-HEAL-LATENCY", 8936)}
DROPS = [20 + 25 * k for k in range(6)]


def dropping_hp(t):
    return 400 if any(d <= t < d + 8 for d in DROPS) else 800


class HealLatencyTest(RepMixin, unittest.TestCase):
    def run_heal(self, tid, delay):
        rid, heal = HEALS[tid]
        events = [event(d + delay, "cast", "bot", "player", heal, 40) for d in DROPS]
        return self.verdict(tid, rid, events, series(200, player_hp=dropping_hp, player_auras=[71]))

    def test_heal_latency_pass_and_fail(self):
        for tid in HEALS:
            fast = self.run_heal(tid, 2)
            slow = self.run_heal(tid, 7)
            self.assertEqual((fast["verdict"], fast["k"], fast["n"]), ("PASS", 6, 6), tid)
            self.assertEqual((slow["verdict"], slow["k"], slow["n"]), ("FAIL", 0, 6), tid)

    def test_heal_on_someone_else_does_not_answer(self):
        rid, heal = HEALS["priest-holy-heal-latency"]
        events = [event(d + 2, "cast", "bot", "bot", heal, 90) for d in DROPS]
        entry = self.verdict("priest-holy-heal-latency", rid, events,
                             series(200, player_hp=dropping_hp, player_auras=[71]))
        self.assertEqual(entry["verdict"], "FAIL")

    def test_too_few_crossings_is_inconclusive(self):
        rid, heal = HEALS["druid-resto-heal-latency"]
        samples = series(200, player_hp=lambda t: 400 if 20 <= t < 28 else 800, player_auras=[71])
        entry = self.verdict("druid-resto-heal-latency", rid,
                             [event(22, "cast", "bot", "player", heal, 40)], samples)
        self.assertEqual(entry["verdict"], "INCONCLUSIVE")

    def test_druid_swiftness_and_priest_shield_count_as_answers(self):
        swift = sid("Nature''s Swiftness")
        events = [event(d + 1, "cast", "bot", "player", swift, 40) for d in DROPS]
        entry = self.verdict("druid-resto-heal-latency", "DRU-RESTO-HEAL-LATENCY", events,
                             series(200, player_hp=dropping_hp, player_auras=[71]))
        self.assertEqual(entry["verdict"], "PASS")
        events = [event(d + 1, "cast", "bot", "player", PWS, 40) for d in DROPS]
        entry = self.verdict("priest-holy-heal-latency", "PRI-HEAL-LATENCY", events,
                             series(200, player_hp=dropping_hp, player_auras=[71]))
        self.assertEqual(entry["verdict"], "PASS")


# ---- dispel, OOC and shadowform ---------------------------------------------

FF = 770
DISPEL = sid("Dispel Magic")
APPLIED = [10, 50, 90, 130]


class DispelTest(RepMixin, unittest.TestCase):
    def test_dispel_magic_pass_and_fail(self):
        trig = [event(t, "aura_on", "player", "player", FF, 80) for t in APPLIED]
        samples = series(200, player_hp=lambda t: 800, player_auras=[71, FF])
        good = trig + [event(t + 3, "cast", "bot", "player", DISPEL, 80) for t in APPLIED]
        late = trig + [event(t + 9, "cast", "bot", "player", DISPEL, 80) for t in APPLIED]
        self.assertEqual(self.verdict("priest-holy-dispel-magic", "PRI-DISPEL-MAGIC", good, samples)["verdict"], "PASS")
        bad = self.verdict("priest-holy-dispel-magic", "PRI-DISPEL-MAGIC", late, samples)
        self.assertEqual((bad["verdict"], bad["k"], bad["n"]), ("FAIL", 0, 4))

    def test_dispel_trial_proves_the_debuff_lands(self):
        trial = load("priest-holy-dispel-magic")
        self.assertIn("player_auras", trial["setup_checks"])
        self.assertEqual(trial["player"]["auras"], [FF])
        self.assertEqual(trial["situation"]["debuff"], {"spell_id": FF})
        self.assertEqual(json.load(open(os.path.join(ROOT, "data", "spells.json")))["by_id"][str(FF)]["dispel"], 1)

    def test_fewer_than_four_applications_is_inconclusive(self):
        trig = [event(t, "aura_on", "player", "player", FF, 80) for t in APPLIED[:2]]
        entry = self.verdict("priest-holy-dispel-magic", "PRI-DISPEL-MAGIC", trig,
                             series(200, player_hp=lambda t: 800, player_auras=[71, FF]))
        self.assertEqual(entry["verdict"], "INCONCLUSIVE")


class OocTest(RepMixin, unittest.TestCase):
    def ooc(self, tid, rid, events):
        return self.verdict(tid, rid, events, series(60, player_hp=lambda t: 1000, combat=False), secs=60)

    def test_fortitude_ooc_pass_and_fail(self):
        fort = sid("Power Word: Fortitude")
        self.assertEqual(self.ooc("priest-holy-fort-ooc", "PRI-FORT-OOC",
                                  [event(12, "aura_on", "bot", "player", fort, 100)])["verdict"], "PASS")
        prayer = sid("Prayer of Fortitude")
        self.assertEqual(self.ooc("priest-holy-fort-ooc", "PRI-FORT-OOC",
                                  [event(29, "aura_on", "bot", "player", prayer, 100)])["verdict"], "PASS")
        late = self.ooc("priest-holy-fort-ooc", "PRI-FORT-OOC", [event(45, "aura_on", "bot", "player", fort, 100)])
        none = self.ooc("priest-holy-fort-ooc", "PRI-FORT-OOC", [])
        self.assertEqual(late["verdict"], "FAIL")
        self.assertEqual(none["verdict"], "FAIL")
        self.assertIsNone(none["value"])

    def test_fortitude_on_the_priest_itself_does_not_count(self):
        fort = sid("Power Word: Fortitude")
        entry = self.ooc("priest-holy-fort-ooc", "PRI-FORT-OOC", [event(5, "aura_on", "bot", "bot", fort, 100)])
        self.assertEqual(entry["verdict"], "FAIL")

    def test_blessing_ooc_pass_and_fail(self):
        might = sid("Blessing of Might")
        greater = sid("Greater Blessing of Kings")
        self.assertEqual(self.ooc("paladin-blessing-ooc", "PAL-BLESSING-OOC",
                                  [event(15, "aura_on", "bot", "player", might, 100)])["verdict"], "PASS")
        self.assertEqual(self.ooc("paladin-blessing-ooc", "PAL-BLESSING-OOC",
                                  [event(28, "aura_on", "bot", "player", greater, 100)])["verdict"], "PASS")
        self.assertEqual(self.ooc("paladin-blessing-ooc", "PAL-BLESSING-OOC",
                                  [event(40, "aura_on", "bot", "player", might, 100)])["verdict"], "FAIL")
        self.assertEqual(self.ooc("paladin-blessing-ooc", "PAL-BLESSING-OOC", [])["verdict"], "FAIL")
        # a blessing on the paladin itself is not a blessing on the player
        self.assertEqual(self.ooc("paladin-blessing-ooc", "PAL-BLESSING-OOC",
                                  [event(10, "aura_on", "bot", "bot", might, 100)])["verdict"], "FAIL")


SHADOWFORM = 15473


class ShadowformTest(RepMixin, unittest.TestCase):
    def test_shadowform_prepull_pass_and_fail(self):
        samples = series(200, player_hp=lambda t: 800, player_auras=[71], bot_auras=[SHADOWFORM])
        # cast out of combat before the pull (t < 0) is outside the window: the form was up at the pull
        before = [event(-5, "cast", "bot", "bot", SHADOWFORM, 100)]
        good = self.verdict("priest-shadow-form-prepull", "PRI-SHADOWFORM-PREPULL", before, samples)
        late = self.verdict("priest-shadow-form-prepull", "PRI-SHADOWFORM-PREPULL",
                            [event(1.2, "cast", "bot", "bot", SHADOWFORM, 100)], samples)
        self.assertEqual((good["verdict"], good["value"]), ("PASS", 0))
        self.assertEqual((late["verdict"], late["value"]), ("FAIL", 1))

    def test_a_recast_late_in_the_fight_is_not_the_pull(self):
        samples = series(200, player_hp=lambda t: 800, player_auras=[71], bot_auras=[SHADOWFORM])
        entry = self.verdict("priest-shadow-form-prepull", "PRI-SHADOWFORM-PREPULL",
                             [event(90, "cast", "bot", "bot", SHADOWFORM, 100)], samples)
        self.assertEqual(entry["verdict"], "PASS")

    def test_companion_uptime_keeps_the_prepull_pass_honest(self):
        up = series(200, player_hp=lambda t: 800, player_auras=[71], bot_auras=[SHADOWFORM])
        down = series(200, player_hp=lambda t: 800, player_auras=[71], bot_auras=[])
        self.assertEqual(self.verdict("priest-shadow-form-prepull", "PRI-SHADOWFORM-UP", [], up)["verdict"], "PASS")
        self.assertEqual(self.verdict("priest-shadow-form-prepull", "PRI-SHADOWFORM-UP", [], down)["verdict"], "FAIL")


# ---- #1553 second-tank trials (extra_bots) -----------------------------------

# the appended block widens the shared tables, so test_expect_today_values,
# test_sources_point_at_real_lines and the validate/unique-id tests cover the new files
TWO_TANK = ["priest-holy-pws-def-arms-2tank", "priest-holy-pws-bear-bal-2tank"]
IDS.extend(TWO_TANK)
EXPECT.update({"PRI-PWS-DEF-ARMS-2TANK": "FAIL", "PRI-PWS-BEAR-BAL-2TANK": "FAIL"})
# live3: the control with a hired BOT warrior (extra_bots) forced to the dps role
IDS.append("priest-holy-pws-bot-warrior-control")
EXPECT.update({"PRI-PWS-BOT-WARRIOR-CONTROL": "PASS"})
# #1553 round 3: the out-of-combat hurt situation (kind ooc_hurt); only the before-case rule is FAIL today
IDS.extend(["priest-holy-pws-def-arms-ooc", "priest-holy-pws-battle-ooc-control"])
EXPECT.update({"PRI-PWS-DEF-ARMS-OOC": "FAIL"})
EXTRA_TANK = "0x0000000000000003"
TWO_TANK_SETUP = {"guids": {"player": PLAYER, "bot": BOT, "extras": [EXTRA_TANK], "mobs": [MOB]}}


class TwoTankTrialFilesTest(unittest.TestCase):
    def test_two_tank_trials_are_the_solo_trials_plus_a_protection_paladin(self):
        for solo_id, tid, aura in (("priest-holy-pws-def-arms", TWO_TANK[0], 71),
                                   ("priest-holy-pws-bear-bal", TWO_TANK[1], 9634)):
            solo, two = load(solo_id), load(tid)
            self.assertEqual(two["extra_bots"], [{"class": "paladin", "spec": "protection",
                                                  "role": "tank", "level": 40, "items": []}])
            for key in ("class", "spec", "player", "bot", "situation", "duration_s", "repeats",
                        "min_valid_s", "opportunity", "group"):
                self.assertEqual(two[key], solo[key], (tid, key))
            self.assertEqual([c for c in two["setup_checks"] if not c.startswith("extra_bots_")],
                             solo["setup_checks"])
            self.assertEqual({c for c in two["setup_checks"] if c.startswith("extra_bots_")},
                             {"extra_bots_present", "extra_bots_role", "extra_bots_in_group"})
            self.assertEqual(two["player"]["stance"], aura)
            self.assertEqual(two["extra_bots"][0]["level"], two["bot"]["level"])

    def test_two_tank_rules_cite_the_second_tank_lines_and_the_issue(self):
        for tid, rid in zip(TWO_TANK, ("PRI-PWS-DEF-ARMS-2TANK", "PRI-PWS-BEAR-BAL-2TANK")):
            rule = rule_of(load(tid), rid)
            self.assertEqual(rule["expect_today"], "FAIL")
            for text in (rule["source"], rule["why_expect"]):
                self.assertIn("1553", text + rule["source"])
            self.assertIn("520-576", rule["source"])
            self.assertIn("597-611", rule["source"])
            self.assertIn("AiFactory.cpp:520-576", rule["why_expect"])
            self.assertIn("597-611", rule["why_expect"])
            self.assertIn("#1553", rule["why_expect"])
            self.assertEqual(rule["pass_if"], "== 0")
            self.assertEqual(rule["check"], rule_of(load(tid.replace("-2tank", "")),
                                                    rid.replace("-2TANK", ""))["check"])


class TwoTankRulesTest(RepMixin, unittest.TestCase):
    def two(self, tid, rid, events, samples):
        return self.verdict(tid, rid, events, samples, setup=TWO_TANK_SETUP)

    def test_def_arms_2tank_fails_when_the_warrior_is_shielded_above_critical(self):
        events = [event(10, "cast", "bot", "bot", PWS, 100),
                  event(60, "cast", "bot", "player", PWS, 60), event(90, "cast", "bot", "player", PWS, 60)]
        samples = series(200, player_hp=tank_hp, player_auras=[71])
        entry = self.two("priest-holy-pws-def-arms-2tank", "PRI-PWS-DEF-ARMS-2TANK", events, samples)
        self.assertEqual((entry["verdict"], entry["value"]), ("FAIL", 2))

    def test_def_arms_2tank_passes_without_shields_above_critical(self):
        events = [event(10, "cast", "bot", "bot", PWS, 100), event(80, "cast", "bot", "player", PWS, 15)]
        samples = series(200, player_hp=tank_hp, player_auras=[71])
        entry = self.two("priest-holy-pws-def-arms-2tank", "PRI-PWS-DEF-ARMS-2TANK", events, samples)
        self.assertEqual((entry["verdict"], entry["value"]), ("PASS", 0))

    def test_bear_bal_2tank_fails_and_passes(self):
        samples = series(200, player_hp=tank_hp, player_auras=[9634])
        bad = self.two("priest-holy-pws-bear-bal-2tank", "PRI-PWS-BEAR-BAL-2TANK",
                       [event(50, "cast", "bot", "player", PWS, 70)], samples)
        good = self.two("priest-holy-pws-bear-bal-2tank", "PRI-PWS-BEAR-BAL-2TANK",
                        [event(10, "cast", "bot", "bot", PWS, 100)], samples)
        self.assertEqual((bad["verdict"], bad["value"]), ("FAIL", 1))
        self.assertEqual((good["verdict"], good["value"]), ("PASS", 0))

    def test_2tank_without_opportunity_is_inconclusive_never_pass(self):
        samples = series(200, player_hp=lambda t: 1000, player_auras=[71])
        entry = self.two("priest-holy-pws-def-arms-2tank", "PRI-PWS-DEF-ARMS-2TANK", [], samples)
        self.assertEqual(entry["verdict"], "INCONCLUSIVE")

    def test_2tank_stance_rules_pass_held_and_fail_lost(self):
        for tid, rid, aura in (("priest-holy-pws-def-arms-2tank", "PRI-PWS-DEF-ARMS-2TANK-STANCE", 71),
                               ("priest-holy-pws-bear-bal-2tank", "PRI-PWS-BEAR-BAL-2TANK-FORM", 9634)):
            held = self.two(tid, rid, [], series(200, player_hp=tank_hp, player_auras=[aura]))
            lost = self.two(tid, rid, [], series(200, player_hp=tank_hp, player_auras=[]))
            self.assertEqual((held["verdict"], lost["verdict"]), ("PASS", "FAIL"), tid)

    def test_failed_extra_bot_setup_is_invalid_not_scored(self):
        for check, why in (("extra_bots_present", "extra1: hire refused: cannot afford"),
                           ("extra_bots_role", "extra1 Bulwark: no 'role set to' reply"),
                           ("extra_bots_in_group", "in_group=True; not in the party: Bulwark")):
            for tid in TWO_TANK:
                setup = dict(TWO_TANK_SETUP, ok=False, invalid_reason=why,
                             checks=[{"id": check, "ok": False, "value": None, "evidence": why}])
                rules, out = self.score(tid, [], series(200), setup=setup)
                self.assertEqual({r["verdict"] for r in rules.values()}, {"INVALID"}, (tid, check))
                self.assertEqual(out["invalid_reason"], why)


if __name__ == "__main__":
    unittest.main()
