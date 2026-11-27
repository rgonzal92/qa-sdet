"""The guard's records: masked denials and errors in the plugin log, and an explorer's request line in the call
trace."""

import functools
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path

import guard_paths

_MASKS = [
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(\S+\s+)?[^\s'\"]+"), r"\1[masked]"),
    (re.compile(r"(?i)\b((?:set-)?cookie|x-api-key|x-auth-token)(\s*:\s*)[^'\"\n]+"), r"\1\2[masked]"),  # header values
    (re.compile(r"(?i)\b(bearer|basic)\s+[\w.~+/=-]+"), r"\1 [masked]"),
    # anywhere in a name (JIRA_API_TOKEN=, accessToken:), also as a JSON key ("password": "…", escaped in a dumped string)
    (
        re.compile(r"(?i)(token|password|passwd|pwd|secret|api[_-]?key)(\\?['\"]?\s*[=:]\s*\\?['\"]?)[^\s'\"&\\]+"),
        r"\1\2[masked]",
    ),
    (
        re.compile(r"(?i)(\s--?[\w-]*(?:token|password|passwd|secret|api[_-]?key)\s+['\"]?)[^\s'\"]+"),
        r"\1[masked]",
    ),  # --password x
    (re.compile(r"(\s(?:-u|--user)\s*['\"]?[^\s:'\"]+:)[^\s'\"]+"), r"\1[masked]"),  # curl -u user:password
    (re.compile(r"://[^\s/?#:@'\"]+:[^\s/?#@'\"]+@"), "://[masked]@"),
]
DENIAL_LOG_LINES = 2000
PLUGIN_LOG_NAME = "plugin-log.jsonl"


def mask(text):
    for rx, rep in _MASKS:
        text = rx.sub(rep, text)
    return text


@functools.cache
def _plugin_version():
    try:
        v = json.loads(
            (Path(guard_paths.PLUGIN_QA_MEM).parent.parent / ".claude-plugin" / "plugin.json").read_text()
        ).get("version")
        return str(v).strip() if v else "unknown"
    except (OSError, ValueError, AttributeError):
        return "unknown"


def _plugin_log_enabled():
    try:
        cfg = json.loads(guard_paths.CONFIG.read_text())
        return not (isinstance(cfg, dict) and cfg.get("pluginLog") is False)
    except (OSError, ValueError):
        return True


def log_event(data, event_type, fields):
    """Append one masked JSONL record to <project>/qa-out/plugin-log.jsonl (newest DENIAL_LOG_LINES kept). Best effort."""
    try:
        if not _plugin_log_enabled():
            return
        project = os.environ.get("CLAUDE_PROJECT_DIR")  # Claude Code names the project: its qa-out is made if missing
        folder = Path(project or str(data.get("cwd") or os.getcwd()), "qa-out")
        if project:
            folder.mkdir(exist_ok=True)
        elif not folder.is_dir():
            return  # no project named: never a new qa-out in whatever folder this ran from (/tmp, a test run)
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "v": _plugin_version(),
            "type": event_type,
            **fields,
        }
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
        log = folder / PLUGIN_LOG_NAME
        fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        size = 0
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return
            f.write(line)
            f.flush()
            size = os.fstat(f.fileno()).st_size
        if size > 150_000:
            rfd = os.open(log, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(rfd, "rb") as rf:
                if not stat.S_ISREG(os.fstat(rf.fileno()).st_mode):
                    return
                lines = rf.read(8 << 20).decode("utf-8", "replace").splitlines(keepends=True)
            if len(lines) > DENIAL_LOG_LINES:
                tfd, tmp = tempfile.mkstemp(dir=folder, prefix=".plugin-log.", suffix=".tmp")
                try:
                    with os.fdopen(tfd, "w", encoding="utf-8", errors="replace") as tf:
                        tf.writelines(lines[-DENIAL_LOG_LINES:])
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, log)
                except BaseException:
                    Path(tmp).unlink(missing_ok=True)
                    raise
    except Exception:  # noqa: BLE001, S110 - logging is best effort
        pass


def log_denial(data, reason, layer="guard"):
    """One JSONL denial record in <project>/qa-out/plugin-log.jsonl, secrets masked, newest DENIAL_LOG_LINES kept."""
    try:
        tin = data.get("tool_input")
        what = (
            str(tin.get("command"))
            if data.get("tool_name") == "Bash" and isinstance(tin, dict) and "command" in tin
            else f"{data.get('tool_name')} {json.dumps(tin, ensure_ascii=False, default=str)[:600]}"
        )
        log_event(
            data,
            "denial",
            {
                "layer": layer,
                "agent": guard_paths.agent_role(data) or "main",
                "tool": guard_paths.clean(data.get("tool_name") or ""),
                "reason": " ".join(mask(str(reason)).split())[:400],
                "call": " ".join(mask(what).split())[:300],
            },
        )
    except Exception:  # noqa: BLE001, S110 - logging is best effort
        pass


def blocker_word(what):
    """A word or shape the model typed, safe to show and to count: never more than a word."""
    return re.sub(r"[^\w .()$&-]", "?", str(what))[:24]


def call_key(cmd):
    """The 12-hex key of a Bash command (posttooluse.classify puts the same one on the result line): never the text,
    which can hold a typed value."""
    return hashlib.sha1(" ".join(str(cmd).split()).encode("utf-8", "replace"), usedforsecurity=False).hexdigest()[:12]


def trace_request(data, reason, approve, blocker):
    """One request line in qa-out/.active.progress for an explorer's Bash call in a live run: when the guard saw it and
    what it decided (a approved, d denied, c left to Claude Code's own check). PostToolUse adds the result line; the
    time between the two is what a call waited. A rule or a word and a hash only: no command, host, path or page text.
    Best effort: a trace that can't be written never changes the decision."""
    try:
        if (
            guard_paths.clean(data.get("tool_name") or "") != "Bash"
            or guard_paths.agent_role(data) not in guard_paths.EXPLORERS
        ):
            return
        path, m = guard_paths.read_marker(guard_paths.cwd_of(data))
        if not path:
            return
        tin = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
        rec = {
            "e": m.get("expires"),
            "t": "q",
            "ts": round(time.time(), 3),
            "d": "d" if reason else "a" if approve else "c",
        }
        if reason:
            rec["w"] = str(reason).split(":", 1)[0][:40]
        elif not approve and blocker:
            rec["w"] = blocker_word(blocker)
        rec["k"] = call_key(tin.get("command") or "")
        if data.get("tool_use_id"):
            rec["id"] = guard_paths.clean(data["tool_use_id"])[:64]
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(path.with_name(".active.progress"), flags, 0o600), "w") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception:  # noqa: BLE001, S110 - the trace is best effort
        pass
