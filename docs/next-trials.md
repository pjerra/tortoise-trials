# Next trials: tanks and DPS

Design notes for the trials after the healer set. All code lines are from TortoiseBots at commit
`2fdf7da8c` (paths are relative to the TortoiseBots repository). The six trials marked **written** are in
`trials/tanks/` and `trials/dps/`, with offline tests in `tests/test_rules_tanks_dps.py`. The others need
either a control file only, or a situation feature that does not exist yet (listed at the end).

## How the list is ranked

1. Is there a code path that probably makes the bot do the wrong thing today (a likely real bug)?
2. Do walks depend on it (a missed taunt or interrupt kills the healer and wipes the pull)?
3. Is there a known complaint or a recent fix that could regress?

## Ranked list

| # | trial | bot | situation | status | why it ranks here |
|---|---|---|---|---|---|
| 1 | `dps-rogue-kick-caster` | Combat rogue | `caster_mob` (exists) | **written** | Kick inherits the single-target threat type, so the party threat back-off can veto it exactly when the rogue is near the tank's threat |
| 2 | `dps-mage-counterspell-caster` | Arcane mage | `caster_mob` (exists) | **written** | same veto on Counterspell; the wand fallback (#922) keeps the mage at the cap |
| 3 | `tank-warrior-taunt-off-healer` | Protection warrior | `tank_damage` + priest extra (exists) | **written** | the healer's life in every walk; the taunt row and the tank-player exception are both in one trigger |
| 4 | `dps-mage-threat-cap` | Fire mage | `tank_damage` (exists) | **written** | the back-off stops the nukes but not the wand; does the mob still come off a player tank? |
| 5 | `dps-mage-cc-respect` | Frost mage | `cc_target` (exists) | **written** | sheep breaks wipe dungeon pulls; many recent fixes (AoE interlock, moon hold) could regress |
| 6 | `tank-paladin-righteous-fury` | Protection paladin | `tank_damage` + priest extra (exists) | **written** | a paladin tank without Righteous Fury holds nothing; the paladin taunt row sits at ACTION_MOVE |
| 7 | `tank-bear-growl-maul` | Feral (bear) druid | `tank_damage` + priest extra (exists) | not written | Growl at ACTION_MOVE; Maul only as the fallback of a Mangle row |
| 8 | `tank-warrior-pack-spread` | Protection warrior | `pack`, 3 mobs (exists) | not written | loose adds on the healer are the common wipe; Thunder Clap priority changed in #949 |
| 9 | `tank-warrior-shield-block` | Protection warrior | needs **F1** (bot shield check) | needs feature | Shield Block needs a shield; today nothing proves the hire holds one |
| 10 | `dps-hunter-feign-death` | Marksmanship hunter | needs **F2** (aggro on the bot) | needs feature | FD on aggro was just changed (`2fdf7da8c`); the pet and the player's threat get in the way |
| 11 | `dps-threat-ratio-80` | any DPS | needs **F3** (threat samples) | needs feature | the 80 percent number itself; trials 4 and 1-2 only see its effects |

Common set-up for all of them:

- The player's role is **forced** (`.bot role self tank|dps`, setup check `player_forced_role`).
  `PlayerbotAI::IsTank` reads the forced role first (`ai/playerbot/PlayerbotAI.cpp:3986-3990`), so neither
  a stance read nor the second-tank rule (`AiFactory.cpp:446-471`, #1553) can flip what the bot thinks the
  player is. The stance is still held and scored as a precondition (`*-STANCE` uptime rules).
- Every trial hires a **Holy priest as `extra1`**. A GM-levelled warrior with no healer hits the rescue cap
  inside a minute (the `paladin-holy-heal-latency` runs, see DECISIONS.md), and a tank bot has no self-heal.
  In the tank trials the priest is also the "healer the mob hits".
- Groups: tanks and the rogue use `tanks-melee`, mages `ranged` (the folder is `tanks/` or `dps/`).
- Mob tables: the `caster` table has two entries that can spawn in the level 50 window (50-52), the `humanoid` table
  four in the level 45 window; level 40 has almost none of either, so the interrupt trials run at 50 and
  the sheep trial at 45.

## 1. `dps-rogue-kick-caster` (written)

- **Situation:** `caster_mob`, one caster mob with refill, level 50; player warrior in Defensive Stance,
  forced tank; Combat rogue hire; priest extra. Setup check `mob_caster_casts`.
- **Rules:**
  - `DPS-ROG-KICK`: `latency`, trigger `cast_start` by `$mob` with `spell_interruptible`, response `cast` by
    `$bot` of Kick or Kidney Shot on the casting mob (`response_dst: $trigger.src`), `within_s` 3,
    `trigger_debounce_s` 10 (Kick's cooldown); `pass_if >= 0.6`, `min_n` 3.
  - `DPS-ROG-KICK-STANCE`: player Defensive Stance uptime `>= 0.9` (precondition).
- **Code:** `strategy/rogue/RogueStrategy.cpp:373-391` (RogueCcStrategy: kick at ACTION_INTERRUPT + 1),
  `:24` (kick -> kidney shot), `strategy/rogue/RogueTriggers.h:9`, `strategy/rogue/RogueActions.h:111-115`
  (CastKickAction is a CastMeleeSpellAction), `strategy/actions/GenericSpellActions.h:39`
  (ACTION_THREAT_SINGLE), `strategy/generic/ThreatStrategy.cpp:60-73` (the high-threat veto),
  `AiFactory.cpp:711-727` (rogues get `cc`), `AiFactory.cpp:473-489,842-850` (non-tank bots under a real
  master get `threat`), `strategy/triggers/GenericTriggers.cpp:936-939`, `PlayerbotAI.cpp:6541-6589`.
- **expect_today: `?`.** With no back-off the kick row outranks the whole rotation and passes. Under a
  player tank the rogue has the `threat` strategy, its white swings keep it near 80 percent of an ungeared
  tank's threat, and the veto scores Kick 0 while "high threat" holds. FAIL here with a PASS on the control
  points at the veto.
- **Control:** the same trial with the player in Battle Stance forced to `dps`: no tank in the group, so
  `ThreatValue` returns 0 (`strategy/values/ThreatValues.cpp:141-142`) and nothing is vetoed; expected PASS.

## 2. `dps-mage-counterspell-caster` (written)

- **Situation:** as trial 1, Arcane mage hire.
- **Rules:** `DPS-MAG-COUNTERSPELL`: as `DPS-ROG-KICK` with Counterspell, `trigger_debounce_s` 30;
  `pass_if >= 0.6`, `min_n` 3. `DPS-MAG-CS-STANCE`: precondition.
- **Code:** `strategy/mage/MageStrategy.cpp:428-453` (MageCcStrategy: counterspell at ACTION_INTERRUPT + 1),
  `strategy/mage/MageTriggers.h:113`, `strategy/mage/MageActions.h:153-154` (plain BEGIN_SPELL_ACTION),
  `AiFactory.cpp:551-567`, `ThreatStrategy.cpp:60-73`, `GenericSpellActions.h:464-468` (Shoot is threat
  type NONE).
- **expect_today: `?`.** Same veto as Kick. Worse: the vetoed nukes fall back to the wand on purpose
  (#922), and the wand is never vetoed, so the mage stays at the cap the longer it fights.
- **Control:** the no-tank variant, expected PASS.

## 3. `tank-warrior-taunt-off-healer` (written)

- **Situation:** `tank_damage`, one melee mob with refill, level 40; the player (Arms warrior, Battle
  Stance, forced `dps`) pulls every mob, so each mob opens on a non-tank; Protection warrior hire; priest
  extra (heal threat pulls fresh mobs onto the healer too).
- **Rules:**
  - `TNK-WAR-TAUNT`: `latency`, trigger `damage` from `$mob` on `$player` or `$extra1`, response Taunt by
    `$bot` on that mob within 5 s, `trigger_debounce_s` 12; `pass_if >= 0.7`, `min_n` 3.
  - `TNK-WAR-MOBS-ON-TANK`: `ratio`, mob damage on the player or the healer over all mob damage;
    `pass_if <= 0.3`, `min_n` 20.
  - `TNK-WAR-DEF-STANCE`: bot Defensive Stance (71) uptime in combat `>= 0.85`.
  - `TNK-WAR-TAUNT-STANCE`: player Battle Stance precondition.
- **Code:** `strategy/warrior/ProtectionWarriorStrategy.cpp:115-117` (lose aggro -> taunt, INTERRUPT + 1),
  `:26`, `:61-63` (stance row), `:203-208` (overpower dance), `strategy/triggers/GenericTriggers.cpp:289-320`
  (LoseAggroTrigger; the tank-player exception at `:299-316`), `PlayerbotAI.cpp:3986-4007`.
- **expect_today:** Taunt `PASS` (the victim is no tank, so the row fires above every DPS row); ratio `?`
  (an outcome; the player always eats the opening hits); stance `PASS`.
- **Control:** `tank-warrior-taunt-tank-player-control`: the player in Defensive Stance forced to `tank`
  and taunting (the runner casts Taunt every 6 s in that stance). Rule: `count` of Taunt casts by `$bot`
  with `dst_target_of: $player` `== 0`, because LoseAggroTrigger returns false when the mob's victim is a
  tank player (`GenericTriggers.cpp:302-311`). Expected PASS. No new feature needed.

## 4. `dps-mage-threat-cap` (written)

- **Situation:** `tank_damage`, one melee mob with refill, level 40; player warrior Defensive Stance,
  forced tank; Fire mage hire; priest extra. `opportunity`: the player in combat.
- **Rules:**
  - `DPS-MAG-THREAT-NO-AGGRO`: `count` of mob damage on `$bot` `== 0`, `min_opportunity_s` 90.
  - `DPS-MAG-THREAT-DAMAGE`: precondition, at least 3 fire nukes on the mobs (an idle mage would pass the
    rule above for the wrong reason).
  - `DPS-MAG-THREAT-STANCE`: precondition.
- **Code:** `strategy/generic/ThreatStrategy.cpp:11-75`, `strategy/triggers/GenericTriggers.cpp:640-669`
  (80 percent with a 5 s look-ahead), `strategy/values/ThreatValues.cpp:78-159`,
  `AiFactory.cpp:473-489,842-850`, `strategy/mage/FireMageStrategy.cpp:27,34-48`, commit `320aa2906` (#922).
- **expect_today:** no-aggro `?` (the wand keeps adding threat while the nukes are vetoed; whether it
  reaches the 130 percent a ranged attacker needs against an ungeared tank is the question); damage `PASS`.
- **Control:** the player in Battle Stance forced `dps`: no tank, no veto, the mage pulls; rule
  `count >= 1`. Expected PASS.

## 5. `dps-mage-cc-respect` (written)

- **Situation:** `cc_target`, two humanoid mobs, level 45; the runner marks the second mob moon and sends
  `.bot cc moon <bot>` (setup check `cc_mark_set`); the player tanks the first mob; Frost mage hire;
  priest extra.
- **Rules:**
  - `DPS-MAG-CC-RESPECT`: `count` of `damage` from `$bot` on `$marked` while the marked mob carries any
    Polymorph rank (`dst_has_aura` 118, 12824, 12825, 12826, 28271, 28272) `== 0`. Periodic and splash
    damage count (Frost Nova, Blizzard, Cone of Cold).
  - `DPS-MAG-CC-POLYMORPH`: precondition, at least one Polymorph by `$bot` on `$marked`.
- **Code:** `strategy/values/PossibleAttackTargetsValue.cpp:49-101,127-168`,
  `strategy/triggers/GenericTriggers.cpp:710-738` (AoE interlock),
  `strategy/actions/GenericSpellActions.cpp:24-33` (BreaksCcInArea), `strategy/mage/MageStrategy.cpp:444-446`,
  commits `180024d15`, `9ccede38a`, `bf9c45077`.
- **expect_today:** both `PASS`.
- **Control:** `pack` of two humanoids with no mark: the mage's damage on the second mob is `>= 1`
  (shows the event path sees mage damage on that mob). No new feature needed.

## 6. `tank-paladin-righteous-fury` (written)

- **Situation:** as trial 3 with a Protection paladin hire.
- **Rules:**
  - `TNK-PAL-RIGHTEOUS-FURY`: bot aura 25780 uptime in combat `>= 0.95`.
  - `TNK-PAL-MOBS-ON-TANK`: the ratio of trial 3.
  - `TNK-PAL-RF-STANCE`: player precondition.
- **Code:** `strategy/paladin/ProtectionPaladinStrategy.cpp:241-262` (Righteous Fury rows in and out of
  combat), `strategy/paladin/PaladinTriggers.h:66`, `strategy/paladin/PaladinActions.h:59`,
  `strategy/paladin/PaladinAiObjectContext.cpp:75-77`; taunt `ProtectionPaladinStrategy.cpp:18,46-49`.
- **expect_today:** Righteous Fury `PASS`; ratio `?` (Hand of Reckoning at ACTION_MOVE, its alternative
  Righteous Defense is not a 1.12 spell; compare with trial 3).
- **Control:** a Retribution paladin hired as `dps`: Righteous Fury uptime `== 0` (the rows live only in
  the protection buff strategy).

## 7. `tank-bear-growl-maul` (no feature needed, not written)

- **Situation:** as trial 3 with a feral druid hired as tank (`spec: bear`), level 40 (Bear Form; Dire
  Bear Form at 40 needs the rank learned).
- **Rules:** `TNK-DRU-GROWL` latency as `TNK-WAR-TAUNT` with Growl, debounce 12, `>= 0.7`;
  `TNK-DRU-MAUL` `rate` of Maul casts by `$bot` `>= 4` per minute; `TNK-DRU-FORM` uptime of 5487 or 9634
  `>= 0.9`.
- **Code:** `strategy/druid/TankFeralDruidStrategy.cpp:58-60` (lose aggro -> growl at ACTION_MOVE),
  `:24` (mangle (bear) -> maul), `:66-68` (medium rage available -> mangle (bear), NORMAL + 1),
  `:33-35` (default action melee).
- **expect_today:** Growl `?` (ACTION_MOVE is far below the warrior's INTERRUPT + 1; anything at HIGH wins
  the tick first); Maul `?` (Maul only fires as the alternative of a Mangle row; it depends on Mangle being
  unknown on this core); form `PASS`.
- **Control:** the forced-tank player control of trial 3 with the bear.

## 8. `tank-warrior-pack-spread` (no feature needed, not written)

- **Situation:** `pack`, three melee mobs with refill, level 40 with `level_delta` -2; the player attacks
  every mob, so all three open on him.
- **Rules:** `TNK-WAR-PACK-ENGAGE`: `latency`, trigger mob damage on `$player`/`$extra1` (debounce 8 per
  mob), response any `cast` by `$bot` on that mob within 6 s, `>= 0.7`; `TNK-WAR-PACK-CLAP`: `count` of
  Thunder Clap `>= 1`; the ratio rule of trial 3 at `<= 0.4`.
- **Code:** `ProtectionWarriorStrategy.cpp:143-145` (melee light aoe -> thunder clap HIGH + 7, #949),
  `:115-117`; `docs/concepts/bot-mechanics-and-quirks.md` section 1 (TankTargetValue buckets loose mobs
  first).
- **expect_today:** `?` for all (one taunt per 10 s cannot cover three loose mobs; the clap carries it).
- **Control:** the same with one mob: the clap count `== 0`.

## 9. `tank-warrior-shield-block` (needs F1)

- **Rules:** `rate` of Shield Block casts by `$bot` in combat `>= 3` per minute; uptime of aura 2565
  is low by design (5 s), so the rate is the rule.
- **Code:** `ProtectionWarriorStrategy.cpp:127-129` (shield block HIGH + 2), `strategy/warrior/WarriorTriggers.h:10`.
- **expect_today:** `PASS` if the hire holds a shield, which nothing proves today.
- **Control:** a Protection warrior hire with the shield removed: count `== 0`.

## 10. `dps-hunter-feign-death` (needs F2)

- **Rules:** `latency`, trigger mob damage on `$bot` (the hunter), response Feign Death by `$bot` within
  3 s, `>= 0.6`, `min_n` 3.
- **Code:** `strategy/hunter/HunterStrategy.cpp:41` (feign death threat node), `:113-115` (medium threat
  -> feign death threat, ACTION_HIGH), `:236-238` (high threat -> ACTION_EMERGENCY, raid strategy),
  `:322-324` (remove feign death once it is up), `strategy/hunter/HunterTriggers.h:10`, commit `2fdf7da8c`
  (no Feign Death while the hunter is the only unit on the mob's threat list).
- **expect_today:** `?`. Feign Death is driven by the threat triggers, not by being hit: it needs a tank
  in the group (no tank, `ThreatValue` is 0 and nothing fires), and a mob on the hunter reads as 100
  percent or more of the tank's threat.
- **Control:** a mob whose threat list holds only the hunter: Feign Death count `== 0` (the new rule).

## 11. `dps-threat-ratio-80` (needs F3)

- **Rules:** while the bot casts damage spells, its threat on the mob over the tank's threat stays below
  0.9 in at least 95 percent of the samples.
- **Code:** `GenericTriggers.cpp:640-669`, `ThreatValues.cpp:78-159`.
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

## Tool findings while writing these

- `trl/rules.py:13` `DEFAULT_CC_AURAS` (read by `marked_has_cc`) has wrong and missing ids. Wrong: 2108
  (Leatherworking), 9035 (Hex of Weakness), 14325 (Hunter's Mark), 17826 (Increase Arcane Dam 30), 18651
  (Multi-Shot). Missing: Polymorph ranks 2-4 (12824, 12825, 12826), Sap ranks 2-3 (2070, 11297),
  Hibernate (2637, 18657, 18658), Freezing Trap Effect (3355, 14308, 14309). A mage above level 19 casts
  a rank the list does not know, so a `marked_has_cc` rule would never match. `dps-mage-cc-respect` uses
  `dst_has_aura` with every Polymorph rank instead.
- The interrupt trials need caster mobs at the player's level, and `data/mobs.json` has few: at level 40
  (window 40-42) only one named caster fits. More caster entries would let the interrupt trials run at 40
  next to the rest.
