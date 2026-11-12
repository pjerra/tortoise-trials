"""An unclaimed `.npc summon`: the repeat is INVALID (never a world_pull), world_pull never takes another slot's
summon, and `trials cleanup` kills a recorded orphan only when it is proved to be the summoned entry near the arena."""

import os
import pathlib
import sys
import tempfile
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_live as tl  # noqa: E402
from trl.live import agent as agent_mod, lease, situations, teardown  # noqa: E402
from trl.live.ledger import Ledger  # noqa: E402

norm = agent_mod.norm_guid


class NoWorldPullTest(unittest.TestCase):
    def sit(self, tmp, foreign=None):
        agent = tl.FakeAgent()
        trial = tl.load_trial()
        led = tl.new_ledger(tmp)
        sit = situations.Situation(agent, trial, None, led, tl.FakeMarks(), tl.StubCollector(),
                                   situations.SitContext(player_guid=tl.PLAYER), arenas=[], mobs={},
                                   foreign_summons=foreign)
        sit.arena = lambda: {"id": "a"}
        return sit, agent

    def test_an_unclaimed_summon_makes_the_repeat_invalid_and_never_pulls_a_world_mob(self):
        with tempfile.TemporaryDirectory() as tmp:
            sit, agent = self.sit(tmp)

            def summon(role, count, near_player=True):
                sit.summon_blocked = True
                return []
            sit.summon = summon
            sit.world_pull = lambda: self.fail("world_pull must not run after an unclaimed summon")
            out = sit.run()
            self.assertFalse(out["ok"])
            self.assertEqual(out["ended"], "no mobs")
            self.assertEqual(out["invalid_reason"], "summon unclaimed")

    def test_a_refused_summon_without_an_orphan_still_falls_back_to_a_world_mob(self):
        with tempfile.TemporaryDirectory() as tmp:
            sit, agent = self.sit(tmp)
            sit.summon = lambda role, count, near_player=True: []
            sit.world_pull = lambda: None
            out = sit.run()
            self.assertEqual(out["invalid_reason"], "no mobs (summon refused and no world mob)")

    def test_world_pull_skips_other_slots_summons_and_this_ledgers(self):
        with tempfile.TemporaryDirectory() as tmp:
            other, mine, free = "0xF130000000000F01", "0xF130000000000F02", "0xF130000000000F03"
            sit, agent = self.sit(tmp, foreign=lambda: [other])
            sit.ledger.add({"kind": "summon", "guid": mine, "entry": 1})
            level = sit.level_window()[0]
            agent.unit_list = [{"guid": g, "hostile": True, "dead": False, "level": level, "distance": d, "entry": 1}
                               for g, d in ((other, 3.0), (mine, 4.0))]
            self.assertIsNone(sit.world_pull())
            agent.unit_list.append({"guid": free, "hostile": True, "dead": False, "level": level, "distance": 30.0,
                                    "entry": 1})
            self.assertEqual(sit.world_pull(), norm(free))


class UnclaimedRowCarriesTheArenaTest(unittest.TestCase):
    def test_the_row_has_the_new_units_and_the_arena_point(self):
        import test_sweep_fixes as sf
        with tempfile.TemporaryDirectory() as tmp:
            # two fresh units keep the miss (a lone one is claimed at any distance: bench fix r2)
            far = [{"guid": "0xF130000000000E01", "entry": 767, "dead": False, "distance": 12.0},
                   {"guid": "0xF130000000000E02", "entry": 767, "dead": False, "distance": 13.0}]
            agent, led, guard = sf.UnclaimedSummonTest().make(tmp, far)
            arena = {"id": "a1", "map": 0, "x": 1.5, "y": 2.5, "z": 3.5, "extra": "dropped"}
            self.assertIsNone(situations.summon_mob(agent, guard, led, 767, timeout=0.1, poll=0.02, arena=arena))
            row = led.of_kind("summon_unclaimed")[0]
            self.assertEqual(row["new_units"][0]["guid"], norm("0xF130000000000E01"))
            self.assertEqual(row["arena"], {"id": "a1", "map": 0, "x": 1.5, "y": 2.5, "z": 3.5})


class OrphanCleanupTest(unittest.TestCase):
    _setup = tl.CleanupTest._setup            # the cleanup fixtures, without re-running that class's tests
    _session = tl.CleanupTest._session
    _env = tl.CleanupTest._env
    _rows = staticmethod(tl.CleanupTest._rows)
    ARENA = {"id": "a1", "map": 0, "x": -12790.0, "y": -400.0, "z": 18.0}
    GOOD, WRONG_ENTRY, FAR = "0xF130000000000A01", "0xF130000000000A02", "0xF130000000000A03"

    def run_cleanup(self, tmp, units):
        run_dir, _slot_dir = self._setup(tmp, [])
        slab = Ledger(os.path.join(run_dir, "s-1.jsonl"))
        slab.add({"kind": "summon_unclaimed", "entry": 777, "arena": self.ARENA,
                  "new_units": [{"guid": norm(g), "distance": 9.0} for g in units],
                  "reply_guids": []})
        a = tl.FakeAgent()
        for g, (entry, dx) in units.items():
            a.unit_rows[norm(g)] = {"guid": norm(g), "entry": entry, "dead": False, "hostile": True,
                                    "x": self.ARENA["x"] + dx, "y": self.ARENA["y"], "z": 18.0}
        a.unit_list = [dict(v) for v in a.unit_rows.values()]
        releases, acquired, acquire, release = self._env()
        with unittest.mock.patch.object(lease, "list_slots", lambda b, s: (True, self._rows())), \
             unittest.mock.patch.object(lease, "acquire", acquire), \
             unittest.mock.patch.object(lease, "release", release), \
             unittest.mock.patch.object(teardown, "slot_teardown",
                                        lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
            rc = teardown.cleanup(run_dir, binary="/bin/true", login_factory=lambda rec, led: self._session(a))
        return rc, a, slab

    def test_a_proved_orphan_is_killed_and_a_wrong_entry_or_far_unit_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, a, slab = self.run_cleanup(tmp, {self.GOOD: (777, 2.0), self.WRONG_ENTRY: (999, 2.0),
                                                 self.FAR: (777, 20.0)})
            killed = [norm(g) for g in a.kills]
            self.assertEqual(killed, [norm(self.GOOD)])
            self.assertEqual(rc, 1)                                  # the two refused ones are reported, not killed
            held = [norm(g) for g in Ledger(slab.path).summon_guids()]      # reloaded: cleanup wrote the file
            self.assertIn(norm(self.GOOD), held)
            self.assertNotIn(norm(self.WRONG_ENTRY), held)
            self.assertNotIn(norm(self.FAR), held)

    def test_a_matching_orphan_alone_gives_a_clean_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, a, _slab = self.run_cleanup(tmp, {self.GOOD: (777, 2.0)})
            self.assertEqual([norm(g) for g in a.kills], [norm(self.GOOD)])
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
