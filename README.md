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
./trials.sh score <run-dir>                          # re-score a finished run, writes card.md/card.json
./trials.sh cleanup <run-dir>                        # remove hires and summons, log out, release leases
./trials.sh compare <run-dir> --baseline <run-dir>
./trials.sh pr-block --before <run-dir> --after <run-dir> [--issue N]
```

`--trial` can repeat. `--class` and `--spec` select trials by the class and spec in the trial file.
`--dry-run` shows the slots it would use and touches nothing. `--parallel` is at most 3 and uses one
slot (own account and character) per worker.

A run folder holds `RUN.json`, one folder per trial and repeat with the raw samples, `events.jsonl`,
`setup.json`, `verdicts.json`, and the `card.md` summary. Verdicts are PASS, FAIL, INCONCLUSIVE
(not enough opportunity or data) or INVALID (a guard failed; the repeat says why). Exit codes:
0 ok, 1 problems, 4 safety refusal, 5 server or RPC error or a leftover summon.

`cleanup` is safe to run twice. It reads the run's ledger and removes what the ledger says is
still there. If a run dies, run `cleanup` on its folder before the next run.

## Trial file format

A trial is one JSON file in `trials/<group>/`. Unknown keys are rejected by `validate`.

- `player`: class, level, talents (tree and spell ids), stance, forced role, auras, items.
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
