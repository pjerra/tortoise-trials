"""Tests for the three rough edges of the live runner (offline; no server is touched).

1. Adaptive difficulty: a rep that ends on the rescue cap makes the NEXT rep of that trial one step
   easier (failing case before the change: rep 2 ran at step 0, level_delta unchanged).
2. A full account moves the session to the next free slot (failing case before: the run failed with
   LoginRefused "no free character slot").
3. `--parallel 2`: two slots, two arenas, ONE pacer, no cross-slot teardown, one merged card.
"""

import contextlib
import copy
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_live as tl  # noqa: E402
from trl import report, scenario  # noqa: E402
from trl.live import agent as agent_mod  # noqa: E402
from trl.live import gm_guard, ladder, lease, run, situations, teardown  # noqa: E402

BaseSituation = situations.Situation      # the real class, kept before rig() swaps run.situations.Situation
CLEAN_EVENTS = [{"kind": "system", "text": "Your role set to clear"},
                {"kind": "system", "text": "Removal requested."}]


def make_trial(tid="t-rough", kind="tank_damage", delta=1, count=2, refill=True, repeats=1, **sit_over):
    trial = tl.load_trial()
    trial["id"] = tid
    trial["repeats"] = repeats
    trial["situation"]["kind"] = kind
    trial["situation"]["mobs"] = {"role": "melee", "count": count, "level_delta": delta, "refill": refill}
    trial["situation"].update(sit_over)
    return trial


def slot_row(sid, port=0):
    return {"server": "s", "slot": {"id": str(sid), "character": "Trldefault%s" % sid, "port": port},
            "state": "free"}


class KeeperStub:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def stop(self):
        pass


def scripted_situation(script, seen):
    """A stand-in for situations.Situation: records the trial it was built with, ends as scripted.

    script: trial id -> list of 'floor' (the rescue cap) / 'duration' per call, in order.
    """
    counters = {}
    lock = threading.Lock()

    class Sit:
        def __init__(self, agent, trial, guard, ledger, marks, collector, ctx, **kwargs):
            with lock:
                n = counters.get(trial["id"], 0)
                counters[trial["id"]] = n + 1
            self.trial, self.ctx, self.n = trial, ctx, n
            self.summoned = []
            self.mob_source = "summon"
            seen.append((trial["id"], n + 1, trial))

        def level_window(self):
            return (34, 36)

        def run(self):
            self.ctx.checks.append({"id": "mobs_ready", "ok": True, "evidence": "scripted"})
            ended = (script.get(self.trial["id"]) or [])
            ended = ended[self.n] if self.n < len(ended) else "duration"
            return {"ok": True, "ended": ended}

    return Sit


@contextlib.contextmanager
def rig(sit_class):
    with unittest.mock.patch.object(run, "run_setup", lambda *a, **k: {
            "ok": True, "checks": [], "guids": {"player": tl.PLAYER, "bot": None, "pet": None,
                                                "mobs": [], "marked": None},
            "bot_name": "", "config": {}}), \
         unittest.mock.patch.object(run.collect, "Collector", tl.StubCollector), \
         unittest.mock.patch.object(run.situations, "Situation", sit_class), \
         unittest.mock.patch.object(run, "check_player_visible", lambda *a, **k: None), \
         unittest.mock.patch.object(run.Run, "pull_evpull", lambda *a, **k: None), \
         unittest.mock.patch.object(run.teardown, "slot_teardown",
                                    lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
        yield


def new_run(tmp, repeats=0, parallel=1):
    args = tl.run_args(tmp)
    args.repeats = repeats
    args.parallel = parallel
    r = run.Run(args, "", tmp, [])
    r.tb_version = "test"
    return r


def add_session(r, tmp, sid, agent=None, account=None):
    """A session the way prepare_slots builds one, without a server."""
    session = run.SlotSession(slot_row(sid), os.path.join(tmp, "slot%s" % sid), "", "holder%s" % sid,
                              r, lambda line: None)
    session.agent = agent or tl.FakeAgent(events=CLEAN_EVENTS)
    session.ledger = tl.new_ledger(os.path.join(tmp, "ledger%s" % sid))
    session.account = account or "ACC%s" % sid
    session.cfg = "cfg"
    session.ensure_character = lambda klass, s=session: setattr(s, "klass", klass)
    r.sessions.append(session)
    r.slot_history.append({"slot": str(sid), "account": session.account, "holder": "holder%s" % sid})
    return session


def read(path):
    with open(path) as f:
        return json.load(f)


class LadderUnitTest(unittest.TestCase):
    def test_steps_from_the_trials_own_setting(self):
        trial = make_trial(delta=1, count=3, refill=True, refill_every_s=30)
        trial["situation"]["mobs"]["refill_every_s"] = 30
        got = [ladder.params(trial, s) for s in range(4)]
        self.assertEqual([g["level_delta"] for g in got], [1, 0, -1, -1])
        self.assertEqual([g["count"] for g in got], [3, 3, 3, 1])
        self.assertEqual([g["refill"] for g in got], [True, True, True, False])
        self.assertEqual(got[3]["refill_every_s"], None)

    def test_the_mob_level_never_goes_below_player_level_minus_1_and_never_gets_harder(self):
        self.assertEqual(ladder.params(make_trial(delta=0), 0)["level_delta"], 0)      # an explicit 0 is a real 0
        self.assertEqual(ladder.params(make_trial(delta=0), 2)["level_delta"], -1)
        self.assertEqual(ladder.params(make_trial(delta=-1), 2)["level_delta"], -1)
        for step in range(4):                                   # written -2: already below the floor
            self.assertEqual(ladder.params(make_trial(delta=-2), step)["level_delta"], -2)

    def test_apply_leaves_the_original_trial_alone(self):
        trial = make_trial(delta=1, count=3)
        before = copy.deepcopy(trial)
        eff = ladder.apply(trial, 3)
        self.assertEqual(trial, before)
        self.assertEqual(eff["situation"]["mobs"]["count"], 1)
        self.assertFalse(eff["situation"]["mobs"]["refill"])
        self.assertEqual(eff["_difficulty"]["step"], 3)

    def test_default_is_on_for_mob_kinds_and_off_for_ooc(self):
        self.assertTrue(ladder.enabled(make_trial(kind="tank_damage")))
        self.assertTrue(ladder.enabled(make_trial(kind="pack")))
        self.assertFalse(ladder.enabled(make_trial(kind="ooc_hurt", count=0)))
        self.assertFalse(ladder.enabled(make_trial(kind="ooc_buffs")))
        self.assertFalse(ladder.enabled(make_trial(kind="ooc_buffs", count=0)))
        self.assertFalse(ladder.enabled(make_trial(kind="cc_target")))
        self.assertFalse(ladder.enabled(make_trial(kind="tank_damage", adaptive=False)))
        self.assertTrue(ladder.enabled(make_trial(kind="ooc_hurt", count=1, adaptive=True)))

    def test_situation_level_window_follows_the_step_and_a_real_zero(self):
        trial = make_trial(delta=1)
        sit0 = situations.Situation(None, ladder.apply(trial, 0), None, None, None, None,
                                    situations.SitContext(), arenas=[], mobs={})
        sit1 = situations.Situation(None, ladder.apply(trial, 1), None, None, None, None,
                                    situations.SitContext(), arenas=[], mobs={})
        sit2 = situations.Situation(None, ladder.apply(trial, 2), None, None, None, None,
                                    situations.SitContext(), arenas=[], mobs={})
        level = trial["player"]["level"]
        self.assertEqual(sit0.level_window(), (level, level + 2))
        self.assertEqual(sit1.level_window(), (level - 1, level + 1))       # delta 0 is a real 0 here
        self.assertEqual(sit2.level_window(), (level - 2, level))


class AdaptiveWorkerTest(unittest.TestCase):
    def setUp(self):
        run.STOP.clear()

    def run_trial(self, trial, script, repeats=0):
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            r = new_run(tmp, repeats=repeats)
            session = add_session(r, tmp, "1")
            with rig(scripted_situation(script, seen)):
                r.worker(session, [trial], r.errors)
            r.write_run_json([trial])
            reps = {}
            for name in sorted(os.listdir(os.path.join(tmp, trial["id"]))):
                reps[name] = read(os.path.join(tmp, trial["id"], name, "setup.json"))
            return seen, reps, read(os.path.join(tmp, "RUN.json"))

    def test_a_cap_rep_makes_the_next_rep_one_step_easier(self):
        # the failing case before the change: rep 2 was built at step 0 (level_delta 1, as written)
        trial = make_trial(delta=1, count=2, repeats=2)
        seen, reps, doc = self.run_trial(trial, {"t-rough": ["floor", "duration"]})
        deltas = [t["situation"]["mobs"]["level_delta"] for (_id, _n, t) in seen]
        self.assertEqual(deltas, [1, 0])
        self.assertEqual([t["_difficulty"]["step"] for (_id, _n, t) in seen], [0, 1])
        self.assertEqual(reps["rep1"]["difficulty"]["step"], 0)         # the cap rep keeps its own record
        self.assertEqual(reps["rep2"]["difficulty"]["step"], 1)
        self.assertEqual(reps["rep2"]["difficulty"]["level_delta"], 0)
        self.assertEqual(reps["rep2"]["slot"], "1")

    def test_a_cap_rep_stays_invalid_and_is_not_counted_valid(self):
        trial = make_trial(delta=1, repeats=2)
        _seen, _reps, doc = self.run_trial(trial, {"t-rough": ["floor", "duration"]})
        rows = doc["reps"]
        self.assertEqual([(r["rep"], r["capped"], r["valid"]) for r in rows], [(1, True, False), (2, False, True)])

    def test_two_cap_reps_give_step_2_and_three_give_step_3(self):
        trial = make_trial(delta=1, count=3, repeats=4)
        seen, reps, doc = self.run_trial(trial, {"t-rough": ["floor", "floor", "floor", "duration"]})
        steps = [t["_difficulty"]["step"] for (_id, _n, t) in seen]
        self.assertEqual(steps[:4], [0, 1, 2, 3])
        mobs3 = seen[3][2]["situation"]["mobs"]
        self.assertEqual((mobs3["level_delta"], mobs3["count"], mobs3["refill"]), (-1, 1, False))
        row3 = [r for r in doc["reps"] if r["rep"] == 4][0]
        self.assertEqual((row3["step"], row3["level_delta"], row3["count"], row3["refill"]), (3, -1, 1, False))

    def test_it_never_gets_harder_again(self):
        trial = make_trial(delta=1, repeats=4)
        seen, _reps, _doc = self.run_trial(trial, {"t-rough": ["floor", "duration", "duration", "duration"]})
        self.assertEqual([t["_difficulty"]["step"] for (_id, _n, t) in seen], [0, 1, 1, 1])

    def test_extra_reps_run_until_planned_minus_1_are_valid(self):
        trial = make_trial(delta=1, repeats=3)
        # cap, cap, ok -> 1 valid of the 2 wanted -> one extra rep (valid) -> stop at 4 reps
        seen, _reps, doc = self.run_trial(trial, {"t-rough": ["floor", "floor", "duration", "duration"]})
        self.assertEqual(len(seen), 4)
        self.assertEqual([t["_difficulty"]["step"] for (_id, _n, t) in seen], [0, 1, 2, 2])
        self.assertEqual(len(doc["reps"]), 4)

    def test_at_most_two_extra_reps(self):
        trial = make_trial(delta=1, repeats=3)
        seen, _reps, _doc = self.run_trial(trial, {"t-rough": ["floor"] * 9})
        self.assertEqual(len(seen), 5)                               # 3 planned + 2 extra
        self.assertEqual([t["_difficulty"]["step"] for (_id, _n, t) in seen], [0, 1, 2, 3, 3])

    def test_no_extra_rep_when_enough_reps_are_valid(self):
        trial = make_trial(delta=1, repeats=3)
        seen, _reps, _doc = self.run_trial(trial, {"t-rough": ["floor", "duration", "duration"]})
        self.assertEqual(len(seen), 3)                               # 2 valid >= 3 - 1
        seen, _reps, _doc = self.run_trial(make_trial(delta=1, repeats=1), {"t-rough": ["floor"]})
        self.assertEqual(len(seen), 1)                               # planned 1 -> nothing to make up

    def test_opt_out_turns_the_ladder_and_the_extras_off(self):
        trial = make_trial(delta=1, repeats=2, adaptive=False)
        seen, reps, doc = self.run_trial(trial, {"t-rough": ["floor"] * 9})
        self.assertEqual(len(seen), 2)
        self.assertEqual([t["situation"]["mobs"]["level_delta"] for (_id, _n, t) in seen], [1, 1])
        self.assertEqual([t["_difficulty"]["step"] for (_id, _n, t) in seen], [0, 0])
        self.assertFalse(reps["rep1"]["difficulty"]["adaptive"])

    def test_an_ooc_trial_is_not_adaptive_by_default(self):
        trial = make_trial(kind="ooc_hurt", count=0, repeats=2)
        seen, _reps, _doc = self.run_trial(trial, {"t-rough": ["floor", "floor"]})
        self.assertEqual([t["_difficulty"]["step"] for (_id, _n, t) in seen], [0, 0])

    def test_the_ladder_restarts_for_the_next_trial(self):
        first = make_trial(tid="a-trial", delta=1, repeats=2)
        second = make_trial(tid="b-trial", delta=1, repeats=1)
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            r = new_run(tmp)
            session = add_session(r, tmp, "1")
            with rig(scripted_situation({"a-trial": ["floor", "floor"]}, seen)):
                r.worker(session, [first, second], r.errors)
        by_trial = {}
        for (tid, _n, t) in seen:
            by_trial.setdefault(tid, []).append(t["_difficulty"]["step"])
        self.assertEqual(by_trial, {"a-trial": [0, 1, 2], "b-trial": [0]})   # 2 capped, 1 extra, then b at 0


class CardTest(unittest.TestCase):
    def card(self, steps, slots=None):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "RUN.json"), "w") as f:
                json.dump({"label": "x", "server": "s", "slots": ["1", "2"], "args": {"repeats": 0}}, f)
            for tid, rows in steps.items():
                for i, step in enumerate(rows, start=1):
                    d = os.path.join(tmp, tid, "rep%d" % i)
                    os.makedirs(d)
                    doc = {"ok": True, "checks": [],
                           "difficulty": {"step": step, "level_delta": 1 - step, "count": 2, "refill": True}}
                    if slots:
                        doc["slot"] = slots[tid]
                    with open(os.path.join(d, "setup.json"), "w") as f:
                        json.dump(doc, f)
            report.write_card(tmp, [{"trial": tid, "verdicts": []} for tid in steps])
            with open(os.path.join(tmp, "card.md")) as f:
                return f.read()

    def test_card_notes_the_step_per_rep_and_a_line_per_trial_below_step_0(self):
        text = self.card({"easy": [0, 0], "eased": [0, 1, 2]})
        self.assertIn("eased rep2: setup ok", text)
        self.assertIn("difficulty step 1", text)
        self.assertIn("difficulty step 2", text)
        lines = [ln for ln in text.splitlines() if "ran easier than written" in ln]
        self.assertEqual(len(lines), 1, text)
        self.assertTrue(lines[0].startswith("- eased:"))
        self.assertIn("rep3 step 2 (level_delta -1", lines[0])
        self.assertNotIn("- easy: ran easier", text)

    def test_card_with_all_reps_at_step_0_has_no_ladder_line(self):
        text = self.card({"easy": [0, 0]})
        self.assertIn("difficulty step 0", text)
        self.assertNotIn("ran easier than written", text)

    def test_scenario_validation_accepts_adaptive(self):
        for value in (True, False):
            trial = tl.load_trial()
            trial["situation"]["adaptive"] = value
            errs = [e for e in scenario.validate(trial) if "adaptive" in e or "unknown" in e.lower()]
            self.assertEqual(errs, [], errs)
        trial = tl.load_trial()
        trial["situation"]["adaptive"] = "yes"
        self.assertTrue([e for e in scenario.validate(trial) if "adaptive" in e])

    def test_every_trial_file_with_adaptive_still_validates(self):
        for path in scenario.trial_files():
            trial = scenario.load_trial(path)
            self.assertFalse([e for e in scenario.validate(trial) if "adaptive" in e], path)


class FullAccountTest(unittest.TestCase):
    """A create refused because the account is full moves the session to the next free slot."""

    def setUp(self):
        run.STOP.clear()
        self.acquired = []
        self.released = []
        self.full = set()
        self.torn = []
        self.teardown_ok = True

    @contextlib.contextmanager
    def server(self, free_ids, acquire_fail=()):
        """A fake lease table: free_ids are the slots pick_free lists (in that order)."""
        def pick_free(binary, server, n):
            return [slot_row(i) for i in free_ids][:n]          # deliberately lists slot 3 when asked

        def acquire(binary, server, slot, holder, ttl=0):
            self.acquired.append(str(slot))
            if str(slot) in acquire_fail:
                return {"ok": False, "lease_id": "", "raw": "held by another run"}
            return {"ok": True, "lease_id": "L%s" % slot, "account": "ACC%s" % slot,
                    "character": "Trldefault%s" % slot, "port": 0, "raw": ""}

        def release(binary, server, slot, holder, lease_id):
            self.released.append((str(slot), str(lease_id)))
            return True

        def login(session, name, create_spec=None):
            if create_spec and session.slot_id in self.full:
                raise run.AccountFull("no free character slot: create failed: too many characters on this realm")
            session.character = name
            session.team = "alliance"

        with unittest.mock.patch.object(run.lease, "pick_free", pick_free), \
             unittest.mock.patch.object(run.lease, "acquire", acquire), \
             unittest.mock.patch.object(run.lease, "release", release), \
             unittest.mock.patch.object(run.lease, "LeaseKeeper", KeeperStub), \
             unittest.mock.patch.object(run.SlotSession, "setup_slot",
                                        lambda s: setattr(s, "cfg", "cfg") or "start"), \
             unittest.mock.patch.object(run.SlotSession, "login", login), \
             unittest.mock.patch.object(run.Run, "pace", lambda *a, **k: 0.0), \
             unittest.mock.patch.object(run, "Agent", lambda d, log=None: tl.FakeAgent(events=CLEAN_EVENTS)), \
             unittest.mock.patch.object(run.teardown, "slot_teardown",
                                        lambda d, binary=None: (self.torn.append(os.path.basename(d))
                                                                or {"ok": self.teardown_ok, "rc": 0, "detail": ""})):
            yield

    def open_first(self, tmp, first=1, parallel=1):
        r = new_run(tmp, parallel=parallel)
        r.slots = [slot_row(first)]
        self.assertTrue(r.prepare_slots([]))
        return r, r.sessions[0]

    def test_a_full_account_on_slot_1_moves_to_slot_2(self):
        # the failing case before the change: LoginRefused ended the run on slot 1
        self.full = {"1"}
        with tempfile.TemporaryDirectory() as tmp, self.server([1, 2, 4]):
            r, session = self.open_first(tmp)
            r.ensure_character(session, "warrior")
            self.assertEqual(session.slot_id, "2")
            self.assertEqual(session.klass, "warrior")
            self.assertEqual(session.account, "ACC2")
            self.assertEqual(self.torn, ["gamma-1"])                # the full slot's graceful teardown ran ...
            self.assertEqual(self.acquired, ["1", "2"])
            r.write_run_json([make_trial()])
            doc = read(os.path.join(tmp, "RUN.json"))
            self.assertEqual(doc["slots"], ["1", "2"])
            self.assertEqual(doc["slot_accounts"], {"1": "ACC1", "2": "ACC2"})
            self.assertEqual(doc["slot_swaps"][0]["from"], "1")
            self.assertEqual(doc["slot_swaps"][0]["to"], "2")
            self.assertEqual(doc["characters"]["2"][0]["class"], "warrior")

    def test_a_failed_teardown_still_releases_the_full_slots_lease(self):
        self.full = {"1"}
        self.teardown_ok = False
        with tempfile.TemporaryDirectory() as tmp, self.server([1, 2]):
            r, session = self.open_first(tmp)
            r.ensure_character(session, "warrior")
            self.assertEqual(session.slot_id, "2")
            self.assertIn(("1", "L1"), self.released)

    def test_slot_3_is_never_tried_even_if_the_table_lists_it_free(self):
        self.full = {"1", "2"}
        with tempfile.TemporaryDirectory() as tmp, self.server([1, 3, 2, 4]):
            r, session = self.open_first(tmp)
            r.ensure_character(session, "warrior")              # 1 full -> (3 skipped) -> 2 full -> 4
            self.assertEqual(session.slot_id, "4")
            self.assertNotIn("3", self.acquired)
            self.assertEqual(self.acquired, ["1", "2", "4"])

    def test_at_most_three_slots_then_a_clear_failure(self):
        self.full = {"1", "2", "4", "5", "6"}
        with tempfile.TemporaryDirectory() as tmp, self.server([1, 2, 4, 5, 6]):
            r, session = self.open_first(tmp)
            with self.assertRaises(run.SetupInvalid) as ctx:
                r.ensure_character(session, "warrior")
            self.assertEqual(str(ctx.exception), "no free character slot on slots 1,2,4")
            self.assertEqual(self.acquired, ["1", "2", "4"])     # 5 and 6 are never touched
            self.assertNotIn("3", self.acquired)

    def test_fewer_free_slots_than_three_lists_the_slots_tried(self):
        self.full = {"1", "2"}
        with tempfile.TemporaryDirectory() as tmp, self.server([1, 2]):
            r, session = self.open_first(tmp)
            with self.assertRaises(run.SetupInvalid) as ctx:
                r.ensure_character(session, "warrior")
            self.assertEqual(str(ctx.exception), "no free character slot on slots 1,2")

    def test_a_slot_another_run_holds_is_skipped(self):
        self.full = {"1"}
        with tempfile.TemporaryDirectory() as tmp, self.server([1, 2, 4], acquire_fail=("2",)):
            r, session = self.open_first(tmp)
            r.ensure_character(session, "warrior")
            self.assertEqual(session.slot_id, "4")
            self.assertEqual(self.acquired, ["1", "2", "4"])
            self.assertEqual(session.tried_slots, ["1", "4"])    # 2 was never ours, so it is not "tried"

    def test_the_other_parallel_slot_is_never_taken_over(self):
        self.full = {"1"}
        with tempfile.TemporaryDirectory() as tmp, self.server([1, 2, 4]):
            r = new_run(tmp, parallel=2)
            r.slots = [slot_row(1), slot_row(2)]
            self.assertTrue(r.prepare_slots([]))
            self.acquired.clear()
            r.ensure_character(r.sessions[0], "warrior")
            self.assertEqual([s.slot_id for s in r.sessions], ["4", "2"])     # slot 2 stays the other worker's
            self.assertEqual(self.acquired, ["4"])

    def test_a_worker_goes_on_with_the_trial_on_the_new_slot(self):
        self.full = {"1"}
        ran = []

        def fake_repeat(self_, session, trial, rep):
            ran.append((session.slot_id, session.account, rep))
            return "", None

        trial = make_trial(repeats=1)
        with tempfile.TemporaryDirectory() as tmp, self.server([1, 2]):
            r, session = self.open_first(tmp)
            session.agent = tl.FakeAgent(events=CLEAN_EVENTS)
            session.ledger = tl.new_ledger(os.path.join(tmp, "l"))
            with unittest.mock.patch.object(run.Run, "run_repeat", fake_repeat), \
                 unittest.mock.patch.object(run.Run, "pull_evpull", lambda *a, **k: None):
                r.worker(session, [trial], r.errors)
            self.assertEqual(ran, [("2", "ACC2", 1)])
            self.assertEqual(r.errors, [])
            self.assertEqual([x for x in r.ledger.rows if x.get("kind") == "invalid"], [])

    def test_when_no_slot_is_left_the_worker_records_the_reason(self):
        self.full = {"1", "2"}
        with tempfile.TemporaryDirectory() as tmp, self.server([1, 2]):
            r, session = self.open_first(tmp)
            r.worker(session, [make_trial()], r.errors)
            self.assertTrue(any("no free character slot on slots 1,2" in e for e in r.errors), r.errors)
            invalids = [x for x in r.ledger.rows if x.get("kind") == "invalid"]
            self.assertEqual(invalids[-1]["reason"], "error: SetupInvalid")

    def test_the_login_text_of_a_full_account_is_recognised(self):
        # what turtle-agent prints today: internal/session/create.go, charCreateResult 52 / 53
        with tempfile.TemporaryDirectory() as tmp:
            for text in ("SMSG_CHAR_CREATE: create failed: too many characters on this account",
                         "SMSG_CHAR_CREATE: create failed: too many characters on this realm"):
                r = new_run(tmp)
                session = run.SlotSession(slot_row(1), os.path.join(tmp, "s1"), "/bin/true", "h", r, lambda l: None)
                session.cfg = "cfg"
                os.makedirs(session.slot_dir, exist_ok=True)

                class Proc:
                    def poll(self):
                        return 1

                def popen(argv, **kw):
                    with open(session.out, "ab") as f:
                        f.write((text + "\n").encode())
                    return Proc()

                with unittest.mock.patch.object(run.subprocess, "Popen", popen), \
                     unittest.mock.patch.object(run.Run, "pace", lambda *a, **k: 0.0), \
                     unittest.mock.patch.object(run.SlotSession, "stop_agent", lambda s: None):
                    session.agent = tl.FakeAgent(status={"state": "disconnected"})
                    with self.assertRaises(run.AccountFull):
                        session.login("Trlwarriora", create_spec="1,1")
            # a name clash is still its own error
            self.assertTrue(issubclass(run.AccountFull, run.LoginRefused))


class ParallelTwoTest(unittest.TestCase):
    def setUp(self):
        run.STOP.clear()

    def test_two_workers_two_slots_two_arenas_one_pacer_one_merged_card(self):
        a_trial = make_trial(tid="a-trial", repeats=2)
        b_trial = make_trial(tid="b-trial", repeats=2)
        arenas = {}
        barrier = threading.Barrier(2, timeout=10)
        lock = threading.Lock()

        class ArenaSit(BaseSituation):
            """The real arena() with the claim callbacks; run() holds the arena until both slots have one."""

            def __init__(self, *args, **kwargs):
                kwargs["slot_index"] = 0            # the same rotation for both: only the claim keeps them apart
                BaseSituation.__init__(self, *args, **kwargs)
                self.quiet_ok = lambda: True

            def run(self):
                arena = self.arena()
                with lock:
                    arenas.setdefault(self.trial["id"], []).append(arena["id"])
                if len(arenas[self.trial["id"]]) == 1:
                    barrier.wait()
                self.ctx.checks.append({"id": "mobs_ready", "ok": True, "evidence": "scripted"})
                return {"ok": True, "ended": "duration"}

            def level_window(self):
                return (34, 36)

        with tempfile.TemporaryDirectory() as tmp:
            r = new_run(tmp, parallel=2)
            r.pacer = run.pacing.LoginPacer()
            s1 = add_session(r, tmp, "1", account="ACCA")
            s2 = add_session(r, tmp, "2", account="ACCB")
            self.assertIs(s1.run.pacer, s2.run.pacer)             # ONE pacer for both slots
            with rig(ArenaSit), unittest.mock.patch.object(situations.time, "sleep", lambda *_: None):
                threads = [threading.Thread(target=r.worker, args=(s, [t], r.errors))
                           for s, t in ((s1, a_trial), (s2, b_trial))]
                for th in threads:
                    th.start()
                for th in threads:
                    th.join(timeout=60)
            self.assertEqual(r.errors, [])
            # the first rep of each worker held its arena at the same time: they must differ
            self.assertNotEqual(arenas["a-trial"][0], arenas["b-trial"][0])
            self.assertEqual(r.arena_claims, {})                  # and every claim went back
            r.write_run_json([a_trial, b_trial])
            doc = read(os.path.join(tmp, "RUN.json"))
            self.assertEqual(doc["slots"], ["1", "2"])
            self.assertEqual(sorted((x["trial"], x["rep"], x["slot"]) for x in doc["reps"]),
                             [("a-trial", 1, "1"), ("a-trial", 2, "1"), ("b-trial", 1, "2"), ("b-trial", 2, "2")])
            report.write_card(tmp, [{"trial": "a-trial", "verdicts": []}, {"trial": "b-trial", "verdicts": []}])
            with open(os.path.join(tmp, "card.md")) as f:
                card = f.read()
            for tid, slot in (("a-trial", "1"), ("b-trial", "2")):
                for rep in ("rep1", "rep2"):
                    line = [ln for ln in card.splitlines() if ln.startswith("- %s %s:" % (tid, rep))]
                    self.assertEqual(len(line), 1, card)
                    self.assertIn("; slot %s" % slot, line[0])
            self.assertIn("slots 1, 2", card)
            # the ledgers stayed apart: each slot's own ledger holds only its own arena rows
            arena_rows = [x for x in r.ledger.rows if x.get("kind") == "arena"]
            self.assertEqual({(x["slot"], x["trial"]) for x in arena_rows},
                             {("1", "a-trial"), ("2", "b-trial")})

    def test_an_arena_another_slot_holds_is_skipped_and_the_only_arena_is_never_shared(self):
        trial = {"id": "t", "player": {"level": 35}, "situation": {"mobs": {"role": "melee", "count": 1}}}
        arenas = [{"id": "a", "map": 0, "x": 1, "y": 2, "z": 3, "level_min": 1, "level_max": 60},
                  {"id": "b", "map": 0, "x": 11, "y": 12, "z": 13, "level_min": 1, "level_max": 60}]
        with tempfile.TemporaryDirectory() as tmp:
            r = new_run(tmp, parallel=2)
            s1 = add_session(r, tmp, "1")
            s2 = add_session(r, tmp, "2")
            a = tl.FakeAgent()
            ctx = tl.ctx_for(arena_coords=frozenset({(0, 1, 2, 3), (0, 11, 12, 13)}))

            def sit(session, pool):
                s = situations.Situation(a, trial, gm_guard.GmGuard(a, tl.new_ledger(tmp), ctx),
                                         tl.new_ledger(tmp), tl.FakeMarks(), tl.StubCollector(),
                                         situations.SitContext(player_guid=tl.PLAYER),
                                         arenas=pool, mobs={}, slot_index=0,
                                         arena_claim=lambda ar: r.claim_arena(session, ar),
                                         arena_free=lambda ar: r.free_arena(session, ar))
                s.quiet_ok = lambda: True
                return s

            with unittest.mock.patch.object(situations.time, "sleep", lambda *_: None):
                self.assertEqual(sit(s1, arenas).arena()["id"], "a")
                self.assertEqual(sit(s2, arenas).arena()["id"], "b")      # slot_index 0 too, claim diverts it
                self.assertIsNone(sit(s2, arenas[:1]).arena())             # nothing else: no shared arena
                r.free_arenas(s1)
                self.assertEqual(sit(s2, arenas[:1]).arena()["id"], "a")

    def test_a_not_quiet_arena_gives_its_claim_back(self):
        trial = {"id": "t", "player": {"level": 35}, "situation": {"mobs": {"role": "melee", "count": 1}}}
        arenas = [{"id": "a", "map": 0, "x": 1, "y": 2, "z": 3, "level_min": 1, "level_max": 60}]
        with tempfile.TemporaryDirectory() as tmp:
            r = new_run(tmp)
            s1 = add_session(r, tmp, "1")
            a = tl.FakeAgent()
            s = situations.Situation(a, trial, gm_guard.GmGuard(
                a, tl.new_ledger(tmp), tl.ctx_for(arena_coords=frozenset({(0, 1, 2, 3)}))),
                tl.new_ledger(tmp), tl.FakeMarks(), tl.StubCollector(), situations.SitContext(player_guid=tl.PLAYER),
                arenas=arenas, mobs={}, arena_claim=lambda ar: r.claim_arena(s1, ar),
                arena_free=lambda ar: r.free_arena(s1, ar))
            s.quiet_ok = lambda: False
            with unittest.mock.patch.object(situations.time, "sleep", lambda *_: None):
                self.assertIsNone(s.arena())
            self.assertEqual(r.arena_claims, {})

    def test_cleanup_of_one_slot_never_touches_the_other_slots_summons_or_hires(self):
        mob_a, mob_b = "0xF13000000000AAA1", "0xF13000000000BBB2"
        with tempfile.TemporaryDirectory() as tmp:
            r = new_run(tmp, parallel=2)
            agent_a = tl.FakeAgent(units=[{"guid": mob_a, "entry": 1234, "dead": False},
                                          {"guid": mob_b, "entry": 1234, "dead": False}],
                                   events=CLEAN_EVENTS)
            agent_b = tl.FakeAgent(units=[{"guid": mob_b, "entry": 1234, "dead": False}], events=CLEAN_EVENTS)
            s1 = add_session(r, tmp, "1", agent=agent_a)
            s2 = add_session(r, tmp, "2", agent=agent_b)
            s1.ledger.add({"kind": "summon", "guid": mob_a, "entry": 1234})
            s2.ledger.add({"kind": "summon", "guid": mob_b, "entry": 1234})
            guard = gm_guard.GmGuard(agent_a, s1.ledger, tl.ctx_for())
            teardown.repeat_cleanup(agent_a, guard, s1.ledger, "HelperA", tl.PLAYER, wait_s=0.1)
            teardown.trial_cleanup(agent_a, s1.ledger, "HelperA")
            self.assertEqual([agent_mod.norm_guid(g) for g in agent_a.kills], [agent_mod.norm_guid(mob_a)])
            self.assertEqual(agent_b.kills, [])
            self.assertNotIn("remove HelperB", agent_a.tbm_calls)
            self.assertIn("remove HelperA", agent_a.tbm_calls)
            self.assertEqual(agent_b.tbm_calls, [])
            # the guard of slot 1 refuses to select or kill slot 2's summon
            for kind in ("kill", "modify_hp"):
                with self.assertRaises(gm_guard.GmRefused):
                    if kind == "kill":
                        guard.kill(mob_b)
                    else:
                        guard.send("modify_hp", mob_b, 10, 100)
            self.assertNotIn(agent_mod.norm_guid(mob_b), [agent_mod.norm_guid(t) for (_t, t) in agent_a.gm_at])
            self.assertNotIn(agent_mod.norm_guid(mob_b), [agent_mod.norm_guid(g) for g in agent_a.targets])

    def test_a_slot_never_claims_the_other_slots_summon(self):
        mob_a, mob_b = "0xF13000000000AAA1", "0xF13000000000BBB2"
        with tempfile.TemporaryDirectory() as tmp:
            r = new_run(tmp, parallel=2)
            agent_a = tl.FakeAgent()
            # slot 2's mob of the same entry stands within 3 yd of slot 1's player and is NEW to slot 1
            agent_a.units_queue = [[], [{"guid": mob_b, "entry": 1234, "distance": 1.0, "hostile": True,
                                         "dead": False, "level": 36, "health": 500, "max_health": 500}]]
            s1 = add_session(r, tmp, "1", agent=agent_a)
            s2 = add_session(r, tmp, "2")
            s2.ledger.add({"kind": "summon", "guid": mob_b, "entry": 1234})
            guard = gm_guard.GmGuard(agent_a, s1.ledger, tl.ctx_for())
            got = situations.summon_mob(agent_a, guard, s1.ledger, 1234, timeout=0.2, poll=0.01,
                                        foreign=lambda: r.foreign_summons(s1))
            self.assertIsNone(got)
            self.assertEqual(s1.ledger.summon_guids(), [])
            # without the foreign set it would have been claimed (the gap this closes)
            agent_a.units_queue = [[], [{"guid": mob_b, "entry": 1234, "distance": 1.0, "hostile": True,
                                         "dead": False, "level": 36, "health": 500, "max_health": 500}]]
            self.assertEqual(situations.summon_mob(agent_a, guard, s1.ledger, 1234, timeout=0.2, poll=0.01),
                             agent_mod.norm_guid(mob_b))

    def test_login_pacing_goes_through_the_one_run_pacer_for_both_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = new_run(tmp, parallel=2)
            calls = []

            class RecordingPacer:
                def acquire(self, what, account=None):
                    calls.append((what, account))
                    return 0.0

            r.pacer = RecordingPacer()
            sessions = []
            for sid, acct in (("1", "ACCA"), ("2", "ACCB")):
                s = run.SlotSession(slot_row(sid), os.path.join(tmp, "s" + sid), "/bin/true", "h", r, lambda l: None)
                s.cfg, s.account = "cfg", acct
                os.makedirs(s.slot_dir, exist_ok=True)
                s.agent = tl.FakeAgent(status={"state": "in_world", "team": "alliance"})
                sessions.append(s)

            class Proc:
                def poll(self):
                    return None

            with unittest.mock.patch.object(run.subprocess, "Popen", lambda argv, **kw: Proc()), \
                 unittest.mock.patch.object(run.SlotSession, "stop_agent", lambda s: None):
                for s in sessions:
                    s.login("Trlwarriora")
            self.assertEqual([c[1] for c in calls], ["ACCA", "ACCB"])

    def test_main_builds_exactly_one_pacer_for_a_two_slot_run(self):
        rows = [slot_row(1), slot_row(2)]
        with tempfile.TemporaryDirectory() as tmp:
            args = tl.run_args(tmp)
            args.parallel = 2
            holder = {}

            def fake_execute(self_, trials):
                holder["run"] = self_
                return 0

            with unittest.mock.patch.object(run.lease, "place_allowed", lambda s, b=None: (True, "alpha")), \
                 unittest.mock.patch.object(run.lease, "pick_free", lambda b, s, n: rows), \
                 unittest.mock.patch.object(run.Run, "execute", fake_execute), \
                 unittest.mock.patch.object(run, "_install_signals", lambda: None):
                self.assertEqual(run.main(args), 0)
            r = holder["run"]
            self.assertEqual(len(r.slots), 2)
            self.assertEqual(r.pacer.state_path, run.pacing.DEFAULT_STATE)     # the machine-wide window
            self.assertIs(r.pace.__self__, r)                                  # sessions pace through the run


if __name__ == "__main__":
    unittest.main()
