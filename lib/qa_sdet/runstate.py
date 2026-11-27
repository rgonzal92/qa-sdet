"""Where a run folder stands and what comes next: `plan-find` (plan reuse, --resume and --retest state),
`next-action` and `explore-next`."""

import json
import re
import time
from pathlib import Path

from . import core, ops, results, signals


def _clip(s, n):
    """short() cut back to the last word so a number or name is never cut in half."""
    s = " ".join(str(s or "").split())
    if len(s) <= n:
        return s
    cut = s[: n - 1]
    return (cut[: cut.rfind(" ")] if " " in cut[n // 2 :] else cut).rstrip(" ,;:") + "…"


def _plan_folders(qa_out):
    """Run folders with a plan.json, the most recently changed first (plan or a run file; not the folder, which a
    re-rendered REPORT.md or an editor's swap file touches): plan-find's latest run and the inbox's Last QA come from
    the same order."""
    dirs = [p for p in qa_out.iterdir() if p.is_dir() and not p.is_symlink() and (p / ".data" / "plan.json").is_file()]
    when = lambda p: max(
        (results._mtime(x) for x in [p / ".data" / "plan.json", *(p / ".data").glob("run-*.json")] if x.exists()),
        default=0.0,
    )
    return sorted(dirs, key=lambda p: (when(p), p.name), reverse=True)


def _run_state(run_dir):
    """Per-route and per-case state of one run folder, or None if plan.json is missing/unreadable."""
    run_dir = Path(run_dir).resolve()
    plan_file = run_dir / ".data" / "plan.json"
    if not plan_file.is_file():
        return None
    d = results.load_run(run_dir)
    plan, runs = d["plan"], d["runs"]
    if not plan:
        return None
    tickets = [t["key"] for t in results._plan_tickets(plan)]
    cases = results.merged_cases(plan, runs)
    counts = {s: sum(c["status"] == s for c in cases) for s in core.CASE_STATUSES}
    builds = sorted(
        {core.norm_build(r.get("build")) for _, r in runs if core.norm_build(r.get("build")) != core.UNKNOWN}
    )
    case_build = {}
    for _, r in runs:
        b = core.norm_build(r.get("build"))
        for x in results._list(r, "cases"):
            if x.get("id") not in (None, "") and not isinstance(x.get("id"), bool):  # a kept result: its own build
                case_build[str(x["id"])] = core.norm_build(x["build"]) if isinstance(x.get("build"), str) else b
    # routes as dispatch keys them (path and query: /list?a=1 and /list?a=2 are two briefs and run files), spelled so a
    # --resume dispatch of one finds its brief, cases and run file again
    routes_order = []
    for c in cases:
        k = core.route_key(c.get("route"))
        if k[0] and k not in routes_order:
            routes_order.append(k)
    run_status_by_route, run_build_by_route = {}, {}
    for _, r in runs:
        k = core.route_key(r.get("target"))
        if k[0]:
            run_status_by_route[k], run_build_by_route[k] = results._first_word(r.get("status")), r.get("build")
    now, of = results._now_build(runs), lambda by, k: by.get(k, by.get((k[0], "")))
    incomplete = []
    for k in routes_order:
        rcases = [c for c in cases if core.route_key(c.get("route")) == k]
        if (
            of(run_status_by_route, k) in ("blocked", "incomplete")
            or results._older_build(of(run_build_by_route, k), now)  # a new build's re-run stopped before this page
            or any(c["status"] in ("BLOCKED", "NOT_RUN") for c in rcases)
        ):
            incomplete.append(k)
    spell = lambda k: k[0] + (f"?{k[1]}" if k[1] else "")
    completed = [spell(k) for k in routes_order if k not in incomplete]
    incomplete = [spell(k) for k in incomplete]
    # verdict()'s rules per case: a PASS that a run ended BLOCKED reported (or ran on its page), or on the page of (or
    # naming) a CONFIRMED CRITICAL/HIGH bug of its ticket that no retested case names, is retested: a partial retest must
    # not make it a prior pass and drop the reason the ticket didn't pass
    bugs = results.run_bugs(runs)
    rechecked = set().union(*(results._case_bugs(c) for c in cases if c["status"] != "PASS"))
    tkey = lambda c: c.get("ticket") if isinstance(c.get("ticket"), str) else None  # a list ticket is no plan ticket's
    serious = {
        t: [
            b
            for b in results.ticket_bugs(t, cases, bugs, tickets)
            if results._serious(b) and not results._bug_refs(b) & rechecked
        ]
        for t in {tkey(c) for c in cases}
    }

    def trusted(c):
        rt = core.norm_route(c.get("route"))
        return (
            c["status"] == "PASS"
            and not d["unread"]
            and not results._blocked_runs([c], runs)
            and not any(
                (rt and rt in (b.get("route"), b.get("target"))) or results._bug_refs(b) & results._case_bugs(c)
                for b in serious[tkey(c)]
            )
        )

    retest = [
        {
            "id": c["id"],
            "ticket": c.get("ticket") or "",
            "ac": c.get("ac") or "",
            "route": core.norm_route(c.get("route")) or "",
            "status": c["status"],
            "title": c.get("title") or "",
        }
        for c in cases
        if not trusted(c)
    ]
    passed = [
        {
            "id": c["id"],
            "ticket": c.get("ticket") or "",
            "ac": c.get("ac") or "",
            "route": core.norm_route(c.get("route")) or "",
            "title": c.get("title") or "",
            "build": case_build.get(c["id"]) or (builds[-1] if builds else core.UNKNOWN),
        }
        for c in cases
        if trusted(c)
    ]
    # an earlier retest's prior passes, not re-run here: carried on, or a chained retest would drop them
    ids = {c["id"] for c in cases}
    passed += [
        {
            "id": str(c["id"]),
            "ticket": c.get("ticket") or "",
            "ac": c.get("ac") or "",
            "route": core.norm_route(c.get("route")) or "",
            "title": core._s(c.get("title")),
            "build": core._s(c.get("build")) or core.UNKNOWN,
        }
        for c in results._prior_passed(plan, tickets)
        if isinstance(c.get("id"), (str, int))
        and not isinstance(c["id"], bool)
        and str(c["id"])
        and str(c["id"]) not in ids
    ]
    zero_exec = (
        bool(runs)
        and all(c["status"] in ("BLOCKED", "NOT_RUN") for c in cases)
        and all(results._first_word(r.get("status")) == "blocked" for _, r in runs)
    )
    kind = str(plan.get("kind") or "")
    return {
        "run_dir": str(run_dir),
        "name": run_dir.name,
        "plan_file": str(plan_file),
        "kind": kind,
        "tickets": tickets,
        "has_runs": bool(runs),
        "zero_executed": zero_exec,
        "builds": builds,
        "total_cases": len(cases),
        "counts": counts,
        "incomplete_routes": incomplete,
        "completed_routes": completed,
        "retest_cases": retest,
        "passed_cases": passed,
        "can_resume": kind == "ticket" and bool(incomplete),
        "can_retest": bool(cases) and bool(runs),
    }


def plan_find(project, keys=None, run_dir=None):
    """Find the newest reusable plan and/or latest ticket run for `keys`, or inspect a specific `--run` folder."""
    proj = ops._project(project)
    if run_dir is not None:
        target = Path(run_dir)
        target = target if target.is_absolute() else (proj / "qa-out" / target if not target.exists() else target)
        target = target.resolve()
        if not target.is_dir() or target.parent.name != "qa-out":
            raise core.BadInput(f"plan-find: not a run folder (qa-out/<run>/): {run_dir}")
        st = _run_state(target)
        if st is None:
            raise core.BadInput(f"plan-find: missing or unreadable plan.json in {target}")
        reusable = st if (st["kind"] == "plan" or not st["has_runs"] or st["zero_executed"]) else None
        return {"reusable_plan": reusable, "latest_run": st}
    want = {k.strip().upper() for k in (keys or []) if k and str(k).strip()}
    if not want:
        raise core.BadInput("plan-find: give --keys K1,K2 or --run DIR")
    qa_out = proj / "qa-out"
    if not qa_out.is_dir():
        return {"reusable_plan": None, "latest_run": None}
    key = next(iter(want)) if len(want) == 1 else ""  # several keys: a combined run, found as before
    mine = qa_out / ops.run_label(keys=[key]) if ops.KEY_RE.fullmatch(key) else None
    st = _run_state(mine) if mine and mine.is_dir() and not mine.is_symlink() else None
    reusable_kind = lambda s: (
        s["kind"] == "plan" or (s["kind"] == "ticket" and (not s["has_runs"] or s["zero_executed"]))
    )
    if st:  # the ticket's folder is its run, re-run in place; its plan is reusable as long as nothing ran
        return {"reusable_plan": st if reusable_kind(st) else None, "latest_run": st}
    dirs = _plan_folders(qa_out)  # no folder of its own (yet): an older dated or combined run, as before
    reusable, latest = None, None
    for p in dirs:
        st = _run_state(p)
        if not st:
            continue
        have = {k.strip().upper() for k in st["tickets"]}
        if reusable is None and have == want and reusable_kind(st):
            reusable = st
        # the newest run that tested them, alone or with other tickets (PROJ-101 of PROJ-100+101: its slice, TC ids
        # kept): an older run of the ticket alone must not hide what a newer multi-ticket run found
        if latest is None and st["kind"] == "ticket" and st["has_runs"] and want <= have:
            latest = st if have == want else _slice(st, want)
        if reusable and latest:
            break
    return {"reusable_plan": reusable, "latest_run": latest}


def block_scope(run):
    """What a BLOCKED run blocks: its block_scope, else read from its summary (the explorer's start reports the sign-in
    and browser failures in fixed words); anything else blocks just its page."""
    from . import ingest

    said = str(run.get("block_scope") or "").strip().lower()
    if said in ingest.BLOCK_SCOPES:
        return said
    text = str(run.get("summary") or "").lower()
    if re.search(r"session expired|qa-login|\bsso\b|sign-?in", text):
        return "session"
    if re.search(r"browser profile in use|can't start|browser tool failed|kill-all", text):
        return "browser"
    return "page"


def next_action(run_dir, slug):
    """{action: continue | recover_sso | abort, block_scope, reason} for the coordinator after the explorer of
    run-<slug>.json: a page blocker doesn't stop the other routes (two in a row do: the app may be down), a sign-in
    blocker is recovered with qa-login (the skill does it once per run), a browser one or a missing run file stops.
    The row of page blockers is counted from these calls (.data/next-action.json) and starts again after an abort, so
    a --resume of the same folder isn't stopped by the run files of the session that stopped."""
    data = Path(run_dir) / ".data"
    f, state_file = data / f"run-{slug}.json", data / "next-action.json"
    prev = results._load_json(state_file, [])
    prev = prev if isinstance(prev, dict) else {}
    mtime = results._mtime(f)
    again = (prev.get("slug"), prev.get("mtime")) == (slug, mtime)  # asked again about the same result
    streak = prev.get("before" if again else "streak")
    streak = streak if isinstance(streak, int) and not isinstance(streak, bool) else 0
    at = prev.get("at") if isinstance(prev.get("at"), (int, float)) else None
    others = at is not None and any(
        results._mtime(b) > at for b in data.glob("brief-*.md") if b.name != f"brief-{slug}.md"
    )
    if not again and (others or (prev.get("slug") == slug and prev.get("action") != "recover_sso")):
        streak = 0  # another route was dispatched and never answered, or this one runs again: a session stopped

    def answer(out, new_streak):
        core._write(
            state_file,
            json.dumps(
                {
                    "slug": slug,
                    "mtime": mtime,
                    "before": streak,
                    "streak": new_streak,
                    "action": out["action"],
                    "at": time.time(),
                }
            )
            + "\n",
        )
        return out

    run = results._load_json(f, [])
    if not isinstance(run, dict):
        return answer({"action": "abort", "block_scope": "browser", "reason": "explorer ended without a run file"}, 0)
    if results._first_word(run.get("status")) != "blocked":
        return answer(
            {"action": "continue", "block_scope": None, "reason": f"run-{slug}.json is {run.get('status') or '?'}"}, 0
        )
    scope, why = block_scope(run), _clip(run.get("summary"), 120)
    if scope == "session":
        return answer({"action": "recover_sso", "block_scope": scope, "reason": why}, streak)
    if scope == "browser":
        return answer({"action": "abort", "block_scope": scope, "reason": why}, 0)
    if streak + 1 >= 2:
        return answer(
            {
                "action": "abort",
                "block_scope": scope,
                "reason": f"{streak + 1} pages in a row blocked (the QA app may be down): {why}",
            },
            0,
        )
    return answer({"action": "continue", "block_scope": scope, "reason": why}, streak + 1)


FRONTIER_SCORE = {"incomplete": 60, "endpoint": 50, "downstream": 40, "shared": 30}  # + 1-4 for the bug's severity
LEAD_SCORE = {"HIGH": 55, "MEDIUM": 35, "LOW": 20}
FRONTIER_CAP = 8
SAFE_ROUTE_RE = re.compile(r"[/#][\w#!/.:~%@+,=-]*")  # a path the coordinator can single-quote into a command


def _safe_text(v, cap=ops.HYPOTHESIS_CAP):
    """One line of page-derived text (a lead's reason, a bug's title) the coordinator can put in single quotes: no
    control characters, no `'`, at most `cap` characters."""
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(v or "")).split()).replace("'", "\u2019")[:cap]


def _focus_terms(*texts):
    from . import memory

    words = [w.lower() for t in texts for w in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", str(t or ""))]
    return list(dict.fromkeys(w for w in words if w not in memory.STOP_WORDS))[:4]


def _frontier(con, runs, explored, done, incomplete, leads):
    """Where to look next, best first: unfinished routes, every page calling a CONFIRMED bug's exact endpoint (no caller
    cliff), the readers of what a write bug saved, pages sharing a call or component with a bug that names no endpoint,
    and the explorers' leads. [{route, entry_route, entry_chain, priority, score, source, hypothesis, focus_terms,
    endpoint, pattern}], a detail route with the chain of pages that opens it (none: nobody reached it). `con` may be None."""
    from . import cards, codemap, gaps, memory

    found = {}

    def add(route, source, score, hypothesis, terms_from, endpoint=None, pattern=None):
        route = core.norm_route(route)
        if not SAFE_ROUTE_RE.fullmatch(route) or (route in explored and source != "incomplete"):
            return
        if route not in found or score > found[route]["score"]:
            found[route] = {
                "route": route,
                "score": score,
                "source": source,
                "hypothesis": _safe_text(hypothesis),
                "focus_terms": _focus_terms(*terms_from),
                "endpoint": endpoint,
                "pattern": pattern,
            }

    for route in incomplete:
        status = (done.get(route) or {}).get("status")
        add(
            route,
            "incomplete",
            FRONTIER_SCORE["incomplete"],
            f"This route was left unfinished in this run ({_clip(status, 30) if status else 'never run'}): do its checks.",
            (),
        )
    for lead in leads:
        said = " - ".join(x for x in (_clip(lead.get("target"), 120), _clip(lead.get("reason"), 200)) if x)
        add(
            lead["route"],
            "lead",
            LEAD_SCORE.get(lead["priority"], 35),
            f"An explorer left this lead: {said or lead.get('kind') or 'look here'}",
            (lead.get("target"), lead.get("reason")),
        )
    for _, r in runs:
        for b in results._list(r, "bugs"):
            if core.norm(b.get("classification")) != "confirmed" or not b.get("route") or con is None:
                continue
            route, weight = (
                core.norm_route(b.get("route")),
                gaps.SEVERITY_WEIGHT.get((core.norm(b.get("severity")).split() or [""])[0], 1),
            )
            title, ref = (
                _clip(b.get("title"), 100),
                codemap._api_ref(b.get("endpoint")) if b.get("endpoint") else ("", ""),
            )
            pattern = memory._pattern(b.get("pattern"))
            terms = (b.get("title"), b.get("symptom"))
            if ref[1]:
                endpoint, group = f"{ref[0]} {ref[1]}".strip(), codemap.page_group(con, route)
                calling = codemap.callers(con, ref)
                for page in codemap.shown(con, calling - group):
                    add(
                        page,
                        "endpoint",
                        FRONTIER_SCORE["endpoint"] + weight,
                        f"A CONFIRMED bug on {route} ({title}) went through {endpoint}; this page calls it too: check for the same failure.",
                        terms,
                        endpoint,
                        pattern,
                    )
                if ref[0] in codemap.WRITE_METHODS:
                    for x in codemap.resource_links(con, {ref}, calling | group)["consumers"]:
                        add(
                            x["route"],
                            "downstream",
                            FRONTIER_SCORE["downstream"] + weight,
                            f"{route} writes through {endpoint} (bug: {title}); this page reads that resource: check it shows what was saved.",
                            terms,
                            endpoint,
                            pattern,
                        )
            else:
                for page in cards._near_pages(con, route):
                    add(
                        page,
                        "shared",
                        FRONTIER_SCORE["shared"] + weight,
                        f"A CONFIRMED bug on {route} ({title}); this page shares a call or component with it: check for the same failure.",
                        terms,
                        None,
                        pattern,
                    )
    out = sorted(found.values(), key=lambda x: (-x["score"], x["route"]))[:FRONTIER_CAP]
    for x in out:
        chain = gaps.entry_chain(con, x["route"]) if con is not None else []
        chain = chain if chain and SAFE_ROUTE_RE.fullmatch(chain[0]["from"]) else []
        x["priority"] = "HIGH" if x["score"] >= 50 else "MEDIUM" if x["score"] >= 30 else "LOW"
        x |= {"entry_route": chain[0]["from"] if chain else None, "entry_chain": chain}
    return out


def _run_patterns(runs):
    """The defect patterns (else the heuristic) of the CONFIRMED bugs this run found so far, one per pattern, with the
    sentence to hand the pages still to explore."""
    from . import memory

    out = {}
    for _, r in runs:
        for b in results._list(r, "bugs"):
            tag = memory._pattern(b.get("pattern") or b.get("heuristic"))
            if tag and core.norm(b.get("classification")) == "confirmed" and tag not in out:
                route, title = core.norm_route(b.get("route") or r.get("target")), _clip(b.get("title"), 100)
                out[tag] = {
                    "pattern": tag,
                    "route": route,
                    "title": _safe_text(title, 100),
                    "hypothesis": _safe_text(
                        f"Seen earlier in this run on {route}: {tag} ({title}). Try the same kind "
                        "of failure here first."
                    ),
                }
    return list(out.values())[:5]


def _expand_gate(runs, run_dir, frontier, leads):
    """(recommended, reason): whether pages beyond the unfinished ones are worth running (X1's marginal-yield gate): a HIGH
    lead, a CONFIRMED bug that may reach other pages, or an unexplained 5xx first seen in this run. Else the run found
    nothing to chase and `--adaptive` / `--continue` stop at the pages asked for."""
    why = []
    high = [x["route"] for x in frontier if x["source"] == "lead" and x["priority"] == "HIGH"]
    if high:
        why.append(f"a HIGH lead ({', '.join(high[:3])})")
    spread = [x["route"] for x in frontier if x["source"] in ("endpoint", "downstream", "shared")]
    if spread:
        why.append(f"a CONFIRMED bug may reach {', '.join(spread[:3])}")
    baseline = signals._signal_baseline()
    new5xx = [
        key[len("[NEW] ") :]
        for name, r in runs
        for key, _ in signals._signals(run_dir, name, r, baseline=baseline)[0]
        if key.startswith("[NEW] ") and re.search(r"→ 5\d\d\b", key)
    ]
    if new5xx:
        why.append(f"a new server error ({new5xx[0]})")
    if why:
        return True, "; ".join(why)
    seen = f"{len(leads)} lead(s), none HIGH" if leads else "no leads"
    return (
        False,
        f"no HIGH lead, no CONFIRMED bug reaching other pages and no new 5xx ({seen}): more pages would add little",
    )


def explore_next(project, run_dir=None):
    """What an explore run (the newest, or `run_dir`) left to do: planned routes with no run file, or INCOMPLETE, or
    blocked by the sign-in or browser; the leads its explorers left on routes it didn't explore; and pages a CONFIRMED
    bug it found may reach (`blast_radius_routes`: the pages calling the bug's exact endpoint, else those sharing a call
    or component). `frontier` ranks them all with why (a `hypothesis` to hand the explorer) and the chain of pages that
    opens a detail route; `run_patterns` are the bug patterns this run has found, to try first on the pages still to do.
    `expand_recommended` (with `expand_reason`) says whether pages beyond the unfinished routes are worth running."""
    proj = ops._project(project)
    qa_out = proj / "qa-out"
    if run_dir is not None:
        folders = [Path(run_dir).resolve()]
    else:
        folders = [
            p
            for p in (_plan_folders(qa_out) if qa_out.is_dir() else [])
            if (results._load_json(p / ".data" / "plan.json", []) or {}).get("kind") == "explore"
        ][:1]
    if not folders or not (folders[0] / ".data" / "plan.json").is_file():
        raise core.BadInput("explore-next: no explore run folder (qa-out/<run>/ with an explore plan.json)")
    d = results.load_run(folders[0])
    title = str((d["plan"] or {}).get("title") or "")
    planned = [core.norm_route(x) for x in title.split(", ") if x.strip().startswith(("/", "http", "#"))]
    done = {core.norm_route(r.get("target")): r for _, r in d["runs"] if core.norm_route(r.get("target"))}
    unfinished = lambda r: (
        results._first_word(r.get("status")) == "incomplete"
        or (results._first_word(r.get("status")) == "blocked" and block_scope(r) != "page")
    )
    incomplete = [x for x in dict.fromkeys(planned) if x and x not in done] + [
        x for x, r in done.items() if unfinished(r)
    ]
    explored = set(planned) | set(done)
    leads = results._leads(d["runs"], skip=explored)
    con = None
    if core.db_path().is_file():
        import sqlite3

        try:
            con = sqlite3.connect(core.db_path().resolve().as_uri() + "?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            frontier = _frontier(con, d["runs"], explored, done, incomplete, leads)
        except sqlite3.Error:
            con, frontier = None, _frontier(None, d["runs"], explored, done, incomplete, leads)
        finally:
            if con is not None:
                con.close()
    else:
        frontier = _frontier(None, d["runs"], explored, done, incomplete, leads)
    radius = sorted(x["route"] for x in frontier if x["source"] in ("endpoint", "shared"))
    expand, reason = _expand_gate(d["runs"], folders[0], frontier, leads)
    return {
        "run": str(folders[0]),
        "incomplete_routes": incomplete,
        "leads": leads[:5],
        "blast_radius_routes": radius[:5],
        "frontier": frontier,
        "run_patterns": _run_patterns(d["runs"]),
        "expand_recommended": expand,
        "expand_reason": reason,
    }


def _slice(st, want):
    """A run state cut to the tickets in `want`: its cases, routes and counts; `subset_of` names the run's tickets."""
    mine = lambda c: str(c.get("ticket") or "").strip().upper() in want
    retest, passed = [c for c in st["retest_cases"] if mine(c)], [c for c in st["passed_cases"] if mine(c)]
    routes = {c["route"] for c in retest + passed if c.get("route")}
    return {
        **st,
        "subset_of": st["tickets"],
        "tickets": [k for k in st["tickets"] if k.strip().upper() in want],
        "retest_cases": retest,
        "passed_cases": passed,
        "total_cases": len(retest) + len(passed),
        "incomplete_routes": [r for r in st["incomplete_routes"] if core.norm_route(r) in {c["route"] for c in retest}],
        "completed_routes": [r for r in st["completed_routes"] if core.norm_route(r) in routes],
    }
