"""What memory says is untested or risky: `gaps`, `blindspots`, `hotspots`, `history` and a page's entry chain."""

from __future__ import annotations

import json

from . import cards, codemap, core

TYPE_CHECKING = False  # mypy reads it as True (pyproject always_true): typing stays out of the import path
if TYPE_CHECKING:  # NOSONAR
    import sqlite3


def _unrechecked_fixed(con, route, build):
    """Count of FIXED bugs on `route` not yet re-checked on `build`."""
    rows = [r[0] for r in con.execute("SELECT hash FROM bugs WHERE page=? AND status='FIXED'", (route,))]
    if not rows:
        return 0
    checked = cards.rechecked(con, route, build)
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


def _last_visits(con, pages):
    """{route: when it was last visited}: its last verification, or its last run when no run could read the build."""
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
    return verified


def _pages_with_forms(con, bound):
    """The pages with something to submit: a validation rule or a write call in the code map (a shell route for its module)."""
    forms = set()  # pages with something to submit: a validation rule or a write call
    for x in con.execute("SELECT page, kind, label, value FROM codemap WHERE page != '*' AND kind IN ('rule', 'api')"):
        if (
            x["kind"] == "rule"
            or codemap.api_ref(x["value"])[0] in ("POST", "PUT", "PATCH", "DELETE")
            or codemap.api_ref(x["label"])[0] in ("POST", "PUT", "PATCH", "DELETE")
        ):
            forms.add(x["page"])
    forms |= {shell for shell, (_, module) in bound.items() if module in forms}
    return forms


@core.record
class Survey:
    """What `gaps` reads once for every page, before it ranks each one."""

    con: sqlite3.Connection
    current: str | None  # the build memory has as the current one; None: no run has read it yet
    bound: dict[str, tuple[str, str]]  # {shell route: (module id, module page)}: codemap.module_bindings
    changed: dict[str, list[str]]  # {page: its source files a commit touched since its last visit}
    changed_only: bool  # only the pages whose source changed are wanted
    forms: set[str]  # the pages with something to submit
    hot: dict[str, int]  # {page: its hotspot score, from its bug history}
    regressed: set[str]  # the pages with a bug that regressed


def _ranked_page(ctx: Survey, pg):
    """[(sort key, text line, JSON entry)] for one page; [] when only changed pages are wanted and its source did not change."""
    ranked = []
    r, b = pg["route"], ctx.current or pg["build"]
    never, unread = pg["explored"] is None, pg["build"] == core.UNKNOWN
    old = not never and not unread and pg["build"] != b
    _, tried, targets = cards.coverage(ctx.con, r, b)
    untried, untested = sum(h not in tried for h in core.HEURISTICS), len(cards.untested_rules(ctx.con, r, targets))
    open_bugs = ctx.con.execute(
        "SELECT count(*) FROM bugs WHERE page=? AND status IN (?,?,?)", (r, *core.VISIBLE)
    ).fetchone()[0]
    fixed_recheck = _unrechecked_fixed(ctx.con, r, b)
    states = len(cards.due_states(ctx.con, r, b))
    apis = cards.unexercised_apis(ctx.con, r, b, ctx.bound)
    src = ctx.changed.get(r, [])
    if ctx.changed_only and not src:
        return ranked
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
        parts.append(f"source changed ({core.short(src[0], 60)}" + (f", +{len(src) - 1}" if len(src) > 1 else "") + ")")
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
    if r in ctx.regressed:
        parts.append("a bug regressed")
    if ctx.hot.get(r, 0) >= 3:
        parts.append(f"hotspot {ctx.hot[r]} (bug history)")
    chain = entry_chain(ctx.con, r)
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
    tier = 0 if src or r in ctx.regressed or fixed_recheck else 1 if never and r in ctx.forms else 2 if old else 3
    info = {
        "route": r,
        "name": pg["name"] or "",
        "tier": TIERS[tier],
        "hotspot": ctx.hot.get(r, 0),
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
            (tier, -ctx.hot.get(r, 0), -(untried + untested + states + len(apis)), -open_bugs, r),
            f'- {r} "{core.short(pg["name"] or "", 60)}": ' + "; ".join(parts),
            info,
        )
    )
    return ranked


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
    verified = _last_visits(con, pages)
    changed, unchecked = cards.source_changes(con, verified)
    hot = _hotspots(con)
    regressed = {p for (p,) in con.execute("SELECT DISTINCT page FROM bugs WHERE status='REGRESSED'")}
    forms = _pages_with_forms(con, bound)
    ranked = []
    ctx = Survey(
        con=con,
        current=current,
        bound=bound,
        changed=changed,
        changed_only=changed_only,
        forms=forms,
        hot=hot,
        regressed=regressed,
    )
    for pg in pages:
        ranked += _ranked_page(ctx, pg)
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
        f"({','.join('?' * len(group))}) AND {cards.ON_BUILD}",
        (*group, build, cards.week_ago()),
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
            "unexercised_apis": cards.unexercised_apis(con, r, b, bound),
            "untested_write_errors": _write_errors_untried(con, r, b, bound),
            "unvisited_states": [x["state_key"] for x in cards.due_states(con, r, b)],
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
