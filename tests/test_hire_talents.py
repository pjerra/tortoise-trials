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


if __name__ == "__main__":
    unittest.main()
