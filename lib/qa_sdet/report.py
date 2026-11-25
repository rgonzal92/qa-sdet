"""REPORT.md, the Jira draft, the run index and `inbox`."""
import fcntl
import html
import json
import os
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
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
        return scrub.redact_json(json.loads(Path(path).read_text(encoding="utf-8-sig")))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError) as e:
        warnings.append(f"{Path(path).name} unreadable ({type(e).__name__})")
        return None


def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def load_run(run_dir):
    """{'plan': {...}, 'runs': [(file name, run dict)], 'warnings': [...]}; missing pieces are simply absent."""
    data, warnings = Path(run_dir) / ".data", []
    plan = _load_json(data / "plan.json", warnings)
    if plan is not None and not isinstance(plan, dict):
        warnings.append("plan.json is not an object")
        plan = None
    runs = []
    for f in sorted(data.glob("run-*.json"), key=lambda x: (_mtime(x), x.name)):
        if f.is_symlink() and not f.exists():
            warnings.append(f"{f.name} unreadable (FileNotFoundError)")
            continue
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


def _plan_tickets(plan):
    """The plan's tickets that can get a verdict: those with a text key."""
    return [t for t in _list(plan, "tickets") if isinstance(t.get("key"), str) and t["key"]]


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
    named in `warnings`, and the case kept under that id gets `id_tickets` (every ticket using the id: verdict()
    blocks them)."""
    warnings = [] if warnings is None else warnings
    valid_id = lambda v: isinstance(v, (str, int, float)) and not isinstance(v, bool) and v != ""  # as core.txt
    results = {}
    rank = lambda c: MERGE_RANK.index(core.case_status(c.get("status")) or "NOT_RUN")
    for _, r in runs:
        for c in _list(r, "cases"):
            if valid_id(c.get("id")):
                cid = str(c["id"])
                if cid not in results or rank(c) >= rank(results[cid]):
                    results[cid] = c
    out, seen = [], set()
    plan_cases = _list(plan, "cases")
    plan_ids = {str(p["id"]) for p in plan_cases if valid_id(p.get("id"))}
    keys = [t["key"] for t in _plan_tickets(plan)]
    canon = {k.strip().upper(): k for k in keys}
    repeated = {}  # id -> the tickets of every plan case under it: which one a result belongs to can't be told
    for n, c in enumerate(plan_cases + [c for c in results.values() if str(c["id"]) not in plan_ids]):
        cid = str(c["id"]) if valid_id(c.get("id")) else ""  # a numeric id 0 is still an id; bool/list is not
        tk = c.get("ticket")  # its plan ticket, however the key is written; every verdict matches on the exact key
        if isinstance(tk, str) and tk.strip():
            tk = canon.get(tk.strip().upper(), tk)
        elif n < len(plan_cases) and len(keys) == 1 and not str(tk or "").strip():
            tk = keys[0]  # a one-ticket plan: a case naming no ticket is that ticket's (an explorer's own case isn't)
        if not cid or cid in seen:
            what = one_line(core.short(c.get("title") or c.get("expected") or "untitled", 60))
            warnings.append(f"plan.json: case '{what}' " + (f"repeats id {cid}" if cid else "has no id")
                            + "; it is left out of this report: give it an id of its own (e.g. TC-99)")
            if cid:  # a ticket that isn't text (a list) is no plan ticket's: None, so the set can hold it
                repeated.setdefault(cid, set()).add(tk if isinstance(tk, str) else None)
            continue
        seen.add(cid)
        res = results.get(cid, {})
        ac = c.get("ac")  # a case may cover several AC: "AC-1, AC-2" or a list
        ac = ", ".join(str(a) for a in ac if isinstance(a, (str, int)) and not isinstance(a, bool)) if isinstance(ac, list) else ac
        merged = {**c, "id": cid, "ac": ac, "ticket": tk, "status": core.case_status(res.get("status")) or "NOT_RUN",
                  "actual": core._s(res.get("actual")), "notes": core._s(res.get("notes")),
                  "bug": core._s(res.get("bug"))}
        if "annotated" in res:
            merged["annotated"] = res["annotated"]
        out.append(merged)
    for m in out:
        if m["id"] in repeated:
            m["id_tickets"] = repeated[m["id"]] | {m["ticket"] if isinstance(m["ticket"], str) else None}
    return out


def run_bugs(runs, warnings=None):
    """Every bug of every run, once, with id (BUG-n from its draft) and route. Two different bugs under one BUG-n
    (two explorers numbered alike) are both kept and named in `warnings`."""
    out, seen = [], {}
    for name, r in runs:
        route = core.norm_route(r.get("target")) if str(r.get("target") or "").startswith(("/", "http", "#")) else ""
        for b in _list(r, "bugs"):
            b = {**b, "id": bug_id(b), "route": core.norm_route(b.get("route")) or route, "target": route}
            key = b["id"] or (b["route"], core.norm(b.get("title")))
            first = seen.get(key)
            if first is None:
                seen[key] = b
                out.append(b)
            elif b["id"] and (first["route"], core.norm(first.get("title"))) != (b["route"], core.norm(b.get("title"))):
                out.append(b)
                if warnings is not None:
                    warnings.append(f"{name}: {b['id']} is also the id of another bug ({core.short(first.get('title'), 50)}); "
                                    f"one draft file can't hold both: both are listed, check bugs/{b['id']}.md")
    return out


def _case_bugs(c):
    """Bugs a case names in `bug`: BUG-n ids and known bugs' 12-char hashes ("BUG-2", "BUG-2, 3f9a…", "see BUG-2")."""
    t = str(c.get("bug") or "")
    return set(re.findall(r"BUG-\d+", t)) | {h.lower() for h in re.findall(r"\b[0-9a-fA-F]{12}\b", t)}


def _bug_refs(b):
    """What a case may call this bug by: its BUG-n id and, for a known bug, its hash."""
    m = re.match(r"#?\s*([0-9a-fA-F]{4,})\b", str(b.get("hash") or "").strip())
    h = m.group(1)[:12].lower() if m else ""
    return {x for x in (str(b.get("id") or ""), h) if x}


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


def _bug_owners(b, cases, tickets):
    """The plan tickets a bug counts for (as their verdicts count it), else the ticket it names (not 'none')."""
    return [str(t) for t in tickets if b in ticket_bugs(t, cases, [b], tickets)] or (
        [str(b["ticket"])] if b.get("ticket") and str(b["ticket"]).strip().lower() != "none" else [])


def _first_word(v):
    """'confirmed' from 'Confirmed (2/2)', 'high' from 'High ': agents add words to the value."""
    return (core.norm(v).split() or [""])[0]


def _serious(b):
    return _first_word(b.get("classification")) == "confirmed" and _first_word(b.get("severity")) in ("critical", "high")


def _bug_word(b):
    sev = " ".join(str(b.get("severity") or "").split()).upper() or "unrated"
    extra = "" if _first_word(b.get("classification")) in ("confirmed", "") else \
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


def _ac_key(a):
    """An AC id as compared: case and spaces don't count ('ac-1 ' is AC-1)."""
    return re.sub(r"\s+", "", str(a)).upper()


def _case_acs(c):
    """The AC ids a case covers, as _ac_key gives them: "AC-1" or "AC-1, AC-2"."""
    return {_ac_key(a) for a in re.split(r"[,;]", str(c.get("ac") or "")) if a.strip()}


def _blocked_runs(cases, runs):
    """[(file name, run)] of the runs that ended BLOCKED on one of these cases' pages or reporting one of them: their
    PASS results can't be trusted. The one rule for verdict() and for what a partial --retest must re-run."""
    ids, routes = {c["id"] for c in cases}, {core.norm_route(c.get("route")) for c in cases if c.get("route")}
    return [(name, r) for name, r in runs if _first_word(r.get("status")) == "blocked"
            and (core.norm_route(r.get("target")) in routes or any(str(x.get("id")) in ids for x in _list(r, "cases")))]


def verdict(ticket, cases, bugs, started=True, known=(), runs=(), prior_passed=(), questions=()):
    """(verdict, reason) for one ticket: FAIL, BLOCKED, PASS WITH NOTES or PASS (rules: README, "Reading REPORT.md");
    NOT STARTED while no test run has reported yet. runs: [(file name, run)], for runs that ended BLOCKED.
    prior_passed: cases from an earlier build not re-run in this partial retest. questions: the plan's, as _q_items gives
    them: an open [BLOCKING] one tagged with this ticket, or with no plan ticket, is a note."""
    if not started:
        return "NOT STARTED", "testing hasn't started yet"
    key = ticket.get("key") if isinstance(ticket, dict) else ticket
    mine = [c for c in cases if c.get("ticket") == key]
    prior_mine = [c for c in prior_passed if c.get("ticket") == key]
    its_bugs = ticket_bugs(key, cases, bugs, known)
    st = lambda c: c.get("status") or "NOT_RUN"
    why = lambda c: f": {_clip(c.get('actual') or c.get('notes'), 60)}" if c.get("notes") or c.get("actual") else \
        f": no result reported for {core.norm_route(c['route'])}" if st(c) == "NOT_RUN" and c.get("route") else ""
    unsure = [c for c in cases if key in c.get("id_tickets", ())]  # its result may be another plan case's
    fails = [c for c in mine if st(c) == "FAIL" and c not in unsure]
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
    if not mine and not prior_mine:
        return "BLOCKED", "no test cases"
    stuck = {}  # same reason → one entry naming all its cases, so 12 blocked cases don't repeat one error 12 times
    for c in unsure:
        stuck.setdefault("repeats an id in plan.json: its result can't be told apart", []).append(c["id"])
    for c in mine:
        if st(c) in ("BLOCKED", "NOT_RUN") and c not in unsure:
            stuck.setdefault(f"{'blocked' if st(c) == 'BLOCKED' else 'not run'}{why(c)}", []).append(c["id"])
    ended = [f"the run on {core.norm_route(r.get('target')) or name} ended BLOCKED"
             + (f": {_clip(r.get('summary'), 60)}" if r.get("summary") else "") for name, r in _blocked_runs(mine, runs)]
    if stuck or ended:
        return "BLOCKED", _some([(f"{ids[0]}" if len(ids) == 1 else f"{len(ids)} cases ({ids[0]}–{ids[-1]})") + f" {r}"
                                 for r, ids in stuck.items()] + ended, 2)
    covered = {a for c in mine for a in _case_acs(c)} | {a for c in prior_mine for a in _case_acs(c)}
    me = str(key).strip().upper()
    plan_keys = {str(k).strip().upper() for k in known} | {me}
    owner = lambda q: str(q.get("ticket") or "").strip().upper()  # '' or a key outside the plan: every ticket's
    qid = lambda q: re.match(r"\s*(Q\d+)\b", q["text"])
    notes = [f"{qid(q).group(1)} open blocking question" if qid(q) else f"open blocking question: {_clip(q['text'], 40)}"
             for q in questions if not q["resolved"] and "[BLOCKING]" in q["text"].upper()
             and (owner(q) == me or owner(q) not in plan_keys)] + \
        [f"{c['id']} open question" for c in mine if st(c) == "QUESTION"] + \
            [_bug_word(b) for b in its_bugs] + \
            [f"{_ac_id(a)} has no test case" for a in _list(ticket, "acs") if _ac_id(a) and _ac_key(_ac_id(a)) not in covered]
    if prior_mine:
        pb = ", ".join(sorted({str(c.get("build") or core.UNKNOWN) for c in prior_mine}))
        notes.append(f"{len(prior_mine)} case{'s' * (len(prior_mine) != 1)} verified on prior build {pb}; not re-run here")
    if notes:
        return "PASS WITH NOTES", _some(notes)
    return "PASS", f"all {len(mine)} cases passed"


def _verdicts(plan, runs, cases, bugs):
    """{ticket key: (verdict, reason)} for every plan ticket: the one place verdict() gets its inputs, so REPORT.md,
    the run index and the inbox's Last QA can't disagree. cases and bugs: merged_cases() and run_bugs() of `runs`."""
    tickets = _plan_tickets(plan)
    keys = [t["key"] for t in tickets]
    prior_all, questions = _prior_passed(plan, keys), _q_items(plan, "test plan")
    return {t["key"]: verdict(t, cases, bugs, bool(runs), keys, runs, [c for c in prior_all if c.get("ticket") == t["key"]],
                              questions) for t in tickets}


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


def _ac_lines(t, cases, with_status, prior_passed=()):
    out = []
    for ac in _list(t, "acs"):
        aid = _ac_id(ac)
        mine = [c for c in cases if _ac_key(aid) in _case_acs(c)]
        prior = [c for c in prior_passed if _ac_key(aid) in _case_acs(c)]
        ids = f" ({one_line(', '.join(str(c['id']) for c in (mine or prior) if c.get('id')))})" if (mine or prior) else ""
        if not mine and not prior:
            state = " — not covered"
        elif mine and with_status:
            state = " — " + STATUS_ICON[next(s for s in STATUS_RANK if any(c["status"] == s for c in mine))]
        elif not mine and prior:
            pb = ", ".join(sorted({str(c.get("build") or core.UNKNOWN) for c in prior}))
            state = f" — verified on prior build {one_line(pb)}"
        else:
            state = ""
        out.append(f"- {one_line(aid)} {cell(ac.get('text'))}{state}{ids}".replace("  ", " "))
    return out


def _prior_passed(plan, keys):
    """Normalized prior_passed entries from a partial --retest plan.json."""
    canon = {k.strip().upper(): k for k in keys}
    out = []
    for c in _list(plan, "prior_passed"):
        tk = c.get("ticket")
        if isinstance(tk, str) and tk.strip():
            tk = canon.get(tk.strip().upper(), tk)
        elif len(keys) == 1:
            tk = keys[0]
        ac = c.get("ac")
        ac = ", ".join(str(a) for a in ac if isinstance(a, (str, int)) and not isinstance(a, bool)) if isinstance(ac, list) else ac
        out.append({**c, "ticket": tk, "ac": ac})
    return out


def _prior_block(prior_mine):
    out = [f"<details><summary>Prior-build passes not re-run here ({len(prior_mine)})</summary>", "",
           row("ID", "AC", "Case", "Prior build"), "|---|---|---|---|"]
    for c in prior_mine:
        out.append(row(c.get("id"), c.get("ac"), c.get("title"), c.get("build") or core.UNKNOWN))
    return out + ["", "</details>", ""]


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
    # plan.json is written before any browser opens: what the runs used wins
    used = sorted({str(e["browser"]) for e in renvs if e.get("browser")})
    m = re.match(r"\d{4}-\d{2}-\d{2}", Path(run_dir).name)
    date = str(plan.get("created") or "")[:10] or (m.group(0) if m else "")
    builds = sorted({str(r.get("build")) for _, r in runs if r.get("build")})
    bits = [kind, date, str(env.get("url") or renv.get("url") or ""),
            "build " + (", ".join(builds) if builds else "not run yet"),
            "browser " + (", ".join(used) or str(env.get("browser") or "?")),
            f"retest of {plan['retest_of']}" if isinstance(plan.get("retest_of"), str) and plan["retest_of"].strip() else ""]
    return [f"# QA report: {one_line(plan.get('title') or Path(run_dir).name)}",
            " · ".join(one_line(b) for b in bits if b)]


def _draft_title(draft_path, fallback):
    for line in _text(draft_path).splitlines():
        if line.startswith("# "):
            return " ".join(line[2:].split())
    return " ".join(str(fallback or "").split())


def _filing_block(bugs, cases, tickets, run_dir, cfg):
    """Folded shell-safe filing commands, existing attachments and bug-status follow-ups for CONFIRMED bug drafts."""
    run_dir = Path(run_dir).resolve()
    entries = []
    default_proj = str(cfg.get("jiraProject") or "").strip() or "<PROJ>"
    for b in bugs:
        bid = b.get("id") or ""
        if not bid or _first_word(b.get("classification")) != "confirmed":
            continue
        draft = run_dir / "bugs" / f"{bid}.md"
        if not draft.is_file():
            continue
        owners = _bug_owners(b, cases, tickets)
        proj = next((o.split("-", 1)[0] for o in owners if ops.KEY_RE.fullmatch(o.upper())), default_proj)
        title = _draft_title(draft, b.get("title") or bid)
        cmd = (f"acli jira workitem create --project {shlex.quote(proj)} --type Bug "
               f"--summary {shlex.quote(title)} --description-file {shlex.quote(str(draft))}")
        # a re-sighting (a regression's draft) is stored under the hash it names, not one of its new wording
        h = core.given_hash(b.get("hash")) or \
            core.bug_hash(core.norm_route(b.get("route")),  # as ingest: a blank symptom falls back to the title
                          core.cap(scrub.redact(core._s(b.get("symptom")))) or core.cap(scrub.redact(core._s(b.get("title")))))
        attachments = [p for p in (run_dir / "evidence" / f"{bid}.png", run_dir / "evidence" / f"{bid}.webm",
                                   run_dir / "repro" / f"{bid}.spec.ts") if p.is_file()]
        block = [f"**{one_line(bid)}** — {one_line(core.short(title, 100))}", "", "```sh", cmd,
                 f"qa-mem bug-status {h} FILED <JIRA-KEY>", "```"]
        if attachments:
            block.append("Attach (inspect images/videos first): " + ", ".join(f"`{p}`" for p in attachments))
        block.append("")
        entries.append("\n".join(block))
    if not entries:
        return []
    return ["", f"<details><summary>Filing commands &amp; attachments ({len(entries)} confirmed)</summary>", "",
            *entries, "</details>"]


def _bug_section(bugs, cases, tickets, run_dir, heading="### Bugs to file", cfg=None):
    out = [heading, row("Bug", "Severity", "Class", "Title", "Page", "Ticket", "Evidence"), "|---|---|---|---|---|---|---|"]
    for b in bugs:
        bid = b.get("id") or ""
        owners = _bug_owners(b, cases, tickets)
        png = Path(run_dir) / "evidence" / f"{bid}.png"
        ev = Path(str(b.get("evidence") or ""))
        img = f"evidence/{quote(png.name)}" if bid and png.is_file() else \
            f"evidence/{quote(ev.name)}" if b.get("evidence") and (Path(run_dir) / "evidence" / ev.name).is_file() else ""
        # (is_file: evidence '.', '..' or '/' names a folder, and a link to it is broken)
        image = f"[image]({img})" + (" (not highlighted)" if b.get("annotated") is False else "") if img else ""
        webm = Path(run_dir) / "evidence" / f"{bid}.webm"  # the video of the clean-state repro
        video = f"[video](evidence/{quote(webm.name)})" if bid and webm.is_file() else ""
        evidence = " · ".join(x for x in (image, video) if x) or "—"
        link = _bug_link(bid, run_dir) or (one_line(f"known {str(b.get('hash'))[:12]}") if b.get("hash") else "—")
        out.append(row(Raw(link), b.get("severity"), b.get("classification"), core.short(b.get("title"), 120), b.get("route"),
                       ", ".join(owners), Raw(evidence)))
    return out + _filing_block(bugs, cases, tickets, run_dir, cfg or {})


def _notes_section(runs):
    out = ["## Exploration notes", "Heuristics: ✅ checked, no problem · ❌ problem found · ⛔ could not be checked", ""]
    for name, r in runs:
        route = r.get("target") or next((p.get("route") for p in _list(r, "pages")), "") or name
        cov = {}
        for p in _list(r, "pages"):
            for c in _list(p, "coverage"):
                if core.norm(c.get("heuristic")):
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


# ---------- browser signals: playwright-cli `requests --all` / `console error --all`, saved by the explorer at its end ----
REQUEST_LINE = re.compile(r"\s*\d+\.\s+\[([A-Z]+)\]\s+(\S+)\s+=>\s+\[(\d{3}|FAILED)\]\s*(.*)")  # 3. [GET] url => [404] text
SIGNALS_SHOWN = 15


def _text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _names_request(named, method, path):
    """Whether lower-cased text names a call by method and path: 'POST /api/zones' or 'POST http://host/api/zones'.
    A path alone (the run's own target, a passing note's 'GET /api/zones') explains no other call failing there."""
    return re.search(rf"\b{re.escape(method.lower())}\s+(?:https?://[^/\s]+)?{re.escape(path.lower())}(?![\w/.-])",
                     named) is not None


def _signals(run_dir, name, run):
    """[(signal, count)] from .data/requests-<slug>.log and console-<slug>.log of run file `name`: failed requests
    (4xx, 5xx, FAILED) and console errors, each once, minus those the run JSON or a bug draft names (a request by its
    method and path, a message by its first 60 characters). "Failed to load resource" lines repeat the requests log
    and are skipped.
    shortcut: no host filter, so a failing third-party call shows too; filter by allowedHosts if real runs get noisy."""
    data, slug, found = Path(run_dir) / ".data", name[len("run-"):-len(".json")], {}
    for line in scrub.redact(_text(data / f"requests-{slug}.log")).splitlines():
        m = REQUEST_LINE.fullmatch(line)
        if not m or m.group(3) != "FAILED" and int(m.group(3)) < 400:
            continue
        try:
            path = urlsplit(m.group(2)).path or m.group(2)
        except ValueError:  # an odd host such as an unclosed [IPv6
            path = m.group(2)
        status = m.group(3) if m.group(3) != "FAILED" else f"FAILED {m.group(4)}".strip()
        found.setdefault(f"{m.group(1)} {path} → {status}", [(m.group(1), path), 0])[1] += 1
    for line in scrub.redact(_text(data / f"console-{slug}.log")).splitlines():
        if not line.strip() or line[:1].isspace() or line.startswith(("###", "Total messages:", "Returning ")):
            continue  # blank, stack lines, headers
        level = re.match(r"\[(\w+)\]\s*", line)  # a page error (uncaught exception) has no [LEVEL]
        msg = re.sub(r"\s+@\s+\S*$", "", line[level.end():] if level else line).strip()  # " @ <source>:1"
        if msg and not msg.startswith("Failed to load resource:") and (not level or level.group(1).upper() == "ERROR"):
            found.setdefault(f"console: {msg}", [msg[:60], 0])[1] += 1
    named = (json.dumps(run, ensure_ascii=False) + "".join(map(_text, sorted((Path(run_dir) / "bugs").glob("*.md"))))).lower()
    explained = lambda w: _names_request(named, *w) if isinstance(w, tuple) else w.lower() in named
    return [(key, n) for key, (what, n) in found.items() if not explained(what)]


# ---------- questions: plan, runs and QUESTION cases, merged; a string is open, {"text","status","answer"} as written ----
def _q_items(obj, label):
    out = []
    raw = obj.get("questions") if isinstance(obj, dict) else None
    for x in raw if isinstance(raw, list) else []:
        if isinstance(x, dict):
            text, status, ans, tk = str(x.get("text") or ""), x.get("status"), str(x.get("answer") or ""), x.get("ticket")
        elif isinstance(x, (str, int, float)) and not isinstance(x, bool):
            text, status, ans, tk = str(x), None, "", None
        else:
            continue
        text = " ".join(text.split())
        if text:
            out.append({"text": text, "resolved": str(status or "").lower() == "resolved",
                        "answer": " ".join(ans.split()), "where": label, "ticket": tk})
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
    return 0 if "[BLOCKING]" in q["text"].upper() else 1 if "[HIGH]" in q["text"].upper() else 2


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


def _jira_draft(t, verdict_v, why, mine, run_dir, cfg, builds, retest_of="", prior_passed=()):
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
    if not mine:  # every case carried from the retested run: its images and builds are the ones to use
        prior_dir = Path(run_dir).parent / " ".join(str(retest_of).split()) if retest_of else None
        for c in prior_passed:
            png = prior_dir / "evidence" / f"{c.get('id')}.png" if prior_dir and _evidence_file(prior_dir, c.get("id")) else None
            if png and route_of(c) not in {r for r, _ in shots}:
                shots.append((route_of(c) or "test page", png))
        builds = [c.get("build") for c in prior_passed]
    lines = ["CHECK BEFORE POSTING (not part of the comment)"]
    if not builds or any(core.norm_build(b) == core.UNKNOWN for b in builds):  # one page on an unknown build is enough to check
        shown = (fix + ".0" if re.fullmatch(r"\d+\.\d+", fix) else fix) if fix else "<version>"
        lines.append(f"- Build: unknown in this run. Confirm the QA app runs {shown} before you post.")
    if prior_passed:
        pb = ", ".join(sorted({str(c.get("build") or core.UNKNOWN) for c in prior_passed}))
        pids = ", ".join(str(c.get("id") or "?") for c in prior_passed)
        tag = f" of {' '.join(str(retest_of).split())}" if retest_of else ""
        lines.append(f"- Partial retest{tag}: {len(prior_passed)} case(s) ({pids}) passed on prior build {pb} and were not re-run here.")
    lines += [f"- Paste this image: {path} ({label})" for label, path in shots]
    if not shots:
        lines.append("- No screenshot for this ticket yet. Add one before you post.")
    if placeholders:
        lines.append("- Placeholders to fill: " + "; ".join(placeholders))
    passed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    lines += ["", "---------- paste below ----------", "", f"Passed on {passed}", "",
              f"Fixed: {fixed or '<write one sentence: what was fixed>'}", ""]
    lines += [f"{c.get('id')} - PASS (prior build {c.get('build') or core.UNKNOWN}): {' '.join(str(c.get('title') or '').split())}"
              for c in prior_passed]
    lines += [f"{c['id']} - {CASE_RESULT_WORD[c['status']]}: {' '.join(str(c.get('title') or '').split())}" for c in mine]
    if verdict_v == "PASS WITH NOTES":
        lines += ["", f"Notes: {why}"]
    return "\n".join(lines) + "\n"


def render_report_parts(run_dir, cfg=None):
    """(REPORT.md text, {draft file name: text}). Drafts exist for PASS and PASS WITH NOTES tickets only."""
    run_dir = Path(run_dir)
    d = load_run(run_dir)
    plan, runs = d["plan"], d["runs"]
    kind = str(plan.get("kind") or (runs[0][1].get("mode") if runs else "") or "explore")
    tickets = _plan_tickets(plan)
    keys = [t["key"] for t in tickets]
    cases, bugs = merged_cases(plan, runs, d["warnings"]), run_bugs(runs, d["warnings"])
    prior_all = _prior_passed(plan, keys)
    for t in tickets:  # a case naming an AC its ticket doesn't list covers nothing: say so
        have = {_ac_key(_ac_id(a)) for a in _list(t, "acs") if _ac_id(a)}
        for c in (c for c in cases if have and c.get("ticket") == t["key"]):
            d["warnings"] += [f"{c['id']} names {a}, which {t['key']} doesn't have: fix the case's `ac` in plan.json"
                              for a in sorted(_case_acs(c) - have)]
    retest_of = str(plan.get("retest_of") or "").strip() if isinstance(plan.get("retest_of"), str) else ""
    with_status = kind != "plan"
    cfg = core.config() if cfg is None else cfg
    drafts, vmap = {}, (_verdicts(plan, runs, cases, bugs) if tickets and with_status else {})
    if vmap:
        for t in tickets:
            prior_mine = [c for c in prior_all if c.get("ticket") == t["key"]]
            v, why = vmap[t["key"]]
            mine = [c for c in cases if c.get("ticket") == t["key"]]
            ids = {c["id"] for c in mine}  # the builds of the runs that tested this ticket, not the whole folder's
            builds = sorted({core.norm_build(r.get("build")) for _, r in runs
                             if any(str(x.get("id")) in ids for x in _list(r, "cases"))})
            if v in ("PASS", "PASS WITH NOTES") and (mine or prior_mine) and _safe_key(t["key"]):
                drafts[f"jira-comment-{t['key']}.md"] = _jira_draft(t, v, why, mine, run_dir, cfg, builds, retest_of, prior_mine)
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
            prior_n = sum(c.get("ticket") == t["key"] for c in prior_all)
            out.append(row(_ticket_title(t), VERDICT_ICON[v], why, _counts(mine) + (f" (+{prior_n} prior)" if prior_n else ""),
                           len(ticket_bugs(t["key"], cases, bugs, keys))))
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
            action += _bug_section(bugs, cases, keys, run_dir, heading=f"### Bugs to file ({len(bugs)})", cfg=cfg) + [""]
        # a draft whose run file a later one replaced (--resume): still shown, but no verdict counts it
        listed = {b["id"] for b in bugs}.union(*map(_case_bugs, cases))
        orphans = sorted((p for p in (run_dir / "bugs").glob("BUG-*.md")
                          if re.fullmatch(r"BUG-\d+", p.stem) and p.stem not in listed and p.is_file()),
                         key=lambda p: int(p.stem[4:]))
        if orphans:
            action += [f"### Bug drafts no run file lists ({len(orphans)})",
                       "A later run file replaced the one that found them (`--resume`), so no verdict counts them: "
                       "check each and file it if it still applies.",
                       *[f"- {_bug_link(p.stem, run_dir)} {one_line(_draft_title(p, ''))}" for p in orphans], ""]
        if open_qs:
            action += [f"### Open questions ({len(open_qs)})"] + [_q_bullet(q) for q in open_qs] + [""]
        if attention:
            action += [f"### Failed, blocked or not-run cases ({len(attention)})"] + [
                f"- {cell(c['id'])} {STATUS_ICON[c['status']]}" + (f" ({cell(c['ac'])})" if c.get("ac") else "") +
                (f" — {cell(core.short(c.get('actual') or c.get('notes'), 160))}" if (c.get("actual") or c.get("notes")) else "")
                + (f" · {cell(c['ticket'])}" if c.get("ticket") else "") for c in attention] + [""]
        left = [(r.get("target") or name, m.group(1).strip()) for name, r in runs
                for m in re.finditer(r"Left in the app:[ \t]*([^\n]+)", core._s(r.get("notes")))]
        if left:  # step 9 couldn't delete them (budget or time used up): they stay in the shared QA app
            action += [f"### Test data left in the app ({len(left)})"] + [
                f"- {one_line(route)}: {cell(core.short(text, 300))}" for route, text in left] + [""]
        signals = [(r.get("target") or name, key, n) for name, r in runs for key, n in _signals(run_dir, name, r)]
        if signals:
            action += [f"### Browser signals no bug explains ({len(signals)})"] + [
                f"- {one_line(route)}: {one_line(core.short(key, 200))}" + (f" (×{n})" if n > 1 else "")
                for route, key, n in signals[:SIGNALS_SHOWN]]
            if len(signals) > SIGNALS_SHOWN:
                action.append(f"- +{len(signals) - SIGNALS_SHOWN} more: see `requests-*.log` and `console-*.log` in `.data/`")
            action.append("")
        out += ["## Needs your action"] + (action or ["Nothing needs your action.", ""])

    for t in tickets:
        mine = [c for c in cases if c.get("ticket") == t["key"]]
        prior_mine = [c for c in prior_all if c.get("ticket") == t["key"]]
        out.append(f"## {one_line(_ticket_title(t))}")
        if with_status and len(tickets) > 1:
            v, why = vmap.get(t["key"]) or ("NOT STARTED", "testing hasn't started yet")
            out.append(f"**Verdict: {VERDICT_ICON[v]}** — {one_line(why)}")
        if len(tickets) > 1 and draft_of(t["key"]):  # one ticket: the link is in the top block
            out.append(f"**Jira comment draft:** [{draft_of(t['key'])}]({draft_of(t['key'])})")
        if _list(t, "acs"):
            out += ["### Acceptance criteria"] + _ac_lines(t, mine, with_status, prior_mine)
        if mine:
            out += ["### Test cases"] + _case_block(mine, run_dir, with_status)
        if prior_mine:
            out += _prior_block(prior_mine)
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
    tickets = _plan_tickets(plan)
    if kind == "plan":
        verdicts = f"plan only ({len(cases)} cases)"
    elif tickets:
        vmap = _verdicts(plan, runs, cases, bugs)
        verdicts = ", ".join(f"{t['key']} {VERDICT_ICON[vmap[t['key']][0]]}" for t in tickets)
    else:
        verdicts = ", ".join(str(r.get("status") or "?") for _, r in runs) or "not run yet"
    m = re.match(r"\d{4}-\d{2}-\d{2}", Path(run_dir).name)
    return (m.group(0) if m else str(plan.get("created") or "")[:10]), kind, verdicts, len(bugs)


def render_index(qa_out):
    """qa-out/README.md: every run folder with a REPORT.md or a plan, newest first."""
    qa_out = Path(qa_out)
    runs = [p for p in qa_out.iterdir() if p.is_dir() and not p.is_symlink()
            and ((p / "REPORT.md").exists() or (p / ".data" / "plan.json").exists())]
    when = lambda p: max((_mtime(x) for x in (p, p / "REPORT.md", p / ".data" / "plan.json") if x.exists()), default=0.0)
    rows = []
    for p in runs:
        try:
            date, kind, verdicts, n = run_summary(p)
            ts = when(p)
        except Exception as e:  # one odd folder must not break the index
            m = re.match(r"\d{4}-\d{2}-\d{2}", p.name)
            date, kind, verdicts, n, ts = (m.group(0) if m else ""), "?", f"unreadable ({type(e).__name__})", "?", 0.0
        name = one_line(p.name).replace("[", "\\[").replace("]", "\\]")
        rows.append(((date, ts), row(date, Raw(f"[{name}]({quote(p.name, safe='+')}/REPORT.md)"), kind, verdicts, n)))
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
            if old.name not in drafts and (old.is_symlink() or old.is_file()):
                old.unlink(missing_ok=True)
    # Two reports at once (two sessions, or report + hook): without the lock the later index write could be one
    # rendered before the other run's REPORT.md existed, and that run would vanish from the index.
    fd = os.open(run_dir.parent, os.O_RDONLY)  # the qa-out folder itself: no lock file left behind
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        core._write(run_dir.parent / "README.md", render_index(run_dir.parent))
    finally:
        os.close(fd)
    return run_dir / "REPORT.md"


# ---------- plan-find: deterministic plan reuse, --resume and --retest state ----------
def _plan_folders(qa_out):
    """Run folders with a plan.json, the most recently changed first (folder, plan or a run file): plan-find's latest
    run and the inbox's Last QA come from the same order."""
    dirs = [p for p in qa_out.iterdir() if p.is_dir() and not p.is_symlink() and (p / ".data" / "plan.json").is_file()]
    when = lambda p: max((_mtime(x) for x in [p, p / ".data" / "plan.json", *(p / ".data").glob("run-*.json")] if x.exists()), default=0.0)
    return sorted(dirs, key=lambda p: (when(p), p.name), reverse=True)


def _run_state(run_dir):
    """Per-route and per-case state of one run folder, or None if plan.json is missing/unreadable."""
    run_dir = Path(run_dir).resolve()
    plan_file = run_dir / ".data" / "plan.json"
    if not plan_file.is_file():
        return None
    d = load_run(run_dir)
    plan, runs = d["plan"], d["runs"]
    if not plan:
        return None
    tickets = [t["key"] for t in _plan_tickets(plan)]
    cases = merged_cases(plan, runs)
    counts = {s: sum(c["status"] == s for c in cases) for s in core.CASE_STATUSES}
    builds = sorted({core.norm_build(r.get("build")) for _, r in runs if core.norm_build(r.get("build")) != core.UNKNOWN})
    case_build = {}
    for _, r in runs:
        b = core.norm_build(r.get("build"))
        for x in _list(r, "cases"):
            if x.get("id") not in (None, "") and not isinstance(x.get("id"), bool):
                case_build[str(x["id"])] = b
    routes_order = []
    for c in cases:
        rt = core.norm_route(c.get("route")) or ""
        if rt and rt not in routes_order:
            routes_order.append(rt)
    run_status_by_route = {}
    for _, r in runs:
        rt = core.norm_route(r.get("target")) or ""
        if rt:
            run_status_by_route[rt] = _first_word(r.get("status"))
    incomplete = []
    for rt in routes_order:
        rcases = [c for c in cases if (core.norm_route(c.get("route")) or "") == rt]
        if run_status_by_route.get(rt) in ("blocked", "incomplete") or any(c["status"] in ("BLOCKED", "NOT_RUN") for c in rcases):
            incomplete.append(rt)
    completed = [rt for rt in routes_order if rt not in incomplete]
    # verdict()'s rules per case: a PASS that a run ended BLOCKED reported (or ran on its page), or on the page of (or
    # naming) a CONFIRMED CRITICAL/HIGH bug of its ticket that no retested case names, is retested: a partial retest must
    # not make it a prior pass and drop the reason the ticket didn't pass
    bugs = run_bugs(runs)
    rechecked = set().union(*(_case_bugs(c) for c in cases if c["status"] != "PASS"))
    tkey = lambda c: c.get("ticket") if isinstance(c.get("ticket"), str) else None  # a list ticket is no plan ticket's
    serious = {t: [b for b in ticket_bugs(t, cases, bugs, tickets) if _serious(b) and not _bug_refs(b) & rechecked]
               for t in {tkey(c) for c in cases}}

    def trusted(c):
        rt = core.norm_route(c.get("route"))
        return c["status"] == "PASS" and not _blocked_runs([c], runs) and not any(
            rt and rt in (b.get("route"), b.get("target")) or _bug_refs(b) & _case_bugs(c) for b in serious[tkey(c)])
    retest = [{"id": c["id"], "ticket": c.get("ticket") or "", "ac": c.get("ac") or "",
               "route": core.norm_route(c.get("route")) or "", "status": c["status"], "title": c.get("title") or ""}
              for c in cases if not trusted(c)]
    passed = [{"id": c["id"], "ticket": c.get("ticket") or "", "ac": c.get("ac") or "",
               "route": core.norm_route(c.get("route")) or "", "title": c.get("title") or "",
               "build": case_build.get(c["id"]) or (builds[-1] if builds else core.UNKNOWN)}
              for c in cases if trusted(c)]
    # an earlier retest's prior passes, not re-run here: carried on, or a chained retest would drop them
    ids = {c["id"] for c in cases}
    passed += [{"id": str(c["id"]), "ticket": c.get("ticket") or "", "ac": c.get("ac") or "",
                "route": core.norm_route(c.get("route")) or "", "title": core._s(c.get("title")),
                "build": core._s(c.get("build")) or core.UNKNOWN}
               for c in _prior_passed(plan, tickets) if isinstance(c.get("id"), (str, int)) and not isinstance(c["id"], bool)
               and str(c["id"]) and str(c["id"]) not in ids]
    zero_exec = bool(runs) and all(c["status"] in ("BLOCKED", "NOT_RUN") for c in cases) and \
        all(_first_word(r.get("status")) == "blocked" for _, r in runs)
    kind = str(plan.get("kind") or "")
    return {
        "run_dir": str(run_dir), "name": run_dir.name, "plan_file": str(plan_file), "kind": kind,
        "tickets": tickets, "has_runs": bool(runs), "zero_executed": zero_exec, "builds": builds,
        "total_cases": len(cases), "counts": counts, "incomplete_routes": incomplete, "completed_routes": completed,
        "retest_cases": retest, "passed_cases": passed,
        "can_resume": kind == "ticket" and bool(incomplete), "can_retest": bool(cases) and bool(runs),
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
    dirs = _plan_folders(qa_out)
    reusable, latest = None, None
    for p in dirs:
        st = _run_state(p)
        if not st or {k.strip().upper() for k in st["tickets"]} != want:
            continue
        if reusable is None and (st["kind"] == "plan" or (st["kind"] == "ticket" and (not st["has_runs"] or st["zero_executed"]))):
            reusable = st
        if latest is None and st["kind"] == "ticket" and st["has_runs"]:
            latest = st
        if reusable and latest:
            break
    return {"reusable_plan": reusable, "latest_run": latest}


# ---------- inbox: Jira tickets assigned to me, NEW ones flagged ----------
def _name(v):
    return str(v.get("name") or v.get("value") or "") if isinstance(v, dict) else str(v or "")


def inbox_items(data):
    """acli search JSON (a list, or {issues|workItems|values: [...]}) -> [{key, summary, status, priority, updated}]."""
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
        raise core.BadInput(f"inbox: none of the {len(data)} items has a Jira key (\"key\": \"PROJ-1234\"); ask the search for "
                       "the key field")
    return out


def _seen_map(raw):
    """Parse inbox_seen meta (legacy list of keys or {key: status} dict) into {KEY: status}."""
    try:
        parsed = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    if isinstance(parsed, dict):
        return {str(k): str(v or "") for k, v in parsed.items() if isinstance(k, str)}
    if isinstance(parsed, list):
        return {k: "" for k in parsed if isinstance(k, str)}
    return {}


def _qa_verdicts_by_key(qa_out, keys):
    """Newest local QA status/verdict per ticket key from `<project>/qa-out/`."""
    if not keys or not qa_out.is_dir():
        return {}
    dirs = _plan_folders(qa_out)
    found = {}
    for p in dirs:
        d = load_run(p)
        plan, runs = d["plan"], d["runs"]
        tickets = _plan_tickets(plan)
        matching = [t for t in tickets if t["key"].strip().upper() in keys and t["key"].strip().upper() not in found]
        if not matching:
            continue
        kind = str(plan.get("kind") or "")
        vmap = {} if kind == "plan" else _verdicts(plan, runs, merged_cases(plan, runs), run_bugs(runs))
        for t in matching:
            found[t["key"].strip().upper()] = "plan" if kind == "plan" else VERDICT_ICON[vmap[t["key"]][0]]
        if len(found) == len(keys):
            break
    return found


def inbox(con, project, data, prefix=None, today=None):
    """Markdown for INBOX.md (also printed); tracks {key: status} in meta inbox_seen and cross-references qa-out/."""
    out = ops._project(project) / "qa-out"  # a typo must not create a stray folder
    items = list({x["key"]: x for x in inbox_items(data)}.values())  # a key listed twice (paged search) is one ticket
    seen = _seen_map(core.meta(con, "inbox_seen"))
    for x in items:
        if x["key"] not in seen:
            x["tag"] = "NEW"
        elif seen[x["key"]] and x["status"] and seen[x["key"]] != x["status"]:
            x["tag"] = "CHANGED"
        else:
            x["tag"] = ""
    rank = {"NEW": 0, "CHANGED": 1, "": 2}
    items.sort(key=lambda x: x["updated"], reverse=True)
    items.sort(key=lambda x: rank[x["tag"]])  # stable: NEW, then CHANGED, then unchanged
    new = [x for x in items if x["tag"] == "NEW"]
    changed = [x for x in items if x["tag"] == "CHANGED"]
    qa_map = _qa_verdicts_by_key(out, {x["key"] for x in items})
    show_updated = any(x["updated"] for x in items)
    show_qa = bool(qa_map)
    counts = f"{today or datetime.now().strftime('%Y-%m-%d')} · {len(items)} open, {len(new)} new" + (
        f", {len(changed)} changed" if changed else "")
    lines = ["# Jira inbox", counts, ""]
    if items:
        head = ["New", "Key", "Summary", "Status", "Priority"] + ["Updated"] * show_updated + ["Last QA"] * show_qa
        lines += [row(*head), "|" + "---|" * len(head)]
        for x in items:
            cols = [x["tag"], x["key"], x["summary"], x["status"], x["priority"]]
            if show_updated:
                cols.append(x["updated"][:10])
            if show_qa:
                cols.append(qa_map.get(x["key"], "—"))
            lines.append(row(*cols))
    else:
        lines.append("No open tickets assigned to you.")
    urgent = [x["key"] for x in items if x["tag"] in ("NEW", "CHANGED")]
    unpassed = [x["key"] for x in items if qa_map.get(x["key"]) != VERDICT_ICON["PASS"]]
    pick = urgent or unpassed[:3] or [x["key"] for x in items[:3]]
    p = str(prefix or "").strip().rstrip("-")
    p = p.upper() if p.isascii() else ""
    nums = [k[len(p) + 1:] if p and k.startswith(p + "-") else k for k in pick]
    lines += ["", f"Next: /qa-sdet:qa-ticket {' '.join(nums)}" if nums else "Next: nothing to test"]
    text = "\n".join(lines) + "\n"
    out.mkdir(exist_ok=True)
    core._write(out / "INBOX.md", text)
    # remembered only once INBOX.md is written (a failed write keeps them NEW); today's keys are always kept, older
    # ones up to 5000 (a cap on sorted keys dropped the alphabetically last, so a big inbox stayed NEW forever)
    with con:
        con.execute("BEGIN IMMEDIATE")  # re-read under the write lock: keys another inbox saved meanwhile stay seen
        old_map = _seen_map(core.meta(con, "inbox_seen"))
        keep = {x["key"]: x["status"] for x in items}
        for k, st in old_map.items():
            if len(keep) >= max(5000, len(items)):
                break
            if k not in keep:
                keep[k] = st
        core.set_meta(con, "inbox_seen", json.dumps(keep))
    return text


# ---------- prep: single deterministic read-only ticket+plan+rules+memory bundle ----------
def _acli_json(acli, argv, timeout=30):
    try:
        r = subprocess.run([acli, *argv], capture_output=True, text=True, errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    if r.returncode != 0:
        return None, core.short((r.stderr or r.stdout or f"exited {r.returncode}").strip(), 200)
    try:
        return json.loads(r.stdout), None
    except (ValueError, RecursionError) as e:
        return None, f"unreadable JSON ({e})"


ADF_JOIN = {"doc": "\n", "bulletList": "\n", "orderedList": "\n", "blockquote": "\n", "panel": "\n", "table": "\n",
            "expand": "\n", "tableRow": " | ", "tableCell": " ", "tableHeader": " ", "listItem": " "}


def _adf_attr_text(node):
    """Text of the inline nodes that carry it in attrs: a mention, a link card, a status lozenge, a date."""
    a = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
    if node.get("type") == "date" and str(a.get("timestamp") or "").isdigit():
        return datetime.fromtimestamp(int(a["timestamp"]) / 1000, timezone.utc).strftime("%Y-%m-%d")
    return next((str(a[k]) for k in ("text", "url", "shortName") if isinstance(a.get(k), (str, int)) and str(a[k])), "")


def _adf_text(node, depth=0):
    """Flatten Jira Cloud Atlassian Document Format (ADF) JSON to plain text so prep stays compact."""
    if depth > 20 or node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "hardBreak":
            return "\n"
        if isinstance(node.get("text"), str):
            return node["text"]
        if isinstance(node.get("content"), list):
            text = ADF_JOIN.get(node.get("type"), "").join(t for t in (_adf_text(c, depth + 1) for c in node["content"]) if t)
            return f"- {text}" if node.get("type") == "listItem" and text else text
        return _adf_attr_text(node)
    if isinstance(node, list):
        return "\n".join(t for t in (_adf_text(c, depth + 1) for c in node) if t)
    return ""


def _trim_comments(raw):
    items = raw if isinstance(raw, list) else (
        next((raw[k] for k in ("comments", "values", "items") if isinstance(raw.get(k), list)), [])
        if isinstance(raw, dict) else [])
    trimmed = []
    for c in items[-20:]:
        if isinstance(c, dict):
            author = c.get("author")
            who = author.get("displayName") or author.get("name") or "" if isinstance(author, dict) else str(author or "")
            body = c.get("body")
            if isinstance(body, dict) and body.get("type") == "doc":
                body = _adf_text(body)
            if isinstance(body, str) and len(body) > 1500:
                body = body[:1500] + "…"
            trimmed.append({"author": who, "created": str(c.get("created") or ""), "body": body})
    return trimmed


def prep(con, project, key_args):
    """Bundle keys, Jira ticket + comments (via read-only acli if signed in), reusable plan/run state, qa-rules.md,
    and memory routes/bugs in one scrubbed JSON dict."""
    from . import codemap, memory
    proj = ops._project(project)
    cfg = core.config()
    keys = ops.expand_keys(key_args, cfg.get("jiraKeyPrefix"))
    ac_field = str(cfg.get("jiraAcField") or "").strip()
    ac_field = ac_field if re.fullmatch(r"customfield_\d+", ac_field) else ""
    acli = shutil.which("acli")
    acli_ok = False
    if acli:
        try:
            acli_ok = subprocess.run([acli, "jira", "auth", "status"], capture_output=True, timeout=20).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            acli_ok = False
    tickets = []
    if acli_ok:
        base_fields = "summary,description,labels,components,issuelinks,fixVersions"
        fields = f"{base_fields},{ac_field}" if ac_field else base_fields
        for key in keys:
            data, err = _acli_json(acli, ["jira", "workitem", "view", key, "--fields", fields, "--json"])
            used_ac = bool(ac_field)
            if err and ac_field:
                data, err = _acli_json(acli, ["jira", "workitem", "view", key, "--fields", base_fields, "--json"])
                used_ac = False
            comm_raw, comm_err = _acli_json(acli, ["jira", "workitem", "comment", "list", "--key", key, "--json"])
            item = {"key": key, "fields": data.get("fields", data) if isinstance(data, dict) else data,
                    "comments": _trim_comments(comm_raw) if comm_raw is not None else []}
            if isinstance(item["fields"], dict):  # Jira Cloud gives rich text as ADF trees: text is what a plan needs
                for f in ("description", ac_field):
                    if f and isinstance(item["fields"].get(f), dict) and item["fields"][f].get("type") == "doc":
                        item["fields"][f] = _adf_text(item["fields"][f])
            if ac_field and not used_ac and not err:
                item["ac_field_skipped"] = ac_field
            if err:
                item["error"] = err
            if comm_err:
                item["comments_error"] = comm_err
            tickets.append(item)
    rules_path = proj / "qa-rules.md"
    rules = ""
    if rules_path.is_file() and not rules_path.is_symlink():
        try:
            rules = rules_path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            rules = ""
    open_bugs = [memory.bugs(con, st) for st in core.VISIBLE]
    open_bugs = [b for b in open_bugs if not b.startswith("no ")]
    bundle = {
        "keys": keys,
        "acli": acli_ok,
        "config": {k: cfg.get(k) for k in ("jiraProject", "jiraKeyPrefix", "jiraAcField", "qaBaseUrl", "browser",
                                           "buildSource", "releaseTemplates", "inboxJql") if cfg.get(k) is not None},
        "tickets": tickets,
        "plan_state": plan_find(proj, keys=keys),
        "rules": rules,
        "routes": codemap.code(con),
        "bugs_open": "\n".join(open_bugs) or "no OPEN bugs",
        "bugs_fixed": memory.bugs(con, "FIXED"),
    }
    return scrub.redact_json(bundle)
