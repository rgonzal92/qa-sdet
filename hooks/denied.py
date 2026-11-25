"""PermissionDenied entry point (hooks.json): Claude Code's auto-mode classifier denied a call. It is logged next to the
guard's own denials (<project>/qa-out/plugin-log.jsonl, layer claude-code) when it concerns qa-sdet: its tools, its
agents, or a project that already has a qa-out folder, and the browser commands the guard counted for it are given back
to the run's budget (the call never ran). The denial stands."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))  # as pretooluse.py: also with PYTHONSAFEPATH set
import guard  # noqa: E402

try:
    data = json.load(sys.stdin)
except (ValueError, RecursionError):
    data = None
if isinstance(data, dict):
    try:
        cwd = guard.clean(data.get("cwd") or os.getcwd())
    except OSError:
        cwd = ""
    project = guard.clean(os.environ.get("CLAUDE_PROJECT_DIR") or "") or cwd
    if (guard.agent_role(data).startswith("qa-")
            or guard.mentions_guarded(json.dumps(data.get("tool_input") or {}, ensure_ascii=False, default=str), True)
            or (project and os.path.isdir(os.path.join(project, "qa-out")))):
        guard.log_denial(data, data.get("reason") or "denied by Claude Code", layer="claude-code")
    marker_path, _ = guard.read_marker(cwd)
    if marker_path and data.get("tool_name") == "Bash":
        try:
            guard.refund(marker_path, guard.clean(data.get("tool_use_id") or ""))
        except OSError:
            pass  # no refund: the call stays counted, as before
