"""The per-project plugin log (<project>/qa-out/plugin-log.jsonl): its writer, `show` and `clear`.

Automatic hooks and qa-mem append scrubbed JSONL events (denials, hook errors, nudges, failed tool calls, stop-hook
bounces, CLI errors, notices, and 1-line run summaries). Users add manual notes via `qa-mem plugin-log note` or
`/qa-sdet:qa-feedback`. Setting `"pluginLog": false` in ~/.config/qa-sdet/config.json disables all writes.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path

from . import core, scrub

TYPE_CHECKING = False  # mypy reads it as True (pyproject always_true): typing stays out of the import path
if TYPE_CHECKING:  # NOSONAR
    from collections.abc import Mapping

    from .contracts import JsonObj


LOG_NAME = "plugin-log.jsonl"
CURSOR_NAME = ".plugin-log.cursor"
MAX_EVENTS = 2000
TRIM_BYTES = 150_000
READ_CAP = 8 << 20
NOTE_KINDS = ("bug", "nuisance", "idea")
SEVERITIES = ("low", "med", "high")
# notice: a change the plugin made on its own that nobody would see otherwise (an unreadable build, a dropped bug)
EVENT_TYPES = (
    "denial",
    "hook_error",
    "nudge",
    "tool_fail",
    "stop_bounce",
    "cli_error",
    "notice",
    "run_summary",
    "user_note",
)
_PLUGIN_ROOT = Path(__file__).resolve().parents[2]

# The secret forms hooks/guard.py mask() covers and scrub.py doesn't, so tool_fail and cli_error calls stay as clean as
# denials; credential-shaped values only, so notes stay readable (tests/test_guard.py LibMirrorsGuard pins the two).
_CLI_MASKS = (
    (re.compile(r"(?i)\b(basic)(\s+)(?!\[REDACTED)(?=[A-Za-z0-9+/]*[0-9+/=])[A-Za-z0-9+/]{8,}=*"), r"\1\2[REDACTED]"),
    (
        re.compile(r"(?i)(token|password|passwd|pwd|secret|api[_-]?key)(\\?['\"]?\s*[=:]\s*\\?['\"]?)[^\s'\"&\\]+"),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"(?i)(\s--?[\w-]*(?:token|password|passwd|secret|api[_-]?key)\s+['\"]?)[^\s'\"]+"), r"\1[REDACTED]"),
    (re.compile(r"(\s(?:-u|--user)\s*['\"]?[^\s:'\"]+:)[^\s'\"]+"), r"\1[REDACTED]"),
)


@functools.cache
def plugin_version() -> str:
    """Version from .claude-plugin/plugin.json, cached per process."""
    try:
        v = json.loads((_PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")).get("version")
        return str(v).strip() if v else core.UNKNOWN
    except (OSError, ValueError, AttributeError):
        return core.UNKNOWN


def _read_config(config_path=None):
    p = Path(config_path) if config_path else core.CONFIG
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def enabled(config_path: str | Path | None = None) -> bool:
    """On by default; disabled only when config explicitly sets `"pluginLog": false`."""
    return _read_config(config_path).get("pluginLog") is not False


def scrub_text(text: object, cap_len: int = 400) -> str:
    """Single-line secret-scrubbed text safe for local JSONL storage."""
    s = str(text or "").encode("utf-8", "replace").decode("utf-8")
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", s).strip()
    s = scrub.redact_json_text(scrub.redact(s))
    for rx, rep in _CLI_MASKS:
        s = rx.sub(rep, s)
    return s[:cap_len] if cap_len and len(s) > cap_len else s


def _scrub_fields(obj, long_keys=("text",)):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if v is None:
                continue
            if isinstance(v, str):
                if scrub.SECRET_NAME_RE.match(str(k)):
                    out[k] = "[REDACTED]" if v else ""
                else:
                    out[k] = scrub_text(v, cap_len=1500 if k in long_keys else 400)
            else:
                out[k] = _scrub_fields(v, long_keys)
        return out
    if isinstance(obj, list):
        return [_scrub_fields(x, long_keys) for x in obj[:20]]
    return obj


def _trim_log(log_path, cap=MAX_EVENTS):
    """Keep only the newest `cap` lines atomically, never following links or FIFOs."""
    try:
        # not a vulnerability (S2083): the project's own qa-out (CLAUDE_PROJECT_DIR or the cwd), not agent data
        fd = os.open(log_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)  # NOSONAR
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return
            lines = fh.read(READ_CAP).decode("utf-8", "replace").splitlines(keepends=True)
        if len(lines) <= cap:
            return
        keep = lines[-cap:]
        tfd, tmp = tempfile.mkstemp(dir=log_path.parent, prefix=".plugin-log.", suffix=".tmp")
        try:
            with os.fdopen(tfd, "w", encoding="utf-8", errors="replace") as fh:
                fh.writelines(keep)
            os.chmod(tmp, 0o600)
            # not a vulnerability (S2083): the project's own qa-out (CLAUDE_PROJECT_DIR or the cwd), not agent data
            os.replace(tmp, log_path)  # NOSONAR
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError:
        pass


def append_event(
    qa_out: str | Path,
    event_type: str,
    fields: Mapping[str, object] | None = None,
    *,
    create: bool = False,
    ts: str | None = None,
    config_path: str | Path | None = None,
) -> bool:
    """Append one scrubbed JSONL event to <qa_out>/plugin-log.jsonl. Best effort: never raises."""
    try:
        if not enabled(config_path):
            return False
        folder = Path(qa_out)
        if create:
            # not a vulnerability (S2083): the project's own qa-out (CLAUDE_PROJECT_DIR or the cwd), not agent data
            folder.mkdir(parents=True, exist_ok=True)  # NOSONAR
        # not a vulnerability (S6549): a local path the user's own project or config names, not agent data
        elif not folder.is_dir():  # NOSONAR
            return False
        rec = {"ts": ts or core.now(), "v": plugin_version(), "type": str(event_type)}
        if isinstance(fields, dict):
            rec.update(_scrub_fields(fields))
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
        log = folder / LOG_NAME
        # not a vulnerability (S2083): the project's own qa-out (CLAUDE_PROJECT_DIR or the cwd), not agent data
        fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)  # NOSONAR
        size = 0
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                return False
            fh.write(line)
            fh.flush()
            size = os.fstat(fh.fileno()).st_size
        if size > TRIM_BYTES:
            _trim_log(log)
        return True
    except Exception:  # noqa: BLE001 - logging must never break hooks or CLI
        return False


def resolve_project(project: str | None = None, cwd: str | None = None) -> Path:
    """Resolve the project folder from explicit --project, CLAUDE_PROJECT_DIR, cwd, or os.getcwd()."""
    raw = project or os.environ.get("CLAUDE_PROJECT_DIR") or cwd or os.getcwd()
    return Path(raw).resolve()


def latest_run_context(qa_out):
    """Best-effort {run, slug, mode, route} from active marker or newest run folder under qa_out."""
    qa_out = Path(qa_out)
    ctx = {}
    try:
        marker = qa_out / ".active"
        if marker.is_file() and not marker.is_symlink():
            m = json.loads(marker.read_text(encoding="utf-8"))
            if isinstance(m, dict) and float(m.get("expires") or 0) > time.time() and m.get("run_dir"):
                rdir = Path(m["run_dir"])
                ctx["run"] = rdir.name
                if m.get("slug"):
                    ctx["slug"] = str(m["slug"])
                _fill_run_meta(rdir, ctx)
                return ctx
    except (OSError, ValueError, TypeError):
        pass
    try:
        runs = [
            d
            for d in qa_out.iterdir()
            if d.is_dir() and not d.is_symlink() and d.name != "init" and (d / ".data").is_dir()
        ]
        runs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        if runs:
            ctx["run"] = runs[0].name
            _fill_run_meta(runs[0], ctx)
    except OSError:
        pass
    return ctx


def _fill_run_meta(run_dir, ctx):
    data = Path(run_dir) / ".data"
    slug = ctx.get("slug")
    run_file = (data / f"run-{slug}.json") if slug else None
    if not (run_file and run_file.is_file()):
        cands = sorted(data.glob("run-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        run_file = cands[0] if cands else None
    if run_file and run_file.is_file():
        if not slug:
            ctx["slug"] = run_file.stem.removeprefix("run-")
        try:
            doc = json.loads(run_file.read_text(encoding="utf-8-sig"))
            if isinstance(doc, dict):
                if doc.get("mode"):
                    ctx["mode"] = str(doc["mode"])
                if doc.get("target"):
                    ctx["route"] = core.norm_route(doc["target"])
                return
        except (OSError, ValueError):
            pass
    try:
        plan = json.loads((data / "plan.json").read_text(encoding="utf-8-sig"))
        if isinstance(plan, dict) and plan.get("kind"):
            ctx["mode"] = str(plan["kind"])
    except (OSError, ValueError):
        pass


def add_note(project: Path, text: str, kind: str = "nuisance", severity: str | None = None) -> JsonObj:
    """Record a manual user note in <project>/qa-out/plugin-log.jsonl and return the stored record."""
    if not enabled():
        raise core.BadInput(f"pluginLog is disabled in {core.CONFIG}; remove it or set it to true to log notes")
    kind = str(kind or "nuisance").strip().lower()
    if kind not in NOTE_KINDS:
        raise core.BadInput(f"plugin-log note: --kind must be one of {', '.join(NOTE_KINDS)} (got {kind!r})")
    if severity is not None:
        severity = str(severity).strip().lower()
        if severity not in SEVERITIES:
            raise core.BadInput(
                f"plugin-log note: --severity must be one of {', '.join(SEVERITIES)} (got {severity!r})"
            )
    body = scrub_text(text, cap_len=1500)
    if not body:
        raise core.BadInput("plugin-log note: give a note describing the bug, nuisance, or idea")
    proj = Path(project).resolve()
    if not proj.is_dir():
        raise core.BadInput(f"plugin-log note: not a folder: {proj}")
    qa_out = proj / "qa-out"
    qa_out.mkdir(exist_ok=True)
    from . import ops

    ops.git_exclude(proj)
    fields = {"agent": "user", "kind": kind, "text": body}
    if severity:
        fields["severity"] = severity
    fields.update(latest_run_context(qa_out))
    ts = core.now()
    if not append_event(qa_out, "user_note", fields, create=True, ts=ts):
        raise core.BadInput(f"plugin-log note: could not write {qa_out / LOG_NAME}")
    return {"ts": ts, "v": plugin_version(), "type": "user_note", **fields}


# ---------- Reading, cursor, show & clear ----------
def _read_events(qa_out):
    """Return ([(line_hash, dict), ...], bad_lines) from <qa_out>/plugin-log.jsonl."""
    log = Path(qa_out) / LOG_NAME
    try:
        fd = os.open(log, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return [], 0
            raw = fh.read(READ_CAP).decode("utf-8", "replace")
    except OSError:
        return [], 0
    items, bad = [], 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
            if isinstance(doc, dict) and doc.get("type"):
                h = hashlib.sha1(line.encode("utf-8", "replace"), usedforsecurity=False).hexdigest()[:12]
                items.append((h, doc))
            else:
                bad += 1
        except ValueError:
            bad += 1
    return items, bad


def _read_cursor(qa_out):
    try:
        doc = json.loads((Path(qa_out) / CURSOR_NAME).read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cursor(qa_out, line_hash, ts):
    core._write(Path(qa_out) / CURSOR_NAME, json.dumps({"ts": ts, "line_hash": line_hash}) + "\n")


def show_log(project: Path, *, limit: int = 30, event_type: str | None = None) -> str:
    """Compact local terminal view of recent events (raw local, secrets already scrubbed on write)."""
    qa_out = Path(project).resolve() / "qa-out"
    items, _ = _read_events(qa_out)
    events = [d for _, d in items]
    if event_type:
        if event_type not in EVENT_TYPES:
            raise core.BadInput(f"plugin-log show: --type must be one of {', '.join(EVENT_TYPES)} (got {event_type!r})")
        events = [e for e in events if e.get("type") == event_type]
    if not events:
        return "no plugin-log events"
    lines = []
    for e in events[-max(1, int(limit or 30)) :]:
        et = e.get("type", "?")
        agent = e.get("agent") or "-"
        detail = (
            e.get("text")
            or e.get("reason")
            or e.get("detail")
            or e.get("error")
            or ("; ".join(e.get("items") or []))
            or (
                f"{e.get('status')} stop={e.get('stop_reason') or '-'} turns={e.get('turns', '-')}"
                if et == "run_summary"
                else ""
            )
            or e.get("cmd")
            or e.get("call")
            or ""
        )
        tag = f"{et}/{e['kind']}" if e.get("kind") else et
        lines.append(f"{e.get('ts', '?')}  {tag:20}  {agent:11}  {core.short(detail, 120)}")
    return "\n".join(lines)


def clear_log(project: Path) -> str:
    """Remove plugin-log.jsonl and .plugin-log.cursor (exported digests stay)."""
    qa_out = Path(project).resolve() / "qa-out"
    removed = []
    for name in (LOG_NAME, CURSOR_NAME):
        p = qa_out / name
        if p.exists() or p.is_symlink():
            p.unlink(missing_ok=True)
            removed.append(name)
    return f"cleared {', '.join(removed)}" if removed else "nothing to clear"
