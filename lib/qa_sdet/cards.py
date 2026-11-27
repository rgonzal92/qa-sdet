"""The page card (`qa-mem card`): what memory knows about one page, with the hints and bug lines it shows. The
page-knowledge helpers the other readers share live here too."""

import re
import time
from datetime import datetime, timezone

from . import codemap, core


def endpoint(raw):
    """'POST api/zones' (method and the call shape the code map uses) from 'POST /api/zones/42', or None."""
    method, path = codemap.api_ref(raw) if raw else ("", "")
    return f"{method} {path}".strip() if path else None


def endpoint_ref(stored):
    m, _, path = str(stored or "").partition(" ")
    return (m, path) if path and re.fullmatch(codemap.HTTP_METHODS, m) else ("", str(stored or ""))


def _frame(locator):
    """The frame a locator names, ready to go in front of another one."""
    return locator[: locator.rindex(core.FRAME_MARK) + len(core.FRAME_MARK)]


def _frame_line(con, route, own=()):
    """The card's FRAME line ([] when no fresh locator of any page names a frame): the frame to put in front of a locator
    that lacks one. `own` are the frames this page's fresh locators name, newest first. An app shell draws its pages in
    one iframe, so the frame the most pages name wins, this page's own on a tie: a page's only framed locators may sit
    in a second frame it opens beside that one (a panel, a chat)."""
    pages = {f: {route} for f in own}
    for x in con.execute(
        "SELECT page, locator FROM locators WHERE page!=? AND stale=0 AND instr(locator, ?) ORDER BY last_verified DESC",
        (route, core.FRAME_MARK),
    ):
        pages.setdefault(_frame(x["locator"]), set()).add(x["page"])
    if not pages:
        return []
    frame = max(pages, key=lambda f: len(pages[f]))  # a tie: this page's own, then the one seen last
    if frame in own:
        return [
            f"FRAME: this page loads in a frame: put `{frame}` in front of a locator that lacks it (a FROM CODE one too)"
        ]
    n = len(pages[frame])
    loads, tip = (
        f"{n} other page{'s load' if n > 1 else ' loads'}",
        "in front of a locator that lacks it (a FROM CODE one too)",
    )
    if own:
        return [
            f"FRAME: {loads} in a frame: try `{frame}` {tip}; only if that matches nothing, `{own[0]}`, which locators "
            "of this page name"
        ]
    return [f"FRAME: no locator of this page names a frame yet; {loads} in one: try `{frame}` {tip}"]


def _card_facts(con, r, focus_words, ids, current):
    """FACTS: the charter's first, then rules before plain notes; near-duplicates collapse to the freshest."""
    out = []
    facts, seen = [], []
    # the charter's facts first (stemmed match on any of its words), then rules, flows, validations, API facts and
    # ticket-stated ones before plain notes: 15 recent notes must not push the page's rules off the card
    on_focus = (
        con.execute(
            "SELECT f.id,f.kind,f.text,f.stale,f.source FROM facts_fts JOIN facts f ON f.id=facts_fts.rowid "
            "WHERE facts_fts MATCH ? AND f.page=? ORDER BY rank LIMIT 15",
            (fts_query([[w] for w in focus_words], []), r),
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
    return out


def _card_questions(con, r):
    """DECISIONS and OPEN QUESTIONS."""
    out = []
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
    return out


def _card_locators(con, current, r):
    """LOCATORS, and with it the locators shown (FROM CODE leaves them out) and the frames they name."""
    out = []
    shown, own = set(), []
    if current:
        locs, size = [], 0
        # a run that couldn't read the build confirmed them for a week, as its coverage (ON_BUILD): a deploy since may drop them
        rows = con.execute(
            "SELECT name,locator FROM locators WHERE page=? AND stale=0 AND (build IS NOT 'unknown' OR "
            "last_verified >= ?) ORDER BY last_verified DESC",
            (r, week_ago()),
        ).fetchall()
        # one locator saved under several names, or behind its frame spelled two ways (a crawl's and
        # `generate-locator`'s), is one locator (its newest); the ones that carry their frame come first: in an app
        # shell a page sits in an iframe, and a locator without it fails as given
        newest = {}
        for x in rows:
            newest.setdefault((core.FRAME_MARK in x["locator"], core.frame_tail(x["locator"])), x)
        rows = sorted(newest.values(), key=lambda x: core.FRAME_MARK not in x["locator"])  # stable: newest first
        for x in rows:
            entry = f"{core.short(x['name'], 40)}={x['locator']}"  # locators stay whole: a cut one is useless
            if len(locs) >= 20 or size + len(entry) > 2000:
                break
            locs.append(entry)
            shown |= {x["locator"], core.frame_tail(x["locator"])}  # the code map has it without the frame
            size += len(entry)
        if locs:
            more = (
                f" (+{len(rows) - len(locs)} more verified: `find '<name>'` gives a ref)"
                if len(rows) > len(locs)
                else ""
            )
            out.append("LOCATORS (fresh): " + "; ".join(locs) + more)
            # as `generate-locator` prints it: locator('iframe[name="module"]').contentFrame().getByTestId('save')
            own = list(dict.fromkeys(_frame(x["locator"]) for x in rows if core.FRAME_MARK in x["locator"]))
    return out, shown, own


def _card_code(con, r, b, pg, shown, focus_words):
    """FROM CODE, CHANGED SINCE LAST VISIT, COVERED, UNTRIED and UNTESTED TARGETS."""
    out = []
    cov, tried, targets = coverage(con, r, b)
    untested = untested_rules(con, r, targets)
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
    return out


def _card_hints(con, r):
    """Rules that differ between client and server or across pages, and RELATED PAGES."""
    out = []
    differ = _client_server_differ(con, r)
    if differ:
        out.append(
            "CLIENT/SERVER RULES DIFFER (a hint to verify through the page, not an oracle): " + "; ".join(differ[:4])
        )
    cross = cross_page_rules_differ(con, r)
    if cross:
        out.append(
            "CROSS-PAGE RULES DIFFER (a page writing the same resource states them otherwise: a hint to verify on both "
            "pages, not an oracle): " + "; ".join(cross[:3])
        )
    related = related_pages(con, r)
    if related:
        out.append(
            "RELATED PAGES (a change here may show there; from the code map): "
            + "; ".join(f"{p} ({core.short(why, 70)})" for p, why in related[:4])
            + (f"; +{len(related) - 4} more (qa-mem impact '{r}')" if len(related) > 4 else "")
        )
    return out


def _card_bugs(con, r, b):
    """The page's bugs, bugs on pages sharing an endpoint or component, and recurring signals."""
    out = []
    out.append(bug_lines(con, r, b))
    shared = _shared_bugs(con, r)
    if shared:
        out += [
            "BUGS ON PAGES SHARING AN ENDPOINT OR COMPONENT (re-check here; seen here = put its hash in run.json, no new "
            "draft):"
        ] + [bug_line(x, True, 60) + (f" | endpoint {x['endpoint']}" if x["endpoint"] else "") for x in shared]
    noisy = con.execute(
        "SELECT sig_key, seen_runs, first_build FROM signals WHERE page=? AND status='ACTIVE' AND "
        "seen_runs >= 2 ORDER BY seen_runs DESC, last_seen DESC LIMIT 3",
        (r,),
    ).fetchall()
    if noisy:
        out += [
            "RECURRING UNEXPLAINED SIGNALS (no bug explains them; investigate or draft; the user can ignore noise):"
        ] + [f"- {x['sig_key']} (seen in {x['seen_runs']} runs since build {x['first_build']})" for x in noisy]
    return out


def _card_unvisited(con, r, b, code_only):
    """UNVISITED STATES and UNEXERCISED ENDPOINTS."""
    out = []
    due = [
        x["state_key"] + (f" ({core.short(x['trigger'], 80)})" if x["trigger"] else "") for x in due_states(con, r, b)
    ]
    if due:
        out.append(
            f"UNVISITED STATES (on this page, not opened on {'build ' + b if b and b != core.UNKNOWN else 'this build'}: "
            "open each, check it, report it as visited): "
            + "; ".join(due[:8])
            + (f" (+{len(due) - 8})" if len(due) > 8 else "")
        )
    apis = [] if code_only else unexercised_apis(con, r, b)
    if apis:
        out.append(
            f"UNEXERCISED ENDPOINTS (mapped in the code, not yet observed on build {b}: exercise them or say why not in "
            "not_tested; a hint, never an oracle): "
            + "; ".join(apis[:4])
            + (f" (+{len(apis) - 4} more)" if len(apis) > 4 else "")
        )
    return out


def _card_routes(con, r):
    """LEADS TO and ENTRY: the transitions out of the page and into it."""
    out = []
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
    return out


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
            + "".join(f"{x}\n" for x in _frame_line(con, ""))
            + tip
            + bug_lines(con, None)
        )
    r = pg["route"]
    b = core.meta(con, "build") or pg["build"]  # coverage only counts on the build deployed now
    unknown_page = pg["build"] == core.UNKNOWN  # no run read its build (a map page without the footer, a planned page)
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
        # a plan lists a page with no build too: "never readable" was wrong for a page no run had opened
        out.append(f"NOTE: no run verified this page on a build it could read; its knowledge may predate build {b}.")
    module = codemap.module_bindings(con).get(r)
    if module:  # an app-shell route: its module page's rows (code, rules, endpoints, changes) are read as this page's
        out.append(f"MODULE: {module[0] or '?'} at {module[1]} (its code-map rows below are this page's)")
    focus_words = list(dict.fromkeys(w.lower() for w in re.findall(r"\w+", str(focus or ""))))[:8]
    out += _card_facts(con, r, focus_words, ids, current)
    out += _card_questions(con, r)
    lines, shown, own = _card_locators(con, current, r)
    out += lines
    out += _frame_line(con, r, own)
    out += _card_code(con, r, b, pg, shown, focus_words)
    out += _card_hints(con, r)
    out += _card_bugs(con, r, b)
    out += _card_unvisited(con, r, b, code_only)
    out += _card_routes(con, r)
    recurring = bug_patterns(con)[:3]
    if recurring:
        out += ["RECURRING APP PATTERNS (this app broke this way on 2+ pages: try it here too):"] + [
            pattern_line(g) for g in recurring
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
    files = source_changes(con, {route: ts})[0].get(route, [])
    labels = {
        x["label"] for x in codemap.codemap_rows(con, route) if x["src"] in files and x["kind"] in ("rule", "api")
    }
    return files, labels


def _field_words(label):
    """A rule label's words, camelCase split ('POST /api/zones speedLimit' -> post api zones speed limit)."""
    return set(core.norm(re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", re.sub(r"\s\(\d+\)$", "", label))).split())


def unexercised_apis(con, route, build, bound=None):
    """['METHOD /path'] the page's code maps (a frontend `api` row, its module and shell routes included) that no run
    observed on `build` (writes first). Empty for a page with no observed call on this build at all: without a request
    log nothing is known, which isn't a gap."""
    group = codemap.page_group(con, route, bound)
    mapped = codemap.group_calls(con, group)
    if not mapped or not build:
        return []
    seen = {
        codemap.api_ref(f"{m} {p}")
        for m, p in con.execute(
            f"SELECT DISTINCT method, path FROM observed_endpoints WHERE route IN ({','.join('?' * len(group))}) AND {ON_BUILD}",
            (*sorted(group), build, week_ago()),
        )
    }
    todo = sorted(
        (c for c in mapped if seen and not any(codemap.same_call(c, o) for o in seen)),
        key=lambda c: (c[0] not in codemap.WRITE_METHODS, c[1], c[0]),
    )
    return [f"{m} /{p}" if m else f"/{p}" for m, p in todo]


def due_states(con, route, build):
    """The page's in-page states not opened on `build` (newest first): what the card's UNVISITED STATES and `gaps` count.
    A state that deletes, removes or signs out is never listed: opening it is no check."""
    week = week_ago()
    return [
        x
        for x in con.execute(
            "SELECT state_key, trigger, status, build, ts FROM page_states WHERE page=? ORDER BY ts DESC", (route,)
        )
        # a run that couldn't read the build confirmed it for a week, as with locators: else, with a recorded build,
        # every visited state is due again on every run
        if not (
            x["status"] == "visited"
            and (x["build"] == build or (x["build"] == core.UNKNOWN and (x["ts"] or "") >= week))
        )
        and not core.SKIP_LINK_RE.search(f"{x['state_key']} {x['trigger'] or ''}")
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
    rows = codemap.codemap_rows(con, route)
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


def cross_page_rules_differ(con, route):
    """Hints where a page rule and the rule of the same field (same words, camelCase or not) on a sibling page that
    writes the same resource (create and edit) don't agree: another limit or `one of` options, a trait or bound only
    one page states. Only the pages' own rules, not the backend's. Hints to verify on both pages, not oracles."""
    own = lambda r: [x for x in codemap.codemap_rows(con, r) if x["kind"] == "rule" and x["page"] != "*"]
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


def related_pages(con, route):
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


def week_ago():
    return datetime.fromtimestamp(time.time() - 7 * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rechecked(con, route, build):
    """Hashes of the page's bugs some run re-checked on `build` (coverage `regression` with the hash as target)."""
    return {
        r[0]
        for r in con.execute(
            f"SELECT lower(trim(target)) FROM coverage WHERE page=? AND heuristic='other:regression' "
            f"AND result!='blocked' AND {ON_BUILD}",
            (route, build, week_ago()),
        )
    }


def near_pages(con, route):
    """The pages a bug without an endpoint may also show on: those sharing a call, and those sharing a component file."""
    return sorted(
        set(codemap.sharing_pages(con, route)) | {x["route"] for x in codemap.component_neighbors(con, route)}
    )


def _shared_bugs(con, route, limit=3):
    """Bugs on other pages that this page may show too: bugs recorded with an endpoint this page calls (any number of
    callers: only bugs of that endpoint match), and for bugs without one the pages sharing a call or a component file."""
    mine = codemap.group_calls(con, codemap.page_group(con, route)) if route != "*" else set()
    found = {}
    cols = f"{BUG_COLS},endpoint,last_seen"
    if mine:
        for x in con.execute(
            f"SELECT {cols} FROM bugs WHERE endpoint IS NOT NULL AND page!=? AND status IN (?,?,?,'FIXED')",
            (route, *core.VISIBLE),
        ):
            if any(codemap.same_call(endpoint_ref(x["endpoint"]), c) for c in mine):
                found[x["hash"]] = x
    sharing = near_pages(con, route)
    if sharing:
        for x in con.execute(
            f"SELECT {cols} FROM bugs WHERE endpoint IS NULL AND status IN (?,?,?,'FIXED') AND page IN "
            f"({','.join('?' * len(sharing))})",
            [*core.VISIBLE, *sharing],
        ):
            found[x["hash"]] = x
    return sorted(found.values(), key=lambda x: x["last_seen"] or "", reverse=True)[:limit]


def bug_patterns(con, min_pages=2):
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


def pattern_line(g):
    return (
        f"- {g['pattern']}"
        + (f" ({g['heuristic']})" if g["heuristic"] else "")
        + f": {g['bugs']} bugs on {g['pages']} pages"
        + (f" ({g['regressed']} regressed)" if g["regressed"] else "")
        + f", e.g. {core.short(g['examples'][0], 80)}"
    )


def coverage(con, route, build):
    """({heuristic: ['target:result', ...]}, heuristics tried, normalised targets checked) on `build`. Coverage from
    runs that couldn't read the build counts for a week, then it is retried."""
    cov, targets = {}, set()
    for c in con.execute(
        f"SELECT heuristic, target, result FROM coverage WHERE page=? AND {ON_BUILD} "
        "ORDER BY heuristic, result='pass', ts DESC",
        (route, build, week_ago()),
    ):  # failures first
        cov.setdefault(c["heuristic"], []).append(f"{core.short(c['target'], 40)}:{core.short(c['result'], 12)}")
        if c["result"] != "blocked" and c["heuristic"] in ADVERSARIAL:
            targets.add(core.norm(c["target"]))
    # A heuristic only blocked (e.g. needs --faults) is still untried: the next run may be able to do it.
    tried = {h for h, t in cov.items() if any(not x.endswith(":blocked") for x in t)}
    return cov, tried, targets


def untested_rules(con, route, targets):
    """Labels of the page's code-map rules, then the linked server rules ('[server] <label>'), that no adversarial check
    named: target 'Speed limit field' covers rule 'speed', target 'date' doesn't cover 'start date'. A check of a server
    rule ('[server] POST /api/zones speed') doesn't cover the page's own field of that name."""
    out, seen = [], set()
    rows = [(x["label"], x["page"] == "*") for x in codemap.codemap_rows(con, route) if x["kind"] == "rule"]
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


def bug_line(x, with_page, n=100):
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
    line = lambda x: bug_line(x, route is None)  # app-wide list: say which page
    out = []
    if rows:
        out += ["KNOWN BUGS (don't re-draft; to report a sighting put its hash in run.json):"] + [line(x) for x in rows]
    done = rechecked(con, route, build) if route is not None else set()
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


def fts_query(groups, nots):
    """An FTS5 query: any group of words, every word of a group (each a quoted string, so typed operators are words)."""
    body = " OR ".join("(" + " ".join(f'"{w}"' for w in g) + ")" for g in groups)
    return (f"({body})" if len(groups) > 1 else body) + "".join(f' NOT "{p}"' for p in nots)


def source_changes(con, verified):
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
        files[page] = (when, {(x["repo"], x["src"]) for x in codemap.codemap_rows(con, page) if x["src"]})
        for repo, _ in files[page][1]:
            by_repo.setdefault(repo, []).append(ts)
    newest, failed = {}, []
    for repo, stamps in sorted(by_repo.items()):
        # --first-parent -m: a merged branch counts when it landed; --no-renames: a renamed file's old path is listed
        log = (
            None
            if core.GIT_SLOW
            else core.git(
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
