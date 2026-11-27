"""The `qa-mem` command table (`COMMANDS`: each command and its arguments), option parsing and usage errors."""

from __future__ import annotations

import sys

from . import core

TYPE_CHECKING = False  # mypy reads it as True (pyproject always_true): typing stays out of the import path
if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from typing import Any


def opts(
    args: Sequence[str], flags: Collection[str] = (), valued: Collection[str] = (), multi: Collection[str] = ()
) -> dict[str, Any]:
    """{'--x': value or True, '--m': [values], '_': [positionals]} from --x v / --x=v / --flag; unknown options are
    BadInput."""
    out: dict[str, Any] = {"_": []}
    it = iter(args)
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


def count(o: Mapping[str, Any], name: str, most: int | None = None) -> int:
    try:
        n = int(o[name]) if name in o else 0  # `--depth ""` is a mistake, not "none"
    except (ValueError, TypeError):
        n = -1
    if n < 0:
        raise core.BadInput(f"{name} must be a whole number >= 0")
    if most is not None and n > most:
        raise core.BadInput(f"{name} must be at most {most}")
    return n


COMMANDS = {  # name -> arguments, for `qa-mem` (no args), `qa-mem <cmd> --help` and bad input
    "doctor": "",
    "browsers": "",
    "where": "",
    "card": "<route|page name> [--focus <terms>] [--ids]",
    "patterns": "[--min-pages N] [--json]",
    "signals": "[route] [--status ACTIVE|IGNORED|ALL]",
    "signal-ignore": "<id>",
    "signal-unignore": "<id>",
    "history": "[route] [--builds N] [--json]",
    "hotspots": "[--top N] [--json]",
    "bug-match": "<route> <symptom> [--endpoint '<METHOD /path>']",
    "impact": "<route|page name|METHOD /path> [--json]",
    "questions": "[open|resolved|all]",
    "answer": "<question id> <answer>",
    "remember": "<route|*> <rule|flow|api|validation|note> <text> [--build-scoped] [--source <src>]",
    "forget": "fact <id> | forget page <route> [--force]",
    "gaps": "[--changed] [--top N] [--json]",
    "blindspots": "[route] [--json]",
    "search": "<words> [-word to exclude]",
    "bugs": "[STATUS]",
    "hash": "<route> <symptom>",
    "bug-status": "<hash-prefix> <" + "|".join(sorted(core.STATUSES)) + "> [--jira KEY]",
    "bug-sync": "[--jql]",
    "stale": "<build>",
    "ingest": "<run.json>",
    "ingest-pending": "<dir>...",
    "code": "[route] | code --repos",
    "codemap-scan": "[--full] [--out DIR] <source folder>...",
    "init-status": "<init dir>",
    "codemap-forget": "<source folder>",
    "keys": "<number|KEY>...",
    "rundir": "<project> (--keys KEY | --route <route> [--prefix plan-] | --label <text> [--prefix plan-])",
    "marker": "start <project> [--faults] [--api-writes] | marker stop <project>",
    "browser-open": "[--session qa|qa-login] [--headed] [<url>]",
    "session-check": "[--record] [--build]",
    "page-start": "<url>",
    "crawl": "<run dir> [<route>...]",
    "discover": "<run dir> [<url>] [--depth D] [--max-pages N]",
    "api-probe": "<GET|HEAD|OPTIONS|POST|PUT|PATCH|DELETE> <url> [--json '<body>'] [--content-type <type>] [--header 'Name: value' ...]",
    "dispatch": "<run dir> --route <route> [--mode ticket|explore] [--focus <charter>] "
    "[--faults] [--api-writes] [--known-bugs <text>] [--open-record [--target-route <detail route>]] "
    "[--hypothesis '<why this page>'] [--cases TC-2,TC-5 | --all]",
    "evidence": "<png> (--selector <css> | --ref <ref>) --label <text>",
    "prune": "<project> [<run dir>]",
    "annotate": "<png> [--box x,y,w,h ...] --label <text>",
    "report": "<run dir>",
    "audit-bugs": "<run dir>",
    "dup-check": "<run dir> <BUG-n>   (acli search JSON on stdin)",
    "trace": "<project> [--keys K1,K2] [--fix-version VER]",
    "next-action": "<run dir> <slug>",
    "explore-next": "<project> [--run DIR]",
    "signals-triage": "<run dir> --slug <slug>",
    "body-check": "<run dir> --slug <slug> <#> | --all   (the response body of request <#> on stdin; --all reads "
    "every unchecked one from the open browser)",
    "plan-find": "<project> (--keys K1,K2 | --run DIR)",
    "prep": "<project> <number|KEY>...",
    "inbox": "<project>",
    "setup-permissions": "[--settings PATH] [--apply]",
    "plugin-log": "note [--kind bug|nuisance|idea] [--severity low|med|high] [--project DIR] <text> | "
    "export [--all] [--since <ISO|Nd>] [--preview] [--stdout] [--project DIR] | "
    "show [--limit N] [--type TYPE] [--project DIR] | clear [--project DIR]",
}
USAGE = "usage: qa-mem <command> [args]; commands:\n" + "\n".join(
    f"  qa-mem {c} {a}".rstrip() for c, a in COMMANDS.items()
)


def log_cli_error(cmd, code, reason):
    """Log a qa-mem exit 1/2 failure to <project>/qa-out/plugin-log.jsonl when qa-out already exists."""
    if not cmd or cmd in ("plugin-log", "-h", "--help", "help"):
        return
    try:
        from . import plugin_log

        plugin_log.append_event(
            plugin_log.resolve_project() / "qa-out",
            "cli_error",
            {"cmd": cmd, "exit_code": code, "reason": str(reason)},
            create=False,
        )
    except Exception:  # noqa: BLE001, S110
        pass


def error(cmd: str, why: str = "") -> int:
    if why:
        log_cli_error(cmd, 2, why)
    print(f"qa-mem: {why + '; ' if why else ''}usage: qa-mem {cmd} {COMMANDS.get(cmd, '')}".rstrip(), file=sys.stderr)
    return 2
