"""The pieces of REPORT.md, each a string the report joins with a blank line: status lines, lists and folds, a case,
an AC's results, a bug's entry with its filing commands, the checks before a Jira comment is posted, exploration
notes and the history table."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from urllib.parse import quote

from . import core, md, ops, results, scrub, signals

TYPE_CHECKING = False  # mypy reads it as True (pyproject always_true): typing stays out of the import path
if TYPE_CHECKING:  # NOSONAR
    from typing import Any

    from .contracts import JsonObj, Verdict

STATUS_RANK = ("FAIL", "BLOCKED", "QUESTION", "NOT_RUN", "PASS")  # worst first
COVERAGE_ICON = {"pass": "✅", "fail": "❌", "blocked": "⛔"}


@core.record
class Folder:
    """A run folder as the report reads it: what its files hold, and what the config, memory, Jira, the audit and the
    reviewer say about its bugs."""

    run_dir: Path
    date: str  # the folder's day
    plan: JsonObj  # .data/plan.json
    keys: list[str]  # the plan's ticket keys
    cases: list[JsonObj]  # the merged cases, without the passes kept from an earlier build
    bugs: list[JsonObj]  # the bugs of its run files
    cfg: JsonObj  # the qa-sdet config
    states: dict[str, Any]  # each bug's state in memory
    dups: dict[str, Any]  # the saved Jira duplicate hits, by bug id
    audit: dict[str, Any]  # the audit of each bug's draft and spec
    reviews: dict[str, Any]  # the reviewer's word on each bug


@core.record
class TicketResult:
    """One ticket's results as render_report_parts gathers them."""

    mine: list[JsonObj]  # its cases, as run here
    prior: list[JsonObj]  # its passes from an earlier build, not re-run here
    builds: list[str]  # the builds of the runs that tested it
    v: Verdict
    why: str  # the verdict's reason
    signals: list[tuple[Any, Any]]  # the unexplained browser signals on its pages: (page, signal)
    name: str  # the file name of its Jira comment


def bug_link(bid, run_dir):
    """A link to bugs/BUG-n.md when bid is exactly BUG-n and the draft exists; anything else as escaped text."""
    bid = str(bid or "")
    if re.fullmatch(r"BUG-\d+", bid) and (Path(run_dir) / "bugs" / f"{bid}.md").exists():
        return f"[{bid}](bugs/{bid}.md)"
    return md.one_line(bid)


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
            out += [f"  - {md.cell(x)}" for x in v]
        elif v and not isinstance(v, (list, dict)):
            out.append(f"- **{label}:** {md.cell(v)}")
    return out


CASE_RESULT_WORD = {"PASS": "PASS", "FAIL": "FAIL", "BLOCKED": "BLOCKED", "NOT_RUN": "NOT RUN", "QUESTION": "QUESTION"}


def _evidence_file(run_dir, cid):
    """evidence/TC-<n>.png (relative, URL-quoted) when the case image exists, else ''. Only TC-style ids."""
    cid = str(cid or "")
    if not re.fullmatch(r"[A-Za-z]+-\d+", cid):
        return ""
    name = f"{cid}.png"
    return f"evidence/{quote(name)}" if (Path(run_dir) / "evidence" / name).is_file() else ""


def draft_title(draft_path, fallback):
    for line in results.read_text(draft_path).splitlines():
        if line.startswith("# "):
            return " ".join(line[2:].split())
    return " ".join(str(fallback or "").split())


def _repro_badge(bid, b, audit, reviews=None):
    """'repro ✓' or '⚠ <worst problem> (+n)' for a CONFIRMED or SUSPECTED draft, and the reviewer's word when there is
    one (a note for the user: it never changes a verdict); '' otherwise."""
    if not bid or results.first_word(b.get("classification")) not in ("confirmed", "suspected"):
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


# ---------- REPORT.md blocks: each a string; the report joins them with a blank line, so every heading, list, table,
# fenced block and fold has one around it
def plural(n, word):
    return f"{n} {word}{'s' * (n != 1)}"


def dot_line(bits):
    """One text line of the report from its parts, escaped (a leading `#` or ``` can't head or fence the rest)."""
    s = " · ".join(md.one_line(b) for b in bits if b)
    return ("\\" if s[:1] in tuple("#`~>-+*=_") else "") + s


def _indent(text, n):
    return "\n".join(" " * n + ln if ln else ln for ln in text.split("\n"))


def bullets(items):
    """A list from its item texts: a blank line between the items when one of them holds more than a line."""
    return ("\n\n" if any("\n" in i for i in items) else "\n").join(items)


def numbered(items):
    out = []
    for n, item in enumerate(items, 1):
        first, _, rest = item.partition("\n")
        out.append(f"{n}. {first}" + ("\n" + _indent(rest, len(f"{n}. ")) if rest else ""))
    return bullets(out)


def fold(summary, blocks, opened=False):
    """A <details> fold: `summary` is plain text (HTML-escaped here), `blocks` Markdown."""
    head = f"<details{' open' if opened else ''}><summary>{md.summary_text(summary)}</summary>"
    return "\n\n".join([head, *blocks, "</details>"])


def _link(text, target):
    """[text](target): the text escaped, `target` a relative path already URL-quoted."""
    shown = md.one_line(text).replace("[", "\\[").replace("]", "\\]")
    return f"[{shown}]({target})"


def _sh(lines):
    return "```sh\n" + "\n".join(lines) + "\n```"


def worst_status(cases):
    return next(s for s in STATUS_RANK if any(c["status"] == s for c in cases))


def case_item(c, run_dir, with_status):
    """A case as a list item: id, title and result in bold, then what it saw and its image; its page, steps and expected
    result folded (open when it did not pass)."""
    esc = lambda v: md.one_line(v).replace("*", "\\*")
    head = " ".join(x for x in (esc(c["id"]), esc(c.get("title"))) if x)
    if with_status:
        word = CASE_RESULT_WORD[c["status"]]
        if c.get("prior"):
            word += f" on build {esc(c.get('build') or core.UNKNOWN)}, not re-run here"
        head += f": {word}" + (f" ({bug_link(c['bug'], run_dir)})" if c.get("bug") else "")
    actual, notes = md.cell(c.get("actual")), md.cell(c.get("notes"))
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
                f"Page: {md.one_line(c['route'])}" if c.get("route") else "",
                f"priority {md.one_line(c['priority'])}" if c.get("priority") else "",
            )
            if x
        )
    ]
    steps = c.get("steps")
    if isinstance(steps, list):
        inner.append("\n".join(f"{i}. {md.paragraph(x)}" for i, x in enumerate(steps, 1)))
    elif steps:
        inner.append(md.paragraph(steps))
    inner += [f"Expected: {md.cell(c['expected'])}"] if c.get("expected") else []
    inner = [b for b in inner if b]
    line = f"- **{head}.**" + (f" {shown}" if shown else "")
    if not inner:
        return line
    return line + "\n\n" + _indent(fold("Steps", inner, with_status and c["status"] != "PASS"), 2)


def ac_results(t, cases, run_dir, with_status, level):
    """Blocks for a ticket's cases: its test plan folded, a heading per AC with the cases that cover it (a case under the
    first AC it names, the others pointing there), then the ticket's cases that name none of its ACs."""
    blocks = [fold("Test plan", ["\n".join(_plan_lines(t))])] if _plan_lines(t) else []
    placed, acs = {}, results.entries(t, "acs")
    for ac in acs:
        aid = results.ac_id(ac)
        covering = [c for c in cases if aid and results.ac_key(aid) in results.case_acs(c)]
        now = [c for c in covering if not c.get("prior")]
        state = (
            "not covered by a case"
            if not covering
            else ""
            if not with_status
            else CASE_RESULT_WORD[worst_status(now)]
            if now
            else "PASS on build " + ", ".join(sorted({str(c.get("build") or core.UNKNOWN) for c in covering}))
        )
        title = " ".join(x for x in (md.one_line(aid), md.one_line(ac.get("text"))) if x) or "untitled"
        blocks.append(f"{'#' * level} {title}" + (f": {md.one_line(state)}" if state else ""))
        items = []
        for c in covering:
            if c["id"] in placed:
                items.append(f"- See {md.one_line(c['id'])} under {placed[c['id']]}.")
            else:
                placed[c["id"]] = md.one_line(aid)
                items.append(case_item(c, run_dir, with_status))
        blocks += [bullets(items)] if items else []
    rest = [c for c in cases if c["id"] not in placed]
    if rest:
        blocks += [f"{'#' * level} Other cases"] if acs else []
        blocks.append(bullets([case_item(c, run_dir, with_status) for c in rest]))
    return blocks


def _env_line(plan, runs):
    """The app's URL and the browser the runs used (plan.json is written before any browser opens: the runs win)."""
    env = plan.get("env") if isinstance(plan.get("env"), dict) else {}
    renvs = [r["env"] for _, r in runs if isinstance(r.get("env"), dict)]
    used = ", ".join(sorted({str(e["browser"]) for e in renvs if e.get("browser")})) or str(env.get("browser") or "")
    url = str(env.get("url") or (renvs[0].get("url") if renvs else "") or "")
    return dot_line([url, f"browser {used}" if used else ""])


def ran_as(runs):
    """'as <tenant and role>' the runs recorded (a tenant or role changes what a page shows), else ''."""
    renvs = [r["env"] for _, r in runs if isinstance(r.get("env"), dict)]
    ran = sorted({" ".join(scrub.redact(str(e["context"])).split()) for e in renvs if e.get("context")})
    return "as " + " / ".join(ran[:3]) if ran else ""


def jira_of(b, states):
    """(status, Jira key) of the memory bug this one is (filed before, now seen again), else (None, None)."""
    h = results.stored_hash(b)
    return next(
        ((st, k) for full, (st, k) in states.items() if k and full.startswith(h) and ops.KEY_RE.fullmatch(k)),
        (None, None),
    )


def bug_label(b):
    return b.get("id") or (f"known bug {str(b.get('hash'))[:12]}" if b.get("hash") else "")


def _bug_title(b, run_dir):
    bid = b.get("id") or ""
    return b.get("title") or (draft_title(Path(run_dir) / "bugs" / f"{bid}.md", "") if bid else "")


def bug_name(b, run_dir, linked=True):
    """'[BUG-n](bugs/BUG-n.md) title' (its id linked when the draft exists), 'known bug <hash> title' or the title."""
    ref = bug_link(b["id"], run_dir) if b.get("id") and linked else md.one_line(bug_label(b))
    return " ".join(x for x in (ref, md.one_line(_bug_title(b, run_dir))) if x) or "untitled bug"


def severity(b):
    return " ".join(str(b.get("severity") or "").split()).upper() or "unrated"


def bug_todo(b, run_dir, states):
    """The What to do line of one bug: comment on the Jira key memory links it to, file a CONFIRMED draft, or check it."""
    cls = results.first_word(b.get("classification"))
    sev = severity(b) + ("" if cls in ("confirmed", "") else f", {' '.join(str(b['classification']).split()).upper()}")
    what = f"{bug_name(b, run_dir)} ({md.one_line(sev)})"
    status, known = jira_of(b, states)
    if known:
        return f"Comment on {known} (already in Jira, {md.one_line(status)}): {what}"
    draft = Path(run_dir) / "bugs" / f"{b.get('id')}.md"
    return ("File the bug: " if cls == "confirmed" and b.get("id") and draft.is_file() else "Check the bug: ") + what


def _filing(b, owners, run_abs, cfg, states):
    """[the list item with a CONFIRMED draft's commands]: a new bug's create (and link, with jiraBugLinkType) and its
    bug-status follow-up, or a comment on the Jira key memory links it to; then the files to attach. [] otherwise."""
    bid = b.get("id") or ""
    draft = run_abs / "bugs" / f"{bid}.md"
    if not bid or results.first_word(b.get("classification")) != "confirmed" or not draft.is_file():
        return []
    status, known = jira_of(b, states)
    if known:  # filed before: the news goes on that issue, not on a duplicate
        say = f"already in Jira as {known} ({md.one_line(status)}): add this draft there as a comment"
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
            f"--summary {shlex.quote(draft_title(draft, b.get('title') or bid))} --description-file {shlex.quote(str(draft))}"
        ]
        if link_type and ticket:
            # not a vulnerability (S6350): printed for the user, never run; every value is shlex-quoted or a validated Jira key
            cmds.append(
                f"acli jira workitem link create --out <JIRA-KEY> --in {ticket} --type {shlex.quote(link_type)} --yes"
            )  # NOSONAR
        cmds.append(f"qa-mem bug-status {results.stored_hash(b)} FILED <JIRA-KEY>")
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


def bug_entry(b, owners, here, level, folder: Folder):
    """A bug's heading and its list: page, the cases that found it, its files and review, possible duplicates and the
    commands to file it. `here`: the plan tickets this part of the report is about (an owner outside them is named)."""
    run_dir, bid = Path(folder.run_dir), b.get("id") or ""
    cls = " ".join(str(b.get("classification") or "").split()).upper()
    head = (
        f"{'#' * level} {bug_name(b, run_dir, linked=False)} ({md.one_line(severity(b) + (', ' + cls if cls else ''))})"
    )
    found = [
        c for c in folder.cases if results.bug_refs(b) & results.case_bugs(c)
    ]  # the case that failed on it, and its AC
    where = [
        f"Page {md.one_line(b['route'])}" if b.get("route") else "",
        "found by "
        + "; ".join(md.one_line(c["id"]) + (f" ({md.one_line(c['ac'])})" if c.get("ac") else "") for c in found)
        if found
        else "",
        f"ticket {md.one_line(', '.join(owners))}" if owners and set(owners) - set(here) else "",
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
        md.cell(_repro_badge(bid, b, folder.audit, folder.reviews)),
    ]
    lines = [f"- {x}" for x in (" · ".join(x for x in where if x), " · ".join(x for x in files if x)) if x]
    if jira_of(b, folder.states)[1] is None:
        lines += [
            f"- possible duplicate: {md.one_line(x['key'])} {md.one_line(x.get('summary'))} (check it before filing)"
            for x in folder.dups.get(bid, [])
            if isinstance(x, dict) and x.get("key")
        ]
    lines += _filing(b, owners, run_dir.resolve(), folder.cfg, folder.states)
    return [head, bullets(lines)] if lines else [head]


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


def post_checks(t, i: TicketResult, run_dir, retest_of):
    """What to check before posting a ticket's Jira comment (`i`: the ticket's verdict, cases, builds and signals as
    render_report_parts gathers them): an unknown build, passes not re-run here, cases without their image; for a pass,
    the image to add per page and the unexplained browser signals on its pages (a pass over a 5xx may hide a bug; the
    verdict never changes)."""
    v, mine, prior, builds, unexplained = i.v, i.mine, i.prior, i.builds, i.signals
    out = []
    if not builds or any(core.norm_build(b) == core.UNKNOWN for b in builds):
        fix = t.get("fix_version")
        fix = " ".join(str(fix).split()) if isinstance(fix, (str, int, float)) and not isinstance(fix, bool) else ""
        fix = fix + ".0" if re.fullmatch(r"\d+\.\d+", fix) else fix
        out.append(
            f"The build is unknown in this run: confirm the QA app runs {md.one_line(fix)}, and name the build in the comment."
            if fix
            else "The build is unknown in this run: confirm which build the QA app runs, and name it in the comment."
        )
    if prior:
        pb = ", ".join(sorted({str(c.get("build") or core.UNKNOWN) for c in prior}))
        out.append(
            dot_line([f"{', '.join(str(c['id']) for c in prior)} passed on build {pb}"])
            + f" and {'was' if len(prior) == 1 else 'were'} not re-run here"
            + (f" (retest of {md.one_line(retest_of)})" if retest_of else "")
            + ": the comment says so."
        )
    out += [f"{md.one_line(cid)} has no evidence image: take it or say why." for cid in _evidence_gaps(mine, run_dir)]
    if v not in ("PASS", "PASS WITH NOTES"):
        return out
    route_of = lambda c: " ".join(str(c.get("route") or "").split())
    shots, seen = [], set()  # one passing image per page: one page and two passes proved neither
    for c in mine:  # the case links its image: named here, not linked again
        if route_of(c) not in seen and c["status"] == "PASS" and _evidence_file(run_dir, c["id"]):
            seen.add(route_of(c))
            shots.append((route_of(c), f"{md.one_line(c['id'])}'s image"))
    if not mine and retest_of:  # every case carried from the retested folder: its images are the ones to use
        for c in prior:
            rel = _evidence_file(Path(run_dir).parent / retest_of, c.get("id"))
            if rel and route_of(c) not in seen:
                seen.add(route_of(c))
                shots.append(
                    (route_of(c), _link(f"../{retest_of}/evidence/{c['id']}.png", quote(f"../{retest_of}/") + rel))
                )
    out += [f"Add {what} ({md.one_line(r or 'test page')}) to the comment in Jira." for r, what in shots]
    if not shots:
        out.append("No screenshot for this ticket yet: add one before you post.")
    severe = [k for _, k in unexplained if signals.signal_severity(k) == "severe"]
    if severe:
        out.append(
            f"{plural(len(severe), 'severe browser signal')} on its pages no bug explains (a 5xx, a failed request, an "
            "error body or a leak): a pass over them may hide a bug. Read Signals no bug explains first."
        )
    if len(unexplained) > len(severe):
        out.append(
            f"{plural(len(unexplained) - len(severe), 'other unexplained browser signal')} (4xx, console errors, request "
            "loops): see Signals no bug explains."
        )
    return out


def post_item(key, name, run_abs, checks, multi):
    head = f"Post the Jira comment{f' for {key}' if multi else ''}: [{name}]({name})" + (
        ", after these checks:" if checks else ""
    )
    cmd = f"acli jira workitem comment create --key {key} --body-file {shlex.quote(str(run_abs / name))}"
    return "\n\n".join([head, *(["\n".join(f"- {c}" for c in checks)] if checks else []), _sh([cmd])])


def notes_blocks(plan, runs):
    blocks = ["## Exploration notes"]
    if env := _env_line(plan, runs):
        blocks.append(f"App: {env}")
    blocks.append("Heuristics: ✅ checked, no problem · ❌ problem found · ⛔ could not be checked")
    for name, r in runs:
        route = r.get("target") or next((p.get("route") for p in results.entries(r, "pages")), "") or name
        cov = {}
        for p in results.entries(r, "pages"):
            for c in results.entries(p, "coverage"):
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
        inner = [f"Stopped: {md.cell(r['stop_reason'])}"] if r.get("stop_reason") else []
        if r.get("notes") or r.get("summary"):
            inner.append(md.paragraph(core.short(r.get("notes") or r.get("summary"), 1500)))
        blocks.append(fold(title, inner))
    leads = results.leads(runs)
    if leads:
        lines = [
            f"- {md.cell(x['priority'])} {md.cell(x['route'])}"
            + (f" ({md.cell(x['kind'])})" if x.get("kind") else "")
            + ": "
            + md.cell(": ".join(str(x[f]) for f in ("target", "reason") if x.get(f)))
            for x in leads[:10]
        ]
        blocks.append(fold(f"Suggested follow-up leads ({len(leads)})", ["\n".join(lines)]))
    return blocks


def history_table(run_dir, key):
    """The table of .data/history.jsonl's lines for one ticket, newest first; '' without one."""
    rows = []
    for line in reversed(results.read_text(Path(run_dir) / ".data" / "history.jsonl").splitlines()):
        try:
            h = json.loads(line)
        except ValueError:
            continue
        if isinstance(h, dict) and h.get("ticket") == key:
            n = h["cases"] if isinstance(h.get("cases"), dict) else {}
            rows.append(
                md.row(
                    h.get("date"),
                    h.get("build"),
                    results.VERDICT_ICON.get(str(h.get("verdict")), h.get("verdict")),
                    f"{n.get('pass', 0)} passed, {n.get('fail', 0)} failed, {n.get('other', 0)} other",
                    h.get("bugs"),
                )
            )
    if not rows:
        return ""
    return "\n".join([md.row("Date", "Build", "Verdict", "Cases", "Bugs"), "|---|---|---|---|---|", *rows])


def question_text(q, show_from=True):
    labels = ", ".join(str(w) for w in q["where"])
    return md.cell(q["text"]) + (f" _(from {md.cell(labels)})_" if labels and show_from else "")
