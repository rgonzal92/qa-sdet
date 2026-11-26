"""Ingest of run JSON, and the `card`, `search` and `bugs` readers."""

from __future__ import annotations

import fcntl
import hashlib
import itertools
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import codemap, core, scrub

TYPE_CHECKING = False  # mypy reads it as True (pyproject always_true): typing stays out of the import path
if TYPE_CHECKING:  # NOSONAR
    from typing import Any

    from .contracts import JsonObj


def resolve_draft(path, run_dir):
    """Absolute draft path: the DB is per app and shared by projects, so relative paths would be ambiguous.
    Relative paths are tried against the project (parent of qa-out/) and then the run folder."""
    if not path:
        return None  # "" from a re-sighting must not wipe the draft path the DB already has
    if run_dir is None:
        return str(path)
    p = Path(os.path.expanduser(str(path)))
    if p.is_absolute():
        return str(p)
    run_dir = Path(run_dir).resolve()
    tries = [run_dir / p]
    if run_dir.name == ".data":  # qa-out/<run>/.data/run-<slug>.json: drafts sit in qa-out/<run>/bugs/
        tries.insert(0, run_dir.parent / p)
    for anc in (run_dir, *run_dir.parents):
        if anc.name == "qa-out":
            tries.insert(0, anc.parent / p)
            break
    return str(next((t for t in tries if t.exists()), tries[0]))


STOP_REASONS = (
    "saturated",
    "charter_complete",
    "diminishing_returns",
    "blocked",
)
BLOCK_SCOPES = ("page", "session", "browser")  # what a BLOCKED run blocks: this page, the sign-in, the browser


def ingest(
    con: sqlite3.Connection, run: JsonObj, ts: str | None = None, run_dir: Path | None = None, src: str | Path = ""
) -> None:
    """Upsert one run.json dict. ts = when the run finished (file mtime); run_dir = folder of the run.json;
    src = the file it came from (a rewrite of that file replaces its bugs).
    Raises ValueError on bad shape."""
    rid, build, known, pages, bug_list = check_run(run)
    ts = ts or core.now()
    _store(con, run, rid, build, known, pages, bug_list, ts, run_dir, src)


def check_run(run: object) -> tuple[str, str, bool, list[JsonObj], list[JsonObj]]:
    """(run id, build, build known, pages, bugs) of a run.json dict, routes normalised; ValueError on a bad shape. The
    PostToolUse hook runs it when the explorer writes the file, ingest before storing it."""
    if not isinstance(run, dict):
        raise ValueError("run.json must be an object")
    rid = core.txt(run.get("run_id"), "run_id")
    if not rid or not rid.strip():
        raise ValueError("run.json missing 'run_id'")
    # Same build text however the page laid it out ("Build:\n 1.42" == "Build: 1.42"); capped like all text.
    # An empty or missing build (empty version element) is unknown, not a broken run.
    build = core.norm_build(core.txt(run.get("build"), "build"))
    known = build.lower() != core.UNKNOWN
    for f in ("mode", "target", "status", "summary", "block_scope"):
        core.txt(run.get(f), f)
    if run.get("block_scope") not in (None, "", *BLOCK_SCOPES):
        raise ValueError(f"'block_scope' must be one of {', '.join(BLOCK_SCOPES)}")
    core.txt(run.get("stop_reason"), "stop_reason")
    if run.get("stop_reason") not in (None, "", *STOP_REASONS):
        raise ValueError(f"'stop_reason' must be one of {', '.join(STOP_REASONS)}")
    pages, bug_list = core.dicts(run, "pages"), core.dicts(run, "bugs")
    for pg in pages:
        pg["route"] = core.norm_route(core.txt(pg.get("route"), "route"))
        if not pg["route"]:
            raise ValueError("page entry missing 'route'")
        core.txt(pg.get("name"), "name")
        for key, fields in (
            ("facts", ("kind", "text", "source")),
            ("locators", ("name", "locator")),
            ("coverage", ("target", "heuristic", "result")),
            ("states", ("key", "kind", "trigger", "status")),
            ("transitions", ("to", "trigger", "sample_path", "kind")),
        ):
            for item in core.dicts(pg, key):
                for f in fields:
                    core.txt(item.get(f), f"{key}.{f}")
    check_report_fields(run)
    for b in bug_list:
        for f in (
            "title",
            "symptom",
            "severity",
            "classification",
            "draft_path",
            "hash",
            "route",
            "evidence",
            "ticket",
            "heuristic",
            "endpoint",
            "pattern",
        ):
            core.txt(b.get(f), f"bugs.{f}")
        if b.get("annotated") is not None and not isinstance(b["annotated"], bool):
            raise ValueError("'bugs.annotated' must be true or false")
        b["route"] = core.norm_route(b.get("route"))
        if not core.cap(b.get("title")) or not b["route"]:
            raise ValueError("bug entry needs 'title' and 'route'")
    return rid, build, known, pages, bug_list


# In _store's bug upsert: this sighting makes a FIXED bug REGRESSED. ? is 1 for a run that hadn't listed the bug yet; a
# run older than the last sighting or than the fix (ingested late) predates them: no regression.
REGRESSES = (
    "status='FIXED' AND ? AND excluded.last_seen >= coalesce(last_seen, '') "
    "AND excluded.last_seen >= coalesce(fixed_ts, '')"
)


def _store(con, run, rid, build, known, pages, bug_list, ts, run_dir, src):
    with con:
        statuses = [core.case_status(c.get("status")) for c in core.dicts(run, "cases")]
        con.execute(
            "INSERT OR REPLACE INTO runs(id,mode,target,build,status,summary,ts,cases_pass,cases_fail,cases_blocked,"
            "bugs_found) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                rid,
                core.cap(run.get("mode")) or None,
                core.cap(run.get("target")),
                build,
                core.cap(run.get("status")) or None,
                core.cap(run.get("summary")),
                ts,
                statuses.count("PASS"),
                statuses.count("FAIL"),
                statuses.count("BLOCKED"),
                len(bug_list),
            ),
        )
        # The deployed build only moves forward in time: an old run.json ingested late must not roll it back.
        if known and (core.meta(con, "build_ts") or "") <= ts:
            core.set_meta(con, "build", build)
            core.set_meta(con, "build_ts", ts)
        # the read-only map (qa-mem crawl and discover) and a blocked start test nothing
        explored = core.norm(run.get("mode")) != "map" and core.norm(run.get("status")) != "blocked"
        for pg in pages:
            route = pg["route"]
            old = con.execute("SELECT build, last_verified FROM pages WHERE route=?", (route,)).fetchone()
            # A run older than what we already know (ingested late) must not roll the page back.
            late = bool(old and old["last_verified"] and ts < old["last_verified"])
            # A late file of the page's current build still saw the current page: its new facts and locators count.
            same_build = bool(known and old and old["build"] == build)
            current = known and (not late or same_build)
            if known and not late and old and old["build"] not in (build, core.UNKNOWN, None):  # new build: re-verify
                con.execute("UPDATE facts SET stale=1 WHERE page=? AND pinned=0", (route,))  # the user's own stay
                con.execute("UPDATE locators SET stale=1 WHERE page=?", (route,))
            if late:
                con.execute("INSERT OR IGNORE INTO pages(route, name, build) VALUES(?,?,?)", (route, None, build))
            elif known:
                con.execute(
                    "INSERT INTO pages(route, name, build, last_verified) VALUES(?,?,?,?) ON CONFLICT(route) DO UPDATE SET "
                    "name=coalesce(excluded.name, name), build=excluded.build, "
                    "last_verified=excluded.last_verified",
                    (route, core.cap(pg.get("name")) or None, build, ts),
                )
            else:  # e.g. a plan from a ticket: knows the page, verified nothing
                con.execute(
                    "INSERT INTO pages(route, name, build) VALUES(?,?,?) ON CONFLICT(route) DO UPDATE "
                    "SET name=coalesce(name, excluded.name), build=coalesce(build, excluded.build)",
                    (route, core.cap(pg.get("name")) or None, core.UNKNOWN),
                )
            if explored:
                con.execute("UPDATE pages SET explored=max(coalesce(explored, ''), ?) WHERE route=?", (ts, route))
            for f in pg.get("facts") or []:
                text = core.cap(f.get("text"))
                if text:
                    # Only a verifying run (known build, not late) refreshes an existing fact.
                    con.execute(
                        "INSERT INTO facts(page,kind,text,source,build,ts,stale) VALUES(?,?,?,?,?,?,?) "
                        "ON CONFLICT(page,kind,text) DO UPDATE SET "
                        "source=CASE WHEN ? AND lower(coalesce(excluded.source, '')) = 'observed' "
                        "THEN 'observed' ELSE source END, "  # a ticket rule the explorer has now seen
                        "stale=CASE WHEN ? THEN 0 ELSE stale END, "
                        "build=CASE WHEN ? THEN excluded.build ELSE build END, "
                        "ts=CASE WHEN ? THEN excluded.ts ELSE ts END",
                        (
                            route,
                            core.cap(f.get("kind"))[:30] or "note",
                            text,
                            core.cap(f.get("source")),
                            build,
                            ts,
                            int(late and not same_build),
                            int(current),
                            int(current),
                            *(int(known and not late),) * 2,
                        ),
                    )
            for loc in pg.get("locators") or []:
                # A truncated locator would be broken, so skip oversized ones instead of capping.
                loc_name, loc_val = (
                    core.cap(loc.get("name")),
                    (core.txt(loc.get("locator"), "locators.locator") or "").strip(),
                )
                if (not late or same_build) and loc_name and loc_val and len(loc_val) <= core.MAX_TEXT:
                    # late + same build, or unknown-build plan on a verified page: add, never overwrite/un-stale verified
                    keep_old = late or (not known and bool(old and old["build"] not in (core.UNKNOWN, None)))
                    con.execute(
                        f"INSERT OR {'IGNORE' if keep_old else 'REPLACE'} INTO locators VALUES(?,?,?,?,?,0)",
                        (route, loc_name, loc_val, build, ts),
                    )
            _store_states(con, route, pg, build, ts)
            for c in pg.get("coverage") or []:
                if core.norm(c.get("heuristic")):
                    # the newest run's result stands: a run.json ingested late must not put its older one back
                    con.execute(
                        "INSERT INTO coverage VALUES(?,?,?,?,?,?,?) ON CONFLICT(page,target,heuristic,build) "
                        "DO UPDATE SET result=excluded.result, run_id=excluded.run_id, ts=excluded.ts "
                        "WHERE excluded.ts >= coalesce(coverage.ts, '')",
                        (
                            route,
                            core.cap(c.get("target")) or "*",
                            core.heuristic_id(c["heuristic"]),
                            core.coverage_result(c.get("result")),
                            build,
                            rid,
                            ts,
                        ),
                    )
        for pg in pages:  # size caps: keep the freshest knowledge per page
            con.execute(
                "DELETE FROM facts WHERE page=? AND id NOT IN (SELECT id FROM facts WHERE page=? "
                "ORDER BY pinned DESC, stale, ts DESC LIMIT ?)",
                (pg["route"], pg["route"], core.MAX_FACTS),
            )
            con.execute(
                "DELETE FROM coverage WHERE page=? AND rowid NOT IN (SELECT rowid FROM coverage WHERE page=? "
                "ORDER BY ts DESC LIMIT ?)",
                (pg["route"], pg["route"], core.MAX_COVERAGE),
            )
            con.execute(
                "DELETE FROM locators WHERE page=? AND name NOT IN (SELECT name FROM locators WHERE page=? "
                "ORDER BY stale, last_verified DESC LIMIT ?)",
                (pg["route"], pg["route"], core.MAX_LOCATORS),
            )
        # sightings = which bugs this run file reported. A rewritten run.json (or a sibling's hook ingesting it
        # mid-run) replaces its earlier version: a bug it no longer lists is un-sighted (dropped when only it ever
        # saw it), and seen_count counts distinct run ids even when runs are re-ingested interleaved.
        src = str(src or "")
        before = {r[0] for r in con.execute("SELECT hash FROM sightings WHERE run_id=? AND src=?", (rid, src))}
        ran = lambda h: con.execute("SELECT 1 FROM sightings WHERE run_id=? AND hash=?", (rid, h)).fetchone()
        now_seen = set()
        for b in bug_list:
            symptom = core.cap(b.get("symptom")) or core.cap(b["title"])
            raw = str(b.get("hash") or "").strip()
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*-\d+", raw) and not re.fullmatch(r"(?i)BUG-\d+", raw):
                continue  # a Jira key: a bug tracked in Jira, not a local one to create (BUG-n is a draft's id: created)
            given = core.given_hash(raw)  # 'n/a', 'TBD', 'new': no hash, so the bug is created
            h = None
            if given:  # re-sighting of a known bug (prefix ok if unique)
                hits = con.execute("SELECT hash FROM bugs WHERE substr(hash,1,?)=?", (len(given), given)).fetchall()
                h = hits[0][0] if len(hits) == 1 else None
            h = h or core.bug_hash(b["route"], symptom)
            # a new sighting counts once; a bug from before sightings existed counts this run via last_run
            fresh = int(
                not ran(h) and not con.execute("SELECT 1 FROM bugs WHERE hash=? AND last_run=?", (h, rid)).fetchone()
            )
            now_seen.add(h)
            con.execute(
                "INSERT OR IGNORE INTO sightings(run_id,src,hash,page,build) VALUES(?,?,?,?,?)",
                (rid, src, h, b["route"], build),
            )  # where it showed: a shared-endpoint bug on another page too
            # a FIXED bug seen again by another run becomes REGRESSED.
            con.execute(
                "INSERT INTO bugs(hash,page,title,severity,classification,draft_path,first_seen,"
                "last_seen,last_run,symptom,heuristic,endpoint,pattern,ticket,first_build,last_build) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(hash) DO UPDATE SET "
                "first_build=coalesce(first_build, excluded.first_build), "
                "last_build=CASE WHEN excluded.last_build IS NOT NULL AND excluded.last_seen >= coalesce(last_seen, '') "
                "THEN excluded.last_build ELSE last_build END, "
                f"regressed_count=coalesce(regressed_count, 0) + CASE WHEN {REGRESSES} THEN 1 ELSE 0 END, "
                "heuristic=coalesce(excluded.heuristic, heuristic), endpoint=coalesce(excluded.endpoint, endpoint), "
                "pattern=coalesce(excluded.pattern, pattern), ticket=coalesce(excluded.ticket, ticket), "
                "seen_count=seen_count + ?, "
                f"status=CASE WHEN {REGRESSES} THEN 'REGRESSED' ELSE status END, "
                f"regressed_by=CASE WHEN {REGRESSES} THEN excluded.last_run ELSE regressed_by END, "
                "last_run=CASE WHEN excluded.last_seen >= coalesce(last_seen, '') THEN excluded.last_run ELSE last_run END, "
                "last_seen=max(coalesce(last_seen, ''), excluded.last_seen), "
                "first_seen=min(coalesce(first_seen, excluded.first_seen), excluded.first_seen), "
                "severity=coalesce(excluded.severity, severity), "
                "classification=coalesce(excluded.classification, classification), "
                "draft_path=coalesce(excluded.draft_path, draft_path), "
                "symptom=coalesce(symptom, excluded.symptom)",
                (
                    h,
                    b.get("route"),
                    core.cap(b["title"]),
                    core.cap(b.get("severity"))[:20] or None,
                    core.cap(b.get("classification"))[:20] or None,
                    resolve_draft(b.get("draft_path"), run_dir),
                    ts,
                    ts,
                    rid,
                    symptom,
                    core.heuristic_id(b["heuristic"]) if core.norm(b.get("heuristic")) else None,
                    _endpoint(b.get("endpoint")),
                    _pattern(b.get("pattern")),
                    core.cap(b.get("ticket"))[:30] or None,
                    build if known else None,
                    build if known else None,
                    fresh,
                    fresh,
                    fresh,
                    fresh,
                ),
            )
        for h in before - now_seen:  # listed by an earlier version of this run.json only
            con.execute("DELETE FROM sightings WHERE run_id=? AND src=? AND hash=?", (rid, src, h))
            if ran(h):
                continue  # another file of the same run id still lists it
            con.execute(
                "UPDATE bugs SET seen_count=max(seen_count - 1, 0), "
                "last_run=CASE WHEN last_run=? THEN NULL ELSE last_run END WHERE hash=?",
                (rid, h),
            )
            # the sighting that made a FIXED bug REGRESSED is gone (a checkpoint listed it, the final file didn't)
            con.execute(
                "UPDATE bugs SET status='FIXED', regressed_count=max(coalesce(regressed_count, 0) - 1, 0), "
                "regressed_by=NULL WHERE hash=? AND status='REGRESSED' AND regressed_by=?",
                (h, rid),
            )
            # only this run ever saw it and the user hasn't triaged it: it was never a bug
            con.execute(
                "DELETE FROM bugs WHERE hash=? AND seen_count=0 AND status='OPEN' AND NOT EXISTS "
                "(SELECT 1 FROM sightings WHERE hash=?)",
                (h, h),
            )
        target = (
            core.norm_route(core.cap(run.get("target")))
            if str(run.get("target") or "").startswith(("/", "http", "#"))
            else ""
        )
        _store_questions(con, run.get("questions"), target or "*", ts)
        if explored and target:
            _store_blind_spots(con, run, target, rid, build, ts)
        con.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY ts DESC LIMIT ?)", (core.MAX_RUNS,))
        con.execute("DELETE FROM sightings WHERE run_id NOT IN (SELECT id FROM runs)")


BLIND_SPOTS_PER_PAGE = 50


def _store_blind_spots(con, run, page, rid, build, ts):
    """The ledger of what runs leave untested, per page: each `not_tested` line of the run counts once per run. A line
    a later COMPLETED run no longer repeats is marked resolved (covered, or no longer deferred); repeating it again
    reopens it. An unfinished run says nothing about what a finished one left out."""
    from . import report

    for cat, text in report._not_tested(run):
        con.execute(
            "INSERT INTO blind_spots(page,category,text,build,first_seen,last_seen,last_run) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(page,category,text) DO UPDATE SET seen_runs=seen_runs + (last_run IS NOT excluded.last_run), "
            "last_run=excluded.last_run, build=excluded.build, last_seen=excluded.last_seen, resolved=0 "
            "WHERE excluded.last_seen >= coalesce(blind_spots.last_seen, '')",
            (page, cat, core.cap(text), build, ts, ts, rid),
        )
    if core.norm(run.get("status")) == "completed":
        con.execute(
            "UPDATE blind_spots SET resolved=1 WHERE page=? AND last_run IS NOT ? AND last_seen <= ?", (page, rid, ts)
        )
    con.execute(
        "DELETE FROM blind_spots WHERE page=? AND rowid NOT IN (SELECT rowid FROM blind_spots WHERE page=? "
        "ORDER BY last_seen DESC, rowid DESC LIMIT ?)",
        (page, page, BLIND_SPOTS_PER_PAGE),
    )


def _endpoint(raw):
    """'POST api/zones' (method and the call shape the code map uses) from 'POST /api/zones/42', or None."""
    method, path = codemap._api_ref(raw) if raw else ("", "")
    return f"{method} {path}".strip() if path else None


def _endpoint_ref(stored):
    m, _, path = str(stored or "").partition(" ")
    return (m, path) if path and re.fullmatch(codemap.HTTP_METHODS, m) else ("", str(stored or ""))


def _pattern(raw):
    """A defect class as a short kebab-case tag ('Silent error as EMPTY!' -> silent-error-as-empty), or None."""
    tag = re.sub(r"[^a-z0-9]+", "-", str(raw or "").lower()).strip("-")[:60].strip("-")
    return tag or None


STATES_PER_PAGE = 60


def _store_states(con, route, pg, build, ts):
    """A page's in-page states (tab, dialog, drawer, wizard step, query view) and the pages it leads to, as reported. A
    state visited on a build stays visited for that build; a new build makes it due again."""
    for st in core.dicts(pg, "states"):
        key = core.cap(st.get("key"))[:120]
        if not key:
            continue
        said = core.norm(st.get("status"))
        status = (
            "visited"
            if said in ("visited", "done", "opened", "checked")
            else "blocked"
            if said == "blocked"
            else "unvisited"
        )
        con.execute(
            "INSERT INTO page_states VALUES(?,?,?,?,?,?,?) ON CONFLICT(page, state_key) DO UPDATE SET "
            "kind=coalesce(excluded.kind, kind), trigger=coalesce(excluded.trigger, trigger), "
            "status=CASE WHEN status='visited' AND build IS excluded.build AND excluded.status!='visited' THEN status "
            "ELSE excluded.status END, build=excluded.build, ts=excluded.ts",
            (
                route,
                key,
                core.cap(st.get("kind"))[:30] or None,
                core.cap(st.get("trigger"))[:200] or None,
                status,
                build,
                ts,
            ),
        )
    con.execute(
        "DELETE FROM page_states WHERE page=? AND state_key NOT IN (SELECT state_key FROM page_states WHERE page=? "
        "ORDER BY ts DESC LIMIT ?)",
        (route, route, STATES_PER_PAGE),
    )
    for t in core.dicts(pg, "transitions"):
        to = core.norm_route(core.cap(t.get("to")))
        if not to or to == route:
            continue
        sample = core.cap(scrub.redact(t.get("sample_path") or ""))
        try:  # its path, and its hash when that is a route (/#/zones/12): never a query string
            at = (
                urlsplit(sample if "://" in sample else "http://x" + sample)
                if sample.startswith(("/", "http"))
                else None
            )
            sample = at.path + core.route_hash(at.fragment) if at else ""
        except ValueError:
            sample = ""
        con.execute(
            "INSERT OR IGNORE INTO pages(route) VALUES(?)", (to,)
        )  # known from a link: its card can say how to reach it
        con.execute(
            "INSERT INTO transitions VALUES(?,?,?,?,?,?,?) ON CONFLICT(from_route, to_route, trigger) DO UPDATE SET "
            "sample_path=coalesce(excluded.sample_path, sample_path), kind=coalesce(excluded.kind, kind), "
            "build=excluded.build, ts=excluded.ts",
            (
                route,
                to,
                core.cap(t.get("trigger"))[:120],
                sample or None,
                core.cap(t.get("kind"))[:30] or None,
                build,
                ts,
            ),
        )


def _store_questions(con, items, page, ts, ticket=None):
    """Upsert questions (strings, or {text, status, answer, ticket}) asked on `page` ('*': app-wide). A resolved one
    with its answer stays resolved when a later run asks it again: answered questions become DECISIONS on cards."""
    for x in items if isinstance(items, list) else []:
        q = x if isinstance(x, dict) else {"text": x} if isinstance(x, str) else {}
        text = core.cap(q.get("text")) if q and isinstance(q.get("text"), str) else ""
        if not text:
            continue
        resolved = str(q.get("status") or "").strip().lower() == "resolved"
        answer = core.cap(q.get("answer")) if isinstance(q.get("answer"), str) else ""
        tk = t if isinstance(t := q.get("ticket"), str) and t.strip() else ticket
        con.execute(
            "INSERT INTO questions(page,ticket,text,status,answer,first_seen,last_seen) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(page,text) DO UPDATE SET last_seen=max(coalesce(last_seen,''), excluded.last_seen), "
            "ticket=coalesce(ticket, excluded.ticket), "
            "status=CASE WHEN excluded.status='resolved' THEN 'resolved' ELSE status END, "
            "answer=CASE WHEN excluded.status='resolved' AND excluded.answer!='' THEN excluded.answer ELSE answer END",
            (page, tk, text, "resolved" if resolved else "open", answer, ts, ts),
        )


SIGNALS_PER_PAGE = 100


def signal_key(key):
    """(kind, key) of a triage signal as the baseline keeps it: request ids and console numbers made generic, so one error
    seen on record 7 and record 12 is one row; None for a request loop (a per-visit count)."""
    if key.startswith("console: "):
        return "console", "console: " + re.sub(r"\d+", "N", key[len("console: ") :])[:200]
    m = re.fullmatch(r"([A-Z]+) (\S+) → (\S+).*", key)
    return (
        ("request", f"{m.group(1)} {core._ids(m.group(2)) or m.group(2)} {m.group(3)}")
        if m and "×" not in key
        else None
    )


def _store_signals(con, run, f, ts):
    """Upsert the unexplained app signals of run file f (.data/run-<slug>.json) for its page."""
    from . import report

    slug = f.name[len("run-") : -len(".json")]
    if not any((f.parent / f"{k}-{slug}.log").is_file() for k in ("requests", "console")):
        return
    target = str(run.get("target") or "")
    page = core.norm_route(target) if target.startswith(("/", "http", "#")) else "*"
    rid, build = str(run.get("run_id")), core.norm_build(core.txt(run.get("build"), "build"))
    with con:
        for key, _, _ in report.triage_signals(f.parent.parent, slug, run)["app"]:
            ks = signal_key(key)
            if ks:
                con.execute(
                    "INSERT INTO signals(page,kind,sig_key,sample,first_build,last_build,first_seen,last_seen,"
                    "first_run,last_run) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(page,kind,sig_key) DO UPDATE SET "
                    "seen_runs=seen_runs + (last_run IS NOT excluded.last_run), last_run=excluded.last_run, "
                    "last_build=excluded.last_build, last_seen=max(last_seen, excluded.last_seen), sample=excluded.sample",
                    (page, ks[0], ks[1], core.cap(key)[:300], build, build, ts, ts, rid, rid),
                )
        con.execute(
            "DELETE FROM signals WHERE page=? AND status='ACTIVE' AND id NOT IN (SELECT id FROM signals WHERE "
            "page=? ORDER BY last_seen DESC LIMIT ?)",
            (page, page, SIGNALS_PER_PAGE),
        )  # IGNORED ones stay


OBSERVED_PER_PAGE = 100  # freshest shapes kept per page
STATIC_FILE = re.compile(r"\.(js|mjs|css|map|png|jpe?g|gif|svg|ico|webp|woff2?|ttf|eot|html?)$", re.I)  # .json stays


def _store_observed(con, run, f, ts):
    """The calls the run's browser made (.data/requests-<slug>.log), kept per page and build as (method, path shape,
    status class): never a query string, a body or a host outside allowedHosts. The read-only crawl is skipped (one log
    for many pages)."""
    from . import report

    slug = f.name[len("run-") : -len(".json")]
    log = f.parent / f"requests-{slug}.log"
    target = str(run.get("target") or "")
    if not log.is_file() or core.norm(run.get("mode")) == "map" or not target.startswith(("/", "http", "#")):
        return
    page, build = core.norm_route(target), core.norm_build(core.txt(run.get("build"), "build"))
    hosts, rows = core.allowed_hosts(core.config()), set()
    for line in scrub.redact(report._text(log)).splitlines():
        m = report.REQUEST_LINE.fullmatch(line)
        if not m:
            continue
        _, method, url, code, _ = m.groups()
        try:
            path = urlsplit(url).path or url
        except ValueError:
            continue
        shape = core._ids(path)
        klass = "failed" if code == "FAILED" else f"{code[0]}xx"
        if (
            not shape
            or STATIC_FILE.search(shape)
            or method not in codemap.HTTP_METHODS.split("|")
            or klass[0] == "1"
            or (hosts and url.lower().startswith(("http://", "https://")) and not core.url_allowed(url, hosts))
        ):
            continue
        rows.add((method, shape, klass))
    with con:
        for method, shape, klass in rows:
            con.execute(
                "INSERT INTO observed_endpoints(method,path,status_class,route,build,ts) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(route,method,path,status_class,build) DO UPDATE SET ts=max(ts, excluded.ts)",
                (method, shape, klass, page, build, ts),
            )
        con.execute(
            "DELETE FROM observed_endpoints WHERE route=? AND rowid NOT IN (SELECT rowid FROM observed_endpoints "
            "WHERE route=? ORDER BY ts DESC, rowid DESC LIMIT ?)",
            (page, page, OBSERVED_PER_PAGE),
        )


def signals(con, route=None, status="ACTIVE"):
    where, args = [], []
    if route:
        where.append("page=?")
        args.append(core.norm_route(route))
    if status != "ALL":
        where.append("status=?")
        args.append(status)
    rows = con.execute(
        "SELECT * FROM signals"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY seen_runs DESC, last_seen DESC LIMIT 50",
        args,
    ).fetchall()
    return (
        "\n".join(
            f"#{x['id']} [{x['status']}] {x['page']} {x['sig_key']} (seen in {x['seen_runs']} run(s), builds "
            f"{x['first_build']}..{x['last_build']})"
            for x in rows
        )
        or "no signals"
    )


def set_signal(con, sid, status):
    with con:
        return con.execute("UPDATE signals SET status=? WHERE id=?", (status, sid)).rowcount == 1


def ingest_plan(con, plan, ts):
    """A plan.json's questions (plans hold no facts or bugs). App-wide ('*'), tagged with their ticket."""
    if isinstance(plan, dict):
        with con:
            _store_questions(con, plan.get("questions"), "*", ts)


def check_report_fields(run):
    """Types of the fields only the report reads (cases, questions, not_tested, notes, env). Raises ValueError."""
    for c in core.dicts(run, "cases"):
        if not core.txt(c.get("id"), "cases.id"):
            raise ValueError("every case needs an 'id' (TC-n from the plan)")
        for f in ("status", "actual", "notes", "bug"):
            core.txt(c.get(f), f"cases.{f}")
        if c.get("annotated") is not None and not isinstance(c["annotated"], bool):
            raise ValueError("'cases.annotated' must be true or false")
        if not core.case_status(c.get("status")):
            raise ValueError(f"case {c['id']}: 'status' must be one of {', '.join(core.CASE_STATUSES)}")
    items = run.get("questions")
    items = [] if items is None else items
    if not isinstance(items, list) or not all(
        isinstance(i, str) or (isinstance(i, dict) and isinstance(i.get("text"), str)) for i in items
    ):
        raise ValueError('\'questions\' must be a list of strings, or of {"text", "status", "answer"} objects')
    items = run.get("not_tested")
    items = [] if items is None else items
    if not isinstance(items, list) or not all(
        isinstance(i, str) or (isinstance(i, dict) and isinstance(i.get("text"), str)) for i in items
    ):
        raise ValueError('\'not_tested\' must be a list of strings, or of {"category", "text"} objects')
    items = run.get("leads")
    if not isinstance([] if items is None else items, list) or not all(
        isinstance(i, dict) and isinstance(i.get("route"), str) for i in items or []
    ):
        raise ValueError('\'leads\' must be a list of {"route", "kind", "target", "reason", "priority"} objects')
    for i in items or []:
        for f in ("kind", "target", "reason", "priority"):
            core.txt(i.get(f), f"leads.{f}")
    core.txt(run.get("notes"), "notes")
    env = run.get("env")
    env = {} if env is None else env
    if not isinstance(env, dict):
        raise ValueError("'env' must be an object")
    for f in ("url", "browser", "context"):
        core.txt(env.get(f), f"env.{f}")


def ingest_pending(root):
    """Scrub the qa-out tree, then ingest every run file and code map that changed since its last ingest. Never raises.
    Touches nothing when root doesn't exist, and no DB when there is nothing new (the hook runs everywhere)."""
    root = Path(root)
    if root.name != "qa-out":  # given qa-out/init or a run folder: its qa-out, where _pending looks
        root = next((p for p in root.parents if p.name == "qa-out"), root)
    if not root.is_dir():
        return 0
    scrub.scrub_tree(root)
    if not _pending(root):  # the common case on every Stop: nothing new, so no DB, no lock
        return 0
    db = core.db_path()
    db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(db.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # Stop and SubagentStop may fire together
        pending = _pending(root)  # re-read under the lock: another hook may have ingested or the agent rewritten
        if not pending:
            return 0
        con = core.connect()
        try:
            return _ingest_files(con, pending)
        finally:
            con.close()


def _first_line(p):
    try:
        return p.read_text().split("\n", 1)[0]
    except OSError:
        return None


def _pending(root):
    """[(file, bytes, mtime, sha1)] for .data/run-*.json and code-map-*.json whose content changed since its last
    ingest, oldest first. Sentinels hold the sha1 of the file they cover, so clock skew or copied mtimes can't
    cause loops."""
    out = []
    for f in [
        *root.glob("*/.data/run-*.json"),
        *root.glob("*/.data/plan.json"),
        *(root / "init").glob("code-map-*.json"),
    ]:  # run folders (runs and plans); code maps: qa-out/init only
        try:
            data, mtime = f.read_bytes(), f.stat().st_mtime
        except OSError:
            continue
        digest = hashlib.sha1(data, usedforsecurity=False).hexdigest()
        if (
            _first_line(f.with_name(f.name + ".ingested"))
            != digest  # (.error is read only when this one doesn't match)
            and _first_line(f.with_name(f.name + ".error")) != digest
        ):
            out.append((f, data, mtime, digest))
    return sorted(out, key=lambda x: x[2])


PARKED = []  # run.json / code-map files this process could not ingest (shown to the agent by the hook)


def parked(f):
    """The error ingest parked run file or code map `f` with, while the file is unchanged since; else None."""
    f = Path(f)
    try:
        digest = hashlib.sha1(f.read_bytes(), usedforsecurity=False).hexdigest()
        lines = f.with_name(f.name + ".error").read_text().split("\n")
    except OSError:
        return None
    return lines[1].split(" ", 1)[-1] if lines[0] == digest and len(lines) > 1 else None


def init_status(init_dir):
    """{total, ingested, dropped_rows: {out: note}, retry: [{area, scan, out, reason}]} for the newest codemap-scan in
    init_dir: a mapper chunk (a scan file with files to read) is settled when its code map, written for that scan, is
    ingested. Maps written but not read yet are ingested first."""
    d = Path(init_dir)
    if d.is_dir():
        ingest_pending(d)
    scans = []
    for f in sorted(d.glob("scan-*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            sid = str(data.get("scan_id") or "")
            repos = [r for r in data.get("repos") or [] if isinstance(r, dict)]
        except (OSError, ValueError, AttributeError, TypeError):
            continue
        m = re.search(r"(\d{8}-\d{6})$", sid)
        if m and any(r.get("files") for r in repos):  # nothing to read: settled, no mapper
            scans.append((m.group(1), f, data))
    newest = max((x[0] for x in scans), default="")
    out: dict[str, Any] = {"total": 0, "ingested": 0, "dropped_rows": {}, "retry": []}
    for stamp, f, data in scans:
        if stamp != newest:
            continue  # a scan file left from an earlier scan
        cm = f.with_name("code-map-" + f.name[len("scan-") :])
        out["total"] += 1
        retry = lambda why: out["retry"].append(
            {"area": data.get("area"), "scan": str(f), "out": str(cm), "reason": why}  # noqa: B023 - called in this loop pass
        )
        try:
            body = cm.read_bytes()
        except OSError:
            retry("no code map written")
            continue
        try:
            written_for = json.loads(body).get("scan_id")
        except (ValueError, AttributeError):
            written_for = data.get("scan_id")  # unreadable: its .error says why
        if written_for != data.get("scan_id"):
            retry(f"the code map was written for an older scan ({written_for})")
        elif err := parked(cm):
            retry(err)
        elif (_first_line(cm.with_name(cm.name + ".ingested")) or "") == hashlib.sha1(
            body, usedforsecurity=False
        ).hexdigest():
            out["ingested"] += 1
            note = next(
                (
                    x[len("dropped rows: ") :]
                    for x in cm.with_name(cm.name + ".ingested").read_text().splitlines()
                    if x.startswith("dropped rows: ")
                ),
                "",
            )
            if note:
                out["dropped_rows"][str(cm)] = note
        else:
            retry("not ingested (the memory DB was busy): run init-status again")
    return out


def parked_codemaps(root):
    """["<file>: <error>"] for the code maps in <qa-out>/init that are parked and unchanged since."""
    root = Path(root)
    if root.name != "qa-out":
        root = next((p for p in root.parents if p.name == "qa-out"), root)
    return [f"{f}: {e}" for f in sorted((root / "init").glob("code-map-*.json")) if (e := parked(f))]


def _ingest_files(con, pending):
    n = 0
    for f, data, mtime, digest in pending:
        done, err = f.with_name(f.name + ".ingested"), f.with_name(f.name + ".error")
        # When the run finished; never in the future (a skewed mtime would freeze the current build).
        ts = datetime.fromtimestamp(min(mtime, time.time()), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            note = ""
            if f.name == "plan.json":
                ingest_plan(con, scrub.redact_json(json.loads(data)), ts)
            elif f.name.startswith("code-map-"):
                cm = scrub.redact_json(json.loads(data))
                dropped = codemap.apply_codemap(con, cm, codemap.load_scan(cm, f), ts)
                note = f"dropped rows: {dropped}\n" if dropped else ""
                if f.parent.name == "init":
                    from . import plugin_log

                    rows_n = len(cm.get("rows")) if isinstance(cm.get("rows"), list) else 0
                    plugin_log.append_event(
                        f.parent.parent,
                        "run_summary",
                        {
                            "run": "init",
                            "slug": f.stem,
                            "mode": "map",
                            "status": "COMPLETED",
                            "bugs_found": 0,
                            "rows": rows_n,
                            "audit_issues": 1 if dropped else 0,
                            "missing_run_file": False,
                        },
                    )
            else:
                doc = scrub.redact_json(json.loads(data))
                ingest(con, doc, ts, f.parent, f.resolve())  # also files the mtime-based scrub skipped
                try:  # the run's browser signals no bug explains: a baseline across runs (never parks the run)
                    _store_signals(con, doc, f, ts)
                except sqlite3.OperationalError:
                    raise  # locked: the whole file is retried on the next hook run (ingest is idempotent)
                except (OSError, ValueError, sqlite3.DatabaseError) as e:
                    print(f"qa-mem: signals of {f.name} not kept: {e}", file=sys.stderr)
                try:  # the calls its browser made, as shapes: what the card's UNEXERCISED ENDPOINTS reads
                    _store_observed(con, doc, f, ts)
                except sqlite3.OperationalError:
                    raise
                except (OSError, ValueError, sqlite3.DatabaseError) as e:
                    print(f"qa-mem: requests of {f.name} not kept: {e}", file=sys.stderr)
            core._write(done, f"{digest}\n{core.now()}\n{note}")
            err.unlink(missing_ok=True)
            n += 1
        except sqlite3.OperationalError as e:  # transient (e.g. DB locked): retry on the next hook run
            print(f"qa-mem: will retry {f}: {e}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - bad file: park it until it is rewritten
            core._write(err, f"{digest}\n{core.now()} {type(e).__name__}: {e}\n")
            print(f"qa-mem: skipped {f}: {e}", file=sys.stderr)
            PARKED.append(f"{f}: {type(e).__name__}: {e}")
    return n


FRAME_MARK = ".contentFrame()."  # in a locator that names its frame, as `playwright-cli generate-locator` prints it


def card(con, q, focus=None, ids=False):
    pg = codemap.find_page(con, q)
    if not pg:
        tip = (
            ""
            if con.execute("SELECT 1 FROM codemap LIMIT 1").fetchone()
            else "tip: /qa-sdet:qa-init maps routes and locators from your source, so pages resolve by name\n"
        )
        return (
            f"NO CARD for '{q}': first visit. Untried heuristics: {', '.join(core.HEURISTICS)}\n"
            + tip
            + bug_lines(con, None)
        )
    r = pg["route"]
    b = core.meta(con, "build") or pg["build"]  # coverage only counts on the build deployed now
    unknown_page = pg["build"] == core.UNKNOWN  # its build was never readable (e.g. a map page without the footer)
    code_only = pg["build"] is None  # known from the code map only: nothing observed yet, nothing to re-verify
    current = pg["build"] == b or unknown_page or code_only
    out = [
        f'PAGE {r} "{pg["name"] or ""}" build={pg["build"]} verified={pg["last_verified"] or "never"}'
        + ("" if core.norm_route(q) == r else f"  (matched from '{q}')")
    ]
    if code_only:
        out.append("NOTE: known from source code (or a link) only; no run has visited it yet.")
    elif not current:
        out.append(f"NOTE: current build is {b}; everything below is from build {pg['build']}: re-verify.")
    elif unknown_page and b != core.UNKNOWN:
        out.append(f"NOTE: this page's build was never readable; its knowledge may predate build {b}.")
    module = codemap.module_bindings(con).get(r)
    if module:  # an app-shell route: its module page's rows (code, rules, endpoints, changes) are read as this page's
        out.append(f"MODULE: {module[0] or '?'} at {module[1]} (its code-map rows below are this page's)")
    facts, seen = [], []
    focus_words = list(dict.fromkeys(w.lower() for w in re.findall(r"\w+", str(focus or ""))))[:8]
    # the charter's facts first (stemmed match on any of its words), then rules, flows, validations, API facts and
    # ticket-stated ones before plain notes: 15 recent notes must not push the page's rules off the card
    on_focus = (
        con.execute(
            "SELECT f.id,f.kind,f.text,f.stale,f.source FROM facts_fts JOIN facts f ON f.id=facts_fts.rowid "
            "WHERE facts_fts MATCH ? AND f.page=? ORDER BY rank LIMIT 15",
            (_fts_query([[w] for w in focus_words], []), r),
        ).fetchall()
        if focus_words
        else []
    )
    pinned = con.execute(
        "SELECT id,kind,text,stale,source FROM facts WHERE pinned=1 AND page IN (?, '*') ORDER BY ts DESC LIMIT 10",
        (r,),
    ).fetchall()  # the user's own (qa-mem remember), app-wide ones included
    rest = con.execute(
        "SELECT id,kind,text,stale,source FROM facts WHERE page=? ORDER BY stale, CASE WHEN kind IN "
        "('rule','validation','flow','api','precondition') OR lower(coalesce(source,'')) NOT IN ('','observed') THEN 0 "
        "ELSE 1 END, ts DESC LIMIT 60",
        (r,),
    ).fetchall()
    pinned_ids = {x["id"] for x in pinned}
    for f in list({x["id"]: x for x in pinned + on_focus + rest}.values()):
        words = set(core.norm(f["text"]).split())
        # Runs restate facts in new words; show only the freshest of near-duplicates.
        if any(
            k == f["kind"]
            and len(words & w) >= 0.8 * max(len(words | w), 1)
            and not any(c.isdigit() for x in words ^ w for c in x)
            for k, w in seen
        ):  # '130km/h' vs '120km/h' is news
            continue
        seen.append((f["kind"], words))
        facts.append(f)
        if len(facts) == 15:
            break
    if facts:
        out.append("FACTS (* = stale, re-verify; 'from X' = stated in X, not yet observed):")
        out += [
            f"- [{f['kind']}]"
            + (" [pinned]" if f["id"] in pinned_ids else "")
            + f" {f['text']}"
            + (f" (from {f['source']})" if f["source"] and f["source"].lower() not in ("observed", "user") else "")
            + (f" #{f['id']}" if ids else "")
            + (" *" if f["id"] not in pinned_ids and (f["stale"] or not current) else "")
            for f in facts
        ]
    decided = con.execute(
        "SELECT text, answer, ticket FROM questions WHERE status='resolved' AND page IN (?, '*') "
        "ORDER BY page='*', last_seen DESC LIMIT 5",
        (r,),
    ).fetchall()
    if decided:
        out += ["DECISIONS (answered questions: oracles; don't ask them again):"] + [
            f"- {x['text']} → {x['answer'] or '(answered)'}" + (f" ({x['ticket']})" if x["ticket"] else "")
            for x in decided
        ]
    asked = con.execute(
        "SELECT id, text FROM questions WHERE status='open' AND page=? ORDER BY last_seen DESC LIMIT 3", (r,)
    ).fetchall()
    if asked:
        out += ["OPEN QUESTIONS (asked before, no answer yet):"] + [f"- #{x['id']} {x['text']}" for x in asked]
    shown = set()
    if current:
        locs, size = [], 0
        # a run that couldn't read the build confirmed them for a week, as its coverage (ON_BUILD): a deploy since may drop them
        rows = con.execute(
            "SELECT name,locator FROM locators WHERE page=? AND stale=0 AND (build IS NOT 'unknown' OR "
            "last_verified >= ?) ORDER BY last_verified DESC",
            (r, _week_ago()),
        ).fetchall()
        # one locator saved under several names is one locator (its newest name); the ones that carry their frame
        # come first: in an app shell a page sits in an iframe, and a locator without it fails as given
        newest = {}
        for x in rows:
            newest.setdefault(x["locator"], x)
        rows = sorted(newest.values(), key=lambda x: FRAME_MARK not in x["locator"])  # stable: newest first in each
        for x in rows:
            entry = f"{core.short(x['name'], 40)}={x['locator']}"  # locators stay whole: a cut one is useless
            if len(locs) >= 20 or size + len(entry) > 2000:
                break
            locs.append(entry)
            shown.add(x["locator"])
            size += len(entry)
        if locs:
            more = (
                f" (+{len(rows) - len(locs)} more verified: `find '<name>'` gives a ref)"
                if len(rows) > len(locs)
                else ""
            )
            out.append("LOCATORS (fresh): " + "; ".join(locs) + more)
            framed = next((x["locator"] for x in rows if FRAME_MARK in x["locator"]), "")
            if (
                framed
            ):  # as `generate-locator` prints it: locator('iframe[name="module"]').contentFrame().getByTestId('save')
                prefix = framed[: framed.rindex(FRAME_MARK) + len(FRAME_MARK)]
                out.append(
                    f"FRAME: this page loads in a frame: put `{prefix}` in front of a locator that lacks it "
                    "(a FROM CODE one too)"
                )
    cov, tried, targets = _coverage(con, r, b)
    untested = _untested_rules(con, r, targets)
    changed, in_changed = _changed_here(con, r, pg)
    out += codemap.codemap_block(
        con, r, shown, first=set(untested) | {x.removeprefix("[server] ") for x in untested} | in_changed
    )
    if changed:
        out.append(
            f"CHANGED SINCE LAST VISIT (git; source HEAD may be ahead of the deployed build: attack these first): "
            f"{', '.join(changed[:5])}"
            + (f" (+{len(changed) - 5})" if len(changed) > 5 else "")
            + (f"; defined there: {'; '.join(sorted(in_changed)[:6])}" if in_changed else "")
        )
    if cov:  # at most 4 targets per heuristic keeps the card short
        out.append(
            f"COVERED on build {b} (only-blocked ones are retried, listed in UNTRIED): "
            + " | ".join(
                f"{h}({', '.join(t[:4])}{f', +{len(t) - 4} more' if len(t) > 4 else ''})" for h, t in cov.items()
            )
        )
    out.append("UNTRIED: " + (", ".join(h for h in core.HEURISTICS if h not in tried) or "none"))
    if focus_words:  # the charter's fields first ('date filters' puts 'Date' and 'Filter' ahead)
        stems = {w.rstrip("s") for w in focus_words}
        untested.sort(key=lambda label: not ({w.rstrip("s") for w in core.norm(label).split()} & stems))
    if untested:  # labels stay whole (the explorer copies them as coverage targets), so the line stops at ~400 chars
        shown_n = next((n for n in range(len(untested), 0, -1) if len("; ".join(untested[:n])) <= 400), 1)
        more = f"; +{len(untested) - shown_n} more (qa-mem code '{r}')" if len(untested) > shown_n else ""
        on = f"build {b}" if b and b != core.UNKNOWN else "this build"
        out.append(
            f"UNTESTED TARGETS (code rules no adversarial check covered on {on}; write the label as the "
            "coverage target): " + "; ".join(untested[:shown_n]) + more
        )
    differ = _client_server_differ(con, r)
    if differ:
        out.append(
            "CLIENT/SERVER RULES DIFFER (a hint to verify through the page, not an oracle): " + "; ".join(differ[:4])
        )
    cross = _cross_page_rules_differ(con, r)
    if cross:
        out.append(
            "CROSS-PAGE RULES DIFFER (a page writing the same resource states them otherwise: a hint to verify on both "
            "pages, not an oracle): " + "; ".join(cross[:3])
        )
    related = _related(con, r)
    if related:
        out.append(
            "RELATED PAGES (a change here may show there; from the code map): "
            + "; ".join(f"{p} ({core.short(why, 70)})" for p, why in related[:4])
            + (f"; +{len(related) - 4} more (qa-mem impact '{r}')" if len(related) > 4 else "")
        )
    out.append(bug_lines(con, r, b))
    shared = _shared_bugs(con, r)
    if shared:
        out += [
            "BUGS ON PAGES SHARING AN ENDPOINT OR COMPONENT (re-check here; seen here = put its hash in run.json, no new "
            "draft):"
        ] + [_bug_line(x, True, 60) + (f" | endpoint {x['endpoint']}" if x["endpoint"] else "") for x in shared]
    noisy = con.execute(
        "SELECT sig_key, seen_runs, first_build FROM signals WHERE page=? AND status='ACTIVE' AND "
        "seen_runs >= 2 ORDER BY seen_runs DESC, last_seen DESC LIMIT 3",
        (r,),
    ).fetchall()
    if noisy:
        out += [
            "RECURRING UNEXPLAINED SIGNALS (no bug explains them; investigate or draft; the user can ignore noise):"
        ] + [f"- {x['sig_key']} (seen in {x['seen_runs']} runs since build {x['first_build']})" for x in noisy]
    due = [
        x["state_key"] + (f" ({core.short(x['trigger'], 80)})" if x["trigger"] else "") for x in _due_states(con, r, b)
    ]
    if due:
        out.append(
            f"UNVISITED STATES (on this page, not opened on {'build ' + b if b and b != core.UNKNOWN else 'this build'}: "
            "open each, check it, report it as visited): "
            + "; ".join(due[:8])
            + (f" (+{len(due) - 8})" if len(due) > 8 else "")
        )
    apis = [] if code_only else _unexercised_apis(con, r, b)
    if apis:
        out.append(
            f"UNEXERCISED ENDPOINTS (mapped in the code, not yet observed on build {b}: exercise them or say why not in "
            "not_tested; a hint, never an oracle): "
            + "; ".join(apis[:4])
            + (f" (+{len(apis) - 4} more)" if len(apis) > 4 else "")
        )
    leads = con.execute(
        "SELECT to_route, trigger FROM transitions WHERE from_route=? ORDER BY ts DESC LIMIT 5", (r,)
    ).fetchall()
    if leads:
        out.append(
            "LEADS TO: " + "; ".join(f"{x['to_route']}" + (f" ({x['trigger']})" if x["trigger"] else "") for x in leads)
        )
    entry = con.execute(
        "SELECT from_route, trigger, sample_path FROM transitions WHERE to_route=? ORDER BY ts DESC LIMIT 1", (r,)
    ).fetchone()
    if entry:
        out.append(
            f"ENTRY: from {entry['from_route']} ("
            + "; ".join(
                x for x in (entry["trigger"], f"e.g. {entry['sample_path']}" if entry["sample_path"] else "") if x
            )
            + ")"
        )
    recurring = _patterns(con)[:3]
    if recurring:
        out += ["RECURRING APP PATTERNS (this app broke this way on 2+ pages: try it here too):"] + [
            _pattern_line(g) for g in recurring
        ]
    return "\n".join(x for x in out if x)


# heuristics that try to break a rule: only these clear a code-map rule from UNTESTED TARGETS (a happy-path save of a
# valid value proves nothing about the rule)
ADVERSARIAL = {
    "boundary",
    "empty-null",
    "invalid-format",
    "unicode-whitespace",
    "duplicate",
    "sequence-repeat",
    "error-handling",
    "permissions",
}


def _visited(con, pg):
    """When the page was last visited: last_verified, else (build never read) its last run's time."""
    if pg["last_verified"]:
        return pg["last_verified"]
    times = [
        ts
        for target, ts in con.execute("SELECT target, ts FROM runs WHERE ts IS NOT NULL")
        if core.norm_route(target) == pg["route"]
    ]
    return max(times, default=None)


def _changed_here(con, route, pg):
    """(files changed since the last visit, labels of the code-map rules and endpoints defined in them)."""
    ts = _visited(con, pg)
    if not ts or not con.execute("SELECT 1 FROM codemap WHERE page=? AND src != '' LIMIT 1", (route,)).fetchone():
        return [], set()
    files = _source_changes(con, {route: ts})[0].get(route, [])
    labels = {
        x["label"] for x in codemap._codemap_rows(con, route) if x["src"] in files and x["kind"] in ("rule", "api")
    }
    return files, labels


def _field_words(label):
    """A rule label's words, camelCase split ('POST /api/zones speedLimit' -> post api zones speed limit)."""
    return set(core.norm(re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", re.sub(r"\s\(\d+\)$", "", label))).split())


def _unexercised_apis(con, route, build, bound=None):
    """['METHOD /path'] the page's code maps (a frontend `api` row, its module and shell routes included) that no run
    observed on `build` (writes first). Empty for a page with no observed call on this build at all: without a request
    log nothing is known, which isn't a gap."""
    group = codemap.page_group(con, route, bound)
    mapped = codemap._group_calls(con, group)
    if not mapped or not build:
        return []
    seen = {
        codemap._api_ref(f"{m} {p}")
        for m, p in con.execute(
            f"SELECT DISTINCT method, path FROM observed_endpoints WHERE route IN ({','.join('?' * len(group))}) AND {ON_BUILD}",
            (*sorted(group), build, _week_ago()),
        )
    }
    todo = sorted(
        (c for c in mapped if seen and not any(codemap._same_call(c, o) for o in seen)),
        key=lambda c: (c[0] not in codemap.WRITE_METHODS, c[1], c[0]),
    )
    return [f"{m} /{p}" if m else f"/{p}" for m, p in todo]


def _due_states(con, route, build):
    """The page's in-page states not opened on `build` (newest first): what the card's UNVISITED STATES and `gaps` count."""
    return [
        x
        for x in con.execute(
            "SELECT state_key, trigger, status, build FROM page_states WHERE page=? ORDER BY ts DESC", (route,)
        )
        if x["status"] != "visited" or x["build"] != build
    ]


_NUM = r"(-?\d+(?:\.\d+)?)"
_FLAGS = {
    "required": r"\b(required|mandatory|not\s?blank|not\s?empty|non-?empty)\b",
    "integer": r"\b(integer|int|whole number)\b",
    "trim": r"\btrim",
    "pattern": r"\b(pattern|regex|regexp|email|uuid|url)\b",
}


def _rule_traits(value):
    """What a rule value says, for comparing a client and a server rule: bounds {min, max} (also minLength / maxLength and
    `0-130`), flags (required, integer, trim, pattern), the options of a `one of: a, b` list, and its other numbers."""
    v = re.sub(r"\s*\([^()]*\)\s*$", "", str(value or "").lower())  # a trailing note: (Joi), (react-hook-form register)
    bounds = {}
    for k in ("min", "max"):
        m = re.search(rf"\b{k}(?:imum)?(?:\s?length)?\b\s*[:=]?\s*{_NUM}", v)
        if m:
            bounds[k] = float(m.group(1))
    span = re.search(rf"(?<![\w.-]){_NUM}\s*(?:-|–|to)\s*{_NUM}(?![\w.-])", v)
    if not bounds and span:
        bounds = {"min": float(span.group(1)), "max": float(span.group(2))}
    enum = re.search(r"\b(?:one of|oneof|enum|valid|allowed)\b\W*(.+)", v)
    options = (
        list(dict.fromkeys(x.strip(" '\"`[]()") for x in re.split(r"[,|/]", enum.group(1)) if x.strip(" '\"`[]()")))
        if enum
        else []
    )
    return {
        "bounds": bounds,
        "nums": set(re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", v)),
        "text": v.strip(),
        "enum": options,
        "flags": {k for k, rx in _FLAGS.items() if re.search(rx, v)},
    }


def _only(mine, other):
    """What this rule states that the other lacks: flags, a bound the other has no word for, `one of` options. A server
    `min 1` beside a client `required` (or the reverse) and a lone `min 0` say the same as nothing."""
    out = sorted(mine["flags"] - other["flags"])
    for k, n in mine["bounds"].items():
        if k in other["bounds"] or (k == "min" and (n == 0 or (n == 1 and "required" in other["flags"]))):
            continue
        out.append(f"{k} {int(n) if n == int(n) else n}")
    return out + (["one of " + ", ".join(mine["enum"])] if mine["enum"] and not other["enum"] else [])


def _rule_pair_hint(c, x, who=("client", "server")):
    """The ways two rules of one field disagree (`who` names the sides), as text parts (none: they agree)."""
    a, b = _rule_traits(c["value"]), _rule_traits(x["value"])
    both = [k for k in ("min", "max") if k in a["bounds"] and k in b["bounds"] and a["bounds"][k] != b["bounds"][k]]
    n = lambda t, k: f"{k} {int(t['bounds'][k]) if t['bounds'][k] == int(t['bounds'][k]) else t['bounds'][k]}"
    parts = []
    if both:
        parts.append(
            f"{who[0]} " + ", ".join(n(a, k) for k in both) + f" vs {who[1]} " + ", ".join(n(b, k) for k in both)
        )
    elif (
        not a["bounds"] and not b["bounds"] and a["nums"] and b["nums"] and a["nums"] != b["nums"]
    ):  # numbers, no min/max word
        parts.append(f"{who[0]} {core.short(a['text'], 40)} vs {who[1]} {core.short(b['text'], 40)}")
    if a["enum"] and b["enum"] and set(a["enum"]) != set(b["enum"]):
        parts.append(f"options {who[0]} {', '.join(a['enum'])} vs {who[1]} {', '.join(b['enum'])}")
    parts += [
        f"{side} only: {', '.join(items)}" for side, items in ((who[0], _only(a, b)), (who[1], _only(b, a))) if items
    ]
    return parts


def _client_server_differ(con, route):
    """Hints where a page rule and a linked server rule on the same field name (camelCase or not) don't agree, one per
    pair: a limit with other numbers (negative ones too), different `one of` options, a trait or bound one side lacks
    (required, integer, trim, a pattern). Then a page rule no server rule matches (only when the backend maps the page's
    write endpoint) and a server rule the page has no rule for (only when the page states rules). Hints, not oracles."""
    rows = codemap._codemap_rows(con, route)
    client = [x for x in rows if x["kind"] == "rule" and x["page"] != "*"]
    server = [x for x in rows if x["kind"] == "rule" and x["page"] == "*"]
    writes = [
        x["label"]
        for x in rows
        if x["kind"] == "api" and x["page"] == "*" and re.match(r"(?i)(post|put|patch)\b", x["label"])
    ]
    matches = lambda c, x: bool(_field_words(c["label"])) and _field_words(c["label"]) <= _field_words(x["label"])
    out = []
    for c in client:
        hits = [x for x in server if matches(c, x)]
        for x in hits:
            if parts := _rule_pair_hint(c, x):
                out.append(f"{c['label']}: " + "; ".join(parts) + (f" [{x['label']}]" if len(hits) > 1 else ""))
    out.sort(key=lambda t: "vs server" not in t)  # a limit that differs first, then what one side lacks
    if writes:  # the backend maps the endpoint this page writes to: a page rule it has no rule for
        out += [
            f"{c['label']}: client only ({core.short(_rule_traits(c['value'])['text'], 40)}), no server rule on {writes[0]}"
            for c in client
            if not any(matches(c, x) for x in server)
        ]
    if client:  # the page states rules: a server rule it has none for
        out += [
            f"{x['label']}: server only ({core.short(_rule_traits(x['value'])['text'], 40)}), the page has no rule for it"
            for x in server
            if not any(matches(c, x) for c in client)
        ]
    return list(dict.fromkeys(out))


def _cross_page_rules_differ(con, route):
    """Hints where a page rule and the rule of the same field (same words, camelCase or not) on a sibling page that
    writes the same resource (create and edit) don't agree: another limit or `one of` options, a trait or bound only
    one page states. Only the pages' own rules, not the backend's. Hints to verify on both pages, not oracles."""
    own = lambda r: [x for x in codemap._codemap_rows(con, r) if x["kind"] == "rule" and x["page"] != "*"]
    mine = own(route)
    out = []
    for sib in codemap.resource_neighbors(con, route)["siblings"][:5] if mine else []:
        for c in mine:
            words = _field_words(c["label"])
            for x in own(sib["route"]) if words else []:
                if words == _field_words(x["label"]) and (parts := _rule_pair_hint(c, x, ("here", sib["route"]))):
                    out.append(f"{c['label']}: " + "; ".join(parts))
    out.sort(key=lambda t: " vs " not in t)  # a limit that differs first, then what one page lacks
    return list(dict.fromkeys(out))


ROLE_TEXT = {"siblings": "also writes", "consumers": "reads", "producers": "writes"}


def _related(con, route):
    """[(route, why)] for the pages a change on `route` may reach, the nearest first: the pages writing the same
    resource, the pages reading what it writes, the pages writing what it reads, the pages sharing a component file."""
    links = codemap.resource_neighbors(con, route)
    found = {}
    for kind in ("siblings", "consumers", "producers"):
        for x in links[kind]:
            found.setdefault(x["route"], f"{ROLE_TEXT[kind]} {x['why']}")
    for x in codemap.component_neighbors(con, route):
        found.setdefault(x["route"], f"shares {x['why']}")
    return list(found.items())


# coverage that counts on a build (params: build, a week ago): runs that couldn't read the build count for a week
ON_BUILD = "((build=? AND build!='unknown') OR (build='unknown' AND ts >= ?))"


def _week_ago():
    return datetime.fromtimestamp(time.time() - 7 * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rechecked(con, route, build):
    """Hashes of the page's bugs some run re-checked on `build` (coverage `regression` with the hash as target)."""
    return {
        r[0]
        for r in con.execute(
            f"SELECT lower(trim(target)) FROM coverage WHERE page=? AND heuristic='other:regression' "
            f"AND result!='blocked' AND {ON_BUILD}",
            (route, build, _week_ago()),
        )
    }


def _near_pages(con, route):
    """The pages a bug without an endpoint may also show on: those sharing a call, and those sharing a component file."""
    return sorted(
        set(codemap.sharing_pages(con, route)) | {x["route"] for x in codemap.component_neighbors(con, route)}
    )


def _shared_bugs(con, route, limit=3):
    """Bugs on other pages that this page may show too: bugs recorded with an endpoint this page calls (any number of
    callers: only bugs of that endpoint match), and for bugs without one the pages sharing a call or a component file."""
    mine = codemap._group_calls(con, codemap.page_group(con, route)) if route != "*" else set()
    found = {}
    cols = f"{BUG_COLS},endpoint,last_seen"
    if mine:
        for x in con.execute(
            f"SELECT {cols} FROM bugs WHERE endpoint IS NOT NULL AND page!=? AND status IN (?,?,?,'FIXED')",
            (route, *core.VISIBLE),
        ):
            if any(codemap._same_call(_endpoint_ref(x["endpoint"]), c) for c in mine):
                found[x["hash"]] = x
    sharing = _near_pages(con, route)
    if sharing:
        for x in con.execute(
            f"SELECT {cols} FROM bugs WHERE endpoint IS NULL AND status IN (?,?,?,'FIXED') AND page IN "
            f"({','.join('?' * len(sharing))})",
            [*core.VISIBLE, *sharing],
        ):
            found[x["hash"]] = x
    return sorted(found.values(), key=lambda x: x["last_seen"] or "", reverse=True)[:limit]


def _patterns(con, min_pages=2):
    """Bugs of one kind (their pattern tag, else the heuristic that found them) on min_pages+ pages, most pages first.
    INVALID bugs are never a pattern; a page counts when the bug was found or sighted there."""
    seen = {}
    for h, page in con.execute("SELECT hash, page FROM sightings WHERE page IS NOT NULL"):
        seen.setdefault(h, set()).add(page)
    groups = {}
    for x in con.execute(
        "SELECT hash, page, title, status, heuristic, pattern FROM bugs WHERE status != 'INVALID' "
        "AND (pattern IS NOT NULL OR heuristic IS NOT NULL) ORDER BY last_seen DESC"
    ):
        g = groups.setdefault(
            x["pattern"] or x["heuristic"],
            {"pattern": x["pattern"], "heuristics": [], "bugs": [], "pages": set(), "regressed": 0},
        )
        g["bugs"].append(x)
        g["pages"] |= {x["page"]} | seen.get(x["hash"], set())
        g["regressed"] += x["status"] == "REGRESSED"
        if x["heuristic"]:
            g["heuristics"].append(x["heuristic"])
    out = []
    for key, g in groups.items():
        if len(g["pages"]) >= min_pages:
            heur = max(set(g["heuristics"]), key=g["heuristics"].count) if g["heuristics"] else None
            out.append(
                {
                    "pattern": key,
                    "heuristic": heur if g["pattern"] else None,
                    "bugs": len(g["bugs"]),
                    "pages": len(g["pages"]),
                    "regressed": g["regressed"],
                    "examples": [f"{x['hash']} {x['title']}" for x in g["bugs"][:2]],
                }
            )
    return sorted(out, key=lambda g: (-g["pages"], -g["bugs"], g["pattern"]))


def _pattern_line(g):
    return (
        f"- {g['pattern']}"
        + (f" ({g['heuristic']})" if g["heuristic"] else "")
        + f": {g['bugs']} bugs on {g['pages']} pages"
        + (f" ({g['regressed']} regressed)" if g["regressed"] else "")
        + f", e.g. {core.short(g['examples'][0], 80)}"
    )


def patterns(con, min_pages=2, as_json=False):
    rows = _patterns(con, min_pages)
    if as_json:
        return json.dumps(rows, indent=1)
    return (
        "\n".join(
            [f"RECURRING PATTERNS (one kind of bug on {min_pages}+ pages; most pages first):"]
            + [_pattern_line(g) for g in rows]
        )
        if rows
        else f"no bug pattern on {min_pages}+ pages yet"
    )


def _coverage(con, route, build):
    """({heuristic: ['target:result', ...]}, heuristics tried, normalised targets checked) on `build`. Coverage from
    runs that couldn't read the build counts for a week, then it is retried."""
    cov, targets = {}, set()
    for c in con.execute(
        f"SELECT heuristic, target, result FROM coverage WHERE page=? AND {ON_BUILD} "
        "ORDER BY heuristic, result='pass', ts DESC",
        (route, build, _week_ago()),
    ):  # failures first
        cov.setdefault(c["heuristic"], []).append(f"{core.short(c['target'], 40)}:{core.short(c['result'], 12)}")
        if c["result"] != "blocked" and c["heuristic"] in ADVERSARIAL:
            targets.add(core.norm(c["target"]))
    # A heuristic only blocked (e.g. needs --faults) is still untried: the next run may be able to do it.
    tried = {h for h, t in cov.items() if any(not x.endswith(":blocked") for x in t)}
    return cov, tried, targets


def _untested_rules(con, route, targets):
    """Labels of the page's code-map rules, then the linked server rules ('[server] <label>'), that no adversarial check
    named: target 'Speed limit field' covers rule 'speed', target 'date' doesn't cover 'start date'. A check of a server
    rule ('[server] POST /api/zones speed') doesn't cover the page's own field of that name."""
    out, seen = [], set()
    rows = [(x["label"], x["page"] == "*") for x in codemap._codemap_rows(con, route) if x["kind"] == "rule"]
    labels = [
        (re.sub(r"\s\(\d+\)$", "", label), server) for label, server in rows
    ]  # apply_codemap's 'speed (2)': one field
    server_words = {core.norm(label) for label, server in labels if server}
    for label, server in sorted(labels, key=lambda t: (t[1], t[0])):
        words = core.norm(label)

        def covers(t, server=server, words=words):
            if server:
                return f" {words} " in f" {t} "
            rest = f" {t} "
            for w in sorted(server_words - {words}, key=len, reverse=True):  # a longer server label holding these words
                if words in w:
                    rest = rest.replace(f" {w} ", " ")
            return not t.startswith("server ") and f" {words} " in rest

        if words and (server, words) not in seen and not any(covers(t) for t in targets):
            out.append(f"[server] {label}" if server else label)
        seen.add((server, words))
    return out


BUG_COLS = "hash,page,severity,status,title,symptom,seen_count,jira_key"


def _bug_line(x, with_page, n=100):
    jk = f" {x['jira_key']}" if x["jira_key"] else ""
    return (
        f"- {x['hash']} {x['status']}{jk} [{x['severity'] or '?'}] "
        + (f"{core.short(x['page'], n)} " if with_page else "")
        + f"{core.short(x['title'], n)} | symptom: {core.short(x['symptom'] or x['title'], n + 20)} (seen {x['seen_count']})"
    )


def bug_lines(con, route, build=None):
    """The bug blocks of a card (route) or of a first visit (route None). A FIXED bug some run re-checked on `build`
    (coverage `regression` with its hash as the target) says so: the explorer re-checks the others."""
    q = f"SELECT {BUG_COLS} FROM bugs WHERE status IN (?,?,?)"
    args = list(core.VISIBLE)
    if route is not None:  # also a bug from another page that a run saw here (a shared endpoint)
        q += " AND (page=? OR hash IN (SELECT hash FROM sightings WHERE page=?))"
        args += [route, route]
    rows = con.execute(q + " ORDER BY last_seen DESC LIMIT 10", args).fetchall()
    fixed = con.execute(
        f"SELECT {BUG_COLS} FROM bugs WHERE status='FIXED'"
        + (" AND page=? ORDER BY last_seen DESC" if route is not None else " ORDER BY last_seen DESC LIMIT 5"),
        [route] if route is not None else [],
    ).fetchall()
    line = lambda x: _bug_line(x, route is None)  # app-wide list: say which page
    out = []
    if rows:
        out += ["KNOWN BUGS (don't re-draft; to report a sighting put its hash in run.json):"] + [line(x) for x in rows]
    done = _rechecked(con, route, build) if route is not None else set()
    # the ones still to re-check first: a bug fixed today keeps its old last_seen, and gaps counts it
    fixed = sorted(fixed, key=lambda x: x["hash"] in done)
    more, fixed = len(fixed) - 5, fixed[:5]
    if fixed:
        on = f" on build {build}" if build and build != core.UNKNOWN else ""
        out += ["PREVIOUSLY FIXED (seen again = regression: put its hash in run.json):"] + [
            line(x) + (f" | re-checked{on}" if x["hash"] in done else "") for x in fixed
        ]
        if more > 0:
            out.append(f"(+{more} more FIXED: qa-mem bugs FIXED)")
    invalid = con.execute(
        f"SELECT {BUG_COLS} FROM bugs WHERE status='INVALID'"
        + (" AND page=?" if route is not None else "")
        + " ORDER BY last_seen DESC LIMIT 5",
        [route] if route is not None else [],
    ).fetchall()
    if invalid:
        out += ["NOT BUGS (the user marked these INVALID: don't report them):"] + [line(x) for x in invalid]
    return "\n".join(out)


def _fts_query(groups, nots):
    """An FTS5 query: any group of words, every word of a group (each a quoted string, so typed operators are words)."""
    body = " OR ".join("(" + " ".join(f'"{w}"' for w in g) + ")" for g in groups)
    return (f"({body})" if len(groups) > 1 else body) + "".join(f' NOT "{p}"' for p in nots)


def search(con, q):
    """Plain words, all required, stemmed ('date' finds 'dates'); `-word` excludes. Facts, bugs and the code map. When
    nothing matches all of 3+ words, the facts and bugs matching any 2 of them are shown, marked relaxed. Built here as an
    FTS query, so FTS operators typed in the terms (NEAR, AND, a leading -) can't change what is matched."""
    terms = [t for t in str(q).split() if t not in ("AND", "OR", "NOT", "NEAR") and re.search(r"\w", t)]
    pos, neg = [t for t in terms if not t.startswith("-")], [t[1:] for t in terms if t.startswith("-")]
    # '/api/zones', 'speed-limit', 'form.html': every word in a term counts; multi-word exclusions match as one phrase
    words = list(dict.fromkeys(w for t in pos for w in re.findall(r"\w+", t)))[:8]
    nots = [" ".join(ws) for t in neg if (ws := re.findall(r"\w+", t))]
    found = lambda groups: (_search(con, _fts_query(groups, nots)), _search_bugs(con, _fts_query(groups, nots)))
    rows, bug_rows = found([words]) if words else ([], [])
    relaxed = not rows and not bug_rows and len(words) >= 3
    if relaxed:
        rows, bug_rows = found(list(itertools.combinations(words, 2)))
    lines = [f"{x['page']} [{x['kind']}] {x['text']}" + (" *" if x["stale"] else "") for x in rows]
    lines += [
        f"{x['page']} [bug:{x['status']}{' ' + x['jira_key'] if x['jira_key'] else ''}] {x['title']} | {x['hash']}"
        for x in bug_rows
    ]
    if relaxed and lines:
        lines.insert(0, f"(relaxed: nothing matched all of '{' '.join(words)}'; these match at least 2 of the words)")
    if words:  # the code map too (incl. backend endpoints no page calls, page '*')
        cond = " AND ".join(
            ["(label || ' ' || coalesce(value, '')) LIKE ? ESCAPE '\\'"] * len(pos)
            + ["(label || ' ' || coalesce(value, '')) NOT LIKE ? ESCAPE '\\'"] * len(neg)
        )
        like = lambda t: (
            "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )  # the term as typed
        esc = [like(t) for t in pos + neg]
        lines += [
            f"{x['page']} [code:{x['kind']}] {x['label']} = {x['value']}"
            for x in con.execute(
                f"SELECT page,kind,label,value FROM codemap WHERE {cond} ORDER BY page='*', kind='route' DESC, page LIMIT 10",
                esc,
            )
        ]
    return "\n".join(lines) or f"no facts match '{q}'"


STOP_WORDS = set(
    """the and for with without from into onto that this these those are was were been being have has had not
can could should would must will when then than but its it's their there here what which who how why all any each per via
also only just more most less such page pages field fields value values show shown shows user users click button new fix
fixed issue ticket story bug bugs test tests when after before while""".split()
)


def ticket_context(con, text, n=8):
    """What memory knows that a ticket's text points at: {terms, candidate_routes: [{route, name, why}], impacted_routes
    (the pages the top candidates reach, at most 6), relevant_facts, relevant_bugs (status kept)}. Routes the text names
    count most, then routes whose facts, bugs or code-map rows match its words (stemmed); capped, so a plan starts from
    a short list instead of every route and bug."""
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", str(text))]
    counts = {}
    for w in words:
        if w not in STOP_WORDS:
            counts[w] = counts.get(w, 0) + 1
    terms = sorted(counts, key=lambda w: -counts[w])[:12]
    names = {x["route"]: x["name"] or "" for x in con.execute("SELECT route, name FROM pages WHERE route != '*'")}
    named = {core.norm_route(m) for m in re.findall(r"(?<![\w.])/[\w#!/.-]+", str(text))} & set(names)
    pairs = _fts_query(list(itertools.combinations(terms, 2)), []) if len(terms) > 1 else None
    facts, bug_rows = (_search(con, pairs), _search_bugs(con, pairs)) if pairs else ([], [])  # 2+ of the ticket's words
    score, why = {}, {}
    shell_of = {m: s for s, (_, m) in codemap.module_bindings(con).items()}  # a module page counts for its shell route

    def add(route, points, reason):
        route = shell_of.get(route, route)
        if route in names:
            score[route] = score.get(route, 0) + points
            why.setdefault(route, [])
            if reason not in why[route]:
                why[route].append(reason)

    for r in named:
        add(r, 10, "named in the ticket")
    for f in facts:
        add(f["page"], 2, "facts")
    for b in bug_rows:
        add(b["page"], 2, "bugs")
    stems = {t.rstrip("s") for t in terms}
    for row in con.execute("SELECT page, label, coalesce(value, '') AS value FROM codemap WHERE page != '*'"):
        if {w.rstrip("s") for w in core.norm(row["label"] + " " + row["value"]).split()} & stems:
            add(row["page"], 1, "code map")
    ranked = sorted(score, key=lambda r: (-score[r], r))[:n]
    impacted = {}  # what the top candidates reach: sibling writers, readers, shared components (one hop)
    for r in ranked[:3]:
        for p, reason in _related(con, r):
            if p not in ranked and p in names:
                impacted.setdefault(p, f"{reason} (via {r})")
    if terms and ranked:  # on the routes the ticket points at, one of its words is enough
        one = _fts_query([[w] for w in terms], [])
        top = ranked[:3]
        facts += [x for x in _search(con, one) if x["page"] in top and x["id"] not in {f["id"] for f in facts}]
        bug_rows += [
            x for x in _search_bugs(con, one) if x["page"] in top and x["hash"] not in {b["hash"] for b in bug_rows}
        ]
    keys = {k.upper() for k in re.findall(r"\b[A-Z][A-Z0-9]+-\d+\b", str(text))}
    decisions = [
        x
        for x in con.execute(
            "SELECT text, answer, ticket, page FROM questions WHERE status='resolved' ORDER BY last_seen DESC LIMIT 200"
        )
        if (x["ticket"] or "").upper() in keys
        or len({w.rstrip("s") for w in core.norm(x["text"]).split()} & stems) >= 2
    ][:10]
    return {
        "terms": terms,
        "patterns": [_pattern_line(g) for g in _patterns(con)[:3]],
        "decisions": [
            f"{x['text']} → {x['answer'] or '(answered)'}" + (f" ({x['ticket']})" if x["ticket"] else "")
            for x in decisions
        ],
        "candidate_routes": [{"route": r, "name": names[r], "why": ", ".join(why[r])} for r in ranked],
        "impacted_routes": [{"route": p, "name": names[p], "why": reason} for p, reason in list(impacted.items())[:6]],
        "relevant_facts": [f"{x['page']} [{x['kind']}] {x['text']}" + (" *" if x["stale"] else "") for x in facts[:15]],
        "relevant_bugs": [_bug_line(x, True) for x in bug_rows[:15]],
    }


IMPACT_TITLES = (
    ("resource_siblings", "SIBLING WRITERS (write the same resource: compare their rules)"),
    ("resource_consumers", "CONSUMERS (read what this writes: check they show it right)"),
    ("resource_producers", "PRODUCERS (write what this reads: their bugs show here)"),
    ("shared_components", "SHARED COMPONENTS (one file renders them: a bug there shows on each)"),
)


def impact(con, target, as_json=False):
    """The pages a change reaches, from the code map: for a route or page name, the module it mounts, the pages
    writing the same resource, reading what it writes, writing what it reads and sharing a component file, and the
    rules of a field that differ between sibling writers; for `METHOD /path`, the pages calling it and the same around
    its resource. Hints from source, never proof."""
    target = " ".join(str(target).split())
    if re.match(rf"(?i)({codemap.HTTP_METHODS})\s", target):
        ref = codemap._api_ref(target)
        if not ref[1]:
            raise core.BadInput(f"impact: no path in {target!r}: use METHOD /path (POST /api/zones)")
        calling = codemap.callers(con, ref)
        links = codemap.resource_links(con, {ref}, calling)
        data = {
            "endpoint": f"{ref[0]} {ref[1]}".strip(),
            "callers": codemap.shown(con, calling),
            "resource_siblings": links["siblings"],
            "resource_consumers": links["consumers"],
            "resource_producers": links["producers"],
        }
        head = f"IMPACT of {data['endpoint']} (callers: {', '.join(data['callers']) or 'no page'}; hints from the code map, not proof):"
    else:
        pg = codemap.find_page(con, target)
        if not pg:
            return f"no page matches '{target}': qa-mem code lists the mapped pages (or give METHOD /path)"
        route = pg["route"]
        links, bound = codemap.resource_neighbors(con, route), codemap.module_bindings(con).get(route)
        data = {
            "route": route,
            "module": f"{bound[0] or '?'} at {bound[1]}" if bound else None,
            "resource_siblings": links["siblings"],
            "resource_consumers": links["consumers"],
            "resource_producers": links["producers"],
            "shared_components": codemap.component_neighbors(con, route),
            "cross_page_rules": _cross_page_rules_differ(con, route),
        }
        head = f"IMPACT of {route} (hints from the code map, not proof):"
    if as_json:
        return json.dumps(data, indent=1)
    out = [head] + ([f"MODULE: {data['module']}"] if data.get("module") else [])
    for key, title in IMPACT_TITLES:
        if data.get(key):
            out.append(f"{title}: " + "; ".join(f"{x['route']} ({x['why']})" for x in data[key]))
    if data.get("cross_page_rules"):
        out.append(
            "CROSS-PAGE RULES DIFFER (a hint to verify on both pages, not an oracle): "
            + "; ".join(data["cross_page_rules"])
        )
    return (
        "\n".join(out) if len(out) > 1 else head + " no related pages (the code map has none for it: /qa-sdet:qa-init)"
    )


def _stems(text):
    """Words of a symptom or title, crudely stemmed ('saves', 'saved', 'save' -> 'sav'), minus filler."""
    out = set()
    for w in core.norm(text).split():
        if len(w) < 3 or w in STOP_WORDS:
            continue
        for suffix in ("ing", "ed", "es", "s"):
            if w.endswith(suffix) and len(w) - len(suffix) >= 3:
                w = w[: -len(suffix)]
                break
        out.add(w[:-1] if w.endswith("e") and len(w) > 3 else w)
    return out


MATCH_ADVICE = {
    "INVALID": "the user marked it NOT A BUG: don't report it",
    "FIXED": "fixed before: if this is it, it's a regression: report its hash with a short draft",
}


def bug_matches(con, route, symptom, endpoint=None):
    """[(score, bug row)] best first: bugs on this page, on pages sharing its calls, sighted here, or recorded on the same
    endpoint, whose title and symptom share words with this symptom (any status). Reads only."""
    page, words = core.norm_route(route), _stems(symptom)
    ep = codemap._api_ref(endpoint) if endpoint else ("", "")
    near = {page} | set(_near_pages(con, page))
    sighted = {h for (h,) in con.execute("SELECT hash FROM sightings WHERE page=?", (page,))}
    scored = []
    for x in con.execute(f"SELECT {BUG_COLS},endpoint FROM bugs"):
        theirs = _stems(f"{x['title']} {x['symptom'] or ''}")
        shared = len(words & theirs)
        same_ep = bool(ep[1] and x["endpoint"] and codemap._same_call(_endpoint_ref(x["endpoint"]), ep))
        if not shared or not (same_ep or x["page"] in near or x["hash"] in sighted):
            continue  # same words on a page that shares nothing with this one: another bug
        score = shared / len(words | theirs) + 0.5 * same_ep + 0.1 * (x["page"] == page)
        if score >= 0.5 or shared >= 2:
            scored.append((score, x))
    scored.sort(key=lambda t: -t[0])
    return scored


def bug_match(con, route, symptom, endpoint=None, limit=3):
    """Likely duplicates of a bug about to be drafted (bug_matches, any status, so a NOT A BUG and a fixed one are named
    too). Candidates for the explorer to judge, never merged by themselves."""
    scored = bug_matches(con, route, symptom, endpoint)
    if not scored:
        return "no likely duplicate on this page, the pages sharing its calls, or that endpoint: draft it as new"
    return "\n".join(
        ["LIKELY DUPLICATES (judge each; the same bug = report a sighting with its hash, no new draft):"]
        + [
            f"- {x['hash']} {x['status']}"
            + (f" {x['jira_key']}" if x["jira_key"] else "")
            + f" [{x['severity'] or '?'}] "
            f"{x['page']} {core.short(x['title'], 80)}"
            + (f" | endpoint {x['endpoint']}" if x["endpoint"] else "")
            + f": {MATCH_ADVICE.get(x['status'], 'known: report a sighting with its hash')}"
            for _, x in scored[:limit]
        ]
    )


def questions(con, status="open"):
    rows = con.execute(
        "SELECT * FROM questions" + ("" if status == "all" else " WHERE status=?") + " ORDER BY last_seen DESC "
        "LIMIT 50",
        () if status == "all" else (status,),
    ).fetchall()
    return (
        "\n".join(
            f"#{x['id']} [{x['status']}] {x['page']}"
            + (f" ({x['ticket']})" if x["ticket"] else "")
            + f" {x['text']}"
            + (f" → {x['answer']}" if x["answer"] else "")
            for x in rows
        )
        or f"no {status} questions"
    )


def answer(con, qid, text):
    """Resolve question #qid with the user's answer (scrubbed): it shows as a DECISION on cards. False: no such question."""
    with con:
        n = con.execute(
            "UPDATE questions SET status='resolved', answer=?, last_seen=? WHERE id=?",
            (core.cap(scrub.redact(text)), core.now(), qid),
        ).rowcount
    return n == 1


FACT_KINDS = ("rule", "flow", "api", "validation", "note")


def remember(con, route, kind, text, pin=True, source="user"):
    """A fact the user states (scrubbed). Pinned by default: a new build doesn't make it stale and the per-page cap
    never drops it. route '*' is app-wide (shown on every card while pinned). Returns the fact's id."""
    if kind not in FACT_KINDS:
        raise core.BadInput(f"remember: kind must be one of {', '.join(FACT_KINDS)}")
    page = "*" if route.strip() == "*" else core.norm_route(route)
    text = core.cap(scrub.redact(text))
    if not page or not text:
        raise core.BadInput("remember: give a route (or *), a kind and the fact")
    with con:
        if page != "*":
            con.execute("INSERT OR IGNORE INTO pages(route) VALUES(?)", (page,))
        con.execute(
            "INSERT INTO facts(page,kind,text,source,build,ts,stale,pinned) VALUES(?,?,?,?,?,?,0,?) "
            "ON CONFLICT(page,kind,text) DO UPDATE SET pinned=excluded.pinned, stale=0, ts=excluded.ts, "
            "source=excluded.source",
            (page, kind, text, source, core.meta(con, "build"), core.now(), int(pin)),
        )
    return con.execute("SELECT id FROM facts WHERE page=? AND kind=? AND text=?", (page, kind, text)).fetchone()[0]


def forget_fact(con, fid):
    with con:
        row = con.execute("SELECT page, kind, text FROM facts WHERE id=?", (fid,)).fetchone()
        con.execute("DELETE FROM facts WHERE id=?", (fid,))
    return row


def forget_page(con, route, force=False):
    """Retire a page (a typo or a removed route): its facts, locators, coverage, states, transitions (to and from it),
    signals, observed endpoints, questions and the page; bugs stay as history.
    Refused while it has open bugs, unless force. Returns (removed, reason)."""
    page = core.norm_route(route)
    open_bugs = con.execute(
        "SELECT count(*) FROM bugs WHERE page=? AND status IN (?,?,?)", (page, *core.VISIBLE)
    ).fetchone()[0]
    if open_bugs and not force:
        return (
            False,
            f"{page} has {open_bugs} open bug(s); triage them first (qa-mem bug-status) or pass --force (bugs are kept)",
        )
    if not con.execute("SELECT 1 FROM pages WHERE route=?", (page,)).fetchone():
        return False, f"no page {page}"
    with con:
        for table in ("facts", "locators", "coverage", "page_states", "signals", "questions"):
            con.execute(f"DELETE FROM {table} WHERE page=?", (page,))
        con.execute("DELETE FROM transitions WHERE from_route=? OR to_route=?", (page, page))
        con.execute("DELETE FROM observed_endpoints WHERE route=?", (page,))
        con.execute("DELETE FROM blind_spots WHERE page=?", (page,))
        con.execute("DELETE FROM pages WHERE route=?", (page,))
    return True, f"forgot {page} (its bugs are kept)"


def _search(con, q, page=None):
    return con.execute(
        "SELECT f.id,f.page,f.kind,f.text,f.stale FROM facts_fts JOIN facts f ON f.id=facts_fts.rowid "
        "WHERE facts_fts MATCH ?" + (" AND f.page=?" if page else "") + " ORDER BY rank LIMIT 20",
        (q, page) if page else (q,),
    ).fetchall()


def _search_bugs(con, q):
    cols = ",".join(f"b.{c}" for c in BUG_COLS.split(","))  # bugs_fts has columns of the same names
    return con.execute(
        f"SELECT {cols} FROM bugs_fts JOIN bugs b ON b.rowid=bugs_fts.rowid "
        "WHERE bugs_fts MATCH ? ORDER BY rank LIMIT 15",
        (q,),
    ).fetchall()


def bugs(con, status="OPEN"):
    total = con.execute("SELECT count(*) FROM bugs WHERE status=?", (status,)).fetchone()[0]
    rows = con.execute("SELECT * FROM bugs WHERE status=? ORDER BY last_seen DESC LIMIT 30", (status,)).fetchall()
    lines = [
        f"{x['hash']} {x['status']}"
        + (f" {x['jira_key']}" if x["jira_key"] else "")
        + f" [{x['severity'] or '?'}] {x['classification'] or '?'} {x['page']} {core.short(x['title'], 100)} "
        f"(seen {x['seen_count']}) {x['draft_path'] or ''}"
        for x in rows
    ]
    if total > len(rows):
        lines.append(f"... {total - len(rows)} older {status} bugs not shown (search facts or ask for a page's card)")
    return "\n".join(lines) or f"no {status} bugs"


DONE_STATUSES = {"done", "closed", "resolved", "verified", "released"}
REJECTED_RESOLUTIONS = {
    "won't do",
    "wont do",
    "won't fix",
    "wont fix",
    "duplicate",
    "cannot reproduce",
    "declined",
    "invalid",
    "not a bug",
    "rejected",
}


def bug_sync_jql(con):
    """Read-only JQL covering every FILED bug with a linked Jira key, or '' when none exist."""
    keys = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT jira_key FROM bugs WHERE status='FILED' AND jira_key IS NOT NULL AND jira_key!='' ORDER BY jira_key"
        )
    ]
    return f"key in ({', '.join(keys)})" if keys else ""


def _jira_str(v):
    return str(v.get("name") or v.get("value") or "") if isinstance(v, dict) else str(v or "")


def _view_resolution(key):
    """A single Done item's resolution via read-only `acli jira workitem view` (acli rejects `resolution` on `search`):
    '' when it has none, None when it couldn't be read (no acli, auth, timeout, odd output)."""
    import shutil
    import subprocess

    acli = shutil.which("acli")
    if not acli:
        return None
    try:
        r = subprocess.run(
            [acli, "jira", "workitem", "view", key, "--fields", "resolution", "--json"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
        )
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        if not isinstance(data, dict):
            return None
        fields = f if isinstance(f := data.get("fields"), dict) else {}
        return _jira_str(fields.get("resolution")).strip()
    except (OSError, subprocess.TimeoutExpired, ValueError, RecursionError):
        return None


def set_bug_status(con: sqlite3.Connection, prefix: str, status: str, jira_key: str | None = None) -> tuple[bool, str]:
    """(done, message): the one bug whose hash starts with `prefix` gets `status` (and `jira_key`). FIXED stamps the fix
    time and build, so a run that finished before it, ingested later, is no regression (bug_sync stamps the same)."""
    hits = [r[0] for r in con.execute("SELECT hash FROM bugs WHERE substr(hash, 1, ?)=?", (len(prefix), prefix))]
    if len(hits) != 1:  # an ambiguous prefix must not change several bugs at once
        return False, f"{len(hits)} bugs match '{prefix}'" + (
            ": " + ", ".join(hits[:10]) if hits else ""
        ) + "; give one hash"
    h = hits[0]
    with con:
        if jira_key is not None:
            con.execute("UPDATE bugs SET status=?, jira_key=? WHERE hash=?", (status, jira_key, h))
        else:
            con.execute("UPDATE bugs SET status=? WHERE hash=?", (status, h))
        if status == "FIXED":
            con.execute(
                "UPDATE bugs SET fixed_ts=?, fixed_build=? WHERE hash=?", (core.now(), core.meta(con, "build"), h)
            )
        # reopened, but its run files no longer list it (they were rewritten without it): it is no bug to track
        gone = con.execute(
            "DELETE FROM bugs WHERE hash=? AND status='OPEN' AND seen_count=0 AND NOT EXISTS "
            "(SELECT 1 FROM sightings WHERE hash=?)",
            (h, h),
        ).rowcount
    if gone:
        return True, f"removed {h}: no run lists this bug any more"
    return True, f"updated {h} -> {status}" + (f" ({jira_key})" if jira_key else "")


def set_build(con: sqlite3.Connection, build: str) -> None:
    """`build` is the deployed build: what pages of other builds verified is stale (the user's pinned facts stay), and
    what this build verified is current again (a corrected build: stale 1.44 then stale 1.43). _store does the same per
    page when a run reports a new build."""
    with con:
        core.set_meta(con, "build", build)
        core.set_meta(con, "build_ts", core.now())
        for table in ("facts", "locators"):
            con.execute(
                f"UPDATE {table} SET stale=1 WHERE page IN (SELECT route FROM pages WHERE build!=?)"
                + (" AND pinned=0" if table == "facts" else ""),
                (build,),
            )
            con.execute(
                f"UPDATE {table} SET stale=0 WHERE build=? AND page IN (SELECT route FROM pages WHERE build=?)",
                (build, build),
            )


def bug_sync(con, data):
    """Sync FILED bugs with read-only Jira search JSON: Done/Resolved (non-rejected) -> FIXED; rejected -> triage hint."""
    if isinstance(data, dict):
        data = next((data[k] for k in ("issues", "workItems", "values") if isinstance(data.get(k), list)), None)
    if not isinstance(data, list):
        raise core.BadInput("bug-sync: expected a list of Jira items (or an object with issues / workItems / values)")
    filed = {}
    for r in con.execute(
        "SELECT hash, page, title, jira_key FROM bugs WHERE status='FILED' AND jira_key IS NOT NULL AND jira_key!=''"
    ):
        filed.setdefault(r["jira_key"].upper(), []).append(r)
    if not filed:
        return "no FILED bugs with a Jira key to sync"
    fixed, triage, unread, unchanged = [], [], [], 0
    with con:
        for it in data:
            if not isinstance(it, dict):
                continue
            key = str(it.get("key") or "").strip().upper()
            if key not in filed:
                continue
            fields = f if isinstance(f := it.get("fields"), dict) else {}
            st_obj = fields.get("status") if fields.get("status") is not None else it.get("status")
            st_name = _jira_str(st_obj).strip()
            cat = st_obj.get("statusCategory") if isinstance(st_obj, dict) else None
            cat_key = str((cat.get("key") or cat.get("name") or "") if isinstance(cat, dict) else "").strip().lower()
            is_done = cat_key == "done" or st_name.lower() in DONE_STATUSES
            if not is_done:
                unchanged += len(filed[key])
                continue
            has_res = "resolution" in fields or "resolution" in it
            res_obj = fields.get("resolution") if "resolution" in fields else it.get("resolution")
            res_name = _jira_str(res_obj).strip() if has_res else _view_resolution(key)
            if res_name is None:  # unknown is not 'Fixed': a Won't Do must not become FIXED because acli failed
                unread.append(key)
                continue
            if res_name.lower() in REJECTED_RESOLUTIONS:
                for b in filed[key]:
                    triage.append(
                        f"- {b['hash']} {key} ({b['page']}) closed as {res_name} -> qa-mem bug-status {b['hash']} INVALID"
                    )
                continue
            for b in filed[key]:
                con.execute(
                    "UPDATE bugs SET status='FIXED', fixed_ts=?, fixed_build=? WHERE hash=? AND status='FILED'",
                    (core.now(), core.meta(con, "build"), b["hash"]),
                )
                fixed.append(f"- {b['hash']} {key} -> FIXED ({b['page']}): {core.short(b['title'], 80)}")
    out = []
    if fixed:
        out += [f"Marked FIXED ({len(fixed)}; ready for regression re-check):"] + fixed
    if triage:
        out += [f"Needs triage ({len(triage)}; closed without a fix):"] + triage
    if unread:
        out.append(
            f"Left FILED, couldn't read the resolution of {', '.join(unread)} (check acli: `acli jira auth status`)"
        )
    if not fixed and not triage:
        out.append(f"bug-sync: 0 changes ({unchanged} still open)")
    elif unchanged:
        out.append(f"{unchanged} filed bug(s) still open")
    return "\n".join(out)


def _unrechecked_fixed(con, route, build):
    """Count of FIXED bugs on `route` not yet re-checked on `build`."""
    rows = [r[0] for r in con.execute("SELECT hash FROM bugs WHERE page=? AND status='FIXED'", (route,))]
    if not rows:
        return 0
    checked = _rechecked(con, route, build)
    return sum(h.lower() not in checked for h in rows)


ENTRY_DEPTH = 4


def entry_chain(con, route, max_depth=ENTRY_DEPTH):
    """The shortest way from a page with no record id to a detail route (`/incidents/:id/history`), over the stored
    transitions: [{from, to, trigger}, ...] from the static page down to `route`; [] for a route with no `:id`, or when
    no chain of at most `max_depth` transitions starts at one. Routes are paths on the QA app (a transition recorded
    with another host is skipped; dispatch refuses hosts outside allowedHosts too). Cycles end the search."""
    if "/:id" not in route:
        return []
    seen, level = {route}, [(route, [])]
    for _ in range(max_depth):
        found = []
        for node, path in level:
            for x in con.execute(
                "SELECT from_route, trigger FROM transitions WHERE to_route=? ORDER BY ts DESC", (node,)
            ):
                src = x["from_route"]
                if src in seen or "://" in src:
                    continue
                seen.add(src)
                way = [{"from": src, "to": node, "trigger": x["trigger"] or ""}] + path
                if "/:id" not in src:
                    return way
                found.append((src, way))
        level = found
    return []


def gaps(con, changed_only=False, top=None, as_json=False):
    """`gaps`: every page, the most urgent to explore first: never explored, its source changed since the last visit,
    verified on an older build, un-rechecked FIXED bugs, then the most untried heuristics and untested fields, then open bugs."""
    bound = codemap.module_bindings(con)
    mounted = {m for _, m in bound.values()}  # module pages: users open their shell route, which stands for both
    pages = [
        pg
        for pg in con.execute("SELECT route, name, build, last_verified, explored FROM pages WHERE route != '*'")
        if pg["route"] not in mounted or pg["route"] in bound
    ]
    current = core.meta(con, "build")
    if not pages:
        if as_json:
            return json.dumps({"build": current or core.UNKNOWN, "pages": [], "unchecked_repos": []}, indent=1)
        return "no pages yet: /qa-sdet:qa-init maps them from the source, or /qa-sdet:qa-explore <route> visits one"
    verified = {pg["route"]: pg["last_verified"] for pg in pages if pg["last_verified"]}
    visits = {}  # a page whose build was never read has no last_verified: its last run is the visit
    for target, ts in con.execute("SELECT target, ts FROM runs WHERE ts IS NOT NULL"):
        r = core.norm_route(target)
        visits[r] = max(visits.get(r, ""), ts)
    verified |= {
        pg["route"]: visits[pg["route"]]
        for pg in pages
        if not pg["last_verified"] and pg["build"] == core.UNKNOWN and visits.get(pg["route"])
    }
    changed, unchecked = _source_changes(con, verified)
    hot = _hotspots(con)
    regressed = {p for (p,) in con.execute("SELECT DISTINCT page FROM bugs WHERE status='REGRESSED'")}
    forms = set()  # pages with something to submit: a validation rule or a write call
    for x in con.execute("SELECT page, kind, label, value FROM codemap WHERE page != '*' AND kind IN ('rule', 'api')"):
        if (
            x["kind"] == "rule"
            or codemap._api_ref(x["value"])[0] in ("POST", "PUT", "PATCH", "DELETE")
            or codemap._api_ref(x["label"])[0] in ("POST", "PUT", "PATCH", "DELETE")
        ):
            forms.add(x["page"])
    forms |= {shell for shell, (_, module) in bound.items() if module in forms}
    ranked = []
    for pg in pages:
        r, b = pg["route"], current or pg["build"]
        never, unread = pg["explored"] is None, pg["build"] == core.UNKNOWN
        old = not never and not unread and pg["build"] != b
        _, tried, targets = _coverage(con, r, b)
        untried, untested = sum(h not in tried for h in core.HEURISTICS), len(_untested_rules(con, r, targets))
        open_bugs = con.execute(
            "SELECT count(*) FROM bugs WHERE page=? AND status IN (?,?,?)", (r, *core.VISIBLE)
        ).fetchone()[0]
        fixed_recheck = _unrechecked_fixed(con, r, b)
        states = len(_due_states(con, r, b))
        apis = _unexercised_apis(con, r, b, bound)
        src = changed.get(r, [])
        if changed_only and not src:
            continue
        parts = [
            "never explored"
            if never
            else "build never read"
            if unread
            else f"verified on build {pg['build']}: re-verify"
            if old
            else f"build {b}"
        ]
        if src:
            parts.append(
                f"source changed ({core.short(src[0], 60)}" + (f", +{len(src) - 1}" if len(src) > 1 else "") + ")"
            )
        if fixed_recheck:
            parts.append(f"{fixed_recheck} fixed bug{'s' * (fixed_recheck != 1)} to re-check")
        if not never:  # a page never explored has every heuristic untried
            parts.append(f"{untried} untried")
        if untested:
            parts.append(f"{untested} untested field{'s' * (untested != 1)}")
        if states:
            parts.append(f"{states} unvisited state{'s' * (states != 1)}")
        if apis:
            parts.append(f"{len(apis)} unexercised endpoint{'s' * (len(apis) != 1)}")
        if open_bugs:
            parts.append(f"{open_bugs} open bug{'s' * (open_bugs != 1)}")
        if r in regressed:
            parts.append("a bug regressed")
        if hot.get(r, 0) >= 3:
            parts.append(f"hotspot {hot[r]} (bug history)")
        chain = entry_chain(con, r)
        if (
            "/:id" in r
        ):  # a detail page: opened from the page that leads to it (maybe through others), else it needs a record
            parts.append(
                f"(open from {chain[0]['from']}: " + ", then ".join(h["trigger"] or "a link" for h in chain) + ")"
                if chain
                else "(needs a record id)"
            )
        # tiers: what changed or broke again first, then unexplored pages with something to submit, then pages on an
        # older build, then the rest; bug history (hotspot) and untried checks order each tier
        tier = 0 if src or r in regressed or fixed_recheck else 1 if never and r in forms else 2 if old else 3
        info = {
            "route": r,
            "name": pg["name"] or "",
            "tier": TIERS[tier],
            "hotspot": hot.get(r, 0),
            "never_explored": never,
            "old_build": old,
            "changed": bool(src),
            "changed_files": src,
            "unrechecked_fixed": fixed_recheck,
            "untried": untried,
            "untested_fields": untested,
            "unvisited_states": states,
            "unexercised_apis": apis,
            "open_bugs": open_bugs,
            "risk_reasons": parts,
        }
        if chain:
            info |= {"entry_route": chain[0]["from"], "entry_trigger": chain[0]["trigger"], "entry_chain": chain}
        ranked.append(
            (
                (tier, -hot.get(r, 0), -(untried + untested + states + len(apis)), -open_bugs, r),
                f'- {r} "{core.short(pg["name"] or "", 60)}": ' + "; ".join(parts),
                info,
            )
        )
    ranked.sort(key=lambda x: x[0])
    shown = ranked[:top] if top is not None else ranked
    if as_json:  # for batch runs: only pages an explorer can open; detail pages are named apart
        can_open = lambda i: "/:id" not in i["route"] or "entry_route" in i
        openable = [x[2] for x in ranked if can_open(x[2])]
        return json.dumps(
            {
                "build": current or core.UNKNOWN,
                "pages": openable[:top] if top is not None else openable,
                "needs_id": [x[2]["route"] for x in ranked if not can_open(x[2])],
                "unchecked_repos": unchecked,
            },
            indent=1,
        )
    if not shown:
        return "no pages with source changes since their last visit"
    limit = top if top is not None else 20
    out = [f"PAGES TO EXPLORE (most urgent first; current build {current or 'unknown'}):"] + [
        x[1] for x in shown[:limit]
    ]
    if len(shown) > limit:
        out.append(f"(+{len(shown) - limit} more)")
    out += [f"source changes not checked for {repo} (not a git repo, or git failed)" for repo in unchecked]
    route = next((x[2]["route"] for x in shown if "/:id" not in x[2]["route"]), None)
    if route is None:
        return "\n".join(out + ["Next: these pages need a record id: /qa-sdet:qa-explore '<a real record URL>'"])
    route = route.replace("'", "'\\''")
    return "\n".join(out + [f"Next: /qa-sdet:qa-explore '{route}'"])


def _write_errors_untried(con, route, build, bound=None):
    """['POST /api/zones'] writes (POST, PUT, PATCH) the page's runs sent on `build` that only ever got a 2xx: the
    server's refusal of bad input was never provoked."""
    group = sorted(codemap.page_group(con, route, bound))
    seen = {}
    for m, p, k in con.execute(
        f"SELECT method, path, status_class FROM observed_endpoints WHERE route IN "
        f"({','.join('?' * len(group))}) AND {ON_BUILD}",
        (*group, build, _week_ago()),
    ):
        seen.setdefault((m, p), set()).add(k)
    return sorted(
        f"{m} {p}"
        for (m, p), k in seen.items()
        if m in ("POST", "PUT", "PATCH") and "2xx" in k and not k & {"4xx", "5xx"}
    )


def blindspots(con, route=None, as_json=False):
    """What the runs left unchecked, per page (the most first) or for one route: mapped endpoints no run observed on the
    current build, writes that only ever got a 2xx, unvisited in-page states, and `not_tested` lines 2+ runs repeated;
    `systemic` are deferrals that hit 2+ pages (one blocker: --faults, a role, test data). Hints, never verdicts."""
    bound = codemap.module_bindings(con)
    mounted = {m for _, m in bound.values()}
    current = core.meta(con, "build")
    only = core.norm_route(route) if route else None
    pages = [
        pg
        for pg in con.execute("SELECT route, build FROM pages WHERE route != '*'")
        if (pg["route"] not in mounted or pg["route"] in bound) and (only is None or pg["route"] == only)
    ]
    out = []
    for pg in pages:
        r, b = pg["route"], current or pg["build"]
        item = {
            "route": r,
            "build": b,
            "unexercised_apis": _unexercised_apis(con, r, b, bound),
            "untested_write_errors": _write_errors_untried(con, r, b, bound),
            "unvisited_states": [x["state_key"] for x in _due_states(con, r, b)],
            "not_tested": [
                {"category": x["category"], "text": x["text"], "runs": x["seen_runs"]}
                for x in con.execute(
                    "SELECT category, text, seen_runs FROM blind_spots WHERE page=? AND resolved=0 AND seen_runs >= 2 "
                    "ORDER BY seen_runs DESC, last_seen DESC",
                    (r,),
                )
            ],
        }
        if any(item[k] for k in ("unexercised_apis", "untested_write_errors", "unvisited_states", "not_tested")):
            out.append(item)
    out.sort(
        key=lambda i: (
            -sum(len(i[k]) for k in ("unexercised_apis", "untested_write_errors", "unvisited_states", "not_tested")),
            i["route"],
        )
    )
    systemic = (
        [
            {"category": c, "text": t, "pages": n}
            for c, t, n in con.execute(
                "SELECT category, min(text), count(DISTINCT page) FROM blind_spots WHERE resolved=0 GROUP BY category, lower(text) "
                "HAVING count(DISTINCT page) >= 2 ORDER BY 3 DESC, 1, 2"
            )
        ]
        if only is None
        else []
    )
    if as_json:
        return json.dumps({"build": current or core.UNKNOWN, "pages": out, "systemic": systemic}, indent=1)
    if not out and not systemic:
        return f"no blind spots recorded for {only}" if only else "no blind spots recorded yet"
    lines = [f"BLIND SPOTS (what runs left unchecked on build {current or 'unknown'}; hints, never verdicts):"]
    for i in out:
        lines.append(f"{i['route']}:")
        for key, head in (
            ("unexercised_apis", "mapped in the code, not observed on this build"),
            ("untested_write_errors", "writes that only ever got a 2xx (server-side validation never provoked)"),
            ("unvisited_states", "in-page states nobody opened"),
        ):
            if i[key]:
                lines.append(f"  {head}: " + "; ".join(i[key]))
        if i["not_tested"]:
            lines.append(
                "  left untested again and again: "
                + "; ".join(f"{x['category']}: {x['text']} ({x['runs']} runs)" for x in i["not_tested"])
            )
    if systemic:
        lines.append(
            "ONE BLOCKER ON SEVERAL PAGES: "
            + "; ".join(f"{x['category']}: {x['text']} ({x['pages']} pages)" for x in systemic)
        )
    return "\n".join(lines)


TIERS = ("changed or regressed", "new, with forms", "old build", "current")
SEVERITY_WEIGHT = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def _hotspots(con):
    """{page: score} from its bug history: each bug by severity (CRITICAL 4 … LOW 1; INVALID never), +4 for a bug that
    regressed, +1 per build a check failed on. Deterministic, and shown as is (gaps, hotspots)."""
    score = {}
    for x in con.execute("SELECT page, severity, status, regressed_count FROM bugs WHERE status != 'INVALID'"):
        regressions = max(x["regressed_count"] or 0, int(x["status"] == "REGRESSED"))
        w = SEVERITY_WEIGHT.get((core.norm(x["severity"]).split() or [""])[0], 1) + 4 * regressions
        score[x["page"]] = score.get(x["page"], 0) + w
    for page, n in con.execute("SELECT page, count(DISTINCT build) FROM coverage WHERE result='fail' GROUP BY page"):
        score[page] = score.get(page, 0) + n
    return score


def hotspots(con, top=10, as_json=False):
    """Pages ranked by bug history (the score gaps breaks ties with), with what makes it up."""
    score, rows = _hotspots(con), []
    fails = dict(
        con.execute("SELECT page, count(DISTINCT build) FROM coverage WHERE result='fail' GROUP BY page").fetchall()
    )
    for page in sorted(score, key=lambda p: (-score[p], p))[:top]:
        sev = {}
        reg = 0
        for x in con.execute(
            "SELECT severity, status, regressed_count FROM bugs WHERE page=? AND status != 'INVALID'", (page,)
        ):
            k = (core.norm(x["severity"]).split() or ["unrated"])[0]
            sev[k] = sev.get(k, 0) + 1
            reg += max(x["regressed_count"] or 0, int(x["status"] == "REGRESSED"))
        rows.append(
            {"page": page, "score": score[page], "bugs": sev, "regressions": reg, "failed_builds": fails.get(page, 0)}
        )
    if as_json:
        return json.dumps(rows, indent=1)
    if not rows:
        return "no bug history yet"
    return "\n".join(
        ["HOTSPOTS (bug history: severity CRITICAL 4 ... LOW 1, +4 per regression, +1 per build a check failed):"]
        + [
            f"- {r['page']}: {r['score']} (bugs {', '.join(f'{n} {k}' for k, n in sorted(r['bugs'].items())) or 'none'}"
            f"; {r['regressions']} regressed; failed on {r['failed_builds']} build(s))"
            for r in rows
        ]
    )


def history(con, route=None, builds=5, as_json=False):
    """Build over build (newest first: runs, pages, cases, bugs new / fixed / regressed), or one page's run timeline."""
    plural = lambda n, w: f"{n} {w}{'s' * (n != 1)}"
    if route:
        page = core.norm_route(route)
        runs = sorted(
            (x for x in con.execute("SELECT * FROM runs") if core.norm_route(x["target"]) == page),
            key=lambda x: x["ts"] or "",
            reverse=True,
        )[:20]
        if as_json:
            return json.dumps({"page": page, "runs": [dict(x) for x in runs]}, indent=1)
        return (
            "\n".join(
                [f"HISTORY of {page} (newest first):"]
                + [
                    f"- {str(x['ts'])[:10]} build {x['build']} {x['mode'] or ''} {x['status'] or '?'}: cases {x['cases_pass']} pass / "
                    f"{x['cases_fail']} fail / {x['cases_blocked']} blocked, {plural(x['bugs_found'], 'bug')}"
                    + (f" — {core.short(x['summary'], 80)}" if x["summary"] else "")
                    for x in runs
                ]
            )
            if runs
            else f"no runs on {page} yet"
        )
    out = []
    for b, last in con.execute(
        "SELECT build, max(ts) FROM runs GROUP BY build ORDER BY max(ts) DESC LIMIT ?", (builds,)
    ):
        rs = con.execute(
            "SELECT target, cases_pass, cases_fail, cases_blocked FROM runs WHERE build=?", (b,)
        ).fetchall()
        n = lambda sql: con.execute(sql, (b,)).fetchone()[0]  # noqa: B023 - called in this loop pass
        out.append(
            {
                "build": b,
                "last_run": last,
                "runs": len(rs),
                "pages": len({core.norm_route(x["target"]) for x in rs}),
                "cases_pass": sum(x["cases_pass"] or 0 for x in rs),
                "cases_fail": sum(x["cases_fail"] or 0 for x in rs),
                "cases_blocked": sum(x["cases_blocked"] or 0 for x in rs),
                "new_bugs": n("SELECT count(*) FROM bugs WHERE first_build=? AND status != 'INVALID'"),
                "fixed": n("SELECT count(*) FROM bugs WHERE fixed_build=?"),
                "regressed": n("SELECT count(*) FROM bugs WHERE last_build=? AND status='REGRESSED'"),
            }
        )
    if as_json:
        return json.dumps({"builds": out}, indent=1)
    if not out:
        return "no runs yet"
    return "\n".join(
        ["BUILD OVER BUILD (newest first):"]
        + [
            f"- build {x['build']}: {plural(x['runs'], 'run')} on {plural(x['pages'], 'page')}; cases {x['cases_pass']} pass / "
            f"{x['cases_fail']} fail / {x['cases_blocked']} blocked; {plural(x['new_bugs'], 'new bug')}, {x['fixed']} fixed, "
            f"{x['regressed']} regressed"
            for x in out
        ]
    )


def _source_changes(con, verified):
    """({page: [src, ...]}, [repos git couldn't read]): the files of each visited page (its code-map rows and the
    backend rows of the endpoints it calls) that a commit touched after the page was last verified. Source HEAD may be
    ahead of the deployed build: a hint, not proof. A submodule's files are mapped under the parent repo, whose log
    names only the submodule folder when it moves: that counts as a change to every mapped file under it."""
    files, by_repo = {}, {}
    for page, ts in verified.items():
        try:
            when = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        files[page] = (when, {(x["repo"], x["src"]) for x in codemap._codemap_rows(con, page) if x["src"]})
        for repo, _ in files[page][1]:
            by_repo.setdefault(repo, []).append(ts)
    newest, failed = {}, []
    for repo, stamps in sorted(by_repo.items()):
        # --first-parent -m: a merged branch counts when it landed; --no-renames: a renamed file's old path is listed
        log = (
            None
            if core.GIT_SLOW
            else core._git(
                repo,
                "log",
                "--first-parent",
                "-m",
                "--no-renames",
                "--relative",
                "--name-only",
                "--format=%x01%ct",
                f"--since={min(stamps)}",
                "--",
                ".",
                timeout=10,
            )
        )
        if log is None:
            failed.append(repo)
            continue
        t = 0
        for line in log.splitlines():
            if line.startswith("\x01"):
                t = int(line[1:]) if line[1:].isdigit() else 0
            elif line:
                newest[repo, line] = max(newest.get((repo, line), 0), t)
    # the file itself, or a folder above it (only a submodule shows up in the log as a folder)
    stamp = lambda repo, src: max(
        newest.get((repo, src[:i]), 0) for i in [len(src)] + [m.start() for m in re.finditer("/", src)]
    )
    changed = {}
    for page, (when, fs) in files.items():
        hit = sorted(src for repo, src in fs if stamp(repo, src) > when)
        if hit:
            changed[page] = hit
    return changed, failed
