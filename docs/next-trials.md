# Next trials: tanks and DPS

Design notes for the trials after the healer set. All code lines are from TortoiseBots `upstream/dev` at
`abb91ff62` (paths are relative to the TortoiseBots repository). The trials marked **written** are in
`trials/tanks/` and `trials/dps/`, with offline tests in `tests/test_rules_tanks_dps.py`. The sweep
added 21 tank trials (sweep-tanks); the remaining rows need a situation feature that does not exist
yet (listed at the end). Edge cases (group shape, resources, variants) are in their own section below, with
the ones that need a new kind.

## How the list is ranked

1. Is there a code path that probably makes the bot do the wrong thing today (a likely real bug)?
2. Do walks depend on it (a missed taunt or interrupt kills the healer and wipes the pull)?
3. Is there a known complaint or a recent fix that could regress?

## Ranked list

| # | trial | bot | situation | status | why it ranks here |
|---|---|---|---|---|---|
| 1 | `dps-rogue-kick-caster` | Combat rogue | `caster_mob` (exists) | **written** | Kick is an interrupt action but keeps the single-target threat type, so the party threat back-off can veto it exactly when the rogue is near the tank's threat |
| 2 | `dps-mage-counterspell-caster` | Arcane mage | `caster_mob` (exists) | **written** | same veto on Counterspell; the wand fallback (#922) keeps the mage at the cap |
| 3 | `tank-warrior-taunt-off-healer` | Protection warrior | `tank_damage` + priest extra (exists) | **written** | the healer's life in every walk; dev added two taunt rows (taunt on attacker, Mocking Blow) that this guards |
| 4 | `dps-mage-threat-cap` | Fire mage | `tank_damage` (exists) | **written** | the back-off stops the nukes but not the wand; does the mob still come off a player tank? |
| 5 | `dps-mage-cc-respect` | Frost mage | `cc_target`, 3 mobs (exists) | **written** | sheep breaks wipe dungeon pulls; many recent fixes (AoE interlock, moon hold) could regress |
| 6 | `tank-paladin-righteous-fury` | Protection paladin | `tank_damage` + priest extra (exists) | **written** | a paladin tank without Righteous Fury holds nothing; both paladin taunt rows sit at ACTION_MOVE |
| 7 | `tank-bear-growl-maul` + `tank-bear-hold-single` | Feral (bear) druid | `tank_damage` + priest extra (exists) | **written** (sweep-tanks) | Growl and Growl on attacker at ACTION_MOVE; Maul now has its own row; the hold trial scores the 1-mob outcome the growl trial's latency leaves open |
| 8 | `tank-warrior-pack-spread` | Protection warrior | `pack`, 3 mobs (exists) | **written** (sweep-tanks) | loose adds on the healer are the common wipe; Thunder Clap priority changed in #949. No standalone 3-mob hold file: `TNK-WAR-PACK-SHARE` already scores that outcome, and the 1-mob outcome is the existing `TNK-WAR-MOBS-ON-TANK` ratio |
| 9 | `tank-warrior-shield-block` | Protection warrior | needs **F1** (bot shield check) | needs feature | Shield Block needs a shield; today nothing proves the hire holds one. Not written: `tank-warrior-defensives` covers Shield Wall + Last Stand instead, which need no new kind |
| 10 | `dps-hunter-feign-death` | Marksmanship hunter | needs **F2** (aggro on the bot) | needs feature | FD was just changed (`2fdf7da8c`); the pet and the player's threat get in the way |
| 11 | `dps-threat-ratio-80` | any DPS | needs **F3** (threat samples) | needs feature | the 80 percent number itself; trials 4 and 1-2 only see its effects |

Common set-up for all of them:

- The player's role is **forced** (`.bot role self tank|dps`, setup check `player_forced_role`).
  `PlayerbotAI::IsTank` reads the forced role first (`ai/playerbot/PlayerbotAI.cpp:4026-4047`, the forced
  role at `:4028-4030`), so neither a stance read nor the second-tank rule (`AiFactory.cpp:580-617`,
  #1553) can flip what the bot thinks the player is. The stance is still held and scored as a precondition
  (`*-STANCE` uptime rules).
- Every trial hires a **Holy priest as `extra1`**. A GM-levelled warrior with no healer hits the rescue cap
  inside a minute (the `paladin-holy-heal-latency` runs, see DECISIONS.md), and a tank bot has no self-heal.
  In the tank trials the priest is also the "healer the mob hits".
- Groups: tanks and the rogue use `tanks-melee`, mages `ranged` (the folder is `tanks/` or `dps/`).
- Mob tables: the `caster` table has two entries that can spawn in the level 50 window (50-52), the
  `humanoid` table four in the level 45 window; level 40 has almost none of either, so the interrupt trials
  run at 50 and the sheep trial at 45.
- Answer debounce: the three latency rules (Taunt, Kick, Counterspell) should carry
  `debounce_responder_s` with the spell's cooldown (10, 10, 30) so one answer is not counted for two
  triggers. The key is being added to the check DSL; it goes into the trial files once `validate` knows it.

## 1. `dps-rogue-kick-caster` (written)

- **Situation:** `caster_mob`, one caster mob with refill, level 50; player warrior in Defensive Stance,
  forced tank; Combat rogue hire; priest extra. Setup check `mob_caster_casts`.
- **Rules:**
  - `DPS-ROG-KICK`: `latency`, trigger `cast_start` by `$mob` with `spell_interruptible`, response Kick by
    `$bot` on the casting mob (`response_dst: $trigger.src`), `within_s` 3, `trigger_debounce_s` 10
    (Kick's cooldown); `pass_if >= 0.6`, `min_n` 5. Kidney Shot (the kick node's alternative) is not
    counted: the events cannot show that it stopped the cast.
  - `DPS-ROG-KICK-STANCE`: player Defensive Stance uptime `>= 0.9` (precondition).
- **Code:** `strategy/rogue/RogueStrategy.cpp:377-396` (RogueCcStrategy; kick at ACTION_INTERRUPT + 1 at
  `:385-387`), `strategy/rogue/RogueTriggers.h:9`, `strategy/rogue/RogueActions.h:113-117` (CastKickAction is
  a CastMeleeInterruptSpellAction), `strategy/actions/GenericSpellActions.h:131-152` (the interrupt action
  classes target the "interrupt target" but keep CastSpellAction's threat type), `:39`
  (ACTION_THREAT_SINGLE), `strategy/generic/ThreatStrategy.cpp:68-83` (the high-threat veto),
  `AiFactory.cpp:876-892` (rogues get `cc`), `AiFactory.cpp:633-642,1007-1016` (non-tank bots under a real
  master get `threat`), `strategy/triggers/GenericTriggers.cpp:1072-1075`, `PlayerbotAISpells.cpp:1799-1847`.
- **expect_today: `?`.** With no back-off the kick row outranks the whole rotation and passes. Under a
  player tank the rogue has the `threat` strategy, its white swings keep it near 80 percent of an ungeared
  tank's threat, and the veto scores Kick 0 while "high threat" holds. FAIL here with a PASS on the control
  points at the veto.
- **Control:** the same trial with the player in Battle Stance forced to `dps`: no tank in the group, so
  `ThreatValue` returns 0 (`strategy/values/ThreatValues.cpp:228-229`) and nothing is vetoed; expected PASS.

## 2. `dps-mage-counterspell-caster` (written)

- **Situation:** as trial 1, Arcane mage hire.
- **Rules:** `DPS-MAG-COUNTERSPELL`: as `DPS-ROG-KICK` with Counterspell, `trigger_debounce_s` 30;
  `pass_if >= 0.6`, `min_n` 5. `DPS-MAG-CS-STANCE`: precondition.
- **Code:** `strategy/mage/MageStrategy.cpp:428-455` (MageCcStrategy; counterspell at ACTION_INTERRUPT + 1
  at `:443-444`), `strategy/mage/MageTriggers.h:113`, `strategy/mage/MageActions.h:153-154`
  (BEGIN_INTERRUPT_SPELL_ACTION), `AiFactory.cpp:704-721`, `ThreatStrategy.cpp:68-83`,
  `GenericSpellActions.h:501-505` (Shoot is threat type NONE).
- **expect_today: `?`.** Same veto as Kick. Worse: the vetoed nukes fall back to the wand on purpose
  (#922), and the wand is never vetoed, so the mage stays at the cap the longer it fights.
- **Control:** the no-tank variant, expected PASS.

## 3. `tank-warrior-taunt-off-healer` (written)

- **Situation:** `tank_damage`, one melee mob with refill, level 40; the player (Arms warrior, Battle
  Stance, forced `dps`) pulls every mob, so each mob opens on a non-tank; Protection warrior hire; priest
  extra (heal threat pulls fresh mobs onto the healer too).
- **Rules:**
  - `TNK-WAR-TAUNT`: `latency`, trigger `damage` from `$mob` on `$player` or `$extra1`, response Taunt,
    Mocking Blow or Challenging Shout by `$bot` on that mob within 5 s, `trigger_debounce_s` 12;
    `pass_if >= 0.7`, `min_n` 5.
  - `TNK-WAR-MOBS-ON-TANK`: `ratio` of mob damage events (hits, not amounts) on the player or the healer
    over all mob damage events; `pass_if <= 0.3`, `min_n` 20.
  - `TNK-WAR-DEF-STANCE`: bot Defensive Stance (71) uptime in combat `>= 0.85`.
  - `TNK-WAR-TAUNT-STANCE`: player Battle Stance precondition.
- **Code:** `strategy/warrior/ProtectionWarriorStrategy.cpp:126-128` (lose aggro -> taunt, INTERRUPT + 1),
  `:129-131` (mocking blow taunt -> mocking blow, INTERRUPT), `:136-138` (party member attacked -> taunt on
  attacker, INTERRUPT + 1), `:27`, `:63-65` (stance row), `:230-234` (overpower dance),
  `strategy/warrior/WarriorActions.h:437-445` (taunt on attacker targets the "attacker of non-tank target"),
  `strategy/triggers/GenericTriggers.h:112-121` (PartyMemberAttackedTrigger),
  `strategy/triggers/GenericTriggers.cpp:312-349` (LoseAggroTrigger; the tank-player exception at
  `:328-343`), `strategy/values/AttackerOfNonTankTargetValue.cpp:11-44` (reads `IsTank` of each victim),
  `PlayerbotAI.cpp:4026-4047`.
- **expect_today:** Taunt `PASS` (two rows at INTERRUPT + 1 answer a mob on a non-tank, above every DPS
  row); ratio `?` (an outcome; the player always eats the opening hits); stance `PASS`.
- **Control:** `tank-warrior-taunt-tank-player-control`: the player in Defensive Stance forced to `tank`.
  Rule: `count` of Taunt casts by `$bot` with `dst_target_of: $player` `== 0`, because LoseAggroTrigger
  returns false when the mob's victim is a tank player (`GenericTriggers.cpp:330-341`) and "attacker of
  non-tank target" reads the victim's tank role (`AttackerOfNonTankTargetValue.cpp:44`). Expected PASS. No new feature needed.

## 4. `dps-mage-threat-cap` (written)

- **Situation:** `tank_damage`, one melee mob with refill, level 40; player warrior Defensive Stance,
  forced tank; Fire mage hire; priest extra. `opportunity`: the player in combat.
- **Rules:**
  - `DPS-MAG-THREAT-NO-AGGRO`: `count` of mob damage on `$bot` `== 0`, `min_opportunity_s` 90. (Being
    reworked with a new tool feature; not changed here.)
  - `DPS-MAG-THREAT-DAMAGE`: precondition, at least 15 fire nukes on the mobs per repeat. A count, not a
    damage share: the DSL counts events, so a share would weigh every melee swing against one Fireball.
    Unvetoed a fire mage casts 40-50 nukes in 180 s; 15 means it nuked for at least a third of the fight,
    so a NO-AGGRO pass cannot come from an idle or wand-only mage.
  - `DPS-MAG-THREAT-STANCE`: precondition.
- **Code:** `strategy/generic/ThreatStrategy.cpp:13-87` (veto at `:68-83`),
  `strategy/triggers/GenericTriggers.cpp:758-788` (80 percent with a 5 s look-ahead),
  `strategy/values/ThreatValues.cpp:124-183,198-246`, `AiFactory.cpp:633-642,1007-1016`,
  `strategy/mage/FireMageStrategy.cpp:25-27,34-48`, commit `320aa2906` (#922).
- **expect_today:** no-aggro `?` (the wand keeps adding threat while the nukes are vetoed; whether it
  reaches the 130 percent a ranged attacker needs against an ungeared tank is the question); damage `PASS`.
- **Control:** the player in Battle Stance forced `dps`: no tank, no veto, the mage pulls; rule
  `count >= 1`. Expected PASS.

## 5. `dps-mage-cc-respect` (written)

- **Situation:** `cc_target`, three humanoid mobs with refill, level 45; the runner marks the second mob
  moon and sends `.bot cc moon <bot>` (setup check `cc_mark_set`); the player tanks the first mob; Frost
  mage hire; priest extra. Three, not two: with two, the gap between a kill and its refill leaves the sheep
  as the only target, and `PossibleAttackTargetsValue.cpp:97-107` then hands it to the mage by design. With
  three the refill keeps an unmarked mob up (the situation refills while fewer than `count` are alive).
- **Rules:**
  - `DPS-MAG-CC-RESPECT`: `count` of `damage` from `$bot` on `$marked` while the marked mob carries any
    Polymorph (`dst_has_aura` 118, 12824, 12825, 12826, 28270, 28271, 28272) `== 0`. Periodic and splash
    damage count (Frost Nova, Blizzard, Cone of Cold). `marked_has_cc` now knows every rank (commit
    `5fa9e61`) but also matches other CC; only the sheep matters here.
  - `DPS-MAG-CC-POLYMORPH`: precondition, at least one Polymorph by `$bot` on `$marked`.
- **Code:** `strategy/values/PossibleAttackTargetsValue.cpp:53-108,131-173`,
  `strategy/triggers/GenericTriggers.cpp:830-860` (AoE interlock),
  `strategy/actions/GenericSpellActions.cpp:46-61` (BreaksCcInArea), `strategy/mage/MageStrategy.cpp:446-448`,
  commits `180024d15`, `9ccede38a`, `bf9c45077`.
- **expect_today:** both `PASS`.
- **Control:** `pack` of two humanoids with no mark: the mage's damage on the second mob is `>= 1`
  (shows the event path sees mage damage on that mob). No new feature needed.

## 6. `tank-paladin-righteous-fury` (written)

- **Situation:** as trial 3 with a Protection paladin hire.
- **Rules:**
  - `TNK-PAL-RIGHTEOUS-FURY`: bot aura 20450, 25780 or 25781 (all "Righteous Fury" in `data/spells.json`)
    uptime in combat `>= 0.95`.
  - `TNK-PAL-MOBS-ON-TANK`: the hit-count ratio of trial 3.
  - `TNK-PAL-RF-STANCE`: player precondition.
- **Code:** `strategy/paladin/ProtectionPaladinStrategy.cpp:252-272` (Righteous Fury rows in and out of
  combat), `strategy/paladin/PaladinTriggers.h:66`, `strategy/paladin/PaladinActions.h:73`,
  `strategy/paladin/PaladinAiObjectContext.cpp:76-78`; taunts `ProtectionPaladinStrategy.cpp:51-54`
  (lose aggro -> hand of reckoning, ACTION_MOVE) and `:58-60` (party member attacked -> hand of reckoning
  on attacker, ACTION_MOVE, #1156), both falling back to Righteous Defense (`:19,23`).
- **expect_today:** Righteous Fury `PASS`; ratio `?` (both taunt rows at ACTION_MOVE, the alternative is not
  a 1.12 spell; compare with trial 3).
- **Control:** a Retribution paladin hired as `dps`: Righteous Fury uptime `== 0` (the rows live only in
  the protection buff strategy).

## 7. `tank-bear-growl-maul` + `tank-bear-hold-single` (written, sweep-tanks)

- **Situation:** as trial 3 with a feral druid hired as tank (`spec: bear`), level 40 (Bear Form; Dire
  Bear Form at 40 needs the rank learned).
- **Rules:** `TNK-DRU-GROWL` latency as `TNK-WAR-TAUNT` with Growl, debounce 12, `>= 0.7`, responder
  cooldown 10; `TNK-DRU-MAUL` `rate` of Maul casts by `$bot` `>= 4` per minute; `TNK-DRU-FORM` uptime
  of 5487 or 9634 `>= 0.9`. The hold trial (`tank-bear-hold-single`) scores the 1-mob outcome
  (`<= 0.3` of mob hits on non-tanks) the latency leaves open.
- **Code:** `strategy/druid/TankFeralDruidStrategy.cpp:55-57` (lose aggro -> growl at ACTION_MOVE),
  `:61-63` (party member attacked -> growl on attacker at ACTION_MOVE), `:73-75` (maul rage available ->
  maul, NORMAL + 1), `:30-33` (default action melee).
- **expect_today:** Growl `?` (ACTION_MOVE is far below the warrior's INTERRUPT + 1; anything at HIGH wins
  the tick first); Maul `PASS` (its own row now); form `PASS`.
- **Control:** `tank-bear-no-growl-tank-player`: the player in Defensive Stance forced to `tank`;
  `count` of Growl casts by `$bot` with `dst_target_of: $player` `== 0` (LoseAggroTrigger returns false
  when the victim is a tank player). Expected PASS. No new feature needed.

## 8. `tank-warrior-pack-spread` (written, sweep-tanks)

- **Situation:** `pack`, three melee mobs with refill, level 40 with `level_delta` -2; the player attacks
  every mob, so all three open on him.
- **Rules:** `TNK-WAR-PACK-ENGAGE`: `latency`, trigger mob damage on `$player`/`$extra1` (debounce 8 per
  mob), response any `cast` by `$bot` on that mob within 6 s, `>= 0.7`; `TNK-WAR-PACK-CLAP`: `count` of
  Thunder Clap `>= 1`; `TNK-WAR-PACK-SHARE` the ratio rule of trial 3 at `<= 0.4`.
- **Code:** `ProtectionWarriorStrategy.cpp:164-166` (melee light aoe -> thunder clap HIGH + 7, #949),
  `:126-138`; `docs/concepts/bot-mechanics-and-quirks.md` section 1 (TankTargetValue buckets loose mobs
  first).
- **expect_today:** `?` for engage and share (one taunt per 10 s cannot cover three loose mobs; taunt on
  attacker and the clap carry it); clap `PASS`.
- **Control:** dropped (the same with one mob, clap count `== 0`): the hold outcome on 1 mob is the
  existing `TNK-WAR-MOBS-ON-TANK` ratio, and the multi-mob clap is the pack trial itself.

## 9. `tank-warrior-shield-block` (needs F1)

- **Rules:** `rate` of Shield Block casts by `$bot` in combat `>= 3` per minute; uptime of aura 2565
  is low by design (5 s), so the rate is the rule.
- **Code:** `ProtectionWarriorStrategy.cpp:148-150` (shield block HIGH + 2), `strategy/warrior/WarriorTriggers.h:22`.
- **expect_today:** `PASS` if the hire holds a shield, which nothing proves today.
- **Control:** a Protection warrior hire with the shield removed: count `== 0`.

## 10. `dps-hunter-feign-death` (needs F2)

- **Rules:** `latency`, trigger mob damage on `$bot` (the hunter), response Feign Death by `$bot` within
  3 s, `>= 0.6`, `min_n` 5.
- **Code:** `strategy/hunter/HunterStrategy.cpp:41` (feign death threat node), `:122-124` (medium threat
  -> feign death threat, ACTION_HIGH), `:248-250` (high threat -> ACTION_EMERGENCY, raid strategy),
  `:334-336` (remove feign death once it is up), `strategy/hunter/HunterTriggers.h:12`, commit `2fdf7da8c`
  (no Feign Death while the hunter is the only unit on the mob's threat list).
- **expect_today:** `?`. Feign Death is driven by the threat triggers, not by being hit: it needs a tank
  in the group (no tank, `ThreatValue` is 0 and nothing fires), and a mob on the hunter reads as 100
  percent or more of the tank's threat.
- **Control:** a mob whose threat list holds only the hunter: Feign Death count `== 0` (the new rule).

## 11. `dps-threat-ratio-80` (needs F3)

- **Rules:** while the bot casts damage spells, its threat on the mob over the tank's threat stays below
  0.9 in at least 95 percent of the samples.
- **Code:** `GenericTriggers.cpp:758-788`, `ThreatValues.cpp:124-183,198-246`.
- **expect_today:** `?`.
- **Control:** the no-tank variant (no veto, the ratio climbs past 1).

## Needed features (not built here)

- **F1. Setup check `bot_shield`.** Read the hire's off-hand (the code that reads it for
  `extra_bots_no_shield` exists) and require a shield; optionally give one by name through `bot.items`
  and equip it. Unblocks trial 9.
- **F2. Situation field `aggro_on: "bot"`.** After the player opens the mob, make the mob attack the hired
  bot while the player stays on its threat list (a GM threat or attack line aimed at the bot), and an
  option to dismiss the hunter's pet so its Growl does not take the mob back. Unblocks trial 10 and an
  honest Fade/Feint trial for priests and rogues.
- **F3. Threat samples.** The sampler reads, once a second, each summoned mob's threat list (threat per
  attacker guid), and the check DSL gets a sample-based metric or a where key such as
  `src_threat_pct_of_tank`. Unblocks trial 11 and would turn the outcome rules of trials 3, 4 and 6 into
  direct ones.

## Sweep-tanks: the 18 tank trials (round 1 fixes, 2026-11-09)

Written 2026-11-09, no live runs. Each trial tests ONE behaviour for warrior (protection), druid
(bear) or paladin (protection); every rule cites `ai/playerbot/` code at `abb91ff62` and carries
`expect_today` with the one-line reason. Offline tests in `tests/test_rules_tanks_dps.py` score every
rule PASS and FAIL (or INCONCLUSIVE at `min_n` / `min_opportunity_s`) through `score_rep`.
Round 1 (this pass) fixed the always-PASS/FAIL shapes found in cold review; the per-item notes are
under the table.

| file | behaviour | expect_today |
|---|---|---|
| `tank-bear-growl-maul.json` | Growl latency, Maul rate, Dire Bear Form uptime | `?` / PASS / PASS |
| `tank-bear-hold-single.json` | holds 1 mob (`<= 0.3` of hits on non-tanks) | `?` |
| `tank-bear-hold-three.json` | holds 3 mobs (`<= 0.4`) | `?` |
| `tank-bear-no-growl-tank-player.json` | no Growl on a mob a player tank holds (control) + bot-fought guard | PASS / PASS |
| `tank-bear-add-pickup.json` | picks up the scheduled refill add (trigger `t_from` 55) | `?` |
| `tank-bear-bash-caster.json` | Bash interrupt (60 s cooldown, min_n 3) + form precondition | `?` / PASS |
| `tank-bear-defensives.json` | Frenzied Regeneration at `src_hp_le` 50 + 30 s opportunity under 50 | `?` / PASS |
| `tank-paladin-taunt-off-healer.json` | Hand of Reckoning latency | `?` |
| `tank-paladin-hold-three.json` | holds 3 mobs (`<= 0.4`) | `?` |
| `tank-paladin-no-taunt-tank-player.json` | no Reckoning on a mob a player tank holds (control) + bot-fought guard | PASS / PASS |
| `tank-paladin-add-pickup.json` | picks up the scheduled refill add (trigger `t_from` 55) | `?` |
| `tank-paladin-hoj-caster.json` | Hammer of Justice stun (60 s cooldown, min_n 3) | `?` |
| `tank-paladin-defensives.json` | Holy Shield rate + Bulwark count + RF precondition | PASS / `?` / PASS |
| `tank-warrior-pack-spread.json` | Thunder Clap count + 3-mob share (engage rule removed) | PASS / `?` |
| `tank-warrior-defensives.json` | Shield Wall (low hp) + Last Stand (critical hp), 30 s opportunity under 50 | `?` / `?` |
| `tank-warrior-shield-bash-caster.json` | Shield Bash interrupt (12 s cooldown) + stance precondition | `?` / PASS |
| `tank-warrior-no-taunt-tank-player.json` | no Taunt/Mocking Blow/Challenging Shout on a mob a player tank holds (control) + bot-fought guard | PASS / PASS |
| `tank-warrior-add-pickup.json` | picks up the scheduled refill add (trigger `t_from` 55) | PASS |

Dropped while writing: `tank-warrior-hold-single`, `tank-paladin-hold-single` (the 1-mob outcome
is the existing `TNK-WAR-MOBS-ON-TANK` / `TNK-PAL-MOBS-ON-TANK` ratio in the taunt/RF trials) and
`tank-warrior-hold-three` (the 3-mob outcome is `TNK-WAR-PACK-SHARE` in the pack trial). Design
limits found: HoJ/Bash need `min_n` 3, not 5 (a 60 s cooldown answers at most 3 casts per 180 s
repeat); `victim: bot` starts no fight (nothing pulls), so the defensives trials keep
`victim: player` and gate on the bot's own hp (`src_hp_le`).

Round 1 removals (need a new kind): `tank-warrior-cc-respect`, `tank-bear-cc-respect`,
`tank-paladin-cc-respect` always PASSed — nothing in them casts Polymorph (a tank bot plus a priest
extra), so the "damage on a sheeped mob == 0" count is 0. A CC-respect trial for a tank needs a
CC-casting extra bot that `cc moon` targets.

Round 1 fixes: defensives rules carry an `opportunity` block (unit `$bot`, `hp_lt` 50, in combat)
with `min_opportunity_s` 30, so a rep where the bot never got that low is INCONCLUSIVE; the
add-pickup triggers gate on `t_from` 55 (the `refill_every_s` 60 add lands about a minute into the
fight — the real spawn time in `trl/live/situations.py`, not the first mob's hits); the
no-taunt controls carry a bot-fought precondition (5+ casts on the mob) and the warrior control
counts Mocking Blow and Challenging Shout too; `tank-warrior-shield-bash-caster` is `?` (no baseline
proves the bot reaches melee on a caster) with the 12 s Shield Bash cooldown in text and responder
debounce; `TNK-WAR-PACK-SHARE` cites `ProtectionWarriorStrategy.cpp:344-355` and says
"at abb91ff62" once in every changed reason.

## Edge cases and variants (2026-11-10, no live runs)

`trials/edge/` holds the edge-case trials; variants (README "Variants") carry the level and group variations of
existing trials. Every rule cites TortoiseBots `upstream/dev` at `0c56cf649` and carries `expect_today`. Offline tests:
`tests/test_edge_trials.py` (every rule scored PASS and FAIL through `score_rep`) and `tests/test_variants.py`.

| id | behaviour | rules (expect_today) | code |
|---|---|---|---|
| `tank-warrior-two-bot-tanks` | a Protection warrior bot never taunts a mob a second bot tank (Protection paladin, `extra1`) holds | no taunt on a mob targeting `$extra1`, after 10 s and outside 8 s after a refill (PASS); the paladin took 5 hits (PASS, precondition); the warrior fought; stance | `GenericTriggers.cpp:326-363` (LoseAggro: the victim IsTank -> false), `AttackerOfNonTankPolicy.h:34-38`, `AttackerOfNonTankTargetValue.cpp:33-44`, `PlayerbotAI.cpp:4220-4241` |
| `tank-warrior-no-taunt-tank-player@bot-offtank` | the player-tank control with a second, paladin bot tank in the group | the base rules (PASS) | as the control; `PlayerbotAI.cpp:4220-4241` |
| `priest-holy-low-mana-drinks` (+ `@mana-5`, `@mana-50`) | a healer low on mana drinks between pulls (ooc window) | Drink aura within 30 s (PASS); Drink uptime >= 25 percent (PASS) | `UseFoodStrategy.cpp:49-54`, `GenericTriggers.cpp:81-86` (under 65), `MaintenanceValues.h:295-345` (stop line 85), `UseItemAction.h:692-774` |
| `priest-holy-low-mana-fight` | at 15 percent mana in a fight: no off-spec nuke under 60 percent mana, still heals | no Smite/Holy Fire/Mind Blast/SW:P (PASS); at least 3 heals on the player (`?`) | `HealerManaPolicy.h:55-69,103-130`, `GenericTriggers.cpp:205-224` |
| `priest-holy-low-mana-potion` | with mana potions in the bags the healer uses one | `Restore Mana` cast within 30 s (PASS); at least 3 heals (PASS) | `UsePotionsStrategy.cpp:49-67`, `UseItemAction.h:173-176` |
| `tank-warrior-zero-rage-opening` | a Protection warrior starting the pull with no rage opens with Bloodrage and melee, no stall | Bloodrage within 10 s (`?`); damage on the mob within 8 s (PASS); rage under 30 percent at the first hits (PASS, precondition); stance | `ProtectionWarriorStrategy.cpp:39-42,236-246` |
| `dps-rogue-low-energy` | a Combat rogue keeps swinging with its energy spent | at least 8 damage events at under 25 percent energy (PASS); at least 20 damage events a minute (PASS); stance | `RogueStrategy.cpp:72-75,88-90`, `CombatRogueStrategy.cpp:12-45`, `GenericTriggers.cpp:316-319`, `UseItemAction.h:402-412` |
| `dps-rogue-thistle-tea` | a level 60 Combat rogue with Thistle Tea uses it when under 20 energy | `Restore Energy` cast within 90 s (`?`); the same activity rule; stance | `RogueStrategy.cpp:88-90`, `GenericTriggers.h:65-74`, `UseItemAction.h:402-412` |

Level variants (README, DECISIONS 2026-11-10): `priest-holy-heal-latency@lvl50|lvl60`,
`tank-warrior-taunt-off-healer@lvl50|lvl60`, `dps-mage-threat-cap@lvl50|lvl60`. Level 20 is refused: the premade build
of levels 20 to 30 does not carry the marker talent the `bot_talents` proof needs.

### Needs a new kind (not written, nothing invented)

1. **The tank dies mid-fight: does a bot off-tank or a DPS pick the mob up?** No situation kills a tank on a
   schedule. What it would take: a situation field such as `drop_tank_at_s: N` with `tank: "bot"|"extraN"` that sends
   the guarded `.modify hp 1 <max>` to a hire (the guard already allows `modify_hp` on a hire; the governor uses it)
   once, N s into the fight, and a marker `tank_down` (a runner mark, like `refill`) the rules can use with
   `not_within_s_after_mark` or `t_from`. The rule side: a latency trigger on the new `death` event kind (`death` is
   in `KIND_ENUM` but not in `EVENT_TRIGGER_KINDS`) with a response of any taunt cast by the surviving tank
   (`dst: $trigger.src` does not fit a death trigger; it would need `response_dst` of the mob that was on the
   victim) or damage by a DPS on that mob. Expected today `?`: with a second bot tank,
   `ProtectionWarriorStrategy.cpp:126-138` (lose aggro and party member attacked, INTERRUPT + 1) says the off-tank taunts
   within a tick; with no second tank the mob goes to the healer (`AttackerOfNonTankPolicy.h:45-71` ranks a healer
   victim first) and nobody holds it, which is the finding.
2. **The bot under control: stunned, silenced, feared or rooted. Does it use its break-free and resume within N s?**
   Three gaps, in the order they bite:
   - **The CC cannot be aimed at the bot.** The `debuff` situation applies a GM `.aura` to `situation.victim`, and
     `victim: bot` starts no fight (nothing pulls), so a debuffed hire never fights. What it would take: a
     `debuff.target: "bot"` (the guard's `aura` kind already allows a hire, and lists the ids the trial file names) so the
     player pulls as usual while the aura lands on the hire, re-applied when it expires (the loop already does that
     for the victim). Aura ids on hand in `data/spells.json`: Cheap Shot 1833 (stun), Hammer of Justice 5589
     (stun), Psychic Scream 8122 and Fear 5782 (fear), Frost Nova 122 (root), Silence 15487.
   - **Mob data cannot name a mob that casts them.** `data/mobs.json` rows hold entry, faction, level range and name;
     `build_data.py` reads the creature spell tables only for the interruptible-caster flag, and the situation picks a
     mob by role and level window, not by entry. A real "the mob stuns the bot" trial needs `build_data.py` to record
     the CC mechanic each creature casts, and `mobs.cc: "stun|fear|root|silence"` or a `mobs.entry` pin (and the mob
     must pick the bot as its target, which the player's pull does not do). The GM aura above is the cheaper route
     and tests the bot, not the mob.
   - **The race of the hire is not chosen.** The break-free is a racial: Will of the Forsaken 7744 (fear, charm, sleep;
     `GenericTriggers.h:1340-1362`) and Escape Artist 20589 (rooted; `GenericTriggers.h:1386`), both at priority 71 in
     `RacialsStrategy.cpp:27-33`; a Berserker Rage row for warriors against fear/sleep/sap
     (`FearSleepSapTrigger`, `GenericTriggers.h:1364-1372`). A trial needs `bot.race` (or a setup check that reads
     the hire's race and makes the rep INVALID when it has no break-free), or it measures only the resume.
   The rules need nothing new: latency with trigger `aura_on` of the CC aura on `$bot` and response a cast of the
   racial within 3 s, and latency with trigger `aura_off` and response any cast or damage by `$bot` within N s
   (both kinds exist). Expected today: `?` for the racial (the rows exist; whether the hire's race has one is the
   open part), `?` for the resume.
3. **A variant of `tank-warrior-two-bot-tanks` with the player as the other tank.** The count rule names its alias
   (`dst_target_of: $extra1`), and `dst_target_of` takes one alias, so the same rules cannot read "the other tank" as the
   player in a variant (the rules list is not variable). What it would take: `dst_target_of` accepting a list of aliases
   or a `$tanks` alias for every forced or hired tank other than the bot. Covered today by
   `tank-warrior-no-taunt-tank-player` and its `bot-offtank` variant.
4. **Rage and energy cannot be set.** `bot.mana_pct` runs `.modify mana`, which the guard allows for a hire and only
   for mana. The rage and energy trials wait for the natural empty state (rage decays out of combat, `pull_after_s` 20;
   energy empties in seconds) and prove it with a rule. A `bot.power_pct` for rage and energy needs a guarded
   `.modify rage` / `.modify energy` kind and a `bot_power` setup check.
5. **Level 20 hires.** A talent proof by marker per level band (today the marker is the level-35 premade's), so a level
   20 to 30 hire can be proven: the table in `trl/talents.py` would hold one marker per band, from the premade link of
   that band.

## Tool findings while writing these

- `trl/rules.py` `DEFAULT_CC_AURAS` (read by `marked_has_cc`) had wrong and missing ids (Leatherworking,
  Hunter's Mark and Multi-Shot ids; no Polymorph ranks 2-4). Fixed in commit `5fa9e61`.
- The interrupt trials need caster mobs at the player's level, and `data/mobs.json` has few: at level 40
  (window 40-42) only one named caster fits. More caster entries would let the interrupt trials run at 40
  next to the rest.
