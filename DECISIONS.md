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

## 2026-11-08: `priest-holy-pws-def-arms-2tank` takes the solo trial's `max_rescues_per_min: 12`

`TwoTankTrialFilesTest` pins the 2tank trials as "the solo trial plus a protection paladin", and the
def-arms 2tank file lacked the solo's `max_rescues_per_min: 12` (a known failing test). Chosen: change the
trial, keep the test strict. The cap only decides when a rep is cut as "floor after N rescues"; the 2tank
trial has the same fight, so the same cap belongs on it, and a second tank only lowers the damage the victim
takes. A run of the bear-form 2tank trial, which also has no cap, lost a rep to the default cap of 6.

Effect on old numbers: the PR #1557 BEFORE/AFTER runs used the trial without the cap and stay valid as
they are; only future runs of this trial use 12, so a future run is compared with those baselines at a
looser cap (a rep that would have been cut at 6 rescues may now finish and score). `priest-holy-pws-bear-bal`
and its 2tank twin carry no cap on either side and stay as they are; they still match each other.

## 2026-11-08: teardown rc=14 on priest runs

What the tool did wrong (fixed): `turtle-agent teardown` prints one line per step and a summary
("teardown: 5 ok, 9 skipped, 2 failed"); the runner kept only the last line, so the log never said which
steps failed. `slot_teardown` now adds every `FAIL`/`ending` step with its detail to the detail line, the
`slot_teardown` ledger row (`failed_steps`) and the run warning.

What the tool did wrong, second part (fixed): the per-repeat cleanup checked every summon in the slot ledger.
From the second repeat on, an earlier repeat's summon had despawned, and the agent answers "unit ... is not
in view" for it; that was read as UNKNOWN for the whole ledger, so `mobs_dead` failed (exit code 5), the
kill loop never ran and the repeat's own summon stayed alive. That is the "one summon left behind" (it
showed up as the only guid left in the run-level cleanup). The cleanup now checks only summons no earlier
cleanup has proved dead or gone, and treats "not in view" as gone for them (the player stands in the arena).

Not fixed (cause not visible offline): the two failed steps of the binary's own teardown. In the binary's
logic, a failed game-side step (an RPC step, or the agent exiting) makes the lease step fail too ("kept:
game-side teardown failed"), which fits "2 failed" with one real cause. Which game-side step it is will
now be in the log of the next run. If it turns out to be server-side or in turtle-agent, it belongs in
that project, not here.

## 2026-11-09: base difficulty of the tank/DPS trials (sweep-1b)

Evidence: `20261108-232904-sweep1b`, 4-6 reps per trial, `--parallel 2`, the ladder stepping a trial down after
each rescue-cap rep. A rep that ends on the cap with 12-21 rescues in a minute wastes itself; the ladder then needs
1-3 reps to reach a step that survives. What each trial needed:

| trial | step 0 (+1, refill on) | what survived | base now |
|---|---|---|---|
| `dps-mage-counterspell-caster` | capped (13 rescues, 125 s) | steps 1-2 capped too; only step 3 (-1, count 1, refill off) ran 180 s, 3 of 3 | `level_delta` -1, refill on, `max_rescues_per_min` 30 |
| `dps-mage-threat-cap` | capped (21 rescues) | steps 1-2 capped; step 3 ran 180 s, but its mob died after 2-3 nukes | `level_delta` -1, refill on, `max_rescues_per_min` 30 |
| `dps-mage-cc-respect` | 3 of 4 ran 180 s (13, 7, 3 rescues); rep 4 capped (16) | the ladder is off for `cc_target`; a live check at -1, refill off still capped at 64 s (12 rescues, three mobs on a gearless tank) | `level_delta` -1, refill off, `max_rescues_per_min` 30 |
| `dps-rogue-kick-caster` | 4 of 4 ran 180 s, 0 rescues | - | unchanged |
| `tank-paladin-righteous-fury` | 2 of 3 clean, rep 3 capped (14) | step 1 (0): 2 of 2 ran 180 s | `level_delta` 0 |
| `tank-warrior-taunt-off-healer` | no fight in sweep-1b (arena) | same shape as the paladin trial | `level_delta` 0 (not measured) |

Why the counterspell and threat trials keep refill on and get a higher cap instead of the ladder's step 3: step 3 turns the refill
off, and then the threat trial's mob dies at 20 s, the rule `DPS-MAG-THREAT-NO-AGGRO` gets 18-24 s of opportunity
(it needs 90) and the counterspell rule sees at most one cast (it needs 5). The capped reps are capped by the
bench's own sustain (a gearless forced tank under a caster), and the question the trials ask is about the mage, so the
cap is raised to 30 rescues a minute instead of making the fight empty. Hit rates after this change are not
measured; the `level_delta` values come from the steps that survived above.

## 2026-11-09: `DPS-MAG-THREAT-DAMAGE` is a rate per mob-alive minute

Was: at least 15 nukes per repeat. At the ladder's easier steps one mob dies after 2-3 nukes and the rest of the
repeat is empty (sweep-1b step 3: 2, 2, 3 nukes), so the rule could never pass and said nothing about the bot.
Now: `metric: rate`, `per: mob_alive`, `min_alive_s: 15`, `pass_if >= 8`. The denominator is the seconds in which at
least one `mob:` sample had hp > 0 (each second once). Threshold: an unvetoed fire mage casts a nuke every 3.5 s,
about 15 a minute; 8 is a little over half of that. A back-off that holds the veto half the time or more (the wand
fills in, #922) gives 7 or fewer, an idle mage about 0. Under 15 s of mob-alive time the rule is INCONCLUSIVE.
sweep-1b's step-3 reps would read about 5-6 a minute (2 nukes in 20 s with Frost Nova, Blink and Cone of Cold cast
in between: the mob was on the mage), i.e. FAIL for a mage that is under attack, which is information, not an artefact.
