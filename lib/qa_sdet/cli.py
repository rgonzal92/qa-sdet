"""qa-mem: compact SQLite memory of the app under test.

Agents read it (card/search/bugs/hash/code). Knowledge is written only by `ingest`, which the plugin's
Stop/SubagentStop hook runs on every changed qa-out/<run>/.data/run-*.json, and by the code map: `codemap-scan` lists
source files deterministically, the qa-mapper agent writes qa-out/init/code-map-<area>.json, and the same
hook ingests it into the `codemap` table (hints from source, never requirements). The user's manual writes
are `bug-status` and `stale`.

v0.4 run folders (qa-out/<date>-<label>/): `rundir` makes one, explorers write .data/run-<slug>.json (ingested
like run.json), and `report` renders REPORT.md + qa-out/README.md from .data/plan.json and those files.
Wrappers (`marker`, `browser-open`, `prune`, `annotate`, `setup-permissions`) keep skill commands to one shape.
"""
import json
import os
import re
import select
import sqlite3
import sys
import time
from pathlib import Path
from . import codemap, core, memory, scrub  # the rest load in the commands that use them (start-up time)


def _opts(args, flags=(), valued=(), multi=()):
    """{'--x': value or True, '--m': [values], '_': [positionals]} from --x v / --x=v / --flag; unknown options are
    BadInput."""
    out, it = {"_": []}, iter(args)
    for a in it:
        name, eq, val = a.partition("=")
        if name in valued or name in multi:
            v = val if eq else next(it, None)
            if v is None:
                raise core.BadOption(f"{name} needs a value")
            if name in multi:
                out.setdefault(name, []).append(v)
            elif name in out:  # last-wins would silently drop a value (--keys 1 --keys 2)
                raise core.BadOption(f"{name} given twice; give it once (comma-separate several keys)")
            else:
                out[name] = v
        elif a in flags:
            out[a] = True
        elif a.startswith("--"):
            raise core.BadOption(f"unknown option {a}")
        else:
            out["_"].append(a)
    return out


def _count(o, name, most=None):
    try:
        n = int(o[name]) if name in o else 0  # `--budget ""` is a mistake, not "unlimited"
    except (ValueError, TypeError):
        n = -1
    if n < 0:
        raise core.BadInput(f"{name} must be a whole number >= 0")
    if most is not None and n > most:
        raise core.BadInput(f"{name} must be at most {most}")
    return n


def wrapper_cmd(cmd, args):
    """The v0.4 commands that need no memory DB. Returns an exit code, or None for other commands."""
    if cmd not in ("keys", "rundir", "marker", "browser-open", "session-check", "page-start", "prune", "annotate",
                   "evidence", "report", "audit-bugs", "signals-triage", "dup-check", "trace", "next-action", "explore-next", "plan-find",
                   "setup-permissions"):
        return None
    from . import ops
    if cmd == "keys":
        print(json.dumps(ops.expand_keys(args, core.config().get("jiraKeyPrefix"))))
    elif cmd == "rundir":
        o = _opts(args, valued=("--keys", "--route", "--label", "--prefix"))
        if len(o["_"]) != 1 or sum(k in o for k in ("--keys", "--route", "--label")) != 1:
            raise core.BadInput("usage: qa-mem rundir <project> (--keys K1,K2 | --route <route> | --label <text>) [--prefix plan-]")
        keys = ops.expand_keys([o["--keys"]], core.config().get("jiraKeyPrefix")) if "--keys" in o else None
        print(ops.make_rundir(o["_"][0], ops.run_label(keys, o.get("--route"), o.get("--label"), o.get("--prefix") or "")))
    elif cmd == "marker":
        o = _opts(args[1:], flags=("--faults",), valued=("--budget", "--minutes"))
        if not args or args[0] not in ("start", "stop") or len(o["_"]) != 1:
            raise core.BadInput("usage: qa-mem marker start <project> [--budget N] [--minutes M] [--faults] | marker stop <project>")
        if args[0] == "start":
            print(json.dumps(ops.marker_start(o["_"][0], _count(o, "--budget"), _count(o, "--minutes", ops.MARKER_HOURS * 60), o.get("--faults", False))))
        else:
            if len(o) > 1:
                raise core.BadOption("marker stop takes no options")
            ops.marker_stop(o["_"][0])
            print("marker removed")
    elif cmd == "browser-open":
        return ops.browser_open(args)
    elif cmd == "session-check":
        o = _opts(args, flags=("--record", "--build"))
        if o["_"]:
            raise core.BadOption("session-check takes no positional arguments")
        return ops.session_check(record=o.get("--record", False), read_build=o.get("--build", False))
    elif cmd == "page-start":
        if len(args) != 1:
            raise core.BadInput("usage: qa-mem page-start <url>")
        return ops.page_start(args[0])
    elif cmd == "evidence":
        o = _opts(args, valued=("--selector", "--ref", "--label"))
        if len(o["_"]) != 1 or ("--selector" in o) == ("--ref" in o) or not o.get("--label"):
            raise core.BadInput("usage: qa-mem evidence <png> (--selector <css> | --ref <ref>) --label <text>")
        return ops.evidence(o["_"][0], o["--label"], selector=o.get("--selector"), ref=o.get("--ref"))
    elif cmd == "prune":
        if len(args) not in (1, 2):
            raise core.BadInput("usage: qa-mem prune <project> [<run dir>]")
        if len(args) == 2 and not (Path(args[1]).is_dir() and Path(args[1]).resolve().parent.name == "qa-out"):
            raise core.BadInput(f"prune: not a run folder (qa-out/<run>/): {args[1]}")
        n = ops.prune(args[0], run=args[1] if len(args) == 2 else None)
        print(f"pruned {n} old snapshot files" if len(args) == 1 else f"pruned {n} snapshot and temp files of this run")
    elif cmd == "annotate":
        o = _opts(args, valued=("--label",), multi=("--box",))
        if len(o["_"]) != 1 or not o.get("--label"):
            raise core.BadInput("usage: qa-mem annotate <png> [--box x,y,w,h ...] --label <text>")
        boxes = [ops.parse_box(b) for b in o.get("--box") or []]  # none: the label alone, as a caption
        png = ops.evidence_png(o["_"][0])
        if not ops.annotate(png, boxes, o["--label"]):
            print(ops.NO_PILLOW)
        else:
            print(f"annotated {png}")
    elif cmd == "report":
        if len(args) != 1:
            raise core.BadInput("usage: qa-mem report <run dir>")
        from . import report
        print(report.write_report(args[0]))
    elif cmd == "signals-triage":
        o = _opts(args, valued=("--slug",))
        if len(o["_"]) != 1 or not o.get("--slug") or not (Path(o["_"][0]) / ".data").is_dir():
            raise core.BadInput("usage: qa-mem signals-triage <run dir> --slug <slug>")
        from . import report
        data = Path(o["_"][0]) / ".data"
        try:
            run = json.loads((data / f"run-{o['--slug']}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            run = {}  # step 9 writes the run file last: nothing named yet
        t = report.triage_signals(o["_"][0], o["--slug"], run if isinstance(run, dict) else {})
        lines = [f"- " + (f"#{i} " if i else "") + key + (f" (×{n})" if n > 1 else "") for key, n, i in t["app"]]
        print(("APP SIGNALS NO BUG EXPLAINS YET (while the browser is open: `playwright-cli -s=qa response-body <#>`; a "
               "real failure is a bug, a known one goes in notes by method and path):\n" + "\n".join(lines))
              if lines else "no unexplained app signals")
        if t["third_party"]:
            print(f"{t['third_party']} third-party signal(s) left out (hosts outside allowedHosts)")
    elif cmd == "next-action":
        if len(args) != 2 or not (Path(args[0]) / ".data").is_dir():
            raise core.BadInput("usage: qa-mem next-action <run dir> <slug>")
        from . import report
        print(json.dumps(report.next_action(args[0], args[1])))
    elif cmd == "explore-next":
        o = _opts(args, valued=("--run",))
        if len(o["_"]) != 1:
            raise core.BadInput("usage: qa-mem explore-next <project> [--run DIR]")
        from . import report
        print(json.dumps(report.explore_next(o["_"][0], o.get("--run"))))
    elif cmd == "trace":
        o = _opts(args, valued=("--keys", "--fix-version"))
        if len(o["_"]) != 1:
            raise core.BadInput("usage: qa-mem trace <project> [--keys K1,K2] [--fix-version VER]")
        from . import report
        keys = ops.expand_keys([o["--keys"]], core.config().get("jiraKeyPrefix")) if o.get("--keys") else None
        print(report.trace(o["_"][0], keys, o.get("--fix-version")))
    elif cmd == "dup-check":
        if len(args) != 2 or not (Path(args[0]) / ".data").is_dir() or not re.fullmatch(r"BUG-\d+", args[1]):
            raise core.BadInput("usage: <acli jira workitem search ... --json> | qa-mem dup-check <run dir> <BUG-n>")
        try:
            data = json.loads(sys.stdin.read() or "[]")
        except ValueError as e:
            raise core.BadInput(f"dup-check: stdin is not the acli search JSON ({e})")
        items = data if isinstance(data, list) else next((data[k] for k in ("issues", "workItems", "values")
                                                          if isinstance(data, dict) and isinstance(data.get(k), list)), [])
        hits = [{"key": str(x.get("key")), "summary": scrub.redact(" ".join(str(
                    (x.get("fields") if isinstance(x.get("fields"), dict) else x).get("summary") or "").split()))[:200]}
                for x in items[:10] if isinstance(x, dict) and x.get("key")]
        f = Path(args[0]) / ".data" / "jira-dups.json"
        try:
            saved = json.loads(f.read_text(encoding="utf-8"))
            saved = saved if isinstance(saved, dict) else {}
        except (OSError, ValueError):
            saved = {}
        saved[args[1]] = hits
        core._write(f, json.dumps(saved, indent=1) + "\n")
        print(f"{args[1]}: {len(hits)} possible duplicate(s) saved; REPORT.md shows them above its filing command")
    elif cmd == "audit-bugs":
        if len(args) != 1 or not Path(args[0]).is_dir():
            raise core.BadInput("usage: qa-mem audit-bugs <run dir>")
        from . import report
        found = report.audit_bugs(args[0])
        print("\n".join(f"{bid}: {'CRITICAL ' if crit else ''}{text}" for bid, issues in found.items() for crit, text in issues)
              or "bug drafts and repro specs: nothing to fix")
    elif cmd == "plan-find":
        from . import report
        o = _opts(args, valued=("--keys", "--run"))
        if len(o["_"]) != 1 or sum(k in o for k in ("--keys", "--run")) != 1:
            raise core.BadInput("usage: qa-mem plan-find <project> (--keys K1,K2 | --run DIR)")
        keys = ops.expand_keys([o["--keys"]], core.config().get("jiraKeyPrefix")) if "--keys" in o else None
        print(json.dumps(report.plan_find(o["_"][0], keys=keys, run_dir=o.get("--run")), indent=1))
    elif cmd == "setup-permissions":
        from . import doctor
        o = _opts(args, flags=("--apply",), valued=("--settings",))
        if o["_"]:
            raise core.BadInput("usage: qa-mem setup-permissions [--settings PATH] [--apply]")
        if not o.get("--apply"):
            print("\n".join(doctor.PERMISSION_RULES))
            return 0
        if "--settings" in o and not o["--settings"].strip():
            raise core.BadOption("--settings needs a path")  # an empty shell variable must not fall back to the real file
        added = doctor.merge_permissions(o.get("--settings") or "~/.claude/settings.json")
        print("added to permissions.allow:\n" + "\n".join(added) if added else "all rules already present; nothing changed")
    else:
        return None
    return 0


COMMANDS = {  # name -> arguments, for `qa-mem` (no args), `qa-mem <cmd> --help` and bad input
    "doctor": "", "browsers": "", "where": "", "card": "<route|page name> [--focus <terms>] [--ids]",
    "patterns": "[--min-pages N] [--json]", "signals": "[route] [--status ACTIVE|IGNORED|ALL]",
    "signal-ignore": "<id>", "signal-unignore": "<id>", "history": "[route] [--builds N] [--json]", "hotspots": "[--top N] [--json]", "bug-match": "<route> <symptom> [--endpoint '<METHOD /path>']", "questions": "[open|resolved|all]", "answer": "<question id> <answer>",
    "remember": "<route|*> <rule|flow|api|validation|note> <text> [--build-scoped] [--source <src>]",
    "forget": "fact <id> | forget page <route> [--force]", "gaps": "[--changed] [--top N] [--json]",
    "search": "<words> [-word to exclude]",
    "bugs": "[STATUS]", "hash": "<route> <symptom>",
    "bug-status": "<hash-prefix> <" + "|".join(sorted(core.STATUSES)) + "> [--jira KEY]",
    "bug-sync": "[--jql]",
    "stale": "<build>", "ingest": "<run.json>", "ingest-pending": "<dir>...", "code": "[route] | code --repos",
    "codemap-scan": "[--full] [--out DIR] <source folder>...", "init-status": "<init dir>", "codemap-forget": "<source folder>",
    "keys": "<number|KEY>...", "rundir": "<project> (--keys K1,K2 | --route <route> | --label <text>) [--prefix plan-]",
    "marker": "start <project> [--budget N] [--minutes M] [--faults] | marker stop <project>",
    "browser-open": "[--session qa|qa-login] [--headed] [<url>]", "session-check": "[--record] [--build]",
    "page-start": "<url>",
    "dispatch": "<run dir> --route <route> [--mode ticket|explore] [--focus <charter>] [--budget N] [--minutes M] "
                "[--faults] [--known-bugs <text>] [--open-record]", "evidence": "<png> (--selector <css> | --ref <ref>) --label <text>",
    "prune": "<project> [<run dir>]",
    "annotate": "<png> [--box x,y,w,h ...] --label <text>", "report": "<run dir>", "audit-bugs": "<run dir>", "dup-check": "<run dir> <BUG-n>   (acli search JSON on stdin)", "trace": "<project> [--keys K1,K2] [--fix-version VER]", "next-action": "<run dir> <slug>", "explore-next": "<project> [--run DIR]", "signals-triage": "<run dir> --slug <slug>",
    "plan-find": "<project> (--keys K1,K2 | --run DIR)", "prep": "<project> <number|KEY>...",
    "inbox": "<project>", "setup-permissions": "[--settings PATH] [--apply]",
}
USAGE = "usage: qa-mem <command> [args]; commands:\n" + "\n".join(f"  qa-mem {c} {a}".rstrip() for c, a in COMMANDS.items())


def usage_error(cmd, why=""):
    print(f"qa-mem: {why + '; ' if why else ''}usage: qa-mem {cmd} {COMMANDS.get(cmd, '')}".rstrip(), file=sys.stderr)
    return 2


def _run_marker(roots):
    """The active run marker that names an explorer's run (run_dir and slug, from dispatch), or None."""
    for root in roots:
        root = Path(root)
        qa_out = root if root.name == "qa-out" else next((p for p in root.parents if p.name == "qa-out"), root)
        try:
            m = json.loads((qa_out / ".active").read_text())
            if isinstance(m, dict) and m.get("run_dir") and m.get("slug") and float(m.get("expires") or 0) > time.time():
                return m
        except (OSError, ValueError, TypeError):
            continue
    return None


def hook_input():
    """The hook's JSON on stdin (Claude Code sends it and closes stdin); {} when run by hand."""
    try:
        # 2 s: on a loaded machine the payload can come late, and missing stop_hook_active forces an extra round
        if sys.stdin is None or sys.stdin.isatty() or not select.select([sys.stdin], [], [], 2.0)[0]:
            return {}
        data = json.loads(sys.stdin.read() or "{}")
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError, RecursionError):
        return {}


def main(argv):
    try:
        return _main(argv)
    except core.BadInput as e:  # also the refusal of a pre-0.4 DB, from core.connect()
        print(f"qa-mem: {e}", file=sys.stderr)
        return 2
    except sqlite3.DatabaseError as e:
        if "locked" not in str(e) and "busy" not in str(e):
            print(f"qa-mem: the memory DB {core.db_path()} failed: {e}", file=sys.stderr)
            return 1
        print(f"qa-mem: the memory DB {core.db_path()} stayed busy (another qa-mem or a run's hook is writing to it); "
              "the command did not finish: run it again in a minute", file=sys.stderr)
        return 1


def _main(argv):
    for stream in (sys.stdout, sys.stderr):  # a non-UTF-8 argument echoed back must not crash the print
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    # bytes that aren't UTF-8 arrive as surrogates, which sqlite3 and print() refuse: make them U+FFFD
    argv = [a.encode("utf-8", "surrogateescape").decode("utf-8", "replace") for a in argv]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd not in COMMANDS:
        print(f"qa-mem: unknown command {cmd!r}\n{USAGE}", file=sys.stderr)
        return 2
    if args and not COMMANDS[cmd]:
        if any(a in ("-h", "--help") for a in args):
            return usage_error(cmd)
        return usage_error(cmd, f"{cmd} takes no arguments")
    # an option the usage line doesn't list is a typo (`stale --bogus` must not set the build to "--bogus");
    # option values (after --label, --box, …) and `hash` symptom text may start with "--" or be "-h"
    known = set(re.findall(r"--[a-z][a-z-]*[a-z]", COMMANDS[cmd]))  # --known-bugs is one option
    valued = set(re.findall(r"(--[a-z][a-z-]*[a-z])\s+[^|\[\]]", COMMANDS[cmd]))
    skip = False
    for k, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a in ("-h", "--help") and not (cmd == "hash" and k >= 1 and a == "-h"):
            return usage_error(cmd)
        if cmd != "hash" and re.match(r"--[A-Za-z]", a) and a.split("=")[0] not in known:
            return usage_error(cmd, f"unknown option {a}")
        skip = a in valued
    if cmd == "ingest-pending":  # runs from hooks: must never break the session
        memory.PARKED.clear()
        roots = list(dict.fromkeys(os.path.realpath(a) for a in (args or ["qa-out"]) if a))  # cwd + project dir
        for root in roots:
            try:
                memory.ingest_pending(root)
            except Exception as e:  # noqa: BLE001
                print(f"qa-mem: {e}", file=sys.stderr)
        # parallel mappers: the first to stop parks every bad code map, so each mapper hears about all still parked
        stale = [x for root in roots for x in memory.parked_codemaps(root)
                 if not any(p.startswith(x.split(": ", 1)[0] + ":") for p in memory.PARKED)]
        marker = _run_marker(roots)  # an explorer's run (dispatch wrote run_dir and slug)
        hin = hook_input() if memory.PARKED or stale or marker else {}
        role = str(hin.get("agent_type") or "").strip().lower().split(":")[-1].strip()
        if stale and hin.get("hook_event_name") == "SubagentStop" and role == "qa-mapper":
            memory.PARKED.extend(stale)
        todo = []
        if marker and hin.get("hook_event_name") == "SubagentStop" and role in ("qa-explorer", "qa-crawler") \
                and not hin.get("stop_hook_active"):
            from . import ops, report  # the marker was started MARKER_HOURS before it expires
            todo = report.verify_run(marker["run_dir"], marker["slug"], float(marker["expires"]) - ops.MARKER_HOURS * 3600)
        if todo:
            print("qa-sdet: before you stop, finish these (then stop again):\n" + "\n".join(f"- {t}" for t in todo),
                  file=sys.stderr)
            if not memory.PARKED:
                return 2
        if memory.PARKED and not hin.get("stop_hook_active"):
            # Exit 2 from a Stop/SubagentStop hook shows this to the agent that just wrote the file, once.
            refs = Path(__file__).resolve().parent.parent.parent / "references"
            print(f"qa-sdet memory could not save this (file(s) below). For each one YOU wrote this run: fix the field the "
                  f"error names (rules: {refs / 'run-json.md'} for run JSON, {refs / 'code-map.md'} for code-map-*.json) "
                  "and rewrite the whole file; it is re-read automatically. A file you did not write belongs to another "
                  "agent: leave it.\n" + "\n".join(memory.PARKED), file=sys.stderr)
            return 2
        return 0
    if cmd == "init-status":
        if len(args) != 1:
            return usage_error(cmd)
        print(json.dumps(memory.init_status(args[0]), indent=1))
        return 0
    if cmd == "doctor":
        from . import doctor
        return 1 if doctor.doctor() else 0
    try:
        code_ = wrapper_cmd(cmd, args)
        if code_ is not None:
            return code_
        if cmd == "inbox":
            from . import report
            o = _opts(args)
            if len(o["_"]) != 1:
                raise core.BadInput("usage: qa-mem inbox <project>   (the acli JSON on stdin)")
            try:
                data = json.loads(sys.stdin.read())
            except (OSError, ValueError, RecursionError) as e:  # RecursionError: garbage nested too deep
                raise core.BadInput(f"inbox: can't read Jira JSON ({e})")
            items = report.inbox_items(data)  # bad input fails before the DB is opened
            con = core.connect()
            try:
                print(report.inbox(con, o["_"][0], items, core.config().get("jiraKeyPrefix")), end="")
            finally:
                con.close()
            return 0
    except core.BadOption as e:
        return usage_error(cmd, str(e))
    except core.BadInput as e:
        print(f"qa-mem: {e}", file=sys.stderr)
        return 2
    if cmd == "browsers":  # what C1 offers when it asks which browser to use
        from . import doctor
        for name, (ok, how) in doctor.installed_browsers().items():
            print(f"{name:8} {'installed' if ok else 'missing  '}  {how}")
        print("(firefox and webkit mean Playwright's own builds; a system Firefox can't be driven)")
        return 0
    if cmd == "hash" and len(args) >= 2:  # same normalisation as ingest, so draft and DB hashes match
        print(core.bug_hash(core.norm_route(args[0]), core.cap(scrub.redact(" ".join(args[1:])))))
        return 0
    if cmd == "where":
        print(core.db_path())
        return 0
    con = core.connect()
    try:
        return run_cmd(con, cmd, args)
    except core.BadOption as e:
        return usage_error(cmd, str(e))
    finally:
        con.close()


def run_cmd(con, cmd, args):
    if cmd == "card" and args:
        o = _opts(args, flags=("--ids",), valued=("--focus",))
        if not o["_"]:
            return usage_error(cmd)
        print(memory.card(con, " ".join(o["_"]), o.get("--focus"), o.get("--ids", False)))
    elif cmd == "bug-match":
        o = _opts(args, valued=("--endpoint",))
        if len(o["_"]) < 2:
            return usage_error(cmd)
        print(memory.bug_match(con, o["_"][0], " ".join(o["_"][1:]), o.get("--endpoint")))
    elif cmd == "history":
        o = _opts(args, flags=("--json",), valued=("--builds",))
        if len(o["_"]) > 1:
            return usage_error(cmd)
        print(memory.history(con, o["_"][0] if o["_"] else None, _count(o, "--builds") or 5, o.get("--json", False)))
    elif cmd == "hotspots":
        o = _opts(args, flags=("--json",), valued=("--top",))
        if o["_"]:
            return usage_error(cmd)
        print(memory.hotspots(con, _count(o, "--top") or 10, o.get("--json", False)))
    elif cmd == "signals":
        o = _opts(args, valued=("--status",))
        status = str(o.get("--status") or "ACTIVE").upper()
        if len(o["_"]) > 1 or status not in ("ACTIVE", "IGNORED", "ALL"):
            return usage_error(cmd)
        print(memory.signals(con, o["_"][0] if o["_"] else None, status))
    elif cmd in ("signal-ignore", "signal-unignore"):
        if len(args) != 1 or not args[0].lstrip("#").isdigit():
            return usage_error(cmd)
        if not memory.set_signal(con, int(args[0].lstrip("#")), "IGNORED" if cmd == "signal-ignore" else "ACTIVE"):
            print(f"no signal #{args[0].lstrip('#')} (qa-mem signals lists them)")
            return 1
        print(f"signal #{args[0].lstrip('#')} " + ("ignored: reports leave it out" if cmd == "signal-ignore" else "active again"))
    elif cmd == "patterns":
        o = _opts(args, flags=("--json",), valued=("--min-pages",))
        if o["_"]:
            return usage_error(cmd)
        print(memory.patterns(con, max(2, _count(o, "--min-pages")) if "--min-pages" in o else 2, o.get("--json", False)))
    elif cmd == "questions":
        if len(args) > 1 or (args and args[0] not in ("open", "resolved", "all")):
            return usage_error(cmd)
        print(memory.questions(con, args[0] if args else "open"))
    elif cmd == "answer":
        if len(args) < 2 or not args[0].lstrip("#").isdigit():
            return usage_error(cmd)
        if not memory.answer(con, int(args[0].lstrip("#")), " ".join(args[1:])):
            print(f"no question #{args[0].lstrip('#')} (qa-mem questions lists them)")
            return 1
        print(f"question #{args[0].lstrip('#')} answered: cards show it under DECISIONS")
    elif cmd == "remember":
        o = _opts(args, flags=("--build-scoped",), valued=("--source",))
        if len(o["_"]) < 3:
            return usage_error(cmd)
        try:
            fid = memory.remember(con, o["_"][0], o["_"][1], " ".join(o["_"][2:]), not o.get("--build-scoped", False),
                                  o.get("--source") or "user")
        except core.BadInput as e:
            print(f"qa-mem: {e}", file=sys.stderr)
            return 2
        print(f"remembered #{fid}" + ("" if o.get("--build-scoped") else " (pinned: kept across builds)"))
    elif cmd == "forget":
        o = _opts(args, flags=("--force",))
        if len(o["_"]) != 2 or o["_"][0] not in ("fact", "page") or (o["_"][0] == "fact" and not o["_"][1].lstrip("#").isdigit()):
            return usage_error(cmd)
        if o["_"][0] == "fact":
            row = memory.forget_fact(con, int(o["_"][1].lstrip("#")))
            print(f"forgot fact #{o['_'][1].lstrip('#')}: {row['page']} [{row['kind']}] {row['text']}" if row
                  else "no such fact (qa-mem card <route> --ids shows them)")
            return 0 if row else 1
        done, why = memory.forget_page(con, o["_"][1], o.get("--force", False))
        print(why)
        return 0 if done else 1
    elif cmd == "code":
        o = _opts(args, flags=("--repos",))
        if o.get("--repos") and o["_"]:
            raise core.BadOption("--repos takes no route")
        print(codemap.code(con, "--repos" if o.get("--repos") else " ".join(o["_"])))
    elif cmd == "codemap-forget" and args:
        n = codemap.codemap_forget(con, " ".join(args))
        if n is None:
            print(f"no mapped source folder '{' '.join(args)}'; give one exactly as `qa-mem code --repos` lists it",
                  file=sys.stderr)
            return 2
        print(f"removed {n} code-map rows for {' '.join(args)}")
    elif cmd == "codemap-scan":
        o = _opts(args, flags=("--full",), valued=("--out",))
        if "--out" in o and not o["--out"].strip():
            raise core.BadOption("--out needs a folder path")
        paths = o["_"]
        if not paths:
            print("give the source folder(s) to scan, e.g. qa-mem codemap-scan ~/work/traffic-ui ~/work/traffic-api")
            return 2
        from . import scan
        print(json.dumps(scan.codemap_scan(con, paths, o.get("--out") or "qa-out/init", o.get("--full", False)), indent=1))
    elif cmd == "gaps":
        o = _opts(args, flags=("--changed", "--json"), valued=("--top",))
        if o["_"]:
            raise core.BadOption("gaps takes no positional arguments")
        top = None
        if "--top" in o:
            top = _count(o, "--top")
            if top < 1:
                raise core.BadInput("--top must be a whole number >= 1")
        print(memory.gaps(con, changed_only=o.get("--changed", False), top=top, as_json=o.get("--json", False)))
    elif cmd == "dispatch":
        o = _opts(args, flags=("--faults", "--open-record"),
                  valued=("--route", "--mode", "--focus", "--budget", "--minutes", "--known-bugs"))
        if len(o["_"]) != 1 or not o.get("--route") or o.get("--mode", "ticket") not in ("ticket", "explore"):
            return usage_error(cmd)
        from . import ops
        print(json.dumps(ops.dispatch(con, o["_"][0], o["--route"], o.get("--mode"), o.get("--focus"), _count(o, "--budget"),
                                      _count(o, "--minutes", ops.MARKER_HOURS * 60), o.get("--faults", False),
                                      o.get("--known-bugs") or "", o.get("--open-record", False))))
    elif cmd == "search" and args:
        print(memory.search(con, " ".join(args)))
    elif cmd == "bugs" and len(args) <= 1:
        status = args[0].upper() if args else "OPEN"
        if status not in core.STATUSES:
            print(f"unknown status '{args[0]}'; use one of {sorted(core.STATUSES)}")
            return 2
        print(memory.bugs(con, status))
    elif cmd == "bug-status":
        o = _opts(args, valued=("--jira",))
        pos = o["_"]
        if len(pos) not in (2, 3) or (len(pos) == 3 and "--jira" in o):
            return usage_error(cmd)
        prefix, status = pos[0].lower(), pos[1].upper()
        jira_raw = o["--jira"] if "--jira" in o else (pos[2] if len(pos) == 3 else None)
        jira_key = None
        if jira_raw is not None:
            jira_key = jira_raw.strip().upper() if jira_raw.strip().isascii() else jira_raw.strip()
            if not re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", jira_key, re.ASCII):
                raise core.BadInput(f"bug-status: not a Jira key: {jira_raw!r} (use e.g. PROJ-123)")
        if not re.fullmatch(r"[0-9a-f]{4,12}", prefix) or status not in core.STATUSES:
            print(f"need a hex hash prefix (4-12 chars) and a status in {sorted(core.STATUSES)}")
            return 2
        hits = [r[0] for r in con.execute("SELECT hash FROM bugs WHERE substr(hash, 1, ?)=?", (len(prefix), prefix))]
        if len(hits) != 1:  # an ambiguous prefix must not change several bugs at once
            print(f"{len(hits)} bugs match '{prefix}'" + (": " + ", ".join(hits[:10]) if hits else "") + "; give one hash")
            return 1
        with con:
            if jira_key is not None:
                con.execute("UPDATE bugs SET status=?, jira_key=? WHERE hash=?", (status, jira_key, hits[0]))
            else:
                con.execute("UPDATE bugs SET status=? WHERE hash=?", (status, hits[0]))
            if status == "FIXED":  # a run that finished before this, ingested later, is no regression
                con.execute("UPDATE bugs SET fixed_ts=?, fixed_build=? WHERE hash=?",
                            (core.now(), core.meta(con, "build"), hits[0]))
            # reopened, but its run files no longer list it (they were rewritten without it): it is no bug to track
            gone = con.execute("DELETE FROM bugs WHERE hash=? AND status='OPEN' AND seen_count=0 AND NOT EXISTS "
                               "(SELECT 1 FROM sightings WHERE hash=?)", (hits[0], hits[0])).rowcount
        extra = f" ({jira_key})" if jira_key else ""
        print(f"removed {hits[0]}: no run lists this bug any more" if gone else f"updated {hits[0]} -> {status}{extra}")
    elif cmd == "bug-sync":
        o = _opts(args, flags=("--jql",))
        if o["_"]:
            raise core.BadOption("bug-sync takes no positional arguments")
        if o.get("--jql"):
            jql = memory.bug_sync_jql(con)
            if jql:
                print(jql)
            return 0
        try:
            data = json.loads(sys.stdin.read())
        except (OSError, ValueError, RecursionError) as e:
            raise core.BadInput(f"bug-sync: can't read Jira JSON ({e})")
        print(memory.bug_sync(con, data))
    elif cmd == "prep":
        if len(args) < 2:
            return usage_error(cmd)
        from . import report
        print(json.dumps(report.prep(con, args[0], args[1:]), indent=1, ensure_ascii=False))
    elif cmd == "ingest" and len(args) == 1:
        # Same scrubbing as the hook path, so secrets stay out of the DB and hashes match `qa-mem hash`.
        try:
            memory.ingest(con, scrub.redact_json(json.loads(Path(args[0]).read_text(encoding="utf-8-sig"))),
                          run_dir=Path(args[0]).resolve().parent, src=Path(args[0]).resolve())
        except (OSError, ValueError, RecursionError) as e:  # ValueError: not JSON, or a field ingest rejects
            print(f"qa-mem: ingest: can't save {args[0]} ({type(e).__name__}: {core.short(e, 200)})", file=sys.stderr)
            return 2
        print("ingested")
    elif cmd == "stale" and args:
        build = core.norm_build(" ".join(args))  # normalised like ingest, so it matches stored builds
        if build == core.UNKNOWN:
            print("give the deployed build, e.g. qa-mem stale 1.43.0")
            return 2
        with con:
            core.set_meta(con, "build", build)
            core.set_meta(con, "build_ts", core.now())
            for table in ("facts", "locators"):
                con.execute(f"UPDATE {table} SET stale=1 WHERE page IN (SELECT route FROM pages WHERE build!=?)"
                            + (" AND pinned=0" if table == "facts" else ""), (build,))  # the user's own facts stay
                # a corrected build (stale 1.44 then stale 1.43): what was verified on it is current again
                con.execute(f"UPDATE {table} SET stale=0 WHERE build=? AND page IN "
                            f"(SELECT route FROM pages WHERE build=?)", (build, build))
        print(f"current build set to {build}; older knowledge marked stale")
    else:
        return usage_error(cmd)
    return 0
