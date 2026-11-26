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
shell, inline code, xargs, a command string handed to another program, a computed path) is denied: run it directly."""

import functools
import glob
import json
import math
import os
import re
import shlex
import stat
import sys
import tempfile
import time
from fnmatch import fnmatch
from itertools import islice, takewhile
from pathlib import Path

# ---------- 1. constants ----------
CONFIG = Path(os.environ.get("QA_SDET_CONFIG") or "~/.config/qa-sdet/config.json").expanduser()
DATA_DIR = os.path.realpath(Path(os.environ.get("QA_SDET_DATA") or "~/.local/share/qa-sdet").expanduser())
PLUGIN_QA_MEM = os.path.realpath(Path(__file__).resolve().parent.parent / "bin" / "qa-mem")
_PLUGIN_QAMEM_VAR_RE = re.compile(r"^\$\{?(?:CLAUDE_PLUGIN_ROOT|CLAUDE_SKILL_DIR)\}?(?:/\.\.)*/bin/qa-mem$")
_PY_SAFE_FLAG_RE = re.compile(r"-[ISsEOuBPqvbdix]+")
PUNCT, MAX_TEXT = ";&|()\n", 100_000
QUOTED_GT, QUOTED_LT = "\x01", "\x02"  # quoted '>' / '<' are data, not redirections
FD_DUP, SUBST = "\x03", "\x04"  # `2>&1` -> `2>` FD_DUP `1`: an fd, never a file; SUBST: a $(...), `...` or <(...)
WRAPPERS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-U", "-D", "-r", "-t", "-T"},
    "nohup": set(),
    "command": set(),
    "exec": {"-a"},
    "builtin": set(),
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "nice": {"-n", "--adjustment"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "time": {"-o", "-f"},
    "stdbuf": {"-i", "-o", "-e"},
    "xargs": {"-I", "-L", "-n", "-P", "-d", "-E", "-s", "-a"},
    "npx": {"-p", "--package", "-c", "--call"},
}  # -> value options
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "source", "."}
SHELL_KEYWORDS = set("if then elif else do while until ! { } fi done esac coproc".split())
INTERP_RE, GUARDED = (
    re.compile(r"python[\d.]*|node|perl|ruby|php|deno|bun|[gmn]?awk"),
    {"playwright-cli", "acli", "qa-mem"},
)
PKG_RE = re.compile(r"npm|npx|pnpm|pnpx|yarn|bunx|pipx?[\d.]*|uvx?|gem|cargo|go")  # they download code and run it
TEXT_TOOLS = set(
    "echo printf cat tee head tail less wc sort uniq cut tr grep egrep fgrep rg ag sed jq diff ls stat file du "
    "realpath readlink basename dirname which type whereis man git gh test [ [[ true false : cp mv ln rm mkdir "
    "touch chmod install".split()
)
PRINTERS, GREP_TOOLS = set("echo printf test [ [[ true :".split()), {"grep", "egrep", "fgrep", "rg", "ag"}
TREE_READERS = {"tar", "rsync", "rg", "ag", "tree", "git", "ln", "7z"}  # walk a folder: always, or with these flags
RECURSIVE = {"cp": "rRa", "scp": "r", "ls": "R", "du": "a", "grep": "rR", "egrep": "rR", "fgrep": "rR", "zip": "r"}
FIND_ACTIONS = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
PW_VALUE_OPTS = set(
    "-s --session --browser --config --device --profile --idle-timeout --filename --depth --domain --path "
    "--host --port --isolated-context --user-data-dir --type --filter --data --size --fps "
    "--status --body --content-type --header --remove-header --cursor --position --duration".split()
)  # route's and
# video-show-actions': else their values pass as the verb
NAV_VERBS, FAULT_VERBS = {"goto", "open", "tab-new"}, {"route", "network-state-set"}
MANUAL_WRITES = {
    "bug-status",
    "stale",
    "answer",
    "remember",
    "forget",
    "signal-ignore",
    "signal-unignore",
    "codemap-forget",
    "plugin-log",
}
PW_SOURCE_OPTS = ("--config", "--extension", "--cdp", "--endpoint")  # a config file or someone else's browser
_SECRET_HINT = "it prints session secrets; use requests / response-body, or one header: --raw eval \"fetch(url).then(r => r.headers.get('n'))\""  # noqa: S105 - a pattern of secret names, not a secret
PW_DENY = {
    "attach": "attach connects to an unverified browser",
    "run-code": "run-code runs Node with the page object and can reach the host; use eval (page sandbox)",
    "delete-data": "it deletes the browser profile and with it the QA sign-in; to sign out for good see README Privacy",
    "tracing-start": "a trace records every request with its headers and cookies into a file; use requests / response-body",
    "state-save": "it writes the session's cookies to a file; the login lives in the main profile (qa-mem browser-open)",
    "install": "it writes a workspace config that picks the browser (.playwright/cli.config.json) or skills into .claude; "
    "run it yourself (`! playwright-cli install`), or install a browser: playwright-cli install-browser <name>",
    **dict.fromkeys(
        (
            "cookie-list cookie-get localstorage-list localstorage-get sessionstorage-list sessionstorage-get "
            "request request-headers response-headers"
        ).split(),
        _SECRET_HINT,
    ),
}  # response: Set-Cookie
# denied to the explorer only (pages steer it); the user's own session may still run these
PW_DENY_EXPLORER = {
    "webmcp-call": "webmcp-call runs a tool the page registered; untrusted page text must not act in the QA session",
    "cookie-clear": "cookie-clear would sign the QA browser out; the login lives in the main profile (qa-mem browser-open)",
    "cookie-delete": "cookie-delete would sign the QA browser out; the login lives in the main profile (qa-mem browser-open)",
    "state-load": "state-load replaces the QA session's cookies and storage with a file's",
}
API_WRITE_METHODS, API_FORBIDDEN_HEADERS = (
    {"POST", "PUT", "PATCH", "DELETE"},
    {"authorization", "cookie", "proxy-authorization"},
)
EVAL_FETCH_RE = re.compile(r"\bfetch\s*\(")
EVAL_METHOD_RE = re.compile(
    r"(?i)\bmethod\W*[:=]\s*['\"`]\s*(post|put|patch|delete)\b"
)  # {method: 'POST'}, "method" : `put`, o.method = 'Patch'
EVAL_WRITE_RE = re.compile(
    r"\bXMLHttpRequest\b|\bsendBeacon\b"
)  # pattern-based: a method built at run time (['PO','ST'].join('')) is not seen
EVAL_WRITE_REASON = (
    "playwright-cli eval sending a write (fetch with POST/PUT/PATCH/DELETE, XMLHttpRequest, sendBeacon) blocked: "
    "qa-mem api-probe is the way to send one (with --api-writes, on qa-sdet- records); GET fetches and UI form submits are fine"
)
SECRET_JS = re.compile(
    r"[dD]ocument\s*(\.\s*cookie|\[\s*['\"`]cookie)|cookieStore|"  # also el.ownerDocument.cookie
    r"\.\s*cookie\b(?![-\w'\"`])|['\"`]cookie['\"`]|[{,]\s*cookie\s*[,}:]|"  # d=document; d.cookie / Reflect.get(document,'cookie') / ({cookie}) =>
    r"(local|session)Storage\s*(\.\s*getItem\b|\[)|\.\.\.\s*(local|session)Storage|"
    r"\bindexedDB\s*\.\s*open\b|"  # auth libraries (Firebase) keep tokens there; indexedDB.databases() names stay allowed
    r"(JSON\.stringify|Object\.(values|entries|assign|fromEntries))\s*\(\s*(local|session)Storage"
)
ACLI_READ, ACLI_AUTH_OK = {"view", "search", "list", "get", "help", "version", "whoami"}, {"login", "status"}
ACLI_WRITE = {
    "create",
    "edit",
    "delete",
    "transition",
    "assign",
    "update",
    "clone",
    "archive",
    "unarchive",
    "link",
    "unlink",
    "move",
    "add",
    "remove",
    "set",
    "import",
    "upload",
    "logout",
    "bulk",
}
REMOVERS = {"rm", "rmdir", "unlink", "mv", "shred", "truncate", "dd", "trash", "trash-put", "gio"}
WRITERS, INPLACE = {"cp", "tee", "install", "ln", "rsync"}, {"sed", "perl", "ruby", "gawk"}
NET_CLIS = set(
    "curl wget http https xh httpie nc ncat netcat telnet socat aria2c ssh scp sftp ftp lynx w3m links elinks".split()
)
HTTP_CLIENTS = {"curl", "wget", "http", "https", "xh", "httpie", "aria2c"}  # the explorer may fetch from allowedHosts
HTTP_REROUTE_OPTS = set(
    "-x --proxy --proxy1.0 --preproxy --socks4 --socks4a --socks5 --socks5-hostname "
    "--resolve --connect-to --doh-url --alt-svc -K --config --unix-socket "  # elsewhere,
    "--abstract-unix-socket --variable".split()
)  # or URLs / options from a file or variable the guard doesn't see
WGET_FILE_OPTS = {"-i", "--input-file", "-e", "--execute"}  # wget only (curl -i/-e: headers, referer)
HTTP_VALUE_OPTS = set(
    "-o -O -d -H -X -u -A -e -b -c -F -T -w -m -r -C -D -E -P -U -t -a --output --data --data-raw "
    "--data-binary --data-urlencode --header --request --user --user-agent --referer --cookie --cookie-jar "
    "--form --upload-file --write-out --max-time --connect-timeout --range --dump-header --cert --cacert --key "
    "--output-document --directory-prefix --post-data --post-file --password --tries --timeout".split()
)  # not hosts
SECRET_FILE_RE = re.compile(
    r"(?i)^\.env|\.(pem|key|p12|pfx|jks|keystore|crt|cer|kdbx)$|^secrets?\.(json|ya?ml|env|"
    r"properties|ini|toml|txt|xml)$|^credentials?\.(json|xml|ya?ml|ini|toml|txt)$|"
    r"^id_(rsa|ed25519|ecdsa|dsa)|^\.(npmrc|netrc|pgpass|pypirc|dockercfg)$|^appsettings\.\w+\.json$|"
    r"^secrets?\.(py|rb|php|go|java|kt|cs)$|^credentials?\.(py|rb|php|go)$|^wp-config\.php$"
)
SECRET_HOME_PATHS = (
    ".ssh .aws .azure .config/gcloud .kube .docker .gnupg .password-store .config/gh .git-credentials "
    ".claude/.credentials.json .local/share/keyrings .mozilla .config/google-chrome .config/chromium"
).split()
GUARDED_TOOL_RE = re.compile(r"(?<![.\w])playwright-cli|@playwright/cli|\bacli\b|(?<![\w.-])qa-mem(?![\w-])")
URL_RE = re.compile(r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,30}://[^\s'\"`)]+", re.I)
SCHEMELESS_URL_RE = re.compile(r"[\"'`(]\s*//([a-z0-9][a-z0-9.-]*)", re.I)  # '//host/path' in JS
HEREDOC_RE = re.compile(
    r"""(?<![<$(\d])<<(?!<)(-?)[ \t]*((?:[^\s<>;&|()'"\\]|\\.|'[^']*'|"[^"]*")+)"""
)  # <<'E"OF' ends at EOF
REDIR_OP_RE = re.compile(r"<<<?|<>|>>?|<")  # unquoted redirection operators (quoted ones are QUOTED_GT/LT)
PW_ENV_RE = re.compile(
    r"(PLAYWRIGHT_MCP_|PWTEST_)\w*=|\b(export|declare|typeset|readonly|local)\b[^;&|\n]*?\b(PLAYWRIGHT_MCP_|PWTEST_)"
)
PROXY_ENV_RE = re.compile(
    r"(?i)\b((https?|all|ftp)_proxy|CURL_HOME|(SYSTEM_)?WGETRC|XDG_CONFIG_HOME)\s*="
)  # or an rc file
HOSTISH_RE = re.compile(
    r"(?i)(localhost|\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-f:.]+\]|[\w-]+(\.[\w-]+)*\.[a-z]{2,})(:\d+)?(/\S*)?"
)
NOT_A_PATH_RE, GLOB_RE = re.compile(r"[\s()'\"`;|<>]"), re.compile(r"[*?\[]")  # code or text in an argument; a glob
MSG_OPT_RE = re.compile(r"(-m|--message|--body|--title)(=|$)")  # its value is text (a commit message), not a path
POS_PARAM_RE = re.compile(r"^\$(\{?[@*]\}?|\{?[1-9]\}?)$")  # "$@", $1: a function passing its arguments through
ONLY_VARS_RE = re.compile(r"(?:\s*(?:\$\{?\w+\}?|\x04))+\s*")  # wholly $VAR / $(...); one way to split spaces: linear
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
EXPLORERS = {"qa-explorer"}  # the agent that reads untrusted pages: R4 holds for it
EXPLORER_TOOLS = MAPPER_TOOLS | {"Bash"}  # no MCP tool: the session's GitHub, Slack, ... servers are not the explorer's
EXPLORER_FILES = {
    "bugs": r"[^/]+",
    "repro": r"[^/]+",
    "evidence": r"[^/]+",  # agents/qa-explorer.md "Files you write"
    "scratch": r"[^/]+",  # a snapshot to grep, a throwaway screenshot: the teardown empties it (ops.prune)
    ".data": r"run-[^/]+\.json|(requests|console)-[^/]+\.log|map-\d+\.png",
}
WRITE_TOOLS, PATH_KEYS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}, ("file_path", "notebook_path", "path")
ODD = "{} can't be checked; run the guarded command directly (spell out program, verb and paths)"
PROFILE_REASON = (
    "the qa-sdet browser profile folder holds the signed-in QA session (cookies); only qa-mem browser-open "
    "(playwright-cli open --profile <main profile>) may use it"
)
CONFIG_REASON = (
    "the qa-sdet config holds allowedHosts (cli.config.json picks the browser): read them, but leave them unchanged"
)
MARKER_REASON = (
    "the run marker (qa-out/.active) is the coordinator's: it sets fault injection and API writes; "
    "finish with what you have (step 9)"
)
MARKER_WRITES = {"dispatch", "marker"}  # qa-mem commands that (re)write or remove the run marker
CRAWLS = {"crawl", "discover"}  # whole browser passes run from Python, out of the guard's sight
DB_REASON = "{} on the qa-sdet memory DB directory is blocked (only qa-mem writes it)"
UPLOAD_REASON = (
    "an upload of a secret (key, .env, credentials, qa-sdet data or config) or a glob; upload a test file, by name"
)
OPEN_REASON = (
    "playwright-cli open starts a new browser without the QA sign-in and drops the request and console history "
    "step 9 saves; navigate with goto <full URL> (qa-mem browser-open starts the run's browser)"
)


# ---------- 2. hosts and paths ----------
def allowed_hosts():
    """config allowedHosts, read on every call; a broken config allows no navigation. localhost only when listed."""
    try:
        listed = json.loads(CONFIG.read_text()).get("allowedHosts", [])
    except (OSError, ValueError, AttributeError):
        listed = []
    hosts = (
        {h.lower().strip().rstrip(".").removeprefix("*.") for h in listed if isinstance(h, str) and h.strip()}
        if isinstance(listed, list)
        else set()
    )
    return hosts | {"localhost", "127.0.0.1", "::1"} if hosts & {"localhost", "127.0.0.1", "::1"} else hosts


def host_ok(url, hosts):
    if url.strip().lower() == "about:blank":
        return True  # reaches no host
    if (
        ("://" in url and url.split("://", 1)[0].strip().lower() not in ("http", "https", "ws", "wss"))
        or "\\" in url
        or "@" in re.split(r"[/?#]", url.split("://", 1)[-1])[0]
    ):
        return (
            False  # javascript://allowed.host/%0Acode runs code; data:, file: reach no QA host; user@host: don't guess
        )
    from urllib.parse import urlparse  # here: ~2 ms of start-up on every tool call, and only URL checks need it

    try:
        # not a vulnerability (S5332): a scheme added only to parse a bare host; nothing is fetched
        host = (urlparse(url if "://" in url else "http://" + url).hostname or "").lower().rstrip(".")  # NOSONAR
    except ValueError:
        return False  # %23, %2f in the host: browsers decode it first, so what urlparse reads isn't where they go
    return bool(host) and "%" not in host and any(host == h or host.endswith("." + h) for h in hosts)


def expand(path):
    """~, $VAR, and `${X:-default}` / `${X:=default}` (X's value, else the default); unset variables stay."""
    path = re.sub(r"\$\{(\w+):?[-=]([^{}]*)\}", lambda m: os.environ.get(m.group(1)) or m.group(2), path)
    return os.path.expandvars(os.path.expanduser(path))


def real(p, cwd):
    return os.path.realpath(os.path.join(cwd, expand(p)))


def under(p, root):
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def ancestor(p, root):  # p is a folder strictly above root
    return root.startswith(p.rstrip(os.sep) + os.sep)


def home():
    return os.path.realpath(os.path.expanduser("~"))


def profiles():
    """The browser profile folders (`main` holds the QA login): beside the DB, and under XDG_DATA_HOME if set."""
    return {
        os.path.realpath(p)
        for p in (
            os.path.join(DATA_DIR, "profiles"),
            os.environ.get("XDG_DATA_HOME")
            and os.path.join(os.path.expanduser(os.environ["XDG_DATA_HOME"]), "qa-sdet", "profiles"),
        )
        if p
    }


def main_profile():
    return {os.path.join(r, "main") for r in profiles()}


def in_profile(p):
    return any(under(p, r) for r in profiles())


def in_area(p):
    """The data or a profile folder, a path in one, or a folder between one and HOME (~/.local/share)."""
    roots = profiles() | {DATA_DIR}
    return any(under(p, r) for r in roots) or (p.startswith(home() + os.sep) and any(ancestor(p, r) for r in roots))


def path_hits(word, cwd):
    """Real paths a word names: itself, and if a glob near the data or a profile folder or in a qa-out folder, its matches (<= 500)."""
    t = re.sub(r"^file:([^?]*).*", r"\1", expand(word), flags=re.S)
    if not t or "\n" in t or len(t) > 4096:
        return []
    full = os.path.join(cwd, t)
    hits, roots = [os.path.realpath(full)], profiles() | {DATA_DIR}
    if GLOB_RE.search(t):
        fixed = os.path.realpath(os.path.dirname(GLOB_RE.split(full, 1)[0]) or "/")
        if (
            any(under(fixed, r) or ancestor(fixed, r) for r in roots) or os.path.basename(fixed) == "qa-out"
        ):  # the run marker
            # not a vulnerability (S2083): the guard expands a command's paths to check them against protected folders
            found = glob.iglob(full, recursive=any(under(fixed, r) for r in roots))  # NOSONAR
            hits += map(os.path.realpath, islice(found, 500))
            if "**" in t:  # recursive glob rooted above a profile: match inside profiles without walking all of HOME
                pat = os.path.normpath(full)
                for r in profiles():
                    if ancestor(fixed, r):
                        cands = [r, os.path.join(r, "main")]
                        cands += islice((os.path.join(dp, fn) for dp, _, fs in os.walk(r) for fn in fs), 500)
                        hits += [c for c in cands if fnmatch(c, pat)]
    return hits


def paths(words, cwds):
    """Every real path the words may name, also after `=` or `@` (`if=<path>`, `--file=<path>`, `-d @<path>`)."""
    return [
        p
        for a in words
        for v in {a, a.split("=", 1)[-1]}
        for w in {v, v.lstrip("@")}
        if w
        for c in cwds
        for p in path_hits(w, c)
    ]


def mentions(text, path):  # absolute, or as ~/…, $HOME/…, ${HOME}/…
    rel = path[len(home()) :] if path.startswith(home() + os.sep) else ""
    return path in text or (bool(rel) and any(f + rel in text for f in ("~", "$HOME", "${HOME}")))


def mentions_data_dir(text):
    return mentions(text, DATA_DIR)


def mentions_profile(text):
    return "qa-sdet/profiles" in text or any(mentions(text, r) for r in profiles())


def mentions_config(text):
    return (
        str(CONFIG) in text
        or "cli.config.json" in text
        or re.search(r"\.config/+qa-sdet|QA_SDET_CONFIG", text) is not None
    )


def mentions_guarded(text, explorer=False):
    """Does text name a guarded tool, the data or a profile folder, or (for the explorer) the config?"""
    return bool(
        GUARDED_TOOL_RE.search(text)
        or mentions_data_dir(text)
        or mentions_profile(text)
        or (explorer and mentions_config(text))
    )


def is_secret_path(p):
    """A key/secret file by name, or a path in a credentials, qa-sdet data or config folder."""
    roots = (
        {os.path.realpath(os.path.join(home(), s)) for s in SECRET_HOME_PATHS}
        | profiles()
        | {DATA_DIR, os.path.realpath(CONFIG.parent)}
    )
    return bool(SECRET_FILE_RE.search(os.path.basename(p))) or any(under(p, r) for r in roots)


# ---------- 3. run marker ----------
def read_marker(cwd):
    """(path, marker) of the active run, or (None, None): an expired or malformed marker is no run (no faults, no API
    writes)."""
    for base in dict.fromkeys(filter(None, (cwd, os.environ.get("CLAUDE_PROJECT_DIR")))):
        path = Path(base) / "qa-out" / ".active"
        try:
            marker = json.loads(path.read_text())
            expires = float(marker["expires"])
            ok = (
                marker.get("faults") in (None, True, False)
                and (
                    marker.get("api_writes") is None or isinstance(marker["api_writes"], bool)
                )  # 1 or "yes" would turn writes on
                and math.isfinite(expires)  # NaN/inf would never expire
                and expires <= time.time() + 2 * 86400  # epoch seconds: ms would never expire
            )
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            continue
        if ok and expires > time.time():
            return path, marker
    return None, None


# ---------- 4. text pre-processing ----------
def normalize(cmd):
    """Drop '#' comments, unquoted newline -> ';', mark quoted '<' '>' as data, `2>&1` -> `2>` FD_DUP `1`, `&>f`/`>|f` -> `>f`."""
    out, q, i = [], None, 0
    while i < len(cmd):
        c, nxt = cmd[i], cmd[i + 1 : i + 2]
        if c == "\\" and nxt and q != "'":
            c, i = cmd[i : i + 2], i + 1
        elif q or c in "'\"":
            c, q = {">": QUOTED_GT, "<": QUOTED_LT}.get(c, c) if q else c, None if c == q else q or c
        elif (
            c == "#"
            and (i == 0 or cmd[i - 1] in " \t\n;&|(")
            and not (out and len(out[-1]) == 2 and out[-1][0] == "\\")
        ):
            end = cmd.find("\n", i)  # the comment runs to the end of the line
            i, c = (end if end >= 0 else len(cmd)) - 1, ""
        elif c == "\n":
            c = ";"
        elif c in "<>" and nxt == "&":
            c, i = c + FD_DUP, i + 1
        elif (c == ">" and nxt == "|") or (c == "&" and nxt == ">" and (i == 0 or cmd[i - 1] != "&")):
            c, i = (">", i + 1) if c == ">" else ("", i)
        out.append(c)
        i += 1
    return "".join(out)


def match_paren(cmd, k):
    """Index of the ')' closing the '(' at cmd[k] (len(cmd) if unclosed); parens inside quotes close nothing."""
    depth, j, q = 0, k, None
    while j < len(cmd):
        c = cmd[j]
        if c == "\\" and q != "'":
            j += 1
        elif q or c in "'\"":
            q = None if c == q else q or c
        elif c in "()":
            depth += 1 if c == "(" else -1
            if depth == 0:
                return j
        j += 1
    return len(cmd)


def substitutions(cmd):
    """(bodies of $(...), <(...), >(...) and `...`, cmd with each replaced by a placeholder word).
    Single-quoted text is literal (JS template literals in `eval '...'` stay put); inside double quotes
    substitutions still run, as in the shell."""
    bodies, out, i, dq = [], [], 0, False
    while i < len(cmd):
        c = cmd[i]
        if c == "'" and not dq:  # literal up to the closing quote
            j = cmd.find("'", i + 1)
            j = len(cmd) - 1 if j == -1 else j
            out.append(cmd[i : j + 1])
            i = j + 1
        elif c == "\\" and i + 1 < len(cmd):
            out.append(cmd[i : i + 2])
            i += 2
        elif c == '"':
            dq = not dq
            out.append(c)
            i += 1
        elif c == "$" and cmd[i + 1 : i + 3] == "((":
            outer, inner = match_paren(cmd, i + 1), match_paren(cmd, i + 2)
            if inner + 1 == outer:  # `$((...))`: arithmetic, a number; nested $(...) in it still runs
                bodies += substitutions(cmd[i + 3 : inner])[0]
                out.append("ARITH")
            else:  # `$((cmd) )` is a command substitution holding a subshell
                bodies.append(cmd[i + 2 : outer])
                out.append(SUBST)
            i = outer + 1
        elif c in "$<>" and cmd[i + 1 : i + 2] == "(":
            j = match_paren(cmd, i + 1)
            bodies.append(cmd[i + 2 : j])
            out.append(SUBST)
            i = j + 1
        elif c == "`":
            j = cmd.find("`", i + 1)
            if j == -1:
                out.append(cmd[i:])
                break
            bodies.append(cmd[i + 1 : j])
            out.append(SUBST)
            i = j + 1
        else:
            out.append(c)
            i += 1
    return bodies, "".join(out)


def heredoc_end(word):
    return re.sub(r"""\\(.)|['"]""", lambda m: m.group(1) or "", word)


def split_heredocs(cmd):
    """(command text without heredoc bodies, [heredoc bodies]). A `<<EOF` inside quotes is text, not a heredoc;
    quote state carries across lines, like the shell."""
    out, docs, pending, q = [], [], [], None
    for line in cmd.split("\n"):
        if pending:
            strip_tabs, end, body = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) == end:
                docs.append("\n".join(body))
                pending.pop(0)
            else:
                body.append(line)
            continue
        out.append(line)
        i = 0
        while i < len(line):
            c = line[i]
            if q:
                if c == "\\" and q == '"':
                    i += 2
                    continue
                if c == q:
                    q = None
            elif c in "'\"":
                q = c
            elif c == "\\":
                i += 2
                continue
            elif c == "#" and (i == 0 or (line[i - 1] in " \t;&|(" and not (i >= 2 and line[i - 2] == "\\"))):
                break  # comment to end of line (not after an escaped space: `a\ #b` is one word)
            elif line.startswith("<<", i) and (m := HEREDOC_RE.match(line, i)):
                pending.append((m.group(1) == "-", heredoc_end(m.group(2)), []))
                i = m.end()
                continue
            i += 1
    docs += ["\n".join(body) for _, _, body in pending]  # unterminated: bash reads to EOF
    return "\n".join(out), docs


def tokens(cmd):
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=PUNCT)
    lex.whitespace_split, lex.commenters = True, ""
    return list(lex)


def split_redirects(seg):
    """(words, output targets, input targets, here-string texts, heredoc count). Fd duplications aren't files."""
    words, outs, ins, here, docs, i = [], [], [], [], 0, 0
    while i < len(seg):
        tok, i = seg[i], i + 1
        ops = list(REDIR_OP_RE.finditer(tok))
        if not ops:
            words.append(tok)
            continue
        lead = tok[: ops[0].start()]
        if lead and not lead.isdigit():  # `2>f` is an fd, `x>f` / `y=>f` keeps the word before `>`
            words.append(lead)
        for idx, m in enumerate(ops):
            kind = m.group(0)
            target = tok[m.end() : ops[idx + 1].start() if idx + 1 < len(ops) else len(tok)]
            if not target and idx + 1 == len(ops) and i < len(seg):
                target, i = seg[i], i + 1
            if kind == "<<":
                docs += 1
            elif kind == "<<<":
                here.append(unmark(target))
            elif target and not (target[:1] == FD_DUP and re.fullmatch(r"\d+-?|-", target[1:])):
                target = target.removeprefix(FD_DUP)  # `>&file` writes stdout and stderr to a file, not to an fd
                if kind == "<>":  # `<>file` opens it read-write
                    outs.append(target)
                    ins.append(target)
                else:
                    (ins if kind == "<" else outs).append(target)
    return words, outs, ins, here, docs


def unmark(text):
    """A quoted string a shell runs again (bash -c, eval): its '<' '>' are redirections there."""
    return text.replace(QUOTED_GT, ">").replace(QUOTED_LT, "<")


# ---------- 5. programs ----------
def prog_name(tok):
    """basename without `@version`; `@playwright/cli` runs playwright-cli."""
    return (
        "playwright-cli"
        if tok.startswith("@playwright/cli")
        else re.sub(r"@[^/]*$", "", os.path.basename(tok)).removesuffix(".js")
    )


def command_word(words):
    """(index of the real program or None, wrappers seen, lookup only (`command -v`), env -C folder)."""
    i, wrappers, env_dir, duration, lookup = 0, [], None, False, False
    while i < len(words):
        tok, w = words[i], wrappers[-1] if wrappers else None
        if re.match(r"[A-Za-z_]\w*\+?=", tok) or (tok in SHELL_KEYWORDS and not w):
            i += 1
        elif tok == "function" and not w:  # `function name { body`
            i += 2
        elif w and tok.startswith("-"):
            if w == "command" and re.fullmatch(r"-p*[vV]p*", tok):
                lookup = True
            if w == "env":
                if tok in ("-C", "--chdir") and i + 1 < len(words):
                    env_dir = words[i + 1]
                elif tok.startswith("--chdir="):
                    env_dir = tok.split("=", 1)[1]
                elif tok.startswith("-C") and len(tok) > 2:
                    env_dir = tok[2:]
            i += 2 if tok in WRAPPERS[w] else 1
        elif duration and re.fullmatch(r"[\d.]+[smhd]?", tok):
            i, duration = i + 1, False
        elif prog_name(tok) in WRAPPERS:
            wrappers.append(prog_name(tok))
            i, duration = i + 1, wrappers[-1] == "timeout"
        else:
            break
    return (i if i < len(words) else None), wrappers, lookup, env_dir


def qamem_args(name, rest, cwds=None, allow_var=True):
    """The arguments after qa-mem when this command runs it (bare qa-mem, or python3 [-I…] <plugin>/bin/qa-mem), else None."""
    if name == "qa-mem":
        return rest
    if not re.fullmatch(r"python[\d.]*", name):
        return None
    k = 0
    while k < len(rest) and _PY_SAFE_FLAG_RE.fullmatch(rest[k]):
        k += 1
    if k >= len(rest):
        return None
    script = rest[k]
    if any(real(script, c) == PLUGIN_QA_MEM for c in (cwds or (os.getcwd(),))) or (
        allow_var and _PLUGIN_QAMEM_VAR_RE.fullmatch(script) and "$" in expand(script)
    ):
        return rest[k + 1 :]
    return None


def dynamic(w):
    """A word the shell computes: an unset $VAR, ${V:-x}, $(...), `...`."""
    return SUBST in w or "$" in os.path.expandvars(w)


def opt_values(rest, *opts):
    """Values of these options, given as `--opt value` or `--opt=value` ("" when the value is missing)."""
    return [
        a.split("=", 1)[1] if "=" in a else (rest[k + 1 : k + 2] or [""])[0]
        for k, a in enumerate(rest)
        if a.split("=", 1)[0] in opts
    ]


def how_run(name, rest):
    """("c", text) for `sh -c`; ("code", None) inline, a module or awk's program; ("file", path); ("stdin", None)."""
    if name in ("source", "."):
        return ("file", rest[0]) if rest else (None, None)  # `.)` in a case pattern runs nothing
    shell, awk, c_text = name in SHELLS, "awk" in name, False
    for k, a in enumerate(rest):
        if k and rest[k - 1] in (("-o", "+o", "-O", "+O") if shell else ("-v", "-F", "-W", "-X")):
            continue  # an option's value
        if shell and re.fullmatch(r"-[a-zA-Z]*", a) and "c" in a:  # not -[a-zA-Z]*c[a-zA-Z]*: quadratic backtracking
            c_text = True  # the text is the first word after the options: `bash -c -x -- 'cmd'`
            continue
        if c_text:
            if a in ("--", "-"):
                return ("c", rest[k + 1]) if k + 1 < len(rest) else (None, None)
            if not a.startswith(("-", "+")):
                return "c", a
            continue
        if awk and a == "-f":
            return "file", (rest[k + 1 :] or [""])[0]
        if a in ("-", "/dev/stdin") or (shell and a == "-s"):
            return "stdin", None
        if not shell and (
            a in ("--version", "-V", "--help", "-h")
            or (not awk and re.fullmatch(r"-[A-Za-z]*[cemEpr]|--eval|--print", a))
        ):
            return "code", None
        if not a.startswith(("-", "+")):
            return ("code", None) if awk else ("file", a)
    return ("code", None) if awk else ("stdin", None)


# ---------- 6. playwright-cli, acli, qa-mem, network ----------
def pw_help(rest):
    """True when playwright-cli would only print help: -h/--help before `--`, not an option's value, not `--help false`."""
    skip = False
    for k, a in enumerate(rest):
        if a == "--":
            return False
        if not skip and a in ("--help", "-h") and rest[k + 1 : k + 2] != ["false"]:
            return True
        skip = not skip and a in PW_VALUE_OPTS
    return False


def pw_args(rest):
    """playwright-cli's positional words (the verb first), as it parses them."""
    out, skip, flag, end = [], False, False, False
    for a in rest:
        if end:
            out.append(a)
        elif skip:
            skip = False
        elif a == "--":
            end = True
        elif a.startswith("-"):
            skip, flag = a in PW_VALUE_OPTS, "=" not in a
        else:
            out += [] if flag and a in ("true", "false") else [a]  # the CLI reads `--raw true` as the option's value
            flag = False
    return (
        out[1:] if out and prog_name(out[0]) == "playwright-cli" else out
    )  # npx --package @playwright/cli playwright-cli


def pw_profile_values(rest):
    return opt_values(rest, "--profile", "--user-data-dir")


def pw_file_args(
    rest, verb, args
):  # paths it writes or reads: --filename/--path values, video-start/upload/drop/pdf paths
    return opt_values(rest, "--filename", "--path") + (
        args[1:] if verb in ("video-start", "upload", "drop", "state-load", "pdf") else []
    )


def check_pw(rest, ctx, cwds, outs):
    """R2, R3, R6 for one playwright-cli command; the files it writes go to outs (R4, R5 check them)."""
    if pw_help(rest):
        return None  # help text only
    ctx.runs_pw, args = True, pw_args(rest)
    verb, hosts = (args or [""])[0], allowed_hosts()
    if opt := next((o for o in PW_SOURCE_OPTS if any(a.split("=", 1)[0] == o for a in rest)), None):
        return f"playwright-cli {opt} picks another browser, profile or config file; open the browser with qa-mem browser-open [<url>]"
    if verb in PW_DENY:
        return f"playwright-cli {verb} blocked: {PW_DENY[verb]}"
    if verb in PW_DENY_EXPLORER and ctx.explorer:
        return f"playwright-cli {verb} blocked for the explorer: {PW_DENY_EXPLORER[verb]}"
    if verb == "open" and ctx.explorer and not pw_profile_values(rest):
        return OPEN_REASON
    if verb == "show" and any(
        h.strip("[]").lower() not in ("localhost", "127.0.0.1", "::1") for h in opt_values(rest, "--host")
    ):
        return "playwright-cli show --host would expose the logged-in QA browser to the network; keep it on localhost"
    if verb == "eval" and secret_js(" ".join(rest)):
        return "playwright-cli eval reading cookie or web storage values blocked; read key names: Object.keys(localStorage)"
    if (
        verb == "eval"
        and (js := " ".join(rest))
        and (EVAL_WRITE_RE.search(js) or (EVAL_FETCH_RE.search(js) and EVAL_METHOD_RE.search(js)))
    ):
        return EVAL_WRITE_REASON
    if any(
        verb != "open" or not v or dynamic(v) or any(real(v, c) not in main_profile() for c in cwds)
        for v in pw_profile_values(rest)
    ):
        return "playwright-cli --profile/--user-data-dir: only `open` with the qa-sdet main profile, spelled out (qa-mem browser-open)"
    sent = args[1:] + opt_values(rest, "--path") if verb in ("upload", "drop") else []
    if any(GLOB_RE.search(f) for f in sent) or any(map(is_secret_path, paths(sent, cwds))):
        return UPLOAD_REASON
    outs += pw_file_args(rest, verb, args)
    if bad := next(
        (t for t in args[1:] if verb in NAV_VERBS and not host_ok(t, hosts)), None
    ):  # unknown flags may shift the URL
        return f"playwright-cli {verb} to non-allowlisted host: {bad} (allowed: {sorted(hosts)})"
    if bad := next(
        (u for u in js_urls(" ".join(rest)) if verb in NAV_VERBS | {"eval"} and not host_ok(u, hosts)), None
    ):
        return f"playwright-cli {verb} references non-allowlisted URL: {bad}"
    if verb in FAULT_VERBS and not (ctx.marker and ctx.marker.get("faults") is True):
        return f"playwright-cli {verb} is fault injection; rerun the skill with --faults to allow it"


def js_urls(text):
    """Full URLs (not ones only compared) and `//host` references in page JS or arguments."""
    hosts = [h for h in SCHEMELESS_URL_RE.findall(text) if "." in h or h.lower() == "localhost"]  # not XPath //td
    return [m.group(0) for m in URL_RE.finditer(text) if not compared_only(text, m.start(), m.group(0))] + [
        # not a vulnerability (S5332): a scheme added only to parse a bare host; nothing is fetched
        "http://" + h  # NOSONAR
        for h in hosts
    ]


XML_NAMESPACES = (
    "http://www.w3.org/2000/svg",
    "http://www.w3.org/1999/xhtml",
    "http://www.w3.org/1999/xlink",
    "http://www.w3.org/2000/xmlns/",
    "http://www.w3.org/XML/1998/namespace",
    "http://www.w3.org/1998/Math/MathML",
)


_SELECTOR_CALL_RE = re.compile(r"(?:querySelector(?:All)?|matches|closest|\$\$?)\s*\(\s*(['\"`])")


def in_selector(js, pos):
    """Is js[pos] inside a CSS attribute selector a selector call is given (`querySelectorAll('a[href^="…`)? A URL in
    `[u = '…']` outside such a string is JS (an array holding an assignment), and fetching it is a fetch."""
    w = js[max(0, pos - 300) : pos]
    calls = list(_SELECTOR_CALL_RE.finditer(w))
    if not calls:
        return False
    call = calls[-1]  # the nearest selector call before the URL
    inside = w[call.end() :]  # still in the call's string: no closing quote, no ${…} code in a template
    return call.group(1) not in inside and "${" not in inside and bool(SELECTOR_ATTR_RE.search(inside))


SELECTOR_ATTR_RE = re.compile(r"\[\s*[\w:-]+\s*[~|^$*]?=\s*['\"]?$")


def compared_only(js, pos, url):
    """A URL used as data, not fetched: an XML namespace, a string comparison (`.startsWith('https://…')`, `=== '…'`),
    or a CSS attribute selector (`a[href^="https://docs.x/"]` matches, fetches nothing)."""
    return (
        url.startswith(XML_NAMESPACES)
        or re.search(
            r"(\.(startsWith|endsWith|includes|indexOf|lastIndexOf)\s*\(|[!=]==?)\s*['\"`]$",
            js[max(0, pos - 40) : pos],
        )
        is not None
        or in_selector(js, pos)
    )


_KEY_READ_RE = re.compile(r"(Object\.keys\(\s*(window\.)?|\bin\s+(window\.)?|typeof\s+(window\.)?)$")
_QUOTE_END_RE = re.compile(r"['\"`]$")
_TEXT_SEARCH_END_RE = re.compile(
    r"(\.(includes|indexOf|lastIndexOf|startsWith|endsWith|match|search|split)\s*\(|[!=]==?)\s*$"
)


def storage_value_read(js):
    """Any use of localStorage/sessionStorage except key names, .length and writes: returning the Storage
    object prints every value, and `localStorage.token` reads one."""
    for m in re.finditer(r"\b(local|session)Storage\b", js):
        # Look back at most 200 chars (pos/endpos keep \b honest at the window edge): a scan of the whole
        # prefix per match made long evals quadratic. More than 200 spaces before the word just counts as a read.
        lo, after = max(0, m.start() - 200), js[m.end() : m.end() + 200]
        if _KEY_READ_RE.search(js, lo, m.start()):
            continue  # key names / existence checks
        if re.match(r"\s*\.\s*(length|key|clear|setItem|removeItem)\b", after):
            continue
        if (
            _QUOTE_END_RE.search(js, lo, m.start())
            and re.match(r"['\"`]", after)
            and _TEXT_SEARCH_END_RE.search(js, max(0, m.start() - 41), m.start() - 1)
        ):
            continue  # the word searched for in text, e.g. innerText.includes('localStorage'); not Reflect.get / window['localStorage']
        return True
    return False


def secret_js(js):
    """Page JS that reads cookie or web-storage values. A length (`document.cookie.length`) or the word searched for
    in page text (`body.innerText.includes('cookie')`, `x === 'cookie'`) reads no value."""
    if re.search(r"(?<![\w$.])with\s*\(", js) and re.search(r"(?<![\w$])cookie\b", js):
        return True  # `with (document) cookie` reads it without naming document.cookie
    for m in SECRET_JS.finditer(js):
        if "ookie" in m.group(0) and re.match(r"\s*\.\s*length\b", js[m.end() : m.end() + 40]):
            continue
        if re.fullmatch(r"['\"`]cookie['\"`]", m.group(0)) and _TEXT_SEARCH_END_RE.search(
            js[max(0, m.start() - 40) : m.start()]
        ):
            continue
        return True
    return storage_value_read(js)


def is_acli_read(word):
    """view, list, ... and their hyphenated forms (list-sprints, get-transitions, list-workitems)."""
    return word in ACLI_READ or re.fullmatch(r"(list|get|view|search)-[a-z-]+", word) is not None


def acli_words(positional):
    """Positional words with entity nouns that look like verbs dropped: `workitem link list` -> the action is
    `list`; `link type` lists link types."""
    out = []
    for k, a in enumerate(positional):
        nxt = positional[k + 1] if k + 1 < len(positional) else ""
        if a == "link" and (is_acli_read(nxt) or nxt in ACLI_WRITE or nxt == "type"):
            continue
        out.append("list" if a == "type" and not nxt and positional[k - 1 : k] == ["link"] else a)
    return out


def acli_head(rest):
    """acli words up to and including the action (view, create, ...): the part that decides read vs write."""
    head = []
    for a in rest:
        if a.startswith("-"):
            break
        head.append(a)
        if (is_acli_read(a) or a in ACLI_WRITE) and acli_words(head)[-1:] == [a]:
            break
    return head


def check_acli(rest):
    positional = []
    for a in rest:
        if a.startswith("-"):
            break
        positional.append(a)
    # Help only when it isn't the value of a preceding flag (`--summary -h` would create the issue).
    if rest and rest[-1] in ("--help", "-h") and (len(rest) < 2 or not rest[-2].startswith("-")) and "--" not in rest:
        return None
    words = [a for k, a in enumerate(acli_words(positional)) if k == 0 or a not in ("help", "version")]
    action = next((a for a in words if is_acli_read(a) or a in ACLI_WRITE), None)
    if not positional and all(a in ("--version", "-v", "--help", "-h") for a in rest):  # `acli --version`
        return None
    if not positional:  # `acli --profile p jira workitem create …`: a flag before the verb hides it
        return "acli: put the command words first (acli jira workitem view KEY …); a flag before them can't be checked"
    if action and is_acli_read(action):  # reads; the first action word decides
        return None
    if action is None and "auth" in positional:  # never after a write action (`workitem create auth login`)
        nxt = positional[positional.index("auth") + 1 : positional.index("auth") + 2]
        if nxt and nxt[0] in ACLI_AUTH_OK:
            return None
    return "acli write command blocked: qa-sdet only reads Jira and drafts content; the user files it"


def check_qamem(args, ctx, cwds=()):
    """qa-mem browser-open and page-start: allowedHosts only (only '--' words are browser-open's options); qa-mem
    evidence and annotate: the image is one of the explorer's run files."""
    if ctx.explorer and args[:1] and args[0] in MANUAL_WRITES:
        return f"qa-mem {args[0]} is the user's call (a manual memory write); put what you saw in the run JSON's notes"
    if ctx.explorer and args[:1] and args[0] in MARKER_WRITES:
        return MARKER_REASON
    if ctx.explorer and args[:1] and args[0] in CRAWLS:
        return (
            f"qa-mem {args[0]} drives a whole browser pass the guard can't see; it is the coordinator's. Test your "
            "page with playwright-cli"
        )
    if ctx.explorer and args[:1] in (["evidence"], ["annotate"]):  # the image it writes: one of its run files
        png = next((a for a in args[1:] if not a.startswith("-")), "")
        if not png or "$" in png or not any(run_file(real(png, c), cwds) for c in cwds):
            return run_files_reason()
    if args[:1] == ["evidence"]:  # it runs playwright-cli
        ctx.runs_pw = True
        return None
    if args[:1] == ["api-probe"]:
        return check_api_probe(args[1:], ctx)
    if args[:1] not in (["browser-open"], ["page-start"]):
        return None
    hosts, pos = (
        allowed_hosts(),
        [a for k, a in enumerate(args[1:]) if not a.startswith("--") and args[k] != "--session"],
    )
    if bad := next((u for u in pos if not host_ok(u, hosts)), None):
        return f"qa-mem {args[0]} to non-allowlisted host: {bad} (allowed: {sorted(hosts)})"
    ctx.runs_pw = True


def check_api_probe(args, ctx):
    """qa-mem api-probe <METHOD> <url> [--json b] [--content-type t] [--header 'N: v']: allowedHosts only, no credential
    headers, writes only in a run with api_writes."""
    pos, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a.split("=", 1)[0] in ("--json", "--content-type", "--header"):
            skip = "=" not in a
        elif not a.startswith("--"):
            pos.append(a)
    if len(pos) != 2 or any(dynamic(w) for w in pos):
        return "qa-mem api-probe <METHOD> <url>: spell out the method and the URL (they can't be checked otherwise)"
    method, url, hosts = pos[0].upper(), pos[1], allowed_hosts()
    if not host_ok(url, hosts):
        return f"qa-mem api-probe to non-allowlisted host: {url} (allowed: {sorted(hosts)})"
    if bad := next(
        (h for h in opt_values(args, "--header") if h.split(":", 1)[0].strip().lower() in API_FORBIDDEN_HEADERS), None
    ):
        return (
            f"qa-mem api-probe header {bad.split(':', 1)[0].strip()} refused: the browser's own session authenticates the "
            "request; credentials are never sent by hand"
        )
    if method in API_WRITE_METHODS and not (ctx.marker and ctx.marker.get("api_writes") is True):
        return (
            f"qa-mem api-probe {method} writes data: only a run started with --api-writes may (and only on qa-sdet- records); "
            "GET, HEAD and OPTIONS are always allowed"
        )
    ctx.runs_pw = True


_HTTP_SHORT_VAL = {o[1] for o in HTTP_VALUE_OPTS if len(o) == 2 and o[0] == "-"}


def explorer_net(name, rest, raw):
    """Deny reason for a network tool the explorer runs, or None: an HTTP client may fetch from the allowed hosts
    (an API check), nothing else reaches out. shortcut: a proxy set in ~/.curlrc or ~/.wgetrc by an earlier command
    isn't seen; check for one in curl/wget calls if the explorer is ever found writing those files."""
    if name not in HTTP_CLIENTS:
        return (
            f"{name} reaches other machines outside the browser; the explorer fetches only from the QA app "
            "(the browser, or curl/wget to an allowed host)"
        )
    opts = HTTP_REROUTE_OPTS | (WGET_FILE_OPTS if name in ("wget", "aria2c") else set())
    bad_short = {o[1] for o in opts if len(o) == 2 and o[0] == "-"}  # glued/bundled short flags: -x, -sx, -sK, -qi
    reroute = PROXY_ENV_RE.search(raw) is not None
    for a in takewhile(lambda x: x != "--", rest):
        if a.startswith("--"):
            opt = a.split("=", 1)[0]
            if opt in opts or (name != "curl" and len(opt) > 3 and any(o.startswith(opt) for o in opts)):
                reroute = True
        elif a.startswith("-") and len(a) > 1:
            for c in a[1:]:
                if c in bad_short:
                    reroute = True
                    break
                if c in _HTTP_SHORT_VAL:
                    break  # rest of cluster is a value (e.g. -dx is --data x, not -x proxy)
    if reroute:
        return f"{name} with a proxy, a resolve override or an option file can send the request elsewhere; call the URL directly"
    hosts, urls, end = allowed_hosts(), URL_RE.findall(" ".join(rest)), False
    for k, a in enumerate(
        rest
    ):  # bare hosts/IPs too (`curl -s qa.example/api`, `curl --url=evil.example`, `169.254.169.254`)
        if not end and a == "--":
            end = True
            continue
        if not end and a.startswith("--url=") and not URL_RE.search(a):
            urls.append(a.split("=", 1)[1])
        elif (
            (end or not a.startswith("-"))
            and not (not end and k and rest[k - 1] in HTTP_VALUE_OPTS)
            and not URL_RE.search(a)
            and HOSTISH_RE.fullmatch(a)
        ):
            urls.append(a)
    if not urls:
        return f"{name} without a URL the guard can check; pass the full URL (https://<allowed host>/…)"
    for u in urls:
        if not host_ok(u, hosts):
            return f"{name} to non-allowlisted host: {u} (allowed: {sorted(hosts)}); the explorer fetches only from the QA app"
    return None


# ---------- 7. paths: the profile (R3), the data folder (R5), the config (R4) ----------
_GREP_SHORT_VAL = set("efmABCdD")
_GREP_SAFE_LONG = set(
    "--extended-regexp --fixed-strings --basic-regexp --perl-regexp --ignore-case --no-ignore-case "
    "--word-regexp --line-regexp --count --files-without-match --files-with-matches --only-matching "
    "--quiet --silent --no-messages --invert-match --line-number --with-filename --no-filename "
    "--text --initial-tab --null --null-data --byte-offset --line-buffered --version --help".split()
)
_RECURSIVE_LONG = {
    "cp": (("--recursive", 5), ("--archive", 5)),
    "ls": (("--recursive", 5),),
    "du": (("--all", 5),),
    "grep": (("--recursive", 5), ("--dereference-recursive", 5)),
    "egrep": (("--recursive", 5), ("--dereference-recursive", 5)),
    "fgrep": (("--recursive", 5), ("--dereference-recursive", 5)),
    "zip": (("--recurse-paths", 5),),
}


def _long_is(opt, full, min_len=5):
    """Match a GNU getopt_long unambiguous option prefix (`--reg` -> `--regexp`)."""
    return len(opt) >= min_len and full.startswith(opt)


def _grep_parse(rest):
    """(has_pat, pos, pat_files, recursive, unknown_long) for grep/rg/ag options, handling GNU prefixes, bundles and `--`."""
    has_pat, pos, pat_files, rec, unknown_long, end, pending = False, [], [], False, False, False, None
    for a in rest:
        if end:
            pos.append(a)
        elif pending is not None:
            if pending == "f":
                pat_files.append(a)
            elif pending == "d" and a.startswith("rec"):
                rec = True
            pending = None
        elif a == "--":
            end = True
        elif a.startswith("--"):
            opt, eq, val = a.partition("=")
            if _long_is(opt, "--regexp", 5) or _long_is(opt, "--file", 4):
                has_pat = True
                if _long_is(opt, "--file", 4):
                    if eq:
                        pat_files.append(val)
                    else:
                        pending = "f"
                elif not eq:
                    pending = "v"
            elif _long_is(opt, "--recursive", 5) or _long_is(opt, "--dereference-recursive", 5):
                rec = True
            elif _long_is(opt, "--directories", 5):
                if eq:
                    rec = rec or val.startswith("rec")
                else:
                    pending = "d"
            elif opt in _GREP_VAL_OPTS:
                if not eq:
                    pending = "v"
            elif opt not in _GREP_SAFE_LONG:
                unknown_long = True
        elif a.startswith("-") and len(a) > 1:
            for idx, c in enumerate(a[1:]):
                if c in _GREP_SHORT_VAL:
                    val = a[idx + 2 :]
                    if c == "e":
                        has_pat = True
                        if not val:
                            pending = "v"
                    elif c == "f":
                        has_pat = True
                        if val:
                            pat_files.append(val)
                        else:
                            pending = "f"
                    elif c == "d":
                        if val:
                            rec = rec or val.startswith("rec")
                        else:
                            pending = "d"
                    elif not val:
                        pending = "v"
                    break
                if c in "rR":
                    rec = True
        else:
            pos.append(a)
    return has_pat, pos, pat_files, rec, unknown_long


def _target_dirs(rest):
    """Every -t / --target-directory value (incl. glued -t/dir, bundled -ft/dir, and GNU --target prefixes)."""
    dirs, skip, end = [], False, False
    for a in rest:
        if end:
            break
        if skip:
            dirs.append(a)
            skip = False
        elif a == "--":
            end = True
        elif a.startswith("--"):
            opt, eq, val = a.partition("=")
            if _long_is(opt, "--target-directory", 5):
                if eq:
                    dirs.append(val)
                else:
                    skip = True
        elif a.startswith("-") and len(a) > 1:
            for idx, c in enumerate(a[1:]):
                if c == "t":
                    if val := a[idx + 2 :]:
                        dirs.append(val)
                    else:
                        skip = True
                    break
                if c in "SmogZ":
                    break  # another value-taking short option consumes the rest of the cluster
    return dirs


def tar_dirs(rest):
    """tar's -C / --directory folders (glued -Cdir, bundled -xzC dir, --dir= prefixes)."""
    dirs, skip = [], False
    for a in rest:
        if skip:
            dirs.append(a)
            skip = False
        elif a == "--":
            break
        elif a.startswith("--"):
            opt, eq, val = a.partition("=")
            if _long_is(opt, "--directory", 5):
                if eq:
                    dirs.append(val)
                else:
                    skip = True
        elif m := re.match(r"-[A-Za-z]*?C(.*)", a, re.S):  # the first C takes the rest, as getopt reads -xCaC/x
            if m.group(1):
                dirs.append(m.group(1))
            else:
                skip = True
    return dirs


def unpack_dirs(name, rest):
    """Folders an archiver unpacks into (tar x -C, unzip -d, 7z x -o), or None when it doesn't unpack."""
    first, pos0 = next((a for a in rest if a != "--"), ""), next((a for a in rest if not a.startswith("-")), "")
    old_tar = name in ("tar", "bsdtar") and bool(first) and not first.startswith("-") and "x" in first
    if not (
        name == "unzip"
        or old_tar
        or (
            name in ("tar", "bsdtar")
            and any(re.match(r"-[A-Za-z]*x", a) or _long_is(a, "--extract", 5) or a == "--get" for a in rest)
        )
        or (name in ("7z", "7za") and pos0 in ("x", "e"))
    ):
        return None
    if name == "unzip":
        dirs = opt_values(rest, "-d") + [a[2:] for a in rest if a.startswith("-d") and len(a) > 2]
    elif name.startswith("7z"):
        dirs = opt_values(rest, "-o") + [a[2:] for a in rest if a.startswith("-o") and len(a) > 2]
    else:
        dirs = tar_dirs(rest)
        if old_tar and "C" in first:
            dirs += [a for a in rest[rest.index(first) + 1 :] if not a.startswith("-")]
    return dirs or ["."]


def check_profile(name, rest, outs, ins, cwds, skip=()):
    """R3: only echo/printf/test name a path in a profile folder or run there; no folder walker (tar, cp -r, grep -r,
    git, ...) starts in or above the data folder in HOME. skip: the --profile values check_pw allowed."""
    if name in PRINTERS:
        return PROFILE_REASON if any(map(in_profile, cwds)) or any(map(in_profile, paths(outs + ins, cwds))) else None
    args = [a for a in rest if a not in skip and a.split("=", 1)[-1] not in skip]
    grep_rec = False
    if name in GREP_TOOLS:
        has_pat, pos, pat_files, grep_rec, _ = _grep_parse(args)
        if pos and not has_pat:
            args.remove(pos.pop(0))  # `grep -n "<profile path>" README.md`: a pattern, not a file
        args = args + pat_files
    else:
        pos = [a for a in args if not a.startswith("-")]
    udirs = unpack_dirs(name, rest)
    flags = RECURSIVE.get(name)
    walks = (
        (name in TREE_READERS and udirs is None)
        or grep_rec
        or (name == "find" and bool(FIND_ACTIONS & set(rest)))
        or (
            flags
            and any(
                re.match(rf"-[a-zA-Z]*[{flags}]", a)
                or any(_long_is(a, full, n) for full, n in _RECURSIVE_LONG.get(name, ()))
                for a in rest
            )
        )
    )
    dest = name in ("cp", "scp", "rsync", "ln") and not _target_dirs(rest)
    srcs = (args if name in ("git", "tar") else pos[1:] if name == "zip" else pos[:-1] if dest else pos) or ["."]
    src_cwds = (
        cwds | {real(d, c) for d in tar_dirs(rest) for c in cwds} if name in ("tar", "bsdtar") else cwds
    )  # `-C ~ .local`
    if (
        any(map(in_profile, cwds))
        or any(map(in_profile, paths(args + (udirs or []) + outs + ins, cwds)))
        or (walks and any(map(in_area, paths(srcs, src_cwds))))
    ):  # `tar c ~/.local/share/qa-sdet`, `grep -r x ~/.local`
        return PROFILE_REASON


_COPY_VAL_OPTS = set(
    "-S --suffix -t --target-directory -m --mode -o --owner -g --group -Z --context "
    "--backup-dir --compare-dest --copy-dest --link-dest -e --rsh --exclude --include --filter "
    "--chmod --chown --bwlimit --log-file".split()
)


def _pos_args(rest, val_opts):
    """Positional arguments of a command, skipping values of val_opts and honoring `--`."""
    out, skip, end = [], False, False
    for a in rest:
        if end:
            out.append(a)
        elif skip:
            skip = False
        elif a == "--":
            end = True
        elif a.startswith("-"):
            skip = a in val_opts
        else:
            out.append(a)
    return out


SED_WRITES = re.compile(r"(?:^|[\s;{}/!$\d])[wWe]\s*\S|/[gpiImM\d]*[we]\b")  # w/W/e commands, s///w and s///e
AWK_WRITES = re.compile(r"\bprintf?\b[^;{}\n]*>|\bsystem\s*\(|\|")  # print > file, a command run, a pipe


def filter_writes(name, rest):
    """Files a text filter writes besides stdout: sort/shuf -o, uniq's second operand, split's prefix, csplit -f; for a
    sed or awk program that can write a file or run a command, every word it names, its input files too (FILENAME)."""
    if name in ("sort", "shuf"):
        return opt_values(rest, "--output") + [
            m.group(1) or (rest[k + 1 : k + 2] or [""])[0]
            for k, a in enumerate(rest)
            if (m := re.fullmatch(r"-[a-zA-Z]*o(.*)", a, re.S))
        ]
    if name == "uniq":
        return _pos_args(rest, {"-f", "-s", "-w", "--skip-fields", "--skip-chars", "--check-chars"})[1:2]
    if name == "split":
        vals = {"-a", "-b", "-C", "-l", "-n", "-t", "--bytes", "--line-bytes", "--lines", "--number", "--separator"}
        return _pos_args(rest, vals | {"--suffix-length", "--additional-suffix", "--filter"})[1:2]
    if name == "csplit":
        return opt_values(rest, "-f", "--prefix") + [a[2:] for a in rest if re.fullmatch(r"-f.+", a, re.S)]
    words = [unmark(a) for a in rest]  # a quoted `>` is marked: here it is the program's own redirection
    if re.fullmatch(r"g?sed", name):  # its script: the -e values, else the first operand (the rest are files)
        prog = SED_WRITES
        code = opt_values(words, "-e", "--expression") or _pos_args(words, {"-f", "--file", "-l", "--line-length"})[:1]
    elif re.fullmatch(r"[gmn]?awk", name):
        prog, code = AWK_WRITES, _pos_args(words, {"-F", "-v", "-f", "-W", "--file", "--assign"})[:1]
    else:
        return []
    if any(prog.search(c) for c in code):
        return [w.strip("\"'{}();") for a in words for w in (a, *re.split(r"[\s>|]+", a))]
    return []


def changes(name, rest, outs, cwds, root):
    """R5/R4: does it change root (the data folder, a config file) or remove a folder above it? Reads and copies don't."""

    def hit(w, up=False):
        return any(under(p, root) or (up and ancestor(p, root)) for c in cwds for p in path_hits(w, c))

    opts, pos = [a for a in rest if a.startswith("-")], [a for a in rest if not a.startswith("-")]
    if any(hit(o) for o in outs) or any(hit(w) for w in filter_writes(name, rest) if w):
        return True
    if name in HTTP_CLIENTS and any(hit(w) for w in http_writes(rest, cwds)):
        return True
    if (dirs := unpack_dirs(name, rest)) is not None:  # into it, or over it
        return any(hit(d) or hit(os.path.join(d, os.path.basename(root))) for d in dirs)
    if name == "find":  # `find ~ -name '*.pyc' -delete` is fine, `find ~ -name '*.db' -delete` isn't
        names = {os.path.basename(root)} | (set(os.listdir(root)) if os.path.isdir(root) else set())
        pats = [(a, rest[k + 1]) for k, a in enumerate(rest[:-1]) if a in ("-name", "-iname")]
        up = not pats or any(
            fnmatch(f.lower(), p.lower()) if o == "-iname" else fnmatch(f, p) for o, p in pats for f in names
        )
        k = 0
        while k < len(rest) and (rest[k] in ("-H", "-L", "-P", "-D", "--") or re.fullmatch(r"-O\d*", rest[k])):
            k += 2 if rest[k] == "-D" else 1
        return bool(FIND_ACTIONS & set(rest)) and any(
            hit(a, up) for a in list(takewhile(lambda a: a[:1] not in "-(!", rest[k:])) or ["."]
        )
    tdir = _target_dirs(rest)
    cpos = _pos_args(rest, _COPY_VAL_OPTS) if name in WRITERS | {"mv"} else pos
    srcs, dests = (cpos, tdir) if tdir else (cpos[:-1], cpos[-1:])
    dests += [os.path.join(t, os.path.basename(s.rstrip("/"))) for t in dests for s in srcs]  # `mv x <folder>/`
    moves = name == "mv" or (name == "rsync" and any(a.startswith("--remove-source") for a in rest))
    mirror = name == "rsync" and any(a.startswith("--delete") for a in rest)  # deletes what the source lacks
    rec = name != "rm" or any(re.match(r"-[a-zA-Z]*[rR]", o) or _long_is(o, "--recursive", 5) for o in opts)
    return (
        (
            name in WRITERS | {"mv"}
            and (
                any(hit(t, mirror) for t in (pos if name == "tee" else dests))
                or (moves and any(hit(s, True) for s in srcs))
            )
        )
        or (
            name in REMOVERS - {"mv"}
            and any(hit(a, rec) for a in ([a[3:] for a in pos if a.startswith("of=")] if name == "dd" else pos))
        )
        or (
            name in INPLACE
            and any(re.match(r"-[a-zA-Z]*i", o) or o.startswith("--in-place") for o in opts)
            and any(hit(a) for a in pos)
        )  # sed -i / perl -pi rewrite the file in place
        or (name == "sqlite3" and "-readonly" not in takewhile(lambda a: a != "--", rest) and any(hit(a) for a in pos))
    )


def http_writes(rest, cwds):
    """Paths an HTTP client may write: any argument (also after `=`, or glued to a short option: -o<file>, -so <file>),
    and each URL's file name in the cwd or a folder argument (curl -O, wget, aria2c save under the remote name)."""
    words = [
        w
        for a in rest
        for w in {a, a.split("=", 1)[-1], *(a[k:] for k in range(2, len(a)) if re.match(r"-[^-]", a))}
        if w
    ]
    names = {os.path.basename(re.split(r"[?#]", w)[0]) for w in words if "/" in w} - {""}
    dirs = set(cwds) | {p for w in words for c in cwds if os.path.isdir(p := real(w, c))}
    return words + [os.path.join(d, n) for d in dirs for n in names]


def marker_files(cwds):
    """R6: the run marker the explorer runs under, as read_marker finds it (cwd or project, then qa-out/.active), and the
    hook's count of its calls beside it (the spin and failure nudges read it)."""
    bases = {b for b in (*cwds, os.environ.get("CLAUDE_PROJECT_DIR")) if b}
    names = (".active", ".active.progress")
    return {real(os.path.join(b, "qa-out", n), "/") for b in bases for n in names} | {
        real(os.path.join(b, n), "/") for b in bases for n in names if os.path.basename(os.path.normpath(b)) == "qa-out"
    }


def config_files(cwds):
    """R4: the qa-sdet config (allowedHosts) and the playwright-cli config files that would pick its browser."""
    return {os.path.realpath(CONFIG), real("~/.playwright/cli.config.json", "/")} | {
        real(".playwright/cli.config.json", c) for c in cwds
    }


# ---------- 8. odd forms ----------
INLINE_REASON = (
    "the explorer runs no inline code (python -c, node -e, perl -e, a pipe or heredoc into an interpreter): a "
    "page it read could have steered it, and the guard can't read what the code does (rewrite the run marker, "
    "reach another host); read files with Read or jq, and use qa-mem and playwright-cli for the rest"
)


def inline_code(name, rest, kind):
    """An interpreter (not awk) given its code inline or on stdin, as how_run reads it; a module (python -m json.tool) and
    --version/--help are not inline code."""
    if not INTERP_RE.fullmatch(name) or "awk" in name or kind not in ("code", "stdin"):
        return False
    for a in rest:
        if a in ("--version", "-V", "--help", "-h") or (name.startswith("python") and re.fullmatch(r"-[A-Za-z]*m", a)):
            return False
        if not a.startswith("-"):
            break
    return True


def odd_form(name, i, words, rest, wrappers, outs, ins, feeds, run, ctx, q=None, cwds=()):
    """Deny reason for a form the guard doesn't read (O1-O9, in a guarded call), or the explorer running a script file or inline code."""
    kind, val = run
    by_path = (
        i is not None
        and "/" in words[i]
        and not (
            any(real(words[i], c) == PLUGIN_QA_MEM for c in cwds) or re.match(r"/(usr|bin|sbin|opt|snap)/", words[i])
        )
    )
    if ctx.explorer and (kind == "file" or (kind == "stdin" and ins) or by_path):  # ./x.sh: may not exist yet
        return "the explorer runs no script files (a page it read could have steered it to write one); run the commands directly"
    if ctx.explorer and inline_code(name, rest, kind):
        return INLINE_REASON
    if ctx.explorer and (
        PKG_RE.fullmatch(name)
        or "npx" in wrappers
        or (
            re.fullmatch(r"python[\d.]*", name)  # npx is a wrapper
            and re.search(r"(^|\s)-m\s*(pip[\d.]*|pipx|uv|ensurepip)\b", " ".join(rest))
        )
    ):
        return (
            "the explorer installs and runs no packages (npm, npx, pip, ...: they download code from other hosts and run "
            "it); test through the browser, or curl to an allowed host"
        )
    if not ctx.guarded:
        return None
    pos = pw_args(rest)
    runs_pkg = name in ("bunx", "pnpx") or bool({"exec", "x", "dlx"} & set(rest))  # `npm i @playwright/cli` installs it
    hidden = lambda t: mentions_guarded(t, ctx.explorer) or (ctx.area and dynamic(t))
    forms = [
        (i is not None and dynamic(words[i]), "a program name from $VAR or $(...)"),  # O1
        (
            name == "playwright-cli"
            and not pw_help(rest)
            and (
                (pos and dynamic(pos[0]))
                or any(map(POS_PARAM_RE.match, rest))  # O2
                or (pos[:1] == ["eval"] and any(ONLY_VARS_RE.fullmatch(a) for a in pos[1:]))
            ),
            'a playwright-cli verb, eval text or arguments from $VAR, $(...) or "$@"',
        ),
        (name == "acli" and any(map(dynamic, acli_head(rest))), "acli command words from $VAR or $(...)"),
        (q and dynamic(q[0]), "a qa-mem subcommand from $VAR or $(...)"),
        (name == "alias" and GUARDED_TOOL_RE.search(" ".join(rest)), "an alias for a guarded tool"),
        (name == "eval" and any(map(dynamic, rest)), "eval of $VAR or $(...)"),  # O3
        (
            ("env" in wrappers and any(re.match(r"-[A-Za-z0-9]*S|--split-string", w) for w in words[:i]))
            or ("npx" in wrappers and any(w.split("=", 1)[0] in ("-c", "--call") for w in words[:i])),
            "a command string given to env -S or npx -c",
        ),  # O4
        (name in SHELLS and kind in ("file", "stdin"), f"{name} running a script file, a pipe, a heredoc or stdin"),
        (
            INTERP_RE.fullmatch(name) and q is None and kind == "file" and dynamic(val),
            f"{name} running a script from $VAR",
        ),
        (
            INTERP_RE.fullmatch(name)
            and q is None
            and mentions_guarded(  # O5: inline code; stdin: the whole call
                ctx.raw if kind == "stdin" else " ".join(rest + feeds), ctx.explorer
            ),
            f"{name} code that names a guarded tool or path",
        ),
        ("xargs" in wrappers and (name in GUARDED or q is not None or ctx.area), f"{name} run by xargs"),  # O6
        (
            name
            and name not in TEXT_TOOLS | SHELLS | GUARDED | {"eval", "alias"}
            and not INTERP_RE.fullmatch(name)
            and any(  # O7
                (
                    prog_name(a.split("=", 1)[-1]) in GUARDED
                    and (a[:1] != "@" or runs_pkg)
                    and k + 1 < len(rest)
                    and (rest[k + 1].startswith("-") or "/" not in rest[k + 1])
                )
                or (re.search(r"\s", a) and hidden(a))
                or prog_name(a) in SHELLS - {".", "source"}
                for k, a in enumerate(rest)
            ),
            f"a guarded tool or command string run by {name}",
        ),
        (
            name not in TEXT_TOOLS and any(map(hidden, feeds)),  # O8
            f"{name or 'a command'} reading a heredoc or here-string",
        ),
        (
            ctx.area
            and any(
                map(
                    dynamic,
                    outs
                    + ins
                    + (
                        []
                        if name in PRINTERS
                        else [
                            a
                            for k, a in enumerate(rest)
                            if not (
                                NOT_A_PATH_RE.search(a) or MSG_OPT_RE.match(a) or (k and MSG_OPT_RE.match(rest[k - 1]))
                            )
                        ]
                    ),
                )
            ),
            "a path from $VAR or $(...) in a call that names the qa-sdet data folder",
        ),
    ]  # O9
    return next((ODD.format(form) for hit, form in forms if hit), None)


# ---------- 9. Bash commands ----------
class Ctx:
    """One Bash call: its run marker, what it names (worked out once), whether it can be approved."""

    def __init__(self, cwd, explorer=False, raw=""):
        self.explorer, self.raw = explorer, raw
        self.marker = read_marker(cwd)[1]
        self.guarded = mentions_guarded(raw, explorer)
        self.area = mentions_data_dir(raw) or mentions_profile(raw) or (explorer and mentions_config(raw))
        self.runs_pw, self.approve, self.segments, self.blocker = False, True, 0, None

    def block(self, what):
        """No approval for this call. `what` (a program, or a shape) is kept from the first time only: it is what
        PostToolUse names to the explorer and the run's trace counts."""
        if self.approve:
            self.blocker = what
        self.approve = False


def check(cmd, cwd, ctx=None, depth=0, explorer=False):
    """Deny reason for a Bash command, or None."""
    ctx = ctx or Ctx(cwd, explorer, cmd)
    if len(cmd) > MAX_TEXT:
        ctx.block("its length")
        return "the command is too long to check; split it" if ctx.guarded else None
    if (reason := _check(cmd, cwd, ctx, depth)) or depth:
        return reason
    if ctx.runs_pw and PW_ENV_RE.search(cmd):
        return "PLAYWRIGHT_MCP_*/PWTEST_* variables change playwright-cli's browser or profile; use qa-mem browser-open without them"
    return None


def _check(cmd, cwd, ctx, depth, carried=()):
    if depth:
        ctx.block("a nested command")
    if depth > 4:
        return ODD.format("nesting deeper than 4 levels") if ctx.guarded else None
    cmd = re.sub(r"\\\n", "", cmd)  # line continuations: bash joins the lines
    cmd = cmd[:-1] if (len(cmd) - len(cmd.rstrip("\\"))) % 2 else cmd  # a lone trailing backslash: bash drops it
    cmd, docs = split_heredocs(cmd)
    docs += carried  # a substitution's heredoc bodies were split off the text around it
    bodies, cmd = substitutions(normalize(cmd))
    if docs or bodies:
        ctx.block("$(...) or a heredoc")
    for body in bodies:
        if r := _check(unmark(body), cwd, ctx, depth + 1, docs):
            return r
    try:
        toks = tokens(cmd)
    except ValueError:
        ctx.block("an unclosed quote")
        return ODD.format("text the shell can't parse (an unclosed quote)") if ctx.guarded else None
    cwds, stack, seg = Dirs({cwd}), [], []
    for tok in toks + [";"]:  # sentinel flushes the last segment
        if not tok or not set(tok) <= set(PUNCT):
            seg.append(tok)
            continue
        if tok not in (";", "&&", "||", "|") or (not seg and tok != ";"):
            ctx.block("a subshell or &")  # subshells, background jobs, a pipe with nothing before it
        if seg:
            ctx.segments += 1
            if r := check_segment(seg, cwds, cwd, ctx, depth, docs):
                return r
            seg = []
        for ch in tok:  # subshells: a cd inside ( ) doesn't leak out
            if ch == "(":
                stack.append(cwds.snapshot())
            elif ch == ")" and stack:
                cwds = stack.pop()


_HEAD_VAL_OPTS = {"-n", "-c", "--lines", "--bytes", "--sleep-interval", "--pid"}
_GREP_VAL_OPTS = set(
    "-e --regexp -f --file -m --max-count -A -B -C --after-context --before-context --context "
    "--include --exclude --exclude-dir --label --binary-files --devices --directories "
    "--color --colour -d -D".split()
)
_HEAD_SAFE_LONG = {"--quiet", "--silent", "--verbose", "--zero-terminated"}


def _run_roots(cwd, bases):
    """The folders an approved filter may read: qa-out/ and the snapshots in .playwright-cli/."""
    return bases | {
        os.path.realpath(os.path.join(b, ".playwright-cli")) for b in (cwd, os.environ.get("CLAUDE_PROJECT_DIR")) if b
    }


# R8, explorer only: the text tools explorers chain after a browser command. Per tool: the short options that take no
# value, the short options whose value is glued on or the next word, and the long options. An option outside these
# could write a file or run a program (sort -o, sort --compress-program, wc --files0-from): no approval then.
_READ_ONLY = {
    "cat": ("AbEensTtuv", "", set()),
    "wc": ("clLmw", "", {"--bytes", "--chars", "--lines", "--words", "--max-line-length"}),
    "cut": ("nsz", "bcdf", {"--complement", "--only-delimited", "--output-delimiter"}),
    "sort": ("bdfghiMnRrsuVz", "kt", {"--reverse", "--numeric-sort", "--unique", "--ignore-case", "--stable"}),
    "uniq": ("cdiuz", "fsw", {"--count", "--repeated", "--unique", "--ignore-case"}),
    "ls": ("1AaCcdFhklmnopqrSstUuvXx", "", set()),
}
_PLAIN_RE = re.compile(rf"[^$`{SUBST}]*")  # a word the shell passes on as written, quoted or not
# a sed regex: one line, and a `$` only as an anchor (in double quotes `$VAR` would be the shell's)
_SED_RE = r"(?:[^/\\$\n]|\\.|\$(?![\w{(]))*"
_SED_ADDR = rf"(?:\d+|\$|/{_SED_RE}/)"
_SED_PRINT = re.compile(rf"{_SED_ADDR}(?:,{_SED_ADDR})?p")  # 1,40p  5p  /dialog/,$p
_SED_SUBST = re.compile(rf"s/{_SED_RE}/{_SED_RE}/[gIip\d]*")  # s/a/b/g: no w (a file) or e (a command) flag
_AWK_READS = re.compile(r"getline|ENVIRON|ARGV|ARGC|PROCINFO|close|@")  # other files, the environment, loaded code


def _file_operands(name, rest):
    """The operands of a _READ_ONLY tool (`-`, stdin, is one), or None for an option the table doesn't list."""
    flags, valued, longs = _READ_ONLY[name]
    files, skip, end = [], False, False
    for a in rest:
        if skip:
            skip = False
        elif end or not a.startswith("-") or a == "-":
            files.append(a)
        elif a == "--":
            end = True
        elif a.startswith("--"):
            if a.split("=", 1)[0] not in longs:
                return None
        elif a[1] in valued:  # -c1-200, -k2,2n, -d ' '
            skip = len(a) == 2
        elif not set(a[1:]) <= set(flags):
            return None
    return None if skip else files


def _read_only(name, rest, cwd, bases):
    """A text tool in a form that reads stdin or a file under qa-out/ or .playwright-cli/ and writes only stdout."""
    roots = _run_roots(cwd, bases)
    in_roots = lambda f, strict=True: (
        bool(_PLAIN_RE.fullmatch(f))
        and not GLOB_RE.search(f)
        and any(real(f, cwd).startswith(b + os.sep) or (not strict and real(f, cwd) == b) for b in roots)
    )
    plain = lambda words: all(_PLAIN_RE.fullmatch(a) for a in words)
    if name in _READ_ONLY:
        files = _file_operands(name, rest)
        if files is None or not plain(rest) or (name == "uniq" and len(files) > 1):  # uniq IN OUT writes OUT
            return False
        if name == "ls":  # a folder of the run, never the project or a walk down the tree (-R is not in the table)
            return bool(files) and all(in_roots(f, strict=False) for f in files)
        return all(f == "-" or in_roots(f) for f in files)
    if name == "tr":  # stdin to stdout, whatever its sets say
        return plain(rest)
    if name == "echo":
        return plain(rest) and not any(GLOB_RE.search(a) for a in rest)
    if name == "date":  # the time, in a format: -f, -r and -s read a file or set the clock
        return plain(rest) and all(a in ("-u", "--utc", "--universal") or a.startswith("+") for a in rest)
    if name not in ("sed", "awk"):
        return False
    valued, flags = (("-e",), r"-[nEr]+") if name == "sed" else (("-F", "-v"), r"-F.+")
    code, files, k = [], [], 0  # the script: sed's -e value, else the first operand; the operands after it are files
    while k < len(rest):
        a = rest[k]
        if a in valued and k + 1 < len(rest):
            k += 1
            if name == "sed":
                code.append(rest[k])
            elif not plain([rest[k]]):
                return False
        elif re.fullmatch(flags, a) and plain([a]):
            pass
        elif a.startswith("-"):
            return False  # -i, -f, --file: a file written in place, or code the guard hasn't read
        elif code or (name == "sed" and "-e" in rest):
            files.append(a)
        else:
            code.append(a)
        k += 1
    if len(code) != 1 or len(files) > 1 or not all(map(in_roots, files)):
        return False
    text = unmark(code[0])
    if name == "sed":
        return bool(_SED_PRINT.fullmatch(text) or _SED_SUBST.fullmatch(text))
    return (  # awk: `$1` and `$NF` are fields; any other `$` could be the shell's inside double quotes
        plain([text.replace("$", "")])
        and not re.search(r"\$(?!\d|NF\b)", text)
        and not AWK_WRITES.search(text)
        and not _AWK_READS.search(text)
    )


def _filter_ok(name, rest, cwd, bases):
    """head/tail/grep auto-approve only as stdin filters, or reading files under qa-out/ or .playwright-cli/."""
    roots = _run_roots(cwd, bases)
    if name in ("head", "tail"):
        if any(
            a.startswith("--") and a.split("=", 1)[0] not in _HEAD_VAL_OPTS | _HEAD_SAFE_LONG
            for a in takewhile(lambda x: x != "--", rest)
        ):
            return False
        files = _pos_args(rest, _HEAD_VAL_OPTS)
    else:
        has_pat, pos, pat_files, rec, unknown_long = _grep_parse(rest)
        if unknown_long:
            return False
        files = (pos if has_pat else pos[1:]) + pat_files
        if not files and rec:
            return False  # `grep -r` / `-d recurse` with no path walks the whole cwd
    return all(
        "$" not in f and not GLOB_RE.search(f) and any(real(f, cwd).startswith(b + os.sep) for b in roots)
        for f in files
    )


def _acli_approvable(rest):
    """Strictly read-only acli calls that never mutate auth state or open a browser (`--web` / `-w`)."""
    if any(a in ("-w", "--web") or a.startswith("--web=") for a in rest):
        return False
    pos = list(takewhile(lambda a: not a.startswith("-"), rest))
    if not pos and rest and all(a in ("--version", "-v", "--help", "-h") for a in rest):
        return True
    return (
        pos[:3] == ["jira", "auth", "status"]
        or pos[:3] in (["jira", "workitem", "view"], ["jira", "workitem", "search"])
        or pos[:4] == ["jira", "workitem", "comment", "list"]
    )


def approvable(
    i, name, rest, outs, ins, feeds, cwd, explorer=False
):  # R8: playwright-cli, qa-mem, read-only acli, head/tail/grep, sleep <=30; for the explorer also _read_only
    bases = {os.path.realpath(os.path.join(b, "qa-out")) for b in (cwd, os.environ.get("CLAUDE_PROJECT_DIR")) if b}
    if (
        i != 0
        or ins
        or feeds
        or any(
            o not in ("/dev/null", "/dev/stdout", "/dev/stderr")
            and ("$" in o or not any(real(o, cwd).startswith(b + os.sep) for b in bases))
            for o in map(unmark, outs)
        )
    ):
        return False
    q = qamem_args(name, rest, {cwd}, allow_var=False)
    return (
        name == "playwright-cli"
        or (name == "acli" and _acli_approvable(rest))
        or (name in ("head", "tail", "grep") and _filter_ok(name, rest, cwd, bases))
        or (
            name == "sleep"
            and len(rest) == 1
            and bool(re.fullmatch(r"\d+(?:\.\d+)?", rest[0]))
            and float(rest[0]) <= 30
        )
        or (q is not None and not (q[:1] == ["setup-permissions"] and "--apply" in q))
        or (  # never with a file redirect: `echo … > run-x.json` would be a write the guard hasn't read
            explorer
            and all(unmark(o) in ("/dev/null", "/dev/stdout", "/dev/stderr") for o in outs)
            and _read_only(name, rest, cwd, bases)
        )
    )


class Dirs(set):
    """The folders a call's later commands may run in, and the ones `cd -` returns to after a cd (None: unknown, as
    the shell's OLDPWD before this call). Without it `cd a; cd -` grew the set, and every path in it, pair by pair."""

    old: "set[str] | None" = None

    def snapshot(self):
        """A copy for a subshell's `( … )`: a cd inside it doesn't leak out."""
        d = Dirs(self)
        d.old = self.old
        return d


def check_segment(seg, cwds, start, ctx, depth, docs):
    """Check one simple command. Mutates cwds on cd."""
    words, outs, ins, here, ndocs = split_redirects(seg)
    redirects = list(outs)  # check_pw adds the files playwright-cli reads and writes to outs
    feeds = here + (list(docs) if ndocs else [])  # what it reads on stdin from here-strings and heredocs
    if words[:1] in (["case"], ["for"], ["select"]) and not (outs or ins or feeds):
        ctx.block(words[0])
        return None  # a list of words (a $(...) in it was checked as its own command)
    i, wrappers, lookup, env_dir = command_word(words)
    name, rest = (prog_name(words[i]), words[i + 1 :]) if i is not None else ("", [])
    local = {real(env_dir, c) for c in cwds} if env_dir else cwds
    q = qamem_args(name, rest, local)
    run = how_run(name, rest) if name in SHELLS or (INTERP_RE.fullmatch(name) and q is None) else (None, None)
    if (r := odd_form(name, i, words, rest, wrappers, outs, ins, feeds, run, ctx, q, local)) or lookup:
        ctx.block(name or "its form")
        return r  # `command -v x` runs nothing
    if name in ("cd", "pushd", "popd"):  # where the later commands run
        target = next((a for a in rest if a not in ("-L", "-P", "-e", "-@", "--")), "~")
        old = getattr(cwds, "old", None)  # where `cd -` goes, once a cd in this call set it
        new = (
            old
            if target == "-" and old is not None
            else {start} | (cwds if target == "-" else set())
            if target == "-" or name == "popd"
            else {real(target, c) for c in cwds}
        )
        here = {os.path.realpath(start)}  # the explorer's `cd <where the call already runs> && …` changes nothing
        if not (ctx.explorer and i == 0 and name == "cd" and target != "-" and new == here and set(cwds) == {start}):
            ctx.block(name)
        if isinstance(cwds, Dirs):
            cwds.old = set(cwds)
        return cwds.clear() or cwds.update(new)
    here_cwd = next(iter(local))  # `bash -c '…'` and `eval '…'`: their text is a command
    r = (
        _check(unmark(run[1]), here_cwd, ctx, depth + 1, docs)
        if run[0] == "c"
        else _check(unmark(" ".join(rest)), here_cwd, ctx, depth + 1, docs)
        if name == "eval"
        else check_pw(rest, ctx, local, outs)
        if name == "playwright-cli"
        else check_acli(rest)
        if name == "acli"
        else check_qamem(q, ctx, local)
        if q is not None
        else None
    ) or (explorer_net(name, rest, ctx.raw) if ctx.explorer and name in NET_CLIS else None)
    if not approvable(i, name, rest, outs, ins, feeds or ndocs, start, ctx.explorer):
        ctx.block(name if i == 0 and name else "a prefix or wrapper")
    return (
        r
        or check_profile(name, rest, outs, ins, local, pw_profile_values(rest) if name == "playwright-cli" else ())
        or (DB_REASON.format(name or "a redirection") if changes(name, rest, outs, local, DATA_DIR) else None)
        or (
            CONFIG_REASON
            if ctx.explorer and any(changes(name, rest, outs, local, f) for f in config_files(local))
            else None
        )
        or (
            MARKER_REASON
            if ctx.explorer and any(changes(name, rest, outs, local, f) for f in marker_files(local))
            else None
        )
        or (
            run_files_reason()
            if ctx.explorer
            and any(
                not explorer_out_ok(o, local)
                for o in redirects + (pw_written(rest) if name == "playwright-cli" else [])
            )
            else None
        )
    )


def pw_written(rest):
    """The files a playwright-cli command writes (not the ones upload, drop or state-load read)."""
    args = pw_args(rest)
    return opt_values(rest, "--filename") + (args[1:] if (args or [""])[0] in ("video-start", "pdf") else [])


def explorer_out_ok(out, cwds):
    """A redirection or playwright-cli file of the explorer: a /dev stream or one of its run files."""
    o = unmark(out)
    return o in ("/dev/null", "/dev/stdout", "/dev/stderr") or (
        "$" not in o and all(run_file(real(o, c), cwds) for c in cwds)
    )


# ---------- 10. other tools, the hook ----------
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
        for t in (real(p, cwd) for p in tool_paths(tin))
    ):
        return "the qa-mapper writes only <project>/qa-out/init/code-map-<area>[-<n>].json"
    if tool in ("Read", "Grep", "Glob", "LS"):
        extra = (
            clean(tin.get("pattern") or "")
            if tool == "Glob"
            else clean(tin.get("glob") or "")
            if tool == "Grep"
            else ""
        )
        if any(
            is_secret_path(real(p or ".", cwd)) or (extra and is_secret_path(real(os.path.join(p or ".", extra), cwd)))
            for p in tool_paths(tin)
        ):
            return "the qa-mapper reads source only; secret files (.env, keys, credentials, qa-sdet data, config) stay closed: write [secret]"


def run_file(p, cwds):
    """R4: p (a real path) is one of the explorer's run files: <project>/qa-out/<run>/{bugs,repro,evidence}/<file> or its
    .data/run-*.json, requests-/console-*.log, map-<n>.png (never REPORT.md, plan.json, the marker or source)."""
    roots = {os.path.realpath(os.path.join(b, "qa-out")) for b in (*cwds, os.environ.get("CLAUDE_PROJECT_DIR")) if b}
    sub = os.path.dirname(p)
    name = EXPLORER_FILES.get(os.path.basename(sub))
    return bool(name and re.fullmatch(name, os.path.basename(p)) and os.path.dirname(os.path.dirname(sub)) in roots)


def run_files_reason(role="explorer"):
    return (
        f"the {role} writes only <project>/qa-out/<run>/{{bugs,repro,evidence}}/<file> and its .data/ run files; "
        "a throwaway file (a snapshot to grep, a screenshot to look at) goes in <run>/scratch/"
    )


def check_explorer(tool, tin, cwd, role="qa-explorer"):
    """R4: the explorer's tools only, and Write only to its run files (run_file)."""
    if tool not in EXPLORER_TOOLS:
        return f"the {role} works through Bash (playwright-cli, qa-mem); {tool} is not one of its tools"
    if tool == "Write" and any(not run_file(real(p, cwd), {cwd}) for p in tool_paths(tin)):
        return run_files_reason(role)


def check_reviewer(tool, tin, cwd):
    """The qa-reviewer (a critic of untrusted drafts) only Reads: the project's qa-out run files and the plugin's references."""
    roots = {
        os.path.realpath(os.path.join(b, "qa-out")) for b in filter(None, (cwd, os.environ.get("CLAUDE_PROJECT_DIR")))
    }
    roots.add(os.path.realpath(Path(PLUGIN_QA_MEM).parent.parent / "references"))
    if tool not in ("Read", "ToolSearch", "TodoWrite", "SubagentHandback"):
        return f"the qa-reviewer only reads run files; {tool} is not one of its tools"
    if tool == "Read" and not all(
        any(under(real(p, cwd), r) for r in roots) and not is_secret_path(real(p, cwd)) for p in tool_paths(tin)
    ):
        return "the qa-reviewer reads only <project>/qa-out/ run files and the plugin's references"


def check_files(tool, tin, cwd, explorer):
    """R3, R4, R5 for the file tools: the profile stays closed, the data folder unwritten, the config the explorer's."""
    for raw in tool_paths(tin):
        p = real(raw or ".", cwd)
        if any(
            map(
                in_profile,
                path_hits(os.path.join(raw or ".", str(tin.get("pattern") or "")), cwd) if tool == "Glob" else [p],
            )
        ) or (
            tool == "Grep"
            and str(tin.get("output_mode")).lower() == "content"
            and (in_area(p) or any(ancestor(p, r) for r in profiles()))
        ):
            return PROFILE_REASON
        if tool in WRITE_TOOLS and under(p, DATA_DIR):
            return "the qa-sdet memory DB directory is written only by qa-mem"
        if tool in WRITE_TOOLS and explorer and (p in config_files({cwd}) or os.path.basename(p) == "cli.config.json"):
            return CONFIG_REASON


def clean(v):
    """A string from the input without NULs and lone surrogates: no path holds them, and os.path raises on them."""
    return str(v).encode("utf-8", "replace").decode("utf-8").replace("\0", "")


def tool_paths(tin):
    """Every path field present: a decoy `file_path` beside the `path` Grep uses must not hide the real target."""
    return [clean(tin[k]) for k in PATH_KEYS if tin.get(k)] or [""]


def agent_role(data):
    """qa-explorer / qa-mapper / "" from agent_type, tolerant of case and stray spaces ("QA-SDET:qa-explorer ")."""
    return str(data.get("agent_type") or "").strip().lower().split(":")[-1].strip()


def _cwd(data):
    if data.get("cwd"):
        return clean(data["cwd"])
    try:
        return clean(os.getcwd())
    except OSError:
        return "/"


def evaluate(data):
    """(deny reason or None, approve) for one tool call."""
    tool, tin, role = clean(data.get("tool_name") or ""), data.get("tool_input"), agent_role(data)
    tin, cwd = {} if tin is None else tin, _cwd(data)
    if not isinstance(tin, dict):
        return "malformed tool input (not an object)", False
    if role == "qa-mapper" and (r := check_mapper(tool, tin, cwd)):
        return r, False
    if role in EXPLORERS and (r := check_explorer(tool, tin, cwd, role)):
        return r, False
    if role == "qa-reviewer" and (r := check_reviewer(tool, tin, cwd)):
        return r, False
    if tool == "Bash":
        ctx = Ctx(cwd, role in EXPLORERS, clean(tin.get("command") or ""))
        r = check(ctx.raw, cwd, ctx)
        return r, not r and ctx.approve and ctx.segments > 0
    if tool in WRITE_TOOLS | {"Read", "Grep", "Glob", "LS"}:
        return check_files(tool, tin, cwd, role in EXPLORERS), False
    return check_jira_mcp(tool), False


def decide(data):
    return evaluate(data)[0]


_MASKS = [
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(\S+\s+)?[^\s'\"]+"), r"\1[masked]"),
    (re.compile(r"(?i)\b((?:set-)?cookie|x-api-key|x-auth-token)(\s*:\s*)[^'\"\n]+"), r"\1\2[masked]"),  # header values
    (re.compile(r"(?i)\b(bearer|basic)\s+[\w.~+/=-]+"), r"\1 [masked]"),
    # anywhere in a name (JIRA_API_TOKEN=, accessToken:), also as a JSON key ("password": "…", escaped in a dumped string)
    (
        re.compile(r"(?i)(token|password|passwd|pwd|secret|api[_-]?key)(\\?['\"]?\s*[=:]\s*\\?['\"]?)[^\s'\"&\\]+"),
        r"\1\2[masked]",
    ),
    (
        re.compile(r"(?i)(\s--?[\w-]*(?:token|password|passwd|secret|api[_-]?key)\s+['\"]?)[^\s'\"]+"),
        r"\1[masked]",
    ),  # --password x
    (re.compile(r"(\s(?:-u|--user)\s*['\"]?[^\s:'\"]+:)[^\s'\"]+"), r"\1[masked]"),  # curl -u user:password
    (re.compile(r"://[^\s/?#:@'\"]+:[^\s/?#@'\"]+@"), "://[masked]@"),
]
DENIAL_LOG_LINES = 2000
PLUGIN_LOG_NAME = "plugin-log.jsonl"


def mask(text):
    for rx, rep in _MASKS:
        text = rx.sub(rep, text)
    return text


@functools.cache
def _plugin_version():
    try:
        v = json.loads((Path(PLUGIN_QA_MEM).parent.parent / ".claude-plugin" / "plugin.json").read_text()).get(
            "version"
        )
        return str(v).strip() if v else "unknown"
    except (OSError, ValueError, AttributeError):
        return "unknown"


def _plugin_log_enabled():
    try:
        cfg = json.loads(CONFIG.read_text())
        return not (isinstance(cfg, dict) and cfg.get("pluginLog") is False)
    except (OSError, ValueError):
        return True


def log_event(data, event_type, fields):
    """Append one masked JSONL record to <project>/qa-out/plugin-log.jsonl (newest DENIAL_LOG_LINES kept). Best effort."""
    try:
        if not _plugin_log_enabled():
            return
        project = os.environ.get("CLAUDE_PROJECT_DIR")  # Claude Code names the project: its qa-out is made if missing
        folder = Path(project or str(data.get("cwd") or os.getcwd()), "qa-out")
        if project:
            folder.mkdir(exist_ok=True)
        elif not folder.is_dir():
            return  # no project named: never a new qa-out in whatever folder this ran from (/tmp, a test run)
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "v": _plugin_version(),
            "type": event_type,
            **fields,
        }
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
        log = folder / PLUGIN_LOG_NAME
        fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        size = 0
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return
            f.write(line)
            f.flush()
            size = os.fstat(f.fileno()).st_size
        if size > 150_000:
            rfd = os.open(log, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(rfd, "rb") as rf:
                if not stat.S_ISREG(os.fstat(rf.fileno()).st_mode):
                    return
                lines = rf.read(8 << 20).decode("utf-8", "replace").splitlines(keepends=True)
            if len(lines) > DENIAL_LOG_LINES:
                tfd, tmp = tempfile.mkstemp(dir=folder, prefix=".plugin-log.", suffix=".tmp")
                try:
                    with os.fdopen(tfd, "w", encoding="utf-8", errors="replace") as tf:
                        tf.writelines(lines[-DENIAL_LOG_LINES:])
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, log)
                except BaseException:
                    Path(tmp).unlink(missing_ok=True)
                    raise
    except Exception:  # noqa: BLE001, S110 - logging is best effort
        pass


def log_denial(data, reason, layer="guard"):
    """One JSONL denial record in <project>/qa-out/plugin-log.jsonl, secrets masked, newest DENIAL_LOG_LINES kept."""
    try:
        tin = data.get("tool_input")
        what = (
            str(tin.get("command"))
            if data.get("tool_name") == "Bash" and isinstance(tin, dict) and "command" in tin
            else f"{data.get('tool_name')} {json.dumps(tin, ensure_ascii=False, default=str)[:600]}"
        )
        log_event(
            data,
            "denial",
            {
                "layer": layer,
                "agent": agent_role(data) or "main",
                "tool": clean(data.get("tool_name") or ""),
                "reason": " ".join(mask(str(reason)).split())[:400],
                "call": " ".join(mask(what).split())[:300],
            },
        )
    except Exception:  # noqa: BLE001, S110 - logging is best effort
        pass


def main():
    try:
        data = json.load(sys.stdin)
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        return 0
    try:
        reason, approve = evaluate(data)
    except Exception as e:  # noqa: BLE001 - a crashing guard must not mean "allowed" for anything guarded (or the mapper)
        role, text = agent_role(data), json.dumps(data.get("tool_input"), ensure_ascii=False, default=str)
        guarded = mentions_guarded(text, role in EXPLORERS) or role == "qa-mapper"
        log_event(
            data,
            "hook_error",
            {
                "hook": "pretooluse",
                "agent": role or "main",
                "error_type": type(e).__name__,
                "detail": " ".join(mask(str(e)).split())[:300],
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
        log_denial(data, reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
