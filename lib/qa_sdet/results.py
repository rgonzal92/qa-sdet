"""What a run folder's files say: `.data/plan.json` and the run files merged into cases, bugs, questions and a
verdict per ticket."""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import core, md, scrub

TYPE_CHECKING = False  # mypy reads it as True (pyproject always_true): typing stays out of the import path
if TYPE_CHECKING:  # NOSONAR
    from collections.abc import Iterable, Sequence
    from typing import Any

    from .contracts import JsonObj, Verdict


VERDICT_ICON = {
    "FAIL": "❌ FAIL",
    "BLOCKED": "⛔ BLOCKED",
    "PASS WITH NOTES": "⚠️ PASS WITH NOTES",
    "PASS": "✅ PASS",
    "NOT STARTED": "⏳ NOT STARTED",
}


def load_json(path, warnings):
    try:
        # not a vulnerability (S2083): callers pass fixed names inside the run folder; bug ids are validated BUG-n
        return scrub.redact_json(json.loads(Path(path).read_text(encoding="utf-8-sig")))  # NOSONAR
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError) as e:
        warnings.append(f"{Path(path).name} unreadable ({type(e).__name__})")
        return None


def mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def load_run(run_dir: str | Path) -> dict[str, Any]:
    """{'plan': {...}, 'runs': [(file name, run dict)], 'warnings': [...], 'unread': [run file names that couldn't be
    read: verdict() blocks on them]}; missing pieces are simply absent."""
    data, warnings, unread = Path(run_dir) / ".data", [], []
    plan = load_json(data / "plan.json", warnings)
    if plan is not None and not isinstance(plan, dict):
        warnings.append("plan.json is not an object")
        plan = None
    runs = []
    for f in sorted(data.glob("run-*.json"), key=lambda x: (mtime(x), x.name)):
        if f.is_symlink() and not f.exists():
            warnings.append(f"{f.name} unreadable (FileNotFoundError)")
            unread.append(f.name)
            continue
        r = load_json(f, warnings)
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


def entries(obj, key):
    v = obj.get(key) if isinstance(obj, dict) else None
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def plan_tickets(plan):
    """The plan's tickets that can get a verdict: those with a text key."""
    return [t for t in entries(plan, "tickets") if isinstance(t.get("key"), str) and t["key"]]


# Not tested items are grouped in this order. A plain string (older runs) goes under "other".
NOT_TESTED_CATEGORIES = [
    ("safety", "Safety rules (not done on purpose)"),
    ("out_of_scope", "Out of scope for this ticket"),
    ("not_reached", "Not reached (route or run ended)"),
    ("needs_setup", "Needs a fault, account or data this run lacks"),
    ("heuristic", "Heuristics not run"),
    ("other", "Other"),
]


def not_tested(obj):
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


def now_build(runs):
    """The build of the newest run (load_run's order) that read one, else None. A ticket's folder keeps every re-run's
    files: a run file from another known build holds results the new build's run has not repeated."""
    known = [b for _, r in runs if (b := core.norm_build(r.get("build"))) != core.UNKNOWN]
    return known[-1] if known else None


def older_build(build, now):
    """A known build that is not `now` (now_build's): an unknown one can't be compared."""
    return bool(now) and core.norm_build(build) not in (core.UNKNOWN, now)


def merged_cases(
    plan: JsonObj, runs: Sequence[tuple[str, JsonObj]], warnings: list[str] | None = None
) -> list[JsonObj]:
    """Plan cases with their result from the run JSONs (missing -> NOT_RUN); cases only a run knows are appended.
    Each gets status, actual, notes, bug. When several runs report one case, the most decisive result wins (FAIL over
    QUESTION over PASS over BLOCKED over NOT_RUN; the newest run on a tie), so another route listing a case as
    "not run" never hides its FAIL. A plan ticket's PASS whose own `build` is not its run file's (carry_prior kept it
    from an earlier attempt), or whose run file is from another known build than the newest run's (a ticket's folder
    keeps every re-run's files: a page the new build's run never reached), gets that build and `prior`: prior_passed
    lists it as a pass not re-run here. A plan case with no id or a repeated one can't get a result: it is left out
    and named in `warnings`, and the case kept under that id gets `id_tickets` (every ticket using the id: verdict()
    blocks them)."""
    warnings = [] if warnings is None else warnings
    valid_id = lambda v: isinstance(v, (str, int, float)) and not isinstance(v, bool) and v != ""  # as core.txt
    results, run_build = {}, {}
    rank = lambda c: MERGE_RANK.index(core.case_status(c.get("status")) or "NOT_RUN")
    for _, r in runs:
        for c in entries(r, "cases"):
            if valid_id(c.get("id")):
                cid = str(c["id"])
                if cid not in results or rank(c) >= rank(results[cid]):
                    results[cid], run_build[cid] = c, r.get("build")
    now = now_build(runs)
    out, seen = [], set()
    plan_cases = entries(plan, "cases")
    plan_ids = {str(p["id"]) for p in plan_cases if valid_id(p.get("id"))}
    keys = [t["key"] for t in plan_tickets(plan)]
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
            what = md.one_line(core.short(c.get("title") or c.get("expected") or "untitled", 60))
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
            "actual": core.as_text(res.get("actual")),
            "notes": core.as_text(res.get("notes")),
            "bug": core.as_text(res.get("bug")),
        }
        if "annotated" in res:
            merged["annotated"] = res["annotated"]
        ran_on = core.norm_build(res["build"] if isinstance(res.get("build"), str) else run_build.get(cid))
        if (
            tk in keys
            and merged["status"] == "PASS"
            and (ran_on != core.norm_build(run_build.get(cid)) or older_build(ran_on, now))
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
        for b in entries(r, "bugs"):
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


def case_bugs(c):
    """Bugs a case names in `bug`: BUG-n ids and known bugs' 12-char hashes ("BUG-2", "BUG-2, 3f9a…", "see BUG-2")."""
    t = str(c.get("bug") or "")
    return set(re.findall(r"BUG-\d+", t)) | {h.lower() for h in re.findall(r"\b[0-9a-fA-F]{12}\b", t)}


def bug_refs(b):
    """What a case may call this bug by: its BUG-n id and, for a known bug, its hash."""
    m = re.match(r"#?\s*([0-9a-fA-F]{4,})\b", str(b.get("hash") or "").strip())
    h = m.group(1)[:12].lower() if m else ""
    return {x for x in (str(b.get("id") or ""), h) if x}


def ticket_bugs(key, cases, bugs, known=()):
    """Bugs that belong to one ticket: tagged with it, referenced by one of its cases, or untagged on one of its routes
    (unless a case of another ticket names it: then it is that ticket's bug, not every ticket's on a shared page)."""
    mine = [c for c in cases if c.get("ticket") == key]
    refs = set().union(*map(case_bugs, mine))
    named = set().union(*(case_bugs(c) for c in cases if c.get("ticket") and c.get("ticket") != key))
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
        or (bug_refs(b) & refs)
        or (
            tag(b) not in tickets
            and (b.get("route") in routes or b.get("target") in routes)
            and not (bug_refs(b) & named)
        )
    ]


def bug_owners(bugs, cases, tickets):
    """Per bug, the plan tickets it counts for (as their verdicts count it), else the ticket it names (not 'none').
    One ticket_bugs pass per ticket: each pass reads every case."""
    owned = [(str(t), {id(x) for x in ticket_bugs(t, cases, bugs, tickets)}) for t in tickets]
    return [
        [t for t, ids in owned if id(b) in ids]
        or ([str(b["ticket"])] if b.get("ticket") and str(b["ticket"]).strip().lower() != "none" else [])
        for b in bugs
    ]


def first_word(v):
    """'confirmed' from 'Confirmed (2/2)', 'high' from 'High ': agents add words to the value."""
    return (core.norm(v).split() or [""])[0]


def is_serious(b):
    return first_word(b.get("classification")) == "confirmed" and first_word(b.get("severity")) in (
        "critical",
        "high",
    )


def _bug_word(b):
    sev = " ".join(str(b.get("severity") or "").split()).upper() or "unrated"
    extra = "" if first_word(b.get("classification")) in ("confirmed", "") else f" {str(b['classification']).lower()}"
    name = b.get("id") or ("known bug " if b.get("hash") else "") + " ".join(core.as_text(b.get("title")).split())
    return f"{name} {sev}{extra}"


def _some(parts, n=3):
    return "; ".join(parts[:n]) + (f"; +{len(parts) - n} more" if len(parts) > n else "")


def ac_id(ac):
    """An AC's id as text ('' when missing or not a string or number: a list there must not break the report)."""
    return str(ac["id"]).strip() if isinstance(ac.get("id"), (str, int)) and not isinstance(ac["id"], bool) else ""


def ac_key(a):
    """An AC id as compared: case and spaces don't count ('ac-1 ' is AC-1)."""
    return re.sub(r"\s+", "", str(a)).upper()


def case_acs(c):
    """The AC ids a case covers, as ac_key gives them: "AC-1" or "AC-1, AC-2"."""
    return {ac_key(a) for a in re.split(r"[,;]", str(c.get("ac") or "")) if a.strip()}


def blocked_runs(cases, runs):
    """[(file name, run)] of the runs that ended BLOCKED on one of these cases' pages or reporting one of them: their
    PASS results can't be trusted. The one rule for verdict() and for what a partial --retest must re-run."""
    ids, routes = {c["id"] for c in cases}, {core.norm_route(c.get("route")) for c in cases if c.get("route")}
    return [
        (name, r)
        for name, r in runs
        if first_word(r.get("status")) == "blocked"
        and (core.norm_route(r.get("target")) in routes or any(str(x.get("id")) in ids for x in entries(r, "cases")))
    ]


def _failed_reason(fails):
    """One entry for the failed cases: the case and its AC, or how many failed and on which ACs; [] for none."""
    if len(fails) == 1:
        c = fails[0]
        failed = [f"{c['id']} failed" + (f" ({c['ac']})" if c.get("ac") else "")]
    else:  # several: one entry naming them all
        acs = sorted(
            {a for c in fails for a in case_acs(c)},
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
    return failed


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
    prior_passed: cases from an earlier build not re-run in this partial retest. questions: the plan's, as question_items gives
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
    failed = _failed_reason(fails)
    serious = [_bug_word(b) for b in its_bugs if is_serious(b)]
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
        for name, r in blocked_runs(mine, runs)
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
    covered = {a for c in mine for a in case_acs(c)} | {a for c in prior_mine for a in case_acs(c)}
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
            f"{ac_id(a)} has no test case"
            for a in entries(ticket, "acs")
            if ac_id(a) and ac_key(ac_id(a)) not in covered
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


def verdicts(
    plan: JsonObj,
    runs: Sequence[tuple[str, JsonObj]],
    cases: list[JsonObj],
    bugs: list[JsonObj],
    unread: Sequence[str] = (),
) -> dict[str, tuple[Verdict, str]]:
    """{ticket key: (verdict, reason)} for every plan ticket: the one place verdict() gets its inputs, so REPORT.md,
    the run index and the inbox's Last QA can't disagree. cases and bugs: merged_cases() and run_bugs() of `runs`."""
    tickets = plan_tickets(plan)
    keys = [t["key"] for t in tickets]
    prior_all, questions = prior_passed(plan, keys, cases), question_items(plan, "test plan")
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


def prior_passed(plan, keys, cases=()):
    """Normalized prior_passed entries from a partial --retest plan.json, then the merged `cases` marked `prior` (a pass
    carry_prior kept from an earlier build): both are passes not re-run here."""
    canon = {k.strip().upper(): k for k in keys}
    kept = [c for c in cases if c.get("prior")]
    ids, out = {c["id"] for c in kept}, []
    for c in entries(plan, "prior_passed"):
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


def stored_hash(b):
    """The hash memory keeps the bug under: a re-sighting (a regression's draft) the hash it names, else one from its
    route and symptom (as ingest: a blank symptom falls back to the title)."""
    return core.given_hash(b.get("hash")) or core.bug_hash(
        core.norm_route(b.get("route")),
        core.cap(scrub.redact(core.as_text(b.get("symptom")))) or core.cap(scrub.redact(core.as_text(b.get("title")))),
    )


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
            news = {str(c.get("id")): c for c in entries(new, "cases")}
            olds = {str(c.get("id")): c for c in entries(old, "cases")}
            ran_again = lambda cid: cid in given and core.case_status(news[cid].get("status")) not in (None, "NOT_RUN")
            cases = [news[cid] if cid in news and ran_again(cid) else {**stamp, **c} for cid, c in olds.items()]
            cases += [c for c in entries(new, "cases") if str(c.get("id")) not in olds]
            if olds:
                out["cases"] = cases
            hash_of = lambda b: bug_refs({"hash": b.get("hash")})
            listed = {bug_id(b) for b in entries(new, "bugs")} - {""}
            hashes = set().union(*map(hash_of, entries(new, "bugs")))
            named = lambda b, cs: any(case_bugs(c) & bug_refs({**b, "id": bug_id(b)}) for c in cs)
            same_build = core.norm_build(new.get("build")) == core.norm_build(old.get("build"))
            carried = [
                b
                for b in entries(old, "bugs")
                if bug_id(b) not in listed
                and not hash_of(b) & hashes
                and (named(b, cases) or (same_build and not named(b, olds.values())))
            ]
            # the explorer names an earlier bug it sees again by its memory hash, under the title the card gave it:
            # that entry and the carried draft are one bug
            title = lambda b: " ".join(core.as_text(b.get("title")).split())
            again = {title(b): b for b in entries(new, "bugs") if b.get("hash") and not bug_id(b)}
            same = {title(b) for b in carried if not b.get("hash")} & set(again)
            carried = [{**b, "hash": again[title(b)]["hash"]} if title(b) in same else b for b in carried]
            if carried:
                out["bugs"] = [b for b in entries(new, "bugs") if again.get(title(b)) is not b or title(b) not in same]
                out["bugs"] += carried
            text = lambda q: str(q.get("text") if isinstance(q, dict) else q)
            qs = lambda r: r["questions"] if isinstance(r.get("questions"), list) else []
            asked = set(map(text, qs(new)))
            if more := [q for q in qs(old) if text(q) not in asked]:
                out["questions"] = qs(new) + more
        if out != new or not rf.exists():
            core.write(rf, json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    if not keep:
        pf.unlink(missing_ok=True)


LEAD_RANK = {"HIGH": 0, "MEDIUM": 1}


def leads(runs, skip=()):
    """Follow-up leads the explorers left (`leads` in their run files), one per route and target, HIGH first; routes in
    `skip` (already explored) left out."""
    seen, out = set(), []
    for _, r in runs:
        for lead in entries(r, "leads"):
            route = core.norm_route(lead.get("route"))
            key = (route, core.norm(lead.get("target")))
            if route and route not in skip and key not in seen:
                seen.add(key)
                out.append({**lead, "route": route, "priority": str(lead.get("priority") or "MEDIUM").strip().upper()})
    return sorted(out, key=lambda x: LEAD_RANK.get(x["priority"], 2))


def read_text(path):
    try:
        # not a vulnerability (S2083): callers pass fixed names inside the run folder; bug ids are validated BUG-n
        return Path(path).read_text(encoding="utf-8", errors="replace")  # NOSONAR
    except OSError:
        return ""


# ---------- questions: plan, runs and QUESTION cases, merged; a string is open, {"text","status","answer"} as written ----
def question_items(obj, label):
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


def merge_questions(items):
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


def asked(c, open_qs):
    """Whether an open question is this QUESTION case's own: it names the case, or one says what the other says.
    Found live: What to do listed one question twice, by its text and by its case."""
    flat = lambda x: " ".join(re.sub(r"[^\w\s]", " ", core.as_text(x).lower()).split())
    said = [t for t in (flat(c.get("notes")), flat(c.get("actual"))) if len(t) >= 12]
    for q in open_qs:
        text = flat(q["text"])
        if re.search(rf"\b{re.escape(flat(c['id']))}\b", text) or any(
            t in text or text in t for t in said if len(text) >= 12
        ):
            return True
    return False


def question_rank(q):
    return 0 if "[BLOCKING]" in q["text"].upper() else 1 if "[HIGH]" in q["text"].upper() else 2
