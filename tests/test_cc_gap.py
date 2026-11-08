"""cc_target: a span with no unmarked summon alive is excluded (why cc_gap), so the mage nuking the only
possible target (the sheep) cannot fail DPS-MAG-CC-RESPECT by the trial's own setup."""

import json
import pathlib
import sys
import tempfile
import types
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_live as tl  # noqa: E402
import test_trials as tt  # noqa: E402
from trl import rules as rules_mod  # noqa: E402
from trl.live import agent as agent_mod, situations  # noqa: E402

norm = agent_mod.norm_guid
A, B, C = (norm(g) for g in ("0xF130000000000D01", "0xF130000000000D02", "0xF130000000000D03"))


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += max(float(s), 0.01)


def run_fight(tmp, unmarked_dead_from=None, refill=False, duration=60):
    """Drive Situation.run for the cc-respect trial. B is the marked mob. A and C die at unmarked_dead_from
    (seconds into the fight); None = they stay alive."""
    with open(str(ROOT / "trials" / "dps" / "dps-mage-cc-respect.json")) as f:
        trial = json.load(f)
    trial["duration_s"] = duration
    trial["situation"]["mobs"]["refill"] = refill
    clock = FakeClock()
    start = clock.t
    agent = tl.FakeAgent()
    led = tl.new_ledger(tmp)
    ctx = situations.SitContext(player_guid=tl.PLAYER)
    marks = tl.FakeMarks()
    sit = situations.Situation(agent, trial, None, led, marks, tl.StubCollector(), ctx, arenas=[], mobs={})
    sit.arena = lambda: {"id": "a"}

    def summon(role, count, near_player=True):
        for g in (A, B, C):
            led.add({"kind": "summon", "guid": g, "entry": 1234})
            sit.summoned.append(g)
        return list(sit.summoned)
    sit.summon = summon
    sit.mark_and_cc = lambda guid: {"id": "cc_mark_set", "ok": True, "value": guid, "evidence": ""}
    sit.engage_cc_targets = lambda timeout=10.0: None
    sit.walk_near = lambda *a, **k: True
    sit.abort = lambda: ""
    sit.collector.tick_hooks = []

    orig = agent.rpc

    def rpc(method, params=None, timeout=None):
        if method == "units":
            dead = unmarked_dead_from is not None and clock.t - start >= unmarked_dead_from
            return [{"guid": A, "entry": 1234, "dead": dead, "in_combat": True, "distance": 5.0},
                    {"guid": B, "entry": 1234, "dead": False, "in_combat": False, "distance": 5.0},
                    {"guid": C, "entry": 1234, "dead": dead, "in_combat": True, "distance": 5.0}]
        return orig(method, params, timeout)
    agent.rpc = rpc
    fake_time = types.SimpleNamespace(time=clock.time, sleep=clock.sleep)
    with unittest.mock.patch.object(situations, "time", fake_time):
        sit.run()
    return marks


class CcGapRunnerTest(unittest.TestCase):
    def gaps(self, marks):
        return [r for r in marks.rows if r.get("kind") == "excluded" and r.get("why") == "cc_gap"]

    def test_no_unmarked_mob_alive_writes_one_excluded_span_that_runs_to_the_fight_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            marks = run_fight(tmp, unmarked_dead_from=10.0, refill=False)
        gaps = self.gaps(marks)
        self.assertEqual(len(gaps), 1)             # before the change: no span, the sheep nuking would FAIL the rule
        self.assertLessEqual(gaps[0]["from"], gaps[0]["to"])   # real wall clock; the fake sleeps do not advance it

    def test_nothing_is_excluded_while_an_unmarked_mob_is_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            marks = run_fight(tmp, unmarked_dead_from=None)
        self.assertEqual(self.gaps(marks), [])


class CcRespectRuleInTheGapTest(unittest.TestCase):
    """The rule itself: a hit on the sheep inside the excluded span does not count, one outside does."""

    def evaluate(self, hit_t, span):
        with open(str(ROOT / "trials" / "dps" / "dps-mage-cc-respect.json")) as f:
            trial = json.load(f)
        rule = trial["rules"][0]
        marked = "0xF130000000000D02"
        hit = {"t": hit_t, "kind": "damage", "src": "b", "src_guid": tt.BOT, "dst": "m", "dst_guid": marked,
               "src_unit": "$bot", "dst_unit": "$marked", "spell_id": 133, "spell": "Fireball", "amount": 100,
               "dst_hp_pct": 80.0, "src_role": "bot", "dst_role": "mob", "origin": "ta"}
        samples = [dict(tt._sample(float(t), marked.lower(), 500, auras=[118]), unit="mob:" + marked.lower())
                   for t in range(0, 60)]
        rows = [hit] + ([tt._excluded(*span)] if span else [])
        ctx = {"guids": {"player": tt.PLAYER, "bot": tt.BOT, "marked": marked, "mobs": [marked]},
               "thresholds": {}, "trial": trial}
        return rules_mod.evaluate(rule, rows, samples, [], ctx)

    def test_a_hit_on_the_sheep_inside_the_cc_gap_is_not_counted(self):
        res = self.evaluate(30.0, (20.0, 40.0))
        self.assertEqual(res["verdict"], "PASS", res)
        self.assertEqual(res["value"], 0)

    def test_the_same_hit_outside_a_gap_fails_the_rule(self):
        res = self.evaluate(30.0, None)
        self.assertEqual(res["verdict"], "FAIL")
        res = self.evaluate(50.0, (20.0, 40.0))
        self.assertEqual(res["verdict"], "FAIL")


if __name__ == "__main__":
    unittest.main()
