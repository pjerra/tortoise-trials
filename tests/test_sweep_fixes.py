"""Fixes from the first live class sweep (28 reps, all INVALID from bench problems)."""

import json
import os
import pathlib
import re
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_live as tl  # noqa: E402
from trl.live import gm_guard, setup, situations  # noqa: E402


class LevelAgent(tl.FakeAgent):
    """`.levelup N` moves the player's level by the signed count, floor 1, like the core's command."""

    def rpc(self, method, params=None, timeout=None):
        if method == "gm":
            m = re.match(r"\.levelup (-?\d+)$", (params or {}).get("text", ""))
            if m:
                self.status_row["level"] = max(1, min(60, int(self.status_row["level"]) + int(m.group(1))))
        return super().rpc(method, params, timeout)


class LevelCarryOverTest(unittest.TestCase):
    def guard(self, agent, tmp):
        return gm_guard.GmGuard(agent, tl.new_ledger(tmp), tl.ctx_for())

    def check(self, agent, guard, level):
        trial = {"id": "t", "player": {"level": level}}
        return setup.check_level(agent, trial, guard, timeout=0.2)

    def test_trials_of_different_levels_in_one_run_each_get_their_own_level(self):
        # the sweep's order on one slot character: a 45 trial, then 40s, then a 50, then 40 again
        with tempfile.TemporaryDirectory() as tmp:
            agent = LevelAgent(status={"state": "in_world", "guid": tl.PLAYER, "level": 1, "health": 1000,
                                       "max_health": 1000, "power": 1, "max_power": 1, "team": "alliance",
                                       "target": tl.PLAYER})
            guard = self.guard(agent, tmp)
            for want in (45, 40, 40, 50, 40, 45):
                row = self.check(agent, guard, want)
                self.assertTrue(row["ok"], (want, row["evidence"]))
                self.assertEqual(int(agent.status()["level"]), want)
            # before the change: "character above trial level (45 > 40)" and no .levelup was sent
            self.assertIn(".levelup -5", agent.gm)
            self.assertIn(".levelup 10", agent.gm)

    def test_a_character_above_the_trial_level_is_lowered_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = LevelAgent(status={"state": "in_world", "guid": tl.PLAYER, "level": 45, "health": 1,
                                       "max_health": 1, "power": 1, "max_power": 1, "team": "alliance",
                                       "target": tl.PLAYER})
            row = self.check(agent, self.guard(agent, tmp), 40)
            self.assertTrue(row["ok"])
            self.assertIn("lowered from 45", row["evidence"])

    def test_a_level_that_does_not_move_is_still_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = tl.FakeAgent(status={"state": "in_world", "guid": tl.PLAYER, "level": 45, "health": 1,
                                         "max_health": 1, "power": 1, "max_power": 1, "team": "alliance",
                                         "target": tl.PLAYER})
            row = self.check(agent, self.guard(agent, tmp), 40)
            self.assertFalse(row["ok"])
            self.assertEqual(row["value"], 45)

    def test_the_guard_takes_a_negative_count_and_still_refuses_nonsense(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = tl.FakeAgent()
            g = self.guard(agent, tmp)
            self.assertEqual(g.send("levelup", tl.PLAYER, -5), ".levelup -5")
            self.assertEqual(g.send("levelup", tl.PLAYER, 10), ".levelup 10")
            for bad in (0, -60, 61):
                with self.assertRaises(gm_guard.GmRefused):
                    g.send("levelup", tl.PLAYER, bad)
            with self.assertRaises(gm_guard.GmRefused):
                g.send("levelup", tl.HIRE, -5)                 # still self only


class TalentLearnRetryTest(unittest.TestCase):
    """`.learn` answered "Player not found!" once after a talents reset: one spell stayed missing."""

    class FlakyAgent(tl.FakeAgent):
        def __init__(self, fail_first):
            super().__init__()
            self.fail_first, self.learns = set(fail_first), {}

        def rpc(self, method, params=None, timeout=None):
            if method == "gm":
                m = re.match(r"\.learn (\d+)$", (params or {}).get("text", ""))
                if m:
                    sid = int(m.group(1))
                    self.learns[sid] = self.learns.get(sid, 0) + 1
                    if sid not in self.fail_first or self.learns[sid] > 1:
                        self.spells_known.add(sid)
            return super().rpc(method, params, timeout)

    TRIAL = {"id": "t", "player": {"level": 40, "talents": {"tree": "arms", "spells": [16462, 12286]}}}

    def run_check(self, agent):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = tl.ctx_for(learn_ids=frozenset({16462, 12286}))
            return setup.check_talents(agent, self.TRIAL, gm_guard.GmGuard(agent, tl.new_ledger(tmp), ctx),
                                       ctx, settle=0.0)

    def test_one_flaky_learn_is_repeated_once(self):
        agent = self.FlakyAgent(fail_first={12286})
        row = self.run_check(agent)
        self.assertTrue(row["ok"], row["evidence"])
        self.assertEqual(agent.learns, {16462: 1, 12286: 2})     # only the missing spell is learned again

    def test_a_learn_that_never_lands_is_still_a_failure(self):
        agent = self.FlakyAgent(fail_first=set())
        agent.spells_known = set()
        agent.learns = {}
        orig = agent.rpc

        def never(method, params=None, timeout=None):
            if method == "gm" and (params or {}).get("text") == ".learn 12286":
                agent.gm.append(params["text"])
                return {}
            return orig(method, params, timeout)
        agent.rpc = never
        row = self.run_check(agent)
        self.assertFalse(row["ok"])
        self.assertIn("12286", row["evidence"])


class ArenaShortageTest(unittest.TestCase):
    """10 of the sweep's 12 mage/rogue reps ended "no arena": at level 50 only two arenas were eligible
    (the others were capped at 40 and 45), one of them is not quiet any more and the other slot held the
    second, so the second slot found nothing."""

    def eligible(self, level):
        trial = {"id": "t", "player": {"level": level}}
        sit = situations.Situation(None, trial, None, None, None, None, situations.SitContext(),
                                   arenas=situations.load_arenas(), mobs={})
        return [a["id"] for a in sit.eligible_arenas()]

    def test_every_level_has_enough_arenas_for_two_parallel_slots_and_a_spare(self):
        for level in range(1, 61):
            ids = self.eligible(level)
            if level >= 15:
                self.assertGreaterEqual(len(ids), 3, (level, ids))     # before: level 50 had 2

    def test_a_level_50_trial_can_use_the_hillsbrad_and_arathi_fields(self):
        ids = self.eligible(50)
        self.assertIn("hillsbrad-field", ids)
        self.assertIn("arathi-field", ids)


class PlayerVisibleExtrasTest(unittest.TestCase):
    """The kick trial's healer is an extra_bots hire (src_role "extra"): its 32 casts on the player did not
    count as proof of visibility, and the mob hit the rogue, not the player: "player not visible"."""

    def verdict(self, rows, role="dps"):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "events.jsonl"), "w") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
            trial = {"id": "t", "bot": {"role": role}, "situation": {"kind": "caster_mob", "victim": "player"}}
            return setup.check_player_visible(tmp, trial)

    @staticmethod
    def cast(t, role):
        return {"t": t, "kind": "cast", "src_role": role, "dst_role": "player"}

    def test_an_extra_hire_casting_on_the_player_counts(self):
        row = self.verdict([self.cast(5.0, "extra")])
        self.assertTrue(row["ok"], row["evidence"])
        self.assertEqual(row["value"]["bot_casts_on_player"], 1)

    def test_the_main_hire_still_counts_and_silence_still_fails(self):
        self.assertTrue(self.verdict([self.cast(5.0, "bot")])["ok"])
        self.assertFalse(self.verdict([])["ok"])
        self.assertFalse(self.verdict([self.cast(45.0, "extra")])["ok"])      # outside the 30 s window
        self.assertFalse(self.verdict([self.cast(5.0, "other")])["ok"])        # not a hire


class CcMarkAckTest(unittest.TestCase):
    """The build acks `TBM:ACTION_ACK|cc moon|bot:<name>|1|<name>`; the check wanted the name inside the
    intent field and read "no TBM:ACTION_ACK" while the ack was there."""

    def run_mark(self, ack_text):
        with tempfile.TemporaryDirectory() as tmp:
            agent = tl.FakeAgent(events=[{"kind": "system", "text": ack_text}])
            agent.mark_row = {"mark": "moon"}
            ctx = situations.SitContext(player_guid=tl.PLAYER, bot_name="Liftryd")
            sit = situations.Situation(agent, {"id": "t", "player": {"level": 45}}, None, tl.new_ledger(tmp),
                                       None, None, ctx, arenas=[], mobs={})
            return sit.mark_and_cc("0xF130000000000C02")

    def test_the_ack_the_build_really_sends_is_accepted(self):
        row = self.run_mark("TBM:ACTION_ACK|cc moon|bot:Liftryd|1|Liftryd")
        self.assertTrue(row["ok"], row["evidence"])

    def test_the_older_shape_with_the_name_in_the_intent_still_works(self):
        row = self.run_mark("TBM:ACTION_ACK|cc moon Liftryd|bot:Liftryd|1|Liftryd")
        self.assertTrue(row["ok"], row["evidence"])

    def test_an_ack_with_count_zero_is_not_a_mark(self):
        row = self.run_mark("TBM:ACTION_ACK|cc moon|bot:Liftryd|0|Liftryd")
        self.assertFalse(row["ok"])


if __name__ == "__main__":
    unittest.main()
