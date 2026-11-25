"""PostToolUse and PostToolUseFailure entry point (hooks.json): feedback an agent can still act on, as additionalContext.
- The qa-explorer hears once when its run's budget or time is nearly used up: step 9 deletes its test records through
  the UI, which the guard refuses once the budget or time is gone. A failed Bash call spent its commands too, and fires
  only PostToolUseFailure.
- A qa-mapper hears at once when the code map it just wrote can't be saved. At its stop it could be too late: a parallel
  mapper's stop hook may have read the file first.
- A file written in a run folder is checked as it lands: a bug draft or repro spec (references/bug-draft.md), a run JSON
  (the shape ingest needs), a plan.json (case ids), so the agent fixes it while its context is still there.
- An explorer whose same playwright-cli command failed 3 times in a row hears once to change approach.
Feedback is best effort: any problem here means no feedback, never a failed tool call."""
import contextlib
import io
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
CODE_MAP = re.compile(r"code-map-(frontend|backend|tests)(-\d+)?\.json")
STUCK_AFTER = 3
PW_CALL = re.compile(r"playwright-cli\s+(?:-s=\S+\s+)?(?:--raw\s+)?([\w-]+)(?:\s+(\S+))?")


def role(data):
    return str(data.get("agent_type") or "").strip().lower().split(":")[-1].strip()


def wind_down(data):
    sys.path.insert(0, HERE)  # as pretooluse.py: also with PYTHONSAFEPATH set
    import guard
    path, m = guard.read_marker(guard._cwd(data))
    if not path:
        return None
    budget, used, deadline = int(float(m.get("budget") or 0)), int(float(m.get("used") or 0)), float(m.get("deadline") or 0)
    left, secs, low = budget - used, deadline - time.time(), []
    if budget and left <= guard.cleanup_reserve(budget):
        low.append(f"{max(left, 0)} browser command{'' if left == 1 else 's'}")
    if deadline and secs <= 120:
        low.append(f"{max(int(secs), 0)} seconds")
    if not low:
        return None
    if guard.warned(path, m):  # one warning per run marker (the guard may have given it by refusing a call)
        return None
    guard.mark_warned(path, m)
    return f"qa-sdet: only {' and '.join(low)} left in this run. {guard.WIND_DOWN[0].upper()}{guard.WIND_DOWN[1:]}."


def code_map_feedback(data):
    tin = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    f = tin.get("file_path")
    if not isinstance(f, str) or not CODE_MAP.fullmatch(os.path.basename(f)):
        return None
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
    from pathlib import Path
    from qa_sdet import memory
    p = Path(str(data.get("cwd") or os.getcwd()), f)
    with contextlib.redirect_stderr(io.StringIO()):  # its notes are for the user's terminal; the agent gets the error below
        memory.ingest_pending(p.parent)
    err = memory.parked(p)  # whoever read it first: a sibling's hook may have parked it a moment ago
    if not err:
        return None
    refs = os.path.join(os.path.dirname(HERE), "references", "code-map.md")
    return (f"qa-sdet memory could not save {p.name}: {err}. Fix the rows or fields it names (rules: {refs}), then Write "
            "the whole file again; it is re-read automatically.")


def _lib():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))


def file_lint(data):
    """Problems of a draft, spec, run JSON or plan.json just written into a run folder, as one note; else None."""
    tin = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    f = tin.get("file_path")
    if not isinstance(f, str):
        return None
    from pathlib import Path
    p = Path(str(data.get("cwd") or os.getcwd()), f)
    where, name = p.parent.name, p.name
    kind = ("draft" if where == "bugs" and re.fullmatch(r"BUG-\d+\.md", name) else
            "spec" if where == "repro" and re.fullmatch(r"BUG-\d+\.spec\.ts", name) else
            "run" if where == ".data" and re.fullmatch(r"run-.+\.json", name) else
            "plan" if where == ".data" and name == "plan.json" else None)
    if not kind or p.parent.parent.parent.name != "qa-out":
        return None
    try:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    _lib()
    from qa_sdet import memory, report
    if kind in ("draft", "spec"):
        problems = [t for _, t in (report.draft_issues(text) if kind == "draft" else report.spec_issues(text))]
        rules = os.path.join(os.path.dirname(HERE), "references", "bug-draft.md")
        return (f"qa-sdet: {name}: {'; '.join(problems)}. Fix it (rules: {rules}) and Write it again."
                if problems else None)
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError) as e:
        return f"qa-sdet: {name} is not valid JSON ({e}); Write the whole file again."
    if kind == "run":
        try:
            memory.check_run(doc)
        except ValueError as e:
            rules = os.path.join(os.path.dirname(HERE), "references", "run-json.md")
            return f"qa-sdet: memory can't save {name}: {e}. Fix that field (rules: {rules}) and Write the whole file again."
        return None
    warnings = []
    report.merged_cases(doc if isinstance(doc, dict) else {}, [], warnings)
    return f"qa-sdet: {name}: {' '.join(warnings)}" if warnings else None


def stuck(data):
    """A one-time hint when the explorer's same playwright-cli command failed STUCK_AFTER times in a row."""
    sys.path.insert(0, HERE)
    import guard
    path, m = guard.read_marker(guard._cwd(data))
    tin = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    call = PW_CALL.search(str(tin.get("command") or ""))
    if not path:
        return None
    state = path.with_name(".active.stuck")
    try:
        seen = json.loads(state.read_text())
        fails = seen["fails"] if seen.get("expires") == m.get("expires") and isinstance(seen.get("fails"), list) else []
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        fails = []
    sig = " ".join(x for x in call.groups() if x) if call else None
    failed = data.get("hook_event_name") == "PostToolUseFailure"
    fails = (fails + [sig] if fails[-1:] in ([sig], []) else [sig]) if failed and sig else []
    note = None
    if len(fails) >= STUCK_AFTER:
        note = (f"qa-sdet: `{sig}` failed {len(fails)} times in a row. Change approach: take a fresh `snapshot` or "
                "`find '<text on the element>'` for its ref (an element in a frame needs a ref), or mark the case BLOCKED "
                "with why and go on.")
        fails = []
    try:  # not through a planted link
        with os.fdopen(os.open(state, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "w") as f:
            json.dump({"expires": m.get("expires"), "fails": fails}, f)
    except OSError:
        pass
    return note


def main():
    try:
        data = json.load(sys.stdin)
        note = None
        if isinstance(data, dict) and data.get("tool_name") == "Bash" and role(data) in ("qa-explorer", "qa-crawler"):
            note = " ".join(x for x in (stuck(data), wind_down(data)) if x) or None
        elif isinstance(data, dict) and data.get("tool_name") == "Write" and role(data) == "qa-mapper":
            note = code_map_feedback(data)
        elif isinstance(data, dict) and data.get("tool_name") == "Write":
            note = file_lint(data)
        if note:
            event = "PostToolUseFailure" if data.get("hook_event_name") == "PostToolUseFailure" else "PostToolUse"
            print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": note}}))
    except Exception:  # noqa: BLE001 - feedback is best effort
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
