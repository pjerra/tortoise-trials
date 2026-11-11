
## From the 2026-11-11 bench round (P3, not in this round)
- apply the taunt chance fix (mobs count 3, 240 s, min_reps 3) to `tank-paladin-taunt-off-healer` after this round's live check.
- add: a druid tank kit for the bear-bal pair.
- add: the same chance fix for `druid-resto-heal-latency`.
- add: a sample-based "mob target" uptime rule (a direct read of who holds aggro).

## From the #1553 between-pulls runs (2026-11-08)
- ooc_hurt: count rules include casts before the window opens (t=0.2/2.1 while fight_start is ~t=9-10 s). Rules should start at the window (fight_start mark), like tank_damage does.
- Cast events carry a stale `dst_hp_pct` (99-100 % while the sample says 35 %); the cast rows should take hp from the nearest sample.
- The runner needs a turtle-agent binary on PATH ("no turtle-agent binary" otherwise); take the binary from RUN.json/args instead (now also TRIALS_BINARY).
- The old trials' why_expect text says "the runner re-casts the stance": only ooc_hurt has keep_stance(); tank_damage has none.
- bot-warrior control (after3): every rep failed the extra_bots_no_shield setup check because the hire had an off-hand item (865); the check rejects any off-hand item, it should reject shields only.

## From the rough-edges job (2026-11-08)
- Extra reps only run for a trial that lost a rep to the rescue cap (a setup failure or an error repeats the same
  way); with `repeats` 1 there is nothing to make up (planned - 1 = 0).
- A rep cut by the cap is still scored INVALID only through `min_valid_s`; a cap end with enough scored seconds
  scores normally, but the ladder and the extra-rep count treat every cap end as not valid.
- `evpull`/`split_botev` pair windows with rep numbers by position; a rep without a window (setup failure) shifts the pairing. Extra reps make this more likely.

## From the first live run of adaptive difficulty, free slot and --parallel 2 (2026-11-08)

## Done 2026-11-08 (reasoning in DECISIONS.md)
- `trials run` now scores the run and writes card.md/card.json.
- Teardown names its failing steps; an earlier repeat's despawned summon no longer blocks the per-repeat kill.
  Still open, server/binary side: which game-side step of `turtle-agent teardown` fails is now logged but its
  cause is not known (see DECISIONS.md "Teardown rc=14").
- `paladin-holy-heal-latency` got a lighter situation (not yet run live).
- `level_delta: 0` is a real 0; the four trial files that write it have no mobs.
- The def-arms 2tank trial took `max_rescues_per_min: 12`; the test stays strict.
- The login pacer writes one debug line per login (`TRIALS_DEBUG=1` prints it).
