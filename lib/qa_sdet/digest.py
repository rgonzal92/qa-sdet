"""`qa-mem plugin-log export`: the plugin log as a Markdown digest, with the work-safe anonymizer applied to it."""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import core, plugin_log, scrub

TYPE_CHECKING = False  # mypy reads it as True (pyproject always_true): typing stays out of the import path
if TYPE_CHECKING:  # NOSONAR
    from typing import Any


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
        self._app = ""
        self._paths = []

        # Configured hosts (stable ordering: ssoHost -> <sso-host>, qaBaseUrl + allowedHosts -> <host-1>..)
        sso = self._bare_host(cfg.get("ssoHost"))
        if sso and sso not in core.LOOPBACK:
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
        raw_paths.append((str(plugin_log.PLUGIN_ROOT).rstrip("/"), "<plugin>"))
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
            # not a vulnerability (S5332): a scheme added only to parse a bare host; nothing is fetched
            u = urlsplit(s if "://" in s else "http://" + s)  # NOSONAR
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
        path_part, q_sep, _query = tail.partition("?")
        frag_part = ""
        if "#" in path_part:
            path_part, _, frag_part = path_part.partition("#")
        norm_p = core.mask_ids(path_part) or "/"
        if frag_part.startswith(("/", "!/")):
            bang = "!" if frag_part.startswith("!/") else ""
            norm_p += f"#{bang}{core.mask_ids(frag_part[len(bang) :].split('?')[0])}"
        return f"{scheme}{net}{norm_p}" + ("?<query>" if q_sep else "")

    def route(self, r):
        if not r:
            return ""
        return self.text(core.norm_route(r))

    def text(self, s):
        """Work-safe anonymization of prose, errors, run names, and user notes."""
        out = plugin_log.scrub_text(s, cap_len=0)
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
            lambda m: core.mask_ids(m.group(1)) if not m.group(1).startswith("/usr/") else m.group(1), out
        )
        return out

    def command(self, cmd):
        """Anonymize a Bash/CLI command: keep tool, verb, ref and flag names; redact typed values, JS evals and JQL."""
        out = plugin_log.scrub_text(cmd, cap_len=0)
        if not out:
            return ""
        out = _PW_EVAL_RE.sub(r"\1 '<redacted>'", out)
        out = _PW_DATA_VERB_RE.sub(r"\1 '<redacted>'", out)
        out = _PW_FIND_RE.sub(r"\1 '<redacted>'", out)
        out = _VALUE_FLAG_RE.sub(r"\1\2'<redacted>'", out)
        return self.text(out)


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


def _filter_window(items, qa_out, all_events=False, since=None):
    if all_events:
        return items
    if since:
        cutoff = _parse_since(since)
        return [(h, d) for h, d in items if str(d.get("ts") or "") >= cutoff]
    cur = plugin_log.read_cursor(qa_out)
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


def _friction_sections(by_type, anon):
    """Recurring Friction & Failures: one section per kind of automatic event that has entries."""
    lines = []
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

    if by_type["notice"]:
        ranked, samples = _group_counts(
            by_type["notice"],
            lambda e: f"`{e.get('kind', '?')}`",
            lambda e: anon.text(e.get("detail") or " ".join(str(e[k]) for k in ("hash", "route") if e.get(k)) or "")[
                :160
            ],
        )
        sec = [f"### Notices ({len(by_type['notice'])})"]
        for k, cnt in ranked[:15]:
            sec.append(f"- {k} (×{cnt})" + (f" — {samples[k]}" if samples[k] else ""))
        sections.append("\n".join(sec))

    lines.append("\n\n".join(sections) if sections else "- No friction or failure events in this window.")
    return lines


def _run_health(runs, anon):
    """Run Health Summary: the runs in a table, then what slowed them."""
    lines = []
    # 3. Run Health Summary
    lines += ["", f"## Run Health Summary ({len(runs)} runs)"]
    if not runs:
        lines.append("- No completed run summaries in this window.")
    else:
        statuses = Counter(str(r.get("status") or "UNKNOWN") for r in runs)
        stops = Counter(str(r.get("stop_reason") or "none") for r in runs)
        bscopes = Counter(str(r.get("block_scope")) for r in runs if r.get("block_scope"))
        build_runs = [r for r in runs if isinstance(r.get("build_known"), bool)]  # code maps read no build
        known_builds = sum(1 for r in build_runs if r["build_known"])
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
        lines.append(
            f"- **Build detection:** {known_builds}/{len(build_runs)} known ({len(build_runs) - known_builds} `unknown`)"
            if build_runs
            else "- **Build detection:** n/a (code-map runs only)"
        )
        avg_t = f"{sum(turns_vals) / len(turns_vals):.1f}" if turns_vals else "n/a"
        avg_d = f"{sum(dur_vals) / len(dur_vals):.0f}s" if dur_vals else "n/a"
        lines.append(
            f"- **Stats:** avg turns {avg_t}, avg duration {avg_d}, bugs found {total_bugs}, "
            f"runs with `audit-bugs` issues {audit_runs}, missing run files {missing_files}"
        )
        traced = [r for r in runs if isinstance(r.get("calls"), int)]  # the hooks' call trace (ops._trace_totals)
        if traced:
            tot = lambda k: sum(r.get(k) or 0 for r in traced if isinstance(r.get(k), (int, float)))
            by = lambda k: sum((Counter(r[k]) for r in traced if isinstance(r.get(k), dict)), Counter())
            words = lambda c: ", ".join(f"`{anon.text(w)[:40]}` {n}" for w, n in c.most_common(10))
            lines.append(
                f"- **Calls:** {tot('calls')} in {len(traced)} runs: {tot('approved')} approved "
                f"({tot('approved_s'):.0f}s), {tot('unapproved')} unapproved ({tot('unapproved_s'):.0f}s), "
                f"{tot('denied')} denied; model {tot('model_s'):.0f}s; after the cases {tot('after_cases_s'):.0f}s"
            )
            if blockers := by("blockers"):
                lines.append(f"- **Unapproved by:** {words(blockers)}")
            if denials := by("denials"):
                lines.append(f"- **Denied by:** {words(denials)}")
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
    return lines


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

    lines += _friction_sections(by_type, anon)

    lines += _run_health(runs, anon)

    md = "\n".join(lines) + "\n"
    return scrub.redact_json_text(scrub.redact(md))


def export_digest(
    project: Path,
    *,
    all_events: bool = False,
    since: str | None = None,
    preview: bool = False,
    stdout: bool = False,
    day: str | None = None,
) -> dict[str, Any]:
    """Export anonymized Markdown digest. Returns {events, path, markdown, summary}."""
    proj = Path(project).resolve()
    qa_out = proj / "qa-out"
    items, bad = plugin_log.read_events(qa_out)
    selected = _filter_window(items, qa_out, all_events=all_events, since=since)
    if not selected:
        cur = plugin_log.read_cursor(qa_out)
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
        path = _next_digest_path(qa_out, day or datetime.now().strftime("%Y-%m-%d"))  # noqa: DTZ005 - the user's local date (stored times use core.now(), UTC)
        core.write(path, md)
    if not preview:
        last_hash, last_ev = selected[-1]
        plugin_log.write_cursor(qa_out, last_hash, str(last_ev.get("ts") or core.now()))
    counts = Counter(str(e.get("type")) for e in events)
    breakdown = ", ".join(f"{k}={v}" for k, v in counts.most_common())
    summary = f"exported {len(events)} event(s) ({breakdown})" + (f" -> {path}" if path else "")
    return {"events": len(events), "path": path, "markdown": md, "summary": summary}
