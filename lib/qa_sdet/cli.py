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
from datetime import datetime
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
    if cmd not in ("keys", "rundir", "marker", "browser-open", "login-ok", "prune", "annotate", "report", "setup-permissions"):
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
    elif cmd == "login-ok":
        ops.ensure_profile_dir()
        core._write(ops.signed_in_stamp(), datetime.now().strftime("%Y-%m-%d %H:%M") + "\n")
        print("sign-in recorded: qa-mem doctor now reports the profile as signed in")
    elif cmd == "prune":
        if len(args) not in (1, 2):
            raise core.BadInput("usage: qa-mem prune <project> [<run dir>]")
        if len(args) == 2 and not (Path(args[1]).is_dir() and Path(args[1]).resolve().parent.name == "qa-out"):
            raise core.BadInput(f"prune: not a run folder (qa-out/<run>/): {args[1]}")
        n = ops.prune(args[0], run=args[1] if len(args) == 2 else None)
        print(f"pruned {n} old snapshot files" if len(args) == 1 else f"pruned {n} snapshot and temp files of this run")
    elif cmd == "annotate":
        o = _opts(args, valued=("--label",), multi=("--box",))
        if len(o["_"]) != 1 or not o.get("--box") or not o.get("--label"):
            raise core.BadInput("usage: qa-mem annotate <png> --box x,y,w,h [--box ...] --label <text>")
        boxes = [ops.parse_box(b) for b in o["--box"]]
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
    "doctor": "", "browsers": "", "where": "", "card": "<route|page name>", "gaps": "", "search": "<words> [-word to exclude]",
    "bugs": "[STATUS]", "hash": "<route> <symptom>", "bug-status": "<hash-prefix> <" + "|".join(sorted(core.STATUSES)) + ">",
    "stale": "<build>", "ingest": "<run.json>", "ingest-pending": "<dir>...", "code": "[route] | code --repos",
    "codemap-scan": "[--full] [--out DIR] <source folder>...", "codemap-forget": "<source folder>",
    "keys": "<number|KEY>...", "rundir": "<project> (--keys K1,K2 | --route <route> | --label <text>) [--prefix plan-]",
    "marker": "start <project> [--budget N] [--minutes M] [--faults] | marker stop <project>",
    "browser-open": "[--session qa|qa-login] [--headed] [<url>]", "login-ok": "", "prune": "<project> [<run dir>]",
    "annotate": "<png> --box x,y,w,h [--box ...] --label <text>", "report": "<run dir>",
    "inbox": "<project>", "setup-permissions": "[--settings PATH] [--apply]",
}
USAGE = "usage: qa-mem <command> [args]; commands:\n" + "\n".join(f"  qa-mem {c} {a}".rstrip() for c, a in COMMANDS.items())


def usage_error(cmd, why=""):
    print(f"qa-mem: {why + '; ' if why else ''}usage: qa-mem {cmd} {COMMANDS.get(cmd, '')}".rstrip(), file=sys.stderr)
    return 2


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
    known = set(re.findall(r"--[a-z]+", COMMANDS[cmd]))
    valued = set(re.findall(r"(--[a-z]+)\s+[^|\[\]]", COMMANDS[cmd]))
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
        for root in dict.fromkeys(os.path.realpath(a) for a in (args or ["qa-out"]) if a):  # cwd + project dir
            try:
                memory.ingest_pending(root)
            except Exception as e:  # noqa: BLE001
                print(f"qa-mem: {e}", file=sys.stderr)
        if memory.PARKED and not hook_input().get("stop_hook_active"):
            # Exit 2 from a Stop/SubagentStop hook shows this to the agent that just wrote the file, once.
            refs = Path(__file__).resolve().parent.parent.parent / "references"
            print(f"qa-sdet memory could not save this (file(s) below). For each one YOU wrote this run: fix the field the "
                  f"error names (rules: {refs / 'run-json.md'} for run JSON, {refs / 'code-map.md'} for code-map-*.json) "
                  "and rewrite the whole file; it is re-read automatically. A file you did not write belongs to another "
                  "agent: leave it.\n" + "\n".join(memory.PARKED), file=sys.stderr)
            return 2
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
        print(memory.card(con, " ".join(args)))
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
        print(memory.gaps(con))
    elif cmd == "search" and args:
        print(memory.search(con, " ".join(args)))
    elif cmd == "bugs" and len(args) <= 1:
        status = args[0].upper() if args else "OPEN"
        if status not in core.STATUSES:
            print(f"unknown status '{args[0]}'; use one of {sorted(core.STATUSES)}")
            return 2
        print(memory.bugs(con, status))
    elif cmd == "bug-status" and len(args) == 2:
        prefix, status = args[0].lower(), args[1].upper()
        if not re.fullmatch(r"[0-9a-f]{4,12}", prefix) or status not in core.STATUSES:
            print(f"need a hex hash prefix (4-12 chars) and a status in {sorted(core.STATUSES)}")
            return 2
        hits = [r[0] for r in con.execute("SELECT hash FROM bugs WHERE substr(hash, 1, ?)=?", (len(prefix), prefix))]
        if len(hits) != 1:  # an ambiguous prefix must not change several bugs at once
            print(f"{len(hits)} bugs match '{prefix}'" + (": " + ", ".join(hits[:10]) if hits else "") + "; give one hash")
            return 1
        with con:
            con.execute("UPDATE bugs SET status=? WHERE hash=?", (status, hits[0]))
            # reopened, but its run files no longer list it (they were rewritten without it): it is no bug to track
            gone = con.execute("DELETE FROM bugs WHERE hash=? AND status='OPEN' AND seen_count=0 AND NOT EXISTS "
                               "(SELECT 1 FROM sightings WHERE hash=?)", (hits[0], hits[0])).rowcount
        print(f"removed {hits[0]}: no run lists this bug any more" if gone else f"updated {hits[0]} -> {status}")
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
                con.execute(f"UPDATE {table} SET stale=1 WHERE page IN (SELECT route FROM pages WHERE build!=?)",
                            (build,))
                # a corrected build (stale 1.44 then stale 1.43): what was verified on it is current again
                con.execute(f"UPDATE {table} SET stale=0 WHERE build=? AND page IN "
                            f"(SELECT route FROM pages WHERE build=?)", (build, build))
        print(f"current build set to {build}; older knowledge marked stale")
    else:
        return usage_error(cmd)
    return 0
