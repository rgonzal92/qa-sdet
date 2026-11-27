"""Browser signals: the request, console and response-body logs an explorer saves at its end (playwright-cli
`requests --all` / `console error --all`), triaged against the bugs and notes of its run."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import core, probe, results, scrub

TYPE_CHECKING = False  # mypy reads it as True (pyproject always_true): typing stays out of the import path
if TYPE_CHECKING:  # NOSONAR
    from typing import Any


REQUEST_LINE = re.compile(
    r"\s*(\d+)\.\s+\[([A-Z]+)\]\s+(\S+)\s+=>\s+\[(\d{3}|FAILED)\]\s*(.*)"
)  # 3. [GET] url => [404] text
SIGNALS_SHOWN = 15


def _names_request(named, method, path):
    """Whether lower-cased text names a call by method and path: 'POST /api/zones' or 'POST http://host/api/zones'.
    A path alone (the run's own target, a passing note's 'GET /api/zones') explains no other call failing there.
    A draft naming the record placeholder ('GET /api/zones/:id') names the call on a record ('/api/zones/42')."""
    paths = {path.lower(), core.mask_ids(path).lower() or path.lower()}
    return any(
        re.search(rf"\b{re.escape(method.lower())}\s+(?:https?://[^/\s]+)?{re.escape(p)}(?![\w/.-])", named)
        for p in paths
    )


STORM_CALLS = 5  # the same call this often in one page visit: a fetch loop, likely
SUSPECT_200_SHOWN = 3  # calls triage names for a response-body check: each one costs a browser command
# `qa-mem body-check --all`: the bodies one Bash call reads, well inside its two-minute limit
SWEEP_CALLS, SWEEP_SECONDS, SWEEP_CALL_SECONDS = 30, 60, 15
FAILED_COUNTS = ("failed", "failedCount", "failureCount", "failures", "errorCount")
GQL_NAME = re.compile(r"[A-Za-z_]\w{0,59}", re.ASCII)  # a GraphQL operation name: it goes into a signal's key
RPC_PATH = re.compile(r"graphql|/gql\b|rpc", re.I)
API_PATH = re.compile(r"/api(?:/|$)|graphql|/gql\b|rpc|\.json$|/v\d+/", re.I)  # where a 200 HTML page is wrong
_NOT_A_URL = (
    r"(?<![:/\w])"  # a browser stack frame is the app's own JS (http://host/main.js:10); a server frame has a file path
)
LEAKS = (  # what a server must never show a client: (label, pattern)
    (
        "stack trace",
        re.compile(
            r"Traceback \(most recent call last\)|\bNullPointerException\b|\bNullReferenceException\b|\bException in thread\b"
            r"|\bat\s+[^\n(]*\(?" + _NOT_A_URL + r"(?:/|[A-Za-z]:\\)[^\s:)]+\.(?:js|ts|java|cs|py|rb|php|go):\d+"
            r"|\bat \S+ in \S+\.cs:line \d+"
        ),
    ),
    (
        "SQL error",
        re.compile(
            r"\bSQLSTATE\b|\bORA-\d{4,5}\b|syntax error at or near|\bPG::\w+|SequelizeDatabaseError|error in your SQL syntax"
            r"|\bsqlite3?\.OperationalError|\bpsycopg2\.\w+|(?:column|relation) \S+ does not exist",
            re.I,
        ),
    ),
    (
        "framework debug page",
        re.compile(
            r"Whitelabel Error Page|you have DEBUG = True|Server Error in '[^']*' Application|Werkzeug Debugger"
            r"|Whoops, looks like something went wrong|ActionController::RoutingError|Django Version:",
            re.I,
        ),
    ),
)


PROFILE_SEGMENTS = {
    "me",
    "profile",
    "userinfo",
    "whoami",
    "currentuser",
}  # the explorer never reads these bodies either


def _credential_call(path):
    """Whether a path is a login, session, token or user-profile call, whose body carries credentials."""
    return bool(probe.auth_segment(path)) or any(
        re.sub(r"[-_]", "", x.lower()) in PROFILE_SEGMENTS for x in path.split("/")
    )


def _err_text(e):
    """The message inside an error value (`"x"`, `[{"message": "x"}]`, `{"detail": "x"}`), at most 80 characters."""
    if isinstance(e, list):
        e = e[0] if e else ""
    if isinstance(e, dict):
        e = next(
            (e[k] for k in ("message", "detail", "title", "error", "code") if e.get(k)),
            json.dumps(e, ensure_ascii=False),
        )
    return "" if isinstance(e, bool) else core.short(scrub.redact(" ".join(str(e).split())), 80)


def _partial_failure(v, where=""):
    """The label of a batch write that says some of its items failed (`failed: 1`, an item with `ok` false), or None."""
    for k in FAILED_COUNTS:
        if type(v.get(k)) is int and v[k] > 0:  # a whole number: `failed: true` and a list are something else
            return f"partial failure: {where}{k} {v[k]}"
    for k in ("results", "items"):
        rows = v[k] if isinstance(v.get(k), list) else []
        bad = [x for x in rows if isinstance(x, dict) and (x.get("ok") is False or x.get("success") is False)]
        if bad:
            m = _err_text(next((bad[0][f] for f in ("reason", "message", "error", "detail") if bad[0].get(f)), ""))
            return f"partial failure: {where}{k}: {len(bad)} of {len(rows)} failed" + (f": {m}" if m else "")
    return None


def body_verdict(text, path="", status=200, write=False):
    """What is wrong with one response body, as short labels ([] when nothing): a leak signature (stack trace, SQL error,
    framework debug page) in any status; on a 2xx also a JSON error envelope (`error`, `errors`, GraphQL `errors`,
    `success`/`ok` false, `status` "error"), a GraphQL mutation's own failure (`data.<field>.userErrors`, or
    `data.<field>.errors` beside nothing else: a record with an `errors` column is data) and, on an API-looking path, an
    HTML page (a login redirect, a proxy page). write: the call changes data, so a batch result with failed items is a
    partial failure (in a read, `failed: 3` is a count the page shows). A 4xx or 5xx that says so in JSON is the right
    answer, so only its leaks count."""
    text = str(text or "")[:200_000]
    out = []
    if 200 <= status < 300:
        try:
            v = json.loads(text)
        except (ValueError, RecursionError):
            v = None
        if isinstance(v, dict):
            for k in ("errors", "error"):
                if v.get(k):
                    out.append(f"error body: {k}" + (f": {m}" if (m := _err_text(v[k])) else ""))
                    break
            else:
                flag = next((k for k in ("success", "ok") if v.get(k) is False), None)
                if flag or str(v.get("status")).lower() in ("error", "fail", "failed", "failure"):
                    m = _err_text(
                        next((v[k] for k in ("message", "detail", "error_description", "reason") if v.get(k)), "")
                    )
                    out.append(
                        f"error body: {flag + ' false' if flag else 'status ' + str(v['status']).lower()}"
                        + (f": {m}" if m else "")
                    )
            data = v["data"] if isinstance(v.get("data"), dict) else {}
            fields = [(f"data.{k}.", f) for k, f in list(data.items())[:20] if isinstance(f, dict)]
            for where, f in fields:
                alone = not any(x for k, x in f.items() if k not in ("errors", "__typename"))
                key = "userErrors" if f.get("userErrors") else "errors" if f.get("errors") and alone else None
                if key:
                    out.append(f"error body: {where}{key}" + (f": {m}" if (m := _err_text(f[key])) else ""))
            if write:
                out += [x for x in (_partial_failure(o, where) for where, o in [("", v), *fields]) if x]
        elif re.match(r"\s*<(?:!doctype html|html)\b", text, re.I) and API_PATH.search(path):
            out.append("HTML on an API endpoint")
    out += [f"leak: {label}" for label, rx in LEAKS if rx.search(text)]
    return out


def signal_severity(key):
    """'severe' (a 5xx, a failed request, a 200 with an error body, a leak signature), 'client' (a 4xx) or 'other'
    (a request loop, a console error) for a triage key, with or without the [NEW] / (seen in N runs) marks."""
    if "[leak:" in key or re.search(r"→ (?:5\d\d|FAILED)\b|→ 2\d\d \[", key):
        return "severe"
    return "client" if re.search(r"→ 4\d\d\b", key) else "other"


def _bodies_file(data, slug):
    try:
        v = json.loads((data / f"bodies-{slug}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _bodies(data, slug):
    """{request number: [finding]} qa-mem body-check recorded in .data/bodies-<slug>.json ({} without one)."""
    return {
        str(k): [scrub.redact(str(f)) for f in fs if isinstance(f, str)]
        for k, fs in _bodies_file(data, slug).items()
        if isinstance(fs, list)
    }


def _body_gone(data, slug):
    """The request numbers whose body the browser no longer held when body-check asked (the file's `gone`): not
    judged, and nothing more to read."""
    v = _bodies_file(data, slug).get("gone")
    return {str(x) for x in v} if isinstance(v, list) else set()


def _body_ops(data, slug):
    """{request number: GraphQL operation name} of the calls `qa-mem body-check --all` checked (the file's `ops`)."""
    v = _bodies_file(data, slug).get("ops")
    return (
        {str(k): n for k, n in v.items() if isinstance(n, str) and GQL_NAME.fullmatch(n)} if isinstance(v, dict) else {}
    )


def check_body(run_dir, slug, index, text, op="", mutation=False):
    """Judge the body of request `index` (the number `requests` printed) and record the verdict, clean or not, in
    .data/bodies-<slug>.json for triage_signals. The method, path and status come from .data/requests-<slug>.log when it
    has the request. op, mutation: the GraphQL operation's name and whether it writes, when the caller read the request
    (every operation shares one method and path, so the name tells them apart). An empty body of a 2xx that should
    have one is not judged: the browser drops a response's body when the page navigates, so empty says nothing. It is
    recorded as gone, never as clean. Returns (what it was, findings), findings None when there was no body."""
    data = Path(run_dir) / ".data"
    line = next(
        (
            m
            for m in map(REQUEST_LINE.fullmatch, results.read_text(data / f"requests-{slug}.log").splitlines())
            if m and m.group(1) == index
        ),
        None,
    )
    path, status = "", 200
    if line:
        try:
            path = urlsplit(line.group(3)).path
        except ValueError:
            pass
        status = int(line.group(4)) if line.group(4).isdigit() else 0
    write = mutation if RPC_PATH.search(path) else line is not None and line.group(2) in probe.API_WRITES
    no_body = not text.strip() and 200 <= status < 300 and status not in (204, 205)
    findings = None if no_body else [scrub.redact(f) for f in body_verdict(text, path, status, write)]
    recorded: dict[str, Any] = _bodies(data, slug)
    names, gone = _body_ops(data, slug), _body_gone(data, slug)
    if findings is None:
        gone.add(index)
    else:
        recorded[index] = findings
    if GQL_NAME.fullmatch(op):
        names[index] = op
    if names:
        recorded["ops"] = names
    if gone:
        recorded["gone"] = sorted(gone, key=lambda x: (len(x), x))
    tmp = data / f".bodies-{slug}.tmp"
    tmp.write_text(json.dumps(recorded, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, data / f"bodies-{slug}.json")
    shown = path + (f"#{names[index]}" if index in names else "")
    return (f"{line.group(2)} {shown} → {line.group(4)}" if line else ""), findings


def _gql_operation(text):
    """(operation name or '', whether it is a mutation) of a GraphQL request body; ('', False) for anything else."""
    try:
        sent = json.loads(text)
    except (ValueError, RecursionError):
        return "", False
    name, mutation = "", False
    for o in (sent if isinstance(sent, list) else [sent])[:10]:  # a batch: named after its first operation
        if not isinstance(o, dict):
            continue
        query = o["query"] if isinstance(o.get("query"), str) else ""
        said = o["operationName"] if isinstance(o.get("operationName"), str) else ""
        m = re.search(rf"\b(query|mutation|subscription)\s+{re.escape(said)}\b", query) if said else None
        m = m or re.match(r"\s*(query|mutation|subscription)\b\s*(\w*)", query)
        said = said or (m.group(2) if m and m.lastindex == 2 else "")
        name = name or (said if GQL_NAME.fullmatch(said) else "")
        mutation = mutation or bool(m and m.group(1) == "mutation")
    return name, mutation


def _sign_in_operation(name):
    """Whether a GraphQL operation is named like a sign-in call (RefreshToken, login): its answer carries credentials."""
    return any(w.lower() in probe.API_AUTH_SEGMENTS for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", name))


def _saved_body(text):
    """A response body as text: playwright-cli saves a binary one to .playwright-cli/response-*.<ext> and prints the
    path instead, so that file is removed and the body is '' (nothing to read)."""
    p = Path(text.strip()) if re.fullmatch(r"\S*\.playwright-cli[/\\]response-[^/\\\s]+", text.strip()) else None
    if p is None:
        return text
    p.unlink(missing_ok=True)
    return ""


def sweep_bodies(run_dir, slug):
    """`qa-mem body-check <run> --slug <slug> --all`: with the browser still open, save its request list as
    .data/requests-<slug>.log (as step 9 does), then read the body of every call triage would name (triage_signals'
    `unchecked`: GraphQL and RPC calls, writes, then failed calls; never a sign-in path) and record each verdict, at
    most SWEEP_CALLS in SWEEP_SECONDS. A GraphQL call's request body is read first for its operation name and kind, and
    is never stored; an operation named like a sign-in is settled without reading its answer. A body the browser no
    longer holds (the page navigated since the call) is counted as gone, never as clean: run this before a reload.
    Returns {"checked": [(request number, what it was, findings)], "gone": n, "unread": n, "why": the first reason one
    could not be read, "left": calls past the cap}."""
    from . import crawl  # the playwright-cli runner; crawl imports nothing from this module

    log = Path(run_dir) / ".data" / f"requests-{slug}.log"
    ok, listed = crawl.pw(["requests", "--all"], SWEEP_CALL_SECONDS)
    if ok:
        core.write(log, listed)
    elif not log.is_file():
        raise core.BadInput(
            f"body-check --all: the browser isn't open and there is no {log} ({core.short(' '.join(listed.split()), 120)})"
        )
    todo = triage_signals(run_dir, slug, {})["unchecked"]
    checked, gone, unread, why, deadline = [], 0, 0, "", time.monotonic() + SWEEP_SECONDS
    for index, _method, path in todo[:SWEEP_CALLS]:
        if time.monotonic() > deadline:
            break
        op, mutation, body = "", False, "{}"  # (a sign-in operation is settled as an empty answer, unread)
        if RPC_PATH.search(path):
            ok, sent = crawl.pw(["--raw", "request-body", index], SWEEP_CALL_SECONDS)
            op, mutation = _gql_operation(sent) if ok else ("", False)
        if not _sign_in_operation(op):
            ok, body = crawl.pw(["--raw", "response-body", index], SWEEP_CALL_SECONDS)
            if not ok:  # the browser dropped the request, or it is closed: the call stays a suspect
                unread, why = unread + 1, why or core.short(" ".join(body.split()), 120)
                continue
            # a binary body was saved to a file: nothing to read in it, and not a body that is gone
            body = body if _saved_body(body) == body else "{}"
        what, findings = check_body(run_dir, slug, index, body[:200_000], op, mutation)
        if findings is None:
            gone += 1
        else:
            checked.append((index, what, findings))
    left = len(todo) - len(checked) - gone - unread
    return {"checked": checked, "gone": gone, "unread": unread, "why": why, "left": left}


def triage_signals(run_dir, slug, run, hosts=None):
    """{"app": [(signal, count, first request index or None)], "third_party": count, "suspect_200": [(call, index)],
    "unchecked": [(index, method, path)] (every call whose body nobody has read yet, for `body-check --all`)} from
    .data/requests-<slug>.log, console-<slug>.log and bodies-<slug>.json (what `qa-mem body-check` found in the bodies the
    explorer read): failed requests (4xx, 5xx, FAILED), a 2xx whose body was an error envelope, an HTML page or a leak, a
    call made STORM_CALLS+ times (a request loop), and console errors, each once, minus those the run JSON or a bug draft
    names (a request by its method and path, a message by its first 60 characters), and minus the requests the
    explorer's own `qa-mem api-probe` sent (.data/probes-<slug>.jsonl). A leak signature (stack trace, SQL
    error) in a body or a console message is added to the signal in brackets. A failed request to a host outside
    allowedHosts (a CDN, map tiles) is only counted. "Failed to load resource" lines repeat the requests log and are
    skipped. `suspect_200` is up to SUSPECT_200_SHOWN 2xx calls nobody has checked or named whose body the log can't show
    (GraphQL and RPC calls, then writes, never login, session or token paths): the explorer reads their bodies while the
    browser is open. hosts: allowedHosts (None: the config's; empty: every host is the app's)."""
    data, found, calls, third = Path(run_dir) / ".data", {}, {}, 0
    hosts = core.allowed_hosts(core.config()) if hosts is None else hosts
    checked, seen_2xx, checked_shapes = _bodies(data, slug), [], set()
    names, gone, unchecked = _body_ops(data, slug), _body_gone(data, slug), []
    probes = []  # [method, url, status] of the requests `qa-mem api-probe` sent (probe._note_probe): the explorer's own
    for line in results.read_text(data / f"probes-{slug}.jsonl").splitlines():
        try:
            p = json.loads(line)
            probes.append([str(p["method"]), scrub.redact(str(p["url"])), str(p["status"])])
        except (ValueError, KeyError, TypeError):
            continue
    probe_failed = {url for _, url, status in probes if status == "FAILED"}  # the console names these too
    for line in scrub.redact(results.read_text(data / f"requests-{slug}.log")).splitlines():
        m = REQUEST_LINE.fullmatch(line)
        if not m:
            continue
        index, method, url, code, text = m.groups()
        if [method, url, code] in probes:  # one log line for each probe: the page's own calls of that URL stay
            probes.remove([method, url, code])
            continue
        try:
            path = urlsplit(url).path or url
        except ValueError:  # an odd host such as an unclosed [IPv6
            path = url
        if hosts and url.lower().startswith(("http://", "https://")) and not core.url_allowed(url, hosts):
            third += code == "FAILED" or int(code) >= 400
            continue
        shape = core.mask_ids(path) or path
        if method == "GET":  # a POST many times is a user (or a test) saving records, not a fetch loop
            calls.setdefault((method, shape), [0, index])[0] += 1
        notes = f" [{'; '.join(checked[index])}]" if checked.get(index) else ""  # what body-check saw in its body
        rpc = bool(RPC_PATH.search(path))
        # every GraphQL operation shares one method and path: a check settles its own call, and the operation's name
        # (when body-check read it) is part of what the call is called
        shown = path + (f"#{names[index]}" if index in names else "")
        if index in checked and not rpc:
            checked_shapes.add((method, shape))
        if index in gone:  # its body was gone when body-check asked: nothing more to read, and no verdict
            pass
        elif index not in checked and code != "FAILED" and not _credential_call(path):  # body-check --all's work list
            turn = (2 if int(code) >= 500 else 3) if int(code) >= 400 else 0 if rpc else 1
            if turn != 1 or method in probe.API_WRITES:
                unchecked.append((turn, index, method, path))
        if code == "FAILED" or int(code) >= 400:
            status = code if code != "FAILED" else f"FAILED {text}".strip()
            found.setdefault(f"{method} {shown} → {status}{notes}", [(method, shown), 0, index])[1] += 1
        elif notes:
            found.setdefault(f"{method} {shown} → {code}{notes}", [(method, shown), 0, index])[1] += 1
        elif index not in checked and index not in gone:
            seen_2xx.append((method, path, shape, index))
    for (method, shape), (n, index) in calls.items():
        if n >= STORM_CALLS:
            found[f"{method} {shape} ×{n} (the same call {n} times: a request loop?)"] = [(method, shape), 1, index]
    for line in scrub.redact(results.read_text(data / f"console-{slug}.log")).splitlines():
        if not line.strip() or line[:1].isspace() or line.startswith(("###", "Total messages:", "Returning ")):
            continue  # blank, stack lines, headers
        level = re.match(r"\[(\w+)\]\s*", line)  # a page error (uncaught exception) has no [LEVEL]
        msg = re.sub(r"\s+@\s+\S*$", "", line[level.end() :] if level else line).strip()  # " @ <source>:1"
        if any(url in msg for url in probe_failed):  # a probe the browser refused (CORS): its own error message
            continue
        if msg and not msg.startswith("Failed to load resource:") and (not level or level.group(1).upper() == "ERROR"):
            leaks = "".join(
                f" [leak: {label}]" for label, rx in LEAKS if rx.search(msg)
            )  # the server's own words in the page
            found.setdefault(f"console: {msg}{leaks}", [msg[:60], 0, None])[1] += 1
    drafts = "".join(map(results.read_text, sorted((Path(run_dir) / "bugs").glob("*.md"))))
    named = (json.dumps(run, ensure_ascii=False) + drafts).lower()  # a console message: any mention in the run
    # a failed request: bugs, notes, non-PASS cases and drafts only; a passing fact or locator that mentions the path
    # (`POST /form saves the zone`) must not hide `POST /form → 500`
    cases = [c for c in results.entries(run, "cases") if core.case_status(c.get("status")) != "PASS"]
    explains = (
        json.dumps([results.entries(run, "bugs"), run.get("notes"), cases], ensure_ascii=False) + drafts
    ).lower()
    explained = lambda w: _names_request(explains, *w) if isinstance(w, tuple) else w.lower() in named
    rank = lambda c: 0 if RPC_PATH.search(c[1]) else 1
    suspects = {}
    for method, path, shape, index in sorted(
        (c for c in seen_2xx if (RPC_PATH.search(c[1]) or c[0] in probe.API_WRITES) and not _credential_call(c[1])),
        key=rank,
    ):
        if (method, shape) not in checked_shapes and not explained((method, path)):
            suspects.setdefault((method, shape), index)
    return {
        "app": [(key, n, index) for key, (what, n, index) in found.items() if not explained(what)],
        "third_party": third,
        "suspect_200": [(f"{method} {shape}", index) for (method, shape), index in suspects.items()][
            :SUSPECT_200_SHOWN
        ],
        "unchecked": [x[1:] for x in sorted(unchecked, key=lambda x: x[0])],
    }


def signal_baseline():
    """{(page, kind, key): (status, runs seen, first run)} from the memory DB, opened read-only; {} without one (the
    report renders without a DB too)."""
    import sqlite3

    p = core.db_path()
    if not p.is_file():
        return {}
    try:
        con = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return {
                (r[0], r[1], r[2]): (r[3], r[4], r[5])
                for r in con.execute("SELECT page, kind, sig_key, status, seen_runs, first_run FROM signals")
            }
        finally:
            con.close()
    except sqlite3.Error:
        return {}


def run_signals(run_dir, name, run, hosts=None, baseline=None):
    """([(signal, count)], third-party count) of run file `name` (triage_signals). With the memory's baseline: a signal
    the user ignored is left out, one first seen in this run is marked [NEW], one seen before says in how many runs."""
    from . import memory

    t = triage_signals(run_dir, name[len("run-") : -len(".json")], run, hosts)
    target = str(run.get("target") or "")
    page = core.norm_route(target) if target.startswith(("/", "http", "#")) else "*"
    out = []
    for key, n, _ in t["app"]:
        status, runs, first = (baseline or {}).get((page, *(memory.signal_key(key) or ("", ""))), (None, 0, None))
        if status == "IGNORED":
            continue
        out.append(
            (
                "[NEW] " + key
                if first is not None and first == str(run.get("run_id"))
                else key + (f" (seen in {runs} runs)" if runs >= 2 else ""),
                n,
            )
        )
    return out, t["third_party"]
