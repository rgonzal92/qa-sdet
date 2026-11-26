"""PostToolUse and PostToolUseFailure entry point (hooks.json): feedback an agent can still act on, as additionalContext.
A failed Bash call fires only PostToolUseFailure.
- A qa-mapper hears at once when the code map it just wrote can't be saved. At its stop it could be too late: a parallel
  mapper's stop hook may have read the file first.
- A file written in a run folder is checked as it lands: a bug draft or repro spec (references/bug-draft.md), a run JSON
  (the shape ingest needs), a plan.json (case ids), so the agent fixes it while its context is still there. A bug draft
  that looks like a NOT A BUG or an already fixed bug in memory says so (a read-only lookup; it never blocks the write).
- An explorer's Bash and Write calls are counted in qa-out/.active.progress: one whose same playwright-cli command failed
  3 times since its last real progress hears to change approach, and 8 browser calls in a row without progress (a spin)
  get one nudge. Each line has the time the call ended: with the guard's request line it is the run's trace.
Feedback is best effort: any problem here means no feedback, never a failed tool call."""

import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
CODE_MAP = re.compile(r"code-map-(frontend|backend|tests)(-\d+)?\.json")
STUCK_AFTER = 3
SPIN_AFTER = 8  # playwright-cli calls in a row with no progress
NOVELTY_WINDOW = 12  # a call repeated within this many playwright-cli calls is no progress
AGENTS = ("qa-explorer",)
# calls that only look: they are never progress by themselves
OBSERVE = {"snapshot", "find", "eval", "requests", "console", "screenshot", "tab-list", "network", "pdf"}
PROGRESS_CAP = 1 << 20
PW_CALL = re.compile(r"playwright-cli\s+(?:-s=\S+\s+)?(?:--raw\s+)?([\w-]+)(?:\s+(\S+))?")
# every verb a call ran, for the trace: a browser verb as it is, a qa-mem one as qa-mem:<verb>; never an argument
VERBS = re.compile(
    r"playwright-cli\s+(?:-s=\S+\s+)?(?:--raw\s+)?([a-z][a-z-]*)|(?<![\w.-])qa-mem[\"']?\s+([a-z][a-z-]*)"
)
QA_TOOL_RE = re.compile(r"(?<![.\w])playwright-cli|\bacli\b|(?<![\w.-])qa-mem(?![\w-])")


def _qa_out(data):
    proj = os.environ.get("CLAUDE_PROJECT_DIR") or (data.get("cwd") if isinstance(data, dict) else None) or os.getcwd()
    return os.path.join(str(proj), "qa-out")


def _log_event(qa_out, event_type, fields):
    try:
        _lib()
        from qa_sdet import plugin_log

        plugin_log.append_event(qa_out, event_type, fields, create=False)
    except Exception:  # noqa: BLE001, S110 - logging is best effort
        pass


def role(data):
    return str(data.get("agent_type") or "").strip().lower().split(":")[-1].strip()


def code_map_feedback(data):
    tin = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    f = tin.get("file_path")
    if not isinstance(f, str) or not CODE_MAP.fullmatch(os.path.basename(f)):
        return None
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from pathlib import Path

    from qa_sdet import memory

    p = Path(str(data.get("cwd") or os.getcwd()), f)
    with contextlib.redirect_stderr(
        io.StringIO()
    ):  # its notes are for the user's terminal; the agent gets the error below
        memory.ingest_pending(p.parent)
    err = memory.parked(p)  # whoever read it first: a sibling's hook may have parked it a moment ago
    if not err:
        return None
    _log_event(
        p.parent.parent if p.parent.name == "init" else _qa_out(data),
        "nudge",
        {"agent": "qa-mapper", "kind": "code_map_lint", "detail": f"{p.name}: {err}"},
    )
    refs = os.path.join(os.path.dirname(HERE), "references", "code-map.md")
    return (
        f"qa-sdet memory could not save {p.name}: {err}. Fix the rows or fields it names (rules: {refs}), then Write "
        "the whole file again; it is re-read automatically."
    )


def _lib():
    lib = os.path.join(os.path.dirname(HERE), "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)


def draft_match(name, text):
    """Advice when the bug draft just written looks like a bug the user marked NOT A BUG, or one fixed before: a read-only
    look at the memory DB (qa-mem bug-match's lookup), never a block. None for no match, no DB, a busy or broken one."""
    title, route = re.match(r"#[ \t]+(.+)", text), re.search(r"\*\*Route:\*\*\s*(\S+)", text)
    if not title or not route:
        return None
    actual = re.search(r"^## Actual[ \t]*\n(.*?)(?=(?:^## )|\Z)", text, re.M | re.S)
    net = re.search(r"Network:\s*`?([A-Z]+ /[^\s`]+)", text)
    try:
        import sqlite3

        from qa_sdet import core, memory

        db = core.db_path()
        if not db.is_file():
            return None
        con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        con.row_factory = sqlite3.Row
        try:
            hits = [
                x
                for _, x in memory.bug_matches(
                    con,
                    route.group(1),
                    f"{title.group(1)} {(actual.group(1) if actual else '')[:300]}",
                    net.group(1) if net else None,
                )
                if x["status"] in memory.MATCH_ADVICE
            ][:2]
        finally:
            con.close()
    except Exception:  # noqa: BLE001 - a lookup that fails is no advice
        return None
    if not hits:
        return None
    cmd = f"`qa-mem bug-match '{route.group(1)}' '<symptom>'`"
    return (
        f"qa-sdet: {name[:-3]} looks like "
        + "; ".join(
            f"{x['hash']} ({x['page']} {memory.core.short(x['title'], 60)}): {memory.MATCH_ADVICE[x['status']]}"
            for x in hits
        )
        + f". Check it with {cmd}; if it is the same bug, leave this draft out of the run JSON's bugs and say so in notes."
    )


def file_lint(data):
    """Problems of a draft, spec, run JSON or plan.json just written into a run folder, as one note; else None."""
    tin = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    f = tin.get("file_path")
    if not isinstance(f, str):
        return None
    from pathlib import Path

    p = Path(str(data.get("cwd") or os.getcwd()), f)
    where, name = p.parent.name, p.name
    kind = (
        "draft"
        if where == "bugs" and re.fullmatch(r"BUG-\d+\.md", name)
        else "spec"
        if where == "repro" and re.fullmatch(r"BUG-\d+\.spec\.ts", name)
        else "run"
        if where == ".data" and re.fullmatch(r"run-.+\.json", name)
        else "plan"
        if where == ".data" and name == "plan.json"
        else None
    )
    if not kind or p.parent.parent.parent.name != "qa-out":
        return None
    try:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    _lib()
    from qa_sdet import memory, report

    qa_out, run_name = p.parent.parent.parent, p.parent.parent.name
    note = None
    if kind in ("draft", "spec"):
        problems = [t for _, t in (report.draft_issues(text) if kind == "draft" else report.spec_issues(text))]
        rules = os.path.join(os.path.dirname(HERE), "references", "bug-draft.md")
        notes = [
            f"qa-sdet: {name}: {'; '.join(problems)}. Fix it (rules: {rules}) and Write it again."
            if problems
            else None,
            draft_match(name, text) if kind == "draft" else None,
        ]
        note = " ".join(x for x in notes if x) or None
    else:
        try:
            doc = json.loads(text)
        except (ValueError, RecursionError) as e:
            note = f"qa-sdet: {name} is not valid JSON ({e}); Write the whole file again."
        else:
            if kind == "run":
                try:
                    memory.check_run(doc)
                except ValueError as e:
                    rules = os.path.join(os.path.dirname(HERE), "references", "run-json.md")
                    note = f"qa-sdet: memory can't save {name}: {e}. Fix that field (rules: {rules}) and Write the whole file again."
            else:
                warnings = []
                report.merged_cases(doc if isinstance(doc, dict) else {}, [], warnings)
                note = f"qa-sdet: {name}: {' '.join(warnings)}" if warnings else None
    if note:
        _log_event(
            qa_out,
            "nudge",
            {"agent": role(data) or "main", "kind": "file_lint", "file_kind": kind, "run": run_name, "detail": note},
        )
    return note


def read_progress(path, expires):
    """This run's records in qa-out/.active.progress (one JSON object per line), not through a planted link or FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            text = f.read(PROGRESS_CAP).decode("utf-8", "replace")
    except OSError:
        return []
    recs = []
    for line in text.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("e") == expires:
            recs.append(r)
    return recs


def append_progress(path, expires, rec):
    """One O_APPEND write of one short line: calls running in parallel add to it without losing each other's lines."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"e": expires, **rec}, separators=(",", ":")) + "\n")
    except OSError:
        pass


def classify(data, recs):
    """What this call was, for the progress file. p: real progress (a file written, or a successful interaction that
    isn't a repeat of one of the last NOVELTY_WINDOW playwright-cli calls): it clears the failure and spin counts."""
    tin = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    if data.get("tool_name") == "Write":
        return {"t": "w", "p": 1}  # a new file
    cmd = " ".join(str(tin.get("command") or "").split())
    ok = data.get("hook_event_name") != "PostToolUseFailure"
    calls = PW_CALL.findall(cmd)
    # never the text: it can hold a typed value (guard.call_key: the request line's key, so the trace pairs the two)
    key = hashlib.sha1(cmd.encode("utf-8", "replace"), usedforsecurity=False).hexdigest()[:12]
    ran = " ".join(pw or f"qa-mem:{mem}" for pw, mem in VERBS.findall(cmd)[:12])
    verbs = {"v": ran} if ran else {}
    if not calls:  # qa-mem and the rest: a turn, but not a browser call; an evidence image is a new file
        return {
            "t": "o",
            "k": key,
            "p": int(ok and bool(re.search(r"\bqa-mem\s+(evidence|annotate)\b", cmd))),
            **verbs,
        }
    seen = {r.get("k") for r in [r for r in recs if r.get("t") == "pw"][-NOVELTY_WINDOW:]}
    interacts = any(verb not in OBSERVE for verb, _ in calls)
    return {
        "t": "pw",
        "s": " ".join(x for x in calls[0] if x),
        "k": key,
        "ok": int(ok),
        "p": int(ok and interacts and key not in seen),
        **verbs,
    }


def replay(recs):
    """(playwright-cli calls since the last progress, failures per call since then, nudges given)."""
    spin = 0
    fails, given = {}, set()
    for r in recs:
        if r.get("t") == "q":  # the guard's request line: the trace's, never a turn of its own
            continue
        if r.get("t") == "n":
            if r.get("n") == "stuck":
                fails.pop(r.get("s"), None)
            else:
                given.add(r.get("n"))
            continue
        if r.get("p"):
            fails.clear()
            spin = 0
        elif r.get("t") == "pw":
            spin += 1
            if not r.get("ok"):
                fails[r.get("s")] = fails.get(r.get("s"), 0) + 1
    return spin, fails, given


def slow_word(guard, data):
    """What kept the guard from approving this Bash call, so that Claude Code's own permission check ran (a program
    such as `jq`, or a shape such as a loop): None for an approved or a denied call."""
    tin = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    if data.get("tool_name") != "Bash":
        return None
    cmd, cwd = guard.clean(tin.get("command") or ""), guard._cwd(data)
    ctx = guard.Ctx(cwd, True, cmd)
    if guard.check(cmd, cwd, ctx) or (ctx.approve and ctx.segments) or not ctx.blocker:
        return None
    return guard.blocker_word(ctx.blocker)


def cases_done(guard, data, m):
    """The explorer wrote this run's <run>/.data/run-<slug>.json: it does so once its given cases are done, so the
    trace's time after this line is what the run spent beyond them."""
    tin = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    f = tin.get("file_path")
    if data.get("tool_name") != "Write" or not isinstance(f, str) or not (m.get("run_dir") and m.get("slug")):
        return False
    want = os.path.join(str(m["run_dir"]), ".data", f"run-{m['slug']}.json")
    return os.path.realpath(os.path.join(guard._cwd(data), guard.clean(f))) == os.path.realpath(want)


def run_feedback(data, who):
    """Count this call in the run's progress file, then: the same failing call repeated, or a spin."""
    sys.path.insert(0, HERE)
    import guard

    path, m = guard.read_marker(guard._cwd(data))
    if not path:
        return None
    state, expires = path.with_name(".active.progress"), m.get("expires")
    recs = read_progress(state, expires)
    rec = classify(data, recs) | {"ts": round(time.time(), 3)}
    if data.get("tool_use_id"):
        rec["id"] = guard.clean(data["tool_use_id"])[:64]
    if cases_done(guard, data, m):
        rec["f"] = "run"
    append_progress(state, expires, rec)
    spin, fails, given = replay(recs + [rec])
    notes, sig = [], rec.get("s")
    run_name = os.path.basename(m["run_dir"]) if m.get("run_dir") else None
    if sig and fails.get(sig, 0) >= STUCK_AFTER:
        notes.append(
            f"qa-sdet: `{sig}` failed {fails[sig]} times since you last got anywhere. Change approach: take a fresh "
            "`snapshot` or `find '<text on the element>'` for its ref (an element in a frame needs a ref), or mark "
            "the case BLOCKED with why and go on."
        )
        append_progress(state, expires, {"t": "n", "n": "stuck", "s": sig})  # it counts again from here
        _log_event(
            path.parent,
            "nudge",
            {"agent": who, "kind": "stuck", "detail": sig, "count": fails[sig], "run": run_name, "slug": m.get("slug")},
        )
    elif spin >= SPIN_AFTER and "spin" not in given:
        notes.append(
            f"qa-sdet: your last {spin} browser calls opened no new page state, sent no new interaction and wrote no "
            "file. If you are not in the middle of a check, change approach: another heuristic or element, or wrap up "
            "and list what you did not test in not_tested."
        )
        append_progress(state, expires, {"t": "n", "n": "spin"})
        _log_event(
            path.parent,
            "nudge",
            {
                "agent": who,
                "kind": "spin",
                "detail": f"{spin} calls without progress",
                "count": spin,
                "run": run_name,
                "slug": m.get("slug"),
            },
        )
    word = slow_word(guard, data)
    if word and f"slow:{word}" not in given:
        notes.append(
            f"qa-sdet: `{word}` sent that call to Claude Code's own permission check (about 5 s, against under 1 for an "
            "approved call). Approved at once: `playwright-cli`, `qa-mem` and `sleep` up to 30, joined by `;` or `&&` "
            "and piped into `grep`, `head`, `tail`, `sed -n 'N,Mp'`, `cut`, `sort`, `uniq`, `wc`, `tr` or `awk`, with "
            "no variable, loop, `$(…)` or `cd` to another folder."
        )
        append_progress(state, expires, {"t": "n", "n": f"slow:{word}"})
        _log_event(
            path.parent,
            "nudge",
            {"agent": who, "kind": "slow_call", "detail": word, "run": run_name, "slug": m.get("slug")},
        )
    return " ".join(x for x in notes if x) or None


def empty_search(raw, err):
    """grep found nothing: exit 1 with no other text, and grep is the last command of the call's last pipeline (whose
    exit the shell reports for the whole call). An answer, not a failure."""
    if not isinstance(err, str) or not re.fullmatch(r"(?i)\s*(?:exit(?: code)?:?\s*1)?\s*", err):
        return False
    try:
        lex = shlex.shlex(raw.replace("\n", ";"), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        words = list(lex)
    except ValueError:  # an unclosed quote: whatever failed, it wasn't only a search
        return False
    last, start = None, True
    for w in words:
        if w and set(w) <= set(";&|()"):  # a list or pipeline operator: the next word runs a command
            start = True
        elif start:
            last, start = w, False
    return os.path.basename(last or "") in ("grep", "egrep", "fgrep")


def _log_tool_failure(data, who):
    tin = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    raw = str(tin.get("command") or "")
    cmd = " ".join(raw.split())
    if who not in ("qa-explorer", "qa-mapper") and not QA_TOOL_RE.search(cmd):
        return
    err = data.get("error") or data.get("tool_response") or ""
    if empty_search(raw, err):
        return
    if isinstance(err, (dict, list)):
        err = json.dumps(err, ensure_ascii=False, default=str)
    _log_event(_qa_out(data), "tool_fail", {"agent": who or "main", "cmd": cmd, "error": str(err)})


def main():
    data = None
    try:
        data = json.load(sys.stdin)
        note = None
        who = role(data) if isinstance(data, dict) else ""
        tool = data.get("tool_name") if isinstance(data, dict) else None
        if isinstance(data, dict) and data.get("hook_event_name") == "PostToolUseFailure" and tool == "Bash":
            _log_tool_failure(data, who)
        if tool in ("Bash", "Write") and who in AGENTS:
            note = (
                " ".join(x for x in (run_feedback(data, who), file_lint(data) if tool == "Write" else None) if x)
                or None
            )
        elif tool == "Write" and who == "qa-mapper":
            note = code_map_feedback(data)
        elif tool == "Write":
            note = file_lint(data)
        if note:
            event = "PostToolUseFailure" if data.get("hook_event_name") == "PostToolUseFailure" else "PostToolUse"
            print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": note}}))
    except Exception as e:  # noqa: BLE001 - feedback is best effort
        try:
            _log_event(
                _qa_out(data),
                "hook_error",
                {
                    "hook": "posttooluse",
                    "agent": role(data) if isinstance(data, dict) else "main",
                    "error_type": type(e).__name__,
                    "detail": str(e),
                },
            )
        except Exception:  # noqa: BLE001, S110
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
