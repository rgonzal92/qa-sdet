"""REPORT.md, the Jira comment, the run index and `inbox`."""

from __future__ import annotations

import fcntl
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import core, ops, probe, scrub

TYPE_CHECKING = False  # mypy reads it as True (pyproject always_true): typing stays out of the import path
if TYPE_CHECKING:  # NOSONAR
    from collections.abc import Iterable, Sequence
    from typing import Any

    from .contracts import JsonObj, Verdict


# ---------- REPORT.md: one human report per run folder, rendered from .data/plan.json + .data/run-*.json ----------
STATUS_RANK = ("FAIL", "BLOCKED", "QUESTION", "NOT_RUN", "PASS")  # worst first
VERDICT_ICON = {
    "FAIL": "❌ FAIL",
    "BLOCKED": "⛔ BLOCKED",
    "PASS WITH NOTES": "⚠️ PASS WITH NOTES",
    "PASS": "✅ PASS",
    "NOT STARTED": "⏳ NOT STARTED",
}
COVERAGE_ICON = {"pass": "✅", "fail": "❌", "blocked": "⛔"}


def _load_json(path, warnings):
    try:
        # not a vulnerability (S2083): callers pass fixed names inside the run folder; bug ids are validated BUG-n
        return scrub.redact_json(json.loads(Path(path).read_text(encoding="utf-8-sig")))  # NOSONAR
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


def load_run(run_dir: str | Path) -> dict[str, Any]:
    """{'plan': {...}, 'runs': [(file name, run dict)], 'warnings': [...], 'unread': [run file names that couldn't be
    read: verdict() blocks on them]}; missing pieces are simply absent."""
    data, warnings, unread = Path(run_dir) / ".data", [], []
    plan = _load_json(data / "plan.json", warnings)
    if plan is not None and not isinstance(plan, dict):
        warnings.append("plan.json is not an object")
        plan = None
    runs = []
    for f in sorted(data.glob("run-*.json"), key=lambda x: (_mtime(x), x.name)):
        if f.is_symlink() and not f.exists():
            warnings.append(f"{f.name} unreadable (FileNotFoundError)")
            unread.append(f.name)
            continue
        r = _load_json(f, warnings)
        if not isinstance(r, dict):  # the file is there, its results are not
            unread.append(f.name)
        if isinstance(r, dict):
            runs.append((f.name, r))
            for key in ("cases", "bugs", "pages", "questions", "not_tested"):  # dropped below: say so, don't hide it
                if r.get(key) is not None and not isinstance(r[key], list):
                    warnings.append(
                        f"{f.name}: `{key}` should be a list (got {type(r[key]).__name__}); "
                        f"its entries are not in this report"
                    )
        elif r is not None:
            warnings.append(f"{f.name} is not an object")
    return {"plan": plan or {}, "runs": runs, "warnings": warnings, "unread": unread}


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
    ("not_reached", "Not reached (route or run ended)"),
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


def _now_build(runs):
    """The build of the newest run (load_run's order) that read one, else None. A ticket's folder keeps every re-run's
    files: a run file from another known build holds results the new build's run has not repeated."""
    known = [b for _, r in runs if (b := core.norm_build(r.get("build"))) != core.UNKNOWN]
    return known[-1] if known else None


def _older_build(build, now):
    """A known build that is not `now` (_now_build's): an unknown one can't be compared."""
    return bool(now) and core.norm_build(build) not in (core.UNKNOWN, now)


def merged_cases(
    plan: JsonObj, runs: Sequence[tuple[str, JsonObj]], warnings: list[str] | None = None
) -> list[JsonObj]:
    """Plan cases with their result from the run JSONs (missing -> NOT_RUN); cases only a run knows are appended.
    Each gets status, actual, notes, bug. When several runs report one case, the most decisive result wins (FAIL over
    QUESTION over PASS over BLOCKED over NOT_RUN; the newest run on a tie), so another route listing a case as
    "not run" never hides its FAIL. A plan ticket's PASS whose own `build` is not its run file's (carry_prior kept it
    from an earlier attempt), or whose run file is from another known build than the newest run's (a ticket's folder
    keeps every re-run's files: a page the new build's run never reached), gets that build and `prior`: _prior_passed
    lists it as a pass not re-run here. A plan case with no id or a repeated one can't get a result: it is left out
    and named in `warnings`, and the case kept under that id gets `id_tickets` (every ticket using the id: verdict()
    blocks them)."""
    warnings = [] if warnings is None else warnings
    valid_id = lambda v: isinstance(v, (str, int, float)) and not isinstance(v, bool) and v != ""  # as core.txt
    results, run_build = {}, {}
    rank = lambda c: MERGE_RANK.index(core.case_status(c.get("status")) or "NOT_RUN")
    for _, r in runs:
        for c in _list(r, "cases"):
            if valid_id(c.get("id")):
                cid = str(c["id"])
                if cid not in results or rank(c) >= rank(results[cid]):
                    results[cid], run_build[cid] = c, r.get("build")
    now = _now_build(runs)
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
            warnings.append(
                f"plan.json: case '{what}' "
                + (f"repeats id {cid}" if cid else "has no id")
                + "; it is left out of this report: give it an id of its own (e.g. TC-99)"
            )
            if cid:  # a ticket that isn't text (a list) is no plan ticket's: None, so the set can hold it
                repeated.setdefault(cid, set()).add(tk if isinstance(tk, str) else None)
            continue
        seen.add(cid)
        res = results.get(cid, {})
        ac = c.get("ac")  # a case may cover several AC: "AC-1, AC-2" or a list
        ac = (
            ", ".join(str(a) for a in ac if isinstance(a, (str, int)) and not isinstance(a, bool))
            if isinstance(ac, list)
            else ac
        )
        merged = {
            **c,
            "id": cid,
            "ac": ac,
            "ticket": tk,
            "status": core.case_status(res.get("status")) or "NOT_RUN",
            "actual": core._s(res.get("actual")),
            "notes": core._s(res.get("notes")),
            "bug": core._s(res.get("bug")),
        }
        if "annotated" in res:
            merged["annotated"] = res["annotated"]
        ran_on = core.norm_build(res["build"] if isinstance(res.get("build"), str) else run_build.get(cid))
        if (
            tk in keys
            and merged["status"] == "PASS"
            and (ran_on != core.norm_build(run_build.get(cid)) or _older_build(ran_on, now))
        ):
            merged.update(build=ran_on, prior=True)
        out.append(merged)
    for m in out:
        if m["id"] in repeated:
            m["id_tickets"] = repeated[m["id"]] | {m["ticket"] if isinstance(m["ticket"], str) else None}
    return out


def run_bugs(runs: Sequence[tuple[str, JsonObj]], warnings: list[str] | None = None) -> list[JsonObj]:
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
                    warnings.append(
                        f"{name}: {b['id']} is also the id of another bug ({core.short(first.get('title'), 50)}); "
                        f"one draft file can't hold both: both are listed, check bugs/{b['id']}.md"
                    )
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
    named = set().union(*(_case_bugs(c) for c in cases if c.get("ticket") and c.get("ticket") != key))
    routes = {core.norm_route(c.get("route")) for c in mine if c.get("route")}
    tickets = {str(c.get("ticket")).strip().upper() for c in cases if c.get("ticket")} | {
        str(k).strip().upper() for k in known
    }
    tag = lambda b: str(b.get("ticket") or "").strip().upper()  # 's-1 ' is S-1; 'none' or an unknown key is no tag
    # untagged: on one of the ticket's routes, or found by the run of one (a record page opened from its list page)
    return [
        b
        for b in bugs
        if tag(b) == str(key).strip().upper()
        or (_bug_refs(b) & refs)
        or (
            tag(b) not in tickets
            and (b.get("route") in routes or b.get("target") in routes)
            and not (_bug_refs(b) & named)
        )
    ]


def _bug_owners(bugs, cases, tickets):
    """Per bug, the plan tickets it counts for (as their verdicts count it), else the ticket it names (not 'none').
    One ticket_bugs pass per ticket: each pass reads every case."""
    owned = [(str(t), {id(x) for x in ticket_bugs(t, cases, bugs, tickets)}) for t in tickets]
    return [
        [t for t, ids in owned if id(b) in ids]
        or ([str(b["ticket"])] if b.get("ticket") and str(b["ticket"]).strip().lower() != "none" else [])
        for b in bugs
    ]


def _first_word(v):
    """'confirmed' from 'Confirmed (2/2)', 'high' from 'High ': agents add words to the value."""
    return (core.norm(v).split() or [""])[0]


def _serious(b):
    return _first_word(b.get("classification")) == "confirmed" and _first_word(b.get("severity")) in (
        "critical",
        "high",
    )


def _bug_word(b):
    sev = " ".join(str(b.get("severity") or "").split()).upper() or "unrated"
    extra = "" if _first_word(b.get("classification")) in ("confirmed", "") else f" {str(b['classification']).lower()}"
    name = b.get("id") or ("known bug " if b.get("hash") else "") + " ".join(core._s(b.get("title")).split())
    return f"{name} {sev}{extra}"


def _some(parts, n=3):
    return "; ".join(parts[:n]) + (f"; +{len(parts) - n} more" if len(parts) > n else "")


def _clip(s, n):
    """short() cut back to the last word so a number or name is never cut in half."""
    s = " ".join(str(s or "").split())
    if len(s) <= n:
        return s
    cut = s[: n - 1]
    return (cut[: cut.rfind(" ")] if " " in cut[n // 2 :] else cut).rstrip(" ,;:") + "…"


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
    return [
        (name, r)
        for name, r in runs
        if _first_word(r.get("status")) == "blocked"
        and (core.norm_route(r.get("target")) in routes or any(str(x.get("id")) in ids for x in _list(r, "cases")))
    ]


def verdict(
    ticket: str | JsonObj,
    cases: list[JsonObj],
    bugs: list[JsonObj],
    started: bool = True,
    known: Iterable[str] = (),
    runs: Sequence[tuple[str, JsonObj]] = (),
    prior_passed: Sequence[JsonObj] = (),
    questions: Sequence[JsonObj] = (),
    unread: Sequence[str] = (),
) -> tuple[Verdict, str]:
    """(verdict, reason) for one ticket: FAIL, BLOCKED, PASS WITH NOTES or PASS (rules: README, "Reading REPORT.md");
    NOT STARTED while no test run has reported yet. runs: [(file name, run)], for runs that ended BLOCKED.
    prior_passed: cases from an earlier build not re-run in this partial retest. questions: the plan's, as _q_items gives
    them: an open [BLOCKING] one tagged with this ticket, or with no plan ticket, is a note. unread: run files that
    couldn't be read (load_run): what they held, and for which ticket, is unknown, so nothing passes."""
    if not started:
        return "NOT STARTED", "testing hasn't started yet"
    key = ticket.get("key") if isinstance(ticket, dict) else ticket
    mine = [c for c in cases if c.get("ticket") == key and not c.get("prior")]  # a prior pass is in prior_passed
    prior_mine = [c for c in prior_passed if c.get("ticket") == key]
    its_bugs = ticket_bugs(key, cases, bugs, known)
    st = lambda c: c.get("status") or "NOT_RUN"
    # a reason names cases, never quotes what they saw: REPORT.md shows that once, in full, under the case's AC
    why = lambda c: (
        f": no result reported for {core.norm_route(c['route'])}"
        if st(c) == "NOT_RUN" and c.get("route") and not (c.get("notes") or c.get("actual"))
        else ""
    )
    unsure = [c for c in cases if key in c.get("id_tickets", ())]  # its result may be another plan case's
    fails = [c for c in mine if st(c) == "FAIL" and c not in unsure]
    if len(fails) == 1:
        c = fails[0]
        failed = [f"{c['id']} failed" + (f" ({c['ac']})" if c.get("ac") else "")]
    else:  # several: one entry naming them all
        acs = sorted(
            {a for c in fails for a in _case_acs(c)},
            key=lambda a: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", a)],
        )
        ids = [c["id"] for c in fails]
        failed = (
            [
                f"{len(ids)} cases failed ({', '.join(ids[:6])}{f', +{len(ids) - 6}' if len(ids) > 6 else ''})"
                + (f" on {', '.join(acs)}" if acs else "")
            ]
            if fails
            else []
        )
    serious = [_bug_word(b) for b in its_bugs if _serious(b)]
    if failed or serious:
        return "FAIL", _some(failed + serious)
    if unread:
        return "BLOCKED", _some([f"{n} unreadable: its results are not counted" for n in unread], 2)
    if not mine and not prior_mine:
        return "BLOCKED", "no test cases"
    stuck = {}  # same reason → one entry naming all its cases, so 12 blocked cases don't repeat one error 12 times
    for c in unsure:
        stuck.setdefault("repeats an id in plan.json: its result can't be told apart", []).append(c["id"])
    for c in mine:
        if st(c) in ("BLOCKED", "NOT_RUN") and c not in unsure:
            stuck.setdefault(f"{'blocked' if st(c) == 'BLOCKED' else 'not run'}{why(c)}", []).append(c["id"])
    ended = [
        f"the run on {core.norm_route(r.get('target')) or name} ended BLOCKED"
        + (f": {' '.join(str(r['summary']).split())}" if r.get("summary") else "")
        for name, r in _blocked_runs(mine, runs)
    ]
    if stuck or ended:
        return "BLOCKED", _some(
            [
                (f"{ids[0]}" if len(ids) == 1 else f"{len(ids)} cases ({ids[0]}–{ids[-1]})") + f" {r}"
                for r, ids in stuck.items()
            ]
            + ended,
            2,
        )
    covered = {a for c in mine for a in _case_acs(c)} | {a for c in prior_mine for a in _case_acs(c)}
    me = str(key).strip().upper()
    plan_keys = {str(k).strip().upper() for k in known} | {me}
    owner = lambda q: str(q.get("ticket") or "").strip().upper()  # '' or a key outside the plan: every ticket's
    qid = lambda q: re.match(r"\s*(Q\d+)\b", q["text"])
    drift = d if isinstance(ticket, dict) and isinstance(d := ticket.get("drift"), str) else ""
    blocking = [
        q
        for q in questions
        if not q["resolved"] and "[BLOCKING]" in q["text"].upper() and (owner(q) == me or owner(q) not in plan_keys)
    ]
    unnumbered = sum(not qid(q) for q in blocking)  # its text is in REPORT.md's What to do: counted here, not quoted
    notes = (
        ([f"requirements changed: {' '.join(drift.split())}"] if drift.strip() else [])
        + [f"{m.group(1)} open blocking question" for q in blocking if (m := qid(q))]
        + ([f"{unnumbered} open blocking question{'s' * (unnumbered != 1)}"] if unnumbered else [])
        + [f"{c['id']} open question" for c in mine if st(c) == "QUESTION"]
        + [_bug_word(b) for b in its_bugs]
        + [
            f"{_ac_id(a)} has no test case"
            for a in _list(ticket, "acs")
            if _ac_id(a) and _ac_key(_ac_id(a)) not in covered
        ]
    )
    if prior_mine:
        pb = ", ".join(sorted({str(c.get("build") or core.UNKNOWN) for c in prior_mine}))
        notes.append(
            f"{len(prior_mine)} case{'s' * (len(prior_mine) != 1)} verified on prior build {pb}; not re-run here"
        )
    if notes:
        return "PASS WITH NOTES", _some(notes)
    return "PASS", f"all {len(mine)} cases passed"


def _verdicts(
    plan: JsonObj,
    runs: Sequence[tuple[str, JsonObj]],
    cases: list[JsonObj],
    bugs: list[JsonObj],
    unread: Sequence[str] = (),
) -> dict[str, tuple[Verdict, str]]:
    """{ticket key: (verdict, reason)} for every plan ticket: the one place verdict() gets its inputs, so REPORT.md,
    the run index and the inbox's Last QA can't disagree. cases and bugs: merged_cases() and run_bugs() of `runs`."""
    tickets = _plan_tickets(plan)
    keys = [t["key"] for t in tickets]
    prior_all, questions = _prior_passed(plan, keys, cases), _q_items(plan, "test plan")
    return {
        t["key"]: verdict(
            t,
            cases,
            bugs,
            bool(runs or unread),
            keys,
            runs,
            [c for c in prior_all if c.get("ticket") == t["key"]],
            questions,
            unread,
        )
        for t in tickets
    }


def cell(v):
    """Text for one table cell or line: `|` escaped, newlines as <br>, and no raw HTML, images or links (an XSS payload,
    `<!--` or `[text](https://…)` typed into a form must show as text, not hide the report or become a hidden-URL link;
    breaking `](` and `]:` stops inline links, images and reference definitions while `[BLOCKING]` stays readable)."""
    return (
        str(v if v is not None else "")
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", "")
        .replace("<", "&lt;")
        .replace("](", "]\\(")
        .replace("]:", "]\\:")
        .strip()
        .replace("\n", "<br>")
    )


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


def _bug_link(bid, run_dir):
    """A link to bugs/BUG-n.md when bid is exactly BUG-n and the draft exists; anything else as escaped text."""
    bid = str(bid or "")
    if re.fullmatch(r"BUG-\d+", bid) and (Path(run_dir) / "bugs" / f"{bid}.md").exists():
        return f"[{bid}](bugs/{bid}.md)"
    return one_line(bid)


def _prior_passed(plan, keys, cases=()):
    """Normalized prior_passed entries from a partial --retest plan.json, then the merged `cases` marked `prior` (a pass
    carry_prior kept from an earlier build): both are passes not re-run here."""
    canon = {k.strip().upper(): k for k in keys}
    kept = [c for c in cases if c.get("prior")]
    ids, out = {c["id"] for c in kept}, []
    for c in _list(plan, "prior_passed"):
        if str(c.get("id")) in ids:
            continue
        tk = c.get("ticket")
        if isinstance(tk, str) and tk.strip():
            tk = canon.get(tk.strip().upper(), tk)
        elif len(keys) == 1:
            tk = keys[0]
        ac = c.get("ac")
        ac = (
            ", ".join(str(a) for a in ac if isinstance(a, (str, int)) and not isinstance(a, bool))
            if isinstance(ac, list)
            else ac
        )
        out.append({**c, "ticket": tk, "ac": ac})
    return out + kept


def _plan_lines(t):
    plan, out = t.get("plan") if isinstance(t.get("plan"), dict) else {}, []
    for key, label in (
        ("scope", "Scope"),
        ("approach", "Approach"),
        ("risks", "Risks"),
        ("out_of_scope", "Out of scope"),
        ("test_data", "Test data"),
    ):
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


def _newest_run_file(run_dir):
    return max((_mtime(f) for f in (Path(run_dir) / ".data").glob("run-*.json")), default=0.0)


def _run_date(run_dir, plan):
    """A folder's date: from its name (a dated run folder), else the local day its newest run file was written (a
    ticket's folder, qa-out/<KEY>/, holds every re-run), else the plan's `created`."""
    m = re.match(r"\d{4}-\d{2}-\d{2}", Path(run_dir).name)
    if m:
        return m.group(0)
    t = _newest_run_file(run_dir)
    if t:
        return datetime.fromtimestamp(t).strftime("%Y-%m-%d")  # noqa: DTZ006 - local, as folder names are (stored times use core.now(), UTC)
    return str(plan.get("created") or "")[:10]


def _run_builds(runs):
    return sorted({str(r.get("build")) for _, r in runs if r.get("build")})


def _draft_title(draft_path, fallback):
    for line in _text(draft_path).splitlines():
        if line.startswith("# "):
            return " ".join(line[2:].split())
    return " ".join(str(fallback or "").split())


def _stored_hash(b):
    """The hash memory keeps the bug under: a re-sighting (a regression's draft) the hash it names, else one from its
    route and symptom (as ingest: a blank symptom falls back to the title)."""
    return core.given_hash(b.get("hash")) or core.bug_hash(
        core.norm_route(b.get("route")),
        core.cap(scrub.redact(core._s(b.get("symptom")))) or core.cap(scrub.redact(core._s(b.get("title")))),
    )


# references/bug-draft.md as checks: (critical, problem, test). Critical = the repro or the record is useless.
SPEC_CHECKS = [
    (True, "has no expect( assertion: it passes while the bug exists", lambda t: "expect(" not in t),
    (False, "doesn't import @playwright/test", lambda t: "@playwright/test" not in t),
    (False, "has no test.use({ baseURL", lambda t: not re.search(r"test\.use\(\s*\{[^;]*baseURL", t, re.S)),
    (False, "has no STORAGE_STATE option (an app behind a sign-in needs one)", lambda t: "STORAGE_STATE" not in t),
    (
        False,
        "opens a literal :id path (use the record's real path)",
        lambda t: re.search(r"goto\(\s*['\"`][^'\"`]*/:\w", t),
    ),
    (
        False,
        "opens an absolute URL (give the path: BASE_URL picks the host)",
        lambda t: re.search(r"goto\(\s*['\"`]https?://", t),
    ),
    (False, "waits with waitForTimeout (wait for the element instead)", lambda t: "waitForTimeout" in t),
]
DRAFT_SECTIONS = ("## Steps", "## Expected", "## Actual", "## Evidence")


def spec_issues(text):
    """[(critical, problem)] of a repro spec's text against references/bug-draft.md."""
    return [(c, f"spec {p}") for c, p, f in SPEC_CHECKS if f(text)]


def draft_issues(text):
    """[(critical, problem)] of a bug draft's text against references/bug-draft.md (the hash is checked by audit_bugs)."""
    out = [] if re.match(r"#[ \t]+\S", text) else [(False, "draft has no '# <title>' first line")]
    out += [
        (False, f"draft has no {h} section") for h in DRAFT_SECTIONS if not re.search(rf"^{re.escape(h)}\b", text, re.M)
    ]
    if "**Oracle:**" not in text:
        out.append((False, "draft names no **Oracle:** (AC, rule or invariant) for the expected result"))
    step1 = re.search(r"^1\.\s+(.*)$", text.split("## Steps", 1)[1], re.M) if "## Steps" in text else None
    if step1 and not re.search(r"https?://", step1.group(1)):
        out.append((False, "draft Step 1 doesn't open the full URL"))
    if re.search(r"\*\*Classification:\*\*\s*CONFIRMED", text) and re.search(r"\*\*Repro:\*\*\s*1/", text):
        out.append((False, "draft says CONFIRMED with Repro 1/…: one hit is SUSPECTED"))
    return out


def audit_bugs(run_dir):
    """{BUG-n: [(critical, problem)]} for the run's CONFIRMED and SUSPECTED bugs: draft and repro spec present and per
    references/bug-draft.md, the draft's hash the one memory stores, an evidence image. Bugs with no problem are left out."""
    run_dir = Path(run_dir)
    out = {}
    for b in run_bugs(load_run(run_dir)["runs"]):
        bid = b.get("id")
        if not bid or _first_word(b.get("classification")) not in ("confirmed", "suspected"):
            continue
        draft, spec, issues = run_dir / "bugs" / f"{bid}.md", run_dir / "repro" / f"{bid}.spec.ts", []
        if not draft.is_file():
            issues.append((True, f"no draft bugs/{bid}.md"))
        else:
            text = _text(draft)
            issues += draft_issues(text)
            m = re.search(r"\*\*qa-mem hash:\*\*\s*`?([0-9a-fA-F]{4,12})\b", text)
            want = _stored_hash(b)
            if not m:
                issues.append((False, "draft has no **qa-mem hash:** line"))
            elif not (want.startswith(m.group(1).lower()) or m.group(1).lower().startswith(want)):
                issues.append(
                    (True, f"draft hash {m.group(1)} is not the bug's ({want}): `qa-mem bug-status` would miss it")
                )
        if not spec.is_file():
            issues.append((True, f"no repro spec repro/{bid}.spec.ts"))
        else:
            issues += spec_issues(_text(spec))
        if not (run_dir / "evidence" / f"{bid}.png").is_file():
            issues.append((False, f"no evidence image evidence/{bid}.png"))
        if issues:
            out[bid] = sorted(issues, key=lambda x: not x[0])
    return out


def carry_prior(run_dir: str | Path, slug: str, keep: bool = False) -> None:
    """Merge .data/prior-<slug>.json (the earlier run file, the date it was written and the case ids given since, saved
    by dispatch) into run-<slug>.json: an explorer dispatched again writes only what it ran, and a rewrite must not lose
    what the earlier attempt found. A given case takes its new result unless that is NOT_RUN; any other case keeps its
    earlier result (even one the explorer copied), stamped with the `build` and `ran` it came from. An earlier bug the
    new file doesn't list (by BUG-n, else hash) stays when a case names it, or when no case ever named it and the build
    is the same (a continuation): a bug whose cases ran again and no longer name it is not carried. Questions are joined
    by text; everything else is the new file's. Idempotent and best effort: an unreadable run file is left as it is,
    with its copy. The copy is removed unless `keep` (the stop hook: a bounced explorer may write its file again)."""
    data = Path(run_dir) / ".data"
    pf, rf = data / f"prior-{slug}.json", data / f"run-{slug}.json"
    try:
        prior = json.loads(pf.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return
    except (OSError, ValueError, RecursionError):
        prior = None  # nothing in it can be merged
    old = prior.get("run") if isinstance(prior, dict) else None
    if isinstance(old, dict):
        try:
            new = json.loads(rf.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            new = old  # the run file is gone: the earlier one is put back
        except (OSError, ValueError, RecursionError):
            return  # the explorer's file can't be read: the stop check sends it back; the copy waits
        if not isinstance(new, dict):
            return
        out = dict(new)
        if new != old:  # equal: the explorer wrote nothing, and the earlier file stands as it is
            ids = prior.get("given") if isinstance(prior.get("given"), list) else []
            given = {str(x) for x in ids if isinstance(x, (str, int))}
            stamp = {"build": core.norm_build(old.get("build"))}
            if isinstance(prior.get("ran"), str):
                stamp["ran"] = prior["ran"]
            news = {str(c.get("id")): c for c in _list(new, "cases")}
            olds = {str(c.get("id")): c for c in _list(old, "cases")}
            ran_again = lambda cid: cid in given and core.case_status(news[cid].get("status")) not in (None, "NOT_RUN")
            cases = [news[cid] if cid in news and ran_again(cid) else {**stamp, **c} for cid, c in olds.items()]
            cases += [c for c in _list(new, "cases") if str(c.get("id")) not in olds]
            if olds:
                out["cases"] = cases
            hash_of = lambda b: _bug_refs({"hash": b.get("hash")})
            listed = {bug_id(b) for b in _list(new, "bugs")} - {""}
            hashes = set().union(*map(hash_of, _list(new, "bugs")))
            named = lambda b, cs: any(_case_bugs(c) & _bug_refs({**b, "id": bug_id(b)}) for c in cs)
            same_build = core.norm_build(new.get("build")) == core.norm_build(old.get("build"))
            carried = [
                b
                for b in _list(old, "bugs")
                if bug_id(b) not in listed
                and not hash_of(b) & hashes
                and (named(b, cases) or (same_build and not named(b, olds.values())))
            ]
            # the explorer names an earlier bug it sees again by its memory hash, under the title the card gave it:
            # that entry and the carried draft are one bug
            title = lambda b: " ".join(core._s(b.get("title")).split())
            again = {title(b): b for b in _list(new, "bugs") if b.get("hash") and not bug_id(b)}
            same = {title(b) for b in carried if not b.get("hash")} & set(again)
            carried = [{**b, "hash": again[title(b)]["hash"]} if title(b) in same else b for b in carried]
            if carried:
                out["bugs"] = [b for b in _list(new, "bugs") if again.get(title(b)) is not b or title(b) not in same]
                out["bugs"] += carried
            text = lambda q: str(q.get("text") if isinstance(q, dict) else q)
            qs = lambda r: r["questions"] if isinstance(r.get("questions"), list) else []
            asked = set(map(text, qs(new)))
            if more := [q for q in qs(old) if text(q) not in asked]:
                out["questions"] = qs(new) + more
        if out != new or not rf.exists():
            core._write(rf, json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    if not keep:
        pf.unlink(missing_ok=True)


def verify_run(run_dir, slug, since=0):
    """What an explorer that is stopping left undone in run file run-<slug>.json, as instructions (empty: complete): the
    run file, the browser signal logs, a status for each case its brief gave, an `actual` on each PASS and FAIL and a
    `bug` on each FAIL, every draft it wrote (since `since`) listed in `bugs`, and the critical problems audit_bugs
    finds in its bugs."""
    run_dir, data = Path(run_dir), Path(run_dir) / ".data"
    rf = data / f"run-{slug}.json"
    try:
        run = json.loads(rf.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return [f"write {rf} (references/run-json.md) as your last file: without it this page's results are lost"]
    except (OSError, ValueError, RecursionError):
        return [f"{rf} is not valid JSON: write it again (references/run-json.md)"]
    if not isinstance(run, dict):
        return [f"{rf} must be one JSON object (references/run-json.md)"]
    todo = [
        f"save the browser signals before closing (step 9): .data/{k}-{slug}.log is missing"
        for k in ("requests", "console")
        if not (data / f"{k}-{slug}.log").is_file()
    ]
    route = re.search(r"^route: (.*)$", _text(data / f"brief-{slug}.md"), re.M)
    plan = _load_json(data / "plan.json", [])
    given = [
        str(c["id"])
        for c in _list(plan, "cases")
        if c.get("id") not in (None, "") and route and core.route_key(c.get("route")) == core.route_key(route.group(1))
    ]
    told = {str(c.get("id")) for c in _list(run, "cases")}
    if missing := [cid for cid in given if cid not in told]:
        todo.append(
            f"give every case of your brief a status in `cases`: {', '.join(missing)} missing "
            "(NOT_RUN with why, when the run ended before you reached it)"
        )
    done = [(str(c.get("id")), core.case_status(c.get("status")), c) for c in _list(run, "cases")]
    if blank := [
        cid for cid, status, c in done if status in ("PASS", "FAIL") and not str(c.get("actual") or "").strip()
    ]:
        todo.append(f"say in `actual` what you saw for each PASS and FAIL case: {', '.join(blank)} need `actual`")
    if no_bug := [cid for cid, status, c in done if status == "FAIL" and not str(c.get("bug") or "").strip()]:
        todo.append(
            f"a FAIL names its bug in `bug` (BUG-<n>, a known bug's hash or a Jira key): {', '.join(no_bug)} failed without a `bug`"
        )
    brief = _text(data / f"brief-{slug}.md")
    open_items = [
        k
        for k in ("UNVISITED STATES", "UNEXERCISED ENDPOINTS", "UNTESTED TARGETS")
        if re.search(rf"^{k} \(", brief, re.M)
    ]
    if (
        open_items
        and _first_word(run.get("status")) == "completed"
        and re.search(r"^mode: explore$", brief, re.M)
        and not any(_list(pg, "coverage") for pg in _list(run, "pages"))
        and not _not_tested(run)
    ):
        todo.append(
            f"your card listed {', '.join(open_items)}, but `coverage` and `not_tested` are both empty: check them, or add a "
            "`not_tested` line for each saying why you did not"
        )
    listed = {bug_id(b) for b in _list(run, "bugs")} - {""}
    drafts = [p.stem for p in sorted((run_dir / "bugs").glob("BUG-*.md")) if p.is_file() and _mtime(p) >= since]
    if unlisted := [x for x in drafts if x not in listed]:
        todo.append(f"list each bug you drafted in `bugs` (draft_path bugs/<id>.md): {', '.join(unlisted)}")
    todo += [
        f"{bid}: {problem}"
        for bid, issues in audit_bugs(run_dir).items()
        if bid in listed
        for critical, problem in issues
        if critical
    ]
    return todo


def _reviews(run_dir, warnings):
    """{BUG-n: (verdict, notes)} from .data/review.json, the qa-reviewer's reply (--verify); {} when there is none."""
    data = _load_json(Path(run_dir) / ".data" / "review.json", warnings)
    out = {}
    for x in _list(data, "bugs"):
        if isinstance(x.get("id"), str) and str(x.get("verdict")).strip().lower() in ("solid", "needs_check"):
            out[x["id"]] = (str(x["verdict"]).strip().lower(), " ".join(str(x.get("notes") or "").split()))
    return out


def _repro_badge(bid, b, audit, reviews=None):
    """'repro ✓' or '⚠ <worst problem> (+n)' for a CONFIRMED or SUSPECTED draft, and the reviewer's word when there is
    one (a note for the user: it never changes a verdict); '' otherwise."""
    if not bid or _first_word(b.get("classification")) not in ("confirmed", "suspected"):
        return ""
    issues = audit.get(bid, [])
    badge = (
        "repro ✓"
        if not issues
        else f"⚠ {issues[0][1]}" + (f" (+{len(issues) - 1}: qa-mem audit-bugs)" if len(issues) > 1 else "")
    )
    verdict, notes = (reviews or {}).get(bid, (None, ""))
    if verdict == "solid":
        badge += " · reviewed ✓"
    elif verdict == "needs_check":
        badge += f" · ⚠ check before filing: {notes or 'see .data/review.json'}"
    return badge


def _db_bug_states():
    """{hash: (status, jira_key)} from the memory DB, opened read-only; {} without one (the report renders without it)."""
    import sqlite3

    p = core.db_path()
    if not p.is_file():
        return {}
    try:
        con = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return {r[0]: (r[1], r[2]) for r in con.execute("SELECT hash, status, jira_key FROM bugs")}
        finally:
            con.close()
    except sqlite3.Error:
        return {}


LEAD_RANK = {"HIGH": 0, "MEDIUM": 1}


def _leads(runs, skip=()):
    """Follow-up leads the explorers left (`leads` in their run files), one per route and target, HIGH first; routes in
    `skip` (already explored) left out."""
    seen, out = set(), []
    for _, r in runs:
        for lead in _list(r, "leads"):
            route = core.norm_route(lead.get("route"))
            key = (route, core.norm(lead.get("target")))
            if route and route not in skip and key not in seen:
                seen.add(key)
                out.append({**lead, "route": route, "priority": str(lead.get("priority") or "MEDIUM").strip().upper()})
    return sorted(out, key=lambda x: LEAD_RANK.get(x["priority"], 2))


# ---------- browser signals: playwright-cli `requests --all` / `console error --all`, saved by the explorer at its end ----
REQUEST_LINE = re.compile(
    r"\s*(\d+)\.\s+\[([A-Z]+)\]\s+(\S+)\s+=>\s+\[(\d{3}|FAILED)\]\s*(.*)"
)  # 3. [GET] url => [404] text
SIGNALS_SHOWN = 15


def _text(path):
    try:
        # not a vulnerability (S2083): callers pass fixed names inside the run folder; bug ids are validated BUG-n
        return Path(path).read_text(encoding="utf-8", errors="replace")  # NOSONAR
    except OSError:
        return ""


def _names_request(named, method, path):
    """Whether lower-cased text names a call by method and path: 'POST /api/zones' or 'POST http://host/api/zones'.
    A path alone (the run's own target, a passing note's 'GET /api/zones') explains no other call failing there.
    A draft naming the record placeholder ('GET /api/zones/:id') names the call on a record ('/api/zones/42')."""
    paths = {path.lower(), core._ids(path).lower() or path.lower()}
    return any(
        re.search(rf"\b{re.escape(method.lower())}\s+(?:https?://[^/\s]+)?{re.escape(p)}(?![\w/.-])", named)
        for p in paths
    )


STORM_CALLS = 5  # the same call this often in one page visit: a fetch loop, likely
SUSPECT_200_SHOWN = 3  # calls triage names for a response-body check: each one costs a browser command
# `qa-mem body-check --all`: the bodies one Bash call reads, well inside its two-minute limit
SWEEP_CALLS, SWEEP_SECONDS, SWEEP_CALL_SECONDS = 30, 60, 15
FAILED_COUNTS = ("failed", "failedCount", "failureCount", "failures", "errorCount")
GQL_NAME = re.compile(r"[A-Za-z_]\w{0,59}", re.ASCII)  # a GraphQL operation name: it goes into a signal's key
RPC_PATH = re.compile(r"graphql|/gql\b|rpc", re.I)
API_PATH = re.compile(r"/api(?:/|$)|graphql|/gql\b|rpc|\.json$|/v\d+/", re.I)  # where a 200 HTML page is wrong
_NOT_A_URL = (
    r"(?<![:/\w])"  # a browser stack frame is the app's own JS (http://host/main.js:10); a server frame has a file path
)
LEAKS = (  # what a server must never show a client: (label, pattern)
    (
        "stack trace",
        re.compile(
            r"Traceback \(most recent call last\)|\bNullPointerException\b|\bNullReferenceException\b|\bException in thread\b"
            r"|\bat\s+[^\n(]*\(?" + _NOT_A_URL + r"(?:/|[A-Za-z]:\\)[^\s:)]+\.(?:js|ts|java|cs|py|rb|php|go):\d+"
            r"|\bat \S+ in \S+\.cs:line \d+"
        ),
    ),
    (
        "SQL error",
        re.compile(
            r"\bSQLSTATE\b|\bORA-\d{4,5}\b|syntax error at or near|\bPG::\w+|SequelizeDatabaseError|error in your SQL syntax"
            r"|\bsqlite3?\.OperationalError|\bpsycopg2\.\w+|(?:column|relation) \S+ does not exist",
            re.I,
        ),
    ),
    (
        "framework debug page",
        re.compile(
            r"Whitelabel Error Page|you have DEBUG = True|Server Error in '[^']*' Application|Werkzeug Debugger"
            r"|Whoops, looks like something went wrong|ActionController::RoutingError|Django Version:",
            re.I,
        ),
    ),
)


PROFILE_SEGMENTS = {
    "me",
    "profile",
    "userinfo",
    "whoami",
    "currentuser",
}  # the explorer never reads these bodies either


def _credential_call(path):
    """Whether a path is a login, session, token or user-profile call, whose body carries credentials."""
    return bool(probe._auth_segment(path)) or any(
        re.sub(r"[-_]", "", x.lower()) in PROFILE_SEGMENTS for x in path.split("/")
    )


def _err_text(e):
    """The message inside an error value (`"x"`, `[{"message": "x"}]`, `{"detail": "x"}`), at most 80 characters."""
    if isinstance(e, list):
        e = e[0] if e else ""
    if isinstance(e, dict):
        e = next(
            (e[k] for k in ("message", "detail", "title", "error", "code") if e.get(k)),
            json.dumps(e, ensure_ascii=False),
        )
    return "" if isinstance(e, bool) else core.short(scrub.redact(" ".join(str(e).split())), 80)


def _partial_failure(v, where=""):
    """The label of a batch write that says some of its items failed (`failed: 1`, an item with `ok` false), or None."""
    for k in FAILED_COUNTS:
        if type(v.get(k)) is int and v[k] > 0:  # a whole number: `failed: true` and a list are something else
            return f"partial failure: {where}{k} {v[k]}"
    for k in ("results", "items"):
        rows = v[k] if isinstance(v.get(k), list) else []
        bad = [x for x in rows if isinstance(x, dict) and (x.get("ok") is False or x.get("success") is False)]
        if bad:
            m = _err_text(next((bad[0][f] for f in ("reason", "message", "error", "detail") if bad[0].get(f)), ""))
            return f"partial failure: {where}{k}: {len(bad)} of {len(rows)} failed" + (f": {m}" if m else "")
    return None


def body_verdict(text, path="", status=200, write=False):
    """What is wrong with one response body, as short labels ([] when nothing): a leak signature (stack trace, SQL error,
    framework debug page) in any status; on a 2xx also a JSON error envelope (`error`, `errors`, GraphQL `errors`,
    `success`/`ok` false, `status` "error"), a GraphQL mutation's own failure (`data.<field>.userErrors`, or
    `data.<field>.errors` beside nothing else: a record with an `errors` column is data) and, on an API-looking path, an
    HTML page (a login redirect, a proxy page). write: the call changes data, so a batch result with failed items is a
    partial failure (in a read, `failed: 3` is a count the page shows). A 4xx or 5xx that says so in JSON is the right
    answer, so only its leaks count."""
    text = str(text or "")[:200_000]
    out = []
    if 200 <= status < 300:
        try:
            v = json.loads(text)
        except (ValueError, RecursionError):
            v = None
        if isinstance(v, dict):
            for k in ("errors", "error"):
                if v.get(k):
                    out.append(f"error body: {k}" + (f": {m}" if (m := _err_text(v[k])) else ""))
                    break
            else:
                flag = next((k for k in ("success", "ok") if v.get(k) is False), None)
                if flag or str(v.get("status")).lower() in ("error", "fail", "failed", "failure"):
                    m = _err_text(
                        next((v[k] for k in ("message", "detail", "error_description", "reason") if v.get(k)), "")
                    )
                    out.append(
                        f"error body: {flag + ' false' if flag else 'status ' + str(v['status']).lower()}"
                        + (f": {m}" if m else "")
                    )
            data = v["data"] if isinstance(v.get("data"), dict) else {}
            fields = [(f"data.{k}.", f) for k, f in list(data.items())[:20] if isinstance(f, dict)]
            for where, f in fields:
                alone = not any(x for k, x in f.items() if k not in ("errors", "__typename"))
                key = "userErrors" if f.get("userErrors") else "errors" if f.get("errors") and alone else None
                if key:
                    out.append(f"error body: {where}{key}" + (f": {m}" if (m := _err_text(f[key])) else ""))
            if write:
                out += [x for x in (_partial_failure(o, where) for where, o in [("", v), *fields]) if x]
        elif re.match(r"\s*<(?:!doctype html|html)\b", text, re.I) and API_PATH.search(path):
            out.append("HTML on an API endpoint")
    out += [f"leak: {label}" for label, rx in LEAKS if rx.search(text)]
    return out


def signal_severity(key):
    """'severe' (a 5xx, a failed request, a 200 with an error body, a leak signature), 'client' (a 4xx) or 'other'
    (a request loop, a console error) for a triage key, with or without the [NEW] / (seen in N runs) marks."""
    if "[leak:" in key or re.search(r"→ (?:5\d\d|FAILED)\b|→ 2\d\d \[", key):
        return "severe"
    return "client" if re.search(r"→ 4\d\d\b", key) else "other"


def _bodies_file(data, slug):
    try:
        v = json.loads((data / f"bodies-{slug}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _bodies(data, slug):
    """{request number: [finding]} qa-mem body-check recorded in .data/bodies-<slug>.json ({} without one)."""
    return {
        str(k): [scrub.redact(str(f)) for f in fs if isinstance(f, str)]
        for k, fs in _bodies_file(data, slug).items()
        if isinstance(fs, list)
    }


def _body_gone(data, slug):
    """The request numbers whose body the browser no longer held when body-check asked (the file's `gone`): not
    judged, and nothing more to read."""
    v = _bodies_file(data, slug).get("gone")
    return {str(x) for x in v} if isinstance(v, list) else set()


def _body_ops(data, slug):
    """{request number: GraphQL operation name} of the calls `qa-mem body-check --all` checked (the file's `ops`)."""
    v = _bodies_file(data, slug).get("ops")
    return (
        {str(k): n for k, n in v.items() if isinstance(n, str) and GQL_NAME.fullmatch(n)} if isinstance(v, dict) else {}
    )


def check_body(run_dir, slug, index, text, op="", mutation=False):
    """Judge the body of request `index` (the number `requests` printed) and record the verdict, clean or not, in
    .data/bodies-<slug>.json for triage_signals. The method, path and status come from .data/requests-<slug>.log when it
    has the request. op, mutation: the GraphQL operation's name and whether it writes, when the caller read the request
    (every operation shares one method and path, so the name tells them apart). An empty body of a 2xx that should
    have one is not judged: the browser drops a response's body when the page navigates, so empty says nothing. It is
    recorded as gone, never as clean. Returns (what it was, findings), findings None when there was no body."""
    data = Path(run_dir) / ".data"
    line = next(
        (
            m
            for m in map(REQUEST_LINE.fullmatch, _text(data / f"requests-{slug}.log").splitlines())
            if m and m.group(1) == index
        ),
        None,
    )
    path, status = "", 200
    if line:
        try:
            path = urlsplit(line.group(3)).path
        except ValueError:
            pass
        status = int(line.group(4)) if line.group(4).isdigit() else 0
    write = mutation if RPC_PATH.search(path) else line is not None and line.group(2) in probe.API_WRITES
    no_body = not text.strip() and 200 <= status < 300 and status not in (204, 205)
    findings = None if no_body else [scrub.redact(f) for f in body_verdict(text, path, status, write)]
    recorded: dict[str, Any] = _bodies(data, slug)
    names, gone = _body_ops(data, slug), _body_gone(data, slug)
    if findings is None:
        gone.add(index)
    else:
        recorded[index] = findings
    if GQL_NAME.fullmatch(op):
        names[index] = op
    if names:
        recorded["ops"] = names
    if gone:
        recorded["gone"] = sorted(gone, key=lambda x: (len(x), x))
    tmp = data / f".bodies-{slug}.tmp"
    tmp.write_text(json.dumps(recorded, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, data / f"bodies-{slug}.json")
    shown = path + (f"#{names[index]}" if index in names else "")
    return (f"{line.group(2)} {shown} → {line.group(4)}" if line else ""), findings


def _gql_operation(text):
    """(operation name or '', whether it is a mutation) of a GraphQL request body; ('', False) for anything else."""
    try:
        sent = json.loads(text)
    except (ValueError, RecursionError):
        return "", False
    name, mutation = "", False
    for o in (sent if isinstance(sent, list) else [sent])[:10]:  # a batch: named after its first operation
        if not isinstance(o, dict):
            continue
        query = o["query"] if isinstance(o.get("query"), str) else ""
        said = o["operationName"] if isinstance(o.get("operationName"), str) else ""
        m = re.search(rf"\b(query|mutation|subscription)\s+{re.escape(said)}\b", query) if said else None
        m = m or re.match(r"\s*(query|mutation|subscription)\b\s*(\w*)", query)
        said = said or (m.group(2) if m and m.lastindex == 2 else "")
        name = name or (said if GQL_NAME.fullmatch(said) else "")
        mutation = mutation or bool(m and m.group(1) == "mutation")
    return name, mutation


def _sign_in_operation(name):
    """Whether a GraphQL operation is named like a sign-in call (RefreshToken, login): its answer carries credentials."""
    return any(w.lower() in probe.API_AUTH_SEGMENTS for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", name))


def _saved_body(text):
    """A response body as text: playwright-cli saves a binary one to .playwright-cli/response-*.<ext> and prints the
    path instead, so that file is removed and the body is '' (nothing to read)."""
    p = Path(text.strip()) if re.fullmatch(r"\S*\.playwright-cli[/\\]response-[^/\\\s]+", text.strip()) else None
    if p is None:
        return text
    p.unlink(missing_ok=True)
    return ""


def sweep_bodies(run_dir, slug):
    """`qa-mem body-check <run> --slug <slug> --all`: with the browser still open, save its request list as
    .data/requests-<slug>.log (as step 9 does), then read the body of every call triage would name (triage_signals'
    `unchecked`: GraphQL and RPC calls, writes, then failed calls; never a sign-in path) and record each verdict, at
    most SWEEP_CALLS in SWEEP_SECONDS. A GraphQL call's request body is read first for its operation name and kind, and
    is never stored; an operation named like a sign-in is settled without reading its answer. A body the browser no
    longer holds (the page navigated since the call) is counted as gone, never as clean: run this before a reload.
    Returns {"checked": [(request number, what it was, findings)], "gone": n, "unread": n, "why": the first reason one
    could not be read, "left": calls past the cap}."""
    from . import crawl  # the playwright-cli runner; crawl imports nothing from this module

    log = Path(run_dir) / ".data" / f"requests-{slug}.log"
    ok, listed = crawl._pw(["requests", "--all"], SWEEP_CALL_SECONDS)
    if ok:
        core._write(log, listed)
    elif not log.is_file():
        raise core.BadInput(
            f"body-check --all: the browser isn't open and there is no {log} ({core.short(' '.join(listed.split()), 120)})"
        )
    todo = triage_signals(run_dir, slug, {})["unchecked"]
    checked, gone, unread, why, deadline = [], 0, 0, "", time.monotonic() + SWEEP_SECONDS
    for index, _method, path in todo[:SWEEP_CALLS]:
        if time.monotonic() > deadline:
            break
        op, mutation, body = "", False, "{}"  # (a sign-in operation is settled as an empty answer, unread)
        if RPC_PATH.search(path):
            ok, sent = crawl._pw(["--raw", "request-body", index], SWEEP_CALL_SECONDS)
            op, mutation = _gql_operation(sent) if ok else ("", False)
        if not _sign_in_operation(op):
            ok, body = crawl._pw(["--raw", "response-body", index], SWEEP_CALL_SECONDS)
            if not ok:  # the browser dropped the request, or it is closed: the call stays a suspect
                unread, why = unread + 1, why or core.short(" ".join(body.split()), 120)
                continue
            # a binary body was saved to a file: nothing to read in it, and not a body that is gone
            body = body if _saved_body(body) == body else "{}"
        what, findings = check_body(run_dir, slug, index, body[:200_000], op, mutation)
        if findings is None:
            gone += 1
        else:
            checked.append((index, what, findings))
    left = len(todo) - len(checked) - gone - unread
    return {"checked": checked, "gone": gone, "unread": unread, "why": why, "left": left}


def triage_signals(run_dir, slug, run, hosts=None):
    """{"app": [(signal, count, first request index or None)], "third_party": count, "suspect_200": [(call, index)],
    "unchecked": [(index, method, path)] (every call whose body nobody has read yet, for `body-check --all`)} from
    .data/requests-<slug>.log, console-<slug>.log and bodies-<slug>.json (what `qa-mem body-check` found in the bodies the
    explorer read): failed requests (4xx, 5xx, FAILED), a 2xx whose body was an error envelope, an HTML page or a leak, a
    call made STORM_CALLS+ times (a request loop), and console errors, each once, minus those the run JSON or a bug draft
    names (a request by its method and path, a message by its first 60 characters), and minus the requests the
    explorer's own `qa-mem api-probe` sent (.data/probes-<slug>.jsonl). A leak signature (stack trace, SQL
    error) in a body or a console message is added to the signal in brackets. A failed request to a host outside
    allowedHosts (a CDN, map tiles) is only counted. "Failed to load resource" lines repeat the requests log and are
    skipped. `suspect_200` is up to SUSPECT_200_SHOWN 2xx calls nobody has checked or named whose body the log can't show
    (GraphQL and RPC calls, then writes, never login, session or token paths): the explorer reads their bodies while the
    browser is open. hosts: allowedHosts (None: the config's; empty: every host is the app's)."""
    data, found, calls, third = Path(run_dir) / ".data", {}, {}, 0
    hosts = core.allowed_hosts(core.config()) if hosts is None else hosts
    checked, seen_2xx, checked_shapes = _bodies(data, slug), [], set()
    names, gone, unchecked = _body_ops(data, slug), _body_gone(data, slug), []
    probes = []  # [method, url, status] of the requests `qa-mem api-probe` sent (probe._note_probe): the explorer's own
    for line in _text(data / f"probes-{slug}.jsonl").splitlines():
        try:
            p = json.loads(line)
            probes.append([str(p["method"]), scrub.redact(str(p["url"])), str(p["status"])])
        except (ValueError, KeyError, TypeError):
            continue
    probe_failed = {url for _, url, status in probes if status == "FAILED"}  # the console names these too
    for line in scrub.redact(_text(data / f"requests-{slug}.log")).splitlines():
        m = REQUEST_LINE.fullmatch(line)
        if not m:
            continue
        index, method, url, code, text = m.groups()
        if [method, url, code] in probes:  # one log line for each probe: the page's own calls of that URL stay
            probes.remove([method, url, code])
            continue
        try:
            path = urlsplit(url).path or url
        except ValueError:  # an odd host such as an unclosed [IPv6
            path = url
        if hosts and url.lower().startswith(("http://", "https://")) and not core.url_allowed(url, hosts):
            third += code == "FAILED" or int(code) >= 400
            continue
        shape = core._ids(path) or path
        if method == "GET":  # a POST many times is a user (or a test) saving records, not a fetch loop
            calls.setdefault((method, shape), [0, index])[0] += 1
        notes = f" [{'; '.join(checked[index])}]" if checked.get(index) else ""  # what body-check saw in its body
        rpc = bool(RPC_PATH.search(path))
        # every GraphQL operation shares one method and path: a check settles its own call, and the operation's name
        # (when body-check read it) is part of what the call is called
        shown = path + (f"#{names[index]}" if index in names else "")
        if index in checked and not rpc:
            checked_shapes.add((method, shape))
        if index in gone:  # its body was gone when body-check asked: nothing more to read, and no verdict
            pass
        elif index not in checked and code != "FAILED" and not _credential_call(path):  # body-check --all's work list
            turn = (2 if int(code) >= 500 else 3) if int(code) >= 400 else 0 if rpc else 1
            if turn != 1 or method in probe.API_WRITES:
                unchecked.append((turn, index, method, path))
        if code == "FAILED" or int(code) >= 400:
            status = code if code != "FAILED" else f"FAILED {text}".strip()
            found.setdefault(f"{method} {shown} → {status}{notes}", [(method, shown), 0, index])[1] += 1
        elif notes:
            found.setdefault(f"{method} {shown} → {code}{notes}", [(method, shown), 0, index])[1] += 1
        elif index not in checked and index not in gone:
            seen_2xx.append((method, path, shape, index))
    for (method, shape), (n, index) in calls.items():
        if n >= STORM_CALLS:
            found[f"{method} {shape} ×{n} (the same call {n} times: a request loop?)"] = [(method, shape), 1, index]
    for line in scrub.redact(_text(data / f"console-{slug}.log")).splitlines():
        if not line.strip() or line[:1].isspace() or line.startswith(("###", "Total messages:", "Returning ")):
            continue  # blank, stack lines, headers
        level = re.match(r"\[(\w+)\]\s*", line)  # a page error (uncaught exception) has no [LEVEL]
        msg = re.sub(r"\s+@\s+\S*$", "", line[level.end() :] if level else line).strip()  # " @ <source>:1"
        if any(url in msg for url in probe_failed):  # a probe the browser refused (CORS): its own error message
            continue
        if msg and not msg.startswith("Failed to load resource:") and (not level or level.group(1).upper() == "ERROR"):
            leaks = "".join(
                f" [leak: {label}]" for label, rx in LEAKS if rx.search(msg)
            )  # the server's own words in the page
            found.setdefault(f"console: {msg}{leaks}", [msg[:60], 0, None])[1] += 1
    drafts = "".join(map(_text, sorted((Path(run_dir) / "bugs").glob("*.md"))))
    named = (json.dumps(run, ensure_ascii=False) + drafts).lower()  # a console message: any mention in the run
    # a failed request: bugs, notes, non-PASS cases and drafts only; a passing fact or locator that mentions the path
    # (`POST /form saves the zone`) must not hide `POST /form → 500`
    cases = [c for c in _list(run, "cases") if core.case_status(c.get("status")) != "PASS"]
    explains = (json.dumps([_list(run, "bugs"), run.get("notes"), cases], ensure_ascii=False) + drafts).lower()
    explained = lambda w: _names_request(explains, *w) if isinstance(w, tuple) else w.lower() in named
    rank = lambda c: 0 if RPC_PATH.search(c[1]) else 1
    suspects = {}
    for method, path, shape, index in sorted(
        (c for c in seen_2xx if (RPC_PATH.search(c[1]) or c[0] in probe.API_WRITES) and not _credential_call(c[1])),
        key=rank,
    ):
        if (method, shape) not in checked_shapes and not explained((method, path)):
            suspects.setdefault((method, shape), index)
    return {
        "app": [(key, n, index) for key, (what, n, index) in found.items() if not explained(what)],
        "third_party": third,
        "suspect_200": [(f"{method} {shape}", index) for (method, shape), index in suspects.items()][
            :SUSPECT_200_SHOWN
        ],
        "unchecked": [x[1:] for x in sorted(unchecked, key=lambda x: x[0])],
    }


def _signal_baseline():
    """{(page, kind, key): (status, runs seen, first run)} from the memory DB, opened read-only; {} without one (the
    report renders without a DB too)."""
    import sqlite3

    p = core.db_path()
    if not p.is_file():
        return {}
    try:
        con = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return {
                (r[0], r[1], r[2]): (r[3], r[4], r[5])
                for r in con.execute("SELECT page, kind, sig_key, status, seen_runs, first_run FROM signals")
            }
        finally:
            con.close()
    except sqlite3.Error:
        return {}


def _signals(run_dir, name, run, hosts=None, baseline=None):
    """([(signal, count)], third-party count) of run file `name` (triage_signals). With the memory's baseline: a signal
    the user ignored is left out, one first seen in this run is marked [NEW], one seen before says in how many runs."""
    from . import memory

    t = triage_signals(run_dir, name[len("run-") : -len(".json")], run, hosts)
    target = str(run.get("target") or "")
    page = core.norm_route(target) if target.startswith(("/", "http", "#")) else "*"
    out = []
    for key, n, _ in t["app"]:
        status, runs, first = (baseline or {}).get((page, *(memory.signal_key(key) or ("", ""))), (None, 0, None))
        if status == "IGNORED":
            continue
        out.append(
            (
                "[NEW] " + key
                if first is not None and first == str(run.get("run_id"))
                else key + (f" (seen in {runs} runs)" if runs >= 2 else ""),
                n,
            )
        )
    return out, t["third_party"]


# ---------- questions: plan, runs and QUESTION cases, merged; a string is open, {"text","status","answer"} as written ----
def _q_items(obj, label):
    out = []
    raw = obj.get("questions") if isinstance(obj, dict) else None
    for x in raw if isinstance(raw, list) else []:
        if isinstance(x, dict):
            text, status, ans, tk = (
                str(x.get("text") or ""),
                x.get("status"),
                str(x.get("answer") or ""),
                x.get("ticket"),
            )
        elif isinstance(x, (str, int, float)) and not isinstance(x, bool):
            text, status, ans, tk = str(x), None, "", None
        else:
            continue
        text = " ".join(text.split())
        if text:
            out.append(
                {
                    "text": text,
                    "resolved": str(status or "").lower() == "resolved",
                    "answer": " ".join(ans.split()),
                    "where": label,
                    "ticket": tk,
                }
            )
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


def _asked(c, open_qs):
    """Whether an open question is this QUESTION case's own: it names the case, or one says what the other says.
    Found live: What to do listed one question twice, by its text and by its case."""
    flat = lambda x: " ".join(re.sub(r"[^\w\s]", " ", core._s(x).lower()).split())
    said = [t for t in (flat(c.get("notes")), flat(c.get("actual"))) if len(t) >= 12]
    for q in open_qs:
        text = flat(q["text"])
        if re.search(rf"\b{re.escape(flat(c['id']))}\b", text) or any(
            t in text or text in t for t in said if len(text) >= 12
        ):
            return True
    return False


def _q_rank(q):
    return 0 if "[BLOCKING]" in q["text"].upper() else 1 if "[HIGH]" in q["text"].upper() else 2


# ---------- REPORT.md blocks: each a string; the report joins them with a blank line, so every heading, list, table,
# fenced block and fold has one around it
def _plural(n, word):
    return f"{n} {word}{'s' * (n != 1)}"


def _line(bits):
    """One text line of the report from its parts, escaped (a leading `#` or ``` can't head or fence the rest)."""
    s = " · ".join(one_line(b) for b in bits if b)
    return ("\\" if s[:1] in tuple("#`~>-+*=_") else "") + s


def _indent(text, n):
    return "\n".join(" " * n + ln if ln else ln for ln in text.split("\n"))


def _items(items):
    """A list from its item texts: a blank line between the items when one of them holds more than a line."""
    return ("\n\n" if any("\n" in i for i in items) else "\n").join(items)


def _numbered(items):
    out = []
    for n, item in enumerate(items, 1):
        first, _, rest = item.partition("\n")
        out.append(f"{n}. {first}" + ("\n" + _indent(rest, len(f"{n}. ")) if rest else ""))
    return _items(out)


def _fold(summary, blocks, opened=False):
    """A <details> fold: `summary` is plain text (HTML-escaped here), `blocks` Markdown."""
    head = f"<details{' open' if opened else ''}><summary>{summary_text(summary)}</summary>"
    return "\n\n".join([head, *blocks, "</details>"])


def _link(text, target):
    """[text](target): the text escaped, `target` a relative path already URL-quoted."""
    shown = one_line(text).replace("[", "\\[").replace("]", "\\]")
    return f"[{shown}]({target})"


def _sh(lines):
    return "```sh\n" + "\n".join(lines) + "\n```"


def _worst(cases):
    return next(s for s in STATUS_RANK if any(c["status"] == s for c in cases))


def _case_item(c, run_dir, with_status):
    """A case as a list item: id, title and result in bold, then what it saw and its image; its page, steps and expected
    result folded (open when it did not pass)."""
    esc = lambda v: one_line(v).replace("*", "\\*")
    head = " ".join(x for x in (esc(c["id"]), esc(c.get("title"))) if x)
    if with_status:
        word = CASE_RESULT_WORD[c["status"]]
        if c.get("prior"):
            word += f" on build {esc(c.get('build') or core.UNKNOWN)}, not re-run here"
        head += f": {word}" + (f" ({_bug_link(c['bug'], run_dir)})" if c.get("bug") else "")
    actual, notes = cell(c.get("actual")), cell(c.get("notes"))
    img = _evidence_file(run_dir, c["id"]) if with_status else ""
    shown = " · ".join(
        x
        for x in (
            f"{actual}{'' if actual[-1] in '.!?:…' else '.'} Notes: {notes}" if actual and notes else actual or notes,
            f"[image]({img})" + (" (not highlighted)" if c.get("annotated") is False else "") if img else "",
        )
        if x
    )
    inner = [
        " · ".join(
            x
            for x in (
                f"Page: {one_line(c['route'])}" if c.get("route") else "",
                f"priority {one_line(c['priority'])}" if c.get("priority") else "",
            )
            if x
        )
    ]
    steps = c.get("steps")
    if isinstance(steps, list):
        inner.append("\n".join(f"{i}. {paragraph(x)}" for i, x in enumerate(steps, 1)))
    elif steps:
        inner.append(paragraph(steps))
    inner += [f"Expected: {cell(c['expected'])}"] if c.get("expected") else []
    inner = [b for b in inner if b]
    line = f"- **{head}.**" + (f" {shown}" if shown else "")
    if not inner:
        return line
    return line + "\n\n" + _indent(_fold("Steps", inner, with_status and c["status"] != "PASS"), 2)


def _results(t, cases, run_dir, with_status, level):
    """Blocks for a ticket's cases: its test plan folded, a heading per AC with the cases that cover it (a case under the
    first AC it names, the others pointing there), then the ticket's cases that name none of its ACs."""
    blocks = [_fold("Test plan", ["\n".join(_plan_lines(t))])] if _plan_lines(t) else []
    placed, acs = {}, _list(t, "acs")
    for ac in acs:
        aid = _ac_id(ac)
        covering = [c for c in cases if aid and _ac_key(aid) in _case_acs(c)]
        now = [c for c in covering if not c.get("prior")]
        state = (
            "not covered by a case"
            if not covering
            else ""
            if not with_status
            else CASE_RESULT_WORD[_worst(now)]
            if now
            else "PASS on build " + ", ".join(sorted({str(c.get("build") or core.UNKNOWN) for c in covering}))
        )
        title = " ".join(x for x in (one_line(aid), one_line(ac.get("text"))) if x) or "untitled"
        blocks.append(f"{'#' * level} {title}" + (f": {one_line(state)}" if state else ""))
        items = []
        for c in covering:
            if c["id"] in placed:
                items.append(f"- See {one_line(c['id'])} under {placed[c['id']]}.")
            else:
                placed[c["id"]] = one_line(aid)
                items.append(_case_item(c, run_dir, with_status))
        blocks += [_items(items)] if items else []
    rest = [c for c in cases if c["id"] not in placed]
    if rest:
        blocks += [f"{'#' * level} Other cases"] if acs else []
        blocks.append(_items([_case_item(c, run_dir, with_status) for c in rest]))
    return blocks


def _env_line(plan, runs):
    """The app's URL and the browser the runs used (plan.json is written before any browser opens: the runs win)."""
    env = plan.get("env") if isinstance(plan.get("env"), dict) else {}
    renvs = [r["env"] for _, r in runs if isinstance(r.get("env"), dict)]
    used = ", ".join(sorted({str(e["browser"]) for e in renvs if e.get("browser")})) or str(env.get("browser") or "")
    url = str(env.get("url") or (renvs[0].get("url") if renvs else "") or "")
    return _line([url, f"browser {used}" if used else ""])


def _ran_as(runs):
    """'as <tenant and role>' the runs recorded (a tenant or role changes what a page shows), else ''."""
    renvs = [r["env"] for _, r in runs if isinstance(r.get("env"), dict)]
    ran = sorted({" ".join(scrub.redact(str(e["context"])).split()) for e in renvs if e.get("context")})
    return "as " + " / ".join(ran[:3]) if ran else ""


def _jira_of(b, states):
    """(status, Jira key) of the memory bug this one is (filed before, now seen again), else (None, None)."""
    h = _stored_hash(b)
    return next(
        ((st, k) for full, (st, k) in states.items() if k and full.startswith(h) and ops.KEY_RE.fullmatch(k)),
        (None, None),
    )


def _bug_label(b):
    return b.get("id") or (f"known bug {str(b.get('hash'))[:12]}" if b.get("hash") else "")


def _bug_title(b, run_dir):
    bid = b.get("id") or ""
    return b.get("title") or (_draft_title(Path(run_dir) / "bugs" / f"{bid}.md", "") if bid else "")


def _bug_name(b, run_dir, linked=True):
    """'[BUG-n](bugs/BUG-n.md) title' (its id linked when the draft exists), 'known bug <hash> title' or the title."""
    ref = _bug_link(b["id"], run_dir) if b.get("id") and linked else one_line(_bug_label(b))
    return " ".join(x for x in (ref, one_line(_bug_title(b, run_dir))) if x) or "untitled bug"


def _sev(b):
    return " ".join(str(b.get("severity") or "").split()).upper() or "unrated"


def _bug_todo(b, run_dir, states):
    """The What to do line of one bug: comment on the Jira key memory links it to, file a CONFIRMED draft, or check it."""
    cls = _first_word(b.get("classification"))
    sev = _sev(b) + ("" if cls in ("confirmed", "") else f", {' '.join(str(b['classification']).split()).upper()}")
    what = f"{_bug_name(b, run_dir)} ({one_line(sev)})"
    status, known = _jira_of(b, states)
    if known:
        return f"Comment on {known} (already in Jira, {one_line(status)}): {what}"
    draft = Path(run_dir) / "bugs" / f"{b.get('id')}.md"
    return ("File the bug: " if cls == "confirmed" and b.get("id") and draft.is_file() else "Check the bug: ") + what


def _filing(b, owners, run_abs, cfg, states):
    """[the list item with a CONFIRMED draft's commands]: a new bug's create (and link, with jiraBugLinkType) and its
    bug-status follow-up, or a comment on the Jira key memory links it to; then the files to attach. [] otherwise."""
    bid = b.get("id") or ""
    draft = run_abs / "bugs" / f"{bid}.md"
    if not bid or _first_word(b.get("classification")) != "confirmed" or not draft.is_file():
        return []
    status, known = _jira_of(b, states)
    if known:  # filed before: the news goes on that issue, not on a duplicate
        say = f"already in Jira as {known} ({one_line(status)}): add this draft there as a comment"
        cmds = [f"acli jira workitem comment create --key {known} --body-file {shlex.quote(str(draft))}"]
    else:
        proj = next(
            (o.split("-", 1)[0] for o in owners if ops.KEY_RE.fullmatch(o.upper())),
            str(cfg.get("jiraProject") or "").strip() or "<PROJ>",
        )
        ticket = next((o.upper() for o in owners if ops.KEY_RE.fullmatch(o.upper())), None)
        link_type = str(cfg.get("jiraBugLinkType") or "").strip()
        say = "File it"
        cmds = [
            f"acli jira workitem create --project {shlex.quote(proj)} --type Bug "
            f"--summary {shlex.quote(_draft_title(draft, b.get('title') or bid))} --description-file {shlex.quote(str(draft))}"
        ]
        if link_type and ticket:
            # not a vulnerability (S6350): printed for the user, never run; every value is shlex-quoted or a validated Jira key
            cmds.append(
                f"acli jira workitem link create --out <JIRA-KEY> --in {ticket} --type {shlex.quote(link_type)} --yes"
            )  # NOSONAR
        cmds.append(f"qa-mem bug-status {_stored_hash(b)} FILED <JIRA-KEY>")
    attach = [
        p
        for p in (
            run_abs / "evidence" / f"{bid}.png",
            run_abs / "evidence" / f"{bid}.webm",
            run_abs / "evidence" / f"{bid}-network.txt",
            run_abs / "repro" / f"{bid}.spec.ts",
        )
        if p.is_file()
    ]
    if attach:
        cmds.append("# attach (look at images and videos first): " + " ".join(shlex.quote(str(p)) for p in attach))
    return [f"- {say}:\n\n" + _indent(_sh(cmds), 2)]


def _bug_entry(b, owners, here, cases, run_dir, cfg, ctx, level):
    """A bug's heading and its list: page, the cases that found it, its files and review, possible duplicates and the
    commands to file it. `here`: the plan tickets this part of the report is about (an owner outside them is named)."""
    run_dir, bid = Path(run_dir), b.get("id") or ""
    cls = " ".join(str(b.get("classification") or "").split()).upper()
    head = f"{'#' * level} {_bug_name(b, run_dir, linked=False)} ({one_line(_sev(b) + (', ' + cls if cls else ''))})"
    found = [c for c in cases if _bug_refs(b) & _case_bugs(c)]  # the case that failed on it, and its AC
    where = [
        f"Page {one_line(b['route'])}" if b.get("route") else "",
        "found by " + "; ".join(one_line(c["id"]) + (f" ({one_line(c['ac'])})" if c.get("ac") else "") for c in found)
        if found
        else "",
        f"ticket {one_line(', '.join(owners))}" if owners and set(owners) - set(here) else "",
        "no ticket's verdict counts it" if here and not owners else "",
    ]
    png, ev = run_dir / "evidence" / f"{bid}.png", Path(str(b.get("evidence") or ""))
    # (is_file: evidence '.', '..' or '/' names a folder, and a link to it is broken)
    img = (
        f"evidence/{quote(png.name)}"
        if bid and png.is_file()
        else f"evidence/{quote(ev.name)}"
        if b.get("evidence") and (run_dir / "evidence" / ev.name).is_file()
        else ""
    )
    files = [
        f"[draft](bugs/{bid}.md)" if bid and (run_dir / "bugs" / f"{bid}.md").is_file() else "",
        f"[image]({img})" + (" (not highlighted)" if b.get("annotated") is False else "") if img else "",
        f"[video](evidence/{bid}.webm)" if bid and (run_dir / "evidence" / f"{bid}.webm").is_file() else "",
        f"[repro](repro/{bid}.spec.ts)" if bid and (run_dir / "repro" / f"{bid}.spec.ts").is_file() else "",
        f"[network](evidence/{bid}-network.txt)"
        if bid and (run_dir / "evidence" / f"{bid}-network.txt").is_file()
        else "",
        cell(_repro_badge(bid, b, ctx["audit"], ctx["reviews"])),
    ]
    lines = [f"- {x}" for x in (" · ".join(x for x in where if x), " · ".join(x for x in files if x)) if x]
    if _jira_of(b, ctx["states"])[1] is None:
        lines += [
            f"- possible duplicate: {one_line(x['key'])} {one_line(x.get('summary'))} (check it before filing)"
            for x in ctx["dups"].get(bid, [])
            if isinstance(x, dict) and x.get("key")
        ]
    lines += _filing(b, owners, run_dir.resolve(), cfg, ctx["states"])
    return [head, _items(lines)] if lines else [head]


def _evidence_gaps(cases, run_dir):
    """Ids of the cases checked to PASS or FAIL without a non-empty evidence/<id>.png."""
    out = []
    for c in cases:
        p = Path(run_dir) / "evidence" / f"{c['id']}.png"
        if (
            c["status"] in ("PASS", "FAIL")
            and re.fullmatch(r"[A-Za-z]+-\d+", str(c["id"]))
            and (not p.is_file() or p.stat().st_size == 0)
        ):
            out.append(c["id"])
    return out


def _post_checks(t, i, run_dir, retest_of):
    """What to check before posting a ticket's Jira comment (`i`: the ticket's verdict, cases, builds and signals as
    render_report_parts gathers them): an unknown build, passes not re-run here, cases without their image; for a pass,
    the image to add per page and the unexplained browser signals on its pages (a pass over a 5xx may hide a bug; the
    verdict never changes)."""
    v, mine, prior, builds, signals = i["v"], i["mine"], i["prior"], i["builds"], i["signals"]
    out = []
    if not builds or any(core.norm_build(b) == core.UNKNOWN for b in builds):
        fix = t.get("fix_version")
        fix = " ".join(str(fix).split()) if isinstance(fix, (str, int, float)) and not isinstance(fix, bool) else ""
        fix = fix + ".0" if re.fullmatch(r"\d+\.\d+", fix) else fix
        out.append(
            f"The build is unknown in this run: confirm the QA app runs {one_line(fix)}, and name the build in the comment."
            if fix
            else "The build is unknown in this run: confirm which build the QA app runs, and name it in the comment."
        )
    if prior:
        pb = ", ".join(sorted({str(c.get("build") or core.UNKNOWN) for c in prior}))
        out.append(
            _line([f"{', '.join(str(c['id']) for c in prior)} passed on build {pb}"])
            + f" and {'was' if len(prior) == 1 else 'were'} not re-run here"
            + (f" (retest of {one_line(retest_of)})" if retest_of else "")
            + ": the comment says so."
        )
    out += [f"{one_line(cid)} has no evidence image: take it or say why." for cid in _evidence_gaps(mine, run_dir)]
    if v not in ("PASS", "PASS WITH NOTES"):
        return out
    route_of = lambda c: " ".join(str(c.get("route") or "").split())
    shots, seen = [], set()  # one passing image per page: one page and two passes proved neither
    for c in mine:  # the case links its image: named here, not linked again
        if route_of(c) not in seen and c["status"] == "PASS" and _evidence_file(run_dir, c["id"]):
            seen.add(route_of(c))
            shots.append((route_of(c), f"{one_line(c['id'])}'s image"))
    if not mine and retest_of:  # every case carried from the retested folder: its images are the ones to use
        for c in prior:
            rel = _evidence_file(Path(run_dir).parent / retest_of, c.get("id"))
            if rel and route_of(c) not in seen:
                seen.add(route_of(c))
                shots.append(
                    (route_of(c), _link(f"../{retest_of}/evidence/{c['id']}.png", quote(f"../{retest_of}/") + rel))
                )
    out += [f"Add {what} ({one_line(r or 'test page')}) to the comment in Jira." for r, what in shots]
    if not shots:
        out.append("No screenshot for this ticket yet: add one before you post.")
    severe = [k for _, k in signals if signal_severity(k) == "severe"]
    if severe:
        out.append(
            f"{_plural(len(severe), 'severe browser signal')} on its pages no bug explains (a 5xx, a failed request, an "
            "error body or a leak): a pass over them may hide a bug. Read Signals no bug explains first."
        )
    if len(signals) > len(severe):
        out.append(
            f"{_plural(len(signals) - len(severe), 'other unexplained browser signal')} (4xx, console errors, request "
            "loops): see Signals no bug explains."
        )
    return out


def _post_item(key, name, run_abs, checks, multi):
    head = f"Post the Jira comment{f' for {key}' if multi else ''}: [{name}]({name})" + (
        ", after these checks:" if checks else ""
    )
    cmd = f"acli jira workitem comment create --key {key} --body-file {shlex.quote(str(run_abs / name))}"
    return "\n\n".join([head, *(["\n".join(f"- {c}" for c in checks)] if checks else []), _sh([cmd])])


def _notes_blocks(plan, runs):
    blocks = ["## Exploration notes"]
    if env := _env_line(plan, runs):
        blocks.append(f"App: {env}")
    blocks.append("Heuristics: ✅ checked, no problem · ❌ problem found · ⛔ could not be checked")
    for name, r in runs:
        route = r.get("target") or next((p.get("route") for p in _list(r, "pages")), "") or name
        cov = {}
        for p in _list(r, "pages"):
            for c in _list(p, "coverage"):
                if core.norm(c.get("heuristic")):
                    h, res = core.heuristic_id(c["heuristic"]), core.coverage_result(c.get("result"))
                    order = ("fail", "blocked", "pass")
                    if h not in cov or (
                        res in order and (cov[h] not in order or order.index(res) < order.index(cov[h]))
                    ):
                        cov[h] = res
        icons = ", ".join(f"{h} {COVERAGE_ICON.get(v, v)}" for h, v in cov.items())
        title = f"{route} ({name}: {r.get('status') or '?'}, build {r.get('build') or '?'})" + (
            f" — {icons}" if icons else ""
        )
        inner = [f"Stopped: {cell(r['stop_reason'])}"] if r.get("stop_reason") else []
        if r.get("notes") or r.get("summary"):
            inner.append(paragraph(core.short(r.get("notes") or r.get("summary"), 1500)))
        blocks.append(_fold(title, inner))
    leads = _leads(runs)
    if leads:
        lines = [
            f"- {cell(x['priority'])} {cell(x['route'])}"
            + (f" ({cell(x['kind'])})" if x.get("kind") else "")
            + ": "
            + cell(": ".join(str(x[f]) for f in ("target", "reason") if x.get(f)))
            for x in leads[:10]
        ]
        blocks.append(_fold(f"Suggested follow-up leads ({len(leads)})", ["\n".join(lines)]))
    return blocks


def _history_table(run_dir, key):
    """The table of .data/history.jsonl's lines for one ticket, newest first; '' without one."""
    rows = []
    for line in reversed(_text(Path(run_dir) / ".data" / "history.jsonl").splitlines()):
        try:
            h = json.loads(line)
        except ValueError:
            continue
        if isinstance(h, dict) and h.get("ticket") == key:
            n = h["cases"] if isinstance(h.get("cases"), dict) else {}
            rows.append(
                row(
                    h.get("date"),
                    h.get("build"),
                    VERDICT_ICON.get(str(h.get("verdict")), h.get("verdict")),
                    f"{n.get('pass', 0)} passed, {n.get('fail', 0)} failed, {n.get('other', 0)} other",
                    h.get("bugs"),
                )
            )
    if not rows:
        return ""
    return "\n".join([row("Date", "Build", "Verdict", "Cases", "Bugs"), "|---|---|---|---|---|", *rows])


def _q_text(q, show_from=True):
    labels = ", ".join(str(w) for w in q["where"])
    return cell(q["text"]) + (f" _(from {cell(labels)})_" if labels and show_from else "")


def _safe_key(key):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", str(key or "")))


# ---------- the Jira comment: one plain-text file per ticket with a verdict, never posted (Jira writes are the user's)
VERDICT_WORD = {"PASS": "PASSED", "PASS WITH NOTES": "PASSED WITH NOTES", "FAIL": "FAILED", "BLOCKED": "BLOCKED"}
AC_WORD = {"PASS": "passed", "FAIL": "FAILED", "BLOCKED": "blocked", "QUESTION": "open question", "NOT_RUN": "not run"}


def _release_names(templates, fix):
    """(names, gap): the config's releaseTemplates with {version} filled from the fix version (3.5 -> 3.5.0); no names
    and what is missing when either is."""
    temps = (
        [" ".join(t.split()) for t in templates if isinstance(t, str) and t.strip()]
        if isinstance(templates, list)
        else []
    )
    if not temps:
        return [], "set releaseTemplates in the qa-sdet config"
    if not fix:
        return [], "the ticket has no single fix version (plan.json fix_version)"
    v = fix + ".0" if re.fullmatch(r"\d+\.\d+", fix) else fix
    return [t.replace("{version}", v) for t in temps], ""


def _on_build(builds):
    known = [b for b in builds if core.norm_build(b) != core.UNKNOWN]
    if not known:
        return "an unknown build"
    return "build " + ", ".join(known) + (" and an unknown build" if len(known) < len(builds) else "")


def _jira_comment(t, v, mine, prior, builds, date, cases, bugs, keys, plan, cfg, states):
    """A ticket's Jira comment, plain text: the result, build and date; the release and the fix when known; each AC with
    what its cases saw; the ticket's bugs by title, filed or not; its open blocking questions. No case or draft ids, no
    paths, no placeholders, nothing cut."""
    flat = lambda x: " ".join(core._s(x).split())
    head = [f"QA result: {VERDICT_WORD[v]} on {_on_build(builds)}" + (f" ({date})" if date else "")]
    fix = t.get("fix_version")
    fix = (
        flat(fix) if isinstance(fix, (str, int, float)) and not isinstance(fix, bool) else ""
    )  # JSON true is no version
    names, _ = _release_names(cfg.get("releaseTemplates"), fix)
    head += ["Release: " + ", ".join(names)] if names else []
    if flat(t.get("fixed_note")) and v in ("PASS", "PASS WITH NOTES"):
        head.append(f"Fixed: {flat(t.get('fixed_note'))}")
    groups, placed, every = ["\n".join(head)], {}, mine + prior
    now_build = ", ".join(b for b in builds if core.norm_build(b) != core.UNKNOWN)
    title = lambda c: flat(c.get("title")) or "untitled check"

    def word(c):
        if c.get("prior"):
            return f"passed on build {flat(c.get('build')) or core.UNKNOWN}, " + (
                f"not re-run on {now_build}" if now_build else "not re-run here"
            )
        return AC_WORD[c["status"]]

    said = lambda c: flat(c.get("actual")) or flat(c.get("notes"))

    def saw(c, several, mixed):
        """What one case saw, under its AC, in full. The AC's entry gives the result of a lone case, and of several that
        all ended alike; when they differ each line says its own, as Other checks do (found live: a reader could not
        tell which of an AC's cases had failed)."""
        if not several:
            return "  " + (said(c) or title(c))
        if mixed:
            return f"  {title(c)}: {word(c)}. {said(c)}".rstrip()
        return f"  {title(c)}" + (f": {said(c)}" if said(c) else "")

    for ac in _list(t, "acs"):
        aid = _ac_id(ac)
        covering = [c for c in every if aid and _ac_key(aid) in _case_acs(c)]
        now = [c for c in covering if not c.get("prior")]
        state = "not covered by a case" if not covering else AC_WORD[_worst(now)] if now else word(covering[0])
        entry = [" ".join(x for x in (flat(aid), flat(ac.get("text"))) if x) + f": {state}."]
        for c in covering:
            if c["id"] in placed:
                entry.append(f"  {title(c)}: see {placed[c['id']]}.")
            else:
                placed[c["id"]] = flat(aid)
                entry.append(saw(c, len(covering) > 1, len({word(x) for x in covering}) > 1))
        groups.append("\n".join(entry))
    rest = [c for c in every if c["id"] not in placed]
    if rest:
        groups.append("\n".join(["Other checks:"] + [f"  {title(c)}: {word(c)}. {said(c)}".rstrip() for c in rest]))
    found, filed = [], set()
    for b in ticket_bugs(t["key"], cases, bugs, keys):
        known = _jira_of(b, states)[1]
        filed |= {known} if known else set()
        # a bug with no draft here goes by its hash in REPORT.md and in what a case says: the line says which it is
        ref = "" if known or b.get("id") or not b.get("hash") else f"{_bug_label(b)}, "
        found.append(f"Bug: {flat(b.get('title')) or 'untitled'} ({_sev(b)}), {ref}{known or 'not filed yet'}")
    named = {k for c in mine for k in ops.KEY_RE.findall(str(c.get("bug") or "").upper())}
    found += [
        f"Bug: {k}" for k in sorted(named - filed - {t["key"].upper()}) if not re.fullmatch(r"BUG-\d+", k)
    ]  # a case's BUG-n is this folder's draft, listed above: only a Jira key is one more
    groups += ["\n".join(found)] if found else []
    qs = [
        re.sub(r"^\s*(Q\d+\s*)?(\[[A-Z]+\]\s*)*", "", q["text"])
        for q in _q_items(plan, "test plan")
        if not q["resolved"]
        and "[BLOCKING]" in q["text"].upper()
        and str(q.get("ticket") or t["key"]).strip().upper() == t["key"].upper()
    ]
    groups += ["\n".join(f"Open question: {q}" for q in qs)] if qs else []
    return "\n\n".join(groups) + "\n"


def render_report_parts(run_dir: str | Path, cfg: JsonObj | None = None) -> tuple[str, dict[str, str]]:
    """(REPORT.md text, {Jira comment file name: text}). One renderer for every kind: a ticket's report (status, what
    to do, results by AC, bugs, not tested, signals, exploration notes, history), an exploration's (the same without
    ACs), a plan's (questions, then cases by AC) and an older folder's several tickets one after another. Each ticket
    with a verdict gets one plain-text Jira comment: jira-comment.txt, or jira-comment-<KEY>.txt beside other tickets."""
    run_dir = Path(run_dir)
    run_abs = run_dir.resolve()
    d = load_run(run_dir)
    plan, runs = d["plan"], d["runs"]
    kind = str(plan.get("kind") or (runs[0][1].get("mode") if runs else "") or "explore")
    tickets = _plan_tickets(plan)
    keys = [t["key"] for t in tickets]
    cases, bugs = merged_cases(plan, runs, d["warnings"]), run_bugs(runs, d["warnings"])
    prior_all = _prior_passed(plan, keys, cases)
    for t in tickets:  # a case naming an AC its ticket doesn't list covers nothing: say so
        have = {_ac_key(_ac_id(a)) for a in _list(t, "acs") if _ac_id(a)}
        for c in (c for c in cases if have and c.get("ticket") == t["key"]):
            d["warnings"] += [
                f"{c['id']} names {a}, which {t['key']} doesn't have: fix the case's `ac` in plan.json"
                for a in sorted(_case_acs(c) - have)
            ]
    retest_of = " ".join(plan["retest_of"].split()) if isinstance(plan.get("retest_of"), str) else ""
    with_status = kind != "plan"
    cfg = core.config() if cfg is None else cfg
    vmap = _verdicts(plan, runs, cases, bugs, d["unread"]) if tickets and with_status else {}
    cases = [c for c in cases if not c.get("prior")]  # from here on in prior_all, as a plan's prior_passed
    prior_all = [{**c, "id": str(c.get("id") or "?"), "status": "PASS", "prior": True} for c in prior_all]
    hosts, baseline = core.allowed_hosts(cfg), _signal_baseline()
    triaged = [(r.get("target") or name, _signals(run_dir, name, r, hosts, baseline)) for name, r in runs]
    dups = _load_json(run_dir / ".data" / "jira-dups.json", [])
    ctx = {
        "states": _db_bug_states() if bugs or tickets else {},
        "dups": dups if isinstance(dups, dict) else {},
        "audit": audit_bugs(run_dir) if bugs else {},
        "reviews": _reviews(run_dir, d["warnings"]) if bugs else {},
    }
    owners = _bug_owners(bugs, cases, keys)
    single, multi, date = len(tickets) == 1, len(tickets) > 1, _run_date(run_dir, plan)
    drafts: dict[str, str] = {}
    info: dict[str, dict[str, Any]] = {}  # per ticket: its cases, builds, verdict, signals and comment file
    for t in tickets:
        key = t["key"]
        mine = [c for c in cases if c.get("ticket") == key]
        prior = [c for c in prior_all if c.get("ticket") == key]
        ids = {c["id"] for c in mine}  # the builds of the runs that tested this ticket, not the whole folder's
        tested = [k for k, (_, r) in enumerate(runs) if any(str(x.get("id")) in ids for x in _list(r, "cases"))]
        builds = (
            sorted({core.norm_build(runs[k][1].get("build")) for k in tested})
            or sorted({core.norm_build(r.get("build")) for _, r in runs})
            or sorted({core.norm_build(c.get("build")) for c in prior})
        )
        v, why = vmap.get(key) or ("NOT STARTED", "testing hasn't started yet")
        seen = [(triaged[k][0], s) for k in tested for s, _ in triaged[k][1][0]]  # its pages' unexplained signals
        name = "jira-comment.txt" if single else f"jira-comment-{key}.txt"
        if v != "NOT STARTED" and _safe_key(key):
            drafts[name] = _jira_comment(t, v, mine, prior, builds, date, cases, bugs, keys, plan, cfg, ctx["states"])
        info[key] = {
            "mine": mine,
            "prior": prior,
            "builds": builds,
            "v": v,
            "why": why,
            "signals": seen,
            "name": name,
        }

    def status(t, folder):  # folder: the report's own status line (one ticket), with what holds for the whole folder
        i = info[t["key"]]
        passed = sum(c["status"] == "PASS" for c in i["mine"]) + len(i["prior"])
        total = len(i["mine"]) + len(i["prior"])
        n = len(ticket_bugs(t["key"], cases, bugs, keys))
        return _line(
            [
                VERDICT_ICON[i["v"]],
                "build " + (", ".join(i["builds"]) if runs else "not run yet"),
                date if folder else "",
                f"{passed} of {_plural(total, 'case')} passed"
                + (f" ({len(i['prior'])} on an earlier build)" if i["prior"] else ""),
                _plural(n, "bug") if n else "no bugs",
                f"retest of {retest_of}" if retest_of and folder else "",
                _ran_as(runs) if folder else "",
            ]
        )

    heading = lambda t: one_line(t["key"]) + (f": {one_line(t['summary'])}" if t.get("summary") else "")
    blocks = [f"# {heading(tickets[0])}" if single else f"# QA report: {one_line(plan.get('title') or run_dir.name)}"]
    if not plan and not runs:
        blocks.append("_Nothing recorded yet: no plan and no test run in this folder._")
    q_items = _q_items(plan, "test plan") + [q for name, r in runs for q in _q_items(r, str(r.get("target") or name))]
    merged = _merge_questions(q_items)
    open_qs = sorted([q for q in merged if not q["resolved"]], key=_q_rank)
    answered = [q for q in merged if q["resolved"]]
    answers = "\n".join(
        f"- {cell(q['text'])}" + (f" — answer: {cell(q['answer'])}" if q["answer"] else "") for q in answered
    )
    resolved_fold = [_fold(f"Resolved questions ({len(answered)})", [answers])] if answered else []
    if kind == "plan":
        blocks.append(_line(["plan", date, "not run yet", _plural(len(cases), "case")]))
        if multi:
            blocks.append(
                _items(
                    [
                        f"- {heading(t)} — {len(_list(t, 'acs'))} AC, {_plural(len(info[t['key']]['mine']), 'case')}"
                        for t in tickets
                    ]
                )
            )
        if open_qs:
            blocks += ["## Questions", _items([f"- {_q_text(q, show_from=False)}" for q in open_qs])]
        blocks += resolved_fold
    else:
        if single:
            blocks += [status(tickets[0], True), _line([info[keys[0]]["why"]])]
        elif multi:
            blocks.append(
                _line(
                    [
                        kind,
                        date,
                        "build " + (", ".join(_run_builds(runs)) or "not run yet"),
                        f"retest of {retest_of}" if retest_of else "",
                        _ran_as(runs),
                    ]
                )
            )
            blocks.append(
                _items([f"- {one_line(k)}: {VERDICT_ICON[info[k]['v']]} — {one_line(info[k]['why'])}" for k in keys])
            )
        elif plan or runs:
            n = len(bugs)
            blocks.append(
                _line(
                    [
                        kind,
                        date,
                        "build " + (", ".join(_run_builds(runs)) or "not run yet"),
                        "; ".join(f"{r.get('target') or name} {r.get('status') or '?'}" for name, r in runs),
                        _plural(n, "bug") if n else "no bugs",
                        _ran_as(runs),
                    ]
                )
            )
        if plan or runs:
            todo = [
                _bug_todo(b, run_dir, ctx["states"])
                for b in sorted(bugs, key=lambda b: _first_word(b.get("classification")) != "confirmed")
            ]
            todo += [f"Answer: {_q_text(q)}" for q in open_qs]
            todo += [
                f"Answer the question {one_line(c['id'])} raised: see Results."
                for c in cases
                if c["status"] == "QUESTION" and not _asked(c, open_qs)
            ]
            todo += [
                f"Delete the test data left in the app on {one_line(r.get('target') or name)}: {cell(m.group(1).strip())}"
                for name, r in runs
                for m in re.finditer(r"Left in the app:[ \t]*([^\n]+)", core._s(r.get("notes")))
                if not re.fullmatch(r"(none|nothing|n/a)\s*([.;,(].*)?|-", m.group(1).strip(), re.I)
            ]  # step 9 couldn't delete them: they stay in the shared QA app
            severe = [k for _, (sig, _) in triaged for k, _ in sig if signal_severity(k) == "severe"]
            if severe:
                todo.append(
                    f"Look into the {_plural(len(severe), 'severe browser signal')} no bug explains (a 5xx, a failed "
                    "request, an error body or a leak): see Signals no bug explains."
                )
            todo += [
                _post_item(
                    t["key"],
                    info[t["key"]]["name"],
                    run_abs,
                    _post_checks(t, info[t["key"]], run_dir, retest_of),
                    multi,
                )
                for t in tickets
                if info[t["key"]]["name"] in drafts
            ]
            blocks += ["## What to do", _numbered(todo) if todo else "Nothing needs your action.", *resolved_fold]

    level = 3 if multi else 2
    for t in tickets:
        i = info[t["key"]]
        if multi:
            blocks.append(f"## {heading(t)}")
            blocks += [status(t, False)] if with_status else []
        res = _results(t, i["mine"] + i["prior"], run_dir, with_status, level + 1)
        if res:
            blocks += [f"{'#' * level} {'Results' if with_status else 'Cases'}", *res]
        mine_bugs = [(b, o) for b, o in zip(bugs, owners) if single or (o and o[0] == t["key"])]
        elsewhere = [  # a bug of several tickets is listed once, under the first
            f"- {_bug_name(b, run_dir)}: listed under {one_line(o[0])}."
            for b, o in zip(bugs, owners)
            if multi and t["key"] in o[1:]
        ]
        if mine_bugs or elsewhere:
            blocks.append(f"{'#' * level} Bugs")
            for b, o in mine_bugs:
                blocks += _bug_entry(b, o, [t["key"]], cases, run_dir, cfg, ctx, level + 1)
            blocks += [_items(elsewhere)] if elsewhere else []
        if multi and (table := _history_table(run_dir, t["key"])):
            blocks += ["### History", table]
    other = [c for c in cases if c.get("ticket") not in keys]
    if other:
        blocks += [
            "## Other test cases" if tickets else "## Cases",
            _items([_case_item(c, run_dir, with_status) for c in other]),
        ]
    if not single:
        rest = [(b, o) for b, o in zip(bugs, owners) if not (o and o[0] in keys)]
        if rest:
            blocks.append("## Other bugs" if tickets else "## Bugs")
            for b, o in rest:
                blocks += _bug_entry(b, o, keys, cases, run_dir, cfg, ctx, 3)

    not_tested = [item for _, r in runs for item in _not_tested(r)]
    if not_tested:
        blocks.append("## Not tested")
        for key, label in NOT_TESTED_CATEGORIES:
            group = [x for k, x in not_tested if k == key]
            if group:
                blocks.append(_fold(f"{label} ({len(group)})", ["\n".join(f"- {cell(x)}" for x in group)]))
    signals = [(route, key, n) for route, (sig, _) in triaged for key, n in sig]
    third = sum(n for _, (_, n) in triaged)
    if signals or third:
        blocks.append("## Signals no bug explains" + (f" ({len(signals)})" if signals else ""))
        lines = [
            f"- {one_line(route)}: {one_line(key)}" + (f" (×{n})" if n > 1 else "")
            for route, key, n in signals[:SIGNALS_SHOWN]
        ]
        if len(signals) > SIGNALS_SHOWN:
            lines.append(
                f"- +{len(signals) - SIGNALS_SHOWN} more: see `requests-*.log` and `console-*.log` in `.data/`"
            )
        blocks += ["\n".join(lines)] if lines else []
        if third:  # a CDN or map-tile failure can still break the page: counted, folded, never mixed with the app's
            blocks.append(
                _fold(
                    f"{third} third-party signal(s) (hosts outside allowedHosts)",
                    ["Failed requests to hosts outside `allowedHosts`: see `requests-*.log` in `.data/`."],
                )
            )
    if runs:
        blocks += _notes_blocks(plan, runs)
    history = [_history_table(run_dir, keys[0])] if single else []
    listed = {b["id"] for b in bugs}.union(*map(_case_bugs, cases))
    # shortcut: a known bug (named by its memory hash) is matched to its earlier draft by title, as the card gives
    # it; read the draft path from memory if explorers start rewording titles
    seen_again = {" ".join(core._s(b.get("title")).split()) for b in bugs if b.get("hash") and not b.get("id")}
    orphans = sorted(
        (
            p
            for p in (run_dir / "bugs").glob("BUG-*.md")
            if re.fullmatch(r"BUG-\d+", p.stem)
            and p.stem not in listed
            and p.is_file()
            and " ".join(_draft_title(p, "").split()) not in seen_again
        ),
        key=lambda p: int(p.stem[4:]),
    )
    if runs and orphans:  # a draft whose run file a later one replaced: still shown, but no verdict counts it
        history += [
            "Earlier bugs this run did not see (no verdict counts them; file one if it still applies):",
            "\n".join(f"- {_bug_link(p.stem, run_dir)} {one_line(_draft_title(p, ''))}" for p in orphans),
        ]
    if any(history):
        blocks += ["## History", *(x for x in history if x)]
    if d["warnings"]:
        blocks += ["## Problems reading this folder", "\n".join(f"- {one_line(w)}" for w in d["warnings"])]
    return "\n\n".join(blocks) + "\n", drafts


def render_report(run_dir: str | Path) -> str:
    return render_report_parts(run_dir)[0]


def run_summary(run_dir: str | Path) -> tuple[str, str, str, int, str]:
    """(date, kind, verdict text, bug count, label) for the qa-out/README.md index: the label is `KEY — summary` for a
    one-ticket plan (a ticket's folder), else the folder's name."""
    d = load_run(run_dir)
    plan, runs = d["plan"], d["runs"]
    kind = str(plan.get("kind") or (runs[0][1].get("mode") if runs else "") or "explore")
    cases, bugs = merged_cases(plan, runs), run_bugs(runs)
    tickets = _plan_tickets(plan)
    if kind == "plan":
        verdicts = f"plan only ({len(cases)} cases)"
    elif tickets:
        vmap = _verdicts(plan, runs, cases, bugs, d["unread"])
        verdicts = ", ".join(f"{t['key']} {VERDICT_ICON[vmap[t['key']][0]]}" for t in tickets)
    else:
        verdicts = ", ".join(str(r.get("status") or "?") for _, r in runs) or "not run yet"
    t = tickets[0] if len(tickets) == 1 else {}
    label = " — ".join(str(x) for x in (t.get("key"), t.get("summary")) if x) or Path(run_dir).name
    return _run_date(run_dir, plan), kind, verdicts, len(bugs), label


def render_index(qa_out):
    """qa-out/README.md: every run folder with a REPORT.md or a plan, newest first."""
    qa_out = Path(qa_out)
    runs = [
        p
        for p in qa_out.iterdir()
        if p.is_dir() and not p.is_symlink() and ((p / "REPORT.md").exists() or (p / ".data" / "plan.json").exists())
    ]
    when = lambda p: max(
        (_mtime(x) for x in (p, p / "REPORT.md", p / ".data" / "plan.json") if x.exists()), default=0.0
    )
    rows = []
    for p in runs:
        n: int | str  # the bug count, or "?" for a folder that can't be read
        try:
            date, kind, verdicts, n, label = run_summary(p)
            ts = when(p)
        except Exception as e:  # one odd folder must not break the index  # noqa: BLE001 - the error is shown, not swallowed
            m = re.match(r"\d{4}-\d{2}-\d{2}", p.name)
            date, kind, verdicts, n, ts = (m.group(0) if m else ""), "?", f"unreadable ({type(e).__name__})", "?", 0.0
            label = p.name
        name = one_line(label).replace("[", "\\[").replace("]", "\\]")
        rows.append(((date, ts), row(date, Raw(f"[{name}]({quote(p.name, safe='+')}/REPORT.md)"), kind, verdicts, n)))
    rows.sort(key=lambda x: x[0], reverse=True)
    return (
        "\n".join(
            [
                "# QA runs",
                "Newest first. Open a run's REPORT.md for test cases, bugs and questions.",
                "",
                row("Date", "Run", "Kind", "Verdict", "Bugs"),
                "|---|---|---|---|---|",
            ]
            + [r for _, r in rows]
        )
        + "\n"
    )


def _scrub_run(run_dir):
    """Scrub the run's text files (drafts, specs, network evidence, signal logs) now: the filing commands link them, and
    the hooks' scrub pass may not have reached them yet (it stops after a few seconds)."""
    for pattern in ("bugs/*.md", "repro/*", "evidence/*.txt", "evidence/*.json", ".data/*.log"):
        for f in Path(run_dir).glob(pattern):
            if f.is_file() and not f.is_symlink():
                try:
                    scrub.scrub_file(f)
                except (OSError, ValueError, RecursionError):
                    pass  # a file it can't read or parse stays as it is; the hook's pass will try again


def _write_history(run_dir):
    """.data/history.jsonl of a ticket run: one line per ticket verdict, date (of the newest run file) and build. A
    report is rendered several times per run (each page's stop, the skill): a line for the same date, build and ticket
    replaces that ticket's last line, another date or build adds one. Best effort: never fails the report."""
    f = run_dir / ".data" / "history.jsonl"
    try:
        d = load_run(run_dir)
        plan, runs = d["plan"], d["runs"]
        if (
            str(plan.get("kind") or "") != "ticket"
            or not (runs or d["unread"])
            or f.parent.is_symlink()
            or f.is_symlink()
        ):
            return  # nothing ran yet, or a link: never written through
        cases, bugs = merged_cases(plan, runs), run_bugs(runs)
        vmap = _verdicts(plan, runs, cases, bugs, d["unread"])
        date = datetime.fromtimestamp(_newest_run_file(run_dir)).strftime("%Y-%m-%d")  # noqa: DTZ006 - local, as _run_date
        build = ", ".join(_run_builds(runs)) or core.UNKNOWN
        lines = f.read_text(encoding="utf-8").splitlines() if f.exists() else []

        def parsed(line):
            try:
                v = json.loads(line)
            except ValueError:
                return {}  # a line it can't read stays as it is
            return v if isinstance(v, dict) else {}

        for t in _plan_tickets(plan):
            mine = [c for c in cases if c.get("ticket") == t["key"] and not c.get("prior")]  # as verdict() counts
            n = {s: sum(c["status"] == s for c in mine) for s in ("PASS", "FAIL")}
            entry = {
                "date": date,
                "build": build,
                "ticket": t["key"],
                "verdict": vmap[t["key"]][0],
                "cases": {"pass": n["PASS"], "fail": n["FAIL"], "other": len(mine) - n["PASS"] - n["FAIL"]},
                "bugs": len(ticket_bugs(t["key"], cases, bugs, list(vmap))),
            }
            last = next((i for i in reversed(range(len(lines))) if parsed(lines[i]).get("ticket") == t["key"]), -1)
            prev = parsed(lines[last]) if last >= 0 else {}
            if (prev.get("date"), prev.get("build")) == (date, build):
                lines[last] = json.dumps(entry, ensure_ascii=False)
            else:
                lines.append(json.dumps(entry, ensure_ascii=False))
        core._write(f, "".join(x + "\n" for x in lines))
    except Exception:  # noqa: BLE001, S110 - the history is a side record: REPORT.md and the index must still be written
        pass


def write_report(run_dir: str | Path) -> Path:
    run_dir = Path(run_dir).resolve()
    if not run_dir.is_dir() or run_dir.parent.name != "qa-out":
        raise core.BadInput(f"report: not a run folder (qa-out/<run>/): {run_dir}")
    from . import ops  # ops imports this module inside its functions too

    live = ops._run_of(run_dir.parent)  # this folder's page whose explorer is still running: it will write again
    live = live[2] if live and live[1].resolve() == run_dir else None
    for p in sorted((run_dir / ".data").glob("prior-*.json")):  # an explorer that crashed never reached the stop hook
        slug = p.name[len("prior-") : -len(".json")]
        try:
            carry_prior(run_dir, slug, keep=slug == live)
        except OSError:
            pass  # the copy stays: the next report or dispatch merges it
    _scrub_run(run_dir)
    # Two reports at once (two sessions, or report + hook): without the lock the later index write could be one
    # rendered before the other run's REPORT.md existed, and that run would vanish from the index; and two renders
    # must not both read the old history file. The history line goes first: REPORT.md's History shows this run too.
    fd = os.open(run_dir.parent, os.O_RDONLY)  # the qa-out folder itself: no lock file left behind
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _write_history(run_dir)
        try:
            text, drafts = render_report_parts(run_dir)
        except Exception as e:  # never fail: say what went wrong in the report itself  # noqa: BLE001 - the error is shown, not swallowed
            text, drafts = (
                (
                    f"# QA report: {one_line(run_dir.name)}\n\n_Could not render this report: {type(e).__name__}: "
                    f"{one_line(core.short(e, 200))}. Check the files in .data/. Jira comment drafts from an earlier "
                    "render were removed: no verdict stands behind them._\n"
                ),
                {},
            )
        core._write(run_dir / "REPORT.md", text)
        # Jira comments: written with the report; one no longer due is removed. A failed render removes them all: an
        # earlier pass would stay postable after the run turned FAIL. A .md twin is from before the plain-text comment.
        for name, body in drafts.items():
            core._write(run_dir / name, body)
        for old in [*run_dir.glob("jira-comment*.md"), *run_dir.glob("jira-comment*.txt")]:
            if old.name not in drafts and (old.is_symlink() or old.is_file()):
                old.unlink(missing_ok=True)
        core._write(run_dir.parent / "README.md", render_index(run_dir.parent))
    finally:
        os.close(fd)
    return run_dir / "REPORT.md"


# ---------- plan-find: deterministic plan reuse, --resume and --retest state ----------
def _plan_folders(qa_out):
    """Run folders with a plan.json, the most recently changed first (plan or a run file; not the folder, which a
    re-rendered REPORT.md or an editor's swap file touches): plan-find's latest run and the inbox's Last QA come from
    the same order."""
    dirs = [p for p in qa_out.iterdir() if p.is_dir() and not p.is_symlink() and (p / ".data" / "plan.json").is_file()]
    when = lambda p: max(
        (_mtime(x) for x in [p / ".data" / "plan.json", *(p / ".data").glob("run-*.json")] if x.exists()),
        default=0.0,
    )
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
    builds = sorted(
        {core.norm_build(r.get("build")) for _, r in runs if core.norm_build(r.get("build")) != core.UNKNOWN}
    )
    case_build = {}
    for _, r in runs:
        b = core.norm_build(r.get("build"))
        for x in _list(r, "cases"):
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
            run_status_by_route[k], run_build_by_route[k] = _first_word(r.get("status")), r.get("build")
    now, of = _now_build(runs), lambda by, k: by.get(k, by.get((k[0], "")))
    incomplete = []
    for k in routes_order:
        rcases = [c for c in cases if core.route_key(c.get("route")) == k]
        if (
            of(run_status_by_route, k) in ("blocked", "incomplete")
            or _older_build(of(run_build_by_route, k), now)  # a new build's re-run stopped before this page
            or any(c["status"] in ("BLOCKED", "NOT_RUN") for c in rcases)
        ):
            incomplete.append(k)
    spell = lambda k: k[0] + (f"?{k[1]}" if k[1] else "")
    completed = [spell(k) for k in routes_order if k not in incomplete]
    incomplete = [spell(k) for k in incomplete]
    # verdict()'s rules per case: a PASS that a run ended BLOCKED reported (or ran on its page), or on the page of (or
    # naming) a CONFIRMED CRITICAL/HIGH bug of its ticket that no retested case names, is retested: a partial retest must
    # not make it a prior pass and drop the reason the ticket didn't pass
    bugs = run_bugs(runs)
    rechecked = set().union(*(_case_bugs(c) for c in cases if c["status"] != "PASS"))
    tkey = lambda c: c.get("ticket") if isinstance(c.get("ticket"), str) else None  # a list ticket is no plan ticket's
    serious = {
        t: [b for b in ticket_bugs(t, cases, bugs, tickets) if _serious(b) and not _bug_refs(b) & rechecked]
        for t in {tkey(c) for c in cases}
    }

    def trusted(c):
        rt = core.norm_route(c.get("route"))
        return (
            c["status"] == "PASS"
            and not d["unread"]
            and not _blocked_runs([c], runs)
            and not any(
                (rt and rt in (b.get("route"), b.get("target"))) or _bug_refs(b) & _case_bugs(c)
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
        for c in _prior_passed(plan, tickets)
        if isinstance(c.get("id"), (str, int))
        and not isinstance(c["id"], bool)
        and str(c["id"])
        and str(c["id"]) not in ids
    ]
    zero_exec = (
        bool(runs)
        and all(c["status"] in ("BLOCKED", "NOT_RUN") for c in cases)
        and all(_first_word(r.get("status")) == "blocked" for _, r in runs)
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
    from . import memory

    said = str(run.get("block_scope") or "").strip().lower()
    if said in memory.BLOCK_SCOPES:
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
    prev = _load_json(state_file, [])
    prev = prev if isinstance(prev, dict) else {}
    mtime = _mtime(f)
    again = (prev.get("slug"), prev.get("mtime")) == (slug, mtime)  # asked again about the same result
    streak = prev.get("before" if again else "streak")
    streak = streak if isinstance(streak, int) and not isinstance(streak, bool) else 0
    at = prev.get("at") if isinstance(prev.get("at"), (int, float)) else None
    others = at is not None and any(_mtime(b) > at for b in data.glob("brief-*.md") if b.name != f"brief-{slug}.md")
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

    run = _load_json(f, [])
    if not isinstance(run, dict):
        return answer({"action": "abort", "block_scope": "browser", "reason": "explorer ended without a run file"}, 0)
    if _first_word(run.get("status")) != "blocked":
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
HYPOTHESIS_CAP = 400
SAFE_ROUTE_RE = re.compile(r"[/#][\w#!/.:~%@+,=-]*")  # a path the coordinator can single-quote into a command


def _safe_text(v, cap=HYPOTHESIS_CAP):
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
    from . import codemap, memory

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
        for b in _list(r, "bugs"):
            if core.norm(b.get("classification")) != "confirmed" or not b.get("route") or con is None:
                continue
            route, weight = (
                core.norm_route(b.get("route")),
                memory.SEVERITY_WEIGHT.get((core.norm(b.get("severity")).split() or [""])[0], 1),
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
                for page in memory._near_pages(con, route):
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
        chain = memory.entry_chain(con, x["route"]) if con is not None else []
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
        for b in _list(r, "bugs"):
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
    baseline = _signal_baseline()
    new5xx = [
        key[len("[NEW] ") :]
        for name, r in runs
        for key, _ in _signals(run_dir, name, r, baseline=baseline)[0]
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
            if (_load_json(p / ".data" / "plan.json", []) or {}).get("kind") == "explore"
        ][:1]
    if not folders or not (folders[0] / ".data" / "plan.json").is_file():
        raise core.BadInput("explore-next: no explore run folder (qa-out/<run>/ with an explore plan.json)")
    d = load_run(folders[0])
    title = str((d["plan"] or {}).get("title") or "")
    planned = [core.norm_route(x) for x in title.split(", ") if x.strip().startswith(("/", "http", "#"))]
    done = {core.norm_route(r.get("target")): r for _, r in d["runs"] if core.norm_route(r.get("target"))}
    unfinished = lambda r: (
        _first_word(r.get("status")) == "incomplete"
        or (_first_word(r.get("status")) == "blocked" and block_scope(r) != "page")
    )
    incomplete = [x for x in dict.fromkeys(planned) if x and x not in done] + [
        x for x, r in done.items() if unfinished(r)
    ]
    explored = set(planned) | set(done)
    leads = _leads(d["runs"], skip=explored)
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
        f = fd if isinstance(fd := it.get("fields"), dict) else {}
        get = lambda k: f.get(k) if f.get(k) is not None else it.get(k)  # noqa: B023 - called in this loop pass
        out.append(
            {
                "key": str(it["key"]).upper(),
                "summary": core.short(" ".join(str(get("summary") or "").split()), 100),
                "status": _name(get("status")),
                "priority": _name(get("priority")),
                "updated": str(get("updated") or ""),
            }
        )
    if data and not out:  # another JSON shape must not read as "no open tickets"
        raise core.BadInput(
            f'inbox: none of the {len(data)} items has a Jira key ("key": "PROJ-1234"); ask the search for '
            "the key field"
        )
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
        vmap = {} if kind == "plan" else _verdicts(plan, runs, merged_cases(plan, runs), run_bugs(runs), d["unread"])
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
    counts = f"{today or datetime.now().strftime('%Y-%m-%d')} · {len(items)} open, {len(new)} new" + (  # noqa: DTZ005 - the user's local date (stored times use core.now(), UTC)
        f", {len(changed)} changed" if changed else ""
    )
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
    nums = [k[len(p) + 1 :] if p and k.startswith(p + "-") else k for k in pick]
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


ADF_JOIN = {
    "doc": "\n",
    "bulletList": "\n",
    "orderedList": "\n",
    "blockquote": "\n",
    "panel": "\n",
    "table": "\n",
    "expand": "\n",
    "tableRow": " | ",
    "tableCell": " ",
    "tableHeader": " ",
    "listItem": " ",
}


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
            text = ADF_JOIN.get(str(node.get("type")), "").join(
                t for t in (_adf_text(c, depth + 1) for c in node["content"]) if t
            )
            return f"- {text}" if node.get("type") == "listItem" and text else text
        return _adf_attr_text(node)
    if isinstance(node, list):
        return "\n".join(t for t in (_adf_text(c, depth + 1) for c in node) if t)
    return ""


def _trim_comments(raw):
    items = (
        raw
        if isinstance(raw, list)
        else (
            next((raw[k] for k in ("comments", "values", "items") if isinstance(raw.get(k), list)), [])
            if isinstance(raw, dict)
            else []
        )
    )
    trimmed = []
    for c in items[-20:]:
        if isinstance(c, dict):
            author = c.get("author")
            who = (
                author.get("displayName") or author.get("name") or "" if isinstance(author, dict) else str(author or "")
            )
            body = c.get("body")
            if isinstance(body, dict) and body.get("type") == "doc":
                body = _adf_text(body)
            if isinstance(body, str) and len(body) > 1500:
                body = body[:1500] + "…"
            trimmed.append({"author": who, "created": str(c.get("created") or ""), "body": body})
    return trimmed


def _ticket_fingerprint(fields, ac_field=""):
    """sha1 of what a ticket asks for: summary, description, the AC field and fix versions (rich text flattened)."""
    import hashlib

    text = lambda v: _adf_text(v) if isinstance(v, dict) else v if isinstance(v, str) else ""
    fixes = sorted(
        str(x.get("name") if isinstance(x, dict) else x)
        for x in fields.get("fixVersions") or []
        if isinstance(fields.get("fixVersions"), list)
    )
    parts = [
        text(fields.get("summary")),
        text(fields.get("description")),
        text(fields.get(ac_field)) if ac_field else "",
    ]
    return hashlib.sha1("\n".join(core.norm(x) for x in parts + fixes).encode(), usedforsecurity=False).hexdigest()


def _with_drift(state, tickets):
    """plan_state plus `drift` {KEY: {fields_changed, prior_captured_at}} for tickets whose Jira text changed since the
    plan's source_snapshot. A drifted ticket's carried passes move to retest_cases: they were checked against the old
    requirements."""
    plan = next(
        (
            _load_json(Path(x["plan_file"]), [])
            for x in (state.get("latest_run"), state.get("reusable_plan"))
            if x and x.get("plan_file")
        ),
        None,
    )
    before = {
        str(t.get("key")).upper(): t.get("source_snapshot")
        for t in _list(plan, "tickets")
        if isinstance(t.get("source_snapshot"), dict)
    }
    drift = {}
    for t in tickets:
        snap, old = t.get("source_snapshot"), before.get(str(t.get("key")).upper())
        if snap and old and old.get("fields_sha1") and old.get("fields_sha1") != snap["fields_sha1"]:
            drift[t["key"]] = {"fields_changed": True, "prior_captured_at": old.get("captured_at")}
    lr = state.get("latest_run")
    if drift and lr:
        moved = [c for c in lr["passed_cases"] if str(c.get("ticket")).upper() in {k.upper() for k in drift}]
        whole = lambda c: {"case": x} if (x := _case_in_chain(lr["run_dir"], c["id"])) else {}
        lr = {
            **lr,
            "passed_cases": [c for c in lr["passed_cases"] if c not in moved],
            "retest_cases": lr["retest_cases"] + [{**c, "status": "PASS", **whole(c)} for c in moved],
        }
    return {**state, "latest_run": lr, "drift": drift}


def _case_in_chain(run_dir, cid, hops=10):
    """The whole case object of TC id `cid` (steps, expected): from the plan that last had it in its cases, following
    retest_of back (a pass an earlier partial retest carried is only a summary in prior_passed)."""
    d = Path(run_dir)
    for _ in range(hops):
        plan = _load_json(d / ".data" / "plan.json", [])
        plan = plan if isinstance(plan, dict) else {}
        hit = next((c for c in _list(plan, "cases") if str(c.get("id")) == str(cid)), None)
        prev = str(plan.get("retest_of") or "")
        if hit or not prev or Path(prev).name != prev or prev in (".", ".."):  # a sibling run folder only
            return hit
        d = d.parent / prev
    return None


def trace(project, keys=None, fix_version=None):
    """Each ticket's latest run across qa-out (newest folder that ran it): verdict, date, builds, AC coverage, bugs,
    and what it retested: a release or sprint sign-off view. Read-only."""
    qa_out = ops._project(project) / "qa-out"
    want = {k.strip().upper() for k in keys or []}
    rows, versions = {}, {}  # each ticket's newest run's row; the fix versions any of its runs gave it
    for p in _plan_folders(qa_out) if qa_out.is_dir() else []:
        d = load_run(p)
        plan, runs = d["plan"], d["runs"]
        if not runs or str(plan.get("kind")) == "plan":
            continue
        cases, bugs = merged_cases(plan, runs), run_bugs(runs)
        vmap = _verdicts(plan, runs, cases, bugs, d["unread"])
        prior = _prior_passed(plan, [t["key"] for t in _plan_tickets(plan)], cases)
        builds = ", ".join(sorted({core.norm_build(r.get("build")) for _, r in runs}))
        for t in _plan_tickets(plan):
            k = t["key"].upper()
            if want and k not in want:
                continue
            versions.setdefault(k, set()).add(str(t.get("fix_version") or ""))
            if k in rows:
                continue
            acs = [_ac_key(_ac_id(a)) for a in _list(t, "acs") if _ac_id(a)]
            covered = {
                a for c in cases + prior if c.get("ticket") == t["key"] for a in _case_acs(c)
            }  # as verdict() counts
            v, why = vmap[t["key"]]
            rows[k] = row(
                t["key"],
                VERDICT_ICON[v],
                f"{sum(a in covered for a in acs)}/{len(acs)} AC",
                builds,
                len(ticket_bugs(t["key"], cases, bugs, list(vmap))),
                p.name,
                str(plan.get("retest_of") or ""),
                why,
            )
    shown = [r for k, r in rows.items() if not fix_version or fix_version in versions[k]]
    if not shown:
        return "no ticket runs match"
    return "\n".join(
        [
            row("Ticket", "Verdict", "ACs covered", "Build", "Bugs", "Latest run", "Retest of", "Why"),
            "|---|---|---|---|---|---|---|---|",
        ]
        + shown
    )


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
            item = {
                "key": key,
                "fields": data.get("fields", data) if isinstance(data, dict) else data,
                "comments": _trim_comments(comm_raw) if comm_raw is not None else [],
            }
            if isinstance(item["fields"], dict):  # Jira Cloud gives rich text as ADF trees: text is what a plan needs
                for f in ("description", ac_field):
                    if f and isinstance(item["fields"].get(f), dict) and item["fields"][f].get("type") == "doc":
                        item["fields"][f] = _adf_text(item["fields"][f])
            if ac_field and not used_ac and not err:
                item["ac_field_skipped"] = ac_field
            if isinstance(item["fields"], dict) and not err:  # what the plan was made against: drift shows on --retest
                item["source_snapshot"] = {
                    "fields_sha1": _ticket_fingerprint(item["fields"], ac_field),
                    "captured_at": core.now(),
                }
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
    said = []  # the tickets' own words: what memory should look up for this plan
    for t in tickets:
        tfields = fd if isinstance(fd := t.get("fields"), dict) else {}
        for k in ("summary", "description", ac_field):
            if k and isinstance(tfields.get(k), str):
                said.append(tfields[k])
        said += [
            str(x.get("name") if isinstance(x, dict) else x)
            for k in ("components", "labels")
            for x in (tfields[k] if isinstance(tfields.get(k), list) else [])
        ]
    open_bugs = [memory.bugs(con, st) for st in core.VISIBLE]
    open_bugs = [b for b in open_bugs if not b.startswith("no ")]
    bundle = {
        "keys": keys,
        "acli": acli_ok,
        "config": {
            k: cfg.get(k)
            for k in (
                "jiraProject",
                "jiraKeyPrefix",
                "jiraAcField",
                "qaBaseUrl",
                "browser",
                "buildSource",
                "releaseTemplates",
                "inboxJql",
            )
            if cfg.get(k) is not None
        },
        "tickets": tickets,
        "plan_state": _with_drift(plan_find(proj, keys=keys), tickets),
        "rules": rules,
        "routes": codemap.code(con),
        "bugs_open": "\n".join(open_bugs) or "no OPEN bugs",
        "bugs_fixed": memory.bugs(con, "FIXED"),
        "context": memory.ticket_context(con, "\n".join(said)) if said else {},
    }
    return scrub.redact_json(bundle)
