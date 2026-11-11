"""The scorer reads a mob's victim from its attack packets, not stale 1 Hz samples.

Covers the war-r3 shape from the AFTER2 taunt trial: the mob's attack_start switches victim
from the player to the healer after the last 1 Hz sample, so the old sample-target read counts
a correct taunt off the healer as a taunt off the player. Packets decide; the sample is only
the fallback when no packet exists in the last 5 s.

Run from the tool directory: python3 -I -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from trl import events as events_mod  # noqa: E402
from trl import rules as rules_mod  # noqa: E402
from trl import score as score_mod  # noqa: E402

PLAYER = "0x0000000000000001"
BOT = "0x0000000000000002"
HEALER = "0x0000000000000003"
MOB = "0xf13000000000abcd"
GUIDS = {"player": PLAYER, "bot": BOT, "extras": [HEALER], "mobs": [MOB]}
TAUNT = 355


def sample(t, guid, target):
    return {"t": float(t), "wall": None, "unit": "mob", "guid": guid, "hp": 600,
            "max_hp": 1000, "power": 0, "max_power": 100, "auras": [],
            "casting": None, "in_combat": True, "target": target,
            "x": 0.0, "y": 0.0, "z": 0.0}


def cast(t, seq):
    return {"t": float(t), "wall": None, "kind": "cast", "src": "bot", "src_guid": BOT,
            "dst": "mob", "dst_guid": MOB, "src_unit": "$bot", "dst_unit": "mob:" + MOB,
            "spell_id": TAUNT, "spell": "Taunt", "amount": None, "dst_hp_pct": 96.0,
            "src_role": "bot", "dst_role": "mob", "origin": "ta", "seq": seq}


def raw(kind, seq, src, dst, t, base="2026-01-01T00:00:%02d+00:00"):
    wall = None
    if t is not None:
        wall = base % int(t) if "%02d" in base else t
    return {"seq": seq, "time": wall, "kind": kind,
            "source": {"name": "m", "guid": src}, "target": {"name": "u", "guid": dst}}


def ctx():
    return {"guids": GUIDS, "thresholds": {}, "trial": {},
            "rows": [], "samples_all": [], "marks": []}


def where(dst_target):
    return {"kind": "cast", "src": "$bot", "dst_target_of": dst_target}

def war_r3():
    """The AFTER2 war-r3 shape: cast seq 18982, packet 18981 (mob->healer) after the sample."""
    samples = [sample(t, MOB, PLAYER) for t in (152.0, 153.0)]
    victims = {MOB.lower(): [(18981, 153.1, HEALER.lower())]}
    rows = [cast(153.38, 18982)]
    return rows, samples, victims


class PacketVictimTest(unittest.TestCase):
    def test_war_r3_packet_overrules_stale_sample(self):
        # the mob's victim by packet is the healer: the cast is NOT off the player.
        rows, samples, victims = war_r3()
        by_guid = rules_mod._sample_rows(samples)
        c = dict(ctx(), victims_by_guid=victims)
        # quoting the failing line on the old code: the sample still says target=player.
        self.assertEqual(samples[-1]["target"], PLAYER)
        self.assertFalse(rules_mod._matches(rows[0], where("$player"), by_guid, c, {}))
        self.assertTrue(rules_mod._matches(rows[0], where("$extra1"), by_guid, c, {}))

    def test_mob_on_player_by_packet_with_stale_healer_sample(self):
        samples = [sample(t, MOB, HEALER) for t in (12.0, 13.0)]
        victims = {MOB.lower(): [(41, 13.1, PLAYER.lower())]}
        row = cast(13.38, 42)
        by_guid = rules_mod._sample_rows(samples)
        c = dict(ctx(), victims_by_guid=victims)
        self.assertTrue(rules_mod._matches(row, where("$player"), by_guid, c, {}))
        self.assertFalse(rules_mod._matches(row, where("$extra1"), by_guid, c, {}))

    def test_no_packet_falls_back_to_sample(self):
        samples = [sample(t, MOB, PLAYER) for t in (12.0, 13.0)]
        row = cast(13.38, 42)
        by_guid = rules_mod._sample_rows(samples)
        self.assertTrue(rules_mod._matches(row, where("$player"), by_guid, dict(ctx()), {}))
        self.assertFalse(rules_mod._matches(row, where("$extra1"), by_guid, dict(ctx()), {}))

    def test_attack_stop_means_no_victim(self):
        samples = [sample(t, MOB, PLAYER) for t in (12.0, 13.0)]
        victims = {MOB.lower(): [(40, 13.1, None)]}
        row = cast(13.38, 42)
        by_guid = rules_mod._sample_rows(samples)
        c = dict(ctx(), victims_by_guid=victims)
        self.assertFalse(rules_mod._matches(row, where("$player"), by_guid, c, {}))

    def test_packet_older_than_5s_falls_back_to_sample(self):
        samples = [sample(t, MOB, PLAYER) for t in (12.0, 13.0)]
        victims = {MOB.lower(): [(40, 7.0, HEALER.lower())]}
        row = cast(13.38, 42)
        by_guid = rules_mod._sample_rows(samples)
        c = dict(ctx(), victims_by_guid=victims)
        self.assertTrue(rules_mod._matches(row, where("$player"), by_guid, c, {}))

    def test_same_t_packet_orders_by_file_order(self):
        # no seqs: the later file row at the same t wins.
        victims = {MOB.lower(): [(None, 13.0, PLAYER.lower()), (None, 13.0, HEALER.lower())]}
        row = dict(cast(13.38, None))
        row.pop("seq")
        by_guid = rules_mod._sample_rows([sample(13.0, MOB, PLAYER)])
        c = dict(ctx(), victims_by_guid=victims)
        self.assertTrue(rules_mod._matches(row, where("$extra1"), by_guid, c, {}))

    def test_seq_orders_packets_not_file_or_time(self):
        # the higher-seq packet wins even when filed first with a later t.
        victims = {MOB.lower(): [(43, 13.4, HEALER.lower()), (42, 13.5, PLAYER.lower())]}
        row = cast(13.6, 44)
        by_guid = rules_mod._sample_rows([sample(13.0, MOB, HEALER)])
        c = dict(ctx(), victims_by_guid=victims)
        self.assertTrue(rules_mod._matches(row, where("$player"), by_guid, c, {}))

    def test_events_carry_seq_and_victim_table_rounds_like_events(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "setup.json"), "w") as f:
            json.dump({"ok": True, "guids": GUIDS}, f)
        with open(os.path.join(tmp, "marks.jsonl"), "w") as f:
            f.write(json.dumps({"kind": "setup_ok", "wall": "2026-01-01T00:00:00+00:00"}) + "\n")
        with open(os.path.join(tmp, "samples.jsonl"), "w") as f:
            f.write(json.dumps(sample(10.0, MOB, PLAYER)) + "\n")
        with open(os.path.join(tmp, "raw-ta.jsonl"), "w") as f:
            for row in (raw("attack_start", 41, MOB, HEALER, 10),
                        {"seq": 42, "time": "2026-01-01T00:00:11+00:00", "kind": "spell_go",
                         "source": {"name": "b", "guid": BOT},
                         "target": {"name": "m", "guid": MOB}, "spell_id": TAUNT}):
                f.write(json.dumps(row) + "\n")
        rows = events_mod.normalise(tmp, {"id": "t"})
        casts = [r for r in rows if r.get("kind") == "cast"]
        self.assertEqual(len(casts), 1)
        self.assertEqual(casts[0]["seq"], 42)
        raws = [json.loads(line) for line in open(os.path.join(tmp, "raw-ta.jsonl")) if line.strip()]
        marks = [json.loads(line) for line in open(os.path.join(tmp, "marks.jsonl")) if line.strip()]
        samples = [json.loads(line) for line in open(os.path.join(tmp, "samples.jsonl")) if line.strip()]
        table = events_mod.attack_victims(raws, marks, samples)
        self.assertEqual(table[MOB.lower()], [(41, 10.0, HEALER.lower())])

    def test_score_rep_builds_victims_from_raw(self):
        # end to end through score_rep on the real no-taunt rule: the war-r3 shape scores 0,
        # and without raw-ta.jsonl the same rep falls back to the sample and scores 1.
        from trl import scenario as scenario_mod
        trial = scenario_mod.load_trial(
            os.path.join(ROOT, "trials", "tanks", "tank-warrior-no-taunt-tank-player.json"))
        rows, samples, _ = war_r3()
        # the packets and the cast share one ISO clock: marks carry walls, and _time_base
        # reads fight_start from them, so t=153.1 lands 0.1 s after the 153.0 sample.
        marks = [{"kind": "fight_start", "wall": "2026-01-01T00:00:00+00:00"},
                 {"kind": "fight_end", "wall": "2026-01-01T00:03:20+00:00", "ended": "duration"}]
        for with_raw in (True, False):
            tmp = tempfile.mkdtemp()
            rep = os.path.join(tmp, "run", "tank-warrior-no-taunt-tank-player", "rep1")
            os.makedirs(rep)
            with open(os.path.join(rep, "setup.json"), "w") as f:
                json.dump({"ok": True, "guids": GUIDS,
                           "config": {"critical": 20, "low": 50, "medium": 70}}, f)
            for name, data in (("samples.jsonl", samples), ("events.jsonl", rows),
                               ("marks.jsonl", marks)):
                with open(os.path.join(rep, name), "w") as f:
                    for row in data:
                        f.write(json.dumps(row) + "\n")
            if with_raw:
                base = "2026-01-01T00:02:%02d+00:00"
                with open(os.path.join(rep, "raw-ta.jsonl"), "w") as f:
                    for row in (raw("attack_start", 18980, MOB, PLAYER, 33, base),
                                raw("attack_start", 18981, MOB, HEALER, 33, base)):
                        f.write(json.dumps(row) + "\n")
            out = score_mod.score_rep(rep, trial)
            got = [r for r in out["rules"] if r["id"] == "TNK-WAR-NO-TAUNT"][0]
            self.assertEqual(got["value"], 0 if with_raw else 1)
            self.assertEqual(got["verdict"], "PASS" if with_raw else "FAIL")


if __name__ == "__main__":
    unittest.main()
