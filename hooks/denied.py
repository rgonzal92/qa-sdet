"""PermissionDenied entry point (hooks.json): Claude Code's auto-mode classifier denied a call. It is logged next to the
guard's own denials (<project>/qa-out/guard-denials.log, layer claude-code) when it concerns qa-sdet: its tools, its
agents, or a project that already has a qa-out folder. It only logs: the denial stands."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # as pretooluse.py: also with PYTHONSAFEPATH set
import guard  # noqa: E402

try:
    data = json.load(sys.stdin)
except ValueError:
    data = None
if isinstance(data, dict):
    project = os.environ.get("CLAUDE_PROJECT_DIR") or str(data.get("cwd") or os.getcwd())
    if (guard.agent_role(data).startswith("qa-") or guard.mentions_guarded(json.dumps(data.get("tool_input") or {}), True)
            or os.path.isdir(os.path.join(project, "qa-out"))):
        guard.log_denial(data, data.get("reason") or "denied by Claude Code", layer="claude-code")
