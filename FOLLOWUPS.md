
## From the #1553 between-pulls runs (2026-11-08)
- ooc_hurt: count rules include casts before the window opens (t=0.2/2.1 while fight_start is ~t=9-10 s). Rules should start at the window (fight_start mark), like tank_damage does.
- Cast events carry a stale `dst_hp_pct` (99-100 % while the sample says 35 %); the cast rows should take hp from the nearest sample.
- `trials.py run` did not write card.md/card.json for these runs; `trials.py score <run-dir>` was needed.
- The runner needs a turtle-agent binary on PATH ("no turtle-agent binary" otherwise); take the binary from RUN.json/args instead (now also TRIALS_BINARY).
- Stale comments in tests/test_ooc_hurt.py ("< min_opportunity_s 60", "not 20").
- Known failure: TwoTankTrialFilesTest (the 2tank trial lacks max_rescues_per_min 12 that the solo trial has).
- The old trials' why_expect text says "the runner re-casts the stance": only ooc_hurt has keep_stance(); tank_damage has none.
- bot-warrior control (after3): every rep failed the extra_bots_no_shield setup check because the hire had an off-hand item (865); the check rejects any off-hand item, it should reject shields only.

## From the rough-edges job (2026-11-08)
- `mobs.level_delta: 0` has always meant +1 (`int(x or 1)` in situations.level_window); 7 trial files write 0.
  The ladder reads it the same way (step 0 = what really ran). Decide whether 0 should become a real 0.
- Extra reps only run for a trial that lost a rep to the rescue cap (a setup failure or an error repeats the same
  way); with `repeats` 1 there is nothing to make up (planned - 1 = 0).
- A rep cut by the cap is still scored INVALID only through `min_valid_s`; a cap end with enough scored seconds
  scores normally, but the ladder and the extra-rep count treat every cap end as not valid.
- `evpull`/`split_botev` pair windows with rep numbers by position; a rep without a window (setup failure) shifts the pairing. Extra reps make this more likely.

## From the first live run of adaptive difficulty, free slot and --parallel 2 (2026-11-08)
- `trials run` writes no card.md/card.json; `trials.py score <run-dir>` is needed afterwards.
- Teardown ends rc=14 on the priest runs (2-3 failed steps, one summon left behind); the log does not say which steps failed. Log the failing step names.
- `paladin-holy-heal-latency` hits the rescue cap on every rep even at difficulty step 3, so it never scores: give it a lighter situation (fewer or weaker mobs, wider hp band).
- The login pacer logs nothing when it does not wait, so shared use across parallel workers is invisible in the log: log one line per login at debug level.
