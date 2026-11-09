"""Load and validate trial files. The schema is the contract with briefs 1, 3, 4."""

import glob as _glob
import json
import os
import re

ID_RE = re.compile(r"^[a-z0-9-]+$")
PASS_IF_RE = re.compile(r"^(==|!=|<=|>=|<|>)\s*-?\d+(\.\d+)?$")
GROUPS = {"healers", "tanks-melee", "ranged", "raid"}
SETUP_CHECKS = {
    "player_level", "player_talents", "player_stance", "player_forced_role",
    "player_auras", "bot_present", "bot_level", "bot_spec", "bot_role",
    "bot_items", "bot_mana", "grouped", "mobs_ready", "mob_caster_casts",
    "cc_mark_set", "config_read", "pet_present", "player_gear",
    "walk_report_ok", "boss_fights_present", "practice_marked",
    "extra_bots_present", "extra_bots_role", "extra_bots_in_group",
    # live3: player_visible = the bots saw the player (checked in the fight, required by every
    # non-raid trial); player_no_shield = no off-hand item on the player (Arms/Battle trials);
    # extra_bots_no_shield = the same for the extra_bots hires
    "player_visible", "player_no_shield", "extra_bots_no_shield",
}
# every non-raid trial must list these (the runner INVALIDs a rep that fails one)
REQUIRED_SETUP_CHECKS = ("player_visible",)
EXTRA_ONLY_CHECKS = ("extra_bots_no_shield",)
# the checks an `extra_bots` trial must list (and only such a trial may list)
EXTRA_BOT_CHECKS = ("extra_bots_present", "extra_bots_role", "extra_bots_in_group")
MAX_EXTRA_BOTS = 3
METRICS = {"count", "ratio", "latency", "uptime", "first", "rate", "walk_metric"}
WHERE_KEYS = {
    "kind", "src", "dst", "spell", "spell_id", "dst_hp_gt", "dst_hp_lt",
    "dst_hp_le", "in_combat", "t_from", "t_to", "dst_has_aura",
    "dst_lacks_aura", "src_power_lt", "response_dst", "same_dst",
    "trigger_debounce_s", "src_target_of", "dst_target_of",
    "src_within_of_marked", "marked_has_cc", "spell_interruptible",
    "src_hp_le", "not_within_s_after_mark",
}
# Closed enums (no prefix escape anywhere).
EVENT_TRIGGER_KINDS = {"damage", "aura_on", "aura_off", "cast", "cast_start"}
TRIGGER_KINDS = {"cross"} | EVENT_TRIGGER_KINDS
EXTRA_ALIASES = ("$extra1", "$extra2", "$extra3")
CROSS_UNITS = {"$player", "$bot", "$pet", "$marked"} | set(EXTRA_ALIASES)
# Check kinds that read `where` / `num`+`den` / `trigger`+`response`.
WHERE_METRICS = {"count", "first", "rate"}
RESPONSE_DST_MODES = {"$trigger.src", "$trigger.dst"}
KIND_ENUM = {"cast", "cast_start", "cast_fail", "aura_on", "aura_off",
             "damage", "heal", "death"}
UNIT_ALIASES = {"$player", "$bot", "$pet", "$marked", "$mob"} | set(EXTRA_ALIASES) | {"$extras"}
THRESHOLD_ALIASES = {"$critical", "$low", "$medium"}
# Closed top-level fields (extra keys rejected at every level).
TOP_FIELDS = {"id", "title", "group", "class", "spec", "player", "bot",
              "situation", "setup_checks", "duration_s", "repeats",
              "min_valid_s", "opportunity", "rules", "boss", "live", "spec_ids",
              "extra_bots"}
PLAYER_FIELDS = {"class", "level", "talents", "stance", "forced_role",
                 "auras", "items", "mana_pct", "cc_mark", "gear", "role", "spec"}
BOT_FIELDS = {"class", "spec", "role", "level", "gear", "items", "mana_pct",
              "cc_mark"}
EXTRA_BOT_FIELDS = {"class", "spec", "role", "level", "items"}
EXTRA_ITEM_FIELDS = {"name", "count"}
SIT_FIELDS = {"kind", "mobs", "hp_band", "floor_pct", "victim", "pull_after_s",
              "ooc_wait_s", "debuff", "refill", "roles", "max_rescues_per_min",
              "hurt_every_s", "adaptive"}
TALENTS_FIELDS = {"tree", "spells"}
OPPORTUNITY_FIELDS = {"unit", "hp_lt", "hp_gt", "in_combat"}
MOBS_FIELDS = {"role", "count", "level_delta", "refill", "refill_every_s"}
RULE_FIELDS = {"id", "desc", "source", "expect_today", "why_expect",
               "check", "pass_if", "min_opportunity_s", "min_n"}
CHECK_FIELDS = {"metric", "where", "num", "den", "trigger", "response",
                "within_s", "response_dst", "same_dst", "trigger_debounce_s",
                "debounce_responder_s", "debounce_responder_spells", "per", "min_alive_s", "name", "aura", "unit", "phase", "alive", "triggers", "min_n"}
TRIGGER_FIELDS = (WHERE_KEYS - {"response_dst", "same_dst", "trigger_debounce_s"}) | {"unit", "below"}
RESPONSE_FIELDS = {"kind", "src", "dst", "spell", "spell_id", "in_combat"}
SPEC_IDS = {
    "arms", "fury", "protection", "holy", "retribution", "bm", "mm",
    "survival", "assa", "combat", "subtlety", "discipline", "shadow",
    "hybrid", "ele", "enh", "restoration", "arcane", "fire", "frost",
    "affliction", "demonology", "destruction", "balance", "boomkin",
    "cat", "bear",
}

_tool_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def trial_files(root=None):
    root = root or os.path.join(_tool_dir, "trials")
    out = []
    for path in sorted(_glob.glob(os.path.join(root, "*", "*.json"))):
        if os.path.basename(os.path.dirname(path)) == "_fixtures":
            continue
        out.append(path)
    return out


def load_trial(path):
    with open(path) as f:
        doc = json.load(f)
    if not isinstance(doc, dict):
        raise ValueError("%s: trial is not a JSON object" % path)
    return doc


def _err(errs, msg):
    errs.append(msg)


def _check_dict(doc, allowed, label, trial_id, rid, errs):
    if not isinstance(doc, dict):
        _err(errs, "%s/%s: %s is not an object" % (trial_id, rid, label))
        return False
    ok = True
    for key in doc:
        if key not in allowed:
            _err(errs, "%s/%s: unknown %s field %r" % (trial_id, rid, label, key))
            ok = False
    return ok


def _is_num(val):
    if isinstance(val, bool):
        return False
    if isinstance(val, (int, float)):
        return True
    if isinstance(val, str):
        try:
            float(val)
        except ValueError:
            return False
        return True
    return False


def _num_or_threshold(val):
    return (isinstance(val, str) and val in THRESHOLD_ALIASES) or _is_num(val)


def _check_filter(doc, label, trial_id, rid, errs, spells_path=None,
                  need_kind=False, check_keys=True, allowed=None):
    """One event filter dict (where / num / den / response / event trigger).

    Field names (unless the caller closed them already) AND values: kind enum,
    unit aliases, spell names and ids, numbers or threshold aliases.
    """
    from . import spells as _spells
    where = "%s/%s" % (trial_id, rid)
    if not isinstance(doc, dict):
        _err(errs, "%s: %s is not an object" % (where, label))
        return
    if check_keys:
        for key in doc:
            if key not in (allowed or WHERE_KEYS):
                _err(errs, "%s: unknown %s key %r" % (where, label, key))
    if "not_within_s_after_mark" in doc:
        spec = doc["not_within_s_after_mark"]
        if label != "where":
            _err(errs, "%s: not_within_s_after_mark is only valid in check.where (it filters the whole check)" % where)
        elif (not isinstance(spec, dict) or set(spec) != {"kind", "s"}
              or not isinstance(spec["kind"], str) or not spec["kind"]
              or isinstance(spec["s"], bool) or not isinstance(spec["s"], (int, float)) or spec["s"] <= 0):
            _err(errs, "%s: bad not_within_s_after_mark %r (want {\"kind\": <mark kind>, \"s\": seconds > 0})"
                 % (where, spec))
    if need_kind and "kind" not in doc:
        # A filter without a `kind` matches every event kind: reject it.
        _err(errs, "%s: %s needs a kind" % (where, label))
    kind = doc.get("kind")
    if kind is not None and kind not in KIND_ENUM:
        _err(errs, "%s: bad kind %r" % (where, kind))
    for side in ("src", "dst"):
        val = doc.get(side)
        if val is None:
            continue
        vals = val if isinstance(val, list) else [val]
        for v in vals:
            if not isinstance(v, str):
                _err(errs, "%s: bad %s alias %r" % (where, side, v))
            elif v.startswith("$") and v not in UNIT_ALIASES and v not in (
                    "$trigger.src", "$trigger.dst"):
                _err(errs, "%s: bad %s alias %r" % (where, side, v))
            elif v.startswith("mob:") and not v[4:]:
                _err(errs, "%s: bad %s alias %r" % (where, side, v))
    for key in ("response_dst",):
        mode = doc.get(key)
        if mode is not None and mode not in RESPONSE_DST_MODES:
            _err(errs, "%s: bad %s %r" % (where, key, mode))
    spell = doc.get("spell")
    if spell is not None:
        names = spell if isinstance(spell, list) else [spell]
        for nm in names:
            if not isinstance(nm, str) or not _spells.ids_for(nm, spells_path):
                _err(errs, "%s: unknown spell %r" % (where, nm))
    sid = doc.get("spell_id")
    if sid is not None:
        for one in (sid if isinstance(sid, list) else [sid]):
            if (isinstance(one, bool) or not isinstance(one, int)
                    or not _spells.has_id(one, spells_path)):
                _err(errs, "%s: unknown spell_id %r" % (where, one))
    for key in ("dst_hp_gt", "dst_hp_lt", "dst_hp_le", "src_hp_le", "src_power_lt"):
        val = doc.get(key)
        if val is not None and not _num_or_threshold(val):
            _err(errs, "%s: bad %s %r" % (where, key, val))
    for key in ("t_from", "t_to", "src_within_of_marked", "trigger_debounce_s"):
        val = doc.get(key)
        if val is not None and not _is_num(val):
            _err(errs, "%s: bad %s %r" % (where, key, val))
    for key in ("dst_has_aura", "dst_lacks_aura"):
        val = doc.get(key)
        if val is None:
            continue
        for one in (val if isinstance(val, list) else [val]):
            if isinstance(one, bool) or not isinstance(one, int):
                _err(errs, "%s: bad %s %r" % (where, key, one))


def _check_where(where, trial_id, rid, errs, spells_path=None, need_kind=True):
    if not isinstance(where, dict):
        _err(errs, "%s/%s: where is not an object" % (trial_id, rid))
        return
    _check_filter(where, "where", trial_id, rid, errs, spells_path, need_kind=need_kind)


def _check_numden(doc, label, trial_id, rid, errs, spells_path=None, need_kind=True):
    _check_filter(doc, label, trial_id, rid, errs, spells_path, need_kind=need_kind)


def _check_rule(rule, trial_id, errs, spells_path=None):
    if not isinstance(rule, dict):
        _err(errs, "%s: rule is not an object" % trial_id)
        return
    rid = rule.get("id")
    if not rid or not isinstance(rid, str):
        _err(errs, "%s: rule without an id" % trial_id)
        return
    _check_dict(rule, RULE_FIELDS, "rule", trial_id, rid, errs)
    for key in ("desc", "source", "expect_today", "check", "pass_if"):
        if key not in rule:
            _err(errs, "%s/%s: missing %s" % (trial_id, rid, key))
    if rule.get("expect_today") not in ("PASS", "FAIL", "?"):
        _err(errs, "%s/%s: bad expect_today %r" % (trial_id, rid, rule.get("expect_today")))
    if not rule.get("source"):
        _err(errs, "%s/%s: empty source" % (trial_id, rid))
    check = rule.get("check")
    if not isinstance(check, dict):
        _err(errs, "%s/%s: check is not an object" % (trial_id, rid))
        return
    _check_dict(check, CHECK_FIELDS, "check", trial_id, rid, errs)
    metric = check.get("metric")
    if metric not in METRICS:
        _err(errs, "%s/%s: unknown metric %r" % (trial_id, rid, metric))
        return
    # `kind` is required only where the dict is actually USED by the metric.
    where = check.get("where")
    if where is not None or metric in WHERE_METRICS:
        _check_where(where if where is not None else {}, trial_id, rid, errs,
                     spells_path, need_kind=metric in WHERE_METRICS)
    for label in ("num", "den"):
        if check.get(label) is not None or metric == "ratio":
            _check_numden(check.get(label) if check.get(label) is not None else {},
                          label, trial_id, rid, errs, spells_path,
                          need_kind=metric == "ratio")
    trig = check.get("trigger")
    if trig is not None:
        _check_trigger(trig, trial_id, rid, errs, spells_path)
    resp = check.get("response")
    if resp is not None:
        if _check_dict(resp, RESPONSE_FIELDS, "response", trial_id, rid, errs):
            _check_filter(resp, "response", trial_id, rid, errs, spells_path,
                          need_kind=metric == "latency", check_keys=False)
    debounce = check.get("trigger_debounce_s")
    if debounce is not None and not _is_num(debounce):
        _err(errs, "%s/%s: bad trigger_debounce_s %r" % (trial_id, rid, debounce))
    cd = check.get("debounce_responder_s")
    if cd is not None:
        if metric != "latency":
            _err(errs, "%s/%s: debounce_responder_s only applies to a latency rule" % (trial_id, rid))
        if isinstance(cd, bool) or not isinstance(cd, (int, float)) or cd <= 0:
            _err(errs, "%s/%s: bad debounce_responder_s %r (a number of seconds > 0)" % (trial_id, rid, cd))
    per = check.get("per")
    if per is not None and (metric != "rate" or per != "mob_alive"):
        _err(errs, "%s/%s: bad per %r (only \"mob_alive\", only on a rate rule)" % (trial_id, rid, per))
    min_alive = check.get("min_alive_s")
    if min_alive is not None:
        if per is None:
            _err(errs, "%s/%s: min_alive_s needs per: mob_alive" % (trial_id, rid))
        if isinstance(min_alive, bool) or not isinstance(min_alive, (int, float)) or min_alive <= 0:
            _err(errs, "%s/%s: bad min_alive_s %r (seconds > 0)" % (trial_id, rid, min_alive))
    cds = check.get("debounce_responder_spells")
    if cds is not None:
        if cd is None:
            _err(errs, "%s/%s: debounce_responder_spells needs debounce_responder_s" % (trial_id, rid))
        resp_spells = (resp or {}).get("spell") if isinstance(resp, dict) else None
        resp_spells = resp_spells if isinstance(resp_spells, list) else ([resp_spells] if resp_spells else [])
        if (not isinstance(cds, list) or not cds
                or not all(isinstance(x, str) and x for x in cds)):
            _err(errs, "%s/%s: bad debounce_responder_spells %r (a non-empty list of spell names)"
                 % (trial_id, rid, cds))
        else:
            for name in cds:
                if name not in resp_spells:
                    _err(errs, "%s/%s: debounce_responder_spells %r is not one of the response spells"
                         % (trial_id, rid, name))
    within = check.get("within_s")
    if within is not None and not _is_num(within):
        _err(errs, "%s/%s: bad within_s %r" % (trial_id, rid, within))
    rd = check.get("response_dst")
    if rd is not None and rd not in RESPONSE_DST_MODES:
        _err(errs, "%s/%s: bad response_dst %r" % (trial_id, rid, rd))
    pass_if = rule.get("pass_if")
    if not isinstance(pass_if, str) or not PASS_IF_RE.match(pass_if.strip()):
        _err(errs, "%s/%s: bad pass_if %r" % (trial_id, rid, pass_if))


def _check_trigger(trig, trial_id, rid, errs, spells_path=None):
    if not _check_dict(trig, TRIGGER_FIELDS, "trigger", trial_id, rid, errs):
        return
    kind = trig.get("kind")
    if kind not in TRIGGER_KINDS:
        _err(errs, "%s/%s: bad trigger kind %r" % (trial_id, rid, kind))
        return
    if kind == "cross":
        if trig.get("unit") not in CROSS_UNITS:
            _err(errs, "%s/%s: bad trigger unit %r" % (trial_id, rid, trig.get("unit")))
        if not _num_or_threshold(trig.get("below")):
            _err(errs, "%s/%s: bad trigger below %r" % (trial_id, rid, trig.get("below")))
        return
    # Event trigger: the kind FILTERS the rows; the where-style fields apply.
    for key in ("unit", "below"):
        if key in trig:
            _err(errs, "%s/%s: trigger %s is only for cross triggers" % (trial_id, rid, key))
    _check_filter(trig, "trigger", trial_id, rid, errs, spells_path,
                  need_kind=True, check_keys=False)


def _check_extra_bots(extra, tid, errs):
    """`extra_bots`: at most MAX_EXTRA_BOTS hires beside `bot`, closed fields, bot-like values."""
    if not isinstance(extra, list):
        _err(errs, "%s: extra_bots must be a list" % tid)
        return
    if not extra:
        _err(errs, "%s: extra_bots must not be empty (leave it out)" % tid)
    if len(extra) > MAX_EXTRA_BOTS:
        _err(errs, "%s: extra_bots holds %d bots, at most %d" % (tid, len(extra), MAX_EXTRA_BOTS))
    for i, bot in enumerate(extra, start=1):
        label = "extra_bots[%d]" % i
        if not isinstance(bot, dict):
            _err(errs, "%s: %s is not an object" % (tid, label))
            continue
        for key in bot:
            if key not in EXTRA_BOT_FIELDS:
                _err(errs, "%s: unknown %s field %r" % (tid, label, key))
        for key in ("class", "spec", "role", "level"):
            if key not in bot:
                _err(errs, "%s: %s missing %s" % (tid, label, key))
        if "class" in bot and not ID_RE.match(str(bot.get("class", ""))):
            _err(errs, "%s: bad %s class" % (tid, label))
        if "spec" in bot and bot.get("spec") not in SPEC_IDS:
            _err(errs, "%s: bad %s spec %r" % (tid, label, bot.get("spec")))
        if "role" in bot and bot.get("role") not in ("tank", "healer", "dps"):
            _err(errs, "%s: bad %s role %r" % (tid, label, bot.get("role")))
        if "level" in bot:
            lvl = bot.get("level")
            if isinstance(lvl, bool) or not isinstance(lvl, int) or not 1 <= lvl <= 60:
                _err(errs, "%s: %s level must be an int 1-60" % (tid, label))
        items = bot.get("items")
        if items is not None:
            if not isinstance(items, list):
                _err(errs, "%s: %s items is not a list" % (tid, label))
                continue
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item.get("name"):
                    _err(errs, "%s: %s item needs a name" % (tid, label))
                    continue
                for key in item:
                    if key not in EXTRA_ITEM_FIELDS:
                        _err(errs, "%s: unknown %s item field %r" % (tid, label, key))
                count = item.get("count", 1)
                if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 20:
                    _err(errs, "%s: %s item count must be an int 1-20" % (tid, label))


def _extra_alias_uses(node, found):
    if isinstance(node, dict):
        for val in node.values():
            _extra_alias_uses(val, found)
    elif isinstance(node, list):
        for val in node:
            _extra_alias_uses(val, found)
    elif isinstance(node, str) and node.startswith("$extra"):
        found.add(node)


def validate(trial, spells_path=None):
    """Return a list of error strings (empty = valid)."""
    errs = []
    if not isinstance(trial, dict):
        return ["trial is not an object"]
    tid = trial.get("id", "?")
    if not isinstance(tid, str) or not ID_RE.match(tid):
        _err(errs, "%s: bad id" % tid)
    for key in trial:
        if key not in TOP_FIELDS:
            _err(errs, "%s: unknown top-level field %r" % (tid, key))
    group = trial.get("group")
    if group not in GROUPS:
        _err(errs, "%s: bad group %r" % (tid, group))
        return errs
    if group == "raid":
        for key in ("boss", "live"):
            if key not in trial:
                _err(errs, "%s: raid trial missing %s" % (tid, key))
        if (trial.get("boss") or {}).get("entry") is None:
            _err(errs, "%s: raid trial boss needs an entry" % tid)
        if trial.get("live") not in ("practice", "walk"):
            _err(errs, "%s: raid trial live must be practice|walk" % tid)
        specs = trial.get("spec") or trial.get("class") or ""
    else:
        for key in ("class", "spec", "player", "bot", "situation", "rules"):
            if key not in trial:
                _err(errs, "%s: missing %s" % (tid, key))
        player = trial.get("player") or {}
        if not _check_dict(player, PLAYER_FIELDS, "player", tid, "", errs):
            player = {}
        elif not isinstance(player, dict):
            player = {}
        bot = trial.get("bot") or {}
        if not _check_dict(bot, BOT_FIELDS, "bot", tid, "", errs):
            bot = {}
        elif not isinstance(bot, dict):
            bot = {}
        spec = trial.get("spec")
        if spec not in SPEC_IDS:
            _err(errs, "%s: bad bot spec %r" % (tid, spec))
        if not ID_RE.match(str(trial.get("class", ""))):
            _err(errs, "%s: bad bot class" % tid)
        talents = player.get("talents") or {}
        if not isinstance(talents, dict):
            _err(errs, "%s: player talents is not an object" % tid)
            talents = {}
        else:
            _check_dict(talents, TALENTS_FIELDS, "talents", tid, "", errs)
        tree = talents.get("tree")
        tspells = talents.get("spells")
        try:
            low_level = int(player.get("level") or 99) < 10
        except (TypeError, ValueError):
            low_level = False
        if not isinstance(tspells, list) or (not tspells and not (tree == "none" and low_level)):
            _err(errs, "%s: talents.spells must be a non-empty list (tree none + level < 10 only)" % tid)
        elif any(isinstance(sp, bool) or not isinstance(sp, int) for sp in tspells):
            _err(errs, "%s: talents.spells must be a list of spell ids" % tid)
        stance = player.get("stance")
        if stance is not None and not isinstance(stance, int):
            _err(errs, "%s: player.stance must be an aura id or null" % tid)
        if player.get("forced_role") not in (None, "tank", "healer", "dps"):
            _err(errs, "%s: bad forced_role %r" % (tid, player.get("forced_role")))
        if player.get("gear") not in (None, "tier"):
            _err(errs, "%s: bad player.gear %r (null or \"tier\")" % (tid, player.get("gear")))
        elif player.get("gear") == "tier":
            from . import gear as _gear
            _items, _kit_level, why = _gear.pick_kit(player)
            if not _items:
                _err(errs, "%s: player.gear tier: %s" % (tid, why))
        sit = trial.get("situation") or {}
        if not _check_dict(sit, SIT_FIELDS, "situation", tid, "", errs):
            sit = {}
        elif not isinstance(sit, dict):
            sit = {}
        else:
            if sit.get("kind") not in ("tank_damage", "member_damage", "pack", "cc_target",
                                       "ooc_buffs", "ooc_hurt", "mana_pressure", "caster_mob", "debuff"):
                _err(errs, "%s: bad situation.kind %r" % (tid, sit.get("kind")))
            mobs = sit.get("mobs") or {}
            if isinstance(mobs, dict):
                _check_dict(mobs, MOBS_FIELDS, "mobs", tid, "", errs)
            if mobs.get("role") not in ("melee", "caster", "humanoid"):
                _err(errs, "%s: bad mobs.role %r" % (tid, mobs.get("role")))
            for numkey in ("count", "level_delta"):
                if not isinstance(mobs.get(numkey), int):
                    _err(errs, "%s: mobs.%s must be int" % (tid, numkey))
            refill = mobs.get("refill_every_s")
            if refill is not None and (not isinstance(refill, int) or refill < 5):
                _err(errs, "%s: refill_every_s must be an int >= 5" % tid)
            band = sit.get("hp_band")
            if (not isinstance(band, list) or len(band) != 2
                    or not all(isinstance(x, (int, float)) for x in band)):
                _err(errs, "%s: hp_band must be [low, high]" % tid)
            if not isinstance(sit.get("floor_pct"), (int, float)):
                _err(errs, "%s: floor_pct must be a number" % tid)
            rescues = sit.get("max_rescues_per_min")
            if rescues is not None and (not isinstance(rescues, int) or isinstance(rescues, bool)
                                        or rescues < 1):
                _err(errs, "%s: max_rescues_per_min must be an int >= 1" % tid)
            adaptive = sit.get("adaptive")
            if adaptive is not None and not isinstance(adaptive, bool):
                _err(errs, "%s: adaptive must be true or false" % tid)
            hurt = sit.get("hurt_every_s")
            if hurt is not None and (isinstance(hurt, bool) or not isinstance(hurt, (int, float))
                                     or not 8 <= hurt <= 120):
                _err(errs, "%s: hurt_every_s must be a number from 8 to 120" % tid)
            if sit.get("kind") == "ooc_hurt":
                # no mobs: the victim is hurt by a GM line, out of combat, inside hp_band
                if (isinstance(band, list) and len(band) == 2
                        and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in band)
                        and not 1 <= band[0] <= band[1] <= 99):
                    _err(errs, "%s: ooc_hurt hp_band must be [low, high] with 1 <= low <= high <= 99" % tid)
                wait = sit.get("ooc_wait_s")
                if isinstance(wait, bool) or not isinstance(wait, (int, float)) or wait <= 0:
                    _err(errs, "%s: ooc_hurt needs ooc_wait_s > 0 (the window length)" % tid)
                if mobs.get("count") != 0:
                    _err(errs, "%s: ooc_hurt takes mobs.count 0" % tid)
            victim = sit.get("victim")
            extra_list = trial.get("extra_bots")
            n_extras = len(extra_list) if isinstance(extra_list, list) else 0
            if victim in ("extra1", "extra2", "extra3"):
                if int(victim[-1]) > n_extras:
                    _err(errs, "%s: victim %r has no extra_bots bot behind it" % (tid, victim))
            elif victim not in ("player", "bot"):
                _err(errs, "%s: bad victim %r" % (tid, victim))
            floor = sit.get("floor_pct")
            if isinstance(floor, (int, float)) and not isinstance(floor, bool) and isinstance(band, list) \
                    and len(band) == 2 and all(isinstance(x, (int, float)) for x in band):
                # a floor at or above the band's low edge would rescue the victim the whole time
                if not 0 < floor < band[0]:
                    _err(errs, "%s: floor_pct %s must be above 0 and below the hp_band low %s" % (tid, floor, band[0]))
    for cid in trial.get("setup_checks") or []:
        if cid not in SETUP_CHECKS:
            _err(errs, "%s: unknown setup check %r" % (tid, cid))
    if group != "raid":
        for cid in REQUIRED_SETUP_CHECKS:
            if cid not in (trial.get("setup_checks") or []):
                _err(errs, "%s: setup_checks must list the required check %r" % (tid, cid))
    extra = trial.get("extra_bots")
    n_extra = len(extra) if isinstance(extra, list) else 0
    if "extra_bots" in trial:
        _check_extra_bots(extra, tid, errs)
        have = set(trial.get("setup_checks") or [])
        for cid in EXTRA_BOT_CHECKS:
            if cid not in have:
                _err(errs, "%s: extra_bots needs setup check %r" % (tid, cid))
    else:
        for cid in EXTRA_BOT_CHECKS + EXTRA_ONLY_CHECKS:
            if cid in (trial.get("setup_checks") or []):
                _err(errs, "%s: setup check %r needs extra_bots" % (tid, cid))
    uses = set()
    _extra_alias_uses(trial.get("rules"), uses)
    _extra_alias_uses(trial.get("opportunity"), uses)
    for alias in sorted(uses):
        if alias == "$extras":
            ok = n_extra > 0
        elif alias in EXTRA_ALIASES:
            ok = EXTRA_ALIASES.index(alias) < n_extra
        else:
            ok = False
        if not ok:
            _err(errs, "%s: alias %r has no extra_bots bot behind it" % (tid, alias))
    for numkey in ("duration_s", "repeats", "min_valid_s"):
        if not isinstance(trial.get(numkey), (int, float)):
            _err(errs, "%s: %s must be a number" % (tid, numkey))
    opp = trial.get("opportunity")
    if opp is not None and not isinstance(opp, dict):
        _err(errs, "%s: opportunity is not an object" % tid)
    elif isinstance(opp, dict):
        _check_dict(opp, OPPORTUNITY_FIELDS, "opportunity", tid, "", errs)
        if opp.get("unit") is not None and opp.get("unit") not in CROSS_UNITS:
            _err(errs, "%s: bad opportunity unit %r" % (tid, opp.get("unit")))
        for key in ("hp_lt", "hp_gt"):
            if opp.get(key) is not None and not _num_or_threshold(opp.get(key)):
                _err(errs, "%s: bad opportunity %s %r" % (tid, key, opp.get(key)))
        if opp.get("in_combat") is not None and not isinstance(opp.get("in_combat"), bool):
            _err(errs, "%s: bad opportunity in_combat %r" % (tid, opp.get("in_combat")))
    rules = trial.get("rules")
    if not isinstance(rules, list) or not rules:
        _err(errs, "%s: rules must be a non-empty list" % tid)
        return errs
    for rule in rules:
        _check_rule(rule, tid, errs, spells_path)
    return errs


GLOB_CHARS = "*?["


def match_selector(sel, entries):
    """Paths one selector picks from `entries` ([(trial id, path), ...]).

    An explicit glob (contains * ? or [) may match MANY trials; a bare id must
    match EXACTLY one (ids are never matched as substrings). No match -> [];
    a bare id that two files carry -> ValueError.
    """
    import fnmatch as _fn
    exact = [p for (tid, p) in entries if tid == sel]
    if exact:
        if len(exact) > 1:
            raise ValueError("selector %r matches %d trials; ids must be unique" % (sel, len(exact)))
        return exact
    if any(ch in sel for ch in GLOB_CHARS):
        return [p for (tid, p) in entries if isinstance(tid, str) and _fn.fnmatchcase(tid, sel)]
    return []


def resolve_selectors(selectors, entries, prefix=None):
    """Resolve every selector or fail the whole command (ValueError).

    `prefix` (the --class shorthand) is tried as `<prefix><selector>` when the
    selector itself matches nothing. Nothing is skipped silently.
    """
    out = []
    bad = []
    for sel in selectors:
        hits = match_selector(sel, entries)
        if not hits and prefix:
            hits = match_selector(prefix + str(sel).lower(), entries)
        if not hits:
            bad.append(sel)
        out.extend(hits)
    if bad:
        raise ValueError("selector%s matched no trial: %s (use an exact id or a glob)" % (
            "s" if len(bad) > 1 else "", ", ".join(repr(b) for b in bad)))
    return sorted(set(h for h in out if h))


def find_trials(selector=None, root=None, trials=None, bot_class=None, spec=None, paths=None):
    """Resolve a CLI selector to trial paths."""
    if trials is None:
        paths = trial_files(root)
        trials = []
        kept = []
        for path in paths:
            try:
                trials.append(load_trial(path))
                kept.append(path)
            except (OSError, ValueError):
                continue
        paths = kept
    else:
        paths = paths or []
    entries = []
    for trial, path in zip(trials, paths or [None] * len(trials)):
        entries.append((trial.get("id"), path if path is not None else trial.get("id")))
    if selector:
        sels = selector if isinstance(selector, list) else [selector]
        return resolve_selectors(sels, entries)
    out = []
    for trial, (tid, path) in zip(trials, entries):
        if bot_class and trial.get("class") != bot_class:
            continue
        if spec and trial.get("spec") != spec:
            continue
        out.append(path)
    return sorted(h for h in out if h)
