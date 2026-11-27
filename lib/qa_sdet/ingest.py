"""Writes into the memory DB: `ingest` of a run JSON (checked by `check_run`), a plan's questions, and
`ingest-pending`, which the hooks run over every changed run file and code map."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import cards, codemap, core, memory, scrub

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


@core.record
class Stamp:
    """What the rows stored from one run file go by: the run's id and build, when it finished, and the file."""

    rid: str  # the run's id
    build: str  # the build it ran on
    known: bool  # that build is known: only such a run verifies what it saw
    ts: str  # when the run finished
    run_dir: Path | None  # the folder of the run file: a bug's relative draft path starts there
    src: str | Path  # the run file itself: a rewrite of it replaces its bugs


def ingest(
    con: sqlite3.Connection, run: JsonObj, ts: str | None = None, run_dir: Path | None = None, src: str | Path = ""
) -> None:
    """Upsert one run.json dict. ts = when the run finished (file mtime); run_dir = folder of the run.json;
    src = the file it came from (a rewrite of that file replaces its bugs).
    Raises ValueError on bad shape."""
    rid, build, known, pages, bug_list = check_run(run)
    ts = ts or core.now()
    _store(con, run, pages, bug_list, Stamp(rid=rid, build=build, known=known, ts=ts, run_dir=run_dir, src=src))


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


# In _store_bugs' upsert: this sighting makes a FIXED bug REGRESSED. ? is 1 for a run that hadn't listed the bug yet; a
# run older than the last sighting or than the fix (ingested late) predates them: no regression.
REGRESSES = (
    "status='FIXED' AND ? AND excluded.last_seen >= coalesce(last_seen, '') "
    "AND excluded.last_seen >= coalesce(fixed_ts, '')"
)


def _store_page(con, pg, explored, stamp: Stamp):
    """One page of a run: the page row, its facts, locators, states and coverage. A run older than what memory holds
    (ingested late) adds what is new and never rolls the page back."""
    route = pg["route"]
    old = con.execute("SELECT build, last_verified FROM pages WHERE route=?", (route,)).fetchone()
    # A run older than what we already know (ingested late) must not roll the page back.
    late = bool(old and old["last_verified"] and stamp.ts < old["last_verified"])
    # A late file of the page's current build still saw the current page: its new facts and locators count.
    same_build = bool(stamp.known and old and old["build"] == stamp.build)
    current = stamp.known and (not late or same_build)
    if (
        stamp.known and not late and old and old["build"] not in (stamp.build, core.UNKNOWN, None)
    ):  # new build: re-verify
        con.execute("UPDATE facts SET stale=1 WHERE page=? AND pinned=0", (route,))  # the user's own stay
        con.execute("UPDATE locators SET stale=1 WHERE page=?", (route,))
    if late:
        con.execute("INSERT OR IGNORE INTO pages(route, name, build) VALUES(?,?,?)", (route, None, stamp.build))
    elif stamp.known:
        con.execute(
            "INSERT INTO pages(route, name, build, last_verified) VALUES(?,?,?,?) ON CONFLICT(route) DO UPDATE SET "
            "name=coalesce(excluded.name, name), build=excluded.build, "
            "last_verified=excluded.last_verified",
            (route, core.cap(pg.get("name")) or None, stamp.build, stamp.ts),
        )
    else:  # e.g. a plan from a ticket: knows the page, verified nothing
        con.execute(
            "INSERT INTO pages(route, name, build) VALUES(?,?,?) ON CONFLICT(route) DO UPDATE "
            "SET name=coalesce(name, excluded.name), build=coalesce(build, excluded.build)",
            (route, core.cap(pg.get("name")) or None, core.UNKNOWN),
        )
    if explored:
        con.execute("UPDATE pages SET explored=max(coalesce(explored, ''), ?) WHERE route=?", (stamp.ts, route))
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
                    stamp.build,
                    stamp.ts,
                    int(late and not same_build),
                    int(current),
                    int(current),
                    *(int(stamp.known and not late),) * 2,
                ),
            )
    framed = set()  # what this run found in a frame, as the same element reads without the frame
    for loc in pg.get("locators") or []:
        # A truncated locator would be broken, so skip oversized ones instead of capping.
        loc_name, loc_val = (
            core.cap(loc.get("name")),
            (core.txt(loc.get("locator"), "locators.locator") or "").strip(),
        )
        if (not late or same_build) and loc_name and loc_val and len(loc_val) <= core.MAX_TEXT:
            # late + same build, or unknown-build plan on a verified page: add, never overwrite/un-stale verified
            keep_old = late or (not stamp.known and bool(old and old["build"] not in (core.UNKNOWN, None)))
            con.execute(
                f"INSERT OR {'IGNORE' if keep_old else 'REPLACE'} INTO locators VALUES(?,?,?,?,?,0)",
                (route, loc_name, loc_val, stamp.build, stamp.ts),
            )
            if not keep_old and core.FRAME_MARK in loc_val:
                framed.add(core.frame_tail(loc_val))
    # the same element saved earlier without its frame, under another name (a page object's `this.page.` in
    # front or not): it matches nothing as written, and it takes a card's room
    for x in (
        con.execute(
            "SELECT name, locator FROM locators WHERE page=? AND instr(locator, ?)=0", (route, core.FRAME_MARK)
        ).fetchall()
        if framed
        else ()
    ):
        if re.sub(r"^(?:[A-Za-z_$][\w$]*\.)+(?=getBy)", "", x["locator"]) in framed:
            con.execute("DELETE FROM locators WHERE page=? AND name=?", (route, x["name"]))
    _store_states(con, route, pg, stamp)
    _store_coverage(con, route, pg, stamp)


def _store_coverage(con, route, pg, stamp: Stamp):
    """A page's coverage as the run reports it: one row per target, heuristic and build."""
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
                    stamp.build,
                    stamp.rid,
                    stamp.ts,
                ),
            )


def _cap_pages(con, pages):
    """Size caps: each page keeps its freshest facts, coverage and locators."""
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


def _store_bugs(con, bug_list, stamp: Stamp):
    """The run file's bugs: each is upserted and sighted; one that an earlier version of the file listed and this one
    doesn't is un-sighted, and dropped when only this file ever saw it."""
    # sightings = which bugs this run file reported. A rewritten run.json (or a sibling's hook ingesting it
    # mid-run) replaces its earlier version: a bug it no longer lists is un-sighted (dropped when only it ever
    # saw it), and seen_count counts distinct run ids even when runs are re-ingested interleaved.
    src = str(stamp.src or "")
    before = {r[0] for r in con.execute("SELECT hash FROM sightings WHERE run_id=? AND src=?", (stamp.rid, src))}
    ran = lambda h: con.execute("SELECT 1 FROM sightings WHERE run_id=? AND hash=?", (stamp.rid, h)).fetchone()
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
            not ran(h) and not con.execute("SELECT 1 FROM bugs WHERE hash=? AND last_run=?", (h, stamp.rid)).fetchone()
        )
        now_seen.add(h)
        con.execute(
            "INSERT OR IGNORE INTO sightings(run_id,src,hash,page,build) VALUES(?,?,?,?,?)",
            (stamp.rid, src, h, b["route"], stamp.build),
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
                resolve_draft(b.get("draft_path"), stamp.run_dir),
                stamp.ts,
                stamp.ts,
                stamp.rid,
                symptom,
                core.heuristic_id(b["heuristic"]) if core.norm(b.get("heuristic")) else None,
                cards.endpoint(b.get("endpoint")),
                memory.pattern_tag(b.get("pattern")),
                core.cap(b.get("ticket"))[:30] or None,
                stamp.build if stamp.known else None,
                stamp.build if stamp.known else None,
                fresh,
                fresh,
                fresh,
                fresh,
            ),
        )
    for h in before - now_seen:  # listed by an earlier version of this run.json only
        con.execute("DELETE FROM sightings WHERE run_id=? AND src=? AND hash=?", (stamp.rid, src, h))
        if ran(h):
            continue  # another file of the same run id still lists it
        con.execute(
            "UPDATE bugs SET seen_count=max(seen_count - 1, 0), "
            "last_run=CASE WHEN last_run=? THEN NULL ELSE last_run END WHERE hash=?",
            (stamp.rid, h),
        )
        # the sighting that made a FIXED bug REGRESSED is gone (a checkpoint listed it, the final file didn't)
        con.execute(
            "UPDATE bugs SET status='FIXED', regressed_count=max(coalesce(regressed_count, 0) - 1, 0), "
            "regressed_by=NULL WHERE hash=? AND status='REGRESSED' AND regressed_by=?",
            (h, stamp.rid),
        )
        # only this run ever saw it and the user hasn't triaged it: it was never a bug
        page = con.execute("SELECT page FROM bugs WHERE hash=?", (h,)).fetchone()
        gone = con.execute(
            "DELETE FROM bugs WHERE hash=? AND seen_count=0 AND status='OPEN' AND NOT EXISTS "
            "(SELECT 1 FROM sightings WHERE hash=?)",
            (h, h),
        ).rowcount
        if gone:
            _bug_dropped(src, h, page[0] if page else None, stamp.rid)


def _store(con, run, pages, bug_list, stamp: Stamp):
    with con:
        statuses = [core.case_status(c.get("status")) for c in core.dicts(run, "cases")]
        con.execute(
            "INSERT OR REPLACE INTO runs(id,mode,target,build,status,summary,ts,cases_pass,cases_fail,cases_blocked,"
            "bugs_found) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                stamp.rid,
                core.cap(run.get("mode")) or None,
                core.cap(run.get("target")),
                stamp.build,
                core.cap(run.get("status")) or None,
                core.cap(run.get("summary")),
                stamp.ts,
                statuses.count("PASS"),
                statuses.count("FAIL"),
                statuses.count("BLOCKED"),
                len(bug_list),
            ),
        )
        # The deployed build only moves forward in time: an old run.json ingested late must not roll it back.
        if stamp.known and (core.meta(con, "build_ts") or "") <= stamp.ts:
            core.set_meta(con, "build", stamp.build)
            core.set_meta(con, "build_ts", stamp.ts)
        # the read-only map (qa-mem crawl and discover) and a blocked start test nothing
        explored = core.norm(run.get("mode")) != "map" and core.norm(run.get("status")) != "blocked"
        for pg in pages:
            _store_page(con, pg, explored, stamp)
        _cap_pages(con, pages)
        _store_bugs(con, bug_list, stamp)
        target = (
            core.norm_route(core.cap(run.get("target")))
            if str(run.get("target") or "").startswith(("/", "http", "#"))
            else ""
        )
        _store_questions(con, run.get("questions"), target or "*", stamp.ts)
        if explored and target:
            _store_blind_spots(con, run, target, stamp)
        con.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY ts DESC LIMIT ?)", (core.MAX_RUNS,))
        con.execute("DELETE FROM sightings WHERE run_id NOT IN (SELECT id FROM runs)")


def _bug_dropped(src, h, page, rid):
    """A `bug_dropped` notice in the plugin log of the qa-out the run file sits in: memory deleted a bug on its own (a
    checkpoint of the run listed it, the final file didn't). Best effort; no qa-out folder, no notice."""
    try:
        qa_out = next((p for p in Path(src).parents if p.name == "qa-out"), None) if src else None
        if qa_out is not None:
            from . import plugin_log

            plugin_log.append_event(qa_out, "notice", {"kind": "bug_dropped", "hash": h, "route": page, "run": rid})
    except Exception:  # noqa: BLE001, S110 - a notice must never fail an ingest
        pass


BLIND_SPOTS_PER_PAGE = 50


def _store_blind_spots(con, run, page, stamp: Stamp):
    """The ledger of what runs leave untested, per page: each `not_tested` line of the run counts once per run. A line
    a later COMPLETED run no longer repeats is marked resolved (covered, or no longer deferred); repeating it again
    reopens it. An unfinished run says nothing about what a finished one left out."""
    from . import results

    for cat, text in results.not_tested(run):
        con.execute(
            "INSERT INTO blind_spots(page,category,text,build,first_seen,last_seen,last_run) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(page,category,text) DO UPDATE SET seen_runs=seen_runs + (last_run IS NOT excluded.last_run), "
            "last_run=excluded.last_run, build=excluded.build, last_seen=excluded.last_seen, resolved=0 "
            "WHERE excluded.last_seen >= coalesce(blind_spots.last_seen, '')",
            (page, cat, core.cap(text), stamp.build, stamp.ts, stamp.ts, stamp.rid),
        )
    if core.norm(run.get("status")) == "completed":
        con.execute(
            "UPDATE blind_spots SET resolved=1 WHERE page=? AND last_run IS NOT ? AND last_seen <= ?",
            (page, stamp.rid, stamp.ts),
        )
    con.execute(
        "DELETE FROM blind_spots WHERE page=? AND rowid NOT IN (SELECT rowid FROM blind_spots WHERE page=? "
        "ORDER BY last_seen DESC, rowid DESC LIMIT ?)",
        (page, page, BLIND_SPOTS_PER_PAGE),
    )


STATES_PER_PAGE = 60


def _store_states(con, route, pg, stamp: Stamp):
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
                stamp.build,
                stamp.ts,
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
                stamp.build,
                stamp.ts,
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


def _store_signals(con, run, f, ts):
    """Upsert the unexplained app signals of run file f (.data/run-<slug>.json) for its page."""
    from . import signals

    slug = f.name[len("run-") : -len(".json")]
    if not any((f.parent / f"{k}-{slug}.log").is_file() for k in ("requests", "console")):
        return
    target = str(run.get("target") or "")
    page = core.norm_route(target) if target.startswith(("/", "http", "#")) else "*"
    rid, build = str(run.get("run_id")), core.norm_build(core.txt(run.get("build"), "build"))
    with con:
        for key, _, _ in signals.triage_signals(f.parent.parent, slug, run)["app"]:
            ks = memory.signal_key(key)
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
    from . import results, signals

    slug = f.name[len("run-") : -len(".json")]
    log = f.parent / f"requests-{slug}.log"
    target = str(run.get("target") or "")
    if not log.is_file() or core.norm(run.get("mode")) == "map" or not target.startswith(("/", "http", "#")):
        return
    page, build = core.norm_route(target), core.norm_build(core.txt(run.get("build"), "build"))
    hosts, rows = core.allowed_hosts(core.config()), set()
    for line in scrub.redact(results.read_text(log)).splitlines():
        m = signals.REQUEST_LINE.fullmatch(line)
        if not m:
            continue
        _, method, url, code, _ = m.groups()
        try:
            path = urlsplit(url).path or url
        except ValueError:
            continue
        shape = core.mask_ids(path)
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
        for f in ("build", "ran"):  # on a result results.carry_prior kept: its own text, compared with the run's build
            if c.get(f) is not None and not isinstance(c[f], str):
                raise ValueError(f"'cases.{f}' must be a string")
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
            core.write(done, f"{digest}\n{core.now()}\n{note}")
            err.unlink(missing_ok=True)
            n += 1
        except sqlite3.OperationalError as e:  # transient (e.g. DB locked): retry on the next hook run
            print(f"qa-mem: will retry {f}: {e}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - bad file: park it until it is rewritten
            core.write(err, f"{digest}\n{core.now()} {type(e).__name__}: {e}\n")
            print(f"qa-mem: skipped {f}: {e}", file=sys.stderr)
            PARKED.append(f"{f}: {type(e).__name__}: {e}")
    return n
