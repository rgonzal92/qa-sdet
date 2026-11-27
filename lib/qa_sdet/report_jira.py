"""The Jira comment: one plain-text file per ticket with a verdict, never posted (Jira writes are the user's)."""

import re

from . import core, ops, report_blocks, results

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


def comment(t, v, mine, prior, builds, date, cases, bugs, keys, plan, cfg, states):
    """A ticket's Jira comment, plain text: the result, build and date; the release and the fix when known; each AC with
    what its cases saw; the ticket's bugs by title, filed or not; its open blocking questions. No case or draft ids, no
    paths, no placeholders, nothing cut."""
    flat = lambda x: " ".join(core.as_text(x).split())
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

    for ac in results.entries(t, "acs"):
        aid = results.ac_id(ac)
        covering = [c for c in every if aid and results.ac_key(aid) in results.case_acs(c)]
        now = [c for c in covering if not c.get("prior")]
        state = (
            "not covered by a case"
            if not covering
            else AC_WORD[report_blocks.worst_status(now)]
            if now
            else word(covering[0])
        )
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
        known = report_blocks.jira_of(b, states)[1]
        filed |= {known} if known else set()
        # a bug with no draft here goes by its hash in REPORT.md and in what a case says: the line says which it is
        ref = "" if known or b.get("id") or not b.get("hash") else f"{report_blocks.bug_label(b)}, "
        found.append(
            f"Bug: {flat(b.get('title')) or 'untitled'} ({report_blocks.severity(b)}), {ref}{known or 'not filed yet'}"
        )
    named = {k for c in mine for k in ops.KEY_RE.findall(str(c.get("bug") or "").upper())}
    found += [
        f"Bug: {k}" for k in sorted(named - filed - {t["key"].upper()}) if not re.fullmatch(r"BUG-\d+", k)
    ]  # a case's BUG-n is this folder's draft, listed above: only a Jira key is one more
    groups += ["\n".join(found)] if found else []
    qs = [
        re.sub(r"^\s*(Q\d+\s*)?(\[[A-Z]+\]\s*)*", "", q["text"])
        for q in results.question_items(plan, "test plan")
        if not q["resolved"]
        and "[BLOCKING]" in q["text"].upper()
        and str(q.get("ticket") or t["key"]).strip().upper() == t["key"].upper()
    ]
    groups += ["\n".join(f"Open question: {q}" for q in qs)] if qs else []
    return "\n\n".join(groups) + "\n"
