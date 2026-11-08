"""Tests for the tank and DPS trial files (trials/tanks/*.json, trials/dps/*.json).

Every trial file is known by id, validates, and cites bot code. Every rule is scored through
score.score_rep on a small synthetic repeat, once for PASS and once for FAIL, so a rule that
can never fail (or never pass) shows up here and not on a test server.

Run from the tool directory: python3 -I -m unittest discover -s tests
"""

import glob
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from trl import scenario as scenario_mod  # noqa: E402
from trl import score as score_mod  # noqa: E402
from trl import spells as spells_mod  # noqa: E402

TANK_DIR = os.path.join(ROOT, "trials", "tanks")
DPS_DIR = os.path.join(ROOT, "trials", "dps")

TANK_IDS = ["tank-paladin-righteous-fury", "tank-warrior-taunt-off-healer"]
DPS_IDS = ["dps-mage-cc-respect", "dps-mage-counterspell-caster", "dps-mage-threat-cap",
           "dps-rogue-kick-caster"]

# rule id -> expect_today ("?" = no baseline yet; the why_expect says what would decide it)
EXPECT = {
    "TNK-WAR-TAUNT": "PASS", "TNK-WAR-MOBS-ON-TANK": "?", "TNK-WAR-DEF-STANCE": "PASS",
    "TNK-WAR-TAUNT-STANCE": "PASS",
    "TNK-PAL-RIGHTEOUS-FURY": "PASS", "TNK-PAL-MOBS-ON-TANK": "?", "TNK-PAL-RF-STANCE": "PASS",
    "DPS-MAG-THREAT-NO-AGGRO": "?", "DPS-MAG-THREAT-DAMAGE": "PASS", "DPS-MAG-THREAT-STANCE": "PASS",
    "DPS-ROG-KICK": "?", "DPS-ROG-KICK-STANCE": "PASS",
    "DPS-MAG-COUNTERSPELL": "?", "DPS-MAG-CS-STANCE": "PASS",
    "DPS-MAG-CC-RESPECT": "PASS", "DPS-MAG-CC-POLYMORPH": "PASS",
}

PLAYER = "0x0000000000000001"
BOT = "0x0000000000000002"
EXTRA1 = "0x0000000000000003"
MOB = "0xf13000000000abcd"
MARKED = "0xf13000000000beef"
ROLE = {PLAYER: "player", BOT: "bot", EXTRA1: "extra1", MOB: "mob", MARKED: "mob"}

LIGHTNING_BOLT = 9532        # a cast-time, silence-interruptible spell (spells.spell_is_interruptible)
POLYMORPH_R3 = 12825         # the rank a level 40-59 mage casts


def path_of(tid):
    folder = TANK_DIR if tid.startswith("tank-") else DPS_DIR
    return os.path.join(folder, tid + ".json")


def load(tid):
    return scenario_mod.load_trial(path_of(tid))


def sid(name):
    return sorted(spells_mod.ids_for(name))[0]


def sample(t, guid, auras=(), target="", combat=True, hp=600):
    return {"t": float(t), "wall": None, "unit": ROLE[guid], "guid": guid, "hp": hp,
            "max_hp": 1000, "power": 500, "max_power": 500, "auras": list(auras),
            "casting": None, "in_combat": combat, "target": target,
            "x": 0.0, "y": 0.0, "z": 0.0}


def event(t, kind, src, dst, spell_id):
    return {"t": float(t), "wall": None, "kind": kind, "src": ROLE[src], "src_guid": src,
            "dst": ROLE[dst], "dst_guid": dst, "src_unit": ROLE[src], "dst_unit": ROLE[dst],
            "spell_id": spell_id, "spell": spells_mod.name_for(spell_id) if spell_id else None,
            "amount": 100, "dst_hp_pct": 60, "src_role": "mob" if src in (MOB, MARKED) else ROLE[src],
            "dst_role": "mob" if dst in (MOB, MARKED) else ROLE[dst], "origin": "ta"}


def series(secs, player=(), bot=(), marked=None):
    """1 Hz samples: player, bot, extra1, the mob on the player, and the marked mob."""
    out = []
    for t in range(0, secs + 1):
        out.append(sample(t, PLAYER, auras=player, target=MOB))
        out.append(sample(t, BOT, auras=bot, target=MOB))
        out.append(sample(t, EXTRA1, target=""))
        out.append(sample(t, MOB, target=PLAYER))
        out.append(sample(t, MARKED, auras=(marked(t) if marked else ()), target=PLAYER))
    return out


class RepMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.n = 0

    def rules(self, tid, events, samples, secs=200):
        """Score one synthetic repeat of trial `tid`; return {rule id: entry}."""
        self.n += 1
        rep = os.path.join(self.tmp, "run", tid, "rep%d" % self.n)
        os.makedirs(rep)
        doc = {"ok": True, "guids": {"player": PLAYER, "bot": BOT, "extras": [EXTRA1],
                                     "mobs": [MOB, MARKED], "marked": MARKED},
               "config": {"critical": 20, "low": 50, "medium": 70}}
        with open(os.path.join(rep, "setup.json"), "w") as f:
            json.dump(doc, f)
        for name, rows in (("samples.jsonl", samples), ("events.jsonl", events),
                           ("marks.jsonl", [{"kind": "fight_start", "t": 0},
                                            {"kind": "fight_end", "t": secs, "ended": "duration"}])):
            with open(os.path.join(rep, name), "w") as f:
                for row in rows:
                    f.write(json.dumps(row) + "\n")
        out = score_mod.score_rep(rep, load(tid))
        return {r["id"]: r for r in out["rules"]}

    def verdict(self, tid, rid, events, samples):
        return self.rules(tid, events, samples)[rid]["verdict"]


class FilesTest(unittest.TestCase):
    def test_every_file_is_known(self):
        for folder, ids in ((TANK_DIR, TANK_IDS), (DPS_DIR, DPS_IDS)):
            files = sorted(glob.glob(os.path.join(folder, "*.json")))
            self.assertEqual([os.path.basename(p)[:-5] for p in files], sorted(ids), folder)

    def test_every_trial_validates(self):
        for tid in TANK_IDS + DPS_IDS:
            trial = load(tid)
            self.assertEqual(trial["id"], tid)
            self.assertEqual(scenario_mod.validate(trial), [], tid)

    def test_groups(self):
        for tid in TANK_IDS:
            self.assertEqual(load(tid)["group"], "tanks-melee", tid)
            self.assertEqual(load(tid)["bot"]["role"], "tank", tid)
        for tid in DPS_IDS:
            trial = load(tid)
            want = "ranged" if trial["class"] in ("mage", "hunter", "warlock") else "tanks-melee"
            self.assertEqual(trial["group"], want, tid)
            self.assertEqual(trial["bot"]["role"], "dps", tid)

    def test_rules_cite_bot_code_and_expect_the_table(self):
        seen = set()
        for tid in TANK_IDS + DPS_IDS:
            for rule in load(tid)["rules"]:
                seen.add(rule["id"])
                self.assertIn(rule["id"], EXPECT, tid)
                self.assertEqual(rule["expect_today"], EXPECT[rule["id"]], rule["id"])
                self.assertTrue(rule.get("why_expect"), rule["id"])
                if not rule["source"].startswith("precondition"):
                    self.assertIn("ai/playerbot/", rule["source"], rule["id"])
        self.assertEqual(seen, set(EXPECT))

    def test_player_role_is_forced(self):
        # the tank trials need a non-tank player, the DPS trials a tank player: the forced role
        # decides first (PlayerbotAI::IsTank), so a stance read cannot flip it
        for tid in TANK_IDS:
            self.assertEqual(load(tid)["player"]["forced_role"], "dps", tid)
        for tid in DPS_IDS:
            self.assertEqual(load(tid)["player"]["forced_role"], "tank", tid)
        for tid in TANK_IDS + DPS_IDS:
            self.assertIn("player_forced_role", load(tid)["setup_checks"], tid)

    def test_every_trial_brings_a_healer(self):
        # a GM-levelled player with no healer hits the rescue cap within a minute (the
        # paladin-holy-heal-latency runs): every tank and DPS trial hires a holy priest as extra1
        for tid in TANK_IDS + DPS_IDS:
            trial = load(tid)
            self.assertEqual([(b["class"], b["role"]) for b in trial["extra_bots"]],
                             [("priest", "healer")], tid)
            self.assertEqual(trial["extra_bots"][0]["level"], trial["bot"]["level"], tid)
            # the three mage trials fight mobs that are on the mage/the gearless tank: 30 (DECISIONS.md, 2026-11-09)
            self.assertEqual(trial["situation"]["max_rescues_per_min"],
                             30 if tid in ("dps-mage-counterspell-caster", "dps-mage-threat-cap",
                                           "dps-mage-cc-respect") else 12, tid)

    def test_caster_trials_prove_the_caster(self):
        for tid in ("dps-rogue-kick-caster", "dps-mage-counterspell-caster"):
            trial = load(tid)
            self.assertEqual(trial["situation"]["mobs"]["role"], "caster", tid)
            self.assertIn("mob_caster_casts", trial["setup_checks"], tid)

    def test_cc_respect_reads_every_polymorph_rank(self):
        rule = [r for r in load("dps-mage-cc-respect")["rules"] if r["id"] == "DPS-MAG-CC-RESPECT"][0]
        auras = set(rule["check"]["where"]["dst_has_aura"])
        for rank in (118, 12824, 12825, 12826):
            self.assertIn(rank, auras)
            self.assertEqual(spells_mod.name_for(rank), "Polymorph")
        self.assertIn("cc_mark_set", load("dps-mage-cc-respect")["setup_checks"])
        # the moon mark plus two unmarked mobs: a kill never leaves the sheep as the only target
        self.assertEqual(load("dps-mage-cc-respect")["situation"]["mobs"]["count"], 3)

    def test_spell_ids_the_rules_read(self):
        self.assertEqual(spells_mod.name_for(25780), "Righteous Fury")
        self.assertEqual(spells_mod.name_for(71), "Defensive Stance")
        self.assertEqual(spells_mod.name_for(2457), "Battle Stance")
        self.assertIn(355, spells_mod.ids_for("Taunt"))
        self.assertTrue(spells_mod.spell_is_interruptible(LIGHTNING_BOLT))


class TankRulesTest(RepMixin, unittest.TestCase):
    TID = "tank-warrior-taunt-off-healer"

    def test_taunt_latency(self):
        times = (10, 30, 70, 100, 130)
        hits = [event(t, "damage", MOB, PLAYER if t < 50 else EXTRA1, None) for t in times]
        taunts = [event(t + 2, "cast", BOT, MOB, 355) for t in times]
        samples = series(200)
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-TAUNT", hits + taunts, samples), "PASS")
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-TAUNT", hits, samples), "FAIL")
        # Mocking Blow and Challenging Shout answer too (the dev rows: mocking blow taunt, AoE taunt)
        other = [event(t + 2, "cast", BOT, MOB, sid("Mocking Blow") if i % 2 else sid("Challenging Shout"))
                 for i, t in enumerate(times)]
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-TAUNT", hits + other, samples), "PASS")
        # a taunt after the 5 s window does not answer
        late = [event(t + 6, "cast", BOT, MOB, 355) for t in times]
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-TAUNT", hits + late, samples), "FAIL")
        # a taunt on another unit (the marked mob, not the one that hit) does not answer
        elsewhere = [event(t + 2, "cast", BOT, MARKED, 355) for t in times]
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-TAUNT", hits + elsewhere, samples), "FAIL")
        # the debounce folds a mob's swings inside 12 s into one trigger: n stays 5
        swings = hits + [event(11, "damage", MOB, PLAYER, None), event(12, "damage", MOB, PLAYER, None)]
        got = self.rules(self.TID, swings + taunts, samples)["TNK-WAR-TAUNT"]
        self.assertEqual((got["verdict"], got["n"]), ("PASS", 5))
        # fewer than min_n 5 triggers is INCONCLUSIVE
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-TAUNT", hits[:4] + taunts[:4], samples),
                         "INCONCLUSIVE")

    def test_mobs_on_tank_ratio(self):
        samples = series(200)
        good = ([event(10 + i, "damage", MOB, BOT, None) for i in range(20)]
                + [event(40 + i, "damage", MOB, PLAYER, None) for i in range(5)])
        bad = ([event(10 + i, "damage", MOB, BOT, None) for i in range(10)]
               + [event(40 + i, "damage", MOB, EXTRA1, None) for i in range(15)])
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-MOBS-ON-TANK", good, samples), "PASS")
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-MOBS-ON-TANK", bad, samples), "FAIL")

    def test_stances(self):
        up = series(200, player=(2457,), bot=(71,))
        down = series(200, player=(71,), bot=(2457,))
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-DEF-STANCE", [], up), "PASS")
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-DEF-STANCE", [], down), "FAIL")
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-TAUNT-STANCE", [], up), "PASS")
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-TAUNT-STANCE", [], down), "FAIL")

    def test_righteous_fury(self):
        tid = "tank-paladin-righteous-fury"
        for aura in (20450, 25780, 25781):
            self.assertEqual(self.verdict(tid, "TNK-PAL-RIGHTEOUS-FURY", [], series(200, bot=(aura,))), "PASS")
        self.assertEqual(self.verdict(tid, "TNK-PAL-RIGHTEOUS-FURY", [], series(200)), "FAIL")
        good = ([event(10 + i, "damage", MOB, BOT, None) for i in range(20)]
                + [event(40 + i, "damage", MOB, EXTRA1, None) for i in range(2)])
        bad = [event(10 + i, "damage", MOB, PLAYER, None) for i in range(25)]
        self.assertEqual(self.verdict(tid, "TNK-PAL-MOBS-ON-TANK", good, series(200)), "PASS")
        self.assertEqual(self.verdict(tid, "TNK-PAL-MOBS-ON-TANK", bad, series(200)), "FAIL")


class DpsRulesTest(RepMixin, unittest.TestCase):
    def test_threat_cap(self):
        tid = "dps-mage-threat-cap"
        samples = series(200, player=(71,))
        nukes = [event(10 + 5 * i, "cast", BOT, MOB, 133) for i in range(15)]
        self.assertEqual(self.verdict(tid, "DPS-MAG-THREAT-NO-AGGRO", nukes, samples), "PASS")
        pulled = nukes + [event(60, "damage", MOB, BOT, None)]
        self.assertEqual(self.verdict(tid, "DPS-MAG-THREAT-NO-AGGRO", pulled, samples), "FAIL")
        # DPS-MAG-THREAT-DAMAGE is a rate per minute of mob-alive time (>= 8), see DECISIONS.md
        def mob_alive(seconds):
            return samples + [{"t": float(t), "unit": "mob:" + MOB.lower(), "guid": MOB.lower(),
                               "hp": 500 if t < seconds else 0, "max_hp": 2000, "in_combat": t < seconds,
                               "auras": []} for t in range(0, 201)]
        quick = [event(2 + 3.5 * i, "cast", BOT, MOB, 133) for i in range(12)]     # 12 nukes in 44 s
        self.assertEqual(self.verdict(tid, "DPS-MAG-THREAT-DAMAGE", quick, mob_alive(50)), "PASS")      # 14 a minute
        # the same 12 nukes against a mob that lives the whole repeat: 3.6 a minute, the back-off held
        self.assertEqual(self.verdict(tid, "DPS-MAG-THREAT-DAMAGE", quick, mob_alive(200)), "FAIL")
        # a mob that lived under 45 s (the step-3 reps of sweep-1b: about 20 s) says nothing
        self.assertEqual(self.verdict(tid, "DPS-MAG-THREAT-DAMAGE", quick[:6], mob_alive(22)), "INCONCLUSIVE")
        # wand shots are not nukes: an only-wanding mage fails the precondition
        wand = [event(10 + 2 * i, "cast", BOT, MOB, sid("Shoot")) for i in range(40)]
        self.assertEqual(self.verdict(tid, "DPS-MAG-THREAT-DAMAGE", wand, mob_alive(200)), "FAIL")
        # out of combat there is no opportunity: the count rule is not scored
        idle = [s for s in samples if s["guid"] != PLAYER] + [
            sample(t, PLAYER, auras=(71,), combat=False) for t in range(0, 201)]
        self.assertEqual(self.verdict(tid, "DPS-MAG-THREAT-NO-AGGRO", nukes, idle), "INCONCLUSIVE")

    def _interrupt(self, tid, rid, spell_id, gap):
        samples = series(200, player=(71,))
        casts = [event(10 + gap * i, "cast_start", MOB, PLAYER, LIGHTNING_BOLT) for i in range(5)]
        answers = [event(11.5 + gap * i, "cast", BOT, MOB, spell_id) for i in range(5)]
        self.assertEqual(self.verdict(tid, rid, casts + answers, samples), "PASS")
        self.assertEqual(self.verdict(tid, rid, casts, samples), "FAIL")
        # an instant (not interruptible) cast is no trigger: no triggers at all -> INCONCLUSIVE
        instants = [event(10 + gap * i, "cast_start", MOB, PLAYER, 355) for i in range(5)]
        self.assertEqual(self.verdict(tid, rid, instants, samples), "INCONCLUSIVE")
        # fewer than min_n 5 triggers is INCONCLUSIVE, not a pass
        self.assertEqual(self.verdict(tid, rid, casts[:4] + answers[:4], samples), "INCONCLUSIVE")
        return casts, samples

    def test_kick(self):
        casts, samples = self._interrupt("dps-rogue-kick-caster", "DPS-ROG-KICK", sid("Kick"), 20)
        # Kidney Shot does not answer: the events cannot show that it stopped the cast
        kidney = [event(11.5 + 20 * i, "cast", BOT, MOB, sid("Kidney Shot")) for i in range(5)]
        self.assertEqual(self.verdict("dps-rogue-kick-caster", "DPS-ROG-KICK", casts + kidney, samples), "FAIL")

    def test_counterspell(self):
        self._interrupt("dps-mage-counterspell-caster", "DPS-MAG-COUNTERSPELL", 2139, 40)

    def test_interrupt_stance_preconditions(self):
        for tid, rid in (("dps-rogue-kick-caster", "DPS-ROG-KICK-STANCE"),
                         ("dps-mage-counterspell-caster", "DPS-MAG-CS-STANCE"),
                         ("dps-mage-threat-cap", "DPS-MAG-THREAT-STANCE")):
            self.assertEqual(self.verdict(tid, rid, [], series(200, player=(71,))), "PASS")
            self.assertEqual(self.verdict(tid, rid, [], series(200, player=(2457,))), "FAIL")

    def test_cc_respect(self):
        tid = "dps-mage-cc-respect"
        sheeped = lambda t: (POLYMORPH_R3,) if 20 <= t <= 70 else ()  # noqa: E731
        samples = series(200, player=(71,), marked=sheeped)
        poly = [event(18, "cast", BOT, MARKED, POLYMORPH_R3)]
        # damage on the marked mob before the sheep, or after it ends, is not a break
        before = [event(10, "damage", BOT, MARKED, 116), event(90, "damage", BOT, MARKED, 116)]
        self.assertEqual(self.verdict(tid, "DPS-MAG-CC-RESPECT", poly + before, samples), "PASS")
        broke = poly + [event(40, "damage", BOT, MARKED, 10)]          # a Blizzard tick on the sheep
        self.assertEqual(self.verdict(tid, "DPS-MAG-CC-RESPECT", broke, samples), "FAIL")
        self.assertEqual(self.verdict(tid, "DPS-MAG-CC-POLYMORPH", poly, samples), "PASS")
        self.assertEqual(self.verdict(tid, "DPS-MAG-CC-POLYMORPH", [], samples), "FAIL")


if __name__ == "__main__":
    unittest.main()
