"""Readers and the user's writes over the memory DB: `search`, `bugs`, `patterns`, `impact`, `bug-match`, a ticket's
context, questions, facts, the signal baseline, `bug-status` and `bug-sync`."""

from __future__ import annotations

import itertools
import json
import re
import sqlite3

from . import cards, codemap, core, scrub


def pattern_tag(raw):
    """A defect class as a short kebab-case tag ('Silent error as EMPTY!' -> silent-error-as-empty), or None."""
    tag = re.sub(r"[^a-z0-9]+", "-", str(raw or "").lower()).strip("-")[:60].strip("-")
    return tag or None


def signal_key(key):
    """(kind, key) of a triage signal as the baseline keeps it: request ids and console numbers made generic, so one error
    seen on record 7 and record 12 is one row; None for a request loop (a per-visit count)."""
    if key.startswith("console: "):
        return "console", "console: " + re.sub(r"\d+", "N", key[len("console: ") :])[:200]
    m = re.fullmatch(r"([A-Z]+) (\S+) → (\S+).*", key)
    return (
        ("request", f"{m.group(1)} {core.mask_ids(m.group(2)) or m.group(2)} {m.group(3)}")
        if m and "×" not in key
        else None
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


def patterns(con, min_pages=2, as_json=False):
    rows = cards.bug_patterns(con, min_pages)
    if as_json:
        return json.dumps(rows, indent=1)
    return (
        "\n".join(
            [f"RECURRING PATTERNS (one kind of bug on {min_pages}+ pages; most pages first):"]
            + [cards.pattern_line(g) for g in rows]
        )
        if rows
        else f"no bug pattern on {min_pages}+ pages yet"
    )


def search(con, q):
    """Plain words, all required, stemmed ('date' finds 'dates'); `-word` excludes. Facts, bugs and the code map. When
    nothing matches all of 3+ words, the facts and bugs matching any 2 of them are shown, marked relaxed. Built here as an
    FTS query, so FTS operators typed in the terms (NEAR, AND, a leading -) can't change what is matched."""
    terms = [t for t in str(q).split() if t not in ("AND", "OR", "NOT", "NEAR") and re.search(r"\w", t)]
    pos, neg = [t for t in terms if not t.startswith("-")], [t[1:] for t in terms if t.startswith("-")]
    # '/api/zones', 'speed-limit', 'form.html': every word in a term counts; multi-word exclusions match as one phrase
    words = list(dict.fromkeys(w for t in pos for w in re.findall(r"\w+", t)))[:8]
    nots = [" ".join(ws) for t in neg if (ws := re.findall(r"\w+", t))]
    found = lambda groups: (
        _search(con, cards.fts_query(groups, nots)),
        _search_bugs(con, cards.fts_query(groups, nots)),
    )
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
    pairs = cards.fts_query(list(itertools.combinations(terms, 2)), []) if len(terms) > 1 else None
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
        for p, reason in cards.related_pages(con, r):
            if p not in ranked and p in names:
                impacted.setdefault(p, f"{reason} (via {r})")
    if terms and ranked:  # on the routes the ticket points at, one of its words is enough
        one = cards.fts_query([[w] for w in terms], [])
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
        "patterns": [cards.pattern_line(g) for g in cards.bug_patterns(con)[:3]],
        "decisions": [
            f"{x['text']} → {x['answer'] or '(answered)'}" + (f" ({x['ticket']})" if x["ticket"] else "")
            for x in decisions
        ],
        "candidate_routes": [{"route": r, "name": names[r], "why": ", ".join(why[r])} for r in ranked],
        "impacted_routes": [{"route": p, "name": names[p], "why": reason} for p, reason in list(impacted.items())[:6]],
        "relevant_facts": [f"{x['page']} [{x['kind']}] {x['text']}" + (" *" if x["stale"] else "") for x in facts[:15]],
        "relevant_bugs": [cards.bug_line(x, True) for x in bug_rows[:15]],
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
        ref = codemap.api_ref(target)
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
            "cross_page_rules": cards.cross_page_rules_differ(con, route),
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
    ep = codemap.api_ref(endpoint) if endpoint else ("", "")
    near = {page} | set(cards.near_pages(con, page))
    sighted = {h for (h,) in con.execute("SELECT hash FROM sightings WHERE page=?", (page,))}
    scored = []
    for x in con.execute(f"SELECT {cards.BUG_COLS},endpoint FROM bugs"):
        theirs = _stems(f"{x['title']} {x['symptom'] or ''}")
        shared = len(words & theirs)
        same_ep = bool(ep[1] and x["endpoint"] and codemap.same_call(cards.endpoint_ref(x["endpoint"]), ep))
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
    cols = ",".join(f"b.{c}" for c in cards.BUG_COLS.split(","))  # bugs_fts has columns of the same names
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
