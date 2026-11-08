"""Tests for `extra_bots` (extra hired bots): schema, hire identity, GM guard, teardown, cleanup.

No server is contacted: ScriptAgent is test_live.FakeAgent plus a tiny scripted server
(a `.bot ...` order appends the reply events, a hire adds a party row).

Run from the tool directory: python3 -I -m unittest discover -s tests
"""

import copy
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
from trl import scenario as scenario_mod  # noqa: E402
from trl import score as score_mod  # noqa: E402
from trl.live import agent as agent_mod  # noqa: E402
from trl.live import gm_guard, lease, setup, teardown  # noqa: E402
from trl.live.ledger import Ledger  # noqa: E402

PLAYER = tl.PLAYER
HIRE = tl.HIRE                      # the main bot (the Holy priest)
EXTRA1 = "0x0000000000000003"
EXTRA2 = "0x0000000000000004"
STRANGER = "0x0000000000000009"
TRIAL_DIR = ROOT / "trials" / "healers"
TWO_TANK = ["priest-holy-pws-def-arms-2tank", "priest-holy-pws-bear-bal-2tank"]


def load(tid):
    return scenario_mod.load_trial(str(TRIAL_DIR / (tid + ".json")))


class ScriptAgent(tl.FakeAgent):
    """FakeAgent that answers `.bot` orders: hires land in the party, the events honour `since`."""

    HIRES = {"priest": ("Helper", HIRE), "paladin": ("Bulwark", EXTRA1), "warrior": ("Wally", EXTRA2)}

    def __init__(self, **kwargs):
        kwargs.setdefault("status", {"state": "in_world", "guid": PLAYER, "level": 40, "health": 1000,
                                     "max_health": 1000, "power": 500, "max_power": 500,
                                     "team": "alliance", "target": PLAYER})
        super().__init__(**kwargs)
        self.party_row = {"in_group": True, "members": []}
        self.hire_replies = True        # False: the server says nothing to a hire
        self.spoken = []

    def _say(self, kind, text):
        self.event_rows.append({"kind": kind, "text": text})

    def rpc(self, method, params=None, timeout=None):
        if method == "events":
            since = int((params or {}).get("since", 0))
            rows = [r for r in self.event_rows[since:]
                    if not (params or {}).get("kinds") or r.get("kind") in params["kinds"]]
            return {"events": rows, "next": len(self.event_rows), "oldest": 0, "dropped": False}
        if method == "tbm":
            command = params["command"]
            since = len(self.event_rows)
            self.tbm_calls.append(command)
            self.calls.append((method, params))
            self._respond(command)
            return {"since": since}
        if method == "chat":
            self.spoken.append(params)
            if str(params.get("text", "")).endswith("co ?"):
                self._say("system", "Strategies: protection, tank assist, holy, heal")
            self.calls.append((method, params))
            return {}
        return super().rpc(method, params, timeout)

    def _respond(self, command):
        words = command.split()
        if words[0] == "hire" and self.hire_replies:
            name, guid = self.HIRES[words[1]]
            self._say("system", "You have hired %s for 30 minutes." % name)
            self.party_row["members"].append({"name": name, "guid": guid, "level": 40})
        elif words[0] == "role" and words[1] == "self":
            self._say("system", "Your role set to clear")
        elif words[0] == "role":
            self._say("system", "Bot %s role set to %s" % (words[1], words[2]))
        elif words[0] == "remove":
            self._say("system", "Removal requested for %s." % words[1])


def two_tank_trial(**over):
    trial = copy.deepcopy(load(TWO_TANK[0]))
    trial.update(over)
    return trial


def minimal(trial):
    """The solo/2tank trial cut to the checks a scripted agent can answer quickly."""
    trial = copy.deepcopy(trial)
    trial["bot"].pop("gear", None)
    trial["bot"]["items"] = []
    keep = {"player_level", "bot_present", "bot_role", "grouped",
            "extra_bots_present", "extra_bots_role", "extra_bots_in_group"}
    trial["setup_checks"] = [c for c in trial["setup_checks"] if c in keep]
    trial["player"]["stance"] = None
    return trial


# ---- schema ----------------------------------------------------------------

class SchemaTest(unittest.TestCase):
    def test_both_2tank_trials_validate_and_carry_the_paladin(self):
        for tid in TWO_TANK:
            trial = load(tid)
            self.assertEqual(scenario_mod.validate(trial), [], tid)
            self.assertEqual(trial["extra_bots"],
                             [{"class": "paladin", "spec": "protection", "role": "tank", "level": 40,
                               "items": []}])
            for cid in scenario_mod.EXTRA_BOT_CHECKS:
                self.assertIn(cid, trial["setup_checks"], tid)

    def errs(self, **extra_over):
        trial = two_tank_trial()
        trial["extra_bots"] = [dict(trial["extra_bots"][0], **extra_over)]
        return scenario_mod.validate(trial)

    def test_accepts_up_to_three_and_items(self):
        trial = two_tank_trial()
        one = trial["extra_bots"][0]
        trial["extra_bots"] = [dict(one), dict(one),
                               dict(one, items=[{"name": "Sweet Nectar", "count": 5}])]
        self.assertEqual(scenario_mod.validate(trial), [])

    def test_rejects_four_extra_bots(self):
        trial = two_tank_trial()
        trial["extra_bots"] = [dict(trial["extra_bots"][0]) for _ in range(4)]
        self.assertTrue(any("at most 3" in e for e in scenario_mod.validate(trial)))

    def test_rejects_unknown_and_bad_fields(self):
        self.assertTrue(any("unknown extra_bots[1] field 'gear'" in e for e in self.errs(gear="blue")))
        self.assertTrue(any("unknown extra_bots[1] field 'mana_pct'" in e for e in self.errs(mana_pct=50)))
        self.assertTrue(any("bad extra_bots[1] spec" in e for e in self.errs(spec="nonsense")))
        self.assertTrue(any("bad extra_bots[1] role" in e for e in self.errs(role="boss")))
        self.assertTrue(any("level must be" in e for e in self.errs(level=61)))
        self.assertTrue(any("level must be" in e for e in self.errs(level="40")))
        self.assertTrue(any("bad extra_bots[1] class" in e for e in self.errs(**{"class": "Pal Adin"})))
        self.assertTrue(any("item" in e for e in self.errs(items=[{"name": "x", "count": 99}])))
        self.assertTrue(any("item" in e for e in self.errs(items=[{"name": "x", "extra": 1}])))

    def test_rejects_non_list_empty_and_non_object(self):
        for bad in ({"class": "paladin"}, [], ["paladin"], "paladin"):
            trial = two_tank_trial(extra_bots=bad)
            self.assertTrue(scenario_mod.validate(trial), bad)

    def test_missing_field_is_rejected(self):
        trial = two_tank_trial()
        del trial["extra_bots"][0]["spec"]
        self.assertTrue(any("missing spec" in e for e in scenario_mod.validate(trial)))

    def test_extra_bots_needs_its_three_setup_checks_and_only_it(self):
        trial = two_tank_trial()
        trial["setup_checks"] = [c for c in trial["setup_checks"] if c != "extra_bots_role"]
        self.assertTrue(any("needs setup check 'extra_bots_role'" in e for e in scenario_mod.validate(trial)))
        solo = load("priest-holy-pws-def-arms")
        solo["setup_checks"] = solo["setup_checks"] + ["extra_bots_present"]
        self.assertTrue(any("needs extra_bots" in e for e in scenario_mod.validate(solo)))

    def test_solo_trials_still_validate_unchanged(self):
        for tid in ("priest-holy-pws-def-arms", "priest-holy-pws-bear-bal"):
            self.assertEqual(scenario_mod.validate(load(tid)), [])
            self.assertNotIn("extra_bots", load(tid))

    def test_aliases_extra1_to_3_and_extras(self):
        trial = two_tank_trial()
        rule = trial["rules"][0]
        rule["check"]["where"]["src"] = ["$bot", "$extra1"]
        self.assertEqual(scenario_mod.validate(trial), [])
        rule["check"]["where"]["dst"] = "$extras"
        self.assertEqual(scenario_mod.validate(trial), [])
        rule["check"]["where"]["dst"] = "$extra4"
        self.assertTrue(any("bad dst alias '$extra4'" in e for e in scenario_mod.validate(trial)))

    def test_alias_without_a_bot_behind_it_is_rejected(self):
        trial = two_tank_trial()
        trial["rules"][0]["check"]["where"]["src"] = "$extra2"
        self.assertTrue(any("'$extra2' has no extra_bots" in e for e in scenario_mod.validate(trial)))
        solo = load("priest-holy-pws-def-arms")
        solo["rules"][0]["check"]["where"]["src"] = "$extras"
        self.assertTrue(any("'$extras' has no extra_bots" in e for e in scenario_mod.validate(solo)))


# ---- aliases in the rule engine ---------------------------------------------

class AliasScoringTest(unittest.TestCase):
    def score(self, where, events, extras=(EXTRA1, EXTRA2)):
        trial = two_tank_trial()
        trial["extra_bots"] = [copy.deepcopy(trial["extra_bots"][0]) for _ in range(max(2, len(extras)))]
        trial["rules"] = [{"id": "X", "desc": "d", "source": "s", "expect_today": "PASS",
                           "check": {"metric": "count", "where": dict(where, kind="cast")},
                           "pass_if": "== 0"}]
        trial["opportunity"] = {"unit": "$extra1", "hp_lt": 90, "in_combat": True}
        self.assertEqual(scenario_mod.validate(trial), [])
        with tempfile.TemporaryDirectory() as tmp:
            rep = os.path.join(tmp, "run", "t", "rep1")
            os.makedirs(rep)
            doc = {"ok": True, "guids": {"player": PLAYER, "bot": HIRE, "extras": list(extras), "mobs": []},
                   "config": {"critical": 20, "low": 50, "medium": 70}}
            samples = [{"t": float(t), "wall": None, "unit": "x", "guid": g, "hp": 600, "max_hp": 1000,
                        "power": 1, "max_power": 1, "auras": [], "casting": None, "in_combat": True,
                        "target": "", "x": 0.0, "y": 0.0, "z": 0.0}
                       for t in range(0, 201) for g in (PLAYER, HIRE) + tuple(extras)]
            marks = [{"kind": "fight_start", "t": 0}, {"kind": "fight_end", "t": 200, "ended": "duration"}]
            for name, rows in (("setup.json", None), ("samples.jsonl", samples), ("events.jsonl", events),
                               ("marks.jsonl", marks)):
                with open(os.path.join(rep, name), "w") as f:
                    if rows is None:
                        json.dump(doc, f)
                    else:
                        for row in rows:
                            f.write(json.dumps(row) + "\n")
            out = score_mod.score_rep(rep, trial)
        return out["rules"][0]

    @staticmethod
    def cast(src, dst=PLAYER, t=50):
        return {"t": float(t), "wall": None, "kind": "cast", "src": "x", "src_guid": src, "dst": "y",
                "dst_guid": dst, "src_unit": "", "dst_unit": "", "spell_id": 6065, "spell": "Power Word: Shield",
                "amount": None, "dst_hp_pct": 60, "src_role": "other", "dst_role": "player", "origin": "ta"}

    def test_extra1_matches_only_the_first_extra(self):
        events = [self.cast(EXTRA1), self.cast(EXTRA2, t=60), self.cast(HIRE, t=70)]
        self.assertEqual(self.score({"src": "$extra1"}, events)["value"], 1)
        self.assertEqual(self.score({"src": "$extra2"}, events)["value"], 1)

    def test_extras_matches_every_extra_and_not_the_main_bot(self):
        events = [self.cast(EXTRA1), self.cast(EXTRA2, t=60), self.cast(HIRE, t=70)]
        self.assertEqual(self.score({"src": "$extras"}, events)["value"], 2)
        self.assertEqual(self.score({"src": "$bot"}, events)["value"], 1)

    def test_missing_extra_matches_nothing(self):
        events = [self.cast(EXTRA1)]
        self.assertEqual(self.score({"src": "$extra2"}, events, extras=(EXTRA1,))["value"], 0)


class NormaliseTest(unittest.TestCase):
    def test_events_label_the_extras(self):
        from trl import events as events_mod
        with tempfile.TemporaryDirectory() as tmp:
            guids = {"player": PLAYER, "bot": HIRE, "pet": None, "extras": [EXTRA1, EXTRA2],
                     "mobs": [], "marked": None}
            with open(os.path.join(tmp, "setup.json"), "w") as f:
                json.dump({"ok": True, "guids": guids}, f)
            with open(os.path.join(tmp, "marks.jsonl"), "w") as f:
                f.write(json.dumps({"kind": "fight_start", "wall": "2026-01-01T00:00:00+00:00"}) + "\n")
            open(os.path.join(tmp, "samples.jsonl"), "w").close()
            with open(os.path.join(tmp, "raw-ta.jsonl"), "w") as f:
                for seq, src in ((1, EXTRA1), (2, EXTRA2), (3, HIRE)):
                    f.write(json.dumps({"seq": seq, "time": "2026-01-01T00:00:0%d+00:00" % seq,
                                        "kind": "spell_go", "source": {"name": "b", "guid": src},
                                        "target": {"name": "p", "guid": PLAYER}, "spell_id": 6065}) + "\n")
            rows = [r for r in events_mod.normalise(tmp, load(TWO_TANK[0])) if r.get("kind") == "cast"]
            self.assertEqual([r["src_unit"] for r in rows], ["$extra1", "$extra2", "$bot"])
            self.assertEqual([r["src_role"] for r in rows], ["extra", "extra", "bot"])


# ---- hire identity ----------------------------------------------------------

class HireIdentityTest(unittest.TestCase):
    def run_extras(self, agent, trial=None, led=None, tmp=None):
        trial = trial or two_tank_trial()
        ctx = tl.ctx_for(hire_guid=HIRE)
        led = led or tl.new_ledger(tmp)
        guard = gm_guard.GmGuard(agent, led, ctx)
        near = []
        with unittest.mock.patch.object(setup, "bring_hire", lambda a, n, **k: near.append(n)):
            out = setup.run_extras(agent, trial, guard, led, ctx, timeout=0.3)
        return out, ctx, led, near

    def test_extra_hire_identity_from_system_reply_and_party_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptAgent()
            (rows, names, guids), ctx, led, near = self.run_extras(a, tmp=tmp)
            self.assertEqual([r["id"] for r in rows],
                             ["extra_bots_present", "extra_bots_role", "extra_bots_in_group"])
            self.assertTrue(all(r["ok"] for r in rows), rows)
            self.assertEqual(names, ["Bulwark"])
            self.assertEqual(guids, [agent_mod.norm_guid(EXTRA1)])
            self.assertEqual(a.tbm_calls[0], "hire paladin tank level=40")
            self.assertIn("role Bulwark protection", a.tbm_calls)
            hires = [r for r in led.rows if r.get("kind") == "hire"]
            self.assertEqual([(h["name"], h["guid"], h["extra"]) for h in hires],
                             [("Bulwark", agent_mod.norm_guid(EXTRA1), 1)])
            self.assertEqual(ctx.extra_hire_guids, frozenset({agent_mod.norm_guid(EXTRA1)}))
            self.assertEqual(ctx.extra["extra_names"], ["Bulwark"])
            self.assertEqual(near, ["Bulwark"])

    def test_whisper_claiming_a_hire_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptAgent(events=[{"kind": "whisper", "text": "hired Bulwark for 100g"},
                                    {"kind": "channel_notify", "text": "hired Bulwark for 5g"}])
            a.hire_replies = False
            (rows, names, guids), ctx, led, _near = self.run_extras(a, tmp=tmp)
            self.assertFalse(rows[0]["ok"])
            self.assertEqual((names, guids), ([""], [""]))
            self.assertFalse([r for r in led.rows if r.get("kind") == "hire"])
            self.assertEqual(ctx.extra_hire_guids, frozenset())
            self.assertFalse(rows[1]["ok"])           # nothing to set a role on: not hired

    def test_named_but_not_a_party_member_is_no_hire(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptAgent()
            a._respond = lambda c: a._say("system", "You have hired Bulwark for 30 minutes.") \
                if c.startswith("hire paladin") else None
            (rows, names, _guids), ctx, _led, _near = self.run_extras(a, tmp=tmp)
            self.assertFalse(rows[0]["ok"])
            self.assertIn("not a party member", rows[0]["evidence"])
            self.assertEqual(ctx.extra_hire_guids, frozenset())
            # the named hire is still handed to the teardown, so its removal goes out
            self.assertEqual(ctx.extra["extra_names"], ["Bulwark"])

    def test_extra_with_the_main_bots_guid_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptAgent()
            a.HIRES = dict(ScriptAgent.HIRES, paladin=("Bulwark", HIRE))
            (rows, _names, guids), ctx, _led, _near = self.run_extras(a, tmp=tmp)
            self.assertFalse(rows[0]["ok"])
            self.assertEqual(guids, [""])
            self.assertEqual(ctx.extra_hire_guids, frozenset())

    def test_a_refused_role_makes_extra_bots_role_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptAgent()
            orig = a._respond

            def respond(command):
                if command.startswith("role Bulwark"):
                    a._say("system", "Bulwark does not use that spec.")
                else:
                    orig(command)
            a._respond = respond
            with unittest.mock.patch.object(setup, "_retry", lambda fn, attempts=2: fn()):
                (rows, _n, _g), _ctx, _led, _near = self.run_extras(a, tmp=tmp)
            self.assertTrue(rows[0]["ok"])
            self.assertFalse(rows[1]["ok"])

    def test_every_extra_must_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            trial = two_tank_trial()
            trial["extra_bots"].append({"class": "warrior", "spec": "protection", "role": "tank",
                                        "level": 40, "items": []})
            a = ScriptAgent()
            (rows, names, guids), ctx, led, _near = self.run_extras(a, trial=trial, tmp=tmp)
            self.assertEqual(names, ["Bulwark", "Wally"])
            self.assertEqual(guids, [agent_mod.norm_guid(EXTRA1), agent_mod.norm_guid(EXTRA2)])
            self.assertTrue(all(r["ok"] for r in rows), rows)
            self.assertEqual([h["extra"] for h in led.rows if h.get("kind") == "hire"], [1, 2])
            self.assertEqual(len(ctx.extra_hire_guids), 2)

    def test_not_in_group_fails_extra_bots_in_group(self):
        a = ScriptAgent()
        a.party_row = {"in_group": True, "members": [{"name": "Helper", "guid": HIRE}]}
        row = setup.check_extras_grouped(a, ["Bulwark"])
        self.assertFalse(row["ok"])
        self.assertIn("Bulwark", row["evidence"])


class RunSetupTest(unittest.TestCase):
    """run_setup end to end on a scripted agent: extras on, and the no-extras output unchanged."""

    def run_it(self, trial, tmp):
        a = ScriptAgent()
        led = tl.new_ledger(tmp)
        ctx = tl.ctx_for(hire_guid="", hire_name="")
        guard = gm_guard.GmGuard(a, led, ctx)
        with unittest.mock.patch.object(setup, "bring_hire", lambda *args, **k: True):
            doc = setup.run_setup(a, trial, guard, led, ctx)
        return doc, a, led, ctx

    def test_no_extra_bots_output_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc, a, led, ctx = self.run_it(minimal(load("priest-holy-pws-def-arms")), tmp)
            self.assertTrue(doc["ok"], doc)
            self.assertEqual([c["id"] for c in doc["checks"]],
                             ["gm_visible", "player_level", "player_forced_role", "bot_present", "bot_role",
                              "grouped"])
            self.assertEqual(sorted(doc["guids"]), ["bot", "marked", "mobs", "pet", "player"])
            self.assertNotIn("extra_names", doc)
            # live3 added player_offhand (the recorded off-hand) to setup.json
            self.assertEqual(set(doc), {"checks", "ok", "invalid_reason", "guids", "bot_name", "player_offhand"})
            hires = [r for r in led.rows if r.get("kind") == "hire"]
            self.assertEqual(len(hires), 1)
            self.assertNotIn("extra", hires[0])
            self.assertEqual(ctx.extra_hire_guids, frozenset())
            self.assertEqual([c for c in a.tbm_calls if c.startswith("hire")], ["hire priest healer level=40"])

    def test_extra_bots_add_three_rows_and_the_guid(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc, a, led, ctx = self.run_it(minimal(two_tank_trial()), tmp)
            self.assertTrue(doc["ok"], doc)
            ids = [c["id"] for c in doc["checks"]]
            self.assertEqual(ids, ["gm_visible", "player_level", "player_forced_role", "bot_present", "bot_role",
                                   "grouped", "extra_bots_present", "extra_bots_role", "extra_bots_in_group"])
            self.assertEqual(doc["guids"]["extras"], [agent_mod.norm_guid(EXTRA1)])
            self.assertEqual(doc["guids"]["bot"], agent_mod.norm_guid(HIRE))
            self.assertEqual(doc["extra_names"], ["Bulwark"])
            self.assertEqual([c for c in a.tbm_calls if c.startswith("hire")],
                             ["hire priest healer level=40", "hire paladin tank level=40"])
            self.assertEqual(ctx.hire_guid, agent_mod.norm_guid(HIRE))
            self.assertEqual(ctx.extra_hire_guids, frozenset({agent_mod.norm_guid(EXTRA1)}))
            main = [r for r in led.rows if r.get("kind") == "hire" and not r.get("extra")]
            self.assertEqual(len(main), 1)

    def test_a_failed_extra_makes_the_repeat_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            trial = minimal(two_tank_trial())
            a = ScriptAgent()
            led = tl.new_ledger(tmp)
            ctx = tl.ctx_for(hire_guid="", hire_name="")
            guard = gm_guard.GmGuard(a, led, ctx)
            orig = a._respond

            def respond(command):
                if command.startswith("hire paladin"):
                    a._say("system", "You cannot afford that hire.")      # a refusal
                else:
                    orig(command)
            a._respond = respond
            with unittest.mock.patch.object(setup, "bring_hire", lambda *args, **k: True), \
                 unittest.mock.patch.object(setup, "_retry", lambda fn, attempts=2: fn()):
                doc = setup.run_setup(a, trial, guard, led, ctx)
            self.assertFalse(doc["ok"])
            failed = {c["id"] for c in doc["checks"] if not c["ok"]}
            self.assertEqual(failed, {"extra_bots_present", "extra_bots_role", "extra_bots_in_group"})
            self.assertIn("refused", doc["invalid_reason"])

    def test_extras_are_not_hired_when_the_main_hire_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptAgent()
            a.hire_replies = False
            led = tl.new_ledger(tmp)
            ctx = tl.ctx_for(hire_guid="", hire_name="")
            guard = gm_guard.GmGuard(a, led, ctx)
            with unittest.mock.patch.object(setup, "bring_hire", lambda *args, **k: True), \
                 unittest.mock.patch.object(setup, "_retry", lambda fn, attempts=2: fn()):
                real = setup.hire_bot
                with unittest.mock.patch.object(
                        setup, "hire_bot",
                        lambda ag, tr, gd, lg, timeout=30.0, previous=None: real(ag, tr, gd, lg, timeout=0.2,
                                                                                   previous=previous)):
                    doc = setup.run_setup(a, minimal(two_tank_trial()), guard, led, ctx)
            self.assertFalse(doc["ok"])
            self.assertEqual([c for c in a.tbm_calls if c.startswith("hire")], ["hire priest healer level=40"])
            self.assertEqual(doc["guids"]["extras"], [])


# ---- slot alignment across repeats --------------------------------------------

class SlotAlignmentTest(unittest.TestCase):
    """extra1 fails, extra2 succeeds: the next repeat keeps each name at its own index."""

    def trial(self):
        trial = minimal(two_tank_trial())
        trial["extra_bots"].append({"class": "warrior", "spec": "protection", "role": "tank",
                                    "level": 40, "items": []})
        trial["repeats"] = 2
        return trial

    def fail_first_paladin(self, agent):
        orig = agent._respond
        seen = []

        def respond(command):
            if command.startswith("hire paladin"):
                seen.append(command)
                if len(seen) == 1:
                    agent._say("system", "You cannot afford that hire.")
                    return
            orig(command)
        agent._respond = respond

    def test_setup_slots_are_index_aligned_with_none_for_a_failed_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptAgent()
            self.fail_first_paladin(a)
            led = tl.new_ledger(tmp)
            ctx = tl.ctx_for(hire_guid="", hire_name="")
            guard = gm_guard.GmGuard(a, led, ctx)
            with unittest.mock.patch.object(setup, "bring_hire", lambda *args, **k: True), \
                 unittest.mock.patch.object(setup, "_retry", lambda fn, attempts=2: fn()):
                doc = setup.run_setup(a, self.trial(), guard, led, ctx)
            self.assertEqual(doc["extra_slots"], [None, "Wally"])
            self.assertEqual(doc["extra_names"], ["Wally"])
            self.assertEqual(doc["guids"]["extras"], ["", agent_mod.norm_guid(EXTRA2)])
            self.assertEqual([(h["name"], h["extra"]) for h in led.rows
                              if h.get("kind") == "hire" and h.get("extra")], [("Wally", 2)])

    def test_a_name_is_only_reused_at_its_own_index_and_a_none_slot_is_hired_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptAgent()
            led = tl.new_ledger(tmp)
            ctx = tl.ctx_for(hire_guid=HIRE)
            guard = gm_guard.GmGuard(a, led, ctx)
            a.party_row["members"].append({"name": "Wally", "guid": EXTRA2, "level": 40})
            with unittest.mock.patch.object(setup, "bring_hire", lambda *args, **k: True):
                rows, names, guids = setup.run_extras(a, self.trial(), guard, led, ctx,
                                                      previous=[None, "Wally"], timeout=0.3)
            self.assertEqual(names, ["Bulwark", "Wally"])
            self.assertEqual([c for c in a.tbm_calls if c.startswith("hire")], ["hire paladin tank level=40"])
            self.assertEqual([(h["name"], h["extra"]) for h in led.rows if h.get("kind") == "hire"],
                             [("Bulwark", 1)])
            self.assertEqual(ctx.extra["extra_slots"], ["Bulwark", "Wally"])
            self.assertTrue(all(r["ok"] for r in rows), rows)

    def test_worker_second_repeat_and_teardown_keep_the_original_indices(self):
        run = tl.run
        run.STOP.clear()
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptAgent()
            self.fail_first_paladin(a)
            r = run.Run(tl.run_args(tmp), "", tmp, [])
            r.tb_version = "test"
            r.args.repeats = 2
            row = {"slot": {"id": "1", "port": 0, "character": "Trlwarriora"}}
            session = run.SlotSession(row, os.path.join(tmp, "slot1"), "", "holder", tl.r_stub(),
                                      lambda line: None)
            session.agent = a
            session.ledger = tl.new_ledger(tmp)
            session.ensure_character = lambda klass: setattr(session, "klass", klass)
            previous_seen = []
            real_setup = setup.run_setup

            def spy(agent, trial, guard, ledger, ctx, first=True, previous=None, previous_extras=None):
                previous_seen.append(list(previous_extras or []))
                return real_setup(agent, trial, guard, ledger, ctx, first=first, previous=previous,
                                  previous_extras=previous_extras)

            class Quiet:
                def __init__(self, *args, **kwargs):
                    self.summoned = []
                    self.mob_source = "summon"

                def level_window(self):
                    return (39, 41)

                def run(self):
                    return {"ok": True, "ended": "duration"}

            with unittest.mock.patch.object(run, "run_setup", spy), \
                 unittest.mock.patch.object(setup, "bring_hire", lambda *args, **k: True), \
                 unittest.mock.patch.object(run.collect, "Collector", tl.StubCollector), \
                 unittest.mock.patch.object(run.collect, "normalise", lambda *args, **k: None), \
                 unittest.mock.patch.object(run.situations, "Situation", Quiet), \
                 unittest.mock.patch.object(run.Run, "pull_evpull", lambda *args, **k: None), \
                 unittest.mock.patch.object(run.teardown, "slot_teardown",
                                            lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
                r.worker(session, [self.trial()], r.errors)
            # repeat 1 carries nothing; repeat 2 is handed the ALIGNED list, not the compacted one
            self.assertEqual(previous_seen[0], [])
            self.assertEqual(previous_seen[1], [None, "Wally"])
            hires = [c for c in a.tbm_calls if c.startswith("hire")]
            self.assertEqual(hires, ["hire priest healer level=40", "hire paladin tank level=40",
                                     "hire warrior tank level=40", "hire paladin tank level=40"])
            ledger_hires = [(h["name"], h["extra"]) for h in session.ledger.rows
                            if h.get("kind") == "hire" and h.get("extra")]
            self.assertEqual(ledger_hires, [("Wally", 2), ("Bulwark", 1)])
            removed = [(h["name"], h.get("extra")) for h in session.ledger.rows
                       if h.get("kind") == "hire_removed"]
            self.assertEqual(removed, [("Helper", None), ("Bulwark", 1), ("Wally", 2)])
            self.assertEqual(session.last_extras, [])
            self.assertEqual(teardown.pending_extra_hires(session.ledger), [])

    def test_trial_cleanup_keeps_the_index_of_a_surviving_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptAgent()
            led = tl.new_ledger(tmp)
            rows = teardown.trial_cleanup(a, led, "Helper", timeout=0.3, extras=[None, "Wally"])
            self.assertEqual([r["id"] for r in rows],
                             ["forced_role_clear", "hire_removed", "hire_removed_extra2"])
            self.assertEqual([(r["name"], r.get("extra")) for r in led.rows
                              if r.get("kind") == "hire_removed"], [("Helper", None), ("Wally", 2)])


class RefusalTest(unittest.TestCase):
    def test_no_race_of_your_faction_is_a_refusal_not_a_timeout(self):
        self.assertTrue(setup.HIRE_REFUSAL.search("No race of your faction can be that class."))
        import time
        for hire in ("main", "extra"):
            with tempfile.TemporaryDirectory() as tmp:
                a = ScriptAgent()
                a._respond = lambda c: a._say("system", "No race of your faction can be that class.") \
                    if c.startswith("hire") else None
                led = tl.new_ledger(tmp)
                begin = time.time()
                if hire == "main":
                    row, name = setup.hire_bot(a, load("priest-holy-pws-def-arms"), None, led, timeout=20.0)
                else:
                    row, name = setup._hire(a, two_tank_trial()["extra_bots"][0], led, 20.0, None, extra=1)
                self.assertLess(time.time() - begin, 3.0)       # at once, not the 20 s timeout
                self.assertFalse(row["ok"])
                self.assertIsNone(name)
                self.assertTrue(row["evidence"].startswith("hire refused: No race of your faction"), row)
                self.assertFalse([x for x in led.rows if x.get("kind") == "hire"])


# ---- GM guard ---------------------------------------------------------------

class GuardTest(unittest.TestCase):
    def guard(self, tmp, extras=(EXTRA1,)):
        a = tl.FakeAgent(units=[
            {"guid": PLAYER, "health": 1000, "max_health": 1000},
            {"guid": HIRE, "health": 500, "max_health": 800, "max_power": 400},
            {"guid": EXTRA1, "health": 500, "max_health": 900, "max_power": 400},
            {"guid": STRANGER, "health": 500, "max_health": 700},
        ])
        ctx = tl.ctx_for(extra_hire_guids=frozenset(agent_mod.norm_guid(g) for g in extras))
        return a, gm_guard.GmGuard(a, tl.new_ledger(tmp), ctx)

    def test_guard_allows_an_extra_hire(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, g = self.guard(tmp)
            g.send("modify_hp", EXTRA1, 900, 900)
            g.send("unaura", EXTRA1, "all")
            g.send("modify_mana", EXTRA1, 200, 400)
            g.send("additem", EXTRA1, 8766, 1)
            self.assertEqual(a.gm, [".modify hp 900 900", ".unaura all", ".modify mana 200 400",
                                    ".additem 8766 1"])
            # selection is confirmed per guid exactly as today
            self.assertEqual([t for _line, t in a.gm_at], [agent_mod.norm_guid(EXTRA1)] * 4)

    def test_guard_still_allows_the_main_hire(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, g = self.guard(tmp)
            g.send("modify_hp", HIRE, 800, 800)
            self.assertEqual(a.gm, [".modify hp 800 800"])

    def test_guard_still_refuses_a_non_hire(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, g = self.guard(tmp)
            for kind, args in (("modify_hp", (700, 700)), ("unaura", ("all",)),
                               ("additem", (8766, 1)), ("modify_mana", (1, 400))):
                with self.assertRaises(gm_guard.GmRefused):
                    g.send(kind, STRANGER, *args)
            self.assertEqual(a.gm, [])
            self.assertTrue(g.refused)

    def test_extra_hire_gets_no_wider_set_than_a_hire(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, g = self.guard(tmp)
            for kind, args in (("damage", (5,)), ("learn", (12285,)), ("levelup", (1,)), ("die", ())):
                with self.assertRaises(gm_guard.GmRefused):
                    g.send(kind, EXTRA1, *args)
            self.assertEqual(a.gm, [])

    def test_without_extra_hires_the_extra_guid_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, g = self.guard(tmp, extras=())
            with self.assertRaises(gm_guard.GmRefused):
                g.send("modify_hp", EXTRA1, 900, 900)
            self.assertEqual(a.gm, [])

    def test_guard_context_default_has_no_extra_hires(self):
        self.assertEqual(gm_guard.GmContext().extra_hire_guids, frozenset())


# ---- teardown ---------------------------------------------------------------

class TeardownTest(unittest.TestCase):
    def test_trial_cleanup_removes_every_hire(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptAgent()
            led = tl.new_ledger(tmp)
            rows = teardown.trial_cleanup(a, led, "Helper", timeout=0.3, extras=["Bulwark", "Wally"])
            self.assertEqual(a.tbm_calls, ["role self clear", "remove Helper", "remove Bulwark", "remove Wally"])
            self.assertEqual([r["id"] for r in rows],
                             ["forced_role_clear", "hire_removed", "hire_removed_extra1", "hire_removed_extra2"])
            self.assertTrue(all(r["ok"] for r in rows), rows)
            removed = [(r["name"], r.get("extra")) for r in led.rows if r.get("kind") == "hire_removed"]
            self.assertEqual(removed, [("Helper", None), ("Bulwark", 1), ("Wally", 2)])

    def test_an_unanswered_extra_removal_is_a_failed_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = ScriptAgent()
            orig = a._respond
            a._respond = lambda c: None if c == "remove Bulwark" else orig(c)
            rows = teardown.trial_cleanup(a, tl.new_ledger(tmp), "Helper", timeout=0.3, extras=["Bulwark"])
            failed = [r["id"] for r in rows if not r["ok"]]
            self.assertEqual(failed, ["hire_removed_extra1"])

    def test_no_extras_output_is_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            a1, a2 = ScriptAgent(), ScriptAgent()
            l1, l2 = tl.new_ledger(os.path.join(tmp, "a")), tl.new_ledger(os.path.join(tmp, "b"))
            plain = teardown.trial_cleanup(a1, l1, "Helper", timeout=0.3)
            empty = teardown.trial_cleanup(a2, l2, "Helper", timeout=0.3, extras=[])
            self.assertEqual(plain, empty)
            self.assertEqual(a1.tbm_calls, ["role self clear", "remove Helper"])
            self.assertEqual(a1.tbm_calls, a2.tbm_calls)
            self.assertEqual([r["id"] for r in plain], ["forced_role_clear", "hire_removed"])
            self.assertEqual([{k: v for k, v in r.items() if k != "at"} for r in l1.rows],
                             [{k: v for k, v in r.items() if k != "at"} for r in l2.rows])

    def test_repeat_cleanup_heals_extras_and_is_unchanged_without(self):
        with tempfile.TemporaryDirectory() as tmp:
            units = [{"guid": PLAYER, "max_health": 1000, "health": 1000},
                     {"guid": HIRE, "name": "Helper", "max_health": 800, "health": 800},
                     {"guid": EXTRA1, "name": "Bulwark", "max_health": 900, "health": 100}]
            a = tl.FakeAgent(units=units)
            led = tl.new_ledger(tmp)
            ctx = tl.ctx_for(extra_hire_guids=frozenset({agent_mod.norm_guid(EXTRA1)}))
            g = gm_guard.GmGuard(a, led, ctx)
            rows = teardown.repeat_cleanup(a, g, led, "Helper", PLAYER, wait_s=0.1, extra_names=["Bulwark"])
            self.assertTrue(all(r["ok"] for r in rows), rows)
            self.assertIn(".modify hp 900 900", a.gm)
            a2 = tl.FakeAgent(units=units)
            g2 = gm_guard.GmGuard(a2, tl.new_ledger(os.path.join(tmp, "b")), ctx)
            teardown.repeat_cleanup(a2, g2, tl.new_ledger(os.path.join(tmp, "b")), "Helper", PLAYER, wait_s=0.1)
            self.assertNotIn(".modify hp 900 900", a2.gm)

    def test_pending_extra_hires_reads_the_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = tl.new_ledger(tmp)
            led.add({"kind": "hire", "name": "Helper", "guid": HIRE})
            led.add({"kind": "hire", "name": "Bulwark", "guid": EXTRA1, "extra": 1})
            led.add({"kind": "hire", "name": "Wally", "guid": EXTRA2, "extra": 2})
            led.add({"kind": "hire_removed", "name": "Wally", "extra": 2})
            self.assertEqual(teardown.pending_extra_hires(led), [("Bulwark", 1)])
            led.add({"kind": "hire", "name": "Wally", "guid": EXTRA2, "extra": 2})      # hired again
            self.assertEqual(teardown.pending_extra_hires(led), [("Bulwark", 1), ("Wally", 2)])


class CleanupReplayTest(unittest.TestCase):
    """`trials cleanup` replays the extra hires from the slot ledger."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        run_dir = os.path.join(self.tmp, "run")
        os.makedirs(run_dir)
        self.slot_dir = os.path.join(self.tmp, "slot1")
        os.makedirs(self.slot_dir)
        self.run_dir = run_dir
        slot_ledger_path = os.path.join(run_dir, "s-1.jsonl")
        self.slab = Ledger(slot_ledger_path)
        self.slab.add({"kind": "hire", "name": "Helper", "guid": HIRE})
        self.slab.add({"kind": "hire", "name": "Bulwark", "guid": EXTRA1, "extra": 1})
        ledger = Ledger(os.path.join(run_dir, "ledger.jsonl"))
        ledger.add({"kind": "lease", "server": "s", "slot": "1", "holder": "holder", "lease_id": "L1",
                    "dir": self.slot_dir, "ledger": slot_ledger_path, "account": "ACC",
                    "character": "Trlwarriora"})
        ledger.add({"kind": "session", "server": "s", "slot": "1", "account": "ACC",
                    "character": "Trlwarriora"})

    def cleanup(self, agent):
        class Session:
            def __init__(self, a):
                self.agent = a

            def stop_agent(self):
                pass

        rows = [{"server": "s", "slot": {"id": "1", "character": "Trlwarriora", "port": 0}, "state": "free"}]
        with unittest.mock.patch.object(lease, "list_slots", lambda b, s: (True, rows)), \
             unittest.mock.patch.object(lease, "acquire",
                                        lambda *a, **k: {"ok": True, "lease_id": "L9", "raw": ""}), \
             unittest.mock.patch.object(lease, "release", lambda *a: True), \
             unittest.mock.patch.object(teardown, "slot_teardown",
                                        lambda d, binary=None: {"ok": True, "rc": 0, "detail": ""}):
            return teardown.cleanup(self.run_dir, binary="/bin/true", log=lambda line: None,
                                    login_factory=lambda rec, led: Session(agent))

    def test_cleanup_replays_extra_hires_once(self):
        a = ScriptAgent()
        self.assertEqual(self.cleanup(a), 0)
        self.assertEqual([c for c in a.tbm_calls if c.startswith("remove")], ["remove Bulwark"])
        self.assertEqual([(r["name"], r["extra"]) for r in Ledger(self.slab.path).rows
                          if r.get("kind") == "hire_removed"], [("Bulwark", 1)])
        a2 = ScriptAgent()
        self.assertEqual(self.cleanup(a2), 0)               # idempotent: nothing left to remove
        self.assertEqual([c for c in a2.tbm_calls if c.startswith("remove")], [])

    def test_cleanup_is_nonzero_while_an_extra_hire_stays(self):
        a = ScriptAgent()
        a.wait_reply = lambda *args, **kw: (None, [])
        self.assertEqual(self.cleanup(a), 1)
        leftovers = [r for r in Ledger(self.slab.path).rows if r.get("kind") == "leftover"]
        self.assertEqual([r["check"] for r in leftovers], ["hire_removed_extra1"])
        # still pending, so a later cleanup tries again
        self.assertEqual(teardown.pending_extra_hires(Ledger(self.slab.path)), [("Bulwark", 1)])

    def test_cleanup_without_extra_hires_sends_no_remove(self):
        self.slab.rows[:] = [r for r in self.slab.rows if not r.get("extra")]
        with open(self.slab.path, "w") as f:
            for r in self.slab.rows:
                f.write(json.dumps(r) + "\n")
        a = ScriptAgent()
        self.assertEqual(self.cleanup(a), 0)
        self.assertEqual([c for c in a.tbm_calls if c.startswith("remove")], [])


if __name__ == "__main__":
    unittest.main()
