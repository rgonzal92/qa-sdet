"""Handlers of the `qa-mem` commands that need no memory DB: run folders, the browser session, reports, browser
signals and the plugin log. One function per command; `HANDLERS` maps the command names to them."""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

# the rest load in the commands that use them (start-up time)
from . import core, scrub, usage


def cmd_keys(args: list[str]) -> int | None:
    from . import ops

    print(json.dumps(ops.expand_keys(args, core.config().get("jiraKeyPrefix"))))
    return 0


def cmd_rundir(args: list[str]) -> int | None:
    from . import ops

    o = usage.opts(args, valued=("--keys", "--route", "--label", "--prefix"))
    if len(o["_"]) != 1 or sum(k in o for k in ("--keys", "--route", "--label")) != 1:
        raise core.BadInput(f"usage: qa-mem rundir {usage.COMMANDS['rundir']}")
    if "--keys" in o:  # a ticket's folder is its key, reused by every re-run: one ticket each, a plan in it too
        keys = ops.expand_keys([o["--keys"]], core.config().get("jiraKeyPrefix"))
        if len(keys) > 1:
            raise core.BadInput(
                f"rundir: one folder per ticket; make one `qa-mem rundir --keys` call per ticket ({', '.join(keys)})"
            )
        if "--prefix" in o:
            raise core.BadInput("rundir: --prefix is for --route and --label; a ticket's plan goes in its folder")
        print(ops.ticket_rundir(o["_"][0], keys[0]))
    else:
        print(
            ops.make_rundir(o["_"][0], ops.run_label(None, o.get("--route"), o.get("--label"), o.get("--prefix") or ""))
        )
    return 0


def cmd_marker(args: list[str]) -> int | None:
    from . import ops

    o = usage.opts(args[1:], flags=("--faults", "--api-writes"))
    if not args or args[0] not in ("start", "stop") or len(o["_"]) != 1:
        raise core.BadInput("usage: qa-mem marker start <project> [--faults] [--api-writes] | marker stop <project>")
    if args[0] == "start":
        print(
            json.dumps(ops.marker_start(o["_"][0], o.get("--faults", False), api_writes=o.get("--api-writes", False)))
        )
    else:
        if len(o) > 1:
            raise core.BadOption("marker stop takes no options")
        ops.marker_stop(o["_"][0])
        print("marker removed")
    return 0


def cmd_browser_open(args: list[str]) -> int | None:
    from . import session

    return session.browser_open(args)


def cmd_session_check(args: list[str]) -> int | None:
    from . import session

    o = usage.opts(args, flags=("--record", "--build"))
    if o["_"]:
        raise core.BadOption("session-check takes no positional arguments")
    return session.session_check(record=o.get("--record", False), read_build=o.get("--build", False))


def cmd_discover(args: list[str]) -> int | None:

    o = usage.opts(args, valued=("--depth", "--max-pages"))
    if len(o["_"]) not in (1, 2):
        raise core.BadInput(f"usage: qa-mem discover {usage.COMMANDS['discover']}")
    from . import crawl

    return crawl.discover(
        o["_"][0],
        o["_"][1] if len(o["_"]) == 2 else None,
        usage.count(o, "--depth", 5) if "--depth" in o else 2,
        (usage.count(o, "--max-pages", 200) if "--max-pages" in o else 25) or 25,
    )


def cmd_page_start(args: list[str]) -> int | None:
    from . import session

    if len(args) != 1:
        raise core.BadInput("usage: qa-mem page-start <url>")
    return session.page_start(args[0])


def cmd_api_probe(args: list[str]) -> int | None:
    from . import probe

    o = usage.opts(args, valued=("--json", "--content-type"), multi=("--header",))
    if len(o["_"]) != 2:
        raise core.BadInput(
            "usage: qa-mem api-probe <METHOD> <url> [--json '<body>'] [--content-type <type>] [--header 'Name: value' ...]"
        )
    return probe.api_probe(o["_"][0], o["_"][1], o.get("--json"), o.get("--content-type"), o.get("--header", []))


def cmd_evidence(args: list[str]) -> int | None:
    from . import evidence

    o = usage.opts(args, valued=("--selector", "--ref", "--label"))
    if len(o["_"]) != 1 or ("--selector" in o) == ("--ref" in o) or not o.get("--label"):
        raise core.BadInput("usage: qa-mem evidence <png> (--selector <css> | --ref <ref>) --label <text>")
    return evidence.evidence(o["_"][0], o["--label"], selector=o.get("--selector"), ref=o.get("--ref"))


def cmd_prune(args: list[str]) -> int | None:
    from . import ops

    if len(args) not in (1, 2):
        raise core.BadInput("usage: qa-mem prune <project> [<run dir>]")
    if len(args) == 2 and not (Path(args[1]).is_dir() and Path(args[1]).resolve().parent.name == "qa-out"):
        raise core.BadInput(f"prune: not a run folder (qa-out/<run>/): {args[1]}")
    n = ops.prune(args[0], run=args[1] if len(args) == 2 else None)
    print(f"pruned {n} old snapshot files" if len(args) == 1 else f"pruned {n} snapshot and temp files of this run")
    return 0


def cmd_annotate(args: list[str]) -> int | None:
    from . import evidence

    o = usage.opts(args, valued=("--label",), multi=("--box",))
    if len(o["_"]) != 1 or not o.get("--label"):
        raise core.BadInput("usage: qa-mem annotate <png> [--box x,y,w,h ...] --label <text>")
    boxes = [evidence.parse_box(b) for b in o.get("--box") or []]  # none: the label alone, as a caption
    png = evidence.evidence_png(o["_"][0])
    if not evidence.annotate(png, boxes, o["--label"]):
        print(evidence.NO_PILLOW)
    else:
        print(f"annotated {png}")
    return 0


def cmd_report(args: list[str]) -> int | None:

    if len(args) != 1:
        raise core.BadInput("usage: qa-mem report <run dir>")
    from . import report

    print(report.write_report(args[0]))
    return 0


def cmd_signals_triage(args: list[str]) -> int | None:

    o = usage.opts(args, valued=("--slug",))
    if len(o["_"]) != 1 or not o.get("--slug") or not (Path(o["_"][0]) / ".data").is_dir():
        raise core.BadInput("usage: qa-mem signals-triage <run dir> --slug <slug>")
    from . import signals

    data = Path(o["_"][0]) / ".data"
    try:
        run = json.loads((data / f"run-{o['--slug']}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        run = {}  # step 9 writes the run file last: nothing named yet
    t = signals.triage_signals(o["_"][0], o["--slug"], run if isinstance(run, dict) else {})
    groups = (
        ("severe", "SEVERE (5xx, failed requests, a 200 with an error body, a leak)"),
        ("client", "CLIENT 4xx"),
        ("other", "OTHER (request loops, console errors)"),
    )
    lines = ["- " + (f"#{i} " if i else "") + key + (f" (×{n})" if n > 1 else "") for key, n, i in t["app"]]
    sev = [signals.signal_severity(key) for key, _, _ in t["app"]]
    print(
        (
            "APP SIGNALS NO BUG EXPLAINS YET (while the browser is open, read a body with `playwright-cli -s=qa response-body "
            f"<#> | qa-mem body-check {shlex.quote(o['_'][0])} --slug {o['--slug']} <#>`; a real failure is a bug, a known one goes in notes by "
            "method and path):\n"
            + "\n".join(
                f"{title}:\n" + "\n".join(line for line, v in zip(lines, sev) if v == kind)
                for kind, title in groups
                if kind in sev
            )
        )
        if lines
        else "no unexplained app signals"
    )
    if t["suspect_200"]:
        slug, d = o["--slug"], o["_"][0]
        print(
            "SUSPECT 200 (a 200 can still be a failure the log can't show; read each body once, while the browser is "
            f"open: `playwright-cli -s=qa response-body <#> | qa-mem body-check {shlex.quote(d)} --slug {slug} <#>`, "
            f"or all of them: `qa-mem body-check {shlex.quote(d)} --slug {slug} --all`):\n"
            + "\n".join(f"- #{i} {call}" for call, i in t["suspect_200"])
        )
    if t["third_party"]:
        print(f"{t['third_party']} third-party signal(s) left out (hosts outside allowedHosts)")
    return 0


def cmd_body_check(args: list[str]) -> int | None:

    o = usage.opts(args, valued=("--slug",), flags=("--all",))
    if o.get("--all"):
        if len(o["_"]) != 1 or not o.get("--slug") or not (Path(o["_"][0]) / ".data").is_dir():
            raise core.BadInput("usage: qa-mem body-check <run dir> --slug <slug> --all   (the browser still open)")
        from . import signals

        r = signals.sweep_bodies(o["_"][0], o["--slug"])
        bad = [x for x in r["checked"] if x[2]]
        print(
            f"body-check --all: {len(r['checked'])} bodies read, {len(bad)} with a finding"
            + (
                f", {r['gone']} with no body left (the browser drops a call's answer when the page navigates: run "
                "this before a reload)"
                if r["gone"]
                else ""
            )
            + (f", {r['unread']} not read ({r['why']})" if r["unread"] else "")
            + (f", {r['left']} left (run it again)" if r["left"] else "")
        )
        for index, what, findings in bad:
            print(f"- #{index} {what}: " + "; ".join(findings))
        if bad:
            print("A real failure is a bug; a harmless one goes in notes by its method and path as printed here.")
        return 0
    if len(o["_"]) != 2 or not o.get("--slug") or not (Path(o["_"][0]) / ".data").is_dir() or not o["_"][1].isdigit():
        raise core.BadInput("usage: <playwright-cli response-body <#>> | qa-mem body-check <run dir> --slug <slug> <#>")
    from . import signals

    what, findings = signals.check_body(o["_"][0], o["--slug"], o["_"][1], sys.stdin.read(200_000))
    print(
        f"body-check #{o['_'][1]}"
        + (f" {what}" if what else "")
        + ": "
        + (
            "no body left to read (the browser drops a call's answer when the page navigates): not judged. Read "
            "answers before a reload, all at once: qa-mem body-check <run dir> --slug <slug> --all"
            if findings is None
            else "nothing suspicious (no error envelope, HTML page or leak signature)"
            if not findings
            else "\n"
            + "\n".join(f"- {f}" for f in findings)
            + "\nA real failure is a bug; a harmless one goes in notes by method and path."
        )
    )
    return 0


def cmd_next_action(args: list[str]) -> int | None:

    if len(args) != 2 or not (Path(args[0]) / ".data").is_dir():
        raise core.BadInput("usage: qa-mem next-action <run dir> <slug>")
    from . import runstate

    print(json.dumps(runstate.next_action(args[0], args[1])))
    return 0


def cmd_explore_next(args: list[str]) -> int | None:

    o = usage.opts(args, valued=("--run",))
    if len(o["_"]) != 1:
        raise core.BadInput("usage: qa-mem explore-next <project> [--run DIR]")
    from . import runstate

    print(json.dumps(runstate.explore_next(o["_"][0], o.get("--run"))))
    return 0


def cmd_trace(args: list[str]) -> int | None:
    from . import ops

    o = usage.opts(args, valued=("--keys", "--fix-version"))
    if len(o["_"]) != 1:
        raise core.BadInput("usage: qa-mem trace <project> [--keys K1,K2] [--fix-version VER]")
    from . import inbox

    keys = ops.expand_keys([o["--keys"]], core.config().get("jiraKeyPrefix")) if o.get("--keys") else None
    print(inbox.trace(o["_"][0], keys, o.get("--fix-version")))
    return 0


def cmd_dup_check(args: list[str]) -> int | None:

    if len(args) != 2 or not (Path(args[0]) / ".data").is_dir() or not re.fullmatch(r"BUG-\d+", args[1]):
        raise core.BadInput("usage: <acli jira workitem search ... --json> | qa-mem dup-check <run dir> <BUG-n>")
    try:
        data = json.loads(sys.stdin.read() or "[]")
    except ValueError as e:
        raise core.BadInput(f"dup-check: stdin is not the acli search JSON ({e})") from e
    items = (
        data
        if isinstance(data, list)
        else next(
            (
                data[k]
                for k in ("issues", "workItems", "values")
                if isinstance(data, dict) and isinstance(data.get(k), list)
            ),
            [],
        )
    )
    hits = [
        {
            "key": str(x.get("key")),
            "summary": scrub.redact(
                " ".join(str((f if isinstance(f := x.get("fields"), dict) else x).get("summary") or "").split())
            )[:200],
        }
        for x in items[:10]
        if isinstance(x, dict) and x.get("key")
    ]
    f = Path(args[0]) / ".data" / "jira-dups.json"
    try:
        saved = json.loads(f.read_text(encoding="utf-8"))
        saved = saved if isinstance(saved, dict) else {}
    except (OSError, ValueError):
        saved = {}
    saved[args[1]] = hits
    core.write(f, json.dumps(saved, indent=1) + "\n")
    print(f"{args[1]}: {len(hits)} possible duplicate(s) saved; REPORT.md shows them above its filing command")
    return 0


def cmd_audit_bugs(args: list[str]) -> int | None:

    if len(args) != 1 or not Path(args[0]).is_dir():
        raise core.BadInput("usage: qa-mem audit-bugs <run dir>")
    from . import audit

    found = audit.audit_bugs(args[0])
    print(
        "\n".join(
            f"{bid}: {'CRITICAL ' if crit else ''}{text}" for bid, issues in found.items() for crit, text in issues
        )
        or "bug drafts and repro specs: nothing to fix"
    )
    return 0


def cmd_plan_find(args: list[str]) -> int | None:
    from . import ops, runstate

    o = usage.opts(args, valued=("--keys", "--run"))
    if len(o["_"]) != 1 or sum(k in o for k in ("--keys", "--run")) != 1:
        raise core.BadInput("usage: qa-mem plan-find <project> (--keys K1,K2 | --run DIR)")
    keys = ops.expand_keys([o["--keys"]], core.config().get("jiraKeyPrefix")) if "--keys" in o else None
    print(json.dumps(runstate.plan_find(o["_"][0], keys=keys, run_dir=o.get("--run")), indent=1))
    return 0


def cmd_setup_permissions(args: list[str]) -> int | None:

    from . import doctor

    o = usage.opts(args, flags=("--apply",), valued=("--settings",))
    if o["_"]:
        raise core.BadInput("usage: qa-mem setup-permissions [--settings PATH] [--apply]")
    if not o.get("--apply"):
        print("\n".join(doctor.PERMISSION_RULES))
        return 0
    if "--settings" in o and not o["--settings"].strip():
        raise core.BadOption("--settings needs a path")  # an empty shell variable must not fall back to the real file
    added = doctor.merge_permissions(o.get("--settings") or "~/.claude/settings.json")
    print("added to permissions.allow:\n" + "\n".join(added) if added else "all rules already present; nothing changed")
    return 0


def cmd_plugin_log(args: list[str]) -> int | None:

    from . import digest, plugin_log

    if not args or args[0] not in ("note", "export", "show", "clear"):
        raise core.BadInput(f"usage: qa-mem plugin-log {usage.COMMANDS['plugin-log']}")
    sub, rest = args[0], args[1:]
    if sub == "note":
        o = usage.opts(rest, valued=("--kind", "--severity", "--project"))
        proj = plugin_log.resolve_project(o.get("--project"))
        rec = plugin_log.add_note(
            proj, " ".join(o["_"]), kind=o.get("--kind") or "nuisance", severity=o.get("--severity")
        )
        tag = rec["kind"] + (f"/{rec['severity']}" if rec.get("severity") else "")
        ctx = f" ({rec['run']})" if rec.get("run") else ""
        print(f"logged [{tag}]{ctx} to {proj / 'qa-out' / plugin_log.LOG_NAME}")
    elif sub == "export":
        o = usage.opts(rest, flags=("--all", "--preview", "--stdout"), valued=("--since", "--project"))
        if o["_"]:
            raise core.BadOption("plugin-log export takes no positional arguments")
        if o.get("--all") and "--since" in o:
            raise core.BadOption("give either --all or --since, not both")
        proj = plugin_log.resolve_project(o.get("--project"))
        res = digest.export_digest(
            proj,
            all_events=o.get("--all", False),
            since=o.get("--since"),
            preview=o.get("--preview", False),
            stdout=o.get("--stdout", False),
        )
        if (o.get("--preview") or o.get("--stdout")) and res["markdown"]:
            print(res["markdown"], end="")
        else:
            print(res["summary"])
    elif sub == "show":
        o = usage.opts(rest, valued=("--limit", "--type", "--project"))
        if o["_"]:
            raise core.BadOption("plugin-log show takes no positional arguments")
        limit = usage.count(o, "--limit", 2000) if "--limit" in o else 30
        if "--limit" in o and limit < 1:
            raise core.BadInput("--limit must be a whole number >= 1")
        proj = plugin_log.resolve_project(o.get("--project"))
        print(plugin_log.show_log(proj, limit=limit, event_type=o.get("--type")))
    else:  # clear
        o = usage.opts(rest, valued=("--project",))
        if o["_"]:
            raise core.BadOption("plugin-log clear takes no positional arguments")
        proj = plugin_log.resolve_project(o.get("--project"))
        print(plugin_log.clear_log(proj))
    return 0


HANDLERS = {  # the commands that need no memory DB
    "keys": cmd_keys,
    "rundir": cmd_rundir,
    "marker": cmd_marker,
    "browser-open": cmd_browser_open,
    "session-check": cmd_session_check,
    "discover": cmd_discover,
    "page-start": cmd_page_start,
    "api-probe": cmd_api_probe,
    "evidence": cmd_evidence,
    "prune": cmd_prune,
    "annotate": cmd_annotate,
    "report": cmd_report,
    "signals-triage": cmd_signals_triage,
    "body-check": cmd_body_check,
    "next-action": cmd_next_action,
    "explore-next": cmd_explore_next,
    "trace": cmd_trace,
    "dup-check": cmd_dup_check,
    "audit-bugs": cmd_audit_bugs,
    "plan-find": cmd_plan_find,
    "setup-permissions": cmd_setup_permissions,
    "plugin-log": cmd_plugin_log,
}
