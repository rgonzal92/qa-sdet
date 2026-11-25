"""Unified per-project plugin self-improvement log (<project>/qa-out/plugin-log.jsonl), work-safe anonymizer, and
Markdown digest exporter.

Automatic hooks and qa-mem append scrubbed JSONL events (denials, hook errors, nudges, failed tool calls, stop-hook
bounces, CLI errors, and 1-line run summaries). Users add manual notes via `qa-mem plugin-log note` or
`/qa-sdet:qa-feedback`. Setting `"pluginLog": false` in ~/.config/qa-sdet/config.json disables all writes.
"""

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from . import core, scrub


LOG_NAME = "plugin-log.jsonl"
CURSOR_NAME = ".plugin-log.cursor"
MAX_EVENTS = 2000
TRIM_BYTES = 150_000
READ_CAP = 8 << 20
NOTE_KINDS = ("bug", "nuisance", "idea")
SEVERITIES = ("low", "med", "high")
EVENT_TYPES = ("denial", "hook_error", "nudge", "tool_fail", "stop_bounce", "cli_error", "run_summary", "user_note")
_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_VERSION_CACHE = None

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


def plugin_version():
    """Version from .claude-plugin/plugin.json, cached per process."""
    global _VERSION_CACHE
    if _VERSION_CACHE is None:
        try:
            v = json.loads((_PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")).get("version")
            _VERSION_CACHE = str(v).strip() if v else core.UNKNOWN
        except (OSError, ValueError, AttributeError):
            _VERSION_CACHE = core.UNKNOWN
    return _VERSION_CACHE


def _read_config(config_path=None):
    p = Path(config_path) if config_path else core.CONFIG
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def enabled(config_path=None):
    """On by default; disabled only when config explicitly sets `"pluginLog": false`."""
    return _read_config(config_path).get("pluginLog") is not False


def scrub_text(text, cap_len=400):
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
        fd = os.open(log_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
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
            os.replace(tmp, log_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError:
        pass


def append_event(qa_out, event_type, fields=None, *, create=False, ts=None, config_path=None):
    """Append one scrubbed JSONL event to <qa_out>/plugin-log.jsonl. Best effort: never raises."""
    try:
        if not enabled(config_path):
            return False
        folder = Path(qa_out)
        if create:
            folder.mkdir(parents=True, exist_ok=True)
        elif not folder.is_dir():
            return False
        rec = {"ts": ts or core.now(), "v": plugin_version(), "type": str(event_type)}
        if isinstance(fields, dict):
            rec.update(_scrub_fields(fields))
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
        log = folder / LOG_NAME
        fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
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


def resolve_project(project=None, cwd=None):
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


def add_note(project, text, kind="nuisance", severity=None):
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


# ---------- Standard work-safe anonymizer (applied on export) ----------
_TECH_PREFIXES = {
    "TC",
    "AC",
    "BUG",
    "HTTP",
    "HTTPS",
    "UTF",
    "ASCII",
    "SHA",
    "MD",
    "TLS",
    "SSL",
    "ISO",
    "PEP",
    "RFC",
    "CVE",
    "ECMA",
    "ES",
    "ERR",
    "EXIT",
    "UUID",
    "TICKET",
    "HOST",
    "SRC",
    "CSS",
    "DOM",
    "API",
    "SQL",
    "HTML",
    "JSON",
    "YAML",
    "XML",
    "JWT",
    "SSO",
    "MFA",
    "URL",
}
_URL_RE = re.compile(r"(https?://)([^/\s'\"`)\]>]+)(/[^\s'\"`)\]>]*)?", re.I)
_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,14})-(\d+)\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_HOME_RE = re.compile(r"/(?:home|Users)/[^/\s'\"`)\]>]+")
_ROUTE_SEG_RE = re.compile(r"(?<![A-Za-z0-9_:])(/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.:-]+)+)")
_VALUE_FLAG_RE = re.compile(
    r"(?<!\S)(--(?:jql|focus|hypothesis|known-bugs|json|body|data|data-raw|data-binary|label|header|summary|description|text|message)|-m)"
    r"(\s+|=)(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|(?!-)[^\s;&|]+)"
)
_PW_EVAL_RE = re.compile(
    r"(playwright-cli(?:\s+-s=\S+)?(?:\s+--raw)?\s+eval)\s+(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|[^;&|\n]+)"
)
_PW_DATA_VERB_RE = re.compile(
    r"(playwright-cli(?:\s+-s=\S+)?(?:\s+--raw)?\s+(?:fill|type|select|press|upload|drop)\s+\S+)\s+"
    r"(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|[^;&|\n]+)"
)
_PW_FIND_RE = re.compile(
    r"(playwright-cli(?:\s+-s=\S+)?(?:\s+--raw)?\s+find)\s+(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|[^;&|\n]+)"
)


class Anonymizer:
    """Deterministic per-export work-safe anonymizer: aliases hosts, Jira keys, paths, emails and CLI inputs while
    keeping Playwright verbs, qa-mem commands, normalized route shapes, error types, and readable user notes."""

    def __init__(self, project=None, cfg=None):
        cfg = cfg if isinstance(cfg, dict) else core.config()
        self._hosts = {}
        self._host_n = 0
        self._tickets = {}
        self._ticket_n = 0
        self._sso_host = ""
        self._app = ""
        self._paths = []

        # Configured hosts (stable ordering: ssoHost -> <sso-host>, qaBaseUrl + allowedHosts -> <host-1>..)
        sso = self._bare_host(cfg.get("ssoHost"))
        if sso and sso not in core.LOOPBACK:
            self._sso_host = sso
            self._hosts[sso] = "<sso-host>"
        base_host = self._bare_host(cfg.get("qaBaseUrl"))
        if base_host:
            self._alias_host(base_host)
        for h in cfg.get("allowedHosts") if isinstance(cfg.get("allowedHosts"), list) else ():
            bh = self._bare_host(h)
            if bh:
                self._alias_host(bh)

        app = str(cfg.get("app") or "").strip()
        if len(app) >= 3 and app.lower() != "default":
            self._app = app

        # Longest paths first so nested folders replace before parent folders
        raw_paths = []
        if project:
            for p in {str(project), str(Path(project).resolve())}:
                if p and p != "/":
                    raw_paths.append((p.rstrip("/"), "<project>"))
        raw_paths.append((str(_PLUGIN_ROOT).rstrip("/"), "<plugin>"))
        for idx, sp in enumerate(cfg.get("sourcePaths") if isinstance(cfg.get("sourcePaths"), list) else (), 1):
            s = str(sp or "").strip().rstrip("/")
            if s:
                raw_paths.append((s, f"<src-{idx}>"))
                try:
                    rp = str(Path(s).expanduser().resolve()).rstrip("/")
                    if rp != s:
                        raw_paths.append((rp, f"<src-{idx}>"))
                except OSError:
                    pass
        home = str(Path.home()).rstrip("/")
        if home and home != "/":
            raw_paths.append((home, "<home>"))
        self._paths = sorted(raw_paths, key=lambda kv: len(kv[0]), reverse=True)

    @staticmethod
    def _bare_host(raw):
        s = str(raw or "").strip().lower()
        if not s:
            return ""
        try:
            u = urlsplit(s if "://" in s else "http://" + s)
            return (u.hostname or "").rstrip(".").removeprefix("*.")
        except ValueError:
            return ""

    def _alias_host(self, host):
        h = host.lower().rstrip(".")
        if not h or h in core.LOOPBACK:
            return h
        if h not in self._hosts:
            self._host_n += 1
            self._hosts[h] = f"<host-{self._host_n}>"
        return self._hosts[h]

    def _alias_ticket(self, match):
        prefix, full = match.group(1), match.group(0)
        if prefix in _TECH_PREFIXES:
            return full
        if full not in self._tickets:
            self._ticket_n += 1
            self._tickets[full] = f"TICKET-{self._ticket_n}"
        return self._tickets[full]

    def _replace_url(self, match):
        scheme, netloc, tail = match.group(1), match.group(2), match.group(3) or ""
        host_part = netloc.rpartition("@")[2]
        host, colon, port = host_part.partition(":")
        alias = self._alias_host(host)
        net = f"{alias}{colon}{port}" if alias in core.LOOPBACK else alias
        if not tail:
            return f"{scheme}{net}"
        path_part, q_sep, rest = tail.partition("?")
        frag_part = ""
        if "#" in path_part:
            path_part, _, frag_part = path_part.partition("#")
        norm_p = core._ids(path_part) or "/"
        if frag_part.startswith(("/", "!/")):
            bang = "!" if frag_part.startswith("!/") else ""
            norm_p += f"#{bang}{core._ids(frag_part[len(bang) :].split('?')[0])}"
        return f"{scheme}{net}{norm_p}" + ("?<query>" if q_sep else "")

    def route(self, r):
        if not r:
            return ""
        return self.text(core.norm_route(r))

    def text(self, s):
        """Work-safe anonymization of prose, errors, run names, and user notes."""
        out = scrub_text(s, cap_len=0)
        if not out:
            return ""
        out = _URL_RE.sub(self._replace_url, out)
        out = _EMAIL_RE.sub("<email>", out)
        for raw_p, label in self._paths:
            if raw_p in out:
                out = out.replace(raw_p, label)
        out = _HOME_RE.sub("<home>", out)
        # Known bare hosts (longest first so api.acme.internal replaces before acme.internal)
        for host, alias in sorted(self._hosts.items(), key=lambda kv: len(kv[0]), reverse=True):
            out = re.sub(rf"(?i)(?<![A-Za-z0-9_.-]){re.escape(host)}(?![A-Za-z0-9_.-])", alias, out)
        out = _JIRA_KEY_RE.sub(self._alias_ticket, out)
        if self._app:
            out = re.sub(rf"(?i)\b{re.escape(self._app)}\b", "<app>", out)
        # Normalize numeric/UUID segments in route-like paths that weren't full URLs
        out = _ROUTE_SEG_RE.sub(
            lambda m: core._ids(m.group(1)) if not m.group(1).startswith("/usr/") else m.group(1), out
        )
        return out

    def command(self, cmd):
        """Anonymize a Bash/CLI command: keep tool, verb, ref and flag names; redact typed values, JS evals and JQL."""
        out = scrub_text(cmd, cap_len=0)
        if not out:
            return ""
        out = _PW_EVAL_RE.sub(r"\1 '<redacted>'", out)
        out = _PW_DATA_VERB_RE.sub(r"\1 '<redacted>'", out)
        out = _PW_FIND_RE.sub(r"\1 '<redacted>'", out)
        out = _VALUE_FLAG_RE.sub(r"\1\2'<redacted>'", out)
        return self.text(out)


# ---------- Reading, cursor, digest export, show & clear ----------
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
                h = hashlib.sha1(line.encode("utf-8", "replace")).hexdigest()[:12]
                items.append((h, doc))
            else:
                bad += 1
        except ValueError:
            bad += 1
    return items, bad


def _parse_since(val):
    """Parse ISO date/timestamp or relative duration (7d, 24h, 30m) into UTC ISO string."""
    s = str(val or "").strip()
    m = re.fullmatch(r"(\d+)\s*([dhm])", s, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = timedelta(days=n) if unit == "d" else timedelta(hours=n) if unit == "h" else timedelta(minutes=n)
        return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s + "T00:00:00Z"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z?", s):
        return s if s.endswith("Z") else s + ("Z" if s.count(":") == 2 else ":00Z")
    raise core.BadInput(
        f"plugin-log export: invalid --since {val!r} (use YYYY-MM-DD, ISO timestamp, or 7d / 24h / 30m)"
    )


def _read_cursor(qa_out):
    try:
        doc = json.loads((Path(qa_out) / CURSOR_NAME).read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cursor(qa_out, line_hash, ts):
    core._write(Path(qa_out) / CURSOR_NAME, json.dumps({"ts": ts, "line_hash": line_hash}) + "\n")


def _filter_window(items, qa_out, all_events=False, since=None):
    if all_events:
        return items
    if since:
        cutoff = _parse_since(since)
        return [(h, d) for h, d in items if str(d.get("ts") or "") >= cutoff]
    cur = _read_cursor(qa_out)
    if not cur:
        return items
    ch, cts = cur.get("line_hash"), str(cur.get("ts") or "")
    if ch:
        for i in range(len(items) - 1, -1, -1):
            if items[i][0] == ch:
                return items[i + 1 :]
    return [(h, d) for h, d in items if str(d.get("ts") or "") > cts]


def _next_digest_path(qa_out, day):
    for n in range(1, 1000):
        p = Path(qa_out) / (f"plugin-digest-{day}.md" if n == 1 else f"plugin-digest-{day}-{n}.md")
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            os.close(fd)
            return p
        except FileExistsError:
            continue
    raise core.BadInput(f"too many digest files for {day}")


def _group_counts(rows, key_fn, sample_fn):
    counts, samples = Counter(), {}
    for ev in rows:
        k = key_fn(ev)
        counts[k] += 1
        samples.setdefault(k, sample_fn(ev))
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])), samples


def render_digest(events, anon, bad_lines=0):
    """Render anonymized Markdown digest from a non-empty list of event dicts."""
    versions = sorted({str(e.get("v") or core.UNKNOWN) for e in events})
    tss = [str(e.get("ts") or "") for e in events if e.get("ts")]
    by_type = defaultdict(list)
    for e in events:
        by_type[str(e.get("type"))].append(e)

    runs = by_type["run_summary"]
    notes = by_type["user_note"]
    lines = [
        "# qa-sdet Plugin Digest",
        "",
        f"- **Plugin version(s):** {', '.join(versions)}",
        f"- **Window:** {min(tss) if tss else '?'} to {max(tss) if tss else '?'} ({len(events)} events, {len(runs)} run summaries, {len(notes)} user notes)",
        "- **Anonymized:** hosts, Jira keys, local paths, emails, and CLI input values redacted",
    ]
    if bad_lines:
        lines.append(f"- **Malformed log lines skipped:** {bad_lines}")

    # 1. User Notes first (highest human signal)
    kind_rank = {"bug": 0, "nuisance": 1, "idea": 2}
    sev_rank = {"high": 0, "med": 1, "low": 2, "": 3}
    lines += ["", f"## User Notes ({len(notes)})"]
    if not notes:
        lines.append("- None.")
    else:
        ordered = sorted(
            notes,
            key=lambda n: (
                kind_rank.get(n.get("kind"), 9),
                sev_rank.get(n.get("severity") or "", 9),
                n.get("ts") or "",
            ),
        )
        for n in ordered:
            tag = f"{n.get('kind', 'nuisance')}" + (f" / {n['severity']}" if n.get("severity") else "")
            ctx_parts = [anon.text(n["run"]) if n.get("run") else "", anon.route(n["route"]) if n.get("route") else ""]
            ctx = f" ({', '.join(p for p in ctx_parts if p)})" if any(ctx_parts) else ""
            lines.append(f"- **[{tag}]** `{n.get('ts', '?')}`{ctx}: {anon.text(n.get('text', ''))}")

    # 2. Recurring Friction & Failures
    lines += ["", "## Recurring Friction & Failures"]
    sections = []

    if by_type["hook_error"]:
        ranked, samples = _group_counts(
            by_type["hook_error"],
            lambda e: f"{e.get('hook', '?')}: {e.get('error_type', '?')}",
            lambda e: anon.text(e.get("detail", ""))[:160],
        )
        sec = [f"### Hook & Ingest Errors ({len(by_type['hook_error'])})"]
        for k, cnt in ranked[:15]:
            sec.append(f"- `{k}` (×{cnt})" + (f" — {samples[k]}" if samples[k] else ""))
        sections.append("\n".join(sec))

    if by_type["denial"]:
        ranked, samples = _group_counts(
            by_type["denial"],
            lambda e: f"[{e.get('layer', 'guard')}/{e.get('agent', 'main')}] {anon.text(e.get('reason', ''))[:140]}",
            lambda e: anon.command(e.get("call", ""))[:160],
        )
        sec = [f"### Guard & Auto-Mode Denials ({len(by_type['denial'])})"]
        for k, cnt in ranked[:15]:
            sec.append(f"- {k} (×{cnt})" + (f" — e.g. `{samples[k]}`" if samples[k] else ""))
        sections.append("\n".join(sec))

    if by_type["stop_bounce"]:
        ranked, samples = _group_counts(
            by_type["stop_bounce"],
            lambda e: (
                f"[{e.get('agent', '?')}] {e.get('kind', 'bounce')}: {anon.text('; '.join(e.get('items') or [])[:140])}"
            ),
            lambda e: anon.text(e.get("run", "")),
        )
        sec = [f"### Stop-Hook Bounces & Parked Files ({len(by_type['stop_bounce'])})"]
        for k, cnt in ranked[:15]:
            sec.append(f"- {k} (×{cnt})" + (f" (run `{samples[k]}`)" if samples[k] else ""))
        sections.append("\n".join(sec))

    if by_type["cli_error"]:
        ranked, samples = _group_counts(
            by_type["cli_error"],
            lambda e: f"`qa-mem {e.get('cmd', '?')}` exit {e.get('exit_code', '?')}",
            lambda e: anon.text(e.get("reason", ""))[:160],
        )
        sec = [f"### CLI Errors (`qa-mem`) ({len(by_type['cli_error'])})"]
        for k, cnt in ranked[:15]:
            sec.append(f"- {k} (×{cnt})" + (f" — {samples[k]}" if samples[k] else ""))
        sections.append("\n".join(sec))

    if by_type["nudge"]:
        ranked, samples = _group_counts(
            by_type["nudge"],
            lambda e: (
                f"[{e.get('agent', '?')}] `{e.get('kind', '?')}`" + (f" ({e['trigger']})" if e.get("trigger") else "")
            ),
            lambda e: anon.text(e.get("detail", ""))[:160],
        )
        sec = [f"### Agent Nudges ({len(by_type['nudge'])})"]
        for k, cnt in ranked[:15]:
            sec.append(f"- {k} (×{cnt})" + (f" — {samples[k]}" if samples[k] else ""))
        sections.append("\n".join(sec))

    if by_type["tool_fail"]:
        ranked, samples = _group_counts(
            by_type["tool_fail"],
            lambda e: f"[{e.get('agent', 'main')}] `{anon.command(e.get('cmd', ''))[:100]}`",
            lambda e: anon.text(e.get("error", ""))[:160],
        )
        sec = [f"### Failed Tool Calls ({len(by_type['tool_fail'])})"]
        for k, cnt in ranked[:15]:
            sec.append(f"- {k} (×{cnt})" + (f" — {samples[k]}" if samples[k] else ""))
        sections.append("\n".join(sec))

    lines.append("\n\n".join(sections) if sections else "- No friction or failure events in this window.")

    # 3. Run Health Summary
    lines += ["", f"## Run Health Summary ({len(runs)} runs)"]
    if not runs:
        lines.append("- No completed run summaries in this window.")
    else:
        statuses = Counter(str(r.get("status") or "UNKNOWN") for r in runs)
        stops = Counter(str(r.get("stop_reason") or "none") for r in runs)
        bscopes = Counter(str(r.get("block_scope")) for r in runs if r.get("block_scope"))
        known_builds = sum(1 for r in runs if r.get("build_known") is True)
        missing_files = sum(1 for r in runs if r.get("missing_run_file"))
        audit_runs = sum(1 for r in runs if int(r.get("audit_issues") or 0) > 0)
        turns_vals = [int(r["turns"]) for r in runs if isinstance(r.get("turns"), int)]
        dur_vals = [int(r["duration_s"]) for r in runs if isinstance(r.get("duration_s"), int)]
        total_bugs = sum(int(r.get("bugs_found") or 0) for r in runs)
        lines.append(
            "- **Outcomes:** "
            + ", ".join(f"{k}: {v}" for k, v in statuses.most_common())
            + (f" (`block_scope`: {', '.join(f'{k}={v}' for k, v in bscopes.most_common())})" if bscopes else "")
        )
        lines.append("- **Stop reasons:** " + ", ".join(f"`{k}`: {v}" for k, v in stops.most_common()))
        lines.append(f"- **Build detection:** {known_builds}/{len(runs)} known ({len(runs) - known_builds} `unknown`)")
        avg_t = f"{sum(turns_vals) / len(turns_vals):.1f}" if turns_vals else "n/a"
        avg_d = f"{sum(dur_vals) / len(dur_vals):.0f}s" if dur_vals else "n/a"
        lines.append(
            f"- **Stats:** avg turns {avg_t}, avg duration {avg_d}, bugs found {total_bugs}, "
            f"runs with `audit-bugs` issues {audit_runs}, missing run files {missing_files}"
        )
        notable = [
            r
            for r in runs
            if r.get("status") != "COMPLETED"
            or r.get("missing_run_file")
            or int(r.get("turns") or 0) >= 35
            or int(r.get("audit_issues") or 0) > 0
        ]
        if notable:
            lines += [
                "",
                "| Run | Route | Status | Stop | Turns | Dur | Bugs | Audit |",
                "|---|---|---|---|---:|---:|---:|---:|",
            ]
            for r in notable[:15]:
                lines.append(
                    f"| `{anon.text(r.get('run') or '?')}` | `{anon.route(r.get('route') or '') or '?'}` "
                    f"| {r.get('status', '?')} | `{r.get('stop_reason') or '-'}` "
                    f"| {r.get('turns', '-')} | {r.get('duration_s', '-')}s "
                    f"| {r.get('bugs_found', 0)} | {r.get('audit_issues', 0)} |"
                )

    md = "\n".join(lines) + "\n"
    return scrub.redact_json_text(scrub.redact(md))


def export_digest(project, *, all_events=False, since=None, preview=False, stdout=False, day=None):
    """Export anonymized Markdown digest. Returns {events, path, markdown, summary}."""
    proj = Path(project).resolve()
    qa_out = proj / "qa-out"
    items, bad = _read_events(qa_out)
    selected = _filter_window(items, qa_out, all_events=all_events, since=since)
    if not selected:
        cur = _read_cursor(qa_out)
        why = (
            f"since {since}"
            if since
            else (f"since last export ({cur['ts']})" if cur.get("ts") and not all_events else "")
        )
        return {
            "events": 0,
            "path": None,
            "markdown": "",
            "summary": f"no new plugin-log events{(' ' + why) if why else ''}",
        }
    events = [d for _, d in selected]
    anon = Anonymizer(proj, core.config())
    md = render_digest(events, anon, bad_lines=bad)
    path = None
    if not preview and not stdout:
        qa_out.mkdir(exist_ok=True)
        path = _next_digest_path(qa_out, day or datetime.now().strftime("%Y-%m-%d"))
        core._write(path, md)
    if not preview:
        last_hash, last_ev = selected[-1]
        _write_cursor(qa_out, last_hash, str(last_ev.get("ts") or core.now()))
    counts = Counter(str(e.get("type")) for e in events)
    breakdown = ", ".join(f"{k}={v}" for k, v in counts.most_common())
    summary = f"exported {len(events)} event(s) ({breakdown})" + (f" -> {path}" if path else "")
    return {"events": len(events), "path": path, "markdown": md, "summary": summary}


def show_log(project, *, limit=30, event_type=None):
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


def clear_log(project):
    """Remove plugin-log.jsonl and .plugin-log.cursor (exported digests stay)."""
    qa_out = Path(project).resolve() / "qa-out"
    removed = []
    for name in (LOG_NAME, CURSOR_NAME):
        p = qa_out / name
        if p.exists() or p.is_symlink():
            p.unlink(missing_ok=True)
            removed.append(name)
    return f"cleared {', '.join(removed)}" if removed else "nothing to clear"
