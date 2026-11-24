"""The stored code map: apply, card block, `code`, forget."""
import json
import os
import re
from pathlib import Path
from . import core, scrub


# ---------- code map: routes, locators, rules and endpoints read from the app's source ----------
CODEMAP_AREAS = ("frontend", "backend", "tests")  # one qa-mapper agent per area
CODEMAP_KINDS = ("route", "locator", "rule", "api")
CODEMAP_PAGE_CAP = 200  # rows per page, route rows not counted; page '*' (the whole backend) is exempt


def _route_pages_by_file(con, repo):
    """{src: {pages}} for the files that define routes (frontend route rows)."""
    out = {}
    for src, page in con.execute("SELECT src, page FROM codemap WHERE repo=? AND kind='route' AND area='frontend' "
                                 "AND page!='*'", (repo,)):
        out.setdefault(src, set()).add(page)
    return out


def _hashes(con, area, repo):
    """{file: sha1 of its content when last mapped} for one area of one repo."""
    try:
        h = json.loads(core.meta(con, f"codemap_hashes:{area}:{repo}") or "{}")
        return h if isinstance(h, dict) else {}
    except ValueError:
        return {}


def _set_hashes(con, area, repo, hashes):
    core.set_meta(con, f"codemap_hashes:{area}:{repo}", json.dumps(dict(sorted(hashes.items()))))


def _drop_code_only_pages(con):
    """Pages that exist only because of the code map, once their rows are gone (a route removed from the code)."""
    con.execute("DELETE FROM pages WHERE build IS NULL AND route NOT IN (SELECT page FROM codemap) "
                "AND route NOT IN (SELECT page FROM facts) AND route NOT IN (SELECT page FROM locators) "
                "AND route NOT IN (SELECT page FROM coverage) AND route NOT IN (SELECT coalesce(page,'') FROM bugs)")


def _row_src(repo, file):
    """'src/x.tsx' from 'src/x.tsx:12', './src/x.tsx' or '<repo>/src/x.tsx:12'."""
    rel = re.sub(r":\d[\d:-]*$", "", str(file or "").strip()[:500])  # capped, no backtracking
    if os.path.isabs(rel) and rel.startswith(repo.rstrip("/") + "/"):
        rel = rel[len(repo.rstrip("/")) + 1:]
    return rel[2:] if rel.startswith("./") else rel


def load_scan(cm, f):
    """The scan-<area>[-<n>].json next to code-map-<area>[-<n>].json `f` (the file the map `cm` was read from), checked
    against the map: same scan_id and area. Raises ValueError."""
    if not isinstance(cm, dict):
        raise ValueError("code-map must be an object")
    area = cm.get("area")
    if area not in CODEMAP_AREAS:
        raise ValueError(f"'area' must be one of {', '.join(CODEMAP_AREAS)}")
    m = re.fullmatch(r"code-map-(\w+(?:-\d+)?)\.json", Path(f).name)
    scan_path = Path(f).with_name(f"scan-{m.group(1) if m else area}.json")
    try:
        scan = json.loads(scan_path.read_text())
    except (OSError, ValueError) as e:
        raise ValueError(f"{scan_path.name} is missing or unreadable ({e}); run qa-mem codemap-scan first")
    if not isinstance(scan, dict) or not isinstance(scan.get("repos") or [], list):
        raise ValueError(f"{scan_path.name} is not a scan file; run qa-mem codemap-scan again")
    if cm.get("scan_id") != scan.get("scan_id"):
        raise ValueError(f"scan_id {cm.get('scan_id')!r} doesn't match {scan_path.name} ({scan.get('scan_id')!r}): "
                         "the map is from an older scan; map the current scan file")
    if scan.get("area") != area:
        raise ValueError(f"area {area!r} doesn't match {scan_path.name} ({scan.get('area')!r})")
    return scan


def apply_codemap(con, cm, scan, ts=None):
    """Upsert the rows of a code map (a qa-mapper's, or one built in memory like the shell menu's), using its scan for
    the repos, commits and which files were read. Rows from re-read or deleted files replace their old rows. A map
    whose rows don't fit the scan (wrong repo or file paths) is rejected, so the mapper is told and nothing is lost.
    Returns {reason: count} of the rows dropped."""
    ts = ts or core.now()
    area = cm.get("area")
    repos = {}
    for r in scan.get("repos") or []:
        path = r.get("path") if isinstance(r, dict) else None
        if isinstance(path, str) and os.path.isabs(path) and os.path.isdir(path):
            repos[os.path.realpath(path)] = {**r, "files": core._strs(r, "files"), "deleted": core._strs(r, "deleted")}
    rows = core.dicts(cm, "rows")
    for x in rows:
        for fld in ("page", "kind", "label", "value", "file", "repo"):
            core.txt(x.get(fld), f"rows.{fld}")
    with_files = [p for p, r in repos.items() if r.get("files")]
    read = {path: set(r.get("files") or []) for path, r in repos.items()}
    kept, dropped = [], {}
    for x in rows:
        raw_repo = str(x.get("repo") or "").rstrip("/")
        repo = os.path.realpath(os.path.expanduser(raw_repo)) if raw_repo else (with_files[0] if len(with_files) == 1 else "")
        kind, label = str(x.get("kind") or "").lower(), core.cap(x.get("label"))[:120]
        src = _row_src(repo, x.get("file")) if repo else ""
        page_raw = str(x.get("page") or "*").strip()
        why = ("unknown repo" if repo not in repos else "unknown kind" if kind not in CODEMAP_KINDS
               else "no label" if not label else "file not in the scan" if src not in read[repo]
               else "route outside frontend" if kind == "route" and (area != "frontend" or page_raw in ("*", ""))
               else "page * outside backend (no card shows it)" if area != "backend" and page_raw in ("*", "")
               else None)
        if why:
            dropped[why] = dropped.get(why, 0) + 1
        else:
            kept.append((x, repo, kind, label, src))
    path_drops = dropped.get("unknown repo", 0) + dropped.get("file not in the scan", 0)
    if path_drops:  # rows that can't be placed would be lost for good (their files count as read): have them fixed
        bad = sorted({str(x.get("file")) for x in rows if not any(x is k[0] for k in kept)})[:5]
        raise ValueError(f"{path_drops} of {len(rows)} rows have a repo or file that isn't in the scan file "
                         f"(e.g. {bad}); every row's repo and file must be one listed there, relative to repos[].path. "
                         "Only the mapper whose out= is this file fixes it; others ignore this")
    with con:
        for path, r in repos.items():
            if r.get("full"):
                con.execute("DELETE FROM codemap WHERE repo=? AND area=?", (path, area))
            else:
                for rel in (r.get("files") or []) + (r.get("deleted") or []):
                    con.execute("DELETE FROM codemap WHERE repo=? AND area=? AND src=?", (path, area, rel))
        pages, placed = set(), set()
        for x, repo, kind, label, src in kept:
            raw_page = str(x.get("page") or "*").strip()
            page = "*" if raw_page in ("*", "") else (core.norm_route(raw_page) or "/")
            value = core.cap(x.get("value"))
            # a config row like 'jwt.secret' = '...' (redact_json keys on `name`; code-map rows use `label`)
            ident = re.split(r"[\s./:\[\]]+", label.strip())[-1] if label.strip() else ""
            # only a bare token is a secret (12+ letters and digits): 'min 8, max 64', 'string|min:8', 'digits:6',
            # 'z.string().min(8)' and 'POST /api/auth/reset-password' are rules and endpoints, the map itself
            named = scrub.SECRET_NAME_RE.match(ident) or re.search(rf"(?i)\b(?:{scrub.SECRET_KEYS})\b", label)  # 'Mapbox API key'
            if kind == "rule" and named and re.fullmatch(r"(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9._~+-]{12,}", value.strip()) \
                    and not re.match(r"(?i)(min|max|len|required|pattern)", value.strip()):
                value = "[REDACTED]"
            n, base_label = 2, label  # two rows of one file under one label: both kept
            while (page, kind, label, repo, src) in placed:
                label, n = f"{base_label[:114]} ({n})", n + 1
            placed.add((page, kind, label, repo, src))
            con.execute("INSERT OR REPLACE INTO codemap(page,kind,label,value,file,repo,area,src,ts) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (page, kind, label, value, core.cap(x.get("file")), repo, area, src, ts))
            if page != "*":  # cards and page-name lookups resolve it, whichever kind of row named the page
                pages.add(page)
                # a page only the code knows (no build) takes its current route label: a renamed menu title moves
                con.execute("INSERT INTO pages(route, name) VALUES(?,?) ON CONFLICT(route) DO UPDATE SET name=CASE WHEN "
                            "pages.build IS NULL THEN coalesce(excluded.name, name) ELSE coalesce(name, excluded.name) END",
                            (page, label if kind == "route" else None))
        for page in pages:  # page '*' holds the whole backend: never capped; route rows are never evicted
            # nor counted: a route added to a full page (a shell menu path) must not push out another file's locator
            con.execute("DELETE FROM codemap WHERE page=? AND kind!='route' AND rowid NOT IN (SELECT rowid FROM codemap "
                        "WHERE page=? AND kind!='route' ORDER BY ts DESC, kind='api' DESC, rowid LIMIT ?)",
                        (page, page, CODEMAP_PAGE_CAP))
        for path, r in repos.items():
            files, gone = set(r.get("files") or []), set(r.get("deleted") or [])
            hashes = {} if r.get("full") else {k: v for k, v in _hashes(con, area, path).items() if k not in gone}
            given = r.get("hashes") if isinstance(r.get("hashes"), dict) else {}
            hashes.update({rel: str(given.get(rel) or "") for rel in files})
            _set_hashes(con, area, path, hashes)
            if core.COMMIT_RE.fullmatch(str(r.get("commit") or "")):
                core.set_meta(con, f"codemap_commit:{area}:{path}", r["commit"])
        _drop_code_only_pages(con)
    return dropped


def _codemap_behind(con, rows):
    """'<sha>' or '<sha>, 12 commits behind' for the repos these rows came from (a card's backend rows included);
    one note per repo when several. Only commits that touch the mapped folder count."""
    notes, counted = {}, {}  # counted: areas of one repo mapped at one commit share a single git call
    for repo, area in sorted({(x["repo"], x["area"]) for x in rows}):
        commit = core.meta(con, f"codemap_commit:{area}:{repo}")
        if not core.COMMIT_RE.fullmatch(commit or ""):
            continue
        if (repo, commit) not in counted:
            # a card is read on every page: a slow or hung git (network share) costs 2 s once, then counts are left out
            counted[repo, commit] = "" if core.GIT_SLOW else (core._git(repo, "rev-list", "--count", "--end-of-options",
                                                              f"{commit}..HEAD", "--", ".", timeout=2) or "").strip()
        behind = counted[repo, commit]
        n = int(behind) if behind.isdigit() else 0
        if repo not in notes or n > notes[repo][0]:  # areas of one repo: the furthest behind
            notes[repo] = (n, commit[:7] + (f", {n} commits behind" if n else ""))
    if len(notes) > 1:
        return "; ".join(f"{os.path.basename(repo)} {note}" for repo, (_, note) in sorted(notes.items()))
    return next(iter(notes.values()))[1] if notes else "not from git"


HTTP_METHODS = r"GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS"


def _api_ref(text):
    """(METHOD or '', 'api/zones/:id') from 'POST /api/zones/{id:int}', "fetch(`${BASE}/zones/${encodeURIComponent(id)}`)",
    "'/api/zones/' + id", 'https://h/api/x', "api.get('zones')": the lowercase shape a frontend call and a backend route
    share. ('', '') when there is no path."""
    t = str(text or "").strip()[:500]
    m = re.match(rf"({HTTP_METHODS})\s", t, re.I) or re.search(rf"\.({HTTP_METHODS})\s*(?:<[^()]*?>)?\s*\(", t, re.I)
    method = m.group(1).upper() if m else ""  # a leading verb or a client call, never a word inside the path
    t = re.sub(r"https?://[^/\s'\"`]+", "", t)  # scheme and host
    t = re.sub(r"(^|['\"`(\s])\$\{[^}]*\}/", r"\1/", t)  # a leading ${BASE}/ style prefix, whatever it is called
    t = re.sub(r"\$\{[^}]*\}", ":id", t)  # other template expressions are values: ${id}, ${encodeURIComponent(id)}
    t = re.sub(r"\{(\w+)(:[^}]*)?\}", r"{\1}", t)  # typed params: {id:int}, {id:guid}
    t = re.sub(r"(:\w+)\([^)]*\)", r"\1", t)  # :id(\d+)
    t = re.sub(r"(['\"`])\s*\+\s*[^'\"`+]+?\s*\+\s*['\"`]", ":id", t)  # '/a/' + id + '/b' -> '/a/:id/b'
    t = re.sub(r"(['\"`])\s*\+\s*[\w.()]+", r":id\1", t) if re.search(r"/['\"`]\s*\+", t) else t  # '/api/zones/' + id
    m = re.search(r"(?:^|[\s'\"`(])/?((?:[\w.{}$:\[\]<>-]+/)+[\w.{}$:\[\]<>-]+)", t) or \
        re.search(rf"(?:^(?:{HTTP_METHODS})\s+/|['\"`]/)([\w.-]+)", t, re.I) or \
        re.search(rf"\.(?:{HTTP_METHODS})\s*(?:<[^()]*?>)?\s*\(\s*['\"`]/?([\w.-]+)['\"`]", t, re.I)  # api.get('zones')
    if not m:
        return method, ""
    path = re.sub(r"\.(json|xml)$", "", m.group(1).split("?")[0].strip("/"))
    segs = [seg if seg.lower() in ("[controller]", "[action]") else
            ":id" if re.fullmatch(r"\{\w+\}|:\w+|\d+|\[\.{0,3}\w+\]|<[^>]+>", seg) else seg
            for seg in path.replace(":id:id", ":id").split("/") if seg]
    return method, "/".join(segs).lower()


def _api_path(text):
    return _api_ref(text)[1]


API_BASE_SEG_RE = re.compile(r"api|rest|v\d+(\.\d+)?|[\w-]+-api|api-[\w-]+")


def _same_endpoint(a, b):
    """Two call shapes name one endpoint: equal, or the longer only adds base segments in front (api/zones = zones,
    api/v1/zones = v1/zones); never a router-relative tail (:id/status) or another resource (api/admin/zones)."""
    if a == b:
        return True
    s, l = sorted((a.split("/"), b.split("/")), key=len)
    return len(s) < len(l) and s[0] != ":id" and l[len(l) - len(s):] == s \
        and all(API_BASE_SEG_RE.fullmatch(x) for x in l[:len(l) - len(s)])


def _codemap_rows(con, route):
    """This page's rows plus the backend rows ('*') for the endpoints its frontend calls (same path; same method
    when both sides name one)."""
    rows = con.execute("SELECT kind,label,value,file,repo,area FROM codemap WHERE page=? ORDER BY kind, label",
                       (route,)).fetchall()
    called = set()
    for x in rows:
        if x["kind"] == "api":
            ref = _api_ref(x["value"])
            ref = ref if ref[1] else _api_ref(x["label"])
            if ref[1]:
                called.add(ref)
    if called and route != "*":
        for x in con.execute("SELECT kind,label,value,file,repo,area FROM codemap WHERE page='*' AND kind IN ('api','rule') "
                             "ORDER BY kind, label"):
            ref = _api_ref(x["label"])
            m, p = ref if ref[1] else _api_ref(x["value"])
            if p and any(_same_endpoint(p, c) and (not m or not cm or m == cm) for cm, c in called):
                rows.append(x)
    return rows


def codemap_block(con, route, shown=None):
    """Card lines for the code map: compact (about 12 lines); `qa-mem code <route>` has everything."""
    all_rows = _codemap_rows(con, route)
    rows = [x for x in all_rows if x["kind"] != "route"]
    if not rows:
        return []
    # Verified locators the card already printed; any it had no room for still show here as hints
    seen = shown if shown is not None else \
        {x["locator"] for x in con.execute("SELECT locator FROM locators WHERE page=? AND stale=0", (route,))}
    by = {k: [x for x in rows if x["kind"] == k] for k in ("locator", "rule", "api")}
    uniq = {}
    for x in by["locator"]:  # confirmed by a run: shown above already; the same locator from two files: once
        if x["value"] not in seen:
            uniq.setdefault(x["value"], x)
    by["locator"] = list(uniq.values())
    limits, out = {"locator": 15, "rule": 6, "api": 4}, []
    out.append(f"FROM CODE (hints from source, not requirements; unverified; {_codemap_behind(con, all_rows)}):")
    if by["locator"]:
        out.append("- locators to try: " + "; ".join(f"{core.short(x['label'], 40)}={x['value']}"
                                                  for x in by["locator"][:limits["locator"]]))
    for k, title in (("rule", "rules in code"), ("api", "endpoints called")):
        if by[k]:
            out.append(f"- {title}: " + "; ".join(f"{core.short(x['label'], 50)}: {core.short(x['value'], 80)}"
                                                 for x in by[k][:limits[k]]))
    more = sum(max(0, len(by[k]) - limits[k]) for k in limits)
    if more:
        out.append(f"- (+{more} more: qa-mem code '{route}')")
    return out if len(out) > 1 else []


def codemap_repos(con):
    """[(repo, rows, exists)] for every source folder the code map knows (rows, or only bookkeeping: hashes, commit)."""
    counts = {r["repo"]: r["n"] for r in con.execute("SELECT repo, count(*) AS n FROM codemap GROUP BY repo")}
    for (key,) in con.execute("SELECT key FROM meta WHERE key LIKE 'codemap_%:%:%'"):
        repo = key.split(":", 2)[2]
        counts.setdefault(repo, 0)
    return [(repo, n, os.path.isdir(repo)) for repo, n in sorted(counts.items())]


def codemap_forget(con, repo):
    """Drop one source folder's rows and commits (a moved, re-cloned or retired repo). Returns rows removed, or None
    when the code map knows no such folder. A folder that is gone matches as typed (~, trailing /, relative)."""
    p = os.path.expanduser(repo)
    keys = {repo, os.path.realpath(p), os.path.normpath(os.path.abspath(p))}
    known = {r[0] for r in codemap_repos(con)} & keys
    if not known:
        return None
    n = 0
    with con:
        for key in known:
            n += con.execute("DELETE FROM codemap WHERE repo=?", (key,)).rowcount
            for a in CODEMAP_AREAS:
                con.execute("DELETE FROM meta WHERE key IN (?,?)", (f"codemap_commit:{a}:{key}", f"codemap_hashes:{a}:{key}"))
        _drop_code_only_pages(con)
    return n


def code(con, q=""):
    """`code`: every route known from source. `code <route|name>`: every code-map row for that page.
    `code --repos`: the source folders mapped (a folder that no longer exists is flagged)."""
    if q.strip() == "--repos" and codemap_repos(con):  # also folders with bookkeeping only (a map that never landed)
        return "MAPPED SOURCE FOLDERS:\n" + "\n".join(
            f"{repo}  {n} rows" + ("" if exists else "  (folder no longer exists: qa-mem codemap-forget '<it>')")
            for repo, n, exists in codemap_repos(con))
    if not con.execute("SELECT 1 FROM codemap LIMIT 1").fetchone():
        return "no code map yet: run /qa-sdet:qa-init to map routes, locators, rules and endpoints from the source"
    if not q.strip():
        rows = con.execute("SELECT page,label,file FROM codemap WHERE kind='route' AND area='frontend' AND page!='*' "
                           "ORDER BY page").fetchall()
        n = {k: c for k, c in con.execute("SELECT kind, count(*) FROM codemap GROUP BY kind")}
        n_star = con.execute("SELECT count(*) FROM codemap WHERE page='*' AND kind='api'").fetchone()[0]
        return ("ROUTES FROM CODE:\n" + ("\n".join(f"{x['page']}  {x['label']}  ({x['file']})" for x in rows)
                                         or "(none: no frontend routes mapped yet)")
                + f"\nTOTAL: {len(rows)} routes, {n.get('locator', 0)} locators, {n.get('rule', 0)} rules, "
                  f"{n.get('api', 0)} endpoints ({n_star} backend: qa-mem code '*')")
    pg = find_page(con, q)
    route = pg["route"] if pg else ("*" if q.strip() == "*" else core.norm_route(q))
    rows = _codemap_rows(con, route)
    if not rows:
        return f"no code-map rows for '{q}'"
    return f"CODE MAP {route} ({_codemap_behind(con, rows)}; hints from source, not requirements):\n" + "\n".join(
        f"[{x['kind']}] {x['label']} = {x['value']}  ({x['file']})" for x in rows)


def find_page(con, q):
    """Routes ('/x') match exactly; anything else matches a route or page name fuzzily."""
    q = q.strip()
    if not q:
        return None
    if q.startswith("/") or "://" in q:
        return con.execute("SELECT * FROM pages WHERE route=?", (core.norm_route(q),)).fetchone()
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like, seg = f"%{esc}%", f"%/{esc}%"
    # Best first: the route itself (/zones, /zones.html), the exact page name, a route segment starting with
    # the word (/map.html before /roadmap), then the shortest route.
    return con.execute("SELECT * FROM pages WHERE route LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' OR route IN "
                       "(SELECT page FROM codemap WHERE kind='route' AND label LIKE ? ESCAPE '\\') "
                       "ORDER BY (route = ? OR route LIKE ? ESCAPE '\\') DESC, lower(coalesce(name, '')) = lower(?) DESC, "
                       "route IN (SELECT page FROM codemap WHERE kind='route' AND lower(label)=lower(?)) DESC, "
                       "route LIKE ? ESCAPE '\\' DESC, length(route) LIMIT 1",
                       (like, like, like, "/" + q, f"/{esc}.%", q, q, seg)).fetchone()
