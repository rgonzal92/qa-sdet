"""REPORT.md, the Jira draft, the run index and `inbox`."""
import json
import os
import re
from datetime import datetime
from pathlib import Path
from . import core, scrub, ops


# ---------- REPORT.md: one human report per run folder, rendered from .data/plan.json + .data/run-*.json ----------
STATUS_ICON = {"PASS": "✅ PASS", "FAIL": "❌ FAIL", "BLOCKED": "⛔ BLOCKED", "NOT_RUN": "⏸️ not run",
               "QUESTION": "❓ QUESTION"}
STATUS_RANK = ("FAIL", "BLOCKED", "QUESTION", "NOT_RUN", "PASS")  # worst first
VERDICT_ICON = {"FAIL": "❌ FAIL", "BLOCKED": "⛔ BLOCKED", "PASS WITH NOTES": "⚠️ PASS WITH NOTES", "PASS": "✅ PASS",
                "NOT STARTED": "⏳ NOT STARTED"}
COVERAGE_ICON = {"pass": "✅", "fail": "❌", "blocked": "⛔"}


def _load_json(path, warnings):
    try:
        return scrub.redact_json(json.loads(Path(path).read_text()))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        warnings.append(f"{Path(path).name} unreadable ({type(e).__name__})")
        return None


def load_run(run_dir):
    """{'plan': {...}, 'runs': [(file name, run dict)], 'warnings': [...]}; missing pieces are simply absent."""
    data, warnings = Path(run_dir) / ".data", []
    plan = _load_json(data / "plan.json", warnings)
    if plan is not None and not isinstance(plan, dict):
        warnings.append("plan.json is not an object")
        plan = None
    runs = []
    for f in sorted(data.glob("run-*.json"), key=lambda x: (x.stat().st_mtime, x.name)):
        r = _load_json(f, warnings)
        if isinstance(r, dict):
            runs.append((f.name, r))
            for key in ("cases", "bugs", "pages", "questions", "not_tested"):  # dropped below: say so, don't hide it
                if r.get(key) is not None and not isinstance(r[key], list):
                    warnings.append(f"{f.name}: `{key}` should be a list (got {type(r[key]).__name__}); "
                                    f"its entries are not in this report")
        elif r is not None:
            warnings.append(f"{f.name} is not an object")
    return {"plan": plan or {}, "runs": runs, "warnings": warnings}


def _list(obj, key):
    v = obj.get(key) if isinstance(obj, dict) else None
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


# Not tested items are grouped in this order. A plain string (older runs) goes under "other".
NOT_TESTED_CATEGORIES = [
    ("safety", "Safety rules (not done on purpose)"),
    ("out_of_scope", "Out of scope for this ticket"),
    ("not_reached", "Not reached (budget, time or route)"),
    ("needs_setup", "Needs a fault, account or data this run lacks"),
    ("heuristic", "Heuristics not run"),
    ("other", "Other"),
]


def _not_tested(obj):
    """[(category key, text)] for run['not_tested']: a string (category other) or {"category", "text"}."""
    out = []
    items = obj.get("not_tested") if isinstance(obj, dict) else None
    for x in items if isinstance(items, list) else []:  # a string is not a list of letters (load_run warns about it)
        if isinstance(x, dict):
            cat = x.get("category")  # a list or dict category can't be looked up in a dict (TypeError)
            cat = cat if isinstance(cat, str) and cat in dict(NOT_TESTED_CATEGORIES) else "other"
            out.append((cat, str(x.get("text") or "")))
        elif isinstance(x, (str, int, float)) and not isinstance(x, bool):  # JSON true is no text ("True")
            out.append(("other", str(x)))
    return [(c, t) for c, t in out if t]


def bug_id(b):
    for f in ("draft_path", "evidence"):
        m = re.search(r"(BUG-\d+)\.\w+$", str(b.get(f) or ""))
        if m:
            return m.group(1)
    return ""


MERGE_RANK = ("NOT_RUN", "BLOCKED", "PASS", "QUESTION", "FAIL")  # weakest first: what a run actually saw beats a skip


def merged_cases(plan, runs, warnings=None):
    """Plan cases with their result from the run JSONs (missing -> NOT_RUN); cases only a run knows are appended.
    Each gets status, actual, notes, bug. When several runs report one case, the most decisive result wins (FAIL over
    QUESTION over PASS over BLOCKED over NOT_RUN; the newest run on a tie), so another route listing a case as
    "not run" never hides its FAIL. A plan case with no id or a repeated one can't get a result: it is left out and
    named in `warnings`."""
    warnings = [] if warnings is None else warnings
    results = {}
    rank = lambda c: MERGE_RANK.index(core.case_status(c.get("status")) or "NOT_RUN")
    for _, r in runs:
        for c in _list(r, "cases"):
            if isinstance(c.get("id"), (str, int)) and c["id"] != "":
                cid = str(c["id"])
                if cid not in results or rank(c) >= rank(results[cid]):
                    results[cid] = c
    out, seen = [], set()
    for c in _list(plan, "cases") + [c for c in results.values() if str(c["id"]) not in
                                       {str(p.get("id")) for p in _list(plan, "cases")}]:
        cid = str(c["id"]) if c.get("id") not in (None, "") else ""  # a numeric id 0 is still an id
        if not cid or cid in seen:
            what = one_line(core.short(c.get("title") or c.get("expected") or "untitled", 60))
            warnings.append(f"plan.json: case '{what}' " + (f"repeats id {cid}" if cid else "has no id")
                            + "; it is left out of this report: give it an id of its own (e.g. TC-99)")
            continue
        seen.add(cid)
        res = results.get(cid, {})
        ac = c.get("ac")  # a case may cover several AC: "AC-1, AC-2" or a list
        ac = ", ".join(str(a) for a in ac if isinstance(a, (str, int))) if isinstance(ac, list) else ac
        out.append({**c, "id": cid, "ac": ac, "status": core.case_status(res.get("status")) or "NOT_RUN",
                    "actual": str(res.get("actual") or ""), "notes": str(res.get("notes") or ""),
                    "bug": str(res.get("bug") or "")})
    return out


def run_bugs(runs):
    """Every bug of every run, once, with id (BUG-n from its draft) and route."""
    out, seen = [], set()
    for _, r in runs:
        route = core.norm_route(r.get("target")) if str(r.get("target") or "").startswith(("/", "http", "#")) else ""
        for b in _list(r, "bugs"):
            b = {**b, "id": bug_id(b), "route": core.norm_route(b.get("route")) or route, "target": route}
            key = b["id"] or (b["route"], core.norm(b.get("title")))
            if key not in seen:
                seen.add(key)
                out.append(b)
    return out


def _case_bugs(c):
    """Bugs a case names in `bug`: BUG-n ids and known bugs' 12-char hashes ("BUG-2", "BUG-2, 3f9a…", "see BUG-2")."""
    t = str(c.get("bug") or "")
    return set(re.findall(r"BUG-\d+", t)) | {h.lower() for h in re.findall(r"\b[0-9a-fA-F]{12}\b", t)}


def _bug_refs(b):
    """What a case may call this bug by: its BUG-n id and, for a known bug, its hash."""
    return {x for x in (str(b.get("id") or ""), str(b.get("hash") or "")[:12].lower()) if x}


def ticket_bugs(key, cases, bugs, known=()):
    """Bugs that belong to one ticket: tagged with it, referenced by one of its cases, or untagged on one of its routes
    (unless a case of another ticket names it: then it is that ticket's bug, not every ticket's on a shared page)."""
    mine = [c for c in cases if c.get("ticket") == key]
    refs = set().union(*map(_case_bugs, mine))
    named = set().union(*map(_case_bugs, cases))
    routes = {core.norm_route(c.get("route")) for c in mine if c.get("route")}
    tickets = {str(c.get("ticket")).strip().upper() for c in cases if c.get("ticket")} | {str(k).strip().upper() for k in known}
    tag = lambda b: str(b.get("ticket") or "").strip().upper()  # 's-1 ' is S-1; 'none' or an unknown key is no tag
    # untagged: on one of the ticket's routes, or found by the run of one (a record page opened from its list page)
    return [b for b in bugs if tag(b) == str(key).strip().upper() or (_bug_refs(b) & refs)
            or (tag(b) not in tickets and (b.get("route") in routes or b.get("target") in routes)
                and not (_bug_refs(b) & named))]


def _serious(b):
    return str(b.get("classification") or "").upper() == "CONFIRMED" and \
        str(b.get("severity") or "").upper() in ("CRITICAL", "HIGH")


def _bug_word(b):
    sev = str(b.get("severity") or "").upper() or "unrated"
    extra = "" if str(b.get("classification") or "").upper() in ("CONFIRMED", "") else \
        f" {str(b['classification']).lower()}"
    name = b.get("id") or ("known bug " if b.get("hash") else "") + core.short(b.get("title"), 40)  # known: re-sighted
    return f"{name} {sev}{extra}"


def _some(parts, n=3):
    return "; ".join(parts[:n]) + (f"; +{len(parts) - n} more" if len(parts) > n else "")


def _clip(s, n):
    """short() cut back to the last word so a number or name is never cut in half."""
    s = " ".join(str(s or "").split())
    if len(s) <= n:
        return s
    cut = s[:n - 1]
    return (cut[:cut.rfind(" ")] if " " in cut[n // 2:] else cut).rstrip(" ,;:") + "…"


def _ac_id(ac):
    """An AC's id as text ('' when missing or not a string or number: a list there must not break the report)."""
    return str(ac["id"]).strip() if isinstance(ac.get("id"), (str, int)) and not isinstance(ac["id"], bool) else ""


def _case_acs(c):
    """The AC ids a case covers: "AC-1" or "AC-1, AC-2"."""
    return {a.strip() for a in re.split(r"[,;]", str(c.get("ac") or "")) if a.strip()}


def verdict(ticket, cases, bugs, started=True, known=()):
    """(verdict, reason) for one ticket: FAIL, BLOCKED, PASS WITH NOTES or PASS (rules in references/report.md);
    NOT STARTED while no test run has reported yet."""
    if not started:
        return "NOT STARTED", "testing hasn't started yet"
    key = ticket.get("key") if isinstance(ticket, dict) else ticket
    mine = [c for c in cases if c.get("ticket") == key]
    its_bugs = ticket_bugs(key, cases, bugs, known)
    st = lambda c: c.get("status") or "NOT_RUN"
    why = lambda c: f": {_clip(c.get('actual') or c.get('notes'), 60)}" if c.get("notes") or c.get("actual") else \
        f": no result reported for {core.norm_route(c['route'])}" if st(c) == "NOT_RUN" and c.get("route") else ""
    fails = [c for c in mine if st(c) == "FAIL"]
    if len(fails) == 1:  # one failure: say what happened (cut at a word, never mid-number: "at 1…" for 131)
        c = fails[0]
        failed = [f"{c['id']} failed" + (f" ({c['ac']})" if c.get("ac") else "") +
                  (f": {_clip(c.get('actual') or c.get('notes'), 50)}" if c.get("actual") or c.get("notes") else "")]
    else:  # several: one entry naming them all; each case's actual is in the case table
        acs = sorted({a for c in fails for a in _case_acs(c)}, key=lambda a: [int(x) if x.isdigit() else x
                                                                             for x in re.split(r"(\d+)", a)])
        ids = [c["id"] for c in fails]
        failed = [f"{len(ids)} cases failed ({', '.join(ids[:6])}{f', +{len(ids) - 6}' if len(ids) > 6 else ''})"
                  + (f" on {', '.join(acs)}" if acs else "")] if fails else []
    serious = [_bug_word(b) for b in its_bugs if _serious(b)]
    if failed or serious:
        return "FAIL", _some(failed + serious)
    if not mine:
        return "BLOCKED", "no test cases"
    stuck = {}  # same reason → one entry naming all its cases, so 12 blocked cases don't repeat one error 12 times
    for c in mine:
        if st(c) in ("BLOCKED", "NOT_RUN"):
            stuck.setdefault(f"{'blocked' if st(c) == 'BLOCKED' else 'not run'}{why(c)}", []).append(c["id"])
    if stuck:
        return "BLOCKED", _some([(f"{ids[0]}" if len(ids) == 1 else f"{len(ids)} cases ({ids[0]}–{ids[-1]})") + f" {r}"
                                 for r, ids in stuck.items()], 2)
    covered = {a for c in mine for a in _case_acs(c)}
    notes = [f"{c['id']} open question" for c in mine if st(c) == "QUESTION"] + \
            [_bug_word(b) for b in its_bugs] + \
            [f"{_ac_id(a)} has no test case" for a in _list(ticket, "acs") if _ac_id(a) and _ac_id(a) not in covered]
    if notes:
        return "PASS WITH NOTES", _some(notes)
    return "PASS", f"all {len(mine)} cases passed"


def cell(v):
    """Text for one table cell or line: `|` escaped, newlines as <br>, and no raw HTML, images or links (an XSS payload,
    `<!--` or `[text](https://…)` typed into a form must show as text, not hide the report or become a hidden-URL link;
    breaking `](` and `]:` stops inline links, images and reference definitions while `[BLOCKING]` stays readable)."""
    return str(v if v is not None else "").replace("\\", "\\\\").replace("|", "\\|").replace("\r", "") \
        .replace("<", "&lt;").replace("](", "]\\(").replace("]:", "]\\:").strip().replace("\n", "<br>")


class Raw(str):
    """Already Markdown (a link): row() puts it in as is."""


def one_line(v):
    """Text for a heading or list line: whitespace and newlines collapsed, `|` escaped."""
    return cell(" ".join(str(v if v is not None else "").split()))


def row(*cols):
    return "| " + " | ".join(c if isinstance(c, Raw) else cell(c) for c in cols) + " |"


def paragraph(v):
    """cell() text that stands alone on its line: a leading ``` or # would fence off or head the rest of the report."""
    s = cell(v)
    return ("\\" if s[:1] in tuple("#`~>-+*=_") else "") + s


def summary_text(v):
    """Text for a <details><summary> line: that line is an HTML block, where Markdown escapes (`\\|`) show as typed,
    so it gets HTML escaping only (no tags, entities as text) and one line."""
    import html
    return html.escape(" ".join(str(v if v is not None else "").split()), quote=False)


def _counts(cases):
    p = sum(c["status"] == "PASS" for c in cases)
    f = sum(c["status"] == "FAIL" for c in cases)
    return f"{p} / {f} / {len(cases) - p - f}"


def _bug_link(bid, run_dir):
    """A link to bugs/BUG-n.md when bid is exactly BUG-n and the draft exists; anything else as escaped text."""
    bid = str(bid or "")
    if re.fullmatch(r"BUG-\d+", bid) and (Path(run_dir) / "bugs" / f"{bid}.md").exists():
        return f"[{bid}](bugs/{bid}.md)"
    return one_line(bid)


def _ticket_title(t):
    return " ".join(str(f"{t.get('key')} — {t['summary']}" if t.get("summary") else t.get("key")).split())


def _ac_lines(t, cases, with_status):
    out = []
    for ac in _list(t, "acs"):
        mine = [c for c in cases if _ac_id(ac) in _case_acs(c)]
        ids = f" ({one_line(', '.join(c['id'] for c in mine))})" if mine else ""
        if not mine:
            state = " — not covered"
        elif with_status:
            state = " — " + STATUS_ICON[next(s for s in STATUS_RANK if any(c["status"] == s for c in mine))]
        else:
            state = ""
        out.append(f"- {one_line(_ac_id(ac))} {cell(ac.get('text'))}{state}{ids}".replace("  ", " "))
    return out


def _plan_lines(t):
    plan, out = t.get("plan") if isinstance(t.get("plan"), dict) else {}, []
    for key, label in (("scope", "Scope"), ("approach", "Approach"), ("risks", "Risks"),
                       ("out_of_scope", "Out of scope"), ("test_data", "Test data")):
        v = plan.get(key)
        if isinstance(v, list) and v:
            out.append(f"- **{label}:**")
            out += [f"  - {cell(x)}" for x in v]
        elif v and not isinstance(v, (list, dict)):
            out.append(f"- **{label}:** {cell(v)}")
    return out


CASE_RESULT_WORD = {"PASS": "PASS", "FAIL": "FAIL", "BLOCKED": "BLOCKED", "NOT_RUN": "NOT RUN", "QUESTION": "QUESTION"}


def _evidence_file(run_dir, cid):
    """evidence/TC-<n>.png (relative, URL-quoted) when the case image exists, else ''. Only TC-style ids."""
    from urllib.parse import quote
    cid = str(cid or "")
    if not re.fullmatch(r"[A-Za-z]+-\d+", cid):
        return ""
    name = f"{cid}.png"
    return f"evidence/{quote(name)}" if (Path(run_dir) / "evidence" / name).is_file() else ""


def _case_table(cases, run_dir, with_status):
    """Compact table: ID, AC, case, status, actual, evidence. Steps and expected are in _case_details."""
    pri = any(c.get("priority") for c in cases)
    routes = len({str(c.get("route")).strip() for c in cases if c.get("route")}) > 1
    head = ["ID"] + ["Pri"] * pri + ["AC"] + ["Page"] * routes + ["Case"] + \
        (["Status", "Actual", "Evidence"] if with_status else [])
    out = [row(*head), "|" + "---|" * len(head)]
    for c in cases:
        cols = [c["id"]] + [c.get("priority")] * pri + [c.get("ac")] + [c.get("route")] * routes + [c.get("title")]
        if with_status:
            actual = Raw(" ".join(x for x in (cell(core.short(c.get("actual") or c.get("notes"), 160)),
                                             _bug_link(c["bug"], run_dir) if c.get("bug") else "") if x))
            img = _evidence_file(run_dir, c["id"])
            ev = Raw(f"[image]({img})" + (" (not highlighted)" if c.get("annotated") is False else "")) if img else "—"
            cols += [STATUS_ICON[c["status"]], actual, ev]
        out.append(row(*cols))
    return out


def _case_details(cases, run_dir, with_status):
    """One folded block per case: steps, expected, actual and notes, with the case image inside. Non-pass cases open."""
    out = []
    for c in cases:
        steps = c.get("steps")
        if isinstance(steps, list):
            step_lines = [f"{i}. {cell(x)}" for i, x in enumerate(steps, 1)]
        else:
            step_lines = [paragraph(steps)] if steps else []
        non_pass = with_status and c["status"] != "PASS"
        head = " ".join(x for x in (c["id"], "—", str(c.get("title") or "")) if x)  # status is in the table
        out += [f"<details{' open' if non_pass else ''}><summary>{summary_text(head)}</summary>", ""]
        out += ["**Steps**", ""] + step_lines + [""] if step_lines else []
        out += [f"**Expected:** {cell(c.get('expected'))}", ""] if c.get("expected") else []
        if with_status:
            out += [f"**Actual:** {cell(c.get('actual'))}", ""] if c.get("actual") else []
            out += [f"**Notes:** {cell(c.get('notes'))}", ""] if c.get("notes") else []
        img = _evidence_file(run_dir, c["id"]) if with_status else ""
        if img:
            out += [f"![{c['id']}]({img})", ""]
        out += ["</details>", ""]
    return out


def _case_block(cases, run_dir, with_status):
    """Table, then the per-case details folded in one block."""
    non_pass = with_status and any(c["status"] != "PASS" for c in cases)
    return _case_table(cases, run_dir, with_status) + [
        "", f"<details{' open' if non_pass else ''}><summary>Steps, expected results and evidence ({len(cases)} cases)"
        "</summary>", ""] + _case_details(cases, run_dir, with_status) + ["</details>", ""]


def _header(run_dir, kind, plan, runs):
    env = plan.get("env") if isinstance(plan.get("env"), dict) else {}
    renvs = [r["env"] for _, r in runs if isinstance(r.get("env"), dict)]
    renv = renvs[0] if renvs else {}
    # plan.json is written before C0 picks the browser: what the runs used (e.g. the MCP fallback) wins
    used = sorted({str(e["browser"]) for e in renvs if e.get("browser")})
    m = re.match(r"\d{4}-\d{2}-\d{2}", Path(run_dir).name)
    date = str(plan.get("created") or "")[:10] or (m.group(0) if m else "")
    builds = sorted({str(r.get("build")) for _, r in runs if r.get("build")})
    bits = [kind, date, str(env.get("url") or renv.get("url") or ""),
            "build " + (", ".join(builds) if builds else "not run yet"),
            "browser " + (", ".join(used) or str(env.get("browser") or "?"))]
    return [f"# QA report: {one_line(plan.get('title') or Path(run_dir).name)}",
            " · ".join(one_line(b) for b in bits if b)]


def _bug_section(bugs, cases, tickets, run_dir, heading="### Bugs to file"):
    from urllib.parse import quote
    out = [heading, row("Bug", "Severity", "Class", "Title", "Page", "Ticket", "Evidence"), "|---|---|---|---|---|---|---|"]
    for b in bugs:
        bid = b.get("id") or ""
        owners = [str(b["ticket"])] if b.get("ticket") else [str(t) for t in tickets if b in ticket_bugs(t, cases, [b], tickets)]
        png = Path(run_dir) / "evidence" / f"{bid}.png"
        ev = Path(str(b.get("evidence") or ""))
        img = f"evidence/{quote(png.name)}" if bid and png.is_file() else \
            f"evidence/{quote(ev.name)}" if b.get("evidence") and (Path(run_dir) / "evidence" / ev.name).is_file() else ""
        # (is_file: evidence '.', '..' or '/' names a folder, and a link to it is broken)
        evidence = f"[image]({img})" + (" (not highlighted)" if b.get("annotated") is False else "") if img else "—"
        link = _bug_link(bid, run_dir) or (one_line(f"known {str(b.get('hash'))[:12]}") if b.get("hash") else "—")
        out.append(row(Raw(link), b.get("severity"), b.get("classification"), core.short(b.get("title"), 120), b.get("route"),
                       ", ".join(owners), Raw(evidence)))
    return out


def _notes_section(runs):
    out = ["## Exploration notes", "Heuristics: ✅ checked, no problem · ❌ problem found · ⛔ could not be checked", ""]
    for name, r in runs:
        route = r.get("target") or next((p.get("route") for p in _list(r, "pages")), "") or name
        cov = {}
        for p in _list(r, "pages"):
            for c in _list(p, "coverage"):
                if c.get("heuristic"):
                    h, res = core.heuristic_id(c["heuristic"]), core.coverage_result(c.get("result"))
                    order = ("fail", "blocked", "pass")
                    if h not in cov or (res in order and (cov[h] not in order or order.index(res) < order.index(cov[h]))):
                        cov[h] = res
        icons = ", ".join(f"{h} {COVERAGE_ICON.get(v, v)}" for h, v in cov.items())
        title = summary_text(f"{route} ({name}: {r.get('status') or '?'}, build {r.get('build') or '?'})" +
                             (f" — {icons}" if icons else ""))
        out += [f"<details><summary>{title}</summary>", ""]
        if r.get("notes") or r.get("summary"):
            out += [paragraph(core.short(r.get("notes") or r.get("summary"), 1500)), ""]
        out += ["</details>", ""]
    return out


# ---------- questions: plan, runs and QUESTION cases, merged; a string is open, {"text","status","answer"} as written ----
def _q_items(obj, label):
    out = []
    raw = obj.get("questions") if isinstance(obj, dict) else None
    for x in raw if isinstance(raw, list) else []:
        if isinstance(x, dict):
            text, status, ans = str(x.get("text") or ""), x.get("status"), str(x.get("answer") or "")
        elif isinstance(x, (str, int, float)) and not isinstance(x, bool):
            text, status, ans = str(x), None, ""
        else:
            continue
        text = " ".join(text.split())
        if text:
            out.append({"text": text, "resolved": str(status or "").lower() == "resolved",
                        "answer": " ".join(ans.split()), "where": label})
    return out


def _merge_questions(items):
    """Equal text (case and spaces ignored) becomes one entry listing every place it came from. Open wins over resolved."""
    merged, index = [], {}
    for q in items:
        k = q["text"].lower()
        if k not in index:
            index[k] = {"text": q["text"], "resolved": q["resolved"], "answer": q["answer"], "where": []}
            merged.append(index[k])
        m = index[k]
        m["resolved"] = m["resolved"] and q["resolved"]
        m["answer"] = m["answer"] or q["answer"]
        if q["where"] and q["where"] not in m["where"]:
            m["where"].append(q["where"])
    return merged


def _q_rank(q):
    return 0 if "[BLOCKING]" in q["text"] else 1 if "[HIGH]" in q["text"] else 2


def _q_bullet(q, show_from=True):
    labels = ", ".join(str(w) for w in q["where"])
    return f"- {cell(core.short(q['text'], 300))}" + (f" _(from {cell(labels)})_" if labels and show_from else "")


def _safe_key(key):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", str(key or "")))


# ---------- Jira pass comment: a paste-ready draft, never posted (passed on, fixed, one line per case)
def _release_names(templates, fix):
    """(names, gap): the config's releaseTemplates with {version} filled from the fix version (3.5 -> 3.5.0)."""
    temps = [" ".join(t.split()) for t in templates if isinstance(t, str) and t.strip()] if isinstance(templates, list) else []
    if not temps:
        return ["<release name>"], "set releaseTemplates in the qa-sdet config"
    if not fix:
        return ["<release name>"], "the ticket has no single fix version (plan.json fix_version)"
    v = fix + ".0" if re.fullmatch(r"\d+\.\d+", fix) else fix
    return [t.replace("{version}", v) for t in temps], ""


def _jira_draft(t, verdict_v, why, mine, run_dir, cfg, builds):
    fix = t.get("fix_version")
    # one line each in the draft: newlines collapsed; JSON true is no version
    fix = " ".join(str(fix).split()) if isinstance(fix, (str, int, float)) and not isinstance(fix, bool) else ""
    fix = fix or None
    names, gap = _release_names(cfg.get("releaseTemplates"), fix)
    fixed = " ".join(str(t.get("fixed_note") or "").split())
    route_of = lambda c: " ".join(str(c.get("route") or "").split())
    routes = []
    for c in mine:
        if route_of(c) not in routes:
            routes.append(route_of(c))
    shots, placeholders = [], ([f"release names ({gap})"] if gap else []) + ([] if fixed else ["the fixed note"])
    for r in routes:
        pick = next((c for c in mine if route_of(c) == r and c.get("status") == "PASS"
                     and _evidence_file(run_dir, c["id"])), None)
        if pick:
            shots.append((r or "test page", Path(run_dir) / "evidence" / f"{pick['id']}.png"))
        else:
            placeholders.append(f"screenshot for {r or 'the test page'}")
    lines = ["CHECK BEFORE POSTING (not part of the comment)"]
    if not builds or "unknown" in builds:  # one page on an unknown build is enough to check
        shown = (fix + ".0" if re.fullmatch(r"\d+\.\d+", fix) else fix) if fix else "<version>"
        lines.append(f"- Build: unknown in this run. Confirm the QA app runs {shown} before you post.")
    lines += [f"- Paste this image: {path} ({label})" for label, path in shots]
    if not shots:
        lines.append("- No screenshot for this ticket yet. Add one before you post.")
    if placeholders:
        lines.append("- Placeholders to fill: " + "; ".join(placeholders))
    passed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    lines += ["", "---------- paste below ----------", "", f"Passed on {passed}", "",
              f"Fixed: {fixed or '<write one sentence: what was fixed>'}", ""]
    lines += [f"{c['id']} - {CASE_RESULT_WORD[c['status']]}: {' '.join(str(c.get('title') or '').split())}" for c in mine]
    if verdict_v == "PASS WITH NOTES":
        lines += ["", f"Notes: {why}"]
    return "\n".join(lines) + "\n"


def _config_dict():
    try:
        data = json.loads(core.CONFIG.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def render_report_parts(run_dir, cfg=None):
    """(REPORT.md text, {draft file name: text}). Drafts exist for PASS and PASS WITH NOTES tickets only."""
    run_dir = Path(run_dir)
    d = load_run(run_dir)
    plan, runs = d["plan"], d["runs"]
    kind = str(plan.get("kind") or (runs[0][1].get("mode") if runs else "") or "explore")
    tickets = [t for t in _list(plan, "tickets") if isinstance(t.get("key"), str) and t["key"]]
    keys = [t["key"] for t in tickets]
    cases, bugs = merged_cases(plan, runs, d["warnings"]), run_bugs(runs)
    with_status = kind != "plan"
    cfg = _config_dict() if cfg is None else cfg
    drafts, vmap = {}, {}
    if tickets and with_status:
        for t in tickets:
            v, why = verdict(t, cases, bugs, bool(runs), keys)
            vmap[t["key"]] = (v, why)
            mine = [c for c in cases if c.get("ticket") == t["key"]]
            ids = {c["id"] for c in mine}  # the builds of the runs that tested this ticket, not the whole folder's
            builds = sorted({str(r.get("build")) for _, r in runs if r.get("build")
                             and any(str(x.get("id")) in ids for x in _list(r, "cases"))})
            if v in ("PASS", "PASS WITH NOTES") and mine and _safe_key(t["key"]):
                drafts[f"jira-comment-{t['key']}.md"] = _jira_draft(t, v, why, mine, run_dir, cfg, builds)
    draft_of = lambda key: f"jira-comment-{key}.md" if f"jira-comment-{key}.md" in drafts else ""

    out = _header(run_dir, kind, plan, runs) + [""]
    if not plan and not runs:
        out += ["_Nothing recorded yet: no plan and no test run in this folder._", ""]
    if tickets and kind == "plan":
        out += ["## Summary", row("Ticket", "ACs", "Cases"), "|---|---|---|"]
        out += [row(_ticket_title(t), len(_list(t, "acs")), sum(c.get("ticket") == t["key"] for c in cases))
                for t in tickets]
    elif tickets and len(tickets) > 1:
        out += ["## Summary", row("Ticket", "Verdict", "Why", "Cases (pass/fail/other)", "Bugs"), "|---|---|---|---|---|"]
        for t in tickets:
            v, why = vmap.get(t["key"]) or ("NOT STARTED", "")
            mine = [c for c in cases if c.get("ticket") == t["key"]]
            out.append(row(_ticket_title(t), VERDICT_ICON[v], why, _counts(mine), len(ticket_bugs(t["key"], cases, bugs, keys))))
    elif tickets and with_status:
        t = tickets[0]
        v, why = vmap.get(t["key"]) or ("NOT STARTED", "testing hasn't started yet")
        out += [f"**Verdict: {VERDICT_ICON[v]}** — {one_line(why)}"]
    elif runs:
        out += ["## Summary", "**Status:** " + "; ".join(
            one_line(f"{r.get('target') or name} {r.get('status') or '?'}") for name, r in runs)
            + f" · {len(bugs)} bug(s)" + (f" · cases {_counts(cases)} (pass/fail/other)" if cases else "")]

    # questions: plan, runs and QUESTION cases, one list
    q_items = _q_items(plan, "test plan") + [q for name, r in runs for q in _q_items(r, str(r.get("target") or name))] + \
        [{"text": f"{c['id']}: {' '.join(str(c.get('notes') or c.get('actual') or c.get('title') or '').split())}",
          "resolved": False, "answer": "", "where": str(c.get("route") or "test cases")}
         for c in cases if c["status"] == "QUESTION"]
    merged = _merge_questions(q_items)
    open_qs = sorted([q for q in merged if not q["resolved"]], key=_q_rank)
    resolved_qs = [q for q in merged if q["resolved"]]
    attention = [c for c in cases if c["status"] in ("FAIL", "BLOCKED", "NOT_RUN")]
    if runs:
        n_img = sum(bool(_evidence_file(run_dir, c["id"])) for c in cases)
        out += [f"{len(bugs)} bug(s) · {len(open_qs)} open question(s) · "
                f"{len([c for c in cases if c['status'] in ('FAIL', 'BLOCKED', 'NOT_RUN', 'QUESTION')])} "
                f"case(s) need attention · {n_img} evidence image(s)"]
    if len(tickets) == 1 and draft_of(tickets[0]["key"]):
        out += [f"Jira comment draft: [{draft_of(tickets[0]['key'])}]({draft_of(tickets[0]['key'])})"]
    out.append("")

    if not runs and open_qs:  # plan only: no run to act on, so the questions are the list (resolved: folded below)
        out += ["## Questions"] + [_q_bullet(q, show_from=False) for q in open_qs] + [""]
    # the things to act on, first
    if runs:
        action = []
        if bugs:
            action += _bug_section(bugs, cases, keys, run_dir, heading=f"### Bugs to file ({len(bugs)})") + [""]
        if open_qs:
            action += [f"### Open questions ({len(open_qs)})"] + [_q_bullet(q) for q in open_qs] + [""]
        if attention:
            action += [f"### Failed, blocked or not-run cases ({len(attention)})"] + [
                f"- {cell(c['id'])} {STATUS_ICON[c['status']]}" + (f" ({cell(c['ac'])})" if c.get("ac") else "") +
                (f" — {cell(core.short(c.get('actual') or c.get('notes'), 160))}" if (c.get("actual") or c.get("notes")) else "")
                + (f" · {cell(c['ticket'])}" if c.get("ticket") else "") for c in attention] + [""]
        out += ["## Needs your action"] + (action or ["Nothing needs your action.", ""])

    for t in tickets:
        mine = [c for c in cases if c.get("ticket") == t["key"]]
        out.append(f"## {one_line(_ticket_title(t))}")
        if with_status and len(tickets) > 1:
            v, why = vmap.get(t["key"]) or ("NOT STARTED", "testing hasn't started yet")
            out.append(f"**Verdict: {VERDICT_ICON[v]}** — {one_line(why)}")
        if len(tickets) > 1 and draft_of(t["key"]):  # one ticket: the link is in the top block
            out.append(f"**Jira comment draft:** [{draft_of(t['key'])}]({draft_of(t['key'])})")
        if _list(t, "acs"):
            out += ["### Acceptance criteria"] + _ac_lines(t, mine, with_status)
        if mine:
            out += ["### Test cases"] + _case_block(mine, run_dir, with_status)
        if _plan_lines(t):
            out += ["<details><summary>Test plan</summary>", ""] + _plan_lines(t) + ["", "</details>", ""]
        out.append("")
    other = [c for c in cases if c.get("ticket") not in keys]
    if other:
        out += ["## Test cases" if not tickets else "## Other test cases"] + _case_block(other, run_dir, with_status) + [""]

    not_tested = [item for _, r in runs for item in _not_tested(r)]
    if not_tested:
        out += ["## Not tested"]
        for key, label in NOT_TESTED_CATEGORIES:
            group = [t for k, t in not_tested if k == key]
            if group:
                out += [f"<details><summary>{label} ({len(group)})</summary>", ""] + \
                    [f"- {cell(core.short(t, 300))}" for t in group] + ["", "</details>", ""]
    if resolved_qs:
        out += [f"<details><summary>Resolved questions ({len(resolved_qs)})</summary>", ""]
        out += [f"- {cell(core.short(q['text'], 300))}" + (f" — answer: {cell(core.short(q['answer'], 300))}" if q["answer"] else "")
                for q in resolved_qs] + ["", "</details>", ""]
    if runs:
        out += _notes_section(runs)
    if d["warnings"]:
        out += ["## Problems reading this folder"] + [f"- {one_line(w)}" for w in d["warnings"]] + [""]
    return "\n".join(out).rstrip() + "\n", drafts


def render_report(run_dir):
    return render_report_parts(run_dir)[0]


def run_summary(run_dir):
    """(date, kind, verdict text, bug count) for the qa-out/README.md index."""
    d = load_run(run_dir)
    plan, runs = d["plan"], d["runs"]
    kind = str(plan.get("kind") or (runs[0][1].get("mode") if runs else "") or "explore")
    cases, bugs = merged_cases(plan, runs), run_bugs(runs)
    tickets = [t for t in _list(plan, "tickets") if t.get("key")]
    if kind == "plan":
        verdicts = f"plan only ({len(cases)} cases)"
    elif tickets:
        verdicts = ", ".join(f"{t['key']} {VERDICT_ICON[verdict(t, cases, bugs, bool(runs), [x['key'] for x in tickets])[0]]}" for t in tickets)
    else:
        verdicts = ", ".join(str(r.get("status") or "?") for _, r in runs) or "not run yet"
    m = re.match(r"\d{4}-\d{2}-\d{2}", Path(run_dir).name)
    return (m.group(0) if m else str(plan.get("created") or "")[:10]), kind, verdicts, len(bugs)


def render_index(qa_out):
    """qa-out/README.md: every run folder with a REPORT.md or a plan, newest first."""
    from urllib.parse import quote
    qa_out = Path(qa_out)
    runs = [p for p in qa_out.iterdir() if p.is_dir() and not p.is_symlink()
            and ((p / "REPORT.md").exists() or (p / ".data" / "plan.json").exists())]
    when = lambda p: max(x.stat().st_mtime for x in (p, p / "REPORT.md", p / ".data" / "plan.json") if x.exists())
    rows = []
    for p in runs:
        try:
            date, kind, verdicts, n = run_summary(p)
        except Exception as e:  # one odd folder must not break the index
            m = re.match(r"\d{4}-\d{2}-\d{2}", p.name)
            date, kind, verdicts, n = (m.group(0) if m else ""), "?", f"unreadable ({type(e).__name__})", "?"
        name = one_line(p.name).replace("[", "\\[").replace("]", "\\]")
        rows.append(((date, when(p)), row(date, Raw(f"[{name}]({quote(p.name, safe='+')}/REPORT.md)"), kind, verdicts, n)))
    rows.sort(key=lambda x: x[0], reverse=True)
    return "\n".join(["# QA runs", "Newest first. Open a run's REPORT.md for test cases, bugs and questions.", "",
                      row("Date", "Run", "Kind", "Verdict", "Bugs"), "|---|---|---|---|---|"] + [r for _, r in rows]) + "\n"


def write_report(run_dir):
    run_dir = Path(run_dir).resolve()
    if not run_dir.is_dir() or run_dir.parent.name != "qa-out":
        raise core.BadInput(f"report: not a run folder (qa-out/<run>/): {run_dir}")
    try:
        text, drafts = render_report_parts(run_dir)
    except Exception as e:  # never fail: say what went wrong in the report itself
        text, drafts = (f"# QA report: {one_line(run_dir.name)}\n\n_Could not render this report: {type(e).__name__}: "
                        f"{one_line(core.short(e, 200))}. Check the files in .data/._\n"), None
    core._write(run_dir / "REPORT.md", text)
    if drafts is not None:  # Jira drafts: written with the report; a draft no longer due is removed (a failed render keeps them)
        for name, body in drafts.items():
            core._write(run_dir / name, body)
        for old in run_dir.glob("jira-comment-*.md"):
            if old.name not in drafts:
                old.unlink(missing_ok=True)
    # Two reports at once (two sessions, or report + hook): without the lock the later index write could be one
    # rendered before the other run's REPORT.md existed, and that run would vanish from the index.
    fd = os.open(run_dir.parent, os.O_RDONLY) if core.fcntl else None  # the qa-out folder itself: no lock file left behind
    try:
        if fd is not None:
            core.fcntl.flock(fd, core.fcntl.LOCK_EX)
        core._write(run_dir.parent / "README.md", render_index(run_dir.parent))
    finally:
        if fd is not None:
            os.close(fd)
    return run_dir / "REPORT.md"


# ---------- inbox: Jira tickets assigned to me, NEW ones flagged ----------
def _name(v):
    return str(v.get("name") or v.get("value") or "") if isinstance(v, dict) else str(v or "")


def inbox_items(data):
    """acli/Jira MCP search JSON (a list, or {issues|workItems|values: [...]}) -> [{key, summary, status, priority, updated}]."""
    if isinstance(data, dict):
        data = next((data[k] for k in ("issues", "workItems", "values") if isinstance(data.get(k), list)), None)
    if not isinstance(data, list):
        raise core.BadInput("inbox: expected a list of Jira items (or an object with issues / workItems / values)")
    out = []
    for it in data:
        if not isinstance(it, dict) or not ops.KEY_RE.fullmatch(str(it.get("key") or "").upper()):
            continue
        f = it.get("fields") if isinstance(it.get("fields"), dict) else {}
        get = lambda k: f.get(k) if f.get(k) is not None else it.get(k)
        out.append({"key": str(it["key"]).upper(), "summary": core.short(" ".join(str(get("summary") or "").split()), 100),
                    "status": _name(get("status")), "priority": _name(get("priority")), "updated": str(get("updated") or "")})
    if data and not out:  # another JSON shape must not read as "no open tickets"
        raise core.BadInput(f"inbox: none of the {len(data)} items has a Jira key (\"key\": \"STUDIO-1234\"); ask the search for "
                       "the key field")
    return out


def inbox(con, project, data, prefix=None, today=None):
    """Markdown for INBOX.md (also printed); remembers every key in meta inbox_seen."""
    out = ops._project(project) / "qa-out"  # a typo must not create a stray folder
    items = list({x["key"]: x for x in inbox_items(data)}.values())  # a key listed twice (paged search) is one ticket
    try:
        old = [k for k in json.loads(core.meta(con, "inbox_seen") or "[]") if isinstance(k, str)]
    except (ValueError, TypeError):
        old = []
    seen = set(old)
    for x in items:
        x["new"] = x["key"] not in seen
    items.sort(key=lambda x: x["updated"], reverse=True)
    items.sort(key=lambda x: not x["new"])  # stable: NEW first, each group by updated desc
    new = [x for x in items if x["new"]]
    lines = [f"# Jira inbox", f"{today or datetime.now().strftime('%Y-%m-%d')} · {len(items)} open, {len(new)} new", ""]
    if items:
        lines += [row("New", "Key", "Summary", "Status", "Priority", "Updated"), "|---|---|---|---|---|---|"]
        lines += [row("NEW" if x["new"] else "", x["key"], x["summary"], x["status"], x["priority"], x["updated"][:10])
                  for x in items]
    else:
        lines.append("No open tickets assigned to you.")
    pick = [x["key"] for x in (new or items[:3])]
    p = str(prefix or "").strip().upper().rstrip("-")
    nums = [k[len(p) + 1:] if p and k.startswith(p + "-") else k for k in pick]
    lines += ["", f"Next: /qa-sdet:qa-ticket {' '.join(nums)}" if nums else "Next: nothing to test"]
    text = "\n".join(lines) + "\n"
    out.mkdir(exist_ok=True)
    core._write(out / "INBOX.md", text)
    # remembered only once INBOX.md is written (a failed write keeps them NEW); today's keys are always kept, older
    # ones up to 5000 (a cap on sorted keys dropped the alphabetically last, so a big inbox stayed NEW forever)
    current = {x["key"] for x in items}
    with con:
        con.execute("BEGIN IMMEDIATE")  # re-read under the write lock: keys another inbox saved meanwhile stay seen
        try:
            old = [k for k in json.loads(core.meta(con, "inbox_seen") or "[]") if isinstance(k, str)]
        except (ValueError, TypeError):
            pass
        keep = [x["key"] for x in items] + [k for k in old if k not in current][:max(0, 5000 - len(items))]
        core.set_meta(con, "inbox_seen", json.dumps(keep))
    return text
