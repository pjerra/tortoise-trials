"""Regression tests for the engine bugs of the first live run (live1, 2026-11-08).

Every fixture in tests/fixtures-live1/ is a trimmed slice of the real files of the first
live run's three run dirs (stamps 20261108-031927-before = priest-holy-pws-def-arms-2tank,
20261108-032354-before = priest-holy-pws-def-arms solo, 20261108-032513-before =
priest-holy-pws-battle-control, kept under the run output folder): sample rows, event
rows, raw-ta system lines, setup.json config replies. The doubles replay those lines
through the real Agent / Collector / Governor / setup code; no server is touched.
"""

import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trl import report, rules, scenario  # noqa: E402
from trl.live import agent as agent_mod  # noqa: E402
from trl.live import collect, gm_guard, setup, situations  # noqa: E402
from trl.live.ledger import Ledger  # noqa: E402

FIX = ROOT / "tests" / "fixtures-live1"
PLAYER = "0x000000000000064c"
BOT = "0x000000000000064e"
EXTRA = "0x000000000000064f"


def jsonl(name):
    with open(FIX / name) as f:
        return [json.loads(line) for line in f if line.strip()]


def jsonf(name):
    with open(FIX / name) as f:
        return json.load(f)


class LogAgent(agent_mod.Agent):
    """An agent with a real event log: `events` honours since/kinds and the head probe.

    Replies are pushed by on_tbm / on_gm / on_chat hooks, so a test decides WHEN a reply
    lands relative to the command (the live bugs were all about that ordering).
    """

    def __init__(self, status=None, units=None, unit_rows=None):
        agent_mod.Agent.__init__(self, "/nonexistent-slot")
        self.log = []
        self.status_row = status or {"state": "in_world", "guid": PLAYER, "level": 40, "health": 1189,
                                     "max_health": 1189, "power": 0, "max_power": 1000, "target": ""}
        self.unit_list = list(units or [])
        self.unit_rows = dict(unit_rows or {})
        self.gm = []
        self.tbm_calls = []
        self.chats = []
        self.on_tbm = lambda cmd: None
        self.on_gm = lambda text: None
        self.on_chat = lambda params: None

    def _unit(self, params):
        guid = params.get("guid")
        name = params.get("name")
        if guid in self.unit_rows:
            return dict(self.unit_rows[guid])
        for u in self.unit_list:
            if guid and agent_mod.norm_guid(u.get("guid")) == agent_mod.norm_guid(guid):
                return dict(u)
            if name and (u.get("name") or "").lower() == name.lower():
                return dict(u)
        return {"_err": "no unit"}

    def push(self, text, kind="system", raw=None):
        row = {"seq": len(self.log) + 1, "kind": kind, "text": text}
        if raw is not None:
            row["data"] = {"raw": raw, "type": "system"}
        self.log.append(row)

    def rpc(self, method, params=None, timeout=None):
        params = params or {}
        if method == "events":
            since = int(params.get("since", 0))
            last = len(self.log)
            if since > last:
                return {"events": [], "next": last, "oldest": 1, "dropped": False, "more": False}
            kinds = params.get("kinds")
            rows = [r for r in self.log if r["seq"] > since and (not kinds or r["kind"] in kinds)]
            if not rows:
                time.sleep(0.02)
            return {"events": rows, "next": last, "oldest": 1, "dropped": False, "more": False}
        if method == "status":
            return dict(self.status_row)
        if method == "units":
            return [dict(u) for u in self.unit_list]
        if method == "unit":
            out = self._unit(params)
            if params.get("guid") and agent_mod.norm_guid(params.get("guid")) == agent_mod.norm_guid(PLAYER):
                # the player's own object always exists and shows the selection (the fake server
                # applies it at once); the guard fails closed without it
                if not isinstance(out, dict) or "_err" in out:
                    out = {"guid": PLAYER}
                out["target"] = self.status_row.get("target") or ""
            return out
        if method == "target":
            self.status_row["target"] = params.get("guid")
            return {"guid": params.get("guid")}
        if method == "tbm":
            self.tbm_calls.append(params["command"])
            since = len(self.log)
            self.on_tbm(params["command"])
            return {"since": since}
        if method == "gm":
            self.gm.append(params["text"])
            self.on_gm(params["text"])
            return {}
        if method == "chat":
            self.chats.append(params)
            self.on_chat(params)
            return {}
        return {"_err": "unknown method: %s" % method}


def unit_from_sample(row):
    """A `units`/`unit` rpc row rebuilt from a real samples.jsonl row."""
    return {"guid": row["guid"], "health": row["hp"], "max_health": row["max_hp"], "power": row["power"],
            "max_power": row["max_power"], "auras": [{"spell_id": a} for a in row["auras"]],
            "in_combat": row["in_combat"], "target": row["target"], "x": row["x"], "y": row["y"],
            "z": row["z"], "casting": row["casting"], "hostile": row["unit"].startswith("mob:"),
            "level": 41, "dead": False}


def ctx_for(**over):
    base = dict(player_guid=PLAYER, band_low=40, floor_pct=30, hire_name="Lingrun", hire_guid=BOT,
                arena_coords=frozenset(), mob_entries=frozenset(), learn_ids=frozenset(),
                aura_ids=frozenset(), item_ids=frozenset(), trial_id="t")
    base.update(over)
    return gm_guard.GmContext(**base)


class SamplerTest(unittest.TestCase):
    """Bugs 1 and 3: the player's row came from `units`, which never lists the player."""

    def _collector(self, tmp, own_row, hooks=()):
        rows = jsonf("sample_rows_t20_76_21_76.json")
        at = [r for r in rows if abs(r["t"] - 20.76) < 0.05]
        others = [unit_from_sample(r) for r in at if r["unit"] != "player"]
        player_row = [r for r in rows if r["unit"] == "player" and abs(r["t"] - 21.76) < 0.05][0]
        status = {"state": "in_world", "guid": PLAYER, "level": 40, "health": player_row["hp"],
                  "max_health": player_row["max_hp"], "power": player_row["power"],
                  "max_power": player_row["max_power"], "target": "",
                  "x": player_row["x"], "y": player_row["y"], "z": player_row["z"]}
        agent = LogAgent(status=status, units=others, unit_rows={PLAYER: own_row} if own_row else {})
        marks = collect.Marks(os.path.join(tmp, "marks.jsonl"))
        marks.start()
        col = collect.Collector(agent, tmp, marks, {"player": PLAYER, "bot": BOT, "extras": [EXTRA],
                                                    "mobs": []}, None, {})
        for hook in hooks:
            col.tick_hooks.append(hook)
        col.tick_hooks.append(lambda sample: col._stop.set())
        return agent, col

    def test_player_auras_and_combat_come_from_the_unit_rpc(self):
        own = {"guid": PLAYER, "health": 1189, "max_health": 1189, "auras": [{"spell_id": 71}],
               "in_combat": True, "level": 40}
        with tempfile.TemporaryDirectory() as tmp:
            agent, col = self._collector(tmp, own)
            col._sample()
            rows = [json.loads(line) for line in open(os.path.join(tmp, "samples.jsonl"))]
            player = [r for r in rows if r["unit"] == "player"][0]
            # live1: every one of the 207 player rows had auras [] and in_combat false
            self.assertEqual(player["auras"], [71])
            self.assertTrue(player["in_combat"])
            self.assertEqual(col.last_sample["player"]["auras"], [71])
            self.assertTrue(col.last_sample["player"]["in_combat"])

    def test_governor_damages_a_player_in_combat_at_full_health(self):
        own = {"guid": PLAYER, "health": 1189, "max_health": 1189, "auras": [{"spell_id": 71}],
               "in_combat": True, "level": 40}
        with tempfile.TemporaryDirectory() as tmp:
            holder = {}

            def hook(sample):
                holder["gov"].tick(sample)
            agent, col = self._collector(tmp, own, hooks=[hook])
            guard = gm_guard.GmGuard(agent, Ledger(os.path.join(tmp, "ledger.jsonl")), ctx_for())
            holder["gov"] = situations.Governor(agent, guard, collect.Marks(os.path.join(tmp, "m2.jsonl")),
                                                victim="player", band=(40, 90), floor=30,
                                                victim_guid=PLAYER)
            col._sample()
            # full health, in combat, above the 90 % top: one .damage to the 65 % middle (1189 -> 773)
            self.assertEqual([g for g in agent.gm if g.startswith(".damage")], [".damage 416"])

    def test_an_unreadable_own_row_falls_back_to_the_targeted_hostile(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, col = self._collector(tmp, None)
            mob = [u for u in agent.unit_list if u["hostile"]][0]
            agent.status_row["target"] = mob["guid"]
            col._sample()
            self.assertTrue(col.last_sample["player"]["in_combat"])


class ScoringChainTest(unittest.TestCase):
    """The real rep: opportunity was 0 s because the player's in_combat was always false."""

    def test_opportunity_needs_the_player_in_combat(self):
        trial = json.load(open(ROOT / "trials" / "healers" / "priest-holy-pws-def-arms-2tank.json"))
        samples = jsonl("samples_2tank_player_fight.jsonl")
        ctx = {"guids": {"player": PLAYER, "bot": BOT, "extras": [EXTRA]},
               "thresholds": {"critical": 20.0, "low": 50.0, "medium": 70.0}, "trial": trial}
        self.assertTrue(samples and not any(s["in_combat"] for s in samples))
        self.assertEqual(rules.opportunity_seconds(trial, samples, [], ctx), 0.0)
        # what the fixed sampler writes while the player fights: in_combat true from fight_start
        fixed = [dict(s, in_combat=s["t"] >= 8.4) for s in samples]
        self.assertGreater(rules.opportunity_seconds(trial, fixed, [], ctx), 0.0)


class GovernorRescueTest(unittest.TestCase):
    """Bug 1b: the solo trial ended 'floor' after 16 s: three rescues in three seconds."""

    def _run(self, ticks, **gov_kw):
        data = jsonf("solo_player_hp_and_floor_rescues.json")
        series = data["series"]
        max_hp = series[0][2]
        # the real fall rate of the solo rep: about 70 hp a second with no healer landing
        fall = (series[3][1] - series[16][1]) / (series[16][0] - series[3][0])
        self.assertGreater(fall, 50)
        with tempfile.TemporaryDirectory() as tmp:
            agent = LogAgent(status={"state": "in_world", "guid": PLAYER, "health": 400,
                                     "max_health": max_hp, "target": PLAYER},
                             unit_rows={PLAYER: {"guid": PLAYER, "health": 400, "max_health": max_hp}})

            def on_gm(text):
                if text.startswith(".modify hp"):
                    agent.status_row["health"] = int(text.split()[2])
            agent.on_gm = on_gm
            clock = {"t": 0.0}
            marks = collect.Marks(os.path.join(tmp, "marks.jsonl"))
            guard = gm_guard.GmGuard(agent, Ledger(os.path.join(tmp, "l.jsonl")), ctx_for())
            gov = situations.Governor(agent, guard, marks, victim="player", band=(40, 90), floor=30,
                                      victim_guid=PLAYER, now=lambda: clock["t"], **gov_kw)
            lines = []
            for _ in range(ticks):
                clock["t"] += 1.0
                hp = agent.status_row["health"] - int(fall)
                agent.status_row["health"] = max(1, hp)
                if gov.tick({"hp": agent.status_row["health"], "max_hp": max_hp, "in_combat": True}):
                    break
                lines.append(agent.status_row["health"])
            return gov, agent

    def test_three_seconds_of_rescues_no_longer_end_the_repeat(self):
        gov, agent = self._run(12)
        self.assertEqual(gov.ended, "")
        heals = [g for g in agent.gm if g.startswith(".modify hp")]
        self.assertLessEqual(len(heals), 3)
        # healed to the band middle (65 % of 1189), not to the 30 % floor
        self.assertTrue(all(h == ".modify hp 772 1189" for h in heals), heals)

    def test_a_runaway_still_ends_as_floor(self):
        gov, _agent = self._run(60)
        self.assertEqual(gov.ended, "floor")
        self.assertEqual(len(gov.rescues), situations.MAX_RESCUES_PER_MIN)

    def test_the_trial_can_raise_the_cap(self):
        gov, _agent = self._run(60, max_rescues=30)
        self.assertEqual(gov.ended, "")

    def test_cap_is_validated_in_the_trial_file(self):
        with open(ROOT / "trials" / "healers" / "priest-holy-pws-def-arms.json") as f:
            trial = json.load(f)
        self.assertEqual(scenario.validate(trial), [])
        trial["situation"]["max_rescues_per_min"] = 12
        self.assertEqual(scenario.validate(trial), [])
        trial["situation"]["max_rescues_per_min"] = 0
        self.assertTrue(any("max_rescues_per_min" in e for e in scenario.validate(trial)))


class ConfigReadTest(unittest.TestCase):
    """Bug 2: `.bot features all <key>` lists only keys SET in aiplayerbot.conf."""

    def _agent(self, replies):
        agent = LogAgent()

        def on_tbm(cmd):
            for line in replies(cmd):
                agent.push(line)
        agent.on_tbm = on_tbm
        return agent

    def test_unset_keys_read_as_the_core_defaults(self):
        raw = jsonf("config_raw_unset.json")
        groups = {}
        key = None
        for line in raw:
            if line.startswith("Config: 0 keys match"):
                key = line.split("'")[1]
            if key:
                groups.setdefault(key, []).append(line)
        self.assertEqual(sorted(groups), ["criticalhealth", "lowhealth", "mediumhealth"])
        agent = self._agent(lambda cmd: groups[cmd.split()[-1]])
        cfg = setup.read_config(agent, timeout=1.0)
        self.assertTrue(cfg["ok"], cfg)
        self.assertEqual((cfg["critical"], cfg["low"], cfg["medium"]), (20, 50, 70))
        self.assertEqual(set(cfg["source"].values()), {"default"})
        row = setup.config_row(cfg)
        self.assertTrue(row["ok"])
        self.assertIn("core default", row["evidence"])

    def test_a_listed_key_wins_over_the_default(self):
        def replies(cmd):
            key = cmd.split()[-1]
            if key == "criticalhealth":
                return ["Config: 1 keys match 'criticalhealth'.", "TBM:CONFIG_BEGIN|1",
                        "TBM:CONFIG|CriticalHealth|25", "TBM:CONFIG_END|1|1"]
            return ["Config: 0 keys match '%s'." % key, "TBM:CONFIG_BEGIN|0", "TBM:CONFIG_END|0|0"]
        cfg = setup.read_config(self._agent(replies), timeout=1.0)
        self.assertEqual((cfg["critical"], cfg["low"], cfg["medium"]), (25, 50, 70))
        self.assertEqual(cfg["source"]["critical"], "file")

    def test_no_answer_stays_unreadable(self):
        cfg = setup.read_config(self._agent(lambda cmd: []), timeout=0.3)
        self.assertFalse(cfg["ok"])
        self.assertIsNone(cfg["critical"])
        self.assertFalse(setup.config_row(cfg)["ok"])

    def test_a_capped_listing_without_the_key_is_not_a_default(self):
        def replies(cmd):
            return ["TBM:CONFIG_BEGIN|300", "TBM:CONFIG|Other|1", "TBM:CONFIG_END|200|300"]
        self.assertFalse(setup.read_config(self._agent(replies), timeout=0.5)["ok"])


def inv_listings():
    """(lookup line, listing without the nectar, listing with it): real raw-ta system lines."""
    rows = jsonl("solo_rep1_inv_and_lookup.jsonl")
    lookup = [r for r in rows if r["seq"] == 54][0]
    first = [r for r in rows if 55 <= r["seq"] <= 85]
    second = [r for r in rows if 87 <= r["seq"] <= 118]
    return lookup, first, second


class ItemsTest(unittest.TestCase):
    """Bug 4: the `.additem` lands a beat after the `.bot inv` that follows it."""

    NAME = "Lingrun"

    def _world(self, carried=False, never=False, lag=1, have=None, partial=None):
        """Sweet Nectar carried: 0 (first listing), 20 (second) or `have`; `.additem N` adds N
        after `lag` listings. partial(read_no, state) -> True makes that read arrive without its
        INV_END (a timed-out read)."""
        lookup, first, second = inv_listings()
        self.assertNotIn("|1708|", " ".join(r["text"] for r in first))
        self.assertIn("|1708|", " ".join(r["text"] for r in second))
        agent = LogAgent(unit_rows={BOT: {"guid": BOT, "name": self.NAME}},
                         units=[{"guid": BOT, "name": self.NAME, "health": 1, "max_health": 1}])
        state = {"count": 20 if carried else (have or 0), "adds": [], "reads": 0, "pending": []}

        def listing():
            if not state["count"]:
                return [r["text"] for r in first]
            return [t.replace("|1708|20|", "|1708|%d|" % state["count"])
                    for t in (r["text"] for r in second)]

        def on_gm(text):
            if text.startswith(".lookup item"):
                agent.push(lookup["text"], raw=lookup["data"]["raw"])
            elif text.startswith(".additem"):
                n = int(text.split()[-1])
                state["adds"].append(n)
                if not never:
                    state["pending"].append([lag, n])

        def on_tbm(cmd):
            if cmd.startswith("inv "):
                state["reads"] += 1
                lines = listing()
                if partial and partial(state["reads"], state):
                    lines = [t for t in lines if "INV_END" not in t]
                for t in lines:
                    agent.push(t)
                # an add shows up `lag` listings AFTER the one that follows it
                for pend in state["pending"]:
                    pend[0] -= 1
                    if pend[0] <= 0 and pend[1]:
                        state["count"] += pend[1]
                        pend[1] = 0
        agent.on_gm = on_gm
        agent.on_tbm = on_tbm
        return agent, state

    def _check(self, agent, tmp, timeout=1.0):
        guard = gm_guard.GmGuard(agent, Ledger(os.path.join(tmp, "l.jsonl")), ctx_for())
        trial = {"bot": {"items": [{"name": "Sweet Nectar", "count": 20}]}}
        return setup.check_items(agent, trial, guard, guard.ctx, None, self.NAME, timeout=timeout,
                                 sleep=lambda s: None)

    def test_a_late_additem_is_waited_for_and_sent_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, state = self._world()
            row = self._check(agent, tmp)
            self.assertTrue(row["ok"], row)
            self.assertEqual(state["adds"], [20])
            self.assertIn("added 1x", row["evidence"])

    def test_a_slower_additem_still_lands_within_the_back_off(self):
        # two listings come back without the stack (live1 battle-control rep1: both attempts failed)
        with tempfile.TemporaryDirectory() as tmp:
            agent, state = self._world(lag=2)
            row = self._check(agent, tmp)
            self.assertTrue(row["ok"], row)
            self.assertEqual(state["adds"], [20])
            self.assertGreaterEqual(state["reads"], 4)

    def test_a_timed_out_first_read_grants_nothing_and_is_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, state = self._world(partial=lambda n, st: True)
            row = self._check(agent, tmp, timeout=0.2)
            self.assertFalse(row["ok"])
            self.assertIn("inventory unreadable", row["evidence"])
            self.assertEqual(state["adds"], [])
            self.assertEqual([g for g in agent.gm if g.startswith(".additem")], [])

    def test_partial_reads_after_a_grant_do_not_grant_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            # read 1 is complete (proves the deficit); every read after the grant is cut off
            agent, state = self._world(partial=lambda n, st: n > 1)
            row = self._check(agent, tmp, timeout=0.2)
            self.assertFalse(row["ok"])
            self.assertIn("inventory unreadable", row["evidence"])
            self.assertEqual(state["adds"], [20])

    def test_have_5_of_20_grants_exactly_15(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, state = self._world(have=5)
            row = self._check(agent, tmp)
            self.assertTrue(row["ok"], row)
            self.assertEqual(state["adds"], [15])
            self.assertEqual(state["count"], 20)

    def test_a_read_without_its_begin_line_is_not_a_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _state = self._world()
            real = agent.on_tbm

            def no_begin(cmd):
                before = len(agent.log)
                real(cmd)
                agent.log[:] = [r for i, r in enumerate(agent.log)
                                if i < before or "INV_BEGIN" not in r["text"]]
            agent.on_tbm = no_begin
            row = self._check(agent, tmp, timeout=0.2)
            self.assertFalse(row["ok"])
            self.assertEqual([g for g in agent.gm if g.startswith(".additem")], [])

    def test_a_reused_hire_that_still_carries_the_stack_gets_nothing_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, state = self._world(carried=True)
            row = self._check(agent, tmp)
            self.assertTrue(row["ok"], row)
            self.assertEqual([g for g in agent.gm if g.startswith(".additem")], [])

    def test_a_stack_that_never_lands_fails_after_a_re_add(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, state = self._world(never=True)
            row = self._check(agent, tmp)
            self.assertFalse(row["ok"])
            # two grants at most, each the measured shortfall, then a plain failure
            self.assertEqual(state["adds"], [20, 20])
            self.assertIn("Sweet Nectar", row["evidence"])

    def test_inventory_entries_reads_only_the_named_span(self):
        lines = ["TBM:INV_BEGIN|Tira|0", "TBM:INV_ITEM|255|23|4599|20|-", "TBM:INV_END|Tira|44|58",
                 "Hello there!", "TBM:INV_BEGIN|Lingrun|0", "TBM:INV_EQ|0|9431|50|50",
                 "TBM:INV_ITEM|255|24|1708|20|-", "TBM:INV_END|Lingrun|46|58"]
        self.assertEqual(setup.inventory_entries(lines, "Lingrun"), {9431: 1, 1708: 20})
        self.assertEqual(setup.inventory_entries(lines, "Tira"), {4599: 20})
        # no name: the whole window, as before
        self.assertEqual(setup.inventory_entries(lines), {4599: 20, 9431: 1, 1708: 20})


class StrategyTest(unittest.TestCase):
    """Bug 4b: extra_bots_role failed because the `co ?` read started at the stale cursor 0."""

    def _agent(self, reply_line):
        line = jsonf("strategies_line.json")["holy_priest_line"]
        agent = LogAgent()
        agent.push("Tira co ?")
        agent.push(line)                       # the first hire's reply, long before
        agent.push("TBM:INV_END|Tira|44|58")
        agent.push("Bot Steelwind role set to tank.")

        def on_chat(params):
            if params.get("text", "").endswith("co ?"):
                # the reply lands later, interleaved with an inventory line
                def late():
                    agent.push("TBM:INV_ITEM|255|37|1708|20|-")
                    agent.push(reply_line)
                threading.Timer(0.3, late).start()
        agent.on_chat = on_chat
        return agent

    def test_snapshot_is_the_log_head_not_the_stale_cursor(self):
        agent = LogAgent()
        for i in range(5):
            agent.push("line %d" % i)
        self.assertEqual(agent.cursor, 0)
        self.assertEqual(agent.snapshot(), 5)

    def test_the_hires_own_strategies_line_counts(self):
        agent = self._agent("Combat Strategies: aoe, protection, tank, threat")
        row = setup._check_strategy(agent, "protection", "Steelwind", timeout=3.0)
        self.assertTrue(row["ok"], row)
        self.assertIn("protection", row["evidence"])

    def test_an_older_bots_line_does_not_stand_in(self):
        agent = self._agent("Combat Strategies: aoe, holy, threat")
        row = setup._check_strategy(agent, "protection", "Steelwind", timeout=1.0)
        self.assertFalse(row["ok"])
        self.assertIn("wanted strategy", row["evidence"])


class SpellIdsTest(unittest.TestCase):
    """Bug 6 (verified, not a bug): the rule's Power Word: Shield ids and the cast rows agree."""

    def test_every_real_shield_cast_carries_an_id_the_rule_matches(self):
        trial = json.load(open(ROOT / "trials" / "healers" / "priest-holy-pws-def-arms-2tank.json"))
        rule = trial["rules"][0]
        casts = jsonl("events_2tank_pws_casts.jsonl")
        self.assertEqual(len(casts), 3)
        ids = rules._spell_ids(rule["check"]["where"])
        self.assertIn(6066, ids)                      # rank 6, the level-40 priest's shield
        for rank in (17, 592, 600, 3747, 6065, 6066, 10898, 10899, 10900, 10901):
            self.assertIn(rank, ids)
        self.assertTrue(all(c["spell_id"] in ids for c in casts))
        samples = jsonl("samples_2tank_bot_near_pws.jsonl") + jsonl("samples_2tank_player_fight.jsonl")
        ctx = {"guids": {"player": PLAYER, "bot": BOT, "extras": [EXTRA]},
               "thresholds": {"critical": 20.0, "low": 50.0, "medium": 70.0}, "trial": trial}
        out = rules.evaluate(rule, casts, samples, [], ctx)
        # none went to the warrior: the shields were Tira's own and Steelwind's, all rank 6
        self.assertEqual(out["value"], 0)
        self.assertEqual(out["evidence"][0].split(",")[1].strip(), "3 same-spell casts on other targets")
        self.assertEqual({c["dst"] for c in casts}, {"Tira", "Steelwind"})


class CardRepeatsTest(unittest.TestCase):
    """Bug 5: the card printed the --repeats flag (0), not the repeats that ran."""

    def test_repeats_that_ran_are_counted(self):
        run = jsonf("RUN_2tank.json")
        self.assertEqual(run["args"]["repeats"], 0)
        with tempfile.TemporaryDirectory() as tmp:
            tid = "priest-holy-pws-def-arms-2tank"
            for rep in ("rep1", "rep2"):
                os.makedirs(os.path.join(tmp, tid, rep))
            self.assertEqual(report._repeats_text(run, tmp, [{"trial": tid}]), "2")
            with open(os.path.join(tmp, "RUN.json"), "w") as f:
                json.dump(run, f)
            report.write_card(tmp, [{"trial": tid, "verdicts": []}])
            card = open(os.path.join(tmp, "card.md")).read()
            self.assertIn("repeats 2.", card)
            self.assertNotIn("repeats 0", card)

    def test_the_flag_is_shown_when_nothing_ran_yet(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(report._repeats_text({"args": {"repeats": 3}}, tmp, []), "3")


if __name__ == "__main__":
    unittest.main()
