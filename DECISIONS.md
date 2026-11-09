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
| `dps-mage-cc-respect` | 3 of 4 ran 180 s (13, 7, 3 rescues); rep 4 capped (16) | the ladder is off for `cc_target`; a live check at -1, refill off still capped at 64 s (12 rescues, three mobs on a gearless tank) | `level_delta` -1, count 3, refill ON, `max_rescues_per_min` 30 (refill off leaves the sheep the only target once both unmarked mobs die, and the mage then nukes it by design; the runner also excludes the span with no unmarked mob alive, `cc_gap`) |
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
Now: `metric: rate`, `per: mob_alive`, `min_alive_s: 45`, `pass_if >= 8`. The denominator is the seconds in which at
least one `mob:` sample had hp > 0 (each second once). Threshold: an unvetoed fire mage casts a nuke every 3.5 s,
about 15 a minute; 8 is a little over half of that. A back-off that holds the veto half the time or more (the wand
fills in, #922) gives 7 or fewer, an idle mage about 0. Under 45 s of mob-alive time the rule is INCONCLUSIVE (a nuke every 3.5 s gives 13 casts in 45 s, enough to tell 8 a minute from 0; a 20 s mob gives 5 and says nothing).
sweep-1b's step-3 reps would read about 5-6 a minute (2 nukes in 20 s with Frost Nova, Blink and Cone of Cold cast
in between: the mob was on the mage), i.e. FAIL for a mage that is under attack, which is information, not an artefact.

## 2026-11-09: `player.gear: "tier"` for the tank and DPS trials

Problem: the player of a tank or DPS trial is a GM-levelled character with no items. Its threat is far below a
real tank's, so the DPS bots sit above 100 percent of it nearly all the time and the threat back-off dominates
every DPS trial (the rogue Kick before/after on test3 showed it).

Built: a trial option `player.gear: "tier"`. The bench had no player-gear path (the bot is geared with
`.bot autogear`, which does not apply to the player; the guard's `.additem` was hire-only), so the smallest one was
added: a data file `data/gear_kits.json` and a new guarded GM kind `additem_self`.
- Kit choice (per class, role and level; warrior only, levels 40, 45 and 50 as the trials use): the plate piece of
  highest armor per slot (cloak for the back), required level within 8 below the kit level, quality uncommon or
  rare, vanilla entries only (below 25000), no random suffix, no reputation/honor/profession/spell requirement, no
  PvP or placeholder names. Weapons: the highest-dps axe, mace or sword (the skills a warrior starts with) with no
  proc or on-use effect. Tank kit: one-handed weapon and the highest-armor shield. DPS kit: a two-handed weapon,
  no shield. No item has intellect or spirit (the first cloak pick was a healer's). Armor totals: tank 3814 / 4697 / 5543, dps 2527 / 2971 / 3640 at levels 40 / 45 / 50.
- Guard: `additem_self` takes only the kit's entries, once each, only on the player itself. It is reachable only
  through `send("additem_self", ...)`; a raw `.additem` line still classifies as the hire kind.
- Verification: the `equipment` read must show every kit item; the evidence is the count and the kit's armor sum
  (no stats read exists in the agent, so there is no live armor figure).

Where it is on:
- `dps-*` (four trials: the player is the forced tank in Defensive Stance): the tank kit. This is the case that
  matters: a geared tank holds threat, so the back-off the trials measure is not the whole fight.
- `tank-*` (two trials: the player is a forced-DPS warrior in Battle Stance; the bot is the tank): the DPS kit, no
  shield. Reason: the bot's tanking is the question; a naked player adds almost no threat or damage and dies
  quickly, which fed the rescue cap. With a two-handed weapon and plate it contributes damage like a real
  player, and having no off-hand item keeps `player_no_shield` and the role read honest.
Not measured: how much the gear changes the DPS bots' threat share or the rescue counts (no live run was made for
this change); the sweep numbers in the earlier entries were taken without gear.

### Gear carry-over (follow-up, same day)

The slot character is kept between trials, so a gear trial left its kit on the next healer trial's player
(`player_no_shield` failed on the seven `priest-holy-pws-*` trials, and the other baselines moved). The agent has no
unequip or destroy call, but the core's `.additem <id> <negative count>` takes items away, equipped ones too
(`DestroyItemCount`, Commands.cpp HandleAddItemCommand). Chosen: a trial WITHOUT `player.gear` strips first
(`player_gear_strip`, recorded in `setup.json`): every entry of any kit that is worn or in the bags is removed
with `.additem <id> -1` through the same guarded self-only kind (kit entries only, one at a time), then the
`equipment` and `inventory` reads must show no kit entry left. The strip set is the union of all kits, not
"whatever is worn": a character's own starting gear stays (counted in the evidence), because widening the guard to
any item would open a destroy command the closed set should not have, and the healer trials only need the off-hand
and plate gone. A trial WITH gear equips from the bags first and grants only what is neither worn nor bagged, so
a repeat never duplicates a kit.

### Kit proficiencies and kit carry-over between gear trials

A GM-levelled warrior never learns Plate Mail (750), so 8 of 11 plate pieces of a tier kit stayed in the bags and
every rep was INVALID; editing 750 into three trial files hid it for those trials only. Chosen: the gear step learns
the proficiencies the kit needs (`trl/gear.py` PROFICIENCY: plate 750, mail 8737, leather 9077, shield 9116, weapon
skills by item subclass; cloth none) for the player itself, only those it does not know, before the equip. The kit
data now records `item_class` and `subclass` (build_data.py); an entry without them falls back to its slot. The
guard's `.learn` accepts this fixed table beside the trial's own spells, still self-only. A level-40 kit left on the
slot character failed a level-50 trial: a gear trial now strips every kit item first when the player carries one
that is not in its own kit (`player_gear_carryover`, the same guarded `.additem <id> -1`; a different level means
different items, so the leftover itself is the trigger). The equip waits for the grant to show in the bags, the
read-back waits for the equip to show, and an unworn piece is equipped again (twice): the likely cause of the piece
that stayed off at random. Not found offline: why rep 1 of the level-40 control was always INVALID.
