"""A hired bot keeps the random talent build it was created with: `role <bot> <spec>` rebuilds strategies only.

The bench whispers `talents <spec>` to every hire whose trial names a spec and proves the build from the bot's own
`talents` reply (a marker talent per spec, trl/talents.py). A wrong build makes the rep INVALID with evidence.
No server is contacted: TalentAgent is the scripted agent of test_extra_bots plus a bot that holds a talent build.
"""

import copy
import glob
import json
import pathlib
import sys
import tempfile
import time
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_extra_bots as eb  # noqa: E402
import test_live as tl  # noqa: E402
from trl import scenario as scenario_mod  # noqa: E402
from trl import talents  # noqa: E402
from trl.live import gm_guard, setup  # noqa: E402

MAGE = "0x0000000000000011"


build_link = eb.build_link


class TalentAgent(eb.ScriptAgent):
    """ScriptAgent (hired bots hold a talent build, see test_extra_bots) that can also hire a mage."""

    HIRES = dict(eb.ScriptAgent.HIRES, mage=("Embercrest", MAGE))
    DEFAULT = dict(eb.ScriptAgent.DEFAULT, mage="arcane")


def mage_trial():
    trial = scenario_mod.load_trial(str(ROOT / "trials" / "dps" / "dps-mage-threat-cap.json"))
    assert trial["bot"]["spec"] == "fire" and trial["bot"]["class"] == "mage"
    trial["bot"].pop("gear", None)
    trial["bot"]["items"] = []
    trial.pop("extra_bots", None)                       # the extra-hire path has its own tests below
    keep = {"player_level", "bot_present", "bot_role", "grouped", "bot_talents"}
    trial["setup_checks"] = [c for c in trial["setup_checks"] if c in keep]
    trial["player"].pop("gear", None)
    trial["player"]["stance"] = None
    return trial


def run_it(trial, agent, tmp):
    led = tl.new_ledger(tmp)
    ctx = tl.ctx_for(hire_guid="", hire_name="")
    guard = gm_guard.GmGuard(agent, led, ctx)
    with unittest.mock.patch.object(setup, "bring_hire", lambda *a, **k: True):
        doc = setup.run_setup(agent, trial, guard, led, ctx)
    return doc, led


def row_of(doc, cid):
    return [c for c in doc["checks"] if c["id"] == cid]


class MarkerTableTest(unittest.TestCase):
    def test_fire_vulnerability_is_the_fire_marker(self):
        m = talents.marker_for("mage", "fire")
        self.assertEqual((m.name, m.tab, m.ranks[0]), ("Fire Vulnerability", 1, 11095))
        self.assertEqual(talents.marker_rank(talents.parse_link("talent spec is: fire Link: -50523201030303211"), m), 3)
        self.assertEqual(talents.marker_rank(talents.parse_link("talent spec is: arcane Link: 2050050310232311201"), m), 0)

    def test_every_marker_is_in_its_premade_and_in_no_other_spec_of_the_class(self):
        for (klass, spec), m in talents.MARKERS.items():
            self.assertGreaterEqual(int(m.premade35[m.index]), 1, (klass, spec))
            for (k2, s2), m2 in talents.MARKERS.items():
                if k2 == klass and s2 != spec and m2.tab == m.tab:
                    self.assertTrue(m.ranks[0] != m2.ranks[0], (klass, spec, s2))

    def test_every_hired_bot_spec_of_every_trial_has_a_marker(self):
        for path in sorted(glob.glob(str(ROOT / "trials" / "**" / "*.json"), recursive=True)):
            trial = json.loads(pathlib.Path(path).read_text())
            for bot in [trial.get("bot") or {}] + list(trial.get("extra_bots") or []):
                if bot.get("spec"):
                    self.assertIsNotNone(talents.marker_for(bot.get("class"), bot["spec"]),
                                         (pathlib.Path(path).name, bot.get("class"), bot["spec"]))

    def test_the_link_reply_is_parsed_and_a_missing_tree_reads_zero(self):
        self.assertEqual(talents.parse_link("My current talent spec is: fire (0/31/0) Link: 0-505"), ["0", "505", ""])
        self.assertIsNone(talents.parse_link("Strategies: fire"))
        self.assertEqual(talents.marker_rank(["", "", ""], talents.marker_for("mage", "fire")), 0)


class ApplyAndCheckTest(unittest.TestCase):
    def test_a_wrong_build_is_changed_before_scoring_and_the_row_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = TalentAgent(rolled={"mage": "arcane"})
            doc, led = run_it(mage_trial(), a, tmp)
            self.assertTrue(doc["ok"], doc)
            self.assertEqual(a.build["Embercrest"], ("mage", "fire"))
            self.assertEqual(a.sent[0], "talents fire")                       # the apply, then the read-back
            self.assertEqual(a.sent[1], "talents")
            kinds = [k for k, _t in a.order]
            self.assertLess(kinds.index("talents"), kinds.index("role"))      # talents first, then the role
            (r,) = row_of(doc, "bot_talents")
            self.assertTrue(r["ok"])
            self.assertIn("Fire Vulnerability", r["evidence"])
            self.assertEqual([x["spec"] for x in led.rows if x.get("kind") == "bot_talents"], ["fire"])

    def test_a_wrong_build_that_stays_wrong_makes_the_rep_invalid_with_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = TalentAgent(rolled={"mage": "arcane"}, apply_works=False)
            doc, _led = run_it(mage_trial(), a, tmp)
            self.assertFalse(doc["ok"])
            (r,) = row_of(doc, "bot_talents")
            self.assertFalse(r["ok"])
            self.assertIn("lacks Fire Vulnerability", r["evidence"])
            self.assertIn("link", r["evidence"])
            self.assertIn("Fire Vulnerability", doc["invalid_reason"])

    def test_a_bot_that_never_answers_the_read_fails_the_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = TalentAgent()
            orig = a.rpc

            def deaf(method, params=None, timeout=None):
                if method == "chat" and str((params or {}).get("text", "")).startswith("talents"):
                    return {}
                return orig(method, params, timeout)
            a.rpc = deaf
            row = setup.check_bot_talents(a, mage_trial()["bot"], "Embercrest", tl.new_ledger(tmp), timeout=0.0)
            self.assertFalse(row["ok"])
            self.assertIn("no talent link", row["evidence"])

    def test_every_rep_checks_even_a_reused_hire(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = TalentAgent(rolled={"mage": "frost"})
            trial = mage_trial()
            led = tl.new_ledger(tmp)
            ctx = tl.ctx_for(hire_guid="", hire_name="")
            guard = gm_guard.GmGuard(a, led, ctx)
            with unittest.mock.patch.object(setup, "bring_hire", lambda *x, **k: True):
                first = setup.run_setup(a, trial, guard, led, ctx)
                second = setup.run_setup(a, trial, guard, led, ctx, first=False, previous="Embercrest")
            self.assertTrue(first["ok"] and second["ok"], (first, second))
            self.assertEqual(len(row_of(second, "bot_talents")), 1)
            self.assertEqual([c for c in a.tbm_calls if c.startswith("hire")], ["hire mage dps level=40"])
            self.assertEqual(a.sent.count("talents fire"), 2)                 # sent again on the reused hire

    def test_a_trial_without_a_known_spec_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = {"class": "mage", "spec": None}
            a = TalentAgent()
            self.assertIsNone(setup.check_bot_talents(a, bot, "Embercrest", tl.new_ledger(tmp)))
            self.assertIsNone(setup.apply_talents(a, {"class": "mage"}, "Embercrest", tl.new_ledger(tmp)))
            self.assertEqual(a.sent, [])
            self.assertEqual([c for c in a.calls if c[0] == "chat"], [])

    def test_the_existing_priest_trial_has_the_talents_row_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = TalentAgent(rolled={"priest": "shadow"})
            trial = eb.minimal(eb.load("priest-holy-pws-def-arms"))
            doc, _ = run_it(trial, a, tmp)
            self.assertTrue(doc["ok"], doc)
            self.assertEqual(a.build["Helper"], ("priest", "holy"))
            self.assertEqual(len(row_of(doc, "bot_talents")), 1)


class ExtraHireTest(unittest.TestCase):
    def test_an_extra_hire_with_the_wrong_build_fails_the_extra_role_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = TalentAgent(rolled={"paladin": "holy"}, apply_works=True)
            trial = eb.minimal(eb.two_tank_trial())
            doc, _ = run_it(trial, a, tmp)
            self.assertTrue(doc["ok"], doc)
            self.assertEqual(a.build["Bulwark"], ("paladin", "protection"))   # fixed by the apply
            self.assertIn("talents protection", a.sent)
            (role,) = row_of(doc, "extra_bots_role")
            self.assertIn("Righteous Defense", role["evidence"])

    def test_an_extra_hire_whose_build_stays_wrong_makes_the_rep_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = TalentAgent(rolled={"paladin": "holy"}, apply_works=False)
            # the main priest must stay right, only the paladin's apply is dead
            orig = a.rpc

            def selective(method, params=None, timeout=None):
                if method == "chat" and (params or {}).get("to") == "Helper":
                    a.apply_works = True
                elif method == "chat":
                    a.apply_works = False
                return orig(method, params, timeout)
            a.rpc = selective
            doc, _ = run_it(eb.minimal(eb.two_tank_trial()), a, tmp)
            self.assertFalse(doc["ok"])
            (role,) = row_of(doc, "extra_bots_role")
            self.assertFalse(role["ok"])
            self.assertIn("lacks Righteous Defense", role["evidence"])


HYBRID = "talent spec is: |h|cffffffffarcane (0/0/0) Link: %s"


def link_reply(link):
    return "My current talent spec is: |h|cffffffffx (0/0/0) Link: %s" % link


def fire_link():
    return build_link("mage", "fire")


def arcane_link():
    return build_link("mage", "arcane")


def hybrid_link():
    """The fire marker (rank 3, index 9) in the fire tree, but nearly every point in the arcane tree."""
    return "%s-%s" % (talents.marker_for("mage", "arcane").premade35, "0000000003")


class ScriptedMage(TalentAgent):
    """A hired mage (Embercrest) whose `talents` answers are scripted: `apply_reply` (None: no answer) and the
    list of event rows said after each bare `talents` (a row is (kind, source, text))."""

    def __init__(self, apply_reply="Apply spec |h|cffffffffx 0/31/20", reads=(), party_reads=(), **kw):
        super().__init__(**kw)
        self.apply_reply = apply_reply
        self.reads = list(reads)                 # rows after a whispered bare `talents`
        self.party_reads = list(party_reads)     # rows after a party `Embercrest talents`
        self.build["Embercrest"] = ("mage", "arcane")

    def rpc(self, method, params=None, timeout=None):
        if method == "chat":
            text = str((params or {}).get("text", ""))
            to = (params or {}).get("to")
            if to == "Embercrest" and text.startswith("talents"):
                self.sent.append(text)
                self.order.append(("talents", text))
                if text == "talents":
                    for kind, source, body in self.reads:
                        self._say(kind, body, source=source)
                elif self.apply_reply is not None:
                    self._say("whisper", self.apply_reply, source="Embercrest")
                self.calls.append((method, params))
                self.spoken.append(params)
                return {}
            if to is None and text == "Embercrest talents":
                for kind, source, body in self.party_reads:
                    self._say(kind, body, source=source)
        return super().rpc(method, params, timeout)


MAGE_BOT = {"class": "mage", "spec": "fire", "role": "dps", "level": 40}


def check(agent, tmp, **kw):
    kw.setdefault("timeout", 0.0)
    return setup.check_bot_talents(agent, MAGE_BOT, "Embercrest", tl.new_ledger(tmp), guid=kw.pop("guid", ""), **kw)


class BoundReplyTest(unittest.TestCase):
    """The link must come from the hire itself: other bots, the player and system text cannot prove it."""

    def test_interleaved_replies_from_two_mages_only_the_hires_own_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            # the rival fire mage answers first, the hire (arcane, apply did nothing) second
            a = ScriptedMage(reads=[("whisper", "Rival", link_reply(fire_link())),
                                    ("whisper", "Embercrest", link_reply(arcane_link())),
                                    ("whisper", "Rival", link_reply(fire_link()))])
            row = check(a, tmp)
            self.assertFalse(row["ok"], row["evidence"])
            self.assertIn("lacks Fire Vulnerability", row["evidence"])
            self.assertIn("ignored 1 link reply", row["evidence"])
        with tempfile.TemporaryDirectory() as tmp:
            # the other way round: the rival is arcane and first, the hire is fire
            a = ScriptedMage(reads=[("whisper", "Rival", link_reply(arcane_link())),
                                    ("whisper", "Embercrest", link_reply(fire_link()))])
            row = check(a, tmp)
            self.assertTrue(row["ok"], row["evidence"])

    def test_a_spoofed_player_line_with_a_link_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptedMage(reads=[("chat", eb.PLAYER, link_reply(fire_link())),
                                    ("whisper", "Hypeer", link_reply(fire_link())),
                                    ("system", "Embercrest", link_reply(fire_link()))])    # system text is no reply
            row = check(a, tmp)
            self.assertFalse(row["ok"])
            self.assertIn("no talent link from Embercrest", row["evidence"])

    def test_a_row_with_no_sender_never_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptedMage(reads=[("whisper", None, link_reply(fire_link()))])
            self.assertFalse(check(a, tmp)["ok"])

    def test_the_party_fallback_reply_from_another_bot_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptedMage(reads=[],                                                       # the hire is deaf to whispers
                             party_reads=[("party", "Rival", link_reply(fire_link()))])
            row = check(a, tmp)
            self.assertFalse(row["ok"])
            self.assertIn("ignored 1 link reply", row["evidence"])
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptedMage(reads=[], party_reads=[("party", "Embercrest", link_reply(fire_link()))])
            self.assertTrue(check(a, tmp)["ok"])                                             # the hire's own party answer

    def test_the_party_guid_binds_the_reply_when_the_row_carries_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptedMage()
            a._say("whisper", link_reply(fire_link()), source="Embercrest", source_guid=eb.EXTRA2)   # a name clash
            rows = a.read_rows(0, timeout=0.0)
            self.assertTrue(setup._from_hire(rows[0], "Embercrest", ""))                      # no guid known: the name
            self.assertFalse(setup._from_hire(rows[0], "Embercrest", MAGE))                   # a guid known: must match
            self.assertTrue(setup._from_hire(rows[0], "Embercrest", eb.EXTRA2))


class BuildVerdictTest(unittest.TestCase):
    def test_a_stale_hybrid_with_the_marker_but_most_points_elsewhere_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptedMage(reads=[("whisper", "Embercrest", link_reply(hybrid_link()))])
            trees = talents.parse_link(link_reply(hybrid_link()))
            self.assertGreaterEqual(talents.marker_rank(trees, talents.marker_for("mage", "fire")), 1)
            row = check(a, tmp)
            self.assertFalse(row["ok"])
            self.assertIn("points are in the fire tree", row["evidence"])
            self.assertNotIn("lacks Fire Vulnerability", row["evidence"])

    def test_a_good_build_passes_with_the_share_in_the_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptedMage(reads=[("whisper", "Embercrest", link_reply(fire_link()))])
            row = check(a, tmp)
            self.assertTrue(row["ok"], row["evidence"])
            self.assertEqual(row["value"]["tree_share"], 1.0)
            self.assertIn("100%", row["evidence"])

    def test_an_explicit_apply_failure_is_invalid_even_with_a_good_build(self):
        for reply in ("Found multiple specs: fire, firestorm", "Default talent spec for this class was not fount."):
            with tempfile.TemporaryDirectory() as tmp:
                a = ScriptedMage(apply_reply=reply, reads=[("whisper", "Embercrest", link_reply(fire_link()))])
                row = check(a, tmp)
                self.assertFalse(row["ok"], reply)
                self.assertIn("the apply failed", row["evidence"])

    def test_no_apply_reply_is_a_failure_only_when_the_build_also_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptedMage(apply_reply=None, reads=[("whisper", "Embercrest", link_reply(fire_link()))])
            row = check(a, tmp)
            self.assertTrue(row["ok"], row["evidence"])
            self.assertIn("gave no reply", row["evidence"])
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptedMage(apply_reply=None, reads=[("whisper", "Embercrest", link_reply(hybrid_link()))])
            self.assertFalse(check(a, tmp)["ok"])
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptedMage(apply_reply=None, reads=[("whisper", "Embercrest", link_reply(arcane_link()))])
            self.assertFalse(check(a, tmp)["ok"])

    def test_a_malformed_link_is_invalid(self):
        for bad in ("9x-", "7", "1-2-3-4", "5-5-5-5", "fire", "50523-xx"):
            with tempfile.TemporaryDirectory() as tmp:
                a = ScriptedMage(reads=[("whisper", "Embercrest", link_reply(bad))])
                row = check(a, tmp)
                self.assertFalse(row["ok"], bad)
                self.assertTrue("malformed" in row["evidence"] or "no talent link" in row["evidence"], row["evidence"])

    def test_a_trial_spec_with_no_marker_is_invalid_not_a_silent_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptedMage()
            row = setup.check_bot_talents(a, {"class": "priest", "spec": "hybrid"}, "Embercrest",
                                          tl.new_ledger(tmp))
            self.assertFalse(row["ok"])
            self.assertIn("no talent marker for priest/hybrid", row["evidence"])
            self.assertEqual(a.sent, [])                                                     # nothing was sent

    def test_the_gear_after_the_apply_is_recorded_in_the_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptedMage(reads=[("whisper", "Embercrest", link_reply(fire_link()))])
            row = check(a, tmp)
            self.assertIn("gear after the apply: 3 item(s)", row["evidence"])

    def test_the_trials_gear_goes_on_after_the_talents(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = TalentAgent(rolled={"mage": "arcane"})
            trial = mage_trial()
            trial["bot"]["gear"] = "blue"
            doc, _ = run_it(trial, a, tmp)
            self.assertTrue(doc["ok"], doc)
            kinds = [k for k, _t in a.order]
            self.assertIn("autogear", kinds)
            self.assertLess(max(i for i, k in enumerate(kinds) if k == "talents"), kinds.index("autogear"))


class TalentDeadlineTest(unittest.TestCase):
    def test_a_silent_hire_ends_within_the_deadline_across_both_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptedMage(apply_reply=None)
            dl = setup.Deadline(1.0)
            started = time.monotonic()
            row = setup._retry(lambda: setup.check_bot_talents(a, MAGE_BOT, "Embercrest", tl.new_ledger(tmp),
                                                               timeout=30.0, deadline=dl, guid=""), deadline=dl)
            self.assertLess(time.monotonic() - started, 4.0)                                 # not 4 x 30 s
            self.assertFalse(row["ok"])

    def test_a_spent_deadline_fails_without_sending_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptedMage()
            clock = [100.0]
            dl = setup.Deadline(5.0, clock=lambda: clock[0])
            clock[0] += 6.0
            row = setup.check_bot_talents(a, MAGE_BOT, "Embercrest", tl.new_ledger(tmp), deadline=dl, guid="")
            self.assertFalse(row["ok"])
            self.assertIn("deadline", row["evidence"])

    def test_every_rpc_timeout_is_capped_by_the_time_left(self):
        clock = [0.0]
        dl = setup.Deadline(5.0, clock=lambda: clock[0])
        self.assertEqual(setup.rpc_timeout(dl), {"timeout": "5s"})
        clock[0] = 4.5
        self.assertEqual(setup.rpc_timeout(dl), {"timeout": "1s"})                           # never below 1 s
        clock[0] = 9.0
        self.assertEqual(setup.rpc_timeout(dl), {"timeout": "1s"})
        self.assertEqual(setup.rpc_timeout(None), {})
        self.assertEqual(setup.rpc_timeout(setup.Deadline(float("inf"))), {})
        seen = []
        a = ScriptedMage()
        orig = a.rpc

        def spy(method, params=None, timeout=None):
            seen.append((method, timeout))
            return orig(method, params, timeout)
        a.rpc = spy
        a.events_since(0, wait_ms=500, cap_s=2.0)
        self.assertIn(("events", "2000ms"), seen)                                          # the page RPC is capped too
        a.events_since(0, wait_ms=500)
        self.assertIn(("events", "20500ms"), seen)                                         # uncapped keeps its default


def _secs(timeout):
    """Seconds of an RPC timeout string ("5s", "20500ms"); the transport's default when none is given."""
    text = str(timeout or "20s")
    return float(text[:-2]) / 1000.0 if text.endswith("ms") else float(text.rstrip("s"))


class BlockingMage(ScriptedMage):
    """A transport whose chosen RPCs hang, as the real CLI does until ITS timeout: `blocked(method, params)`
    selects them, each sleeps for the RPC's own timeout (at most BLOCK_S) and then fails. An RPC sent without a
    cap therefore hangs 20 s; one capped by the deadline hangs only what was left."""
    BLOCK_S = 20.0

    def __init__(self, blocked, **kw):
        super().__init__(**kw)
        self.blocked = blocked
        self.started = []                            # (method, seconds into the test) of every blocked RPC
        self.t0 = time.monotonic()

    def rpc(self, method, params=None, timeout=None, **kw):
        if self.blocked(method, params or {}):
            self.started.append((method, time.monotonic() - self.t0))
            time.sleep(min(self.BLOCK_S, _secs(timeout)))
            return {"_err": "%s: timed out" % method}
        return super().rpc(method, params, timeout)


BUDGET = 1.5
SLACK = 2.5          # one RPC that started just inside the budget may run to its 1 s floor


class BlockingRpcTest(unittest.TestCase):
    """The whole talent check ends within the (scaled) budget across both `_retry` passes, whichever call hangs."""

    def run_blocked(self, blocked, timeout=30.0, guid="", **kw):
        with tempfile.TemporaryDirectory() as tmp:
            a = BlockingMage(blocked, **kw)
            dl = setup.Deadline(BUDGET)
            started = time.monotonic()
            row = setup._retry(lambda: setup.check_bot_talents(a, MAGE_BOT, "Embercrest", tl.new_ledger(tmp),
                                                               timeout=timeout, deadline=dl, guid=guid),
                               deadline=dl)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, BUDGET + SLACK, "ran %.1f s of a %.1f s budget" % (elapsed, BUDGET))
        return row, a, dl

    def test_a_hung_snapshot_head_probe(self):
        row, a, dl = self.run_blocked(lambda m, p: m == "events" and p.get("wait_ms") == 0)
        self.assertFalse(row["ok"])
        self.assertTrue(a.started)

    def test_a_hung_apply_chat(self):
        row, a, dl = self.run_blocked(lambda m, p: m == "chat" and str(p.get("text", "")).startswith("talents f"))
        self.assertFalse(row["ok"])
        self.assertIn("setup deadline", row["evidence"])
        self.assertIn("talent apply", row["evidence"])

    def test_a_hung_read_back_page(self):
        row, a, dl = self.run_blocked(lambda m, p: m == "events" and p.get("wait_ms") == 500)
        self.assertFalse(row["ok"])
        self.assertIn("setup deadline", row["evidence"])

    def test_a_hung_party_fallback(self):
        # the hire is deaf to the whisper (its own short read times out), the party fallback chat hangs
        row, a, dl = self.run_blocked(lambda m, p: m == "chat" and p.get("type") == "party", timeout=0.3)
        self.assertFalse(row["ok"])
        self.assertIn("party fallback", row["evidence"])
        self.assertTrue(any(m == "chat" for m, _t in a.started))

    def test_a_hung_party_read(self):
        row, a, dl = self.run_blocked(lambda m, p: m == "party", guid=None)
        self.assertFalse(row["ok"])
        self.assertIn("setup deadline", row["evidence"])

    def test_a_hung_inventory_read_is_capped_and_noted(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = BlockingMage(lambda m, p: m == "tbm" and str(p.get("command", "")).startswith("inv"),
                             reads=[("whisper", "Embercrest", link_reply(fire_link()))])
            dl = setup.Deadline(BUDGET)
            started = time.monotonic()
            row = setup.check_bot_talents(a, MAGE_BOT, "Embercrest", tl.new_ledger(tmp), timeout=30.0,
                                          deadline=dl, guid="")
            self.assertLess(time.monotonic() - started, BUDGET + SLACK)
            self.assertTrue(row["ok"], row["evidence"])                       # the build was proven before the hang
            self.assertIn("unreadable", row["evidence"])

    def test_no_rpc_starts_once_the_deadline_is_spent(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptedMage()
            clock = [0.0]
            dl = setup.Deadline(5.0, clock=lambda: clock[0])
            clock[0] = 6.0
            for call in (lambda: a.snapshot(dl), lambda: a.chat("whisper", "x", to="E", deadline=dl),
                         lambda: a.say_to_bot("E", "x", deadline=dl), lambda: a.tbm("inv E", deadline=dl),
                         lambda: a.spell_ids(deadline=dl), lambda: a.members(dl),
                         lambda: setup.read_inventory(a, "E", deadline=dl)):
                call()
            self.assertEqual(a.calls, [])                                          # nothing reached the transport
            out = a.dl_rpc("status", None, dl)
            self.assertTrue(out.get("_deadline"))

    def test_the_real_transport_gets_a_short_kill_grace_under_a_deadline(self):
        import unittest.mock as um
        from trl.live import agent as agent_mod
        real = agent_mod.Agent("/nonexistent-slot")
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"], captured["timeout"] = argv, kw.get("timeout")

            class R:
                returncode, stdout, stderr = 0, "{}", ""
            return R()
        clock = [0.0]
        with um.patch.object(agent_mod.subprocess, "run", fake_run):
            real.dl_rpc("party", None, setup.Deadline(3.0, clock=lambda: clock[0]))
            self.assertEqual(captured["argv"][captured["argv"].index("--timeout") + 1], "3s")
            self.assertLessEqual(captured["timeout"], 4.0)                         # 3 s + 1 s grace, not + 15 s
            real.dl_rpc("party", None, None)
            self.assertGreaterEqual(captured["timeout"], 35.0)                     # no deadline: the old 20 + 15 s


if __name__ == "__main__":
    unittest.main()
