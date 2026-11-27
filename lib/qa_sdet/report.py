"""REPORT.md (one human report per run folder) and the run index: `render_report_parts` assembles the report from
`report_blocks`, section by section, with what `results` reads in the folder's `.data/plan.json` and run files."""

from __future__ import annotations

import fcntl
import json
import os
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from . import audit, core, md, report_blocks, report_jira, results, scrub, signals

TYPE_CHECKING = False  # mypy reads it as True (pyproject always_true): typing stays out of the import path
if TYPE_CHECKING:  # NOSONAR
    from .contracts import JsonObj


def _newest_run_file(run_dir):
    return max((results.mtime(f) for f in (Path(run_dir) / ".data").glob("run-*.json")), default=0.0)


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


def _reviews(run_dir, warnings):
    """{BUG-n: (verdict, notes)} from .data/review.json, the qa-reviewer's reply (--verify); {} when there is none."""
    data = results.load_json(Path(run_dir) / ".data" / "review.json", warnings)
    out = {}
    for x in results.entries(data, "bugs"):
        if isinstance(x.get("id"), str) and str(x.get("verdict")).strip().lower() in ("solid", "needs_check"):
            out[x["id"]] = (str(x["verdict"]).strip().lower(), " ".join(str(x.get("notes") or "").split()))
    return out


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


def _safe_key(key):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", str(key or "")))


def _warn_unknown_acs(tickets, cases, d):
    """A case naming an AC its ticket doesn't list covers nothing: a warning says so."""
    for t in tickets:  # a case naming an AC its ticket doesn't list covers nothing: say so
        have = {results.ac_key(results.ac_id(a)) for a in results.entries(t, "acs") if results.ac_id(a)}
        for c in (c for c in cases if have and c.get("ticket") == t["key"]):
            d["warnings"] += [
                f"{c['id']} names {a}, which {t['key']} doesn't have: fix the case's `ac` in plan.json"
                for a in sorted(results.case_acs(c) - have)
            ]


def _questions(plan, runs):
    """(open questions, the fold of the resolved ones) from the plan and the runs."""
    q_items = results.question_items(plan, "test plan") + [
        q for name, r in runs for q in results.question_items(r, str(r.get("target") or name))
    ]
    merged = results.merge_questions(q_items)
    open_qs = sorted([q for q in merged if not q["resolved"]], key=results.question_rank)
    answered = [q for q in merged if q["resolved"]]
    answers = "\n".join(
        f"- {md.cell(q['text'])}" + (f" — answer: {md.cell(q['answer'])}" if q["answer"] else "") for q in answered
    )
    resolved_fold = [report_blocks.fold(f"Resolved questions ({len(answered)})", [answers])] if answered else []
    return open_qs, resolved_fold


def _todo(rep, open_qs):
    """What to do, in order: each bug, each open question, test data left in the app, severe signals, then the Jira comments to post."""
    todo = [
        report_blocks.bug_todo(b, rep.run_dir, rep.folder.states)
        for b in sorted(rep.bugs, key=lambda b: results.first_word(b.get("classification")) != "confirmed")
    ]
    todo += [f"Answer: {report_blocks.question_text(q)}" for q in open_qs]
    todo += [
        f"Answer the question {md.one_line(c['id'])} raised: see Results."
        for c in rep.cases
        if c["status"] == "QUESTION" and not results.asked(c, open_qs)
    ]
    todo += [
        f"Delete the test data left in the app on {md.one_line(r.get('target') or name)}: {md.cell(m.group(1).strip())}"
        for name, r in rep.runs
        for m in re.finditer(r"Left in the app:[ \t]*([^\n]+)", core.as_text(r.get("notes")))
        if not re.fullmatch(r"(none|nothing|n/a)\s*([.;,(].*)?|-", m.group(1).strip(), re.I)
    ]  # step 9 couldn't delete them: they stay in the shared QA app
    severe = [k for _, (sig, _) in rep.triaged for k, _ in sig if signals.signal_severity(k) == "severe"]
    if severe:
        todo.append(
            f"Look into the {report_blocks.plural(len(severe), 'severe browser signal')} no bug explains (a 5xx, a failed "
            "request, an error body or a leak): see Signals no bug explains."
        )
    todo += [
        report_blocks.post_item(
            t["key"],
            rep.info[t["key"]].name,
            rep.run_abs,
            report_blocks.post_checks(t, rep.info[t["key"]], rep.run_dir, rep.retest_of),
            rep.multi,
        )
        for t in rep.tickets
        if rep.info[t["key"]].name in rep.drafts
    ]
    return todo


def _heading(t):
    """A ticket's heading: its key, and its summary when it has one."""
    return md.one_line(t["key"]) + (f": {md.one_line(t['summary'])}" if t.get("summary") else "")


def _status_line(rep, t, folder):
    """One ticket's status line. folder: it is the report's own (one ticket), so it also says what holds for the whole
    folder."""
    i = rep.info[t["key"]]
    passed = sum(c["status"] == "PASS" for c in i.mine) + len(i.prior)
    total = len(i.mine) + len(i.prior)
    n = len(results.ticket_bugs(t["key"], rep.cases, rep.bugs, rep.keys))
    return report_blocks.dot_line(
        [
            results.VERDICT_ICON[i.v],
            "build " + (", ".join(i.builds) if rep.runs else "not run yet"),
            rep.date if folder else "",
            f"{passed} of {report_blocks.plural(total, 'case')} passed"
            + (f" ({len(i.prior)} on an earlier build)" if i.prior else ""),
            report_blocks.plural(n, "bug") if n else "no bugs",
            f"retest of {rep.retest_of}" if rep.retest_of and folder else "",
            report_blocks.ran_as(rep.runs) if folder else "",
        ]
    )


def _head_blocks(rep, open_qs, resolved_fold):
    """The status line(s) under the title, then a plan's questions or a run's What to do."""
    blocks = []
    if rep.kind == "plan":
        blocks.append(
            report_blocks.dot_line(["plan", rep.date, "not run yet", report_blocks.plural(len(rep.cases), "case")])
        )
        if rep.multi:
            blocks.append(
                report_blocks.bullets(
                    [
                        f"- {_heading(t)} — {len(results.entries(t, 'acs'))} AC, {report_blocks.plural(len(rep.info[t['key']].mine), 'case')}"
                        for t in rep.tickets
                    ]
                )
            )
        if open_qs:
            blocks += [
                "## Questions",
                report_blocks.bullets([f"- {report_blocks.question_text(q, show_from=False)}" for q in open_qs]),
            ]
        blocks += resolved_fold
    else:
        if rep.single:
            blocks += [_status_line(rep, rep.tickets[0], True), report_blocks.dot_line([rep.info[rep.keys[0]].why])]
        elif rep.multi:
            blocks.append(
                report_blocks.dot_line(
                    [
                        rep.kind,
                        rep.date,
                        "build " + (", ".join(_run_builds(rep.runs)) or "not run yet"),
                        f"retest of {rep.retest_of}" if rep.retest_of else "",
                        report_blocks.ran_as(rep.runs),
                    ]
                )
            )
            blocks.append(
                report_blocks.bullets(
                    [
                        f"- {md.one_line(k)}: {results.VERDICT_ICON[rep.info[k].v]} — {md.one_line(rep.info[k].why)}"
                        for k in rep.keys
                    ]
                )
            )
        elif rep.plan or rep.runs:
            n = len(rep.bugs)
            blocks.append(
                report_blocks.dot_line(
                    [
                        rep.kind,
                        rep.date,
                        "build " + (", ".join(_run_builds(rep.runs)) or "not run yet"),
                        "; ".join(f"{r.get('target') or name} {r.get('status') or '?'}" for name, r in rep.runs),
                        report_blocks.plural(n, "bug") if n else "no bugs",
                        report_blocks.ran_as(rep.runs),
                    ]
                )
            )
        if rep.plan or rep.runs:
            todo = _todo(rep, open_qs)
            blocks += [
                "## What to do",
                report_blocks.numbered(todo) if todo else "Nothing needs your action.",
                *resolved_fold,
            ]
    return blocks


def _ticket_blocks(rep):
    """Each ticket's results and bugs, then the cases and bugs no ticket owns."""
    blocks = []
    level = 3 if rep.multi else 2
    for t in rep.tickets:
        i = rep.info[t["key"]]
        if rep.multi:
            blocks.append(f"## {_heading(t)}")
            blocks += [_status_line(rep, t, False)] if rep.with_status else []
        res = report_blocks.ac_results(t, i.mine + i.prior, rep.run_dir, rep.with_status, level + 1)
        if res:
            blocks += [f"{'#' * level} {'Results' if rep.with_status else 'Cases'}", *res]
        mine_bugs = [(b, o) for b, o in zip(rep.bugs, rep.owners) if rep.single or (o and o[0] == t["key"])]
        elsewhere = [  # a bug of several tickets is listed once, under the first
            f"- {report_blocks.bug_name(b, rep.run_dir)}: listed under {md.one_line(o[0])}."
            for b, o in zip(rep.bugs, rep.owners)
            if rep.multi and t["key"] in o[1:]
        ]
        if mine_bugs or elsewhere:
            blocks.append(f"{'#' * level} Bugs")
            for b, o in mine_bugs:
                blocks += report_blocks.bug_entry(b, o, [t["key"]], level + 1, rep.folder)
            blocks += [report_blocks.bullets(elsewhere)] if elsewhere else []
        if rep.multi and (table := report_blocks.history_table(rep.run_dir, t["key"])):
            blocks += ["### History", table]
    other = [c for c in rep.cases if c.get("ticket") not in rep.keys]
    if other:
        blocks += [
            "## Other test cases" if rep.tickets else "## Cases",
            report_blocks.bullets([report_blocks.case_item(c, rep.run_dir, rep.with_status) for c in other]),
        ]
    if not rep.single:
        rest = [(b, o) for b, o in zip(rep.bugs, rep.owners) if not (o and o[0] in rep.keys)]
        if rest:
            blocks.append("## Other bugs" if rep.tickets else "## Bugs")
            for b, o in rest:
                blocks += report_blocks.bug_entry(b, o, rep.keys, 3, rep.folder)
    return blocks


def _untested_and_signals(runs, triaged):
    """Not tested, by category, and the browser signals no bug explains."""
    blocks = []
    not_tested = [item for _, r in runs for item in results.not_tested(r)]
    if not_tested:
        blocks.append("## Not tested")
        for key, label in results.NOT_TESTED_CATEGORIES:
            group = [x for k, x in not_tested if k == key]
            if group:
                blocks.append(
                    report_blocks.fold(f"{label} ({len(group)})", ["\n".join(f"- {md.cell(x)}" for x in group)])
                )
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
                report_blocks.fold(
                    f"{third} third-party signal(s) (hosts outside allowedHosts)",
                    ["Failed requests to hosts outside `allowedHosts`: see `requests-*.log` in `.data/`."],
                )
            )
    return blocks


def _history_blocks(rep):
    """History: a single ticket's earlier runs, and drafts of earlier runs this run did not see."""
    blocks = []
    history = [report_blocks.history_table(rep.run_dir, rep.keys[0])] if rep.single else []
    listed = {b["id"] for b in rep.bugs}.union(*map(results.case_bugs, rep.cases))
    # shortcut: a known bug (named by its memory hash) is matched to its earlier draft by title, as the card gives
    # it; read the draft path from memory if explorers start rewording titles
    seen_again = {" ".join(core.as_text(b.get("title")).split()) for b in rep.bugs if b.get("hash") and not b.get("id")}
    orphans = sorted(
        (
            p
            for p in (rep.run_dir / "bugs").glob("BUG-*.md")
            if re.fullmatch(r"BUG-\d+", p.stem)
            and p.stem not in listed
            and p.is_file()
            and " ".join(report_blocks.draft_title(p, "").split()) not in seen_again
        ),
        key=lambda p: int(p.stem[4:]),
    )
    if rep.runs and orphans:  # a draft whose run file a later one replaced: still shown, but no verdict counts it
        history += [
            "Earlier bugs this run did not see (no verdict counts them; file one if it still applies):",
            "\n".join(
                f"- {report_blocks.bug_link(p.stem, rep.run_dir)} {md.one_line(report_blocks.draft_title(p, ''))}"
                for p in orphans
            ),
        ]
    if any(history):
        blocks += ["## History", *(x for x in history if x)]
    return blocks


def _folder(run_dir, cfg, keys, cases, bugs, d):
    """The folder as the report's blocks read it: its plan, cases and bugs, and beside the run files each bug's state in
    memory, the saved Jira duplicate hits, the audit of its draft and spec, and the reviewer's word."""
    dups = results.load_json(run_dir / ".data" / "jira-dups.json", [])
    return report_blocks.Folder(
        run_dir=run_dir,
        date=_run_date(run_dir, d["plan"]),
        plan=d["plan"],
        keys=keys,
        cases=cases,
        bugs=bugs,
        cfg=cfg,
        states=_db_bug_states() if bugs or keys else {},
        dups=dups if isinstance(dups, dict) else {},
        audit=audit.audit_bugs(run_dir) if bugs else {},
        reviews=_reviews(run_dir, d["warnings"]) if bugs else {},
    )


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
    tickets = results.plan_tickets(plan)
    keys = [t["key"] for t in tickets]
    cases, bugs = results.merged_cases(plan, runs, d["warnings"]), results.run_bugs(runs, d["warnings"])
    prior_all = results.prior_passed(plan, keys, cases)
    _warn_unknown_acs(tickets, cases, d)
    retest_of = " ".join(plan["retest_of"].split()) if isinstance(plan.get("retest_of"), str) else ""
    with_status = kind != "plan"
    cfg = core.config() if cfg is None else cfg
    vmap = results.verdicts(plan, runs, cases, bugs, d["unread"]) if tickets and with_status else {}
    cases = [c for c in cases if not c.get("prior")]  # from here on in prior_all, as a plan's prior_passed
    prior_all = [{**c, "id": str(c.get("id") or "?"), "status": "PASS", "prior": True} for c in prior_all]
    hosts, baseline = core.allowed_hosts(cfg), signals.signal_baseline()
    triaged = [(r.get("target") or name, signals.run_signals(run_dir, name, r, hosts, baseline)) for name, r in runs]
    folder = _folder(run_dir, cfg, keys, cases, bugs, d)
    owners = results.bug_owners(bugs, cases, keys)
    single, multi, date = len(tickets) == 1, len(tickets) > 1, folder.date
    drafts: dict[str, str] = {}
    info: dict[str, report_blocks.TicketResult] = {}
    for t in tickets:
        key = t["key"]
        mine = [c for c in cases if c.get("ticket") == key]
        prior = [c for c in prior_all if c.get("ticket") == key]
        ids = {c["id"] for c in mine}  # the builds of the runs that tested this ticket, not the whole folder's
        tested = [
            k for k, (_, r) in enumerate(runs) if any(str(x.get("id")) in ids for x in results.entries(r, "cases"))
        ]
        builds = (
            sorted({core.norm_build(runs[k][1].get("build")) for k in tested})
            or sorted({core.norm_build(r.get("build")) for _, r in runs})
            or sorted({core.norm_build(c.get("build")) for c in prior})
        )
        v, why = vmap.get(key) or ("NOT STARTED", "testing hasn't started yet")
        seen = [(triaged[k][0], s) for k in tested for s, _ in triaged[k][1][0]]  # its pages' unexplained signals
        name = "jira-comment.txt" if single else f"jira-comment-{key}.txt"
        info[key] = i = report_blocks.TicketResult(
            mine=mine, prior=prior, builds=builds, v=v, why=why, signals=seen, name=name
        )
        if v != "NOT STARTED" and _safe_key(key):
            drafts[name] = report_jira.comment(t, i, folder)

    rep = SimpleNamespace(
        bugs=bugs,
        cases=cases,
        folder=folder,
        date=date,
        drafts=drafts,
        info=info,
        keys=keys,
        kind=kind,
        multi=multi,
        plan=plan,
        retest_of=retest_of,
        run_abs=run_abs,
        run_dir=run_dir,
        runs=runs,
        single=single,
        tickets=tickets,
        triaged=triaged,
        cfg=cfg,
        owners=owners,
        with_status=with_status,
    )
    blocks = [
        f"# {_heading(tickets[0])}" if single else f"# QA report: {md.one_line(plan.get('title') or run_dir.name)}"
    ]
    if not plan and not runs:
        blocks.append("_Nothing recorded yet: no plan and no test run in this folder._")
    open_qs, resolved_fold = _questions(plan, runs)
    blocks += _head_blocks(rep, open_qs, resolved_fold)

    blocks += _ticket_blocks(rep)

    blocks += _untested_and_signals(runs, triaged)
    if runs:
        blocks += report_blocks.notes_blocks(plan, runs)
    blocks += _history_blocks(rep)
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
    tickets = results.plan_tickets(plan)
    if kind == "plan":
        verdicts = f"plan only ({len(cases)} cases)"
    elif tickets:
        vmap = results.verdicts(plan, runs, cases, bugs, d["unread"])
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
        (results.mtime(x) for x in (p, p / "REPORT.md", p / ".data" / "plan.json") if x.exists()), default=0.0
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
        vmap = results.verdicts(plan, runs, cases, bugs, d["unread"])
        date = datetime.fromtimestamp(_newest_run_file(run_dir)).strftime("%Y-%m-%d")  # noqa: DTZ006 - local, as _run_date
        build = ", ".join(_run_builds(runs)) or core.UNKNOWN
        lines = f.read_text(encoding="utf-8").splitlines() if f.exists() else []

        def parsed(line):
            try:
                v = json.loads(line)
            except ValueError:
                return {}  # a line it can't read stays as it is
            return v if isinstance(v, dict) else {}

        for t in results.plan_tickets(plan):
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
        core.write(f, "".join(x + "\n" for x in lines))
    except Exception:  # noqa: BLE001, S110 - the history is a side record: REPORT.md and the index must still be written
        pass


def write_report(run_dir: str | Path) -> Path:
    run_dir = Path(run_dir).resolve()
    if not run_dir.is_dir() or run_dir.parent.name != "qa-out":
        raise core.BadInput(f"report: not a run folder (qa-out/<run>/): {run_dir}")
    from . import ops  # ops imports this module inside its functions too

    live = ops.run_of(run_dir.parent)  # this folder's page whose explorer is still running: it will write again
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
        core.write(run_dir / "REPORT.md", text)
        # Jira comments: written with the report; one no longer due is removed. A failed render removes them all: an
        # earlier pass would stay postable after the run turned FAIL. A .md twin is from before the plain-text comment.
        for name, body in drafts.items():
            core.write(run_dir / name, body)
        for old in [*run_dir.glob("jira-comment*.md"), *run_dir.glob("jira-comment*.txt")]:
            if old.name not in drafts and (old.is_symlink() or old.is_file()):
                old.unlink(missing_ok=True)
        core.write(run_dir.parent / "README.md", render_index(run_dir.parent))
    finally:
        os.close(fd)
    return run_dir / "REPORT.md"
