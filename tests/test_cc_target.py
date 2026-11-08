"""cc_target: the player attacks every unmarked summon (the marked one stays untouched) and mobs_ready
needs every unmarked summon in combat. Before, only summoned[0] was attacked."""

import json
import pathlib
import sys
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_live as tl  # noqa: E402
from trl.live import agent as agent_mod, situations  # noqa: E402

norm = agent_mod.norm_guid
A, B, C = (norm(g) for g in ("0xF130000000000C01", "0xF130000000000C02", "0xF130000000000C03"))   # as the runner stores them


def make(in_combat=(A, B, C), marked=B):
    with open(str(ROOT / "trials" / "dps" / "dps-mage-cc-respect.json")) as f:
        trial = json.load(f)
    agent = tl.FakeAgent()
    ctx = situations.SitContext(player_guid=tl.PLAYER)
    ctx.guids["marked"] = marked
    sit = situations.Situation(agent, trial, None, None, None, None, ctx, arenas=[], mobs={})
    level = sit.level_window()[0] + 1          # inside the trial's own level window, whatever its level_delta
    for g in (A, B, C):
        agent.unit_rows[norm(g)] = {"guid": norm(g), "hostile": True, "dead": False, "level": level,
                                    "in_combat": g in in_combat}
    sit.summoned = [A, B, C]
    ctx.checks.append({"id": "mobs_ready", "ok": True, "value": [], "evidence": "before the pull"})
    return sit, agent


class CcTargetEngageTest(unittest.TestCase):
    def attacked(self, agent):
        """The guids the player selected and attacked, in order."""
        order, current = [], None
        for method, params in agent.calls:
            if method == "target":
                current = norm((params or {}).get("guid"))
            elif method == "attack":
                order.append(current)
        return order

    def test_every_unmarked_summon_is_attacked_and_the_marked_one_is_not(self):
        sit, agent = make()
        sit.engage_cc_targets(timeout=0.0)
        # the failing case before the change: only summoned[0] was attacked, the third mob stayed idle
        self.assertEqual(self.attacked(agent), [norm(A), norm(C)])
        self.assertNotIn(norm(B), self.attacked(agent))

    def test_mobs_ready_needs_every_unmarked_summon_in_combat(self):
        sit, agent = make(in_combat=(A, B))               # the third mob is idle
        sit.engage_cc_targets(timeout=0.0)
        row = [c for c in sit.ctx.checks if c["id"] == "mobs_ready"]
        self.assertEqual(len(row), 1)                     # replaced, not duplicated
        self.assertFalse(row[0]["ok"])
        self.assertIn("1/2 unmarked summons in combat", row[0]["evidence"])

    def test_mobs_ready_passes_when_all_unmarked_are_in_combat_even_if_the_marked_one_is_not(self):
        sit, agent = make(in_combat=(A, C))               # B is sheeped: not in combat
        sit.engage_cc_targets(timeout=0.0)
        row = [c for c in sit.ctx.checks if c["id"] == "mobs_ready"][0]
        self.assertTrue(row["ok"], row["evidence"])
        self.assertIn("2/2 unmarked summons in combat", row["evidence"])

    def test_run_calls_it_for_cc_target(self):
        # wiring: run() hands the pull to engage_cc_targets (no summoned[0]-only attack)
        sit, agent = make()
        sit.summoned = []
        sit.ctx.guids.pop("marked")
        sit.arena = lambda: {"id": "a"}
        made = []

        def summon(role, count, near_player=True):
            sit.summoned.extend([A, B, C])
            made.extend([A, B, C])
            return made
        sit.summon = summon
        sit.mark_and_cc = lambda guid: {"id": "cc_mark_set", "ok": True, "value": guid, "evidence": ""}
        sit.collector = tl.StubCollector()
        sit.marks = tl.FakeMarks()
        sit.abort = lambda: "stop at once"                # the fight loop ends on its first pass
        calls = []
        with unittest.mock.patch.object(situations.Situation, "engage_cc_targets",
                                        lambda self_, timeout=10.0: calls.append(list(self_.cc_targets()))):
            sit.run()
        self.assertEqual([[norm(g) for g in c] for c in calls], [[norm(A), norm(C)]])   # B is summoned[1]


if __name__ == "__main__":
    unittest.main()
