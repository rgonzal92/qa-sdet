"""Tickets across run folders: `inbox` (Jira tickets assigned to me), `prep` (one ticket's bundle) and `trace`."""

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import core, md, ops, results, runstate, scrub


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
    dirs = runstate.plan_folders(qa_out)
    found = {}
    for p in dirs:
        d = results.load_run(p)
        plan, runs = d["plan"], d["runs"]
        tickets = results.plan_tickets(plan)
        matching = [t for t in tickets if t["key"].strip().upper() in keys and t["key"].strip().upper() not in found]
        if not matching:
            continue
        kind = str(plan.get("kind") or "")
        vmap = (
            {}
            if kind == "plan"
            else results.verdicts(plan, runs, results.merged_cases(plan, runs), results.run_bugs(runs), d["unread"])
        )
        for t in matching:
            found[t["key"].strip().upper()] = "plan" if kind == "plan" else results.VERDICT_ICON[vmap[t["key"]][0]]
        if len(found) == len(keys):
            break
    return found


def inbox(con, project, data, prefix=None, today=None):
    """Markdown for INBOX.md (also printed); tracks {key: status} in meta inbox_seen and cross-references qa-out/."""
    out = ops.project_dir(project) / "qa-out"  # a typo must not create a stray folder
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
        lines += [md.row(*head), "|" + "---|" * len(head)]
        for x in items:
            cols = [x["tag"], x["key"], x["summary"], x["status"], x["priority"]]
            if show_updated:
                cols.append(x["updated"][:10])
            if show_qa:
                cols.append(qa_map.get(x["key"], "—"))
            lines.append(md.row(*cols))
    else:
        lines.append("No open tickets assigned to you.")
    urgent = [x["key"] for x in items if x["tag"] in ("NEW", "CHANGED")]
    unpassed = [x["key"] for x in items if qa_map.get(x["key"]) != results.VERDICT_ICON["PASS"]]
    pick = urgent or unpassed[:3] or [x["key"] for x in items[:3]]
    p = str(prefix or "").strip().rstrip("-")
    p = p.upper() if p.isascii() else ""
    nums = [k[len(p) + 1 :] if p and k.startswith(p + "-") else k for k in pick]
    lines += ["", f"Next: /qa-sdet:qa-ticket {' '.join(nums)}" if nums else "Next: nothing to test"]
    text = "\n".join(lines) + "\n"
    out.mkdir(exist_ok=True)
    core.write(out / "INBOX.md", text)
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
            results.load_json(Path(x["plan_file"]), [])
            for x in (state.get("latest_run"), state.get("reusable_plan"))
            if x and x.get("plan_file")
        ),
        None,
    )
    before = {
        str(t.get("key")).upper(): t.get("source_snapshot")
        for t in results.entries(plan, "tickets")
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
        plan = results.load_json(d / ".data" / "plan.json", [])
        plan = plan if isinstance(plan, dict) else {}
        hit = next((c for c in results.entries(plan, "cases") if str(c.get("id")) == str(cid)), None)
        prev = str(plan.get("retest_of") or "")
        if hit or not prev or Path(prev).name != prev or prev in (".", ".."):  # a sibling run folder only
            return hit
        d = d.parent / prev
    return None


def trace(project, keys=None, fix_version=None):
    """Each ticket's latest run across qa-out (newest folder that ran it): verdict, date, builds, AC coverage, bugs,
    and what it retested: a release or sprint sign-off view. Read-only."""
    qa_out = ops.project_dir(project) / "qa-out"
    want = {k.strip().upper() for k in keys or []}
    rows, versions = {}, {}  # each ticket's newest run's row; the fix versions any of its runs gave it
    for p in runstate.plan_folders(qa_out) if qa_out.is_dir() else []:
        d = results.load_run(p)
        plan, runs = d["plan"], d["runs"]
        if not runs or str(plan.get("kind")) == "plan":
            continue
        cases, bugs = results.merged_cases(plan, runs), results.run_bugs(runs)
        vmap = results.verdicts(plan, runs, cases, bugs, d["unread"])
        prior = results.prior_passed(plan, [t["key"] for t in results.plan_tickets(plan)], cases)
        builds = ", ".join(sorted({core.norm_build(r.get("build")) for _, r in runs}))
        for t in results.plan_tickets(plan):
            k = t["key"].upper()
            if want and k not in want:
                continue
            versions.setdefault(k, set()).add(str(t.get("fix_version") or ""))
            if k in rows:
                continue
            acs = [results.ac_key(results.ac_id(a)) for a in results.entries(t, "acs") if results.ac_id(a)]
            covered = {
                a for c in cases + prior if c.get("ticket") == t["key"] for a in results.case_acs(c)
            }  # as verdict() counts
            v, why = vmap[t["key"]]
            rows[k] = md.row(
                t["key"],
                results.VERDICT_ICON[v],
                f"{sum(a in covered for a in acs)}/{len(acs)} AC",
                builds,
                len(results.ticket_bugs(t["key"], cases, bugs, list(vmap))),
                p.name,
                str(plan.get("retest_of") or ""),
                why,
            )
    shown = [r for k, r in rows.items() if not fix_version or fix_version in versions[k]]
    if not shown:
        return "no ticket runs match"
    return "\n".join(
        [
            md.row("Ticket", "Verdict", "ACs covered", "Build", "Bugs", "Latest run", "Retest of", "Why"),
            "|---|---|---|---|---|---|---|---|",
        ]
        + shown
    )


def prep(con, project, key_args):
    """Bundle keys, Jira ticket + comments (via read-only acli if signed in), reusable plan/run state, qa-rules.md,
    and memory routes/bugs in one scrubbed JSON dict."""
    from . import codemap, memory

    proj = ops.project_dir(project)
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
        "plan_state": _with_drift(runstate.plan_find(proj, keys=keys), tickets),
        "rules": rules,
        "routes": codemap.code(con),
        "bugs_open": "\n".join(open_bugs) or "no OPEN bugs",
        "bugs_fixed": memory.bugs(con, "FIXED"),
        "context": memory.ticket_context(con, "\n".join(said)) if said else {},
    }
    return scrub.redact_json(bundle)
