"""Trials CLI (usage: trials ...).

Subcommands run/cleanup dispatch to trl.live.run (if it is
missing: "live runner not installed (brief trials-1)", exit 2). `raid`
dispatches to trl.raid (brief 4; same fallback). The short form (no
subcommand, --class/--scenario/--trial given) = run.
"""

import argparse
import glob
import json
import os
import sys

TOOL_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOL_ROOT)

from trl import naming  # noqa: E402
from trl import scenario  # noqa: E402


def _all_trials():
    paths = scenario.trial_files()
    trials = []
    kept = []
    for path in paths:
        try:
            trial = scenario.load_trial(path)
        except (OSError, ValueError):
            continue
        trials.append(trial)
        kept.append(path)
        # each valid variant is its own entry, `<trial>@<variant>`, reached as "path@variant"
        for name, merged in scenario.expand_variants(trial):
            trials.append(merged)
            kept.append("%s%s%s" % (path, scenario.VARIANT_SEP, name))
    return trials, kept


def _close_ids(want, trials):
    import fnmatch as _fn
    want = str(want)
    scored = []
    for trial in trials:
        tid = str(trial.get("id") or "")
        if _fn.fnmatchcase(tid, want) or _fn.fnmatchcase(tid, "*%s*" % want):
            scored.append(tid)
    return sorted(scored)[:8]


def resolve_trials(args, trials=None, paths=None):
    """Selected trial paths. Raises ValueError for any bad or unmatched selector
    (the whole command then fails before anything runs)."""
    trials, paths = trials or [], paths or []
    if not trials:
        trials, paths = _all_trials()
    entries = [(trial.get("id"), path) for trial, path in zip(trials, paths)]
    sels = []
    if getattr(args, "trial", None):
        sels.extend(args.trial if isinstance(args.trial, list) else [args.trial])
    if getattr(args, "scenario", None):
        sels.extend(args.scenario if isinstance(args.scenario, list) else [args.scenario])
    klass = getattr(args, "klass", None)
    if klass:
        if sels:
            return scenario.resolve_selectors(sels, entries, prefix=str(klass).lower() + "-")
        return scenario.find_trials(trials=trials, paths=paths, bot_class=klass,
                                    spec=getattr(args, "spec", None))
    if sels:
        return scenario.resolve_selectors(sels, entries)
    return []


def cmd_list(args):
    trials, _ = _all_trials()
    for trial in sorted(trials, key=lambda t: t.get("id") or ""):
        if args.klass and trial.get("class") != args.klass:
            continue
        print("%s [%s/%s] %s" % (trial.get("id"), trial.get("group"),
                                 trial.get("spec"), trial.get("title") or ""))


def cmd_validate(args):
    files = args.files or scenario.trial_files()
    if not files:
        files = sorted(glob.glob(os.path.join(TOOL_ROOT, "trials", "_fixtures", "*.json")))
    seen_rule_ids = {}
    failed = 0
    for path in files:
        try:
            trial = scenario.load_trial(path)
        except (OSError, ValueError) as exc:
            print("%s: cannot load: %s" % (path, exc))
            failed += 1
            continue
        errs = scenario.validate(trial)
        stem = os.path.splitext(os.path.basename(path))[0]
        if isinstance(trial.get("id"), str) and trial.get("id") != stem:
            errs.append("%s: trial id %r != file stem %r" % (
                trial.get("id"), trial.get("id"), stem))
        for rule in trial.get("rules") or []:
            rid = rule.get("id")
            if rid in seen_rule_ids:
                errs.append("%s: duplicate rule id %s (also in %s)" % (
                    trial.get("id"), rid, seen_rule_ids[rid]))
            else:
                seen_rule_ids[rid] = trial.get("id")
        if errs:
            failed += 1
            for err in errs:
                print("%s: %s" % (path, err))
        else:
            print("%s: ok" % path)
    return 1 if failed else 0


def cmd_score(args):
    from trl import score
    run_trials = score.score_run(os.path.abspath(args.run_dir))
    n_fail = sum(1 for t in run_trials for r in (t.get("verdicts") or []) if r.get("verdict") == "FAIL")
    n_invalid = sum(1 for t in run_trials for r in (t.get("verdicts") or []) if r.get("verdict") == "INVALID")
    print("scored %s: %d FAIL, %d INVALID" % (args.run_dir, n_fail, n_invalid))
    if n_fail:
        return 1
    if n_invalid:
        return 3
    return 0


def cmd_compare(args):
    from trl import report
    rows = report.compare(os.path.abspath(args.run_dir), os.path.abspath(args.baseline))
    print(report.render_compare(rows), end="")


def cmd_pr_block(args):
    from trl import report
    print(report.pr_block(os.path.abspath(args.before), os.path.abspath(args.after),
                          args.issue), end="")


def cmd_run(args):
    try:
        from trl.live import run as live_run
    except ImportError:
        print("live runner not installed (brief trials-1)", file=sys.stderr)
        return 2
    if not getattr(live_run, "main", None):
        print("live runner not installed (brief trials-1)", file=sys.stderr)
        return 2
    return live_run.main(args)


def cmd_cleanup(args):
    try:
        from trl.live import run as live_run
    except ImportError:
        print("live runner not installed (brief trials-1)", file=sys.stderr)
        return 2
    if not getattr(live_run, "cleanup", None):
        print("live runner not installed (brief trials-1)", file=sys.stderr)
        return 2
    return live_run.cleanup(args)


def cmd_raid(args):
    try:
        from trl import raid as raid_mod
    except ImportError:
        print("raid trials not installed (brief trials-4)", file=sys.stderr)
        return 2
    return raid_mod.main(args)


def build_parser():
    ap = argparse.ArgumentParser(prog=naming.CLI, description=naming.TOOL_NAME)
    sub = ap.add_subparsers(dest="cmd")
    # The subcommands repeat the top-level flags so both orders parse, but with
    # SUPPRESS defaults: an absent subcommand flag never overwrites a value the
    # top-level parser already read (argparse would reset it to the default).
    S = argparse.SUPPRESS
    p = sub.add_parser("run")
    p.add_argument("--server", default=S)
    p.add_argument("--trial", action="append", default=S)
    p.add_argument("--scenario", action="append", default=S)
    p.add_argument("--class", dest="klass", default=S)
    p.add_argument("--spec", default=S)
    p.add_argument("--label", default=S)
    p.add_argument("--repeats", type=int, default=S)
    p.add_argument("--parallel", type=int, default=S)
    p.add_argument("--out", default=S)
    p.add_argument("--bind", action="store_true", default=S)
    p.add_argument("--dry-run", action="store_true", default=S)
    c = sub.add_parser("cleanup")
    c.add_argument("run_dir", nargs="?")
    c.add_argument("--dir", default=None)
    s = sub.add_parser("score")
    s.add_argument("run_dir")
    m = sub.add_parser("compare")
    m.add_argument("run_dir")
    m.add_argument("--baseline", required=True)
    b = sub.add_parser("pr-block")
    b.add_argument("--before", required=True)
    b.add_argument("--after", required=True)
    b.add_argument("--issue", default=None)
    v = sub.add_parser("validate")
    v.add_argument("files", nargs="*")
    l = sub.add_parser("list")
    l.add_argument("--class", dest="klass", default=S)
    r = sub.add_parser("raid")
    r.add_argument("--practice-dir", default=None)
    r.add_argument("--boss", default=None)
    r.add_argument("--walk", default=None)
    r.add_argument("--label", default=S)
    ap.add_argument("--class", dest="klass", default=None)
    ap.add_argument("--spec", default=None)
    ap.add_argument("--trial", action="append", default=[])
    ap.add_argument("--scenario", action="append", default=[])
    # Short form: trials --class priest --scenario shield-stance --label x --server S
    ap.add_argument("--server", default="")
    ap.add_argument("--label", default="")
    ap.add_argument("--repeats", type=int, default=0)
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument("--out", default="")
    ap.add_argument("--bind", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd == "list":
        cmd_list(args)
        return 0
    if args.cmd == "validate":
        return cmd_validate(args)
    if args.cmd == "score":
        return cmd_score(args)
    if args.cmd == "compare":
        cmd_compare(args)
        return 0
    if args.cmd == "pr-block":
        cmd_pr_block(args)
        return 0
    if args.cmd == "cleanup":
        return cmd_cleanup(args)
    if args.cmd == "raid":
        args.label = getattr(args, "label", "") or "run"
        return cmd_raid(args)
    if args.cmd == "run" or args.klass or args.trial or args.scenario:
        args.server = getattr(args, "server", "") or ""
        args.label = getattr(args, "label", "") or "run"
        args.repeats = getattr(args, "repeats", 0) or 0
        args.parallel = getattr(args, "parallel", 1) or 1
        args.out = getattr(args, "out", "") or ""
        args.bind = getattr(args, "bind", False)
        args.dry_run = getattr(args, "dry_run", False)
        trials, paths = _all_trials()
        try:
            resolved = resolve_trials(args, trials, paths)
        except ValueError as exc:
            print("error: %s" % exc, file=sys.stderr)
            entries = [(t.get("id"), pth) for t, pth in zip(trials, paths)]
            for sel in (args.trial or []) + (args.scenario or []):
                try:
                    matched = scenario.match_selector(sel, entries)
                except ValueError:
                    matched = [sel]
                if matched:
                    continue
                for close in _close_ids(sel, trials):
                    print("  close: %s" % close, file=sys.stderr)
            return 2
        if not resolved:
            print("no trials resolved", file=sys.stderr)
            for sel in (args.trial or []) + (args.scenario or []):
                for close in _close_ids(sel, trials):
                    print("  close: %s" % close, file=sys.stderr)
            return 2
        args.trials = resolved
        return cmd_run(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
