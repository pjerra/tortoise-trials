"""Tests for the edge-case trial files (trials/edge/*.json) and the variants added to existing trials.

Every file is known by id, validates, and cites bot code at a stated commit. Every rule is scored through
score.score_rep on a small synthetic repeat, once for PASS and once for FAIL (or INCONCLUSIVE), so a rule that
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

EDGE_DIR = os.path.join(ROOT, "trials", "edge")
COMMIT = "0c56cf649"

EDGE_IDS = ["dps-rogue-low-energy", "dps-rogue-thistle-tea", "priest-holy-low-mana-drinks",
            "priest-holy-low-mana-fight", "priest-holy-low-mana-potion", "tank-warrior-two-bot-tanks",
            "tank-warrior-zero-rage-opening"]

EXPECT = {
    "TNK-WAR-2T-NO-STEAL": "PASS", "TNK-WAR-2T-OTHER-HELD": "PASS", "TNK-WAR-2T-FOUGHT": "PASS",
    "TNK-WAR-2T-STANCE": "PASS",
    "PRI-LOWMANA-DRINK": "PASS", "PRI-LOWMANA-DRINK-UPTIME": "PASS",
    "PRI-LOWMANA-NO-NUKE": "PASS", "PRI-LOWMANA-HEALS": "?",
    "PRI-LOWMANA-POTION": "PASS", "PRI-LOWMANA-POTION-HEALS": "PASS",
    "TNK-WAR-RAGE0-BLOODRAGE": "?", "TNK-WAR-RAGE0-ENGAGE": "PASS", "TNK-WAR-RAGE0-EMPTY": "PASS",
    "TNK-WAR-RAGE0-STANCE": "PASS",
    "DPS-ROG-LOWENERGY-SWINGS": "PASS", "DPS-ROG-LOWENERGY-ACTIVE": "PASS", "DPS-ROG-LOWENERGY-STANCE": "PASS",
    "DPS-ROG-TEA": "?", "DPS-ROG-TEA-ACTIVE": "PASS", "DPS-ROG-TEA-STANCE": "PASS",
}

# trial id -> the variants the file carries (name -> what it overrides)
VARIANTS = {
    "priest-holy-heal-latency": ["lvl50", "lvl60"],
    "tank-warrior-taunt-off-healer": ["lvl50", "lvl60"],
    "dps-mage-threat-cap": ["lvl50", "lvl60", "frost"],
    "tank-warrior-no-taunt-tank-player": ["bot-offtank"],
    "priest-holy-low-mana-drinks": ["mana-5", "mana-50"],
}

PLAYER = "0x0000000000000001"
BOT = "0x0000000000000002"
EXTRA1 = "0x0000000000000003"
EXTRA2 = "0x0000000000000004"
MOB = "0xf13000000000abcd"
ROLE = {PLAYER: "player", BOT: "bot", EXTRA1: "extra1", EXTRA2: "extra2", MOB: "mob"}

STANCE_BATTLE = 2457
STANCE_DEF = 71
DRINK = 24355


def load(tid):
    return scenario_mod.load_trial(os.path.join(EDGE_DIR, tid + ".json"))


def sid(name):
    return sorted(spells_mod.ids_for(name))[0]


def sample(t, guid, auras=(), target="", combat=True, hp=600, power=500, max_power=1000):
    return {"t": float(t), "wall": None, "unit": ROLE[guid], "guid": guid, "hp": hp,
            "max_hp": 1000, "power": power, "max_power": max_power, "auras": list(auras),
            "casting": None, "in_combat": combat, "target": target,
            "x": 0.0, "y": 0.0, "z": 0.0}


def event(t, kind, src, dst, spell, amount=100):
    return {"t": float(t), "wall": None, "kind": kind, "src": ROLE[src], "src_guid": src,
            "dst": ROLE[dst], "dst_guid": dst, "src_unit": ROLE[src], "dst_unit": ROLE[dst],
            "spell_id": sid(spell) if spell else None, "spell": spell, "amount": amount, "dst_hp_pct": 60,
            "src_role": "mob" if src == MOB else ROLE[src], "dst_role": "mob" if dst == MOB else ROLE[dst],
            "origin": "ta"}


def series(secs, player_auras=(), bot_auras=(), bot_power=500, mob_target=PLAYER, combat=True,
           extras=(EXTRA1, EXTRA2), mob_target_at=None):
    """1 Hz samples: player, bot, the extras and one mob. `mob_target_at(t)` overrides the mob's target."""
    out = []
    for t in range(0, secs + 1):
        power = bot_power(t) if callable(bot_power) else bot_power
        auras = bot_auras(t) if callable(bot_auras) else bot_auras
        out.append(sample(t, PLAYER, auras=player_auras, target=MOB, combat=combat))
        out.append(sample(t, BOT, auras=auras, target=MOB, combat=combat, power=power))
        for guid in extras:
            out.append(sample(t, guid, target="", combat=combat))
        target = mob_target_at(t) if mob_target_at else mob_target
        out.append(sample(t, MOB, target=target, combat=combat))
    return out


class RepMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.n = 0

    def rules(self, tid, events, samples, secs=200, extra_marks=(), trial=None):
        """Score one synthetic repeat of trial `tid`; return {rule id: entry}."""
        self.n += 1
        trial = trial or load(tid)
        rep = os.path.join(self.tmp, "run", tid, "rep%d" % self.n)
        os.makedirs(rep)
        doc = {"ok": True, "guids": {"player": PLAYER, "bot": BOT, "extras": [EXTRA1, EXTRA2], "mobs": [MOB]},
               "config": {"critical": 20, "low": 50, "medium": 70}}
        with open(os.path.join(rep, "setup.json"), "w") as f:
            json.dump(doc, f)
        marks = [{"kind": "fight_start", "t": 0}] + list(extra_marks) + [
            {"kind": "fight_end", "t": secs, "ended": "duration"}]
        for name, rows in (("samples.jsonl", samples), ("events.jsonl", events), ("marks.jsonl", marks)):
            with open(os.path.join(rep, name), "w") as f:
                for row in rows:
                    f.write(json.dumps(row) + "\n")
        out = score_mod.score_rep(rep, trial)
        return {r["id"]: r for r in out["rules"]}

    def verdict(self, tid, rid, events, samples, **kw):
        return self.rules(tid, events, samples, **kw)[rid]["verdict"]


class FilesTest(unittest.TestCase):
    def test_every_file_is_known(self):
        files = sorted(glob.glob(os.path.join(EDGE_DIR, "*.json")))
        self.assertEqual([os.path.basename(p)[:-5] for p in files], sorted(EDGE_IDS))

    def test_every_trial_validates(self):
        for tid in EDGE_IDS:
            trial = load(tid)
            self.assertEqual(trial["id"], tid)
            self.assertEqual(scenario_mod.validate(trial), [], tid)

    def test_rules_cite_bot_code_at_a_commit_and_expect_the_table(self):
        seen = set()
        for tid in EDGE_IDS:
            for rule in load(tid)["rules"]:
                seen.add(rule["id"])
                self.assertIn(rule["id"], EXPECT, tid)
                self.assertEqual(rule["expect_today"], EXPECT[rule["id"]], rule["id"])
                self.assertTrue(rule.get("why_expect"), rule["id"])
                if not rule["source"].startswith("precondition"):
                    self.assertIn("ai/playerbot/", rule["source"], rule["id"])
                    self.assertIn("upstream/dev " + COMMIT, rule["source"], rule["id"])
                    # a file:line cite, not a bare file name
                    self.assertRegex(rule["source"], r"\.(cpp|h):\d+", rule["id"])
        self.assertEqual(seen, set(EXPECT))

    def test_variants_the_files_carry(self):
        found = {}
        for path in scenario_mod.trial_files():
            trial = scenario_mod.load_trial(path)
            if "variants" in trial:
                found[trial["id"]] = [v["name"] for v in trial["variants"]]
        self.assertEqual(found, VARIANTS)

    def test_level_variants_never_go_under_the_talent_proof_floor(self):
        # the three levels the sweep asks for are 40 (the file), 50 and 60; level 20 is refused (see DECISIONS.md)
        for tid in ("priest-holy-heal-latency", "tank-warrior-taunt-off-healer", "dps-mage-threat-cap"):
            path = [p for p in scenario_mod.trial_files() if os.path.basename(p) == tid + ".json"][0]
            trial = scenario_mod.load_trial(path)
            merged = {n: m for n, m in scenario_mod.expand_variants(trial)}
            self.assertEqual(trial["player"]["level"], 40)
            self.assertEqual(merged["lvl50"]["player"]["level"], 50)
            self.assertEqual(merged["lvl60"]["player"]["level"], 60)
            for m in merged.values():
                self.assertEqual(m["bot"]["level"], m["player"]["level"])
                for extra in m.get("extra_bots") or []:
                    self.assertEqual(extra["level"], m["player"]["level"])
            # no gear kit above level 50: the level 60 variants run without one
            self.assertEqual(merged["lvl50"]["player"].get("gear"), trial["player"].get("gear"))
            self.assertIsNone(merged["lvl60"]["player"].get("gear"))

    def test_frost_variant_is_a_frost_mage_with_frost_rules(self):
        path = [p for p in scenario_mod.trial_files() if os.path.basename(p) == "dps-mage-threat-cap.json"][0]
        trial = scenario_mod.load_trial(path)
        merged = dict(scenario_mod.expand_variants(trial))["frost"]
        self.assertEqual((merged["class"], merged["spec"]), ("mage", "frost"))
        self.assertEqual((merged["bot"]["class"], merged["bot"]["spec"]), ("mage", "frost"))
        dmg = [r for r in merged["rules"] if r["id"] == "DPS-MAG-THREAT-DAMAGE"][0]
        self.assertEqual(dmg["check"]["where"]["spell"], ["Frostbolt", "Fire Blast", "Frost Nova"])
        self.assertIn("FrostMageStrategy.cpp", dmg["source"])
        noaggro = [r for r in merged["rules"] if r["id"] == "DPS-MAG-THREAT-NO-AGGRO"][0]
        self.assertIn("frost", noaggro["desc"])

    def test_bot_offtank_variant_adds_the_paladin_tank(self):
        path = os.path.join(ROOT, "trials", "tanks", "tank-warrior-no-taunt-tank-player.json")
        merged = dict(scenario_mod.expand_variants(scenario_mod.load_trial(path)))["bot-offtank"]
        self.assertEqual([(b["class"], b["role"]) for b in merged["extra_bots"]],
                         [("priest", "healer"), ("paladin", "tank")])
        self.assertEqual(merged["player"]["forced_role"], "tank")

    def test_mana_variants_differ_only_in_the_mana(self):
        trial = load("priest-holy-low-mana-drinks")
        merged = dict(scenario_mod.expand_variants(trial))
        self.assertEqual(trial["bot"]["mana_pct"], 15)
        self.assertEqual(merged["mana-5"]["bot"]["mana_pct"], 5)
        self.assertEqual(merged["mana-50"]["bot"]["mana_pct"], 50)
        for m in merged.values():
            self.assertEqual(m["rules"], trial["rules"])
            self.assertIn("bot_mana", m["setup_checks"])

    def test_groups_and_roles(self):
        self.assertEqual(load("tank-warrior-two-bot-tanks")["group"], "tanks-melee")
        self.assertEqual(load("tank-warrior-two-bot-tanks")["player"]["forced_role"], "dps")
        extras = load("tank-warrior-two-bot-tanks")["extra_bots"]
        self.assertEqual((extras[0]["class"], extras[0]["role"]), ("paladin", "tank"))
        self.assertEqual(extras[1]["role"], "healer")
        for tid in ("priest-holy-low-mana-drinks", "priest-holy-low-mana-fight", "priest-holy-low-mana-potion"):
            self.assertEqual(load(tid)["bot"]["mana_pct"], 15, tid)
            self.assertIn("bot_mana", load(tid)["setup_checks"], tid)

class TwoBotTanksTest(RepMixin, unittest.TestCase):
    TID = "tank-warrior-two-bot-tanks"

    def steal(self, events, samples, marks=()):
        return self.verdict(self.TID, "TNK-WAR-2T-NO-STEAL", events, samples, extra_marks=marks)

    def test_no_steal(self):
        taunt = sid("Taunt")
        on_player = series(200, mob_target=PLAYER)
        on_paladin = series(200, mob_target=EXTRA1)
        ev = [event(30, "cast", BOT, MOB, "Taunt")]
        self.assertEqual(self.steal(ev, on_player), "PASS")          # taunting a mob on the DPS player is the job
        self.assertEqual(self.steal(ev, on_paladin), "FAIL")         # the same cast on the mob the paladin holds
        self.assertEqual(self.steal([event(30, "cast", BOT, MOB, "Mocking Blow")], on_paladin), "FAIL")
        self.assertEqual(self.steal([event(30, "cast", BOT, MOB, "Challenging Shout")], on_paladin), "FAIL")
        self.assertEqual(self.steal([], on_paladin), "PASS")
        self.assertTrue(taunt)

    def test_the_opening_race_and_a_refill_are_not_counted(self):
        on_paladin = series(200, mob_target=EXTRA1)
        self.assertEqual(self.steal([event(5, "cast", BOT, MOB, "Taunt")], on_paladin), "PASS")      # before t 10
        refill = [{"kind": "refill", "t": 60}]
        self.assertEqual(self.steal([event(64, "cast", BOT, MOB, "Taunt")], on_paladin, refill), "PASS")
        self.assertEqual(self.steal([event(70, "cast", BOT, MOB, "Taunt")], on_paladin, refill), "FAIL")

    def test_the_other_tank_must_have_held_a_mob(self):
        held = [event(20 + i, "damage", MOB, EXTRA1, None) for i in range(5)]
        samples = series(200)
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-2T-OTHER-HELD", held, samples), "PASS")
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-2T-OTHER-HELD", held[:4], samples), "FAIL")
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-2T-OTHER-HELD",
                                      [event(20 + i, "damage", MOB, PLAYER, None) for i in range(9)], samples), "FAIL")

    def test_fought_and_stance(self):
        samples = series(200, player_auras=[STANCE_BATTLE])
        cast = [event(10 + i, "cast", BOT, MOB, "Sunder Armor") for i in range(5)]
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-2T-FOUGHT", cast, samples), "PASS")
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-2T-FOUGHT", cast[:4], samples), "FAIL")
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-2T-STANCE", [], samples), "PASS")
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-2T-STANCE", [], series(200)), "FAIL")


class LowManaDrinkTest(RepMixin, unittest.TestCase):
    TID = "priest-holy-low-mana-drinks"

    def ooc(self, **kw):
        return series(60, combat=False, extras=(), **kw)

    def test_first_drink_within_30_s(self):
        aura = lambda t: [DRINK] if t >= 8 else []        # noqa: E731
        ev = lambda t: [event(t, "aura_on", BOT, BOT, "Drink")]    # noqa: E731
        self.assertEqual(self.verdict(self.TID, "PRI-LOWMANA-DRINK", ev(8), self.ooc(bot_auras=aura), secs=60), "PASS")
        self.assertEqual(self.verdict(self.TID, "PRI-LOWMANA-DRINK", ev(45), self.ooc(), secs=60), "FAIL")
        self.assertEqual(self.verdict(self.TID, "PRI-LOWMANA-DRINK", [], self.ooc(), secs=60), "FAIL")
        # a Drink on the player does not count
        self.assertEqual(self.verdict(self.TID, "PRI-LOWMANA-DRINK",
                                      [event(8, "aura_on", BOT, PLAYER, "Drink")], self.ooc(), secs=60), "FAIL")

    def test_uptime(self):
        long_drink = self.ooc(bot_auras=lambda t: [DRINK] if 8 <= t < 38 else [])
        short_drink = self.ooc(bot_auras=lambda t: [DRINK] if 8 <= t < 12 else [])
        self.assertEqual(self.verdict(self.TID, "PRI-LOWMANA-DRINK-UPTIME", [], long_drink, secs=60), "PASS")
        self.assertEqual(self.verdict(self.TID, "PRI-LOWMANA-DRINK-UPTIME", [], short_drink, secs=60), "FAIL")
        self.assertEqual(self.verdict(self.TID, "PRI-LOWMANA-DRINK-UPTIME", [], self.ooc(), secs=60), "FAIL")


class LowManaFightTest(RepMixin, unittest.TestCase):
    TID = "priest-holy-low-mana-fight"

    def test_no_nuke_while_mana_is_low(self):
        smite = [event(30, "cast", BOT, MOB, "Smite")]
        low = series(200, bot_power=300)           # 30 percent of the pool
        high = series(200, bot_power=900)          # 90 percent
        self.assertEqual(self.verdict(self.TID, "PRI-LOWMANA-NO-NUKE", smite, low), "FAIL")
        self.assertEqual(self.verdict(self.TID, "PRI-LOWMANA-NO-NUKE", smite, high), "PASS")
        self.assertEqual(self.verdict(self.TID, "PRI-LOWMANA-NO-NUKE", [], low), "PASS")
        wand = [event(30, "cast", BOT, MOB, "Shoot")]
        self.assertEqual(self.verdict(self.TID, "PRI-LOWMANA-NO-NUKE", wand, low), "PASS")   # the wand costs no mana
        for spell in ("Holy Fire", "Mind Blast", "Shadow Word: Pain"):
            self.assertEqual(self.verdict(self.TID, "PRI-LOWMANA-NO-NUKE", [event(30, "cast", BOT, MOB, spell)], low),
                             "FAIL", spell)

    def test_still_heals(self):
        heals = [event(20 + 10 * i, "cast", BOT, PLAYER, "Renew") for i in range(3)]
        samples = series(200)
        self.assertEqual(self.verdict(self.TID, "PRI-LOWMANA-HEALS", heals, samples), "PASS")
        self.assertEqual(self.verdict(self.TID, "PRI-LOWMANA-HEALS", heals[:2], samples), "FAIL")
        # a heal on the paladin or the mob is not a heal on the player
        other = [event(20 + 10 * i, "cast", BOT, EXTRA1, "Renew") for i in range(5)]
        self.assertEqual(self.verdict(self.TID, "PRI-LOWMANA-HEALS", other, samples), "FAIL")


class LowManaPotionTest(RepMixin, unittest.TestCase):
    TID = "priest-holy-low-mana-potion"

    def test_potion_within_30_s(self):
        samples = series(200)
        self.assertEqual(self.verdict(self.TID, "PRI-LOWMANA-POTION",
                                      [event(12, "cast", BOT, BOT, "Restore Mana")], samples), "PASS")
        self.assertEqual(self.verdict(self.TID, "PRI-LOWMANA-POTION",
                                      [event(55, "cast", BOT, BOT, "Restore Mana")], samples), "FAIL")
        self.assertEqual(self.verdict(self.TID, "PRI-LOWMANA-POTION", [], samples), "FAIL")
        self.assertEqual(self.verdict(self.TID, "PRI-LOWMANA-POTION",
                                      [event(12, "cast", BOT, BOT, "Renew")], samples), "FAIL")

    def test_a_potion_from_2_s_before_the_window_counts(self):
        # bench-fix-1112: a ready bot answers during the pull (rep1's potion 0.24 s before
        # fight_start was not counted). A potion at t -2 is inside the window now.
        samples = series(200)
        got = self.rules(self.TID, [event(-0.24, "cast", BOT, BOT, "Restore Mana")], samples)
        self.assertEqual((got["PRI-LOWMANA-POTION"]["verdict"],
                          got["PRI-LOWMANA-POTION"]["value"]), ("PASS", -0.24))
        far = self.rules(self.TID, [event(-2.5, "cast", BOT, BOT, "Restore Mana")], samples)
        self.assertEqual(far["PRI-LOWMANA-POTION"]["verdict"], "FAIL")

    def test_the_trial_carries_the_item_cooldown_across_reps(self):
        rule = [r for r in load(self.TID)["rules"] if r["id"] == "PRI-LOWMANA-POTION"][0]
        self.assertEqual(rule["check"]["where"]["t_from"], -2)
        self.assertTrue(rule["check"]["debounce_responder_across_reps"])
        self.assertEqual(rule["check"]["debounce_responder_s"], 120)
        self.assertEqual(rule["check"]["debounce_responder_spells"], ["Restore Mana"])
        self.assertIn("120 s", rule["why_expect"])
        self.assertEqual(scenario_mod.validate(load(self.TID)), [])

    def test_bad_cooldown_shapes_on_a_first_rule_are_rejected(self):
        import copy
        trial = load(self.TID)
        orig = copy.deepcopy(trial["rules"][0]["check"])
        trial["rules"][0]["check"]["debounce_responder_spells"] = ["Renew"]
        self.assertTrue(any("debounce_responder_spells" in e
                            for e in scenario_mod.validate(trial)))
        trial["rules"][0]["check"] = copy.deepcopy(orig)
        trial["rules"][0]["check"]["debounce_responder_across_reps"] = False
        self.assertTrue(any("debounce_responder_across_reps" in e
                            for e in scenario_mod.validate(trial)))

    def test_heals_after_the_potion(self):
        heals = [event(20 + 10 * i, "cast", BOT, PLAYER, "Flash Heal") for i in range(3)]
        self.assertEqual(self.verdict(self.TID, "PRI-LOWMANA-POTION-HEALS", heals, series(200)), "PASS")
        self.assertEqual(self.verdict(self.TID, "PRI-LOWMANA-POTION-HEALS", heals[:1], series(200)), "FAIL")


class PotionCrossRepCooldownTest(unittest.TestCase):
    """Rep2 of the nightly run opened ~2 min after rep1's potion: the 120 s item cooldown the
    per-rep `.cooldown` cannot clear. bench-fix-1112 carries it across reps (the tea precedent):
    a rep whose only potion falls inside an earlier rep's 120 s is INCONCLUSIVE, not a miss."""

    TID = "priest-holy-low-mana-potion"

    def wall(self, base, offset):
        import datetime
        return datetime.datetime.fromtimestamp(base + offset,
                                               datetime.timezone.utc).isoformat(timespec="milliseconds")

    def write_rep(self, trial_dir, name, base, potion_at):
        rep = os.path.join(trial_dir, name)
        os.makedirs(rep)
        setup = {"ok": True, "guids": {"player": PLAYER, "bot": BOT, "mobs": [MOB]}, "config": {}}
        with open(os.path.join(rep, "setup.json"), "w") as f:
            json.dump(setup, f)
        marks = [{"kind": "fight_start", "wall": self.wall(base, 0)},
                 {"kind": "fight_end", "wall": self.wall(base, 200), "ended": "duration"}]
        events = []
        if potion_at is not None:
            events.append(event(potion_at, "cast", BOT, BOT, "Restore Mana"))
        for fname, rows in (("marks.jsonl", marks), ("events.jsonl", events), ("samples.jsonl", [])):
            with open(os.path.join(rep, fname), "w") as f:
                for row in rows:
                    f.write(json.dumps(row) + "\n")
        return rep

    def score(self, potions, spacing=122.0):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tdir = os.path.join(tmp, "run", self.TID)
            base = 1_800_000_000.0
            for i, at in enumerate(potions):
                # reps start `spacing` apart (122 s is the nightly spacing: rep2 opens inside rep1's cooldown)
                self.write_rep(tdir, "rep%d" % (i + 1), base + i * spacing, at)
            trial = load(self.TID)
            return score_mod.score_trial(tdir, trial)["verdicts"][0]

    def test_a_potion_inside_a_partially_covering_cooldown_is_a_miss(self):
        # bench-fix-1112 round 4: at the nightly 122 s spacing rep 2's cooldown is -112..8, so the
        # t=5 potion stamped inside it is impossible (or a proc row) and is dropped; rep 2 had 8..30
        # left to drink, so the empty hit list is a miss, not no-opportunity.
        got = self.score([10, 5])
        self.assertEqual((got["verdict"], got["k"], got["n"]), ("FAIL", 1, 2), got)
        rep2 = [d for d in got["detail"] if d["rep"] == "rep2"][0]
        self.assertEqual(rep2["verdict"], "FAIL")
        self.assertEqual(rep2["reason"], "")

    def test_a_potion_inside_a_fully_covered_window_is_no_chance(self):
        # 80 s spacing leaves rep 2's cooldown at -70..50, which covers its whole -2..30 window:
        # the potion at t=5 sits in it, so the rep is no-opportunity, never a miss.
        got = self.score([10, 5], spacing=80.0)
        self.assertEqual((got["verdict"], got["k"], got["n"]), ("PASS", 1, 1), got)
        rep2 = [d for d in got["detail"] if d["rep"] == "rep2"][0]
        self.assertEqual(rep2["verdict"], "INCONCLUSIVE")
        self.assertEqual(rep2["reason"], "no opportunity (item on cooldown from an earlier rep)")

    def test_a_potion_after_the_cooldown_counts(self):
        got = self.score([10, None, 5])
        rep3 = [d for d in got["detail"] if d["rep"] == "rep3"][0]
        self.assertEqual((rep3["verdict"], rep3["value"]), ("PASS", 5.0))

    def test_no_potion_at_all_is_still_a_miss(self):
        got = self.score([None])
        rep1 = [d for d in got["detail"] if d["rep"] == "rep1"][0]
        self.assertEqual(rep1["verdict"], "FAIL")

    def test_no_potion_in_a_fully_covered_window_is_no_chance(self):
        # codex HIGH (round 1, tightened in round 3): rep 2 opens inside rep 1's 120 s item cooldown
        # with NO potion cast. Only a cooldown that covers the WHOLE response window (here -2..30 on
        # rep2's base) is no opportunity; 80 s spacing leaves the cooldown at -70..50.
        got = self.score([10, None], spacing=80.0)
        rep2 = [d for d in got["detail"] if d["rep"] == "rep2"][0]
        self.assertEqual(rep2["verdict"], "INCONCLUSIVE", rep2)
        self.assertEqual(rep2["reason"], "no opportunity (item on cooldown from an earlier rep)")

    def test_no_potion_in_a_partially_covered_window_is_a_miss(self):
        # round 3: at the nightly 122 s spacing the cooldown is -112..8, so rep2 still had 8..30 to
        # drink: the no-match scoring applies ("<= 30" FAILs), it is not no-opportunity.
        got = self.score([10, None])
        rep2 = [d for d in got["detail"] if d["rep"] == "rep2"][0]
        self.assertEqual(rep2["verdict"], "FAIL", rep2)
        self.assertEqual(rep2["reason"], "")

class ZeroRageTest(RepMixin, unittest.TestCase):
    TID = "tank-warrior-zero-rage-opening"

    def test_bloodrage_within_10_s(self):
        samples = series(200)
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-RAGE0-BLOODRAGE",
                                      [event(4, "cast", BOT, BOT, "Bloodrage")], samples), "PASS")
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-RAGE0-BLOODRAGE",
                                      [event(25, "cast", BOT, BOT, "Bloodrage")], samples), "FAIL")
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-RAGE0-BLOODRAGE", [], samples), "FAIL")

    def test_engages_within_8_s(self):
        samples = series(200)
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-RAGE0-ENGAGE",
                                      [event(3, "damage", BOT, MOB, None)], samples), "PASS")
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-RAGE0-ENGAGE",
                                      [event(15, "damage", BOT, MOB, None)], samples), "FAIL")
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-RAGE0-ENGAGE", [], samples), "FAIL")

    def test_the_start_really_is_empty(self):
        hit = [event(3, "damage", BOT, MOB, None)]
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-RAGE0-EMPTY", hit, series(200, bot_power=100)), "PASS")
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-RAGE0-EMPTY", hit, series(200, bot_power=800)), "FAIL")
        late = [event(30, "damage", BOT, MOB, None)]
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-RAGE0-EMPTY", late, series(200, bot_power=100)), "FAIL")

    def test_stance(self):
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-RAGE0-STANCE", [],
                                      series(200, player_auras=[STANCE_BATTLE])), "PASS")
        self.assertEqual(self.verdict(self.TID, "TNK-WAR-RAGE0-STANCE", [], series(200)), "FAIL")

    def test_pull_waits_so_the_rage_decays(self):
        self.assertEqual(load(self.TID)["situation"]["pull_after_s"], 20)


class RogueEnergyTest(RepMixin, unittest.TestCase):
    def swings(self, n, start=10, step=1.5):
        return [event(start + step * i, "damage", BOT, MOB, None) for i in range(n)]

    def test_swings_while_empty(self):
        tid = "dps-rogue-low-energy"
        empty = series(200, bot_power=100)         # 10 percent of the bar
        full = series(200, bot_power=900)
        self.assertEqual(self.verdict(tid, "DPS-ROG-LOWENERGY-SWINGS", self.swings(8), empty), "PASS")
        self.assertEqual(self.verdict(tid, "DPS-ROG-LOWENERGY-SWINGS", self.swings(7), empty), "FAIL")
        self.assertEqual(self.verdict(tid, "DPS-ROG-LOWENERGY-SWINGS", self.swings(40), full), "FAIL")

    def test_active_rate(self):
        for tid, rid in (("dps-rogue-low-energy", "DPS-ROG-LOWENERGY-ACTIVE"), ("dps-rogue-thistle-tea", "DPS-ROG-TEA-ACTIVE")):
            samples = series(200)
            self.assertEqual(self.verdict(tid, rid, self.swings(100, step=1.8), samples), "PASS", tid)
            self.assertEqual(self.verdict(tid, rid, self.swings(20, step=8), samples), "FAIL", tid)

    def test_stances(self):
        for tid, rid in (("dps-rogue-low-energy", "DPS-ROG-LOWENERGY-STANCE"), ("dps-rogue-thistle-tea", "DPS-ROG-TEA-STANCE")):
            self.assertEqual(self.verdict(tid, rid, [], series(200, player_auras=[STANCE_DEF])), "PASS", tid)
            self.assertEqual(self.verdict(tid, rid, [], series(200)), "FAIL", tid)

    def test_thistle_tea_answers_the_drain(self):
        tid = "dps-rogue-thistle-tea"
        samples = series(200)
        drains = ({"kind": "energy_drain", "t": 20.0}, {"kind": "energy_drain", "t": 60.0})
        tea = [event(22, "cast", BOT, BOT, "Restore Energy")]
        self.assertEqual(self.verdict(tid, "DPS-ROG-TEA", tea, samples, extra_marks=drains),
                         "PASS")
        # answered once: the 300 s responder cooldown skips the second drain (answered 1/1, not 1/2)
        self.assertEqual(self.rules(tid, tea, samples, extra_marks=drains)["DPS-ROG-TEA"]["n"], 1)
        # no answer to two drains: 0/2 FAIL
        self.assertEqual(self.verdict(tid, "DPS-ROG-TEA", [], samples, extra_marks=drains),
                         "FAIL")
        # no drain, no chance: INCONCLUSIVE, not a verdict on the bot
        self.assertEqual(self.verdict(tid, "DPS-ROG-TEA", [], samples), "INCONCLUSIVE")

    def test_the_tea_trial_gives_the_item_and_runs_at_60_without_a_kit(self):
        trial = load("dps-rogue-thistle-tea")
        self.assertEqual(trial["bot"]["items"], [{"name": "Thistle Tea", "count": 3}])
        self.assertIn("bot_items", trial["setup_checks"])
        self.assertNotIn("player_gear", trial["setup_checks"])
        self.assertIsNone(trial["player"]["gear"])
        self.assertEqual(trial["player"]["level"], 60)


if __name__ == "__main__":
    unittest.main()
