"""Checks of what an explorer wrote: its bug drafts and repro specs (`audit-bugs`), and its run file when it stops."""

import json
import re
from pathlib import Path

from . import core, results

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
    for b in results.run_bugs(results.load_run(run_dir)["runs"]):
        bid = b.get("id")
        if not bid or results.first_word(b.get("classification")) not in ("confirmed", "suspected"):
            continue
        draft, spec, issues = run_dir / "bugs" / f"{bid}.md", run_dir / "repro" / f"{bid}.spec.ts", []
        if not draft.is_file():
            issues.append((True, f"no draft bugs/{bid}.md"))
        else:
            text = results.read_text(draft)
            issues += draft_issues(text)
            m = re.search(r"\*\*qa-mem hash:\*\*\s*`?([0-9a-fA-F]{4,12})\b", text)
            want = results.stored_hash(b)
            if not m:
                issues.append((False, "draft has no **qa-mem hash:** line"))
            elif not (want.startswith(m.group(1).lower()) or m.group(1).lower().startswith(want)):
                issues.append(
                    (True, f"draft hash {m.group(1)} is not the bug's ({want}): `qa-mem bug-status` would miss it")
                )
        if not spec.is_file():
            issues.append((True, f"no repro spec repro/{bid}.spec.ts"))
        else:
            issues += spec_issues(results.read_text(spec))
        if not (run_dir / "evidence" / f"{bid}.png").is_file():
            issues.append((False, f"no evidence image evidence/{bid}.png"))
        if issues:
            out[bid] = sorted(issues, key=lambda x: not x[0])
    return out


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
    route = re.search(r"^route: (.*)$", results.read_text(data / f"brief-{slug}.md"), re.M)
    plan = results.load_json(data / "plan.json", [])
    given = [
        str(c["id"])
        for c in results.entries(plan, "cases")
        if c.get("id") not in (None, "") and route and core.route_key(c.get("route")) == core.route_key(route.group(1))
    ]
    told = {str(c.get("id")) for c in results.entries(run, "cases")}
    if missing := [cid for cid in given if cid not in told]:
        todo.append(
            f"give every case of your brief a status in `cases`: {', '.join(missing)} missing "
            "(NOT_RUN with why, when the run ended before you reached it)"
        )
    done = [(str(c.get("id")), core.case_status(c.get("status")), c) for c in results.entries(run, "cases")]
    if blank := [
        cid for cid, status, c in done if status in ("PASS", "FAIL") and not str(c.get("actual") or "").strip()
    ]:
        todo.append(f"say in `actual` what you saw for each PASS and FAIL case: {', '.join(blank)} need `actual`")
    if no_bug := [cid for cid, status, c in done if status == "FAIL" and not str(c.get("bug") or "").strip()]:
        todo.append(
            f"a FAIL names its bug in `bug` (BUG-<n>, a known bug's hash or a Jira key): {', '.join(no_bug)} failed without a `bug`"
        )
    brief = results.read_text(data / f"brief-{slug}.md")
    open_items = [
        k
        for k in ("UNVISITED STATES", "UNEXERCISED ENDPOINTS", "UNTESTED TARGETS")
        if re.search(rf"^{k} \(", brief, re.M)
    ]
    if (
        open_items
        and results.first_word(run.get("status")) == "completed"
        and re.search(r"^mode: explore$", brief, re.M)
        and not any(results.entries(pg, "coverage") for pg in results.entries(run, "pages"))
        and not results.not_tested(run)
    ):
        todo.append(
            f"your card listed {', '.join(open_items)}, but `coverage` and `not_tested` are both empty: check them, or add a "
            "`not_tested` line for each saying why you did not"
        )
    listed = {results.bug_id(b) for b in results.entries(run, "bugs")} - {""}
    drafts = [p.stem for p in sorted((run_dir / "bugs").glob("BUG-*.md")) if p.is_file() and results.mtime(p) >= since]
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
