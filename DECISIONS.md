# Decisions

Short, dated notes on choices that are not obvious from the code.

## 2026-11-13: pooled stun interrupts, meal-safe ooc window, recorded attack state, no add direction

Bench round 1113, from `after-t1113/OPEN-3.md`.

**`TNK-DRU-BASH` and `TNK-PAL-HOJ` carry `pool: true`.** Both stuns have a 60 s cooldown and the
rules a 60 s responder debounce, so a 180 s repeat offers 2-5 triggers; per-repeat `min_n 3` threw
every repeat away (the cards read 0/0 INCONCLUSIVE while the per-repeat verdicts were Bash 2/2, 2/2,
1/1, 1/1 and HoJ 1/2, 1/1, 0/1, 0/1). Rescored from the frozen snapshots (copies in
`after-t1113/rescore-1113/`, the originals are untouched): **Bash PASS 6/6 = 1.0**, **HoJ FAIL 2/5 =
0.4**. HoJ's misses are the first cast right after a cooldown expiry, hidden from the card by the 10 s
debounce; 0.4 is the honest number and the bot side (`InterruptCastingPolicy.h` `stunImmunity`,
`PlayerbotAISpells.cpp`) has not been looked at yet.

**The ooc window never `.unaura all` over a Drink/Food aura.** `.unaura all` over a live meal is what
left the low-mana priest asleep in it (33-35 s of nothing after the setup drink was stripped in
reps 1 and 4). The window now waits (bounded, `MEAL_WAIT_S` 30 s) for the meal aura to end with the
hire's auras untouched. A meal still up after the wait does not open the window at all: its Drink
keeps restoring mana, so the repeat is INVALID (`meal still active`, no `fight_start`). Because a
meal ending refills mana, a trial that sets `bot.mana_pct` gets that level put back (`.modify mana`,
`ooc_mana_reset`) so the window still tests a low-mana bot. That reset must land: an unreadable max
mana or a refused line makes the repeat INVALID (`mana reset failed`, no `fight_start`), because the
hire may still sit far above the trial's level and the window would score the wrong bot. An
unreadable aura list is UNKNOWN, not "no meal": it is retried inside the same wait with the hire's
auras untouched, and a list that never becomes readable makes the repeat INVALID too (`meal state
unknown`, no `fight_start`, no mana reset). A strip on a guess is the bug this removes, and so is
opening the window on a meal nobody saw. The old strip-and-wait-for-a-re-drink flow
(`ooc_drink_settled`, `DRINK_SETTLE_S`) is gone.

**The attack state at every Drink is recorded, and the newest event decides.** `TNK-PAL-NO-DRINK-
UNDER-ATTACK` was INCONCLUSIVE for one repeat (paladin add-pickup rep3, Drink at 155.9) because no
sample fell inside 1 s and the mob's last packet was an `attack_stop`. A sample up to 5 s old now
decides when no newer event exists — every target switch emits an `attack_start` packet, so with
none the last read target is current — and an `attack_stop` newer than the last sample decides
"not attacking" instead of handing the decision back to that older sample (round 1: the stop used to
be discarded). Each Drink's state goes into the verdict evidence
(`0xf130…d6c -> $bot (sample t=154.2)`). Rescored 0/4 PASS at `== 0`.

**The add-pickup refill add cannot be made to open on the player or the healer, and was left alone.**
The bench has no facility for it: the GM guard is a closed template set with no threat/aggro line
(`docs/next-trials.md` F2 `aggro_on` is still an unbuilt feature), the sample `target` is read-only,
the player attacking the add (`Situation.attack`, already sent for every refill) does not decide the
mob's target, and the one existing lever — the player's stance threat cast (`THREAT_CAST`, taunted
on the shared selection by `threat()` every 6 s, the same line `engage_marked` uses for Taunt) — is
no control at all here: it is a Battle Stance player, so the learned spell is Mocking Blow, which in
the AFTER run failed 18/29 times on `unit_not_infront` and once on `no_power`, and the adds still
took the tank (160 of 165 mob hits). A real fix needs a bench feature (a GM threat line, or a
spawn-time forced target), not a trial edit; the trigger count stays ~0.5 per repeat.

Suite delta against main 8bc775f, run per module (`test_cc_target`, `test_live1` and `test_orphans`
never end and are killed at the 120 s cap), same runner under `nice` and
`flock /tmp/trials-tests.lock`: branch
`Ran 847 tests` over the modules that finish, main `Ran 846`; the one extra test is
`tests/test_addpickup_pooled.py` (18 vs 17, the replaced 1 s-unknown case became two). The
`FAIL:`/`ERROR:` name lists are identical on both sides — `test_cc_gap` 1, `test_live3` 1,
`test_ooc_hurt` 4 failures + 4 errors, `test_rules_healers` 1, `test_live`
`Ran 81 tests ... FAILED (failures=1, errors=1, skipped=1)` (`test_refill_uses_the_trials_mob_kind`
errors, `test_an_rpc_error_skips_the_refill_tick` fails; both are an arena stub these tests no
longer provide, and both spend the 600 s arena give-up first), and `test_cc_target`,
`test_live1`, `test_orphans` killed at the 120 s cap on both sides. No new failing test name.

## 2026-11-12: no `dps-rogue-kick-caster@ignore-threat` variant (bench round 1112, brief item 5)

Skipped with the reason written down, as the brief allows. The bench has no per-trial server-key
path: the GM guard (`trl/live/gm_guard.py` TEMPLATES) is a closed template set with no config line,
`config_read` only reads the three health thresholds, and a variant override (`trl/scenario.py`
OVERRIDE_FIELDS) can only touch level/player/bot/extra_bots/situation/rules — the `@` variants set
trial fields (gear, mana, extra bots), never server keys. `InterruptIgnoresThreat` is a
server-global key needing a `.reload` after the change, so a per-trial variant cannot flip it for
one trial without moving every other trial in the run. Until the bench grows a server-keys knob,
the Kick row keeps measuring the shipped default (FAIL by design) and the AFTER stays manual.

## 2026-11-12: paladin add-pickup scores pooled; a drink-under-attack guard

`TNK-PAL-ADD-PICKUP` now carries `pool: true`. WHY.md's artefact part: 0-5 triggers per repeat with
`min_n 3` applied per repeat meant one repeat decided the verdict, and the card that PASSed (t1106r,
3/3) did so because the guard dropped the two repeats with the misses (rep1 1/3 raw, 2 misses). A
pooled rule takes ONE verdict from `sum k / sum n` over the valid repeats, applies `min_n` to the
pooled n, and `score_rep` skips the per-repeat guard for it. The bench already displayed that pooled
k/n on the card (`report.POOLED_METRICS`), so `pool` reuses the same number instead of adding a
second aggregation: no new metric, no change to `report._agg_value`. A repetition with no triggers
stays INCONCLUSIVE on its own and adds nothing to the pooled n; the repeats with triggers still
supply the denominator. Re-scored against the run dirs (copies, originals untouched; each
`trial.effective.json` got `pool: true` and the new rule):

| image | pooled answered | verdict |
|---|---|---|
| t1106u-before | 2/2 | INCONCLUSIVE (pooled n 2 < min_n 3) |
| t1106r | 3/5 | FAIL |
| t1106u-taunt | 6/6 | PASS |
| t1106u-taunt2 guards4 | 4/8 | FAIL |
| t1106u-taunt2 8rep | 10/14 | PASS |
| t1112-priest | 4/9 | FAIL |

These are WHY.md's table counts exactly. t1106u-before reads INCONCLUSIVE rather than the old pass:
2 triggers is under the pooled 3-trigger minimum, which is the brief's rule, not a regression.

`TNK-PAL-NO-DRINK-UNDER-ATTACK` counts `Drink` casts by the bot while a mob is on the player or the
healer (`check.where.party_under_attack: ["$player", "$extras"]`). The attacker's victim comes from
its last `attack_start` packet (the `src_target_of` rule) with the mob's sample `target` as the
fallback; only `mob:` units are attackers, and the bot itself is deliberately not in the list, so a
tank may drink while holding the mob. The state is tri-state: a mob that was live at the cast (a
sample inside the last 5 s with hp left, or an attack packet) but has neither a deciding packet nor
a fresh sample (1 s) saying who it is on leaves the state unknown, and the rule is INCONCLUSIVE for
that repeat rather than passing it — missing attack evidence is not "not under attack". It is pooled
too, so one repeat with a cast fails the trial; and an INCONCLUSIVE repeat with reason
`attack state unknown` is not a benign no-trigger repeat, so it makes the pooled verdict
INCONCLUSIVE rather than being dropped from the pool. (2026-11-13: a sample up to 5 s old now
decides when no newer packet exists, and every Drink cast records its state in the evidence; see the
entry above.)
On the six images it rescores 0/n PASS in every one: `DrinkAction::isUseful` already requires
`!GroupIsFighting`, so a start while a party member is under attack is only a stale-value race. The
meal-gap misses (12 of 15) happen when a refill mob opens on the party DURING the 15 s meal, which
this rule does not grade; a rule for that needs the meal state in the stream (FOLLOWUPS.md). The
suite delta: run per module, because `test_cc_target` and `test_orphans` hang and `test_live` never
ends (all three behave the same on main). Branch and main, same runner under `nice` and
`flock /tmp/trials-tests.lock`: the non-clean modules are identical on both sides — `test_cc_gap`
1 failure, `test_live3` 1 (the new `priest-shadow-inform-heal-solo-healer` has floor 12), `test_ooc_hurt`
4 failures + 4 errors, `test_rules_healers` 1 (the same new trial is missing from its IDS list),
`test_cc_target`/`test_orphans` killed at 90 s, `test_live` killed at 425 s. The branch adds
`tests/test_addpickup_pooled.py` (12 tests OK) and updates `EXPECT` in `test_rules_tanks_dps.py`;
no new failing test name.

## 2026-11-12: fix round 2 (trials/bench-fix-r2): an unreadable aura list is no sample

Codex HIGH: `hire_aura_ids()` built an empty set when the unit row had no `auras`, a null `auras`
or only malformed entries. `clear_prepull_auras`'s `gone()` reads an empty set as every aura
absent, so the prepull rule scored a form the hire never lost. The sample now counts only when
`auras` is a list and every entry has an integer `spell_id`; otherwise it returns `None`, the same
as an unreadable unit row, and the repeat ends INVALID `prepull unaura: no fresh aura sample of
the hire` before the summon. Three tests (missing, null, malformed entry) assert that path and no
summon; README names the unreadable sample next to the refused line.

Suite delta against main 4180cde in a scratch copy (both `python3 -m unittest discover -s tests -v`,
under `nice` and `flock /tmp/trials-tests.lock`): branch `Ran 839 tests ... FAILED (failures=12,
errors=5, skipped=5)`, main `Ran 806 tests ... FAILED (failures=12, errors=5, skipped=5)` (the 33
extra tests are this branch's own). The `FAIL:`/`ERROR:` name lists are identical, 17 names in
cc_gap (1), cc_target (1), hire_talents (1), live (4), ooc_hurt (8) and orphans (2). The lists are
quoted in the round report.

## 2026-11-12: fix round 1 (trials/bench-fix-r2): direct visibility proof, prepull failure, suite delta

Codex HIGH: `player_visible` passed on a bot attacking a mob within 5 yd of the player. That proves
the party shares the spot, not that a bot ever saw the player, so the check now passes only on direct
evidence: a hired bot (main or extra) casting/healing/buffing at the player in the window, or a
sampler row where a bot's own API `target` is the player (`_bot_targets_player`). The nearby fight
and mob hits on the player stay in the row's value and evidence as diagnostics; nothing else passes.
When one of the old fallbacks was the only evidence the rep is INVALID with `visibility unproven`
(`REASON_VISIBILITY_UNPROVEN`, carried by `apply_player_visible`) instead of the generic
`player not visible to bots`. Group membership is not used as proof: every non-raid trial already
requires the `grouped` setup check, and live3's hidden GM was grouped while the bots skipped it, so
the branch would be vacuous; no rep file records group membership anyway. Mob hits on the player no
longer pass for a non-healer either (test_live3's two mob-hit tests now assert INVALID): a mob seeing
the player says nothing about the bots.

Codex HIGH: a refused `.unaura` was logged and the run continued, and a missing hire guid returned
silently. `clear_prepull_auras` now returns a reason for a missing hire identity, a refused line, an
unreadable fresh aura read, or an aura still on the hire after the unaura; `run()` ends the repeat
INVALID (`ended: "prepull"`, the reason is kept because "prepull" counts as no fight) before the
summon. The fresh read is a `unit` RPC sample polled for `PREPULL_AURA_WAIT_S` 3 s: the aura must be
absent, not merely the line accepted.

Suite delta against main 4180cde in a scratch copy (both `python3 -m unittest discover -s tests -v`,
under `nice` and `flock /tmp/trials-tests.lock`): branch `Ran 836 tests ... FAILED (failures=9,
errors=5, skipped=5)`, main `Ran 806 tests ... FAILED (failures=9, errors=5, skipped=5)` (the 30
extra tests are this branch's own). The two lists are identical, 14 names in cc_gap (1), cc_target
(1), live (2), ooc_hurt (9) and orphans (2), so this round added no failing test. Every one ends on
the pre-existing test bug that stubs `sit.arena` while `run()` calls `acquire_arena`, so the repeat
ends "no quiet arena" after the 10-minute give-up. The lists are quoted in the round report.

## 2026-11-11: bench fix round 2 (bench-fix-1111): summon claim, marker timing, tea, forms

The FAIL reviews of run bench-fix-1111 (TRIAGE in the audit folder) named a bench problem behind most
of the INVALID and INCONCLUSIVE reps: on this core `.npc summon` prints no guid, and `summon_mob`
claimed a new summon only within 3 yd. 39 of 14 trials' summons stayed unclaimed, each one unit of
the right entry at 3.27-14.7 yd, and every miss set `summon_blocked` and stopped the refills for the
rest of the repeat. New rule: when exactly one fresh unit of the entry appeared (absent from the
pre-summon list, not ledgered, not another slot's), claim it at any distance inside the 30 yd scan;
with zero or two or more fresh units the 3 yd claim and the miss stay, because there is no way to
tell them apart. The unclaimed row and its log line are unchanged.

Markers are now stamped BEFORE the GM line they announce, not after it returns. The refill marker
carries the `.npc summon` send wall (`summon_mob(..., now_iso=, sent=)` fills a dict before the send;
`Situation.add_refill_mark` uses it), and `energy_drain` stamps `marks.now_iso()` before
`.modify energy`. bench-fix-1111: rep3 of dps-mage-threat-cap stamped its refill 5.6 s after the
summon and all 6 counted threat hits fell in that gap; rep4 of dps-rogue-thistle-tea drank 64 ms
before the mark and the 300 s debounce then ate every later drain.

`where.not_within_s_after_mark` takes a list of specs, and the rule's `min_opportunity_s` reads the
same filtered samples (`rules.after_mark_samples`), so a window dropped from the count carries no
opportunity either. dps-mage-threat-cap: leaves out `fight_start` 12 s and each refill 12 s, measures
the opportunity on the mage (`$bot` in combat: a bot tank leaves the player out of combat) and needs
3 valid reps; one rep decided it in bench-fix-1111.

`check.debounce_responder_across_reps` (needs `debounce_responder_s`) carries the responder cooldown
over the repeat boundary: `score_rep` receives the earlier repeats' events (`score_trial`, with each
row placed on its own rep's fight_start wall) and adds a matching response as an excluded span on
this rep's base. dps-rogue-thistle-tea uses it because `.cooldown` does NOT reset Thistle Tea's
5-minute item cooldown (bench-fix-1111: the bot answered every drain at least 300 s after its last
tea, within ~10 ms, and none inside). Every in-rep behaviour is unchanged.

`situation.prepull_unaura: [<aura id>]` strips those auras off the hire before the pull
(`Situation.clear_prepull_auras`, before the first summon). priest-shadow-form-prepull uses it for
Shadowform 15473: the hire is reused, reps 2-3 "passed" only because the form carried over, and the
per-rep value was a coin flip.

The two second-tank PWS trials (priest-holy-pws-bear-bal-2tank, priest-holy-pws-def-arms-2tank) set
`expect_today` PASS and their descriptions become regression guards: #1553 is fixed by TortoiseBots
5f57e84f2 / PR #1557 (PriestActions.h:43-49 -> ShouldShieldTarget, ShieldTargetPolicy.h:23-39: the
form/stance counts as a tank even when HasOtherTank makes GetPlayerRoles return the talent role).
Both get `min_reps 3`: def-arms passed the round on one valid rep.

`player_visible` passes for a bot that is not a healer when a bot attacked a mob within 5 yd of the
player in the first 30 s (positions from `samples.jsonl`; a rep without samples keeps the older
rules). bench-fix-1111: 5 reps were INVALID with 0 bot casts at the player and 0 mob hits on it while
the tank's casts and swings landed on mobs 0-3.5 yd from the player; the player was never targeted by
any bot in those reps, so a target-list rule would not have fired, and the "group" side of the option
is already proven by the `grouped` setup check and would make the guard vacuous. A hired healer still
must cast at the player (the live3 hidden-GM finding), and a `bot role` reread stays as strict.

## 2026-11-11: the marked mob is taunted into combat before the fight counts (P4)

The moon-marked mob never entered combat (the bench keeps it idle on purpose), so TortoiseBots' CC
chooser never saw it (candidates come from the group's attackers, which need InCombat) while the mage
free-picked an unmarked mob — 0 sheep on the mark in every valid rep, RESPECT passing with nothing to
respect. New `engage_marked` (called between `mark_and_cc` and the unmarked attacks; the run's pull
order lives in `run_cc_pull`): walk near, Taunt (355, already learned, melee range, no damage) under
the action lock, wait for `in_combat`, retry the cast once. Only `engage_marked` may send that Taunt;
no attack ever targets the marked guid, and the player's last selection is an unmarked mob again
(`threat()` casts on the selection). `mobs_ready` takes `marked` and names it in the evidence
("marked <guid> in combat: yes/no"); a mark left idle makes the rep INVALID. Rules: `opportunity`
gains `has_aura` (non-empty int list; a sample counts only when its auras intersect it); the trial gets
`opportunity {unit $marked, has_aura <all 7 Polymorph ranks>}` and RESPECT `min_opportunity_s 10`, so
no sheep means INCONCLUSIVE, never a vacuous PASS. The `count` zero-hit branch now prints, for any
`pass_if`, `opportunity <x>` (or `n/a (no opportunity block)`, never "0 s" with no block) plus the
same-spell casts on other targets with up to 3 evidence lines — a POLYMORPH FAIL with the mark in
combat and other-target polymorphs reads as the free sheep holding the chooser, and is filed against
TortoiseBots. POLYMORPH `min_reps 3`, repeats 3 → 4. Old baselines of this trial are not comparable
with new runs. Pre-existing, out of scope: `test_cc_gap.py::test_no_unmarked_mob_alive...` fails on
the base commit too (its `run_fight` stubs `sit.arena` but `run()` calls `acquire_arena`, so the run
ends "no arena"); the P4 fake update only adds the `engage_marked` stub.

## 2026-11-11: minimum samples, more chances, and the Bulwark level move (P3)

One valid rep decided the trial verdict (a majority of PASS/FAIL reps; taunt-off-healer went PASS →
INCONCLUSIVE → FAIL on single reps of 5/5 and 3/5). New rule field `min_reps` (int ≥ 1, default 1, so
every existing rule is unchanged; variants may override it): a trial verdict is PASS/FAIL only with at
least `min_reps` PASS/FAIL reps, else INCONCLUSIVE with reason `valid reps <k> < min_reps <m>`. Compare
gives no Fisher p and the note "too few valid reps" for such a side; pr-block prints the trial-level
reason line. Heal-latency pair: the trigger (player falling through 50) never fired because the
governor's top-up drops only to the band middle (65 for [40,90]) and the healer heals before mob hits
cross 50. New situation option `top_up_to_pct`: the governor's top-up drops to this percent instead of
the band middle (never below the band low). Set below the trigger line, so each top-up is one fall
through 50 (~10-20 triggers per 180 s rep). `priest-holy-heal-latency`: band [40,90] → [40,80], floor
30 → 15, cap 12, repeats 2 → 4, LATENCY `min_n` 5 (kept) + `min_reps` 3. `paladin-holy-heal-latency`:
band [30,90] → [40,80], `top_up_to_pct` 45, repeats 2 → 4, LATENCY `min_reps` 3 (floor 12, delta -4 and
cap 12 stay). Priest variants inherit. Taunt-off-healer: mobs count 1 → 3 (the shape
`tank-paladin-defensives` runs), duration 180 → 240, min_valid 120 → 160, repeats 3 → 4, TAUNT `min_reps`
3; MOBS-ON-TANK gets a new baseline. Thistle tea: the TortoiseBots trigger (energy < 20) almost never
fires (the rogue pools energy; the hire is reused between reps so a tea may still be on cooldown; the
`first ≤ 90` rule FAILs a rep that never had the chance). New guarded GM kind `modify_energy` (hire
only, `.modify energy {cur} {max}`) and situation options `energy_drain {first_at_s 5-170, every_s ≥ 20,
to 0-19}` (rogue + tank_damage only; drains the hire on schedule after fight_start with marks; a refused
line logs and marks `energy_drain_refused`) and `reset_hire_cooldowns: true` (`.cooldown` on the hire
after the arena is taken, before the first summon; the guard's `TARGETS["cooldown"]` gains `"hire"`;
situation check `hire_cooldowns_reset`, INVALID on failure; marks `cooldowns_reset`), plus a new latency
trigger kind `mark {mark}` that reads runner marks (`mark_times` on the event time base; `mark_spans`
rewritten on it) with the usual debounce/response logic. DPS-ROG-TEA becomes mark-latency (drain →
Restore Energy within 5 s, debounce 300 s on Restore Energy, pass ≥ 0.5, min_n 1, min_reps 3), repeats
3 → 4. Paladin defensives: Bulwark rank 1 (51346) has a 300 s cooldown and the hire is reused, so 0 casts
in 2 reps; the level-40 Protection premade holds Righteous Defense at rank 3 and zero Bulwark points
(Talent.dbc index 15, beyond the `053123133203` link) — level 40 → 50 (dps kit exists at 50), plus
`reset_hire_cooldowns: true`, repeats 3 → 4, BULWARK `min_reps` 3. Old baselines not comparable:
heal-latency pair, taunt-off-healer all, thistle tea, paladin-defensives. `tank-paladin-taunt-off-healer`
keeps count 1 / 180 s for now (FOLLOWUPS: apply the same fix after this round's live check).

## 2026-11-11: lighter fights for the bear-bal pair and shadow-form-prepull

The governor itself stays as it is. A gearless level-40 druid against a level-41 mob, and a gearless
warrior with no healer at floor 30 against a +1 mob, hit the rescue cap and cut the rep short (the
bear-bal PASS rested on one valid rep; shadow-form-prepull scored 31-43 s reps). `priest-holy-pws-bear-bal`
and its 2tank twin: mob delta 1 → -4 (the value chosen for `paladin-holy-heal-latency` on 2026-11-08;
the mob table has at least 3 beasts in 35-37) and `max_rescues_per_min: 12`, the same change in both
files so the twins still match. The band and floor stay [40,90] and 12; the GM `.damage` still holds
the druid inside the band. No druid kit in this round (FOLLOWUPS). `priest-shadow-form-prepull`: player
forced tank with the tier tank kit (`player_gear` check), a Holy priest healer hire, delta 1 → -1, floor
30 → 12, cap 12. Supersedes the 11-08 sentence "`priest-holy-pws-bear-bal` and its 2tank twin carry no
cap … stay as they are". Old baselines of these three trials are not comparable with new runs.

## 2026-11-11: mage threat cap measured against a hired bot tank

The scripted player (melee + Taunt only) is not a believable tank, so the trial measured the bench,
not the mage: NO-AGGRO and DAMAGE failed together. New shape: the player in Battle Stance with the dps
kit (forced dps, `player_no_shield`), a hired Protection warrior tank as `extra_bots[0]` (geared with
`autogear blue` via the new `extra_bots[].gear` field), the priest healer as `$extra2`, 1 mob at delta 0,
band [35,99], floor 12, cap 12, 4 repeats. Two new precondition rules: DPS-MAG-THREAT-TANK-HELD (at
least 60 percent of mob hits on the hire, ThreatValues.cpp:124-183) and DPS-MAG-THREAT-TANK-FOUGHT (the
hire casts at least 5 times, same wording as TNK-WAR-2T-FOUGHT). The `@lvl60` override is now delta -2
(was -3). Old baselines of this trial (all variants) are not comparable with new runs.

`@lvl60` stays gearless: no level-60 kit is built. The player no longer tanks, so its gear only shifts
how much threat a dps player adds. A level-60 kit from `build_data.py` would pull 173 entries at or
above 20000, TBC-scale server customs (a 26-item "90 Green Warrior" batch, ilvl 90 at req 60) that break
the kit's vanilla rule. `GEAR_MAX_ENTRY` goes from 25000 to 20000 (constant only; no kit rebuilt, every
current entry is at most 17734). Level-40 protection premade check (Talent.dbc via `trl/talents.py`,
premade `053123133203`): Righteous Defense at rank 3, zero points in Bulwark of the Righteous (rank-1
spell 51346, tree index 15, beyond the link) — the level-40 build stops short of the talent.

## 2026-11-11: runs use a pinned, detached worktree

Two runs on 2026-11-10 broke because someone was editing the live checkout in the
middle of the run (an IndentationError mid-edit, and FAILED rows while the working copy was being
edited). `trials run` now refuses to start (exit 4, before any lease or server contact, `--dry-run`
excepted) unless the tool directory has a detached HEAD and no modified tracked files. The override
is `TRIALS_ALLOW_UNPINNED=1` (tests and emergencies); a run with the override records
`"bench_unpinned": true`. Every `RUN.json` records `bench_sha`, `bench_detached` and `bench_dirty`.

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
  rare, vanilla entries only (below 20000; the server's own additions sit at 20000 and up and are unproven), no random suffix, no reputation/honor/profession/spell requirement, no
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

### A hire's talent build is applied and proven (bot_talents)

A hired bot keeps the random talent build it was created with (a mage: arcane, fire or frost, 1/3 each): TB's
`role <bot> <spec>` rebuilds the strategies only (commands/BotCommands.cpp). `bot_spec` read only the strategy list, so
an arcane mage on a fire trial passed setup and scored silently (the Scorch rows fire only for a bot that knows Fire
Vulnerability, so the no-harm control swung with the roll). Chosen: for every hire (the main bot and every extra_bots
hire) whose spec has a marker (trl/talents.py), the bench whispers `talents <premade name>` (TB ChangeTalentsAction:
a premade spec by name) BEFORE the role order, then reads the bot's own bare `talents` reply (`Link: <link>`) and
requires the spec's marker talent at rank 1 or more (fire mage: Fire Vulnerability). `bot_talents` is a setup row of
every such trial, not only of those that list it; for extra hires it is part of `extra_bots_role`. A wrong build after
the apply makes the rep INVALID with the link found as evidence. It runs on every rep, a reused hire included (the
send is idempotent). The marker table comes from the client's Talent.dbc / TalentTab.dbc and TB's premade links
(its header says how); a trial whose bot spec has no marker is rejected by a test, not silently unchecked.

### Gear setup after review (FIX FIRST round 1)

- Slot characters are bench-owned; the strip removes only kit items foreign to the trial. A gear trial strips the kit
  entries that are NOT in its own kit (every copy of each, one guarded `-1` per copy) and never touches its own
  kit's entries; a player carrying none gets no GM line. No durable per-grant provenance is kept: the characters the
  bench logs in are its own, and the guard still limits the removal to kit entries on the player itself.
- Learned proficiencies are kept on the slot character (Plate Mail, Mail, weapon skills): there is no unlearn, the
  next trial of the same character needs them anyway, and an armor skill has no effect on a trial that wears no such
  armor.
- One deadline for the whole gear setup: GEAR_SETUP_BUDGET_S (90 s) is started at the first gear step of a rep and
  shared by the carry-over strip, the proficiency learn, the grants, the equips and both `_retry` passes. Every
  wait is capped by what is left, a spent budget stops the sends, and the evidence says which phase ran out. A piece
  the server refused (no proficiency, wrong slot) is not retried or waited for.
- The guard's `.learn` accepts the trial's own spells plus the proficiencies of THIS trial's kit only. A kit entry
  with no item class (missing, None or 0) fails the rep with "kit data lacks item class": there is no plate guess
  from the slot, and build_data.py writes None for a missing column. The strip read-back waits for the bags and the
  body to settle the same way the equip read-back does.



### Talent proof after review (FIX FIRST round 1)

- The proof is bound to the hire: the bench reads the event ROWS and accepts the apply reply and the `Link:` only
  from the hired bot (its party guid when the row carries one, else its name; turtle-agent's whisper rows carry the
  sender in `source`). Another hire's reply, a party-fallback answer from someone else, the player's own line,
  system text and a row with no sender are ignored (and counted in the evidence).
- A marker alone does not prove the spec. `bot_talents` needs ALL of: the marker talent at rank 1 or more; at least
  60 % of the points in the reported link in the spec's own tree (a stale hybrid with the marker fails); a well-formed
  link (ranks 0-5, at most three trees); and an apply reply that is not a failure ("Found multiple specs", "not
  found", an invalid or too-high spec fail the row whatever the build looks like). No apply reply at all fails it only
  when the build also fails the checks above. Exact premade-link matching was rejected: it is brittle across levels.
- A trial spec with no entry in the marker table is INVALID ("no talent marker for <class>/<spec>"), never a silent
  pass. The talent check runs under one 60 s deadline per bot, shared by both `_retry` passes, and every RPC and page
  read under a deadline gets a timeout capped by the time left (also in the gear setup).
- TB re-gears a hire after a build change (UpgradeGearBest, or the tier-kit refit). The evidence records the hire's
  gear after the apply (`.bot inv`), and a trial's `bot.gear` step runs after the talents, so the gear is the trial's.
  Baselines from before this change ran with rolled builds and rolled gear.

### Deadlines reach the transport (FIX FIRST round 2)

A per-bot deadline that only capped the waits still let `snapshot`, `chat`, `tbm`, `party`, `spells` and the event page
reads run on the default 20 s RPC, plus 15 s for the subprocess kill. Now every call under a deadline goes through
`Agent.dl_rpc`: it is not started once the deadline is spent (the call returns a `_deadline` error and the check records
the phase that ran out), and otherwise its own timeout is the time left (1 s floor) and the subprocess kill grace is 1 s
instead of 15. `read_rows`, `read_lines`, `read_inventory`, `party_guid`, the talent apply and read-back (party fallback
included) and the gear step's spell read carry the deadline, and the GM guard caps its 10 s selection RPCs by the
deadline of the step that is using it. A send that started just inside the budget can still run to its 1 s floor.


## 2026-11-10: trial variants, and the edge-case trials (trials/edge)

Variants (`variants` in a trial file, README "Variants"): chosen to keep the format small. An override is deep-merged
onto the base trial and the merged trial goes through the ordinary validation (`scenario.merge_variant`), so a
variant can never be looser than a trial file; the only rule edits allowed are `pass_if`, `min_n` and
`min_opportunity_s` by rule id (the brief: "rule thresholds, only where the level changes them"), and the rules list,
every `check` and every `expect_today` stay the base's. The id is `<trial>@<variant>` end to end: the run folder, the
`trial.json`, the card rows and the compare/pr-block key. The ids are not folded into the base because a level 60 run
is a different measurement, and a baseline of one level must not be compared with another. Selection: a bare id, a glob
without `@` and `--class` reach base trials only (a sweep of `tank-*` must not start three times the runs); a
variant is asked for as `id@variant` or `id@*`. Rejected: a per-variant rules list (it would make two behaviours of
one trial), a `variants` file beside the trial (one more place to look), variants that change the hire's class, spec
or role (a different bot is a different trial).

Level variants are 50 and 60, not 20. The sweep brief asked for 20 / 40 / 60 on one heal, one tank and one DPS trial.
Level 20 cannot be run: `bot_talents` proves a hire's build by a marker talent (`trl/talents.py`) that the level-35
premade link has. Read from TB `aiplayerbot.conf.dist.in` (`PremadeSpecLink.<class>.<spec>.<level>`, upstream/dev
`0c56cf649`) with the bench's own marker functions: the marker rank is 0 at levels 20, 25 and 30 for Holy priest,
Protection warrior, Fire mage, Protection paladin, Combat rogue and Feral druid (the Fire mage has it from 30, the
others from 35), so every repeat of a level 20 trial would be INVALID on the talent proof. `validate` refuses a variant
whose hire is under level 35. The levels used are 40 (the file), 50 and 60:

| trial | variants | notes |
|---|---|---|
| `priest-holy-heal-latency` (heal) | `lvl50`, `lvl60` | `lvl60` mobs `level_delta` -2 (the mob table has melee at 57-59) |
| `tank-warrior-taunt-off-healer` (tank) | `lvl50`, `lvl60` | the gear kit exists for 40, 45, 50 only (at most 9 levels down), so `lvl60` runs with `player.gear: null`; `level_delta` -2 |
| `dps-mage-threat-cap` (DPS) | `lvl50`, `lvl60` | same: `lvl60` has no gear kit, `level_delta` -3 |

None of the level numbers has been run: the `level_delta` of the level 60 variants is a guess at a fight a gearless
player survives (the gear kits exist because an ungeared player is a weak tank and moves the DPS trials' threat back-off). The player's talent spell
ids are `.learn`ed whatever the level, so they are not varied.

Edge-case trials (`trials/edge/`, a folder of their own so the per-folder test lists of tanks, dps and healers stay
as they were; each file's `group` is still the runner's group). Written 2026-11-10 with no live run; every rule carries
`expect_today` and a TB file:line at `upstream/dev` `0c56cf649` (the earlier tank trials cite `abb91ff62`; the
ProtectionWarrior row lines agree between the two). Per edge:

- Two bot tanks: `tank-warrior-two-bot-tanks` (a Protection paladin as `extra1`, tank role). The question is the
  warrior's taunt on a mob whose current target is the paladin (`dst_target_of: $extra1`), counted 10 s into the
  fight and outside 8 s after a refill (both tanks may answer a fresh mob on the DPS player in the same second; the
  loser of that race would otherwise score as a steal). A precondition rule proves the paladin took 5 hits.
  "A player tank against a bot tank, as a variant": that case already exists as
  `tank-warrior-no-taunt-tank-player`; it gets the variant `bot-offtank` (a second, paladin bot tank in the group).
  A variant of the two-tank trial that swaps the other tank for the player cannot exist yet: the rule names its
  alias, and `dst_target_of` takes one alias (see "needs a new kind" in `docs/next-trials.md`).
- Resources: `priest-holy-low-mana-drinks` (ooc window, `bot.mana_pct` 15, variants `mana-5` and `mana-50`; the
  high mana trigger opens under 65 percent), `priest-holy-low-mana-fight` (no off-spec nuke under 60 percent mana,
  at least 3 heals on the player), `priest-holy-low-mana-potion` (Greater Mana Potion x3 given by name, a
  `Restore Mana` cast within 30 s), `tank-warrior-zero-rage-opening` (`pull_after_s` 20 so the rage has decayed, a
  precondition that the first hits come at under 30 percent rage), `dps-rogue-low-energy` (swings while under 25
  percent energy) and `dps-rogue-thistle-tea` (level 60, Thistle Tea given by name; the tea action is level-60 only).
  Rage and energy cannot be set (the guard has `.modify mana` for hires only), so "0 rage" and "no energy" are the
  natural states the trials wait for and prove with a rule.
- Not written, listed as needing a new kind: the tank dying mid-fight, and the bot stunned, silenced, feared or
  rooted (`docs/next-trials.md`).

Open items a run has to settle: the item names `Greater Mana Potion` and `Thistle Tea` (the bot_items check will say if
the server does not know them), whether the bot's own `Drink` aura and `Restore Mana`/`Restore Energy` casts reach the
event stream for a hire (the drinks trial has an uptime rule from samples beside the event rule, so the two can be told
apart), and the 60 percent mana line of `PRI-LOWMANA-NO-NUKE`, chosen under the code's 70 percent floor so a drained
healer that has recovered a little is not scored.
