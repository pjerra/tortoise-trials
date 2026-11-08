# Decisions

Short, dated notes on choices that are not obvious from the code.

## 2026-11-08: explicit `level_delta: 0` is 0 (was +1)

`mobs.level_delta` is read by `trl/live/ladder.base_delta` (the live situation and the difficulty ladder
both start from it). It used `int(x or 1)`, so a written 0 ran one level harder than written. Now only an
absent (or null) value defaults to +1; an explicit 0 is 0 and the ladder steps down from 0.

The FOLLOWUPS note counted 7 trial files that write 0. Four are live trial files; the other three were
`.bak` copies in the old working copy. Per file:

| file | kind | mobs.count | decision |
|---|---|---|---|
| `trials/healers/paladin-blessing-ooc.json` | ooc_buffs | 0 | 0 was meant: no mob is summoned, so the value is a placeholder (validation wants an int). Kept as 0; no behaviour change. |
| `trials/healers/priest-holy-fort-ooc.json` | ooc_buffs | 0 | same |
| `trials/healers/priest-holy-pws-battle-ooc-control.json` | ooc_hurt | 0 | same |
| `trials/healers/priest-holy-pws-def-arms-ooc.json` | ooc_hurt | 0 | same |

None of the four reaches `level_window` (no summon, no mobs-ready check, the ladder is off for these kinds),
so the change alters no existing trial. No trial file was changed. Every other trial writes 1, -2 or leaves
the value as the validator-required int.

## 2026-11-08: `paladin-holy-heal-latency` gets a lighter situation

Evidence (four reps of one live run, step 0 to 3 of the difficulty ladder): every rep ended on the rescue
cap ("floor after 6-7 rescues", 77-113 s scored of the 120 s `min_valid_s`), so the rule never reached
`min_n` 5 and every verdict was INVALID. The cap is 6 rescues per rolling minute; the fight dealt about as
much damage as the paladin healed (about 120 hp/s of heals against a 1189 hp warrior), so the victim
kept falling under the 30 percent floor. The priest twin on the identical situation scores because a
priest out-heals it. The ladder could not help: its floor is "player level - 1", and step 3 (one mob,
no refill, level -1) still capped.

Changed (`situation` only; rules, player, bot and duration are as before):

| field | was | now | why |
|---|---|---|---|
| `mobs.level_delta` | 1 | -4 | mobs of level 35-37 against a level 40 player: weaker hits and a better miss chance; the mob table has at least 3 beasts fully inside that window. The ladder keeps -4 at every step (its floor is `min(written, -1)`), so a cap rep no longer retries at a harder level. |
| `floor_pct` | 30 | 12 | the rescue heals the victim only below 12 percent, the value the def-arms trials already use; far fewer rescues per minute, and the falls through 50 percent (the rule's trigger) still happen. |
| `hp_band` | [40, 90] | [30, 90] | the governor tops the player up to the middle of the band after a rescue; a 10-point lower edge leaves room for more heal races inside one fight. Floor 12 stays below the band's low edge as validation demands. |
| `max_rescues_per_min` | (default 6) | 12 | the same cap the other capped trials carry. |

Count stays 1 and refill stays on (a dead mob must be replaced or the fight ends before `min_valid_s`).
These numbers are chosen from the offline data of the failed run; they have not been run live.
