#!/usr/bin/env python3
"""PreToolUse guard for qa-sdet (runs on every tool; most pass straight through). It stops mistakes; it isn't a sandbox.
Every agent, unless noted: R1 Jira stays read-only (acli reads, never from inline code; Jira MCP reads). R2 The browser
goes only to config allowedHosts (goto/open/tab-new, qa-mem browser-open, URLs in eval); no other browser, config file, install or
PLAYWRIGHT_MCP_*/PWTEST_* variable; --profile only on `open`, only the main profile. R3 Session secrets stay out:
cookie/storage/header reads, traces, state-save, run-code, the browser profile folder, uploads of secret files or globs.
R4 The qa-explorer uses only its tools (no MCP), writes only its run files, leaves the qa-sdet config and
.playwright/cli.config.json alone, fetches only from allowedHosts and
runs no script file, inline code or package (a page it read could have steered it to write or name one). R5 Nobody changes the qa-sdet data folder
(memory DB); sqlite3 on it only -readonly. R6 The run marker (qa-out/.active): faults need `faults: true`, API writes
`api_writes: true`; the explorer neither writes nor removes it. R7 The qa-mapper reads source
(no secrets) and writes only its code map. R8 A Bash
call fully checked in one parse is approved; every denial is logged. It doesn't emulate the shell: a call that names a
guarded tool or path and hides something in an odd form ($VAR program or verb, eval of a variable, a script fed to a
shell, inline code, xargs, a command string handed to another program, a computed path) is denied: run it directly.

The rules live in the guard_*.py modules beside this file, each importing only the ones before it: paths, shell,
tools, files, bash, log. This file holds the rules of the file and MCP tools, the verdict and the hook's entry."""

import json
import os
import re
import sys
from pathlib import Path

import guard_bash
import guard_files
import guard_log
import guard_paths

MCP_JIRA_RE = re.compile(
    r"^mcp__.*(atlassian|jira|confluence)", re.I
)  # server or tool name (mcp__corp__createJiraIssue)
MCP_JIRA_READ_RE = re.compile(r"^(batch_)?(get|search|lookup|list|fetch|read|view|download)|^atlassianuserinfo$", re.I)
MCP_WRITE_WORDS = set(
    "create edit update delete remove transition add assign archive unarchive move upload post put patch "
    "write set clone restore replace publish rank invoke run execute send import bulk upsert modify change".split()
)
# SubagentHandback only returns the finished report to the parent: no file, shell or network side effect
MAPPER_TOOLS = set(
    "Read Grep Glob LS Write ToolSearch TodoWrite TaskCreate TaskUpdate TaskGet TaskList SubagentHandback".split()
)
EXPLORER_TOOLS = MAPPER_TOOLS | {"Bash"}  # no MCP tool: the session's GitHub, Slack, ... servers are not the explorer's


# ---------- other tools, the hook ----------
def mcp_name_words(
    name,
):  # getJiraIssueWithEdit -> [get, jira, issue, with, edit]; jira_create_issue -> [jira, create, issue]
    return [w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", name)]


def check_jira_mcp(tool):
    """R1: an Atlassian/Jira MCP tool that isn't a read is denied; every other MCP tool passes."""
    name = tool[len("mcp__") :].rpartition("__")[2]
    bare = re.sub(r"^(jira|confluence)_", "", name, flags=re.I)
    if MCP_JIRA_RE.match(tool) and (
        not MCP_JIRA_READ_RE.search(bare) or MCP_WRITE_WORDS & set(mcp_name_words(bare)[1:])
    ):
        return f"{name} writes to Jira/Confluence; Jira writes are yours: the bug draft ends with the command to run"


def check_mapper(tool, tin, cwd):
    """R7: reading tools on source (no secret files), and Write only to its code map."""
    dirs = {
        os.path.realpath(os.path.join(b, "qa-out", "init"))
        for b in filter(None, (cwd, os.environ.get("CLAUDE_PROJECT_DIR")))
    }
    if tool not in MAPPER_TOOLS:
        return f"the qa-mapper reads source files and writes its code map; {tool} is not one of its tools"
    if tool == "Write" and any(
        os.path.dirname(t) not in dirs
        or not re.fullmatch(r"code-map-(frontend|backend|tests)(-\d+)?\.json", os.path.basename(t))
        for t in (guard_paths.real(p, cwd) for p in guard_paths.tool_paths(tin))
    ):
        return "the qa-mapper writes only <project>/qa-out/init/code-map-<area>[-<n>].json"
    if tool in ("Read", "Grep", "Glob", "LS"):
        extra = (
            guard_paths.clean(tin.get("pattern") or "")
            if tool == "Glob"
            else guard_paths.clean(tin.get("glob") or "")
            if tool == "Grep"
            else ""
        )
        if any(
            guard_paths.is_secret_path(guard_paths.real(p or ".", cwd))
            or (extra and guard_paths.is_secret_path(guard_paths.real(os.path.join(p or ".", extra), cwd)))
            for p in guard_paths.tool_paths(tin)
        ):
            return "the qa-mapper reads source only; secret files (.env, keys, credentials, qa-sdet data, config) stay closed: write [secret]"


def check_explorer(tool, tin, cwd, role="qa-explorer"):
    """R4: the explorer's tools only, and Write only to its run files (run_file)."""
    if tool not in EXPLORER_TOOLS:
        return f"the {role} works through Bash (playwright-cli, qa-mem); {tool} is not one of its tools"
    if tool == "Write" and any(
        not guard_paths.run_file(guard_paths.real(p, cwd), {cwd}) for p in guard_paths.tool_paths(tin)
    ):
        return guard_paths.run_files_reason(role)


def check_reviewer(tool, tin, cwd):
    """The qa-reviewer (a critic of untrusted drafts) only Reads: the project's qa-out run files and the plugin's references."""
    roots = {
        os.path.realpath(os.path.join(b, "qa-out")) for b in filter(None, (cwd, os.environ.get("CLAUDE_PROJECT_DIR")))
    }
    roots.add(os.path.realpath(Path(guard_paths.PLUGIN_QA_MEM).parent.parent / "references"))
    if tool not in ("Read", "ToolSearch", "TodoWrite", "SubagentHandback"):
        return f"the qa-reviewer only reads run files; {tool} is not one of its tools"
    if tool == "Read" and not all(
        any(guard_paths.under(guard_paths.real(p, cwd), r) for r in roots)
        and not guard_paths.is_secret_path(guard_paths.real(p, cwd))
        for p in guard_paths.tool_paths(tin)
    ):
        return "the qa-reviewer reads only <project>/qa-out/ run files and the plugin's references"


def check_files(tool, tin, cwd, explorer):
    """R3, R4, R5 for the file tools: the profile stays closed, the data folder unwritten, the config the explorer's."""
    for raw in guard_paths.tool_paths(tin):
        p = guard_paths.real(raw or ".", cwd)
        if any(
            map(
                guard_paths.in_profile,
                guard_paths.path_hits(os.path.join(raw or ".", str(tin.get("pattern") or "")), cwd)
                if tool == "Glob"
                else [p],
            )
        ) or (
            tool == "Grep"
            and str(tin.get("output_mode")).lower() == "content"
            and (guard_paths.in_area(p) or any(guard_paths.ancestor(p, r) for r in guard_paths.profiles()))
        ):
            return guard_files.PROFILE_REASON
        if tool in guard_paths.WRITE_TOOLS and guard_paths.under(p, guard_paths.DATA_DIR):
            return "the qa-sdet memory DB directory is written only by qa-mem"
        if (
            tool in guard_paths.WRITE_TOOLS
            and explorer
            and (p in guard_files.config_files({cwd}) or os.path.basename(p) == "cli.config.json")
        ):
            return guard_bash.CONFIG_REASON


def evaluate(data):
    """(deny reason or None, approve) for one tool call."""
    return verdict(data)[:2]


def verdict(data):
    """(deny reason or None, approve, blocker): the blocker is what kept a Bash call from being approved (Ctx.block)."""
    tool, tin, role = (
        guard_paths.clean(data.get("tool_name") or ""),
        data.get("tool_input"),
        guard_paths.agent_role(data),
    )
    tin, cwd = {} if tin is None else tin, guard_paths.cwd_of(data)
    if not isinstance(tin, dict):
        return "malformed tool input (not an object)", False, None
    if role == "qa-mapper" and (r := check_mapper(tool, tin, cwd)):
        return r, False, None
    if role in guard_paths.EXPLORERS and (r := check_explorer(tool, tin, cwd, role)):
        return r, False, None
    if role == "qa-reviewer" and (r := check_reviewer(tool, tin, cwd)):
        return r, False, None
    if tool == "Bash":
        ctx = guard_bash.Ctx(cwd, role in guard_paths.EXPLORERS, guard_paths.clean(tin.get("command") or ""))
        r = guard_bash.check(ctx.raw, cwd, ctx)
        return r, not r and ctx.approve and ctx.segments > 0, ctx.blocker
    if tool in guard_paths.WRITE_TOOLS | {"Read", "Grep", "Glob", "LS"}:
        return check_files(tool, tin, cwd, role in guard_paths.EXPLORERS), False, None
    return check_jira_mcp(tool), False, None


def decide(data):
    return evaluate(data)[0]


def main():
    try:
        data = json.load(sys.stdin)
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        return 0
    blocker = None
    try:
        reason, approve, blocker = verdict(data)
    except Exception as e:  # noqa: BLE001 - a crashing guard must not mean "allowed" for anything guarded (or the mapper)
        role, text = guard_paths.agent_role(data), json.dumps(data.get("tool_input"), ensure_ascii=False, default=str)
        guarded = guard_paths.mentions_guarded(text, role in guard_paths.EXPLORERS) or role == "qa-mapper"
        guard_log.log_event(
            data,
            "hook_error",
            {
                "hook": "pretooluse",
                "agent": role or "main",
                "error_type": type(e).__name__,
                "detail": " ".join(guard_log.mask(str(e)).split())[:300],
            },
        )
        reason, approve = (
            f"guard error ({type(e).__name__}) on a call that names a guarded tool or path" if guarded else None,
            False,
        )
    if reason or approve:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny" if reason else "allow",
                        "permissionDecisionReason": f"qa-sdet guard: {reason or 'checked'}",
                    }
                }
            )
        )
    if reason:
        guard_log.log_denial(data, reason)
    guard_log.trace_request(data, reason, approve, blocker)
    return 0


if __name__ == "__main__":
    sys.exit(main())
