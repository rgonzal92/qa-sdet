"""REPORT.md (one human report per run folder), the Jira comment and the run index, rendered from what `results`
reads in the folder's `.data/plan.json` and run files."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from . import audit, core, md, ops, results, scrub, signals

TYPE_CHECKING = False  # mypy reads it as True (pyproject always_true): typing stays out of the import path
if TYPE_CHECKING:  # NOSONAR
    from typing import Any

    from .contracts import JsonObj


STATUS_RANK = ("FAIL", "BLOCKED", "QUESTION", "NOT_RUN", "PASS")  # worst first
COVERAGE_ICON = {"pass": "✅", "fail": "❌", "blocked": "⛔"}


def _bug_link(bid, run_dir):
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


def _newest_run_file(run_dir):
    return max((results._mtime(f) for f in (Path(run_dir) / ".data").glob("run-*.json")), default=0.0)


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
    for line in results._text(draft_path).splitlines():
        if line.startswith("# "):
            return " ".join(line[2:].split())
    return " ".join(str(fallback or "").split())


def _reviews(run_dir, warnings):
    """{BUG-n: (verdict, notes)} from .data/review.json, the qa-reviewer's reply (--verify); {} when there is none."""
    data = results._load_json(Path(run_dir) / ".data" / "review.json", warnings)
    out = {}
    for x in results._list(data, "bugs"):
        if isinstance(x.get("id"), str) and str(x.get("verdict")).strip().lower() in ("solid", "needs_check"):
            out[x["id"]] = (str(x["verdict"]).strip().lower(), " ".join(str(x.get("notes") or "").split()))
    return out


def _repro_badge(bid, b, audit, reviews=None):
    """'repro ✓' or '⚠ <worst problem> (+n)' for a CONFIRMED or SUSPECTED draft, and the reviewer's word when there is
    one (a note for the user: it never changes a verdict); '' otherwise."""
    if not bid or results._first_word(b.get("classification")) not in ("confirmed", "suspected"):
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


# ---------- REPORT.md blocks: each a string; the report joins them with a blank line, so every heading, list, table,
# fenced block and fold has one around it
def _plural(n, word):
    return f"{n} {word}{'s' * (n != 1)}"


def _line(bits):
    """One text line of the report from its parts, escaped (a leading `#` or ``` can't head or fence the rest)."""
    s = " · ".join(md.one_line(b) for b in bits if b)
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
    head = f"<details{' open' if opened else ''}><summary>{md.summary_text(summary)}</summary>"
    return "\n\n".join([head, *blocks, "</details>"])


def _link(text, target):
    """[text](target): the text escaped, `target` a relative path already URL-quoted."""
    shown = md.one_line(text).replace("[", "\\[").replace("]", "\\]")
    return f"[{shown}]({target})"


def _sh(lines):
    return "```sh\n" + "\n".join(lines) + "\n```"


def _worst(cases):
    return next(s for s in STATUS_RANK if any(c["status"] == s for c in cases))


def _case_item(c, run_dir, with_status):
    """A case as a list item: id, title and result in bold, then what it saw and its image; its page, steps and expected
    result folded (open when it did not pass)."""
    esc = lambda v: md.one_line(v).replace("*", "\\*")
    head = " ".join(x for x in (esc(c["id"]), esc(c.get("title"))) if x)
    if with_status:
        word = CASE_RESULT_WORD[c["status"]]
        if c.get("prior"):
            word += f" on build {esc(c.get('build') or core.UNKNOWN)}, not re-run here"
        head += f": {word}" + (f" ({_bug_link(c['bug'], run_dir)})" if c.get("bug") else "")
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
    return line + "\n\n" + _indent(_fold("Steps", inner, with_status and c["status"] != "PASS"), 2)


def _results(t, cases, run_dir, with_status, level):
    """Blocks for a ticket's cases: its test plan folded, a heading per AC with the cases that cover it (a case under the
    first AC it names, the others pointing there), then the ticket's cases that name none of its ACs."""
    blocks = [_fold("Test plan", ["\n".join(_plan_lines(t))])] if _plan_lines(t) else []
    placed, acs = {}, results._list(t, "acs")
    for ac in acs:
        aid = results._ac_id(ac)
        covering = [c for c in cases if aid and results._ac_key(aid) in results._case_acs(c)]
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
        title = " ".join(x for x in (md.one_line(aid), md.one_line(ac.get("text"))) if x) or "untitled"
        blocks.append(f"{'#' * level} {title}" + (f": {md.one_line(state)}" if state else ""))
        items = []
        for c in covering:
            if c["id"] in placed:
                items.append(f"- See {md.one_line(c['id'])} under {placed[c['id']]}.")
            else:
                placed[c["id"]] = md.one_line(aid)
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
    h = results._stored_hash(b)
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
    ref = _bug_link(b["id"], run_dir) if b.get("id") and linked else md.one_line(_bug_label(b))
    return " ".join(x for x in (ref, md.one_line(_bug_title(b, run_dir))) if x) or "untitled bug"


def _sev(b):
    return " ".join(str(b.get("severity") or "").split()).upper() or "unrated"


def _bug_todo(b, run_dir, states):
    """The What to do line of one bug: comment on the Jira key memory links it to, file a CONFIRMED draft, or check it."""
    cls = results._first_word(b.get("classification"))
    sev = _sev(b) + ("" if cls in ("confirmed", "") else f", {' '.join(str(b['classification']).split()).upper()}")
    what = f"{_bug_name(b, run_dir)} ({md.one_line(sev)})"
    status, known = _jira_of(b, states)
    if known:
        return f"Comment on {known} (already in Jira, {md.one_line(status)}): {what}"
    draft = Path(run_dir) / "bugs" / f"{b.get('id')}.md"
    return ("File the bug: " if cls == "confirmed" and b.get("id") and draft.is_file() else "Check the bug: ") + what


def _filing(b, owners, run_abs, cfg, states):
    """[the list item with a CONFIRMED draft's commands]: a new bug's create (and link, with jiraBugLinkType) and its
    bug-status follow-up, or a comment on the Jira key memory links it to; then the files to attach. [] otherwise."""
    bid = b.get("id") or ""
    draft = run_abs / "bugs" / f"{bid}.md"
    if not bid or results._first_word(b.get("classification")) != "confirmed" or not draft.is_file():
        return []
    status, known = _jira_of(b, states)
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
            f"--summary {shlex.quote(_draft_title(draft, b.get('title') or bid))} --description-file {shlex.quote(str(draft))}"
        ]
        if link_type and ticket:
            # not a vulnerability (S6350): printed for the user, never run; every value is shlex-quoted or a validated Jira key
            cmds.append(
                f"acli jira workitem link create --out <JIRA-KEY> --in {ticket} --type {shlex.quote(link_type)} --yes"
            )  # NOSONAR
        cmds.append(f"qa-mem bug-status {results._stored_hash(b)} FILED <JIRA-KEY>")
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
    head = f"{'#' * level} {_bug_name(b, run_dir, linked=False)} ({md.one_line(_sev(b) + (', ' + cls if cls else ''))})"
    found = [c for c in cases if results._bug_refs(b) & results._case_bugs(c)]  # the case that failed on it, and its AC
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
        md.cell(_repro_badge(bid, b, ctx["audit"], ctx["reviews"])),
    ]
    lines = [f"- {x}" for x in (" · ".join(x for x in where if x), " · ".join(x for x in files if x)) if x]
    if _jira_of(b, ctx["states"])[1] is None:
        lines += [
            f"- possible duplicate: {md.one_line(x['key'])} {md.one_line(x.get('summary'))} (check it before filing)"
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
    v, mine, prior, builds, unexplained = i["v"], i["mine"], i["prior"], i["builds"], i["signals"]
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
            _line([f"{', '.join(str(c['id']) for c in prior)} passed on build {pb}"])
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
            f"{_plural(len(severe), 'severe browser signal')} on its pages no bug explains (a 5xx, a failed request, an "
            "error body or a leak): a pass over them may hide a bug. Read Signals no bug explains first."
        )
    if len(unexplained) > len(severe):
        out.append(
            f"{_plural(len(unexplained) - len(severe), 'other unexplained browser signal')} (4xx, console errors, request "
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
        route = r.get("target") or next((p.get("route") for p in results._list(r, "pages")), "") or name
        cov = {}
        for p in results._list(r, "pages"):
            for c in results._list(p, "coverage"):
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
        blocks.append(_fold(title, inner))
    leads = results._leads(runs)
    if leads:
        lines = [
            f"- {md.cell(x['priority'])} {md.cell(x['route'])}"
            + (f" ({md.cell(x['kind'])})" if x.get("kind") else "")
            + ": "
            + md.cell(": ".join(str(x[f]) for f in ("target", "reason") if x.get(f)))
            for x in leads[:10]
        ]
        blocks.append(_fold(f"Suggested follow-up leads ({len(leads)})", ["\n".join(lines)]))
    return blocks


def _history_table(run_dir, key):
    """The table of .data/history.jsonl's lines for one ticket, newest first; '' without one."""
    rows = []
    for line in reversed(results._text(Path(run_dir) / ".data" / "history.jsonl").splitlines()):
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


def _q_text(q, show_from=True):
    labels = ", ".join(str(w) for w in q["where"])
    return md.cell(q["text"]) + (f" _(from {md.cell(labels)})_" if labels and show_from else "")


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

    for ac in results._list(t, "acs"):
        aid = results._ac_id(ac)
        covering = [c for c in every if aid and results._ac_key(aid) in results._case_acs(c)]
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
    for b in results.ticket_bugs(t["key"], cases, bugs, keys):
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
        for q in results._q_items(plan, "test plan")
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
    d = results.load_run(run_dir)
    plan, runs = d["plan"], d["runs"]
    kind = str(plan.get("kind") or (runs[0][1].get("mode") if runs else "") or "explore")
    tickets = results._plan_tickets(plan)
    keys = [t["key"] for t in tickets]
    cases, bugs = results.merged_cases(plan, runs, d["warnings"]), results.run_bugs(runs, d["warnings"])
    prior_all = results._prior_passed(plan, keys, cases)
    for t in tickets:  # a case naming an AC its ticket doesn't list covers nothing: say so
        have = {results._ac_key(results._ac_id(a)) for a in results._list(t, "acs") if results._ac_id(a)}
        for c in (c for c in cases if have and c.get("ticket") == t["key"]):
            d["warnings"] += [
                f"{c['id']} names {a}, which {t['key']} doesn't have: fix the case's `ac` in plan.json"
                for a in sorted(results._case_acs(c) - have)
            ]
    retest_of = " ".join(plan["retest_of"].split()) if isinstance(plan.get("retest_of"), str) else ""
    with_status = kind != "plan"
    cfg = core.config() if cfg is None else cfg
    vmap = results._verdicts(plan, runs, cases, bugs, d["unread"]) if tickets and with_status else {}
    cases = [c for c in cases if not c.get("prior")]  # from here on in prior_all, as a plan's prior_passed
    prior_all = [{**c, "id": str(c.get("id") or "?"), "status": "PASS", "prior": True} for c in prior_all]
    hosts, baseline = core.allowed_hosts(cfg), signals._signal_baseline()
    triaged = [(r.get("target") or name, signals._signals(run_dir, name, r, hosts, baseline)) for name, r in runs]
    dups = results._load_json(run_dir / ".data" / "jira-dups.json", [])
    ctx = {
        "states": _db_bug_states() if bugs or tickets else {},
        "dups": dups if isinstance(dups, dict) else {},
        "audit": audit.audit_bugs(run_dir) if bugs else {},
        "reviews": _reviews(run_dir, d["warnings"]) if bugs else {},
    }
    owners = results._bug_owners(bugs, cases, keys)
    single, multi, date = len(tickets) == 1, len(tickets) > 1, _run_date(run_dir, plan)
    drafts: dict[str, str] = {}
    info: dict[str, dict[str, Any]] = {}  # per ticket: its cases, builds, verdict, signals and comment file
    for t in tickets:
        key = t["key"]
        mine = [c for c in cases if c.get("ticket") == key]
        prior = [c for c in prior_all if c.get("ticket") == key]
        ids = {c["id"] for c in mine}  # the builds of the runs that tested this ticket, not the whole folder's
        tested = [k for k, (_, r) in enumerate(runs) if any(str(x.get("id")) in ids for x in results._list(r, "cases"))]
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
        n = len(results.ticket_bugs(t["key"], cases, bugs, keys))
        return _line(
            [
                results.VERDICT_ICON[i["v"]],
                "build " + (", ".join(i["builds"]) if runs else "not run yet"),
                date if folder else "",
                f"{passed} of {_plural(total, 'case')} passed"
                + (f" ({len(i['prior'])} on an earlier build)" if i["prior"] else ""),
                _plural(n, "bug") if n else "no bugs",
                f"retest of {retest_of}" if retest_of and folder else "",
                _ran_as(runs) if folder else "",
            ]
        )

    heading = lambda t: md.one_line(t["key"]) + (f": {md.one_line(t['summary'])}" if t.get("summary") else "")
    blocks = [
        f"# {heading(tickets[0])}" if single else f"# QA report: {md.one_line(plan.get('title') or run_dir.name)}"
    ]
    if not plan and not runs:
        blocks.append("_Nothing recorded yet: no plan and no test run in this folder._")
    q_items = results._q_items(plan, "test plan") + [
        q for name, r in runs for q in results._q_items(r, str(r.get("target") or name))
    ]
    merged = results._merge_questions(q_items)
    open_qs = sorted([q for q in merged if not q["resolved"]], key=results._q_rank)
    answered = [q for q in merged if q["resolved"]]
    answers = "\n".join(
        f"- {md.cell(q['text'])}" + (f" — answer: {md.cell(q['answer'])}" if q["answer"] else "") for q in answered
    )
    resolved_fold = [_fold(f"Resolved questions ({len(answered)})", [answers])] if answered else []
    if kind == "plan":
        blocks.append(_line(["plan", date, "not run yet", _plural(len(cases), "case")]))
        if multi:
            blocks.append(
                _items(
                    [
                        f"- {heading(t)} — {len(results._list(t, 'acs'))} AC, {_plural(len(info[t['key']]['mine']), 'case')}"
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
                _items(
                    [
                        f"- {md.one_line(k)}: {results.VERDICT_ICON[info[k]['v']]} — {md.one_line(info[k]['why'])}"
                        for k in keys
                    ]
                )
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
                for b in sorted(bugs, key=lambda b: results._first_word(b.get("classification")) != "confirmed")
            ]
            todo += [f"Answer: {_q_text(q)}" for q in open_qs]
            todo += [
                f"Answer the question {md.one_line(c['id'])} raised: see Results."
                for c in cases
                if c["status"] == "QUESTION" and not results._asked(c, open_qs)
            ]
            todo += [
                f"Delete the test data left in the app on {md.one_line(r.get('target') or name)}: {md.cell(m.group(1).strip())}"
                for name, r in runs
                for m in re.finditer(r"Left in the app:[ \t]*([^\n]+)", core._s(r.get("notes")))
                if not re.fullmatch(r"(none|nothing|n/a)\s*([.;,(].*)?|-", m.group(1).strip(), re.I)
            ]  # step 9 couldn't delete them: they stay in the shared QA app
            severe = [k for _, (sig, _) in triaged for k, _ in sig if signals.signal_severity(k) == "severe"]
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
            f"- {_bug_name(b, run_dir)}: listed under {md.one_line(o[0])}."
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

    not_tested = [item for _, r in runs for item in results._not_tested(r)]
    if not_tested:
        blocks.append("## Not tested")
        for key, label in results.NOT_TESTED_CATEGORIES:
            group = [x for k, x in not_tested if k == key]
            if group:
                blocks.append(_fold(f"{label} ({len(group)})", ["\n".join(f"- {md.cell(x)}" for x in group)]))
    unexplained = [(route, key, n) for route, (sig, _) in triaged for key, n in sig]
    third = sum(n for _, (_, n) in triaged)
    if unexplained or third:
        blocks.append("## Signals no bug explains" + (f" ({len(unexplained)})" if unexplained else ""))
        lines = [
            f"- {md.one_line(route)}: {md.one_line(key)}" + (f" (×{n})" if n > 1 else "")
            for route, key, n in unexplained[: signals.SIGNALS_SHOWN]
        ]
        if len(unexplained) > signals.SIGNALS_SHOWN:
            lines.append(
                f"- +{len(unexplained) - signals.SIGNALS_SHOWN} more: see `requests-*.log` and `console-*.log` in `.data/`"
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
    listed = {b["id"] for b in bugs}.union(*map(results._case_bugs, cases))
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
            "\n".join(f"- {_bug_link(p.stem, run_dir)} {md.one_line(_draft_title(p, ''))}" for p in orphans),
        ]
    if any(history):
        blocks += ["## History", *(x for x in history if x)]
    if d["warnings"]:
        blocks += ["## Problems reading this folder", "\n".join(f"- {md.one_line(w)}" for w in d["warnings"])]
    return "\n\n".join(blocks) + "\n", drafts


def render_report(run_dir: str | Path) -> str:
    return render_report_parts(run_dir)[0]


def run_summary(run_dir: str | Path) -> tuple[str, str, str, int, str]:
    """(date, kind, verdict text, bug count, label) for the qa-out/README.md index: the label is `KEY — summary` for a
    one-ticket plan (a ticket's folder), else the folder's name."""
    d = results.load_run(run_dir)
    plan, runs = d["plan"], d["runs"]
    kind = str(plan.get("kind") or (runs[0][1].get("mode") if runs else "") or "explore")
    cases, bugs = results.merged_cases(plan, runs), results.run_bugs(runs)
    tickets = results._plan_tickets(plan)
    if kind == "plan":
        verdicts = f"plan only ({len(cases)} cases)"
    elif tickets:
        vmap = results._verdicts(plan, runs, cases, bugs, d["unread"])
        verdicts = ", ".join(f"{t['key']} {results.VERDICT_ICON[vmap[t['key']][0]]}" for t in tickets)
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
        (results._mtime(x) for x in (p, p / "REPORT.md", p / ".data" / "plan.json") if x.exists()), default=0.0
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
        name = md.one_line(label).replace("[", "\\[").replace("]", "\\]")
        rows.append(
            ((date, ts), md.row(date, md.Raw(f"[{name}]({quote(p.name, safe='+')}/REPORT.md)"), kind, verdicts, n))
        )
    rows.sort(key=lambda x: x[0], reverse=True)
    return (
        "\n".join(
            [
                "# QA runs",
                "Newest first. Open a run's REPORT.md for test cases, bugs and questions.",
                "",
                md.row("Date", "Run", "Kind", "Verdict", "Bugs"),
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
        d = results.load_run(run_dir)
        plan, runs = d["plan"], d["runs"]
        if (
            str(plan.get("kind") or "") != "ticket"
            or not (runs or d["unread"])
            or f.parent.is_symlink()
            or f.is_symlink()
        ):
            return  # nothing ran yet, or a link: never written through
        cases, bugs = results.merged_cases(plan, runs), results.run_bugs(runs)
        vmap = results._verdicts(plan, runs, cases, bugs, d["unread"])
        date = datetime.fromtimestamp(_newest_run_file(run_dir)).strftime("%Y-%m-%d")  # noqa: DTZ006 - local, as _run_date
        build = ", ".join(_run_builds(runs)) or core.UNKNOWN
        lines = f.read_text(encoding="utf-8").splitlines() if f.exists() else []

        def parsed(line):
            try:
                v = json.loads(line)
            except ValueError:
                return {}  # a line it can't read stays as it is
            return v if isinstance(v, dict) else {}

        for t in results._plan_tickets(plan):
            mine = [c for c in cases if c.get("ticket") == t["key"] and not c.get("prior")]  # as verdict() counts
            n = {s: sum(c["status"] == s for c in mine) for s in ("PASS", "FAIL")}
            entry = {
                "date": date,
                "build": build,
                "ticket": t["key"],
                "verdict": vmap[t["key"]][0],
                "cases": {"pass": n["PASS"], "fail": n["FAIL"], "other": len(mine) - n["PASS"] - n["FAIL"]},
                "bugs": len(results.ticket_bugs(t["key"], cases, bugs, list(vmap))),
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
            results.carry_prior(run_dir, slug, keep=slug == live)
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
                    f"# QA report: {md.one_line(run_dir.name)}\n\n_Could not render this report: {type(e).__name__}: "
                    f"{md.one_line(core.short(e, 200))}. Check the files in .data/. Jira comment drafts from an earlier "
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
