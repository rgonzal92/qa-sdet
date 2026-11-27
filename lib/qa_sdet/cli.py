"""qa-mem: compact SQLite memory of the app under test.

Agents read it (card/search/bugs/hash/code). Knowledge is written only by `ingest`, which the plugin's
Stop/SubagentStop hook runs on every changed qa-out/<run>/.data/run-*.json, and by the code map: `codemap-scan` lists
source files deterministically, the qa-mapper agent writes qa-out/init/code-map-<area>.json, and the same
hook ingests it into the `codemap` table (hints from source, never requirements). The user's manual writes
are `bug-status` and `stale`.

v0.4 run folders (qa-out/<KEY>/ per ticket, reused by its re-runs; qa-out/<date>-<label>/ per exploration): `rundir`
makes one, explorers write .data/run-<slug>.json (ingested like run.json), and `report` renders REPORT.md +
qa-out/README.md from .data/plan.json and those files.
Wrappers (`marker`, `browser-open`, `prune`, `annotate`, `setup-permissions`) keep skill commands to one shape.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import select
import sqlite3
import sys
import time
from pathlib import Path

# the rest load in the commands that use them (start-up time)
from . import core, ingest, scrub, usage

TYPE_CHECKING = False  # mypy reads it as True (pyproject always_true): typing stays out of the import path
if TYPE_CHECKING:
    from .contracts import JsonObj


def wrapper_cmd(cmd: str, args: list[str]) -> int | None:
    """The v0.4 commands that need no memory DB. Returns an exit code, or None for other commands."""
    from . import cli_run

    handler = cli_run.HANDLERS.get(cmd)
    return None if handler is None else handler(args)


class _StderrTee:
    """sys.stderr that still prints everything and keeps the start, so cli_error logs why a command failed."""

    def __init__(self, real):
        self.real, self.seen = real, ""

    def write(self, text):
        if len(self.seen) < 400:
            self.seen += text[: 400 - len(self.seen)]
        return self.real.write(text)

    def __getattr__(self, name):  # flush, isatty, encoding, fileno, ... stay the real stream's
        return getattr(self.real, name)

    def reason(self, cmd, code):
        line = next((x.strip() for x in self.seen.splitlines() if x.strip()), "")
        return line.removeprefix("qa-mem: ") if line else f"{cmd} exited {code}"


@contextlib.contextmanager
def _tee_stderr():
    tee = _StderrTee(sys.stderr)
    sys.stderr = tee
    try:
        yield tee
    finally:
        sys.stderr = tee.real


def _run_marker(roots):
    """The active run marker that names an explorer's run (run_dir and slug, from dispatch), or None."""
    for root in roots:
        root = Path(root)
        qa_out = root if root.name == "qa-out" else next((p for p in root.parents if p.name == "qa-out"), root)
        try:
            m = json.loads((qa_out / ".active").read_text())
            if (
                isinstance(m, dict)
                and m.get("run_dir")
                and m.get("slug")
                and float(m.get("expires") or 0) > time.time()
            ):
                return m
        except (OSError, ValueError, TypeError):
            continue
    return None


def hook_input() -> JsonObj:
    """The hook's JSON on stdin (Claude Code sends it and closes stdin); {} when run by hand."""
    try:
        # 2 s: on a loaded machine the payload can come late, and missing stop_hook_active forces an extra round
        if sys.stdin is None or sys.stdin.isatty() or not select.select([sys.stdin], [], [], 2.0)[0]:
            return {}
        data = json.loads(sys.stdin.read() or "{}")
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError, RecursionError):
        return {}


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    try:
        return _main(argv)
    except core.BadInput as e:  # also the refusal of a pre-0.4 DB, from core.connect()
        usage.log_cli_error(cmd, 2, e)
        print(f"qa-mem: {e}", file=sys.stderr)
        return 2
    except sqlite3.DatabaseError as e:
        usage.log_cli_error(cmd, 1, e)
        if "locked" not in str(e) and "busy" not in str(e):
            print(f"qa-mem: the memory DB {core.db_path()} failed: {e}", file=sys.stderr)
            return 1
        print(
            f"qa-mem: the memory DB {core.db_path()} stayed busy (another qa-mem or a run's hook is writing to it); "
            "the command did not finish: run it again in a minute",
            file=sys.stderr,
        )
        return 1


def _ingest_pending(args: list[str]) -> int:
    """`qa-mem ingest-pending`: runs from hooks: must never break the session."""
    from . import plugin_log

    ingest.PARKED.clear()
    roots = list(dict.fromkeys(os.path.realpath(a) for a in (args or ["qa-out"]) if a))  # cwd + project dir
    marker = _run_marker(roots)  # an explorer's run (dispatch wrote run_dir and slug)
    hin = hook_input() if marker else None
    role = str((hin or {}).get("agent_type") or "").strip().lower().split(":")[-1].strip()
    explorer_stops = marker and (hin or {}).get("hook_event_name") == "SubagentStop" and role == "qa-explorer"
    # the earlier attempt's results back in its run file before memory and the stop check read it
    if explorer_stops:
        from . import results

        try:  # the copy stays: a bounced explorer may write its file again (the report or next dispatch removes it)
            results.carry_prior(marker["run_dir"], str(marker["slug"]), keep=True)
        except Exception as e:  # noqa: BLE001
            plugin_log.append_event(
                Path(marker["run_dir"]).parent,
                "hook_error",
                {"hook": "ingest-pending", "error_type": type(e).__name__, "detail": str(e)},
                create=False,
            )
    for root in roots:
        try:
            ingest.ingest_pending(root)
        except Exception as e:  # noqa: BLE001
            plugin_log.append_event(
                root,
                "hook_error",
                {"hook": "ingest-pending", "error_type": type(e).__name__, "detail": str(e)},
                create=False,
            )
            print(f"qa-mem: {e}", file=sys.stderr)
    # parallel mappers: the first to stop parks every bad code map, so each mapper hears about all still parked
    stale = [
        x
        for root in roots
        for x in ingest.parked_codemaps(root)
        if not any(p.startswith(x.split(": ", 1)[0] + ":") for p in ingest.PARKED)
    ]
    if hin is None:  # stdin is read once: above when a marker is active
        hin = hook_input() if ingest.PARKED or stale else {}
        role = str(hin.get("agent_type") or "").strip().lower().split(":")[-1].strip()
    if stale and hin.get("hook_event_name") == "SubagentStop" and role == "qa-mapper":
        ingest.PARKED.extend(stale)
    todo = []
    if explorer_stops and not hin.get("stop_hook_active"):
        from . import audit, ops  # the marker was started MARKER_HOURS before it expires

        todo = audit.verify_run(marker["run_dir"], marker["slug"], float(marker["expires"]) - ops.MARKER_HOURS * 3600)
    if todo:
        plugin_log.append_event(
            Path(marker["run_dir"]).parent,
            "stop_bounce",
            {
                "agent": role,
                "kind": "verify_run",
                "run": Path(marker["run_dir"]).name,
                "slug": marker.get("slug"),
                "items": todo,
            },
            create=False,
        )
        print(
            "qa-sdet: before you stop, finish these (then stop again):\n" + "\n".join(f"- {t}" for t in todo),
            file=sys.stderr,
        )
        if not ingest.PARKED:
            return 2
    if ingest.PARKED and not hin.get("stop_hook_active"):
        qa_out = next((Path(r) for r in roots if Path(r).is_dir()), None)
        if qa_out:
            plugin_log.append_event(
                qa_out,
                "stop_bounce",
                {
                    "agent": role or "main",
                    "kind": "parked",
                    "items": ingest.PARKED[:5],
                },
                create=False,
            )
        # Exit 2 from a Stop/SubagentStop hook shows this to the agent that just wrote the file, once.
        refs = Path(__file__).resolve().parent.parent.parent / "references"
        print(
            f"qa-sdet memory could not save this (file(s) below). For each one YOU wrote this run: fix the field the "
            f"error names (rules: {refs / 'run-json.md'} for run JSON, {refs / 'code-map.md'} for code-map-*.json) "
            "and rewrite the whole file; it is re-read automatically. A file you did not write belongs to another "
            "agent: leave it.\n" + "\n".join(ingest.PARKED),
            file=sys.stderr,
        )
        return 2
    return 0


def _inbox(args: list[str]) -> int:
    from . import inbox

    o = usage.opts(args)
    if len(o["_"]) != 1:
        raise core.BadInput("usage: qa-mem inbox <project>   (the acli JSON on stdin)")
    try:
        data = json.loads(sys.stdin.read())
    except (OSError, ValueError, RecursionError) as e:  # RecursionError: garbage nested too deep
        raise core.BadInput(f"inbox: can't read Jira JSON ({e})") from e
    items = inbox.inbox_items(data)  # bad input fails before the DB is opened
    con = core.connect()
    try:
        print(inbox.inbox(con, o["_"][0], items, core.config().get("jiraKeyPrefix")), end="")
    finally:
        con.close()
    return 0


def _main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):  # a non-UTF-8 argument echoed back must not crash the print
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]  # a replaced stream: AttributeError, caught below
        except (AttributeError, ValueError):
            pass
    # bytes that aren't UTF-8 arrive as surrogates, which sqlite3 and print() refuse: make them U+FFFD
    argv = [a.encode("utf-8", "surrogateescape").decode("utf-8", "replace") for a in argv]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage.USAGE)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd not in usage.COMMANDS:
        print(f"qa-mem: unknown command {cmd!r}\n{usage.USAGE}", file=sys.stderr)
        return 2
    if args and not usage.COMMANDS[cmd]:
        if any(a in ("-h", "--help") for a in args):
            return usage.error(cmd)
        return usage.error(cmd, f"{cmd} takes no arguments")
    # an option the usage line doesn't list is a typo (`stale --bogus` must not set the build to "--bogus");
    # option values (after --label, --box, …) and `hash` symptom text may start with "--" or be "-h"
    known = set(re.findall(r"--[a-z][a-z-]*[a-z]", usage.COMMANDS[cmd]))  # --known-bugs is one option
    valued = set(re.findall(r"(--[a-z][a-z-]*[a-z])\s+[^|\[\]]", usage.COMMANDS[cmd]))
    skip = False
    for k, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a in ("-h", "--help") and not (cmd == "hash" and k >= 1 and a == "-h"):
            return usage.error(cmd)
        if cmd != "hash" and re.match(r"--[A-Za-z]", a) and a.split("=")[0] not in known:
            return usage.error(cmd, f"unknown option {a}")
        skip = a in valued
    if cmd == "ingest-pending":
        return _ingest_pending(args)
    if cmd == "init-status":
        if len(args) != 1:
            return usage.error(cmd)
        print(json.dumps(ingest.init_status(args[0]), indent=1))
        return 0
    if cmd == "doctor":
        from . import doctor

        problems = doctor.doctor()
        if problems:
            usage.log_cli_error("doctor", 1, f"{problems} doctor gap(s)")
        return 1 if problems else 0
    try:
        with _tee_stderr() as tee:
            code_ = wrapper_cmd(cmd, args)
        if code_ is not None:
            if code_ != 0:
                usage.log_cli_error(cmd, code_, tee.reason(cmd, code_))
            return code_
        if cmd == "inbox":
            return _inbox(args)
    except core.BadOption as e:
        return usage.error(cmd, str(e))
    except core.BadInput as e:
        usage.log_cli_error(cmd, 2, e)
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
        with _tee_stderr() as tee:
            rc = run_cmd(con, cmd, args)
        if rc != 0 and cmd != "ingest":
            usage.log_cli_error(cmd, rc, tee.reason(cmd, rc))
        return rc
    except core.BadOption as e:
        return usage.error(cmd, str(e))
    finally:
        con.close()


def run_cmd(con: sqlite3.Connection, cmd: str, args: list[str]) -> int:
    from . import cli_memory

    handler = cli_memory.HANDLERS.get(cmd)
    return usage.error(cmd) if handler is None else handler(con, cmd, args)
