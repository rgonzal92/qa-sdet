"""PostToolUse and PostToolUseFailure entry point (hooks.json): feedback an agent can still act on, as additionalContext.
- The qa-explorer hears once when its run's budget or time is nearly used up: step 9 deletes its test records through
  the UI, which the guard refuses once the budget or time is gone. A failed Bash call spent its commands too, and fires
  only PostToolUseFailure.
- A qa-mapper hears at once when the code map it just wrote can't be saved. At its stop it could be too late: a parallel
  mapper's stop hook may have read the file first.
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
    flag, stamp = path.with_name(".active.warned"), str(m.get("expires"))  # one warning per run marker
    try:
        if flag.read_text() == stamp:
            return None
    except OSError:
        pass
    try:  # not through a planted link
        with os.fdopen(os.open(flag, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "w") as f:
            f.write(stamp)
    except OSError:
        pass
    return (f"qa-sdet: only {' and '.join(low)} left in this run. Stop new checks now and go to step 9: delete the "
            "qa-sdet-… records you created while UI actions still work (the guard refuses them once the budget or time is "
            "used up), then save the signals, close the browser and write run-<slug>.json.")


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


def main():
    try:
        data = json.load(sys.stdin)
        note = None
        if isinstance(data, dict) and data.get("tool_name") == "Bash" and role(data) == "qa-explorer":
            note = wind_down(data)
        elif isinstance(data, dict) and data.get("tool_name") == "Write" and role(data) == "qa-mapper":
            note = code_map_feedback(data)
        if note:
            event = "PostToolUseFailure" if data.get("hook_event_name") == "PostToolUseFailure" else "PostToolUse"
            print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": note}}))
    except Exception:  # noqa: BLE001 - feedback is best effort
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
