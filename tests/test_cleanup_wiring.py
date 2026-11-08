"""run.py wiring of the per-repeat summon cleanup, driven through run_repeat with a fake agent.

The scoping lives in teardown/situations (unit-tested in test_followups); these tests pin how
run_repeat uses it, so removing or loosening an argument fails here:
- earlier unverified summons are checked with "not in view" = UNKNOWN (never gone), only this
  repeat's own summons may read gone;
- summons an earlier cleanup proved dead or gone are not asked about again;
- the setup-failed path scopes the same way;
- at the run end every still-unverified summon is checked once and reported.
"""

import json
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
from trl import report  # noqa: E402
from trl.live import agent as agent_mod, run  # noqa: E402

CLEAN_EVENTS = [{"kind": "system", "text": "Your role set to clear"},
                {"kind": "system", "text": "Removal requested."}]
MOB0, MOB1, MOB2, MOB3 = ("0xF130000000000A%02d" % i for i in range(4))
NIV = "error: -32003 unit %s is not in view"


def norm(g):
    return agent_mod.norm_guid(g)


class Stop(Exception):
    """Raised by the fake situation after it did its work: run_repeat's finally runs the cleanup."""


class Harness:
    def __init__(self, tmp):
        self.tmp = tmp
        self.agent = tl.FakeAgent(events=CLEAN_EVENTS)
        self.session = tl.WorkerCleanupTest.make_session(None, tmp, self.agent)
        self.r = run.Run(tl.run_args(tmp), "", tmp, [])
        self.r.tb_version = "test"
        self.trial = tl.load_trial()
        self.script = None
        self.setup_ok = True

    def in_view(self, *guids, dead=False):
        self.agent.unit_list = [{"guid": g, "entry": 1234, "dead": dead} for g in guids]
        for g in guids:
            self.agent.unit_rows.pop(norm(g), None)

    def not_in_view(self, *guids):
        for g in guids:
            self.agent.unit_rows[norm(g)] = {"_err": NIV % g}

    def rep(self, n, summons=(), alive=(), despawned=()):
        """One repeat: the situation summons `summons` (ledger rows), `alive` stay in view,
        `despawned` answer 'not in view'."""
        def sit_run():
            for g in summons:
                self.session.ledger.add({"kind": "summon", "guid": g, "entry": 1234})
            self.in_view(*alive)
            self.not_in_view(*despawned)
            raise Stop()

        class FakeSit:
            summoned = []

            def __init__(self, *a, **k):
                pass

            def run(self):
                return sit_run()

        setup_doc = {"ok": self.setup_ok, "checks": [], "guids": {"player": tl.PLAYER, "bot": None, "pet": None,
                                                                 "mobs": [], "marked": None},
                     "bot_name": "Helper", "config": {}, "invalid_reason": "no hire"}
        with unittest.mock.patch.object(run, "run_setup", lambda *a, **k: setup_doc), \
             unittest.mock.patch.object(run.collect, "Collector", tl.StubCollector), \
             unittest.mock.patch.object(run.situations, "Situation", FakeSit), \
             unittest.mock.patch.object(run.Run, "pull_evpull", lambda *a, **k: None):
            try:
                self.r.run_repeat(self.session, self.trial, n)
            except Stop:
                pass
        path = os.path.join(self.tmp, self.trial["id"], "rep%d" % n, "marks.jsonl")
        rows = []
        if os.path.isfile(path):
            with open(path) as f:
                rows = [json.loads(line) for line in f]
        teardown_rows = [row for m in rows if m.get("kind") == "teardown" for row in m["rows"]]
        return [row for row in teardown_rows if row["id"] == "mobs_dead"]

    @property
    def verified(self):
        return self.session.verified_summons


class CleanupWiringTest(unittest.TestCase):
    def setUp(self):
        run.STOP.clear()
        patcher = unittest.mock.patch.object(run.teardown.time, "sleep", lambda s: None)   # the 1.5 s kill retries
        patcher.start()
        self.addCleanup(patcher.stop)

    def three_reps(self, h):
        """rep 1: MOB0 is killed. rep 2: MOB1 survives (its kill fails). rep 3 is in another arena:
        MOB0's corpse and MOB1 are 'not in view'; MOB2 is killed; MOB3 despawned during the repeat."""
        h.rep(1, summons=[MOB0], alive=[MOB0])
        h.agent.kill_raise = True
        h.rep(2, summons=[MOB1], alive=[MOB1])
        h.agent.kill_raise = False
        return h.rep(3, summons=[MOB2, MOB3], alive=[MOB2], despawned=[MOB0, MOB1, MOB3])

    def test_an_earlier_repeats_surviving_mob_is_never_marked_gone_by_a_later_arena(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            self.three_reps(h)
            # the failing case before the change: MOB1 read "not in view" = gone and was dropped for good
            self.assertNotIn(norm(MOB1), h.verified)

    def test_the_own_mobs_are_killed_and_a_despawned_own_mob_counts_as_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            rows = self.three_reps(h)
            self.assertIn(norm(MOB2), [norm(g) for g in h.agent.kills])
            self.assertIn(norm(MOB2), h.verified)
            self.assertIn(norm(MOB3), h.verified)        # own + not in view = gone (absent_is_gone for own)
            self.assertFalse(rows[0]["ok"])              # MOB1 is still unknown, so the row says so
            self.assertIn("UNKNOWN", rows[0]["evidence"])

    def test_summons_an_earlier_cleanup_proved_are_not_asked_about_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            rows = self.three_reps(h)
            self.assertIn(norm(MOB0), h.verified)
            self.assertNotIn(norm(MOB0), rows[0]["evidence"])     # not re-checked: no `only` fails this
            self.assertNotIn(MOB0, json.dumps([c for c in h.agent.calls if c[0] == "unit"]).replace("0xf", "0xF"))

    def test_setup_failed_cleanup_does_not_read_old_despawned_mobs_as_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            h.rep(1, summons=[MOB0], alive=[MOB0])           # verified dead
            h.in_view()
            h.not_in_view(MOB0)                              # its corpse despawned
            h.setup_ok = False
            h.rep(2)                                         # setup fails; the cleanup runs on the ledger
            # before: the whole ledger was checked, MOB0 read UNKNOWN -> server_error -> exit 5
            self.assertFalse([w for w in h.r.warnings if "summons_unverified" in w])
            self.assertEqual(h.r.exit_code(), 0)

    def test_setup_failed_cleanup_still_checks_an_unverified_summon_without_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            h.agent.kill_raise = True
            h.rep(1, summons=[MOB1], alive=[MOB1])           # survives
            h.agent.kill_raise = False
            h.in_view()                                      # nothing in view any more (another arena)
            h.not_in_view(MOB1)
            h.setup_ok = False
            h.rep(2)
            self.assertNotIn(norm(MOB1), h.verified)         # not in view stays UNKNOWN, not gone
            self.assertTrue(h.r.server_error)

    def test_run_end_rechecks_an_unverified_summon_and_reports_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            h.agent.kill_raise = True
            h.rep(1, summons=[MOB1], alive=[MOB1])
            h.in_view()
            h.not_in_view(MOB1)
            with unittest.mock.patch.object(run.teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                h.r.stop_session(h.session)
            self.assertEqual(h.r.unverified, [norm(MOB1)])
            self.assertEqual(h.r.exit_code(), 5)
            self.assertEqual([w["summons_unverified"] for w in h.r.warnings if "summons_unverified" in w],
                             [[norm(MOB1)]])
            h.r.write_run_json([h.trial])
            with open(os.path.join(tmp, "RUN.json")) as f:
                doc = json.load(f)
            self.assertEqual(doc["unverified_summons"], [norm(MOB1)])
            report.write_card(tmp, [])
            with open(os.path.join(tmp, "card.md")) as f:
                self.assertIn("not proved dead or gone", f.read())

    def test_run_end_recheck_that_finds_it_dead_reports_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(tmp)
            h.agent.kill_raise = True
            h.rep(1, summons=[MOB1], alive=[MOB1])
            h.in_view(MOB1, dead=True)                       # it died by itself
            with unittest.mock.patch.object(run.teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                h.r.stop_session(h.session)
            self.assertEqual(h.r.unverified, [])
            self.assertFalse([w for w in h.r.warnings if "summons_unverified" in w])
            self.assertIn(norm(MOB1), h.verified)


if __name__ == "__main__":
    unittest.main()
