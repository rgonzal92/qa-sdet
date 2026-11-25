"""PostToolUse and PostToolUseFailure entry point (hooks.json): feedback an agent can still act on, as additionalContext.
- The qa-explorer hears once when its run's budget or time is nearly used up: step 9 deletes its test records through
  the UI, which the guard refuses once the budget or time is gone. A failed Bash call spent its commands too, and fires
  only PostToolUseFailure.
- A qa-mapper hears at once when the code map it just wrote can't be saved. At its stop it could be too late: a parallel
  mapper's stop hook may have read the file first.
- A file written in a run folder is checked as it lands: a bug draft or repro spec (references/bug-draft.md), a run JSON
  (the shape ingest needs), a plan.json (case ids), so the agent fixes it while its context is still there.
- An explorer or crawler near its maxTurns hears once, even in a run with no budget or deadline (a default run), so it
  reaches step 9 (or the crawler's step 4) before Claude Code ends it. Its calls are counted in qa-out/.active.progress
  (Bash and Write calls only: a Read or Grep is not seen here, so the line is drawn early).
- An explorer whose same playwright-cli command failed 3 times since its last real progress hears once to change approach.
Feedback is best effort: any problem here means no feedback, never a failed tool call."""
import contextlib
import hashlib
import io
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
CODE_MAP = re.compile(r"code-map-(frontend|backend|tests)(-\d+)?\.json")
STUCK_AFTER = 3
SPIN_AFTER = 8  # playwright-cli calls in a row with no progress
NOVELTY_WINDOW = 12  # a call repeated within this many playwright-cli calls is no progress
AGENTS = ("qa-explorer", "qa-crawler")
# calls that only look: they are never progress by themselves
OBSERVE = {"snapshot", "find", "eval", "requests", "console", "screenshot", "tab-list", "network", "pdf"}
PROGRESS_CAP = 1 << 20
PW_CALL = re.compile(r"playwright-cli\s+(?:-s=\S+\s+)?(?:--raw\s+)?([\w-]+)(?:\s+(\S+))?")


def role(data):
    return str(data.get("agent_type") or "").strip().lower().split(":")[-1].strip()


def max_turns(m, who):
    """The agent's turn limit: the marker's max_turns, else maxTurns in the agent's own frontmatter."""
    try:
        n = int(float(m.get("max_turns")))
        if n > 0:
            return n
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        text = open(os.path.join(os.path.dirname(HERE), "agents", f"{who}.md"), encoding="utf-8").read(2000)
        return int(re.search(r"^maxTurns:\s*(\d+)", text, re.M).group(1))
    except (OSError, AttributeError):
        return 0


def turn_line(limit):
    """Calls to count before the wind-down: the last few turns are kept for step 9 (a Read or Grep isn't counted)."""
    return max(limit - max(8, limit // 4), 1)


def wind_down(path, m, who, turns):
    """The one wind-down warning: the budget or time nearly used up, else (any run) the turn limit near."""
    import guard
    budget, used, deadline = int(float(m.get("budget") or 0)), int(float(m.get("used") or 0)), float(m.get("deadline") or 0)
    left, secs, low = budget - used, deadline - time.time(), []
    if budget and left <= guard.cleanup_reserve(budget):
        low.append(f"{max(left, 0)} browser command{'' if left == 1 else 's'}")
    if deadline and secs <= 120:
        low.append(f"{max(int(secs), 0)} seconds")
    limit = max_turns(m, who)
    if not low and not (limit and turns >= turn_line(limit)):
        return None
    if guard.warned(path, m):  # one warning per run marker (the guard may have given it by refusing a call)
        return None
    guard.mark_warned(path, m)
    if low:
        return f"qa-sdet: only {' and '.join(low)} left in this run. {guard.WIND_DOWN[0].upper()}{guard.WIND_DOWN[1:]}."
    done = ("Stop crawling now and do step 4: save the requests and console logs, close the browser and write "
            "run-<slug>.json with status INCOMPLETE and the pages and locators so far" if who == "qa-crawler" else
            "Stop new checks now and go to step 9: delete the qa-sdet-… records you created, save the signals, close the "
            "browser and write run-<slug>.json with stop_reason turn_limit")
    return f"qa-sdet: {turns} of the {limit} turns you have are used, and the run ends at {limit} however much budget is left. {done}."


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
    if not calls:  # qa-mem and the rest: a turn, but not a browser call; an evidence image is a new file
        return {"t": "o", "p": int(ok and bool(re.search(r"\bqa-mem\s+(evidence|annotate)\b", cmd)))}
    key = hashlib.sha1(cmd.encode("utf-8", "replace")).hexdigest()[:12]  # never the text: it can hold a typed value
    seen = {r.get("k") for r in [r for r in recs if r.get("t") == "pw"][-NOVELTY_WINDOW:]}
    interacts = any(verb not in OBSERVE for verb, _ in calls)
    return {"t": "pw", "s": " ".join(x for x in calls[0] if x), "k": key, "ok": int(ok), "p": int(ok and interacts and key not in seen)}


def replay(recs):
    """(calls counted, playwright-cli calls since the last progress, failures per call since then, nudges given)."""
    turns = spin = 0
    fails, given = {}, set()
    for r in recs:
        if r.get("t") == "n":
            if r.get("n") == "stuck":
                fails.pop(r.get("s"), None)
            else:
                given.add(r.get("n"))
            continue
        turns += 1
        if r.get("p"):
            fails.clear()
            spin = 0
        elif r.get("t") == "pw":
            spin += 1
            if not r.get("ok"):
                fails[r.get("s")] = fails.get(r.get("s"), 0) + 1
    return turns, spin, fails, given


def run_feedback(data, who):
    """Count this call in the run's progress file, then: the same failing call repeated, a spin, the wind-down."""
    sys.path.insert(0, HERE)
    import guard
    path, m = guard.read_marker(guard._cwd(data))
    if not path:
        return None
    state, expires = path.with_name(".active.progress"), m.get("expires")
    recs = read_progress(state, expires)
    rec = classify(data, recs)
    append_progress(state, expires, rec)
    turns, spin, fails, given = replay(recs + [rec])
    notes, sig = [], rec.get("s")
    if sig and fails.get(sig, 0) >= STUCK_AFTER:
        notes.append(f"qa-sdet: `{sig}` failed {fails[sig]} times since you last got anywhere. Change approach: take a fresh "
                     "`snapshot` or `find '<text on the element>'` for its ref (an element in a frame needs a ref), or mark "
                     "the case BLOCKED with why and go on.")
        append_progress(state, expires, {"t": "n", "n": "stuck", "s": sig})  # it counts again from here
    elif spin >= SPIN_AFTER and "spin" not in given:
        notes.append(f"qa-sdet: your last {spin} browser calls opened no new page state, sent no new interaction and wrote no "
                     "file. If you are not in the middle of a check, change approach: another heuristic or element, or wrap up "
                     "and list what you did not test in not_tested.")
        append_progress(state, expires, {"t": "n", "n": "spin"})
    notes.append(wind_down(path, m, who, turns))
    return " ".join(x for x in notes if x) or None


def main():
    try:
        data = json.load(sys.stdin)
        note = None
        who = role(data) if isinstance(data, dict) else ""
        tool = data.get("tool_name") if isinstance(data, dict) else None
        if tool in ("Bash", "Write") and who in AGENTS:
            note = " ".join(x for x in (run_feedback(data, who), file_lint(data) if tool == "Write" else None) if x) or None
        elif tool == "Write" and who == "qa-mapper":
            note = code_map_feedback(data)
        elif tool == "Write":
            note = file_lint(data)
        if note:
            event = "PostToolUseFailure" if data.get("hook_event_name") == "PostToolUseFailure" else "PostToolUse"
            print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": note}}))
    except Exception:  # noqa: BLE001 - feedback is best effort
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
