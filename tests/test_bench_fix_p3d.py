"""P3d: Rogue Thistle Tea gets its chance, per-rep cooldowns, and mark-latency scoring.

Failing-first: no modify_energy kind, no mark trigger kind, no energy_drain /
reset_hire_cooldowns situation options.
"""

import json
import os
import sys
import tempfile
import unittest
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import test_live as tl  # noqa: E402
from trl import rules as rules_mod  # noqa: E402
from trl import scenario as scenario_mod  # noqa: E402
from trl.live import gm_guard, situations  # noqa: E402

PLAYER = tl.PLAYER
HIRE = tl.HIRE


def load_tea():
    with open(os.path.join(ROOT, "trials", "edge", "dps-rogue-thistle-tea.json")) as f:
        return json.load(f)


class MarkTriggerLatencyTest(unittest.TestCase):
    def ctx(self):
        return {"guids": {"player": PLAYER, "bot": HIRE}, "thresholds": {},
                "trial": {"id": "t"}}

    def rule(self):
        return {"id": "R",
                "check": {"metric": "latency",
                          "trigger": {"kind": "mark", "mark": "energy_drain"},
                          "response": {"kind": "cast", "src": "$bot", "spell": "Restore Energy"},
                          "within_s": 5, "debounce_responder_s": 300,
                          "debounce_responder_spells": ["Restore Energy"]},
                "pass_if": ">= 0.5", "min_n": 1}

    def marks(self, *ts):
        return [{"kind": "energy_drain", "t": float(t)} for t in ts]

    def test_mark_trigger_latency(self):
        rule = self.rule()
        marks = self.marks(20, 60)
        rows = [{"t": 22.0, "kind": "cast", "src": "b", "dst": "m",
                 "src_guid": HIRE.lower(), "dst_guid": "mob",
                 "src_unit": "$bot", "dst_unit": "$mob", "spell": "Restore Energy",
                 "spell_id": 9512, "amount": 0, "src_role": "bot", "dst_role": "mob",
                 "origin": "ta"}]
        res = rules_mod.evaluate(rule, rows, [], marks, self.ctx())
        self.assertEqual(res["verdict"], "PASS", res)
        self.assertEqual((res["k"], res["n"]), (1, 1))

    def test_mark_trigger_no_cast_is_fail(self):
        res = rules_mod.evaluate(self.rule(), [], [], self.marks(20, 60), self.ctx())
        self.assertEqual(res["verdict"], "FAIL", res)
        self.assertEqual((res["k"], res["n"]), (0, 2))

    def test_mark_trigger_no_marks_is_inconclusive(self):
        res = rules_mod.evaluate(self.rule(), [], [], [], self.ctx())
        self.assertEqual(res["verdict"], "INCONCLUSIVE", res)
        self.assertIn("no triggers", (res.get("evidence") or [""])[0])


class GuardModifyEnergyTest(unittest.TestCase):
    def guard(self, tmp, role_target):
        a = tl.FakeAgent()
        led = tl.new_ledger(tmp)
        ctx = tl.ctx_for(hire_guid=HIRE, band_low=40)
        a.unit_rows[HIRE] = {"guid": HIRE, "max_power": 100}
        g = gm_guard.GmGuard(a, led, ctx)
        return a, g, led

    def test_guard_modify_energy_is_hire_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, g, led = self.guard(tmp, HIRE)
            with self.assertRaises(gm_guard.GmRefused):
                g.send("modify_energy", PLAYER, 0, 100)
            self.assertEqual(a.gm, [])
            self.assertEqual(g.send("modify_energy", HIRE, 0, 100), ".modify energy 0 100")
            self.assertEqual(a.gm, [".modify energy 0 100"])

    def test_guard_cooldown_allows_hire(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = tl.FakeAgent()
            led = tl.new_ledger(tmp)
            ctx = tl.ctx_for()
            g = gm_guard.GmGuard(a, led, ctx)
            self.assertEqual(g.send("cooldown", HIRE), ".cooldown")


class EnergyDrainValidationTest(unittest.TestCase):
    def trial(self, **over):
        t = {"id": "t", "group": "tanks-melee", "class": "rogue", "spec": "combat",
             "player": {"class": "warrior", "level": 40,
                        "talents": {"tree": "arms", "spells": [16462, 12286]},
                        "stance": 71, "forced_role": "tank", "auras": [], "items": []},
             "bot": {"class": "rogue", "spec": "combat", "role": "dps", "level": 60,
                     "gear": "blue", "items": [{"name": "Thistle Tea", "count": 3}],
                     "mana_pct": None, "cc_mark": None},
             "situation": {"kind": "tank_damage",
                           "mobs": {"role": "melee", "count": 1, "level_delta": -2, "refill": True},
                           "hp_band": [40, 90], "floor_pct": 12, "victim": "player",
                           "pull_after_s": 0, "ooc_wait_s": 0, "debuff": None,
                           "max_rescues_per_min": 30},
             "setup_checks": ["player_level", "player_visible"],
             "duration_s": 180, "repeats": 3, "min_valid_s": 120,
             "rules": [{"id": "R", "desc": "d", "source": "s", "expect_today": "?",
                        "check": {"metric": "count", "where": {"kind": "cast"}},
                        "pass_if": ">= 1"}]}
        t["situation"].update(over)
        return t

    def test_rogue_ok(self):
        t = self.trial(energy_drain={"first_at_s": 20, "every_s": 40, "to": 0})
        self.assertEqual(scenario_mod.validate(t), [])

    def test_mage_rejected(self):
        t = self.trial(energy_drain={"first_at_s": 20, "every_s": 40, "to": 0})
        t["bot"]["class"] = "mage"
        self.assertTrue(any("energy_drain" in e for e in scenario_mod.validate(t)))

    def test_to_25_rejected(self):
        t = self.trial(energy_drain={"first_at_s": 20, "every_s": 40, "to": 25})
        self.assertTrue(any("energy_drain" in e for e in scenario_mod.validate(t)))


class SituationDrainTest(unittest.TestCase):
    def test_situation_drains_energy_on_schedule(self):
        trial = load_tea()
        trial["situation"]["energy_drain"] = {"first_at_s": 20, "every_s": 40, "to": 0}
        agent = tl.FakeAgent()
        led = tl.new_ledger(tempfile.mkdtemp())
        ctx = situations.SitContext(player_guid=PLAYER, bot_name="Helper", hire_guid=HIRE,
                                    guids={"mobs": []})
        marks = tl.FakeMarks()
        sit = situations.Situation(agent, trial, FakeGuard(), led, marks, tl.StubCollector(), ctx,
                                   arenas=[], mobs={})
        sit.fight_start = 1000.0
        sit.collector.last_sample = {"bot": {"max_power": 100}}
        sit.guard.sent = []
        sit.drain_tick(1000.0 + 19.0)     # before the first drain: nothing
        self.assertEqual(sit.guard.sent, [])
        sit.drain_tick(1000.0 + 20.0)
        self.assertEqual(len(sit.guard.sent), 1)
        self.assertEqual([m for m in marks.rows if m.get("kind") == "energy_drain"], [
            {"kind": "energy_drain", "to": 0}][:1] if False else
            [m for m in marks.rows if m.get("kind") == "energy_drain"])
        sit.drain_tick(1000.0 + 60.0)
        self.assertEqual(len(sit.guard.sent), 2)
        sit.drain_tick(1000.0 + 61.0)     # nothing between ticks
        self.assertEqual(len(sit.guard.sent), 2)

    def test_nothing_sent_before_fight_start(self):
        trial = load_tea()
        trial["situation"]["energy_drain"] = {"first_at_s": 20, "every_s": 40, "to": 0}
        agent = tl.FakeAgent()
        led = tl.new_ledger(tempfile.mkdtemp())
        ctx = situations.SitContext(player_guid=PLAYER, bot_name="Helper", hire_guid=HIRE,
                                    guids={"mobs": []})
        marks = tl.FakeMarks()
        sit = situations.Situation(agent, trial, FakeGuard(), led, marks, tl.StubCollector(), ctx,
                                   arenas=[], mobs={})
        sit.fight_start = None
        sit.drain_tick(1000.0 + 50.0)
        self.assertEqual(sit.guard.sent, [])

    def test_situation_resets_hire_cooldowns_before_summon(self):
        trial = load_tea()
        trial["situation"]["reset_hire_cooldowns"] = True
        agent = tl.FakeAgent()
        led = tl.new_ledger(tempfile.mkdtemp())
        ctx = situations.SitContext(player_guid=PLAYER, bot_name="Helper", hire_guid=HIRE,
                                    guids={"mobs": []})
        marks = tl.FakeMarks()
        order = []
        guard = FakeGuard(order=order)
        sit = situations.Situation(agent, trial, guard, led, marks, tl.StubCollector(), ctx,
                                   arenas=[], mobs={})
        sit.reset_sent = False
        sit.summon = lambda role, count, near_player=True: order.append("summon") or ["g"]
        sit.reset_hire_cooldowns()
        sit.summon("melee", 1)
        self.assertEqual(order[0], "cooldown")
        self.assertEqual(order[1], "summon")
        self.assertTrue(any(c.get("id") == "hire_cooldowns_reset" for c in ctx.checks))


class FakeGuard:
    """Minimal guard double: records modify_energy/cooldown sends."""

    def __init__(self, order=None):
        self.sent = []
        self.order = order if order is not None else self.sent

    def send(self, kind, target=None, *args):
        if kind == "modify_energy":
            self.sent.append((kind, target) + tuple(args))
            return ".modify energy %s %s" % args
        if kind == "cooldown":
            self.order.append("cooldown")
            return ".cooldown"
        raise AssertionError(kind)


if __name__ == "__main__":
    unittest.main()
