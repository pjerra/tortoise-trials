# Next trials: tanks and DPS

Design notes for the trials after the healer set. All code lines are from TortoiseBots `upstream/dev` at
`abb91ff62` (paths are relative to the TortoiseBots repository). The trials marked **written** are in
`trials/tanks/` and `trials/dps/`, with offline tests in `tests/test_rules_tanks_dps.py`. The sweep
added 21 tank trials (sweep-tanks); the remaining rows need a situation feature that does not exist
yet (listed at the end).

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

## Sweep-tanks: the 21 new tank trials

Written 2026-11-09, no live runs. Each trial tests ONE behaviour for warrior (protection), druid
(bear) or paladin (protection); every rule cites `ai/playerbot/` code at `abb91ff62` and carries
`expect_today` with the one-line reason. Offline tests in `tests/test_rules_tanks_dps.py` score every
rule PASS and FAIL (or INCONCLUSIVE at `min_n`) through `score_rep`.

| file | behaviour | expect_today |
|---|---|---|
| `tank-bear-growl-maul.json` | Growl latency, Maul rate, Dire Bear Form uptime | `?` / PASS / PASS |
| `tank-bear-hold-single.json` | holds 1 mob (`<= 0.3` of hits on non-tanks) | `?` |
| `tank-bear-hold-three.json` | holds 3 mobs (`<= 0.4`) | `?` |
| `tank-bear-no-growl-tank-player.json` | no Growl on a mob a player tank holds (control) | PASS |
| `tank-bear-add-pickup.json` | picks up a scheduled second mob (refill_every_s 60) | `?` |
| `tank-bear-bash-caster.json` | Bash interrupt (60 s cooldown, min_n 3) + form precondition | `?` / PASS |
| `tank-bear-cc-respect.json` | no Swipe damage on the sheep + Swipe precondition | PASS / PASS |
| `tank-bear-defensives.json` | Frenzied Regeneration at `src_hp_le` 50 + form precondition | `?` / PASS |
| `tank-paladin-taunt-off-healer.json` | Hand of Reckoning latency | `?` |
| `tank-paladin-hold-three.json` | holds 3 mobs (`<= 0.4`) | `?` |
| `tank-paladin-no-taunt-tank-player.json` | no Reckoning on a mob a player tank holds (control) | PASS |
| `tank-paladin-add-pickup.json` | picks up a scheduled second mob | `?` |
| `tank-paladin-hoj-caster.json` | Hammer of Justice stun (60 s cooldown, min_n 3) | `?` |
| `tank-paladin-cc-respect.json` | no Consecration damage on the sheep + Consecration precondition | PASS / PASS |
| `tank-paladin-defensives.json` | Holy Shield rate + Bulwark count + RF precondition | PASS / `?` / PASS |
| `tank-warrior-pack-spread.json` | engage latency, Thunder Clap count, 3-mob share | `?` / PASS / `?` |
| `tank-warrior-defensives.json` | Shield Wall (low hp) + Last Stand (critical hp) | `?` / `?` |
| `tank-warrior-shield-bash-caster.json` | Shield Bash interrupt + stance precondition | PASS / PASS |
| `tank-warrior-no-taunt-tank-player.json` | no Taunt on a mob a player tank holds (control) | PASS |
| `tank-warrior-add-pickup.json` | picks up a scheduled second mob | PASS |
| `tank-warrior-cc-respect.json` | no Thunder Clap damage on the sheep + Clap precondition | PASS / PASS |

Dropped while writing: `tank-warrior-hold-single`, `tank-paladin-hold-single` (the 1-mob outcome
is the existing `TNK-WAR-MOBS-ON-TANK` / `TNK-PAL-MOBS-ON-TANK` ratio in the taunt/RF trials) and
`tank-warrior-hold-three` (the 3-mob outcome is `TNK-WAR-PACK-SHARE` in the pack trial). Design
limits found: HoJ/Bash need `min_n` 3, not 5 (a 60 s cooldown answers at most 3 casts per 180 s
repeat); `victim: bot` starts no fight (nothing pulls), so the defensives trials keep
`victim: player` and gate on the bot's own hp (`src_hp_le`).

## Tool findings while writing these

- `trl/rules.py` `DEFAULT_CC_AURAS` (read by `marked_has_cc`) had wrong and missing ids (Leatherworking,
  Hunter's Mark and Multi-Shot ids; no Polymorph ranks 2-4). Fixed in commit `5fa9e61`.
- The interrupt trials need caster mobs at the player's level, and `data/mobs.json` has few: at level 40
  (window 40-42) only one named caster fits. More caster entries would let the interrupt trials run at 40
  next to the rest.
