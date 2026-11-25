"""Ingest of run JSON, and the `card`, `search` and `bugs` readers."""
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
from . import core, scrub, codemap


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


def ingest(con, run, ts=None, run_dir=None, src=""):
    """Upsert one run.json dict. ts = when the run finished (file mtime); run_dir = folder of the run.json;
    src = the file it came from (a rewrite of that file replaces its bugs).
    Raises ValueError on bad shape."""
    if not isinstance(run, dict):
        raise ValueError("run.json must be an object")
    rid = core.txt(run.get("run_id"), "run_id")
    if not rid or not rid.strip():
        raise ValueError("run.json missing 'run_id'")
    # Same build text however the page laid it out ("Build:\n 1.42" == "Build: 1.42"); capped like all text.
    # An empty or missing build (empty version element) is unknown, not a broken run.
    build, ts = core.norm_build(core.txt(run.get("build"), "build")), ts or core.now()
    known = build.lower() != core.UNKNOWN
    for f in ("mode", "target", "status", "summary"):
        core.txt(run.get(f), f)
    pages, bug_list = core.dicts(run, "pages"), core.dicts(run, "bugs")
    for pg in pages:
        pg["route"] = core.norm_route(core.txt(pg.get("route"), "route"))
        if not pg["route"]:
            raise ValueError("page entry missing 'route'")
        core.txt(pg.get("name"), "name")
        for key, fields in (("facts", ("kind", "text", "source")), ("locators", ("name", "locator")),
                            ("coverage", ("target", "heuristic", "result"))):
            for item in core.dicts(pg, key):
                for f in fields:
                    core.txt(item.get(f), f"{key}.{f}")
    check_report_fields(run)
    for b in bug_list:
        for f in ("title", "symptom", "severity", "classification", "draft_path", "hash", "route", "evidence", "ticket"):
            core.txt(b.get(f), f"bugs.{f}")
        if b.get("annotated") is not None and not isinstance(b["annotated"], bool):
            raise ValueError("'bugs.annotated' must be true or false")
        b["route"] = core.norm_route(b.get("route"))
        if not core.cap(b.get("title")) or not b["route"]:
            raise ValueError("bug entry needs 'title' and 'route'")
    with con:
        con.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?)",
                    (rid, core.cap(run.get("mode")) or None, core.cap(run.get("target")), build, core.cap(run.get("status")) or None,
                     core.cap(run.get("summary")), ts))
        # The deployed build only moves forward in time: an old run.json ingested late must not roll it back.
        if known and (core.meta(con, "build_ts") or "") <= ts:
            core.set_meta(con, "build", build)
            core.set_meta(con, "build_ts", ts)
        for pg in pages:
            route = pg["route"]
            old = con.execute("SELECT build, last_verified FROM pages WHERE route=?", (route,)).fetchone()
            # A run older than what we already know (ingested late) must not roll the page back.
            late = bool(old and old["last_verified"] and ts < old["last_verified"])
            # A late file of the page's current build still saw the current page: its new facts and locators count.
            same_build = bool(known and old and old["build"] == build)
            current = known and (not late or same_build)
            if known and not late and old and old["build"] not in (build, core.UNKNOWN, None):  # new build: re-verify
                con.execute("UPDATE facts SET stale=1 WHERE page=?", (route,))
                con.execute("UPDATE locators SET stale=1 WHERE page=?", (route,))
            if late:
                con.execute("INSERT OR IGNORE INTO pages(route, name, build) VALUES(?,?,?)", (route, None, build))
            elif known:
                con.execute("INSERT INTO pages VALUES(?,?,?,?) ON CONFLICT(route) DO UPDATE SET "
                            "name=coalesce(excluded.name, name), build=excluded.build, "
                            "last_verified=excluded.last_verified",
                            (route, core.cap(pg.get("name")) or None, build, ts))
            else:  # e.g. a plan from a ticket: knows the page, verified nothing
                con.execute("INSERT INTO pages(route, name, build) VALUES(?,?,?) ON CONFLICT(route) DO UPDATE "
                            "SET name=coalesce(name, excluded.name), build=coalesce(build, excluded.build)",
                            (route, core.cap(pg.get("name")) or None, core.UNKNOWN))
            for f in pg.get("facts") or []:
                text = core.cap(f.get("text"))
                if text:
                    # Only a verifying run (known build, not late) refreshes an existing fact.
                    con.execute("INSERT INTO facts(page,kind,text,source,build,ts,stale) VALUES(?,?,?,?,?,?,?) "
                                "ON CONFLICT(page,kind,text) DO UPDATE SET "
                                "source=CASE WHEN ? AND lower(coalesce(excluded.source, '')) = 'observed' "
                                "THEN 'observed' ELSE source END, "  # a ticket rule the explorer has now seen
                                "stale=CASE WHEN ? THEN 0 ELSE stale END, "
                                "build=CASE WHEN ? THEN excluded.build ELSE build END, "
                                "ts=CASE WHEN ? THEN excluded.ts ELSE ts END",
                                (route, core.cap(f.get("kind"))[:30] or "note", text, core.cap(f.get("source")), build, ts,
                                 int(late and not same_build), int(current), int(current),
                                 *(int(known and not late),) * 2))
            for loc in pg.get("locators") or []:
                # A truncated locator would be broken, so skip oversized ones instead of capping.
                loc_name, loc_val = core.cap(loc.get("name")), (core.txt(loc.get("locator"), "locators.locator") or "").strip()
                if (not late or same_build) and loc_name and loc_val and len(loc_val) <= core.MAX_TEXT:
                    # late + same build, or unknown-build plan on a verified page: add, never overwrite/un-stale verified
                    keep_old = late or (not known and bool(old and old["build"] not in (core.UNKNOWN, None)))
                    con.execute(f"INSERT OR {'IGNORE' if keep_old else 'REPLACE'} INTO locators VALUES(?,?,?,?,?,0)",
                                (route, loc_name, loc_val, build, ts))
            for c in pg.get("coverage") or []:
                if core.norm(c.get("heuristic")):
                    # the newest run's result stands: a run.json ingested late must not put its older one back
                    con.execute("INSERT INTO coverage VALUES(?,?,?,?,?,?,?) ON CONFLICT(page,target,heuristic,build) "
                                "DO UPDATE SET result=excluded.result, run_id=excluded.run_id, ts=excluded.ts "
                                "WHERE excluded.ts >= coalesce(coverage.ts, '')",
                                (route, core.cap(c.get("target")) or "*", core.heuristic_id(c["heuristic"]),
                                 core.coverage_result(c.get("result")), build, rid, ts))
        for pg in pages:  # size caps: keep the freshest knowledge per page
            con.execute("DELETE FROM facts WHERE page=? AND id NOT IN (SELECT id FROM facts WHERE page=? "
                        "ORDER BY stale, ts DESC LIMIT ?)", (pg["route"], pg["route"], core.MAX_FACTS))
            con.execute("DELETE FROM coverage WHERE page=? AND rowid NOT IN (SELECT rowid FROM coverage WHERE page=? "
                        "ORDER BY ts DESC LIMIT ?)", (pg["route"], pg["route"], core.MAX_COVERAGE))
            con.execute("DELETE FROM locators WHERE page=? AND name NOT IN (SELECT name FROM locators WHERE page=? "
                        "ORDER BY stale, last_verified DESC LIMIT 60)", (pg["route"], pg["route"]))
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
            m = re.match(r"#?\s*([0-9a-fA-F]{4,12})\b", raw)  # '<hash> ', '#<hash>', '<hash> (known)'
            given = m.group(1).lower() if m else ""  # 'n/a', 'TBD', 'new': no hash, so the bug is created
            h = None
            if given:  # re-sighting of a known bug (prefix ok if unique)
                hits = con.execute("SELECT hash FROM bugs WHERE substr(hash,1,?)=?", (len(given), given)).fetchall()
                h = hits[0][0] if len(hits) == 1 else None
            h = h or core.bug_hash(b["route"], symptom)
            # a new sighting counts once; a bug from before sightings existed counts this run via last_run
            fresh = int(not ran(h) and not con.execute(
                "SELECT 1 FROM bugs WHERE hash=? AND last_run=?", (h, rid)).fetchone())
            now_seen.add(h)
            con.execute("INSERT OR IGNORE INTO sightings VALUES(?,?,?)", (rid, src, h))
            # a FIXED bug seen again by another run becomes REGRESSED.
            con.execute("INSERT INTO bugs(hash,page,title,severity,classification,draft_path,first_seen,"
                        "last_seen,last_run,symptom) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(hash) DO UPDATE SET "
                        "seen_count=seen_count + ?, "
                        # a run older than the last sighting (ingested late) predates the fix: no regression, no last_run
                        "status=CASE WHEN status='FIXED' AND ? AND excluded.last_seen >= coalesce(last_seen, '') "
                        "THEN 'REGRESSED' ELSE status END, "
                        "last_run=CASE WHEN excluded.last_seen >= coalesce(last_seen, '') THEN excluded.last_run ELSE last_run END, "
                        "last_seen=max(coalesce(last_seen, ''), excluded.last_seen), "
                        "first_seen=min(coalesce(first_seen, excluded.first_seen), excluded.first_seen), "
                        "severity=coalesce(excluded.severity, severity), "
                        "classification=coalesce(excluded.classification, classification), "
                        "draft_path=coalesce(excluded.draft_path, draft_path), "
                        "symptom=coalesce(symptom, excluded.symptom)",
                        (h, b.get("route"), core.cap(b["title"]), core.cap(b.get("severity"))[:20] or None,
                         core.cap(b.get("classification"))[:20] or None,
                         resolve_draft(b.get("draft_path"), run_dir), ts, ts, rid, symptom, fresh, fresh))
        for h in before - now_seen:  # listed by an earlier version of this run.json only
            con.execute("DELETE FROM sightings WHERE run_id=? AND src=? AND hash=?", (rid, src, h))
            if ran(h):
                continue  # another file of the same run id still lists it
            con.execute("UPDATE bugs SET seen_count=max(seen_count - 1, 0), "
                        "last_run=CASE WHEN last_run=? THEN NULL ELSE last_run END WHERE hash=?", (rid, h))
            # only this run ever saw it and the user hasn't triaged it: it was never a bug
            con.execute("DELETE FROM bugs WHERE hash=? AND seen_count=0 AND status='OPEN' AND NOT EXISTS "
                        "(SELECT 1 FROM sightings WHERE hash=?)", (h, h))
        con.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY ts DESC LIMIT ?)", (core.MAX_RUNS,))
        con.execute("DELETE FROM sightings WHERE run_id NOT IN (SELECT id FROM runs)")


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
            isinstance(i, str) or (isinstance(i, dict) and isinstance(i.get("text"), str)) for i in items):
        raise ValueError("'questions' must be a list of strings, or of {\"text\", \"status\", \"answer\"} objects")
    items = run.get("not_tested")
    items = [] if items is None else items
    if not isinstance(items, list) or not all(
            isinstance(i, str) or (isinstance(i, dict) and isinstance(i.get("text"), str)) for i in items):
        raise ValueError("'not_tested' must be a list of strings, or of {\"category\", \"text\"} objects")
    core.txt(run.get("notes"), "notes")
    env = run.get("env")
    env = {} if env is None else env
    if not isinstance(env, dict):
        raise ValueError("'env' must be an object")
    for f in ("url", "browser"):
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
    for f in [*root.glob("*/.data/run-*.json"), *(root / "init").glob("code-map-*.json")]:  # run folders; code maps: qa-out/init only
        try:
            data, mtime = f.read_bytes(), f.stat().st_mtime
        except OSError:
            continue
        digest = hashlib.sha1(data).hexdigest()
        if (_first_line(f.with_name(f.name + ".ingested")) != digest  # (.error is read only when this one doesn't match)
                and _first_line(f.with_name(f.name + ".error")) != digest):
            out.append((f, data, mtime, digest))
    return sorted(out, key=lambda x: x[2])


PARKED = []  # run.json / code-map files this process could not ingest (shown to the agent by the hook)


def _ingest_files(con, pending):
    n = 0
    for f, data, mtime, digest in pending:
        done, err = f.with_name(f.name + ".ingested"), f.with_name(f.name + ".error")
        # When the run finished; never in the future (a skewed mtime would freeze the current build).
        ts = datetime.fromtimestamp(min(mtime, time.time()), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            note = ""
            if f.name.startswith("code-map-"):
                cm = scrub.redact_json(json.loads(data))
                dropped = codemap.apply_codemap(con, cm, codemap.load_scan(cm, f), ts)
                note = f"dropped rows: {dropped}\n" if dropped else ""
            else:
                ingest(con, scrub.redact_json(json.loads(data)), ts, f.parent, f.resolve())  # also files the mtime-based scrub skipped
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


def card(con, q):
    pg = codemap.find_page(con, q)
    if not pg:
        tip = "" if con.execute("SELECT 1 FROM codemap LIMIT 1").fetchone() else \
            "tip: /qa-sdet:qa-init maps routes and locators from your source, so pages resolve by name\n"
        return (f"NO CARD for '{q}': first visit. Untried heuristics: {', '.join(core.HEURISTICS)}\n" + tip
                + bug_lines(con, None))
    r = pg["route"]
    b = core.meta(con, "build") or pg["build"]  # coverage only counts on the build deployed now
    unknown_page = pg["build"] == core.UNKNOWN  # its build was never readable (e.g. a map page without the footer)
    code_only = pg["build"] is None  # known from the code map only: nothing observed yet, nothing to re-verify
    current = pg["build"] == b or unknown_page or code_only
    out = [f"PAGE {r} \"{pg['name'] or ''}\" build={pg['build']} verified={pg['last_verified'] or 'never'}"
           + ("" if core.norm_route(q) == r else f"  (matched from '{q}')")]
    if code_only:
        out.append("NOTE: known from source code only; no run has visited it yet.")
    elif not current:
        out.append(f"NOTE: current build is {b}; everything below is from build {pg['build']}: re-verify.")
    elif unknown_page and b != core.UNKNOWN:
        out.append(f"NOTE: this page's build was never readable; its knowledge may predate build {b}.")
    facts, seen = [], []
    for f in con.execute("SELECT kind,text,stale,source FROM facts WHERE page=? ORDER BY stale, ts DESC LIMIT 60",
                         (r,)):
        words = set(core.norm(f["text"]).split())
        # Runs restate facts in new words; show only the freshest of near-duplicates.
        if any(k == f["kind"] and len(words & w) >= 0.8 * max(len(words | w), 1)
               and not any(c.isdigit() for x in words ^ w for c in x) for k, w in seen):  # '130km/h' vs '120km/h' is news
            continue
        seen.append((f["kind"], words))
        facts.append(f)
        if len(facts) == 15:
            break
    if facts:
        out.append("FACTS (* = stale, re-verify; 'from X' = stated in X, not yet observed):")
        out += [f"- [{f['kind']}] {f['text']}"
                + (f" (from {f['source']})" if f["source"] and f["source"].lower() != "observed" else "")
                + (" *" if f["stale"] or not current else "") for f in facts]
    shown = set()
    if current:
        locs, size = [], 0
        rows = con.execute("SELECT name,locator FROM locators WHERE page=? AND stale=0 ORDER BY last_verified DESC",
                           (r,)).fetchall()
        for x in rows:
            entry = f"{core.short(x['name'], 40)}={x['locator']}"  # locators stay whole: a cut one is useless
            if len(locs) >= 20 or size + len(entry) > 2000:
                break
            locs.append(entry)
            shown.add(x["locator"])
            size += len(entry)
        if locs:
            more = f" (+{len(rows) - len(locs)} more verified; find them with a snapshot)" if len(rows) > len(locs) else ""
            out.append("LOCATORS (fresh): " + "; ".join(locs) + more)
    out += codemap.codemap_block(con, r, shown)
    cov, tried, targets = _coverage(con, r, b)
    if cov:  # at most 4 targets per heuristic keeps the card short
        out.append(f"COVERED on build {b} (only-blocked ones are retried, listed in UNTRIED): " + " | ".join(
            f"{h}({', '.join(t[:4])}{f', +{len(t) - 4} more' if len(t) > 4 else ''})" for h, t in cov.items()))
    out.append("UNTRIED: " + (", ".join(h for h in core.HEURISTICS if h not in tried) or "none"))
    untested = _untested_rules(con, r, targets)
    if untested:  # labels stay whole: the explorer copies them as coverage targets
        more = f"; +{len(untested) - 10} more (qa-mem code '{r}')" if len(untested) > 10 else ""
        on = f"build {b}" if b and b != core.UNKNOWN else "this build"
        out.append(f"UNTESTED TARGETS (code rules no run covered on {on}; write the label as the coverage target): "
                   + "; ".join(untested[:10]) + more)
    out.append(bug_lines(con, r))
    return "\n".join(x for x in out if x)


def _coverage(con, route, build):
    """({heuristic: ['target:result', ...]}, heuristics tried, normalised targets checked) on `build`. Coverage from
    runs that couldn't read the build counts for a week, then it is retried."""
    cov, targets = {}, set()
    week_ago = datetime.fromtimestamp(time.time() - 7 * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for c in con.execute("SELECT heuristic, target, result FROM coverage WHERE page=? AND ((build=? AND "
                         "build!='unknown') OR (build='unknown' AND ts >= ?)) ORDER BY heuristic, result='pass', ts DESC",
                         (route, build, week_ago)):  # failures first
        cov.setdefault(c["heuristic"], []).append(f"{core.short(c['target'], 40)}:{core.short(c['result'], 12)}")
        if c["result"] != "blocked":
            targets.add(core.norm(c["target"]))
    # A heuristic only blocked (e.g. needs --faults) is still untried: the next run may be able to do it.
    tried = {h for h, t in cov.items() if any(not x.endswith(":blocked") for x in t)}
    return cov, tried, targets


def _untested_rules(con, route, targets):
    """Labels of the page's code-map rules that no checked target names: target 'Speed limit field' covers rule
    'speed', target 'date' doesn't cover 'start date'. Backend rules (page '*') need API calls, so they don't count."""
    out, seen = [], set()
    for (label,) in con.execute("SELECT label FROM codemap WHERE page=? AND kind='rule' ORDER BY label", (route,)):
        label = re.sub(r"\s\(\d+\)$", "", label)  # apply_codemap's 'speed (2)': another rule row of the same field
        words = core.norm(label)
        if words and words not in seen and not any(f" {words} " in f" {t} " for t in targets):
            out.append(label)
        seen.add(words)
    return out


def bug_lines(con, route):
    q = "SELECT hash,page,severity,status,title,symptom,seen_count FROM bugs WHERE status IN (?,?,?)"
    args = list(core.VISIBLE)
    if route is not None:
        q += " AND page=?"
        args.append(route)
    rows = con.execute(q + " ORDER BY last_seen DESC LIMIT 10", args).fetchall()
    fixed = con.execute("SELECT hash,page,severity,status,title,symptom,seen_count FROM bugs WHERE status='FIXED'"
                        + (" AND page=?" if route is not None else "") + " ORDER BY last_seen DESC LIMIT 5",
                        [route] if route is not None else []).fetchall()
    line = lambda x: (f"- {x['hash']} {x['status']} [{x['severity'] or '?'}] "
                      + (f"{x['page']} " if route is None else "")  # app-wide list: say which page
                      + f"{core.short(x['title'], 100)} | symptom: {core.short(x['symptom'] or x['title'], 120)} (seen {x['seen_count']})")
    out = []
    if rows:
        out += ["KNOWN BUGS (don't re-draft; to report a sighting put its hash in run.json):"] + [line(x) for x in rows]
    if fixed:
        out += ["PREVIOUSLY FIXED (seen again = regression: put its hash in run.json):"] + [line(x) for x in fixed]
    invalid = con.execute("SELECT hash,page,severity,status,title,symptom,seen_count FROM bugs WHERE status='INVALID'"
                          + (" AND page=?" if route is not None else "") + " ORDER BY last_seen DESC LIMIT 5",
                          [route] if route is not None else []).fetchall()
    if invalid:
        out += ["NOT BUGS (the user marked these INVALID: don't report them):"] + [line(x) for x in invalid]
    return "\n".join(out)


def search(con, q):
    """Plain words, all required; `-word` excludes. Built here as an FTS query, so FTS operators typed in the terms
    (NEAR, AND, a leading -) can't change what is matched."""
    terms = [t for t in str(q).split() if t not in ("AND", "OR", "NOT", "NEAR") and re.search(r"\w", t)]
    pos, neg = [t for t in terms if not t.startswith("-")], [t[1:] for t in terms if t.startswith("-")]
    # '/api/zones', 'speed-limit', 'form.html': every word in a term counts; multi-word exclusions match as one phrase
    words = [w for t in pos for w in re.findall(r"\w+", t)]
    nots = [" ".join(ws) for t in neg if (ws := re.findall(r"\w+", t))]
    rows = _search(con, " ".join(f'"{w}"' for w in words) + "".join(f' NOT "{p}"' for p in nots)) if words else []
    lines = [f"{x['page']} [{x['kind']}] {x['text']}" + (" *" if x["stale"] else "") for x in rows]
    if words:  # the code map too (incl. backend endpoints no page calls, page '*')
        cond = " AND ".join(["(label || ' ' || coalesce(value, '')) LIKE ? ESCAPE '\\'"] * len(pos)
                            + ["(label || ' ' || coalesce(value, '')) NOT LIKE ? ESCAPE '\\'"] * len(neg))
        like = lambda t: "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"  # the term as typed
        esc = [like(t) for t in pos + neg]
        lines += [f"{x['page']} [code:{x['kind']}] {x['label']} = {x['value']}" for x in con.execute(
            f"SELECT page,kind,label,value FROM codemap WHERE {cond} ORDER BY page='*', kind='route' DESC, page LIMIT 10",
            esc)]
    return "\n".join(lines) or f"no facts match '{q}'"


def _search(con, q):
    return con.execute("SELECT f.page,f.kind,f.text,f.stale FROM facts_fts JOIN facts f ON f.id=facts_fts.rowid "
                       "WHERE facts_fts MATCH ? ORDER BY rank LIMIT 20", (q,)).fetchall()


def bugs(con, status="OPEN"):
    total = con.execute("SELECT count(*) FROM bugs WHERE status=?", (status,)).fetchone()[0]
    rows = con.execute("SELECT * FROM bugs WHERE status=? ORDER BY last_seen DESC LIMIT 30", (status,)).fetchall()
    lines = [f"{x['hash']} [{x['severity'] or '?'}] {x['classification'] or '?'} {x['page']} {core.short(x['title'], 100)} "
             f"(seen {x['seen_count']}) {x['draft_path'] or ''}" for x in rows]
    if total > len(rows):
        lines.append(f"... {total - len(rows)} older {status} bugs not shown (search facts or ask for a page's card)")
    return "\n".join(lines) or f"no {status} bugs"
