# Tortoise Trials

A behaviour test bench for TortoiseBots. A real turtle-agent player plays next to a hired
TortoiseBots bot in a set-up situation (for example: a Holy priest bot, an Arms warrior player in
Defensive Stance, one mob), and a set of rules scores what the bot did. Every rule cites the bot
code it tests (`file:line`) and says what it expects today, so a failing rule points at a line.

Nothing here changes the bots. The tool sets a situation up on a test server, records what
happens, scores it, and compares a run before a fix with a run after it.

## Requirements

- Python 3.11 or newer, standard library only.
- A turtle-agent binary (https://github.com/pjerra/turtle-agent), built with the `lease` and
  `slot setup` commands, on `PATH` as `turtle-agent`, or given with `--binary` or `TRIALS_BINARY`.
- A TortoiseBots test server with the Eluna test toolkit (the runner sends GM and `.bot` commands,
  summons mobs, and reads positions and health through the toolkit).
- A GM account for the player character. The player is the turtle-agent character. The bot is
  hired by the player with the normal `.bot` commands.

Never point this at a live server. The runner asks the binary for its list of test servers and
refuses any other server name (exit 4).

## Configuration

Nothing about your servers is stored in the code. The server is a required argument
(`--server NAME`). The slots (account, character, port) come from turtle-agent's slot table, which
the runner reads with `turtle-agent lease list <server> --json`; see turtle-agent for the file
format and the `--slots FILE` option. The runner leases a free slot, runs `turtle-agent slot setup`
for it, and releases the lease when it is done. A slot that is reserved or already has a listener
is skipped. Slot 3 is never used by default; change that with `TRIALS_RESERVED_SLOTS="5,6"` (or
an empty value).

Environment variables (all optional):

| Variable | Meaning |
|---|---|
| `TRIALS_BINARY` | path to the turtle-agent binary (the `--binary` flag wins) |
| `TRIALS_OUT` | where run folders are written (default `./runs` next to `trials.py`) |
| `TRIALS_RESERVED_SLOTS` | comma list of slot ids never taken (default `3`) |
| `TRIALS_EVPULL` | script `script <run_dir> <server>` that copies the bot event log to `ev-<server>.csv`; without it the bot-side events are skipped |
| `TRIALS_CORE_DIR` | core checkout for `build_data.py` (needs `sql/base`) |
| `TRIALS_DEBUG` | set to `1` to print debug-level lines (one per login from the login pacer) |

`--bind` also calls a `tbdash-get <server> <api path>` command on `PATH` to pull the dashboard's
fight list for the player; without `--bind` nothing like that is used.

Example (placeholder names):

```
export TRIALS_BINARY=$HOME/bin/turtle-agent
./trials.sh run --server my-test-server --trial priest-holy-pws-def-arms --label before
```

The login pacer keeps a small state file at `~/.local/state/tortoise-trials-logins.json` so that
several runs together stay under the realm's login limit. It holds timestamps and account names
only.

## Running, scoring, cleaning up

```
./trials.sh list [--class priest]
./trials.sh validate [files...]                      # check trial files, no server needed
./trials.sh run --server S --trial ID --label before [--repeats N] [--parallel 2] [--dry-run]
./trials.sh score <run-dir>                          # re-score a finished run (`run` already scores at its end)
./trials.sh cleanup <run-dir>                        # remove hires and summons, log out, release leases
./trials.sh compare <run-dir> --baseline <run-dir>
./trials.sh pr-block --before <run-dir> --after <run-dir> [--issue N]
```

`--trial` can repeat. `--class` and `--spec` select trials by the class and spec in the trial file.
`--trial ID@VARIANT` runs one variant of a trial (see "Variants" below), `--trial 'ID@*'` runs all of them;
a bare id, a glob without `@` and `--class` run base trials only.
`--dry-run` shows the slots it would use and touches nothing. `--parallel` is at most 3 and uses one
slot (own account and character) per worker.

A run folder holds `RUN.json`, one folder per trial and repeat with the raw samples, `events.jsonl`,
`setup.json`, `verdicts.json`, and the `card.md` / `card.json` summary (written when `run` ends). Verdicts are PASS, FAIL, INCONCLUSIVE
(not enough opportunity or data) or INVALID (a guard failed; the repeat says why). Exit codes:
0 ok, 1 problems, 4 safety refusal, 5 server or RPC error or a leftover summon.

`cleanup` is safe to run twice. It reads the run's ledger and removes what the ledger says is
still there. If a run dies, run `cleanup` on its folder before the next run.

## Trial file format

A trial is one JSON file in `trials/<group>/`. Unknown keys are rejected by `validate`.

- `player`: class, level, talents (tree and spell ids), stance, forced role, auras, items, and `gear`
  (null or `"tier"`, see below).
- `bot` (the hire) and `extra_bots` (more hires, for example a second tank): class, spec, role,
  level, gear tier, items.
- `situation`: how to make the situation, by `kind` (`tank_damage`, `ooc_hurt`, `ooc_buffs`,
  `cc_target` and others): which mobs, a health band to hold the target in, a floor, a pull delay.
- `setup_checks`: facts that must hold before a repeat counts (the guards below).
- `opportunity`: when the bot had a reason to act (the rules only count then).
- `rules`: each has `id`, `source` (the bot code), `expect_today`, `why_expect`, a `check`
  (metric `count`, `ratio`, `latency`, `uptime`, `first`, `rate`, `walk_metric`), `pass_if`
  (such as `== 0`) and `min_opportunity_s`.

Shortened example (from `trials/healers/priest-holy-pws-def-arms-2tank.json`):

```json
{
  "id": "priest-holy-pws-def-arms-2tank",
  "class": "priest", "spec": "holy",
  "player": {"class": "warrior", "level": 40, "stance": 71,
             "talents": {"tree": "arms", "spells": [16462, 12286]}},
  "bot": {"class": "priest", "spec": "holy", "role": "healer", "level": 40, "gear": "blue"},
  "extra_bots": [{"class": "paladin", "spec": "protection", "role": "tank", "level": 40}],
  "situation": {"kind": "tank_damage", "mobs": {"role": "melee", "count": 1, "refill": true},
                "hp_band": [40, 90], "floor_pct": 12, "victim": "player"},
  "opportunity": {"unit": "$player", "hp_lt": 90, "hp_gt": "$critical", "in_combat": true},
  "rules": [{"id": "PRI-PWS-DEF-ARMS-2TANK", "source": "ai/playerbot/AiFactory.cpp:520-576,597-611",
             "expect_today": "FAIL",
             "check": {"metric": "count", "where": {"kind": "cast", "src": "$bot", "dst": "$player",
                       "spell": "Power Word: Shield", "dst_hp_gt": "$critical", "in_combat": true}},
             "pass_if": "== 0", "min_opportunity_s": 90}]
}
```

### Variants

A trial may carry `variants`: named, small overrides of the base trial, for the same behaviour at another level,
with another kit or another group.

```json
"variants": [
  {"name": "lvl60", "title": "level 60",
   "override": {"level": 60, "player": {"gear": null}, "situation": {"mobs": {"level_delta": -2}}}}
]
```

- `override` may hold `level` (a shortcut: sets `player.level`, `bot.level` and every `extra_bots[].level`),
  `player`, `bot`, `extra_bots`, `situation` and `rules`. Dicts merge key by key (so `{"situation": {"mobs":
  {"level_delta": -2}}}` changes that one number), a list or a scalar replaces what the base has (`extra_bots` is
  replaced whole) and `null` clears a value (`"gear": null`). Explicit fields win over the `level` shortcut.
- `rules` is `{"RULE-ID": {"pass_if": ..., "min_n": ..., "min_opportunity_s": ...}}`: those three thresholds
  are the only part of a rule a variant can change, and only for a rule that exists. A variant never adds,
  drops or rewrites a rule (its `check`, `source`, `expect_today` stay the base's): one behaviour per trial.
- The class, spec and role of the player's class and of the hire cannot change (`player.class`, `bot.class`,
  `bot.spec`, `bot.role`), and a hire (the main bot or an extra) must be level 35 or more: the talent proof
  (`bot_talents`) needs a marker talent that the premade build of level 35 has, and the builds of levels 20 to 30 stop
  short of it for every class (see DECISIONS.md).
- `validate` merges each variant onto the base and runs the ordinary validation on the result, so a bad override
  (a player level with no gear kit, a victim with no extra behind it, an unknown situation field) is refused with the
  variant's name. At most 6 variants per trial; raid trials take none.
- Each variant is its own trial id, `<trial>@<variant>`: its own run folder (`runs/<run>/<trial>@<variant>/`), its
  own `trial.json`, its own rows in `card.md`/`card.json`, and its own key in `compare` and `pr-block` (the baseline
  diff keys on `(trial id, rule id)`, so `x@lvl60` is compared with `x@lvl60` and never folded into `x`). A trial with no
  variants is unchanged. `trials.sh list` shows the variants as their own lines.

### Player gear (`player.gear: "tier"`)

The player is a GM-levelled character with no items, so its threat is far below a real tank's and the
DPS bots sit above 100 percent of it almost all the time: the threat back-off then decides every DPS trial.
`"gear": "tier"` makes the runner grant and equip a fixed kit before the repeat (setup check `player_gear`,
after the level and talents):

- The kit comes from `data/gear_kits.json` (made by `build_data.py` from the public core's `item_template`):
  per class (warrior only so far), per role and per level (40, 45, 50). Role is `tank` when the player's
  `forced_role` is `tank` (plate, a one-handed weapon and a shield) and `dps` otherwise (plate and a
  two-handed weapon, no shield, so the player does not read as a tank). A player level between kits takes the
  highest kit below it, at most 9 levels down. Items are plain vanilla (entry below 25000) plate pieces of
  the highest armor, weapons without a proc or on-use effect, no intellect or spirit, no reputation, honor or
  profession requirement.
- The kit is put on in this order: first the armor and weapon proficiencies its items need are learned (a
  GM-levelled warrior has no Plate Mail 750; Mail is 8737, weapon skills from the kit's item class and
  subclass; cloth needs none; only the player itself, through the guarded `.learn`, from a fixed table in
  `trl/gear.py`); an item already worn stays; one already in the bags (left by an earlier
  repeat or trial) is equipped without a new grant; only an item that is neither is granted with the guarded
  self-only `.additem` (kind `additem_self`: only the kit's entries, only on the player itself; the hire
  `.additem` is unchanged) and equipped with the agent's `equip`. A repeat never piles up duplicates. The
  equip waits for the grant to show in the bags and the read-back waits for the equip to show on the body;
  a piece still off gets its equip sent again (twice at most). A kit item left by another trial (a level-40
  kit before a level-50 trial) is stripped first (setup check `player_gear_carryover`, same guarded `-1`).
- A trial WITHOUT `player.gear` strips first (setup check `player_gear_strip`): the slot character is kept
  between trials, so a gear trial's kit would otherwise stay on the next trial's player (plate and a shield
  fail `player_no_shield` and move every baseline). Every entry of any kit that is worn or in the bags is
  removed with `.additem <id> -1` (the guarded self-only kind again: one copy per line, kit entries only; the
  core takes equipped items away too), then the `equipment` and `inventory` reads must show none left.
  Items that belong to no kit (a character's own starting gear) stay and are counted in the evidence. An agent
  without the reads gives an UNVERIFIED pass for the strip.
- Verified from the agent's `equipment` read: every kit item must be worn, and the evidence in `setup.json` is
  the count and the kit's armor total (the agent has no stats read, so the armor figure is the sum of the worn
  kit items' armor). A class, role or level with no kit fails `validate` and the setup, with the reason; an
  agent without an `equipment` read fails it too instead of passing unverified.

### Rule options for latency and filtered rules

- `check.debounce_responder_s` (latency rules only, a number of seconds > 0, default off): the
  responder's own cooldown. A trigger that arrives less than N s after the bot's last matching
  response (same `response` filter, any target) is skipped: it is not counted and not a miss. Use it
  with the cooldown of the answering ability (for example 10 for Taunt, 30 for Counterspell), so one
  answer is not scored against two triggers. A trigger skipped for the cooldown does not start its
  source mob's `trigger_debounce_s` window. `check.debounce_responder_spells` (a list of response
  spell names, each one of the `response` spells; needs `debounce_responder_s`) limits which responses
  start the cooldown: Taunt has a 10 s cooldown, a Mocking Blow does not start it. `trigger_debounce_s` is different: it thins the
  triggers per source before anything is answered.
- `check.where.not_within_s_after_mark` (`{"kind": "<mark kind>", "s": N}`, only in `check.where`,
  any metric, default off): drop the events and samples within N s after each runner mark of that
  kind (the marks are the ones in `marks.jsonl`; `refill` is written when a refilled mob is
  summoned). A mob refill brings a fresh mob that the bot has not engaged yet, so a rule about
  threat or aggro can leave that window out. Excluded-span rows stay; hp, aura and combat
  lookups still read all samples.

Spell, item and creature ids come from `data/` (built offline from the public core SQL with
`build_data.py`; it never contacts a server). The `Trl*` character names in `data/characters.json`
are the generic names the runner creates on a test server.

## Validity guards

A repeat that fails a guard is INVALID or INCONCLUSIVE, never a pass or a fail:

- **Player visible.** The player must be visible to the bots. A GM that logs in hidden is skipped
  by the bots' party logic, so the runner checks and fails the repeat if the player is hidden.
- **Opportunity.** A rule only counts the time the bot had a reason to act (the `opportunity`
  block, and `min_opportunity_s`). Too little opportunity gives INCONCLUSIVE.
- **Stance and set-up.** The player's stance, talents, forced role, level and shield state, the
  bot's level, role, spec and items, and the group are read back and checked before the repeat
  counts (`setup_checks`). The stance is also scored as an uptime rule where it matters.
- **GM selection confirm.** Before a GM command that acts on a selected unit, the runner waits
  until the server confirms the selection is that unit. If it is not confirmed the run stops with
  exit 5 instead of acting on the wrong unit.
- **Rescue cap and adaptive difficulty.** The runner heals the player when health falls to the
  floor, at most N times a minute. A repeat that ends on the cap is INVALID, and the next repeat of
  that trial runs one step easier (lower mob level, then fewer mobs and no refill), never harder
  again in that run. `situation.adaptive: false` turns this off.
- **Controls.** Control trials keep the same set-up but change the one thing the rule is about (a
  player in Battle Stance, a warrior hired as a bot, no combat). They must still behave as
  expected, which shows the rule is not just always passing or always failing.

## Before and after

Run the same trials against the build without the fix (`--label before`) and with it
(`--label after`), then:

```
./trials.sh pr-block --before runs/<before> --after runs/<after> --issue 1553
```

This prints a Markdown block for a PR description: a table of trial, rule, before and after
verdicts, and the value before and after; an Evidence section with the cited bot code and the
event lines behind each value; and the set-up proof (stance, talents, bot spec). Differences use
Fisher's exact test on the summed k/n of the valid repeats (`compare` prints the same table with
the p value and a note: "differs (p<0.05)", "suggestive (p<0.20)" or "no evidence").

## Example: TortoiseBots #1553 / PR #1557

Issue #1553: a Holy priest shielded an Arms warrior who stood in Defensive Stance. With a second
tank in the group the bot read the warrior as DPS, not tank. Trials `priest-holy-pws-def-arms-2tank`
and `priest-holy-pws-def-arms-ooc` reproduced it, and the before/after block for PR #1557 showed:

- two tanks, in combat: Power Word: Shield on the warrior above the critical band 16 before, 0 after;
- between pulls: 18 before, 0 after;
- controls (the Battle Stance warrior and the warrior-as-bot trial): still shielded, as they should be.

## Known limits

From `FOLLOWUPS.md`:

- `mobs.level_delta: 0` means +1 (seven trial files write 0); decide whether 0 should become a real 0.
- Cast events can carry a stale `dst_hp_pct`; the rows should take health from the nearest sample.
- `ooc_hurt` count rules also count casts before the window opens.
- Extra repeats run only when a repeat was lost to the rescue cap; a setup failure repeats the same way.
- Event and bot-event windows are paired with repeats by position; a repeat without a window
  shifts the pairing.
- The bot-warrior control rejects any off-hand item; it should reject shields only.
- `tests/test_rules_healers.py::TwoTankTrialFilesTest` fails: the 2-tank trial lacks
  `max_rescues_per_min` that the solo trial has.
- Some `why_expect` texts say the runner re-casts the stance; only `ooc_hurt` does.
- The bot-event pull (`TRIALS_EVPULL`) is a hook; this repository does not ship the script.

## Tests

```
python3 -m unittest discover -s tests
python3 -I trials.py validate
```

No server is contacted. Tests that need the bot source tree, the talent DBC files, or the event
script are skipped unless `TRIALS_BOT_SRC`, `TRIALS_DBC_DIR`, `TRIALS_RULE_BRIEFS` or
`TRIALS_EVPULL` is set.

## License

GNU Affero General Public License v3.0 or later, as turtle-agent. See `LICENSE` and `NOTICE`.
