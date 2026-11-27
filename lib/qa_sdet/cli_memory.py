"""Handlers of the `qa-mem` commands that open the memory DB: the card and the other readers, the user's writes,
ingest, the crawl and dispatch. One function per command; `HANDLERS` maps the command names to them."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

# the rest load in the commands that use them (start-up time)
from . import cards, codemap, core, gaps, ingest, memory, scrub, usage


def cmd_card(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    if not args:
        return usage.error(cmd)
    o = usage.opts(args, flags=("--ids",), valued=("--focus",))
    if not o["_"]:
        return usage.error(cmd)
    print(cards.card(con, " ".join(o["_"]), o.get("--focus"), o.get("--ids", False)))
    return 0


def cmd_bug_match(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    o = usage.opts(args, valued=("--endpoint",))
    if len(o["_"]) < 2:
        return usage.error(cmd)
    print(memory.bug_match(con, o["_"][0], " ".join(o["_"][1:]), o.get("--endpoint")))
    return 0


def cmd_impact(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    o = usage.opts(args, flags=("--json",))
    if not o["_"]:
        return usage.error(cmd)
    print(memory.impact(con, " ".join(o["_"]), o.get("--json", False)))
    return 0


def cmd_history(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    o = usage.opts(args, flags=("--json",), valued=("--builds",))
    if len(o["_"]) > 1:
        return usage.error(cmd)
    print(gaps.history(con, o["_"][0] if o["_"] else None, usage.count(o, "--builds") or 5, o.get("--json", False)))
    return 0


def cmd_hotspots(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    o = usage.opts(args, flags=("--json",), valued=("--top",))
    if o["_"]:
        return usage.error(cmd)
    print(gaps.hotspots(con, usage.count(o, "--top") or 10, o.get("--json", False)))
    return 0


def cmd_signals(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    o = usage.opts(args, valued=("--status",))
    status = str(o.get("--status") or "ACTIVE").upper()
    if len(o["_"]) > 1 or status not in ("ACTIVE", "IGNORED", "ALL"):
        return usage.error(cmd)
    print(memory.signals(con, o["_"][0] if o["_"] else None, status))
    return 0


def cmd_signal_ignore(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    if len(args) != 1 or not args[0].lstrip("#").isdigit():
        return usage.error(cmd)
    if not memory.set_signal(con, int(args[0].lstrip("#")), "IGNORED" if cmd == "signal-ignore" else "ACTIVE"):
        print(f"no signal #{args[0].lstrip('#')} (qa-mem signals lists them)")
        return 1
    print(
        f"signal #{args[0].lstrip('#')} "
        + ("ignored: reports leave it out" if cmd == "signal-ignore" else "active again")
    )
    return 0


def cmd_patterns(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    o = usage.opts(args, flags=("--json",), valued=("--min-pages",))
    if o["_"]:
        return usage.error(cmd)
    print(
        memory.patterns(con, max(2, usage.count(o, "--min-pages")) if "--min-pages" in o else 2, o.get("--json", False))
    )
    return 0


def cmd_questions(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    if len(args) > 1 or (args and args[0] not in ("open", "resolved", "all")):
        return usage.error(cmd)
    print(memory.questions(con, args[0] if args else "open"))
    return 0


def cmd_answer(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    if len(args) < 2 or not args[0].lstrip("#").isdigit():
        return usage.error(cmd)
    if not memory.answer(con, int(args[0].lstrip("#")), " ".join(args[1:])):
        print(f"no question #{args[0].lstrip('#')} (qa-mem questions lists them)")
        return 1
    print(f"question #{args[0].lstrip('#')} answered: cards show it under DECISIONS")
    return 0


def cmd_remember(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    o = usage.opts(args, flags=("--build-scoped",), valued=("--source",))
    if len(o["_"]) < 3:
        return usage.error(cmd)
    try:
        fid = memory.remember(
            con,
            o["_"][0],
            o["_"][1],
            " ".join(o["_"][2:]),
            not o.get("--build-scoped", False),
            o.get("--source") or "user",
        )
    except core.BadInput as e:
        print(f"qa-mem: {e}", file=sys.stderr)
        return 2
    print(f"remembered #{fid}" + ("" if o.get("--build-scoped") else " (pinned: kept across builds)"))
    return 0


def cmd_forget(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    o = usage.opts(args, flags=("--force",))
    if (
        len(o["_"]) != 2
        or o["_"][0] not in ("fact", "page")
        or (o["_"][0] == "fact" and not o["_"][1].lstrip("#").isdigit())
    ):
        return usage.error(cmd)
    if o["_"][0] == "fact":
        row = memory.forget_fact(con, int(o["_"][1].lstrip("#")))
        print(
            f"forgot fact #{o['_'][1].lstrip('#')}: {row['page']} [{row['kind']}] {row['text']}"
            if row
            else "no such fact (qa-mem card <route> --ids shows them)"
        )
        return 0 if row else 1
    done, why = memory.forget_page(con, o["_"][1], o.get("--force", False))
    print(why)
    return 0 if done else 1


def cmd_code(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    o = usage.opts(args, flags=("--repos",))
    if o.get("--repos") and o["_"]:
        raise core.BadOption("--repos takes no route")
    print(codemap.code(con, "--repos" if o.get("--repos") else " ".join(o["_"])))
    return 0


def cmd_codemap_forget(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    if not args:
        return usage.error(cmd)
    n = codemap.codemap_forget(con, " ".join(args))
    if n is None:
        print(
            f"no mapped source folder '{' '.join(args)}'; give one exactly as `qa-mem code --repos` lists it",
            file=sys.stderr,
        )
        return 2
    print(f"removed {n} code-map rows for {' '.join(args)}")
    return 0


def cmd_codemap_scan(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    o = usage.opts(args, flags=("--full",), valued=("--out",))
    if "--out" in o and not o["--out"].strip():
        raise core.BadOption("--out needs a folder path")
    paths = o["_"]
    if not paths:
        print("give the source folder(s) to scan, e.g. qa-mem codemap-scan ~/work/traffic-ui ~/work/traffic-api")
        return 2
    from . import scan

    print(json.dumps(scan.codemap_scan(con, paths, o.get("--out") or "qa-out/init", o.get("--full", False)), indent=1))
    return 0


def cmd_blindspots(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    o = usage.opts(args, flags=("--json",))
    if len(o["_"]) > 1:
        raise core.BadOption("blindspots takes at most one route")
    print(gaps.blindspots(con, o["_"][0] if o["_"] else None, o.get("--json", False)))
    return 0


def cmd_gaps(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    o = usage.opts(args, flags=("--changed", "--json"), valued=("--top",))
    if o["_"]:
        raise core.BadOption("gaps takes no positional arguments")
    top = None
    if "--top" in o:
        top = usage.count(o, "--top")
        if top < 1:
            raise core.BadInput("--top must be a whole number >= 1")
    print(gaps.gaps(con, changed_only=o.get("--changed", False), top=top, as_json=o.get("--json", False)))
    return 0


def cmd_crawl(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    if not args:
        return usage.error(cmd)
    from . import crawl

    return crawl.crawl(con, args[0], args[1:])


def cmd_dispatch(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    o = usage.opts(
        args,
        flags=("--faults", "--open-record", "--api-writes", "--all"),
        valued=(
            "--route",
            "--mode",
            "--focus",
            "--known-bugs",
            "--target-route",
            "--hypothesis",
            "--cases",
        ),
    )
    if len(o["_"]) != 1 or not o.get("--route") or o.get("--mode", "ticket") not in ("ticket", "explore"):
        return usage.error(cmd)
    if "--cases" in o and o.get("--all"):
        return usage.error(cmd, "--cases and --all can't go together")
    ids = [x for x in re.split(r"[,\s]+", o["--cases"]) if x] if "--cases" in o else None
    from . import ops

    print(
        json.dumps(
            ops.dispatch(
                con,
                o["_"][0],
                o["--route"],
                ops.Order(
                    mode=o.get("--mode"),
                    focus=o.get("--focus"),
                    faults=o.get("--faults", False),
                    known_bugs=o.get("--known-bugs") or "",
                    open_record=o.get("--open-record", False),
                    api_writes=o.get("--api-writes", False),
                    hypothesis=o.get("--hypothesis"),
                    target_route=o.get("--target-route"),
                    case_ids=ids,
                    all_cases=o.get("--all", False),
                ),
            )
        )
    )
    return 0


def cmd_search(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    if not args:
        return usage.error(cmd)
    print(memory.search(con, " ".join(args)))
    return 0


def cmd_bugs(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    if len(args) > 1:
        return usage.error(cmd)
    status = args[0].upper() if args else "OPEN"
    if status not in core.STATUSES:
        print(f"unknown status '{args[0]}'; use one of {sorted(core.STATUSES)}")
        return 2
    print(memory.bugs(con, status))
    return 0


def cmd_bug_status(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    o = usage.opts(args, valued=("--jira",))
    pos = o["_"]
    if len(pos) not in (2, 3) or (len(pos) == 3 and "--jira" in o):
        return usage.error(cmd)
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
    done, why = memory.set_bug_status(con, prefix, status, jira_key)
    print(why)
    return 0 if done else 1


def cmd_bug_sync(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    o = usage.opts(args, flags=("--jql",))
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
        raise core.BadInput(f"bug-sync: can't read Jira JSON ({e})") from e
    print(memory.bug_sync(con, data))
    return 0


def cmd_prep(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    if len(args) < 2:
        return usage.error(cmd)
    from . import inbox

    print(json.dumps(inbox.prep(con, args[0], args[1:]), indent=1, ensure_ascii=False))
    return 0


def cmd_ingest(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    if len(args) != 1:
        return usage.error(cmd)
    # Same scrubbing as the hook path, so secrets stay out of the DB and hashes match `qa-mem hash`.
    try:
        ingest.ingest(
            con,
            scrub.redact_json(json.loads(Path(args[0]).read_text(encoding="utf-8-sig"))),
            run_dir=Path(args[0]).resolve().parent,
            src=Path(args[0]).resolve(),
        )
    except (OSError, ValueError, RecursionError) as e:  # ValueError: not JSON, or a field ingest rejects
        usage.log_cli_error("ingest", 2, f"{type(e).__name__}: {core.short(e, 200)}")
        print(f"qa-mem: ingest: can't save {args[0]} ({type(e).__name__}: {core.short(e, 200)})", file=sys.stderr)
        return 2
    print("ingested")
    return 0


def cmd_stale(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    if not args:
        return usage.error(cmd)
    build = core.norm_build(" ".join(args))  # normalised like ingest, so it matches stored builds
    if build == core.UNKNOWN:
        print("give the deployed build, e.g. qa-mem stale 1.43.0")
        return 2
    memory.set_build(con, build)
    print(f"current build set to {build}; older knowledge marked stale")
    return 0


HANDLERS = {  # the commands that open the memory DB
    "card": cmd_card,
    "bug-match": cmd_bug_match,
    "impact": cmd_impact,
    "history": cmd_history,
    "hotspots": cmd_hotspots,
    "signals": cmd_signals,
    "signal-ignore": cmd_signal_ignore,
    "signal-unignore": cmd_signal_ignore,
    "patterns": cmd_patterns,
    "questions": cmd_questions,
    "answer": cmd_answer,
    "remember": cmd_remember,
    "forget": cmd_forget,
    "code": cmd_code,
    "codemap-forget": cmd_codemap_forget,
    "codemap-scan": cmd_codemap_scan,
    "blindspots": cmd_blindspots,
    "gaps": cmd_gaps,
    "crawl": cmd_crawl,
    "dispatch": cmd_dispatch,
    "search": cmd_search,
    "bugs": cmd_bugs,
    "bug-status": cmd_bug_status,
    "bug-sync": cmd_bug_sync,
    "prep": cmd_prep,
    "ingest": cmd_ingest,
    "stale": cmd_stale,
}
