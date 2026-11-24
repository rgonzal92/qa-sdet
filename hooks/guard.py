#!/usr/bin/env python3
"""PreToolUse guard for qa-sdet (runs on every tool; most pass straight through). Denies, by default:
1. playwright-cli navigation to hosts not in config allowedHosts; run-code and attach (they reach
   outside the page: playwright-cli itself calls run-code RCE-equivalent); show bound to a non-local host
2. acli anything except read verbs (Jira stays read-only; bugs are drafts), and acli run from inline
   interpreter code (python -c, node -e, awk system()); the explorer runs no guarded tool from code at all
3. during a run (marker qa-out/.active): browser commands past the command budget or deadline,
   and route / network-state-set unless the run allows faults
4. commands that print session secrets, any read of the saved browser session file, and any use of the
   qa-sdet browser profile folder (the signed-in login) except `playwright-cli open --profile <main profile>`;
   playwright-cli state-save always (it dumps cookies)
5. destructive commands / redirections onto the qa-sdet DB directory; Write/Edit into it
6. for the qa-explorer subagent only: touching the run marker (or browser commands with no active marker),
   network CLIs (curl, wget, ...), dumping the page's HTML, and browser output files outside
   <run>/evidence/ and <run>/.data/; for everyone: uploading secret files (keys, .env, credentials)
7. the same rules for MCP fallbacks: a Playwright MCP's browser_* tools (hosts, run-code, secrets, files,
   budget) and any Atlassian/Jira MCP tool that isn't a read; the explorer gets only its own tools
8. for the qa-mapper subagent: only reading tools (no secret files: .env, keys, credentials, qa-sdet data and
   config), and Write only to qa-out/init/code-map-<area>[-<n>].json
9. qa-mem: the explorer may run only card/hash/code/search/annotate/browser-open and Write only its run files
   (bugs/, repro/, .data/run-*.json, .data/snapshot.md); for everyone `browser-open <url>` passes the host
   allowlist and counts against the budget, and `annotate` takes only <project>/qa-out/<run>/evidence/*.png
Only words in command position are checked, so ordinary commands that merely mention these tools
pass. If the guard itself fails, it denies commands that mention a guarded tool or file.
Limit: it checks command text, not intent (curl, scripts, JS that builds URLs at runtime, etc.).
"""
import codecs
import functools
import glob
import json
import os
import re
import math
import shlex
import signal
import sys
import time
from fnmatch import fnmatch
from pathlib import Path

try:
    import fcntl
except ImportError:  # shortcut: no lock on Windows; budget counting may race there
    fcntl = None

CONFIG = Path(os.environ.get("QA_SDET_CONFIG", "~/.config/qa-sdet/config.json")).expanduser()
DATA_DIR = os.path.realpath(Path(os.environ.get("QA_SDET_DATA", "~/.local/share/qa-sdet")).expanduser())
PUNCT = ";&|()\n"
QUOTED_GT, QUOTED_LT = "\x01", "\x02"  # quoted '>' / '<' are data, not redirections
FD_DUP = "\x03"  # `2>&1` comes out as `2>\x031`: an fd, never a file (a file may be named `FD:1`)


class _LazyRe:
    """re.compile on first use: most tool calls (Read, Edit, `git status`) need only a few of the patterns below, and
    compiling them all costs ~3 ms of start-up on every call. After first use the pattern's methods sit on the instance."""
    def __init__(self, pattern, flags=0):
        self.pattern, self._flags = pattern, flags

    def __getattr__(self, name):
        rx = re.compile(self.pattern, self._flags)
        self.__dict__.update((n, getattr(rx, n)) for n in ("search", "match", "fullmatch", "findall", "finditer", "sub", "split"))
        return getattr(rx, name)


# Wrapper -> its options that take a separate value.
WRAPPERS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-U", "-D", "-r", "-t", "-T"}, "doas": {"-u", "-C"},
    "env": {"-u"}, "nohup": set(), "nice": {"-n"}, "time": {"-o", "-f"}, "command": set(),
    "exec": {"-a"}, "xargs": {"-I", "-L", "-n", "-P", "-d", "-E", "-s", "-a"}, "parallel": {"-j"},
    "stdbuf": {"-i", "-o", "-e"}, "timeout": {"-s", "-k"}, "npx": {"-p", "--package", "-c"},
    "bunx": {"-p", "--package"}, "pnpx": set(), "npm": set(), "pnpm": set(), "yarn": set(),
    "builtin": set(), "setsid": set(), "ionice": {"-c", "-n", "-p"}, "taskset": {"-c", "-p"},
    "flock": {"-w", "-E", "-c"}, "unbuffer": set(), "chronic": set(), "rlwrap": set(),
    "strace": {"-o", "-e", "-p", "-s", "-E", "-u"}, "ltrace": {"-o", "-e", "-p"},
    "systemd-run": {"-u", "-p", "--unit", "--property", "-E", "--setenv"}, "busybox": set(),
    "node": {"-r", "--require", "--import"}, "bun": set(), "watch": {"-n", "-d"}, "caffeinate": set(),
    "perf": {"-o", "-e", "-p", "-r", "-C"},
}
WRAPPER_POSITIONALS = {"flock": 1, "taskset": 1}  # `flock FILE cmd`, `taskset MASK cmd` (unless -c/-p took it)
LOOP_WRAPPERS = {"xargs", "parallel", "watch"}
# Programs with rules keyed on their own name, wherever a launcher puts them (REMOVERS is added after it is defined)
LAUNCHED = {"playwright-cli", "acli", "git", "sqlite3", "cp", "ln", "tar", "dd", "rsync", "install", "chmod", "chown",
            "unzip", "7z", "7za", "bsdtar", "cpio"}
LOOP_KEYWORDS = {"for", "while", "until", "select"}
SHELL_KEYWORDS = {"if", "then", "elif", "else", "do", "while", "until", "!", "{", "}", "fi", "done", "esac", "coproc"}
PKG_SUBCOMMANDS = {"exec", "x", "run", "dlx"}  # npm exec / pnpm dlx / yarn run ...
SHELLS = {"bash", "sh", "zsh", "fish", "dash", "ksh", "ash", "source", ".", "su", "script", "tcsh", "csh", "nu", "elvish",
          "xonsh", "yash", "mksh", "oksh", "posh", "rc", "es", "ion"}
PW_VALUE_OPTS = {"-s", "--session", "--browser", "--config", "--device", "--profile", "--idle-timeout",
                 "--filename", "--depth", "--domain", "--path", "--host", "--port", "--isolated-context",
                 "--user-data-dir", "--type", "--filter", "--data", "--size", "--fps",
                 # route's options: put before the verb, an unlisted one would pass its value off as the verb
                 "--status", "--body", "--content-type", "--header", "--remove-header"}
NAV_VERBS = {"goto", "open", "tab-new"}
URL_SCAN_VERBS = NAV_VERBS | {"eval"}  # not route: a mock answers the page locally, it reaches no host
FAULT_VERBS = {"route", "network-state-set"}
BUDGET_FREE = {"", "close", "close-all", "kill-all", "list", "state-save"}  # always allowed so a run can finish
_SECRET_HINT = ("it prints session secrets into the conversation; use requests / request-body / response-body, "
                "or read one header with --raw eval \"fetch(url).then(r => r.headers.get('name'))\" "
                "(page JS never sees Set-Cookie)")
PW_DENY = {"attach": "attach connects to an unverified browser",
           "run-code": "run-code runs Node with the page object and can reach the host; use eval (page sandbox)"}
for _verb in ("cookie-list", "cookie-get", "localstorage-list", "localstorage-get", "sessionstorage-list",
              "sessionstorage-get", "request", "request-headers", "response-headers"):  # response: Set-Cookie
    PW_DENY[_verb] = _SECRET_HINT
PW_DENY["delete-data"] = ("it deletes the session's browser profile, and with it the QA sign-in; to sign out for good "
                           "see README Privacy")
PW_DENY["tracing-start"] = ("a trace records every request with its headers, cookies included, into a file; "
                            "use requests / response-body, and screenshots for evidence")
# Page-side reads of cookies / web storage (eval runs in the page).
SECRET_JS = _LazyRe(
    r"[dD]ocument\s*(\.\s*cookie|\[\s*['\"`]cookie)|cookieStore|"  # also el.ownerDocument.cookie
    r"\.\s*cookie\b(?![-\w'\"`])|['\"`]cookie['\"`]|[{,]\s*cookie\s*[,}:]|"  # d=document; d.cookie / Reflect.get(document,'cookie') / ({cookie}) =>
    r"(local|session)Storage\s*(\.\s*getItem\b|\[)|\.\.\.\s*(local|session)Storage|"
    r"\bindexedDB\s*\.\s*open\b|"  # auth libraries (Firebase) keep tokens there; indexedDB.databases() names stay allowed
    r"(JSON\.stringify|Object\.(values|entries|assign|fromEntries))\s*\(\s*(local|session)Storage")
# Key names (Object.keys(localStorage), localStorage.key(i), .length, for..in) are allowed: they show what a page stores.
ACLI_READ = {"view", "search", "list", "get", "help", "version", "whoami"}
ACLI_AUTH_OK = {"login", "status"}
ACLI_WRITE = {"create", "edit", "delete", "transition", "assign", "update", "clone", "archive", "unarchive",
              "link", "unlink", "move", "add", "remove", "set", "import", "upload", "logout", "bulk"}
DB_DESTRUCTIVE = {"rm", "mv", "shred", "truncate", "dd", "unlink", "rmdir", "trash", "trash-put", "gio",
                  "rmtrash", "kioclient", "kioclient5"}
DB_WRITERS = {"cp", "tee", "install", "ln", "rsync"}
INPLACE_EDITORS = {"sed", "gsed", "perl", "ruby", "gawk"}
SESSION_FILE_OK = {"chmod", "ls", "stat", "test", "[", "[[", "rm", "touch", "echo", "printf", "file", "wc",
                   "realpath", "readlink", "du", "mkdir", "dirname", "basename"}
URL_RE = _LazyRe(r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,30}://[^\s'\"`)]+", re.I)
# `<<EOF`, `<<-'EOF'`, `<<E"OF"`, `<<\EOF`: the delimiter is the whole word with its quotes removed (bash: EOF)
HEREDOC_RE = _LazyRe(r"""(?<![<$(\d])<<(?!<)(-?)[ \t]*((?:[^\s<>;&|()'"\\]|\\.|'[^']*'|"[^"]*")+)""")


def heredoc_end(word):
    return re.sub(r"""\\(.)|['"]""", lambda m: m.group(1) or "", word)
REDIRECT_RE = re.compile(r"^(\d*)(>>?|<<?<?)(.*)$", re.S)
GUARDED_TOOLS_RE = re.compile(r"(?<![.\w])playwright-cli|@playwright/cli|\bacli\b|(?<![\w.-])qa-mem(?![\w-])")  # not .playwright-cli/
POS_PARAM_RE = _LazyRe(r"^\$(\{?[@*]\}?|\{?[1-9]\}?)$")  # exactly "$@", $1: a wrapper passing its args through ('$100' is data)
SCHEMELESS_URL_RE = _LazyRe(r"[\"'`(]\s*//([a-z0-9][a-z0-9.-]*)", re.I)  # '//host/path' in JS
PAGE_DUMP_RE = _LazyRe(r"(documentElement|document\.body|document\.head|\bdocument)\s*\.\s*(outer|inner)HTML"
                          r"|XMLSerializer|document\.scripts|querySelectorAll\(\s*['\"`]script")
NOT_A_PATH_RE = re.compile(r"[\s()'\"=;`|<>]")  # page JS/CSS in an argument, not a file path
CODE_TEXT_RE = _LazyRe(r"[()'\"=;`|<>\n]")  # like NOT_A_PATH_RE, but a path may hold spaces ('/home/me/my proj/.env')
GLOB_RE = re.compile(r"[*?\[{]")
NET_CLIS = {"curl", "wget", "http", "https", "xh", "httpie", "nc", "ncat", "netcat", "telnet", "socat", "aria2c", "ssh", "scp",
            "sftp", "ftp",
            "lynx", "w3m", "links", "elinks"}
GREP_TOOLS = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "ugrep", "zgrep"}
ARCHIVERS = {"tar", "bsdtar", "zip", "7z", "7za", "rsync", "rclone", "cpio", "pax"}
COPY_TOOLS = {"cp", "mv", "rsync", "scp", "install", "ln"}
READ_ONLY_TOOLS = {"ls", "du", "stat", "cat", "head", "tail", "wc", "file", "grep", "rg", "egrep", "fgrep", "test", "[",
                   "[[", "realpath", "readlink", "jq", "less", "diff", "tree"}
REMOVERS = {"rm", "rmdir", "unlink", "mv", "shred", "trash", "trash-put", "gio", "truncate"}
LAUNCHED |= REMOVERS
INTERPRETERS = {"python", "python3", "node", "perl", "ruby", "php", "deno", "bun", "osascript", "pwsh", "powershell",
                "expect", "tclsh", "wish", "lua", "luajit", "Rscript", "julia", "groovy", "raku", "guile", "racket",
                "elixir", "escript", "jshell", "swift", "kotlin", "scala"}
# Tools that only name or print a program (`grep acli README.md`, `which playwright-cli`): never launch it
# Programs that run a command given as one string (not `gh issue create --title "acli broken"`: that is text)
STRING_RUNNERS = {"ssh", "parallel", "tmux", "screen", "at", "batch", "watch", "su", "runuser", "flock", "script",
                  "dtach", "abduco", "xterm", "konsole", "gnome-terminal", "kitty", "alacritty", "wezterm", "entr",
                  "hyperfine", "sg", "npx"}
EXEC_FLAGS = {"-exec", "-execdir", "-ok", "-okdir", "--exec", "--exec-batch", "-x", "-X", "--"}  # a program follows
NAME_ONLY_TOOLS = {"echo", "printf", "true", ":", "which", "type", "whereis", "whatis", "man", "info", "help", "hash",
                   "apropos", "tldr", "alias", "unalias", "sort", "uniq", "cut", "tr", "column", "fold", "nl", "tee"}
GUARDED_ANY_RE = _LazyRe(r"playwright-cli|@playwright/cli|\bacli\b|qa-state|qa-mem|qa-sdet/profiles")
PROFILE_OK = {"test", "[", "[[", "echo", "printf", "true", ":"}  # name a profile path without reading it
PROFILE_REASON = ("the qa-sdet browser profile folder holds the signed-in QA session (cookies); only "
                  "qa-mem browser-open (playwright-cli open --profile <main profile>) may use it")
LIST_READERS = {"cpio", "pax"}  # read the files named on stdin (`find <dir> | cpio -o`)
LIST_READER_OPTS = {"-T", "--files-from", "-@", "--names-stdin"}  # tar -T -, rsync --files-from, zip -@
STDIN_SCRIPT_TOOLS = {"sqlite3", "duckdb", "awk", "gawk", "mawk", "nawk", "busybox", "ed", "ex"}
CODE_TEXT_OK = {"git", "gh"}  # commit messages / PR bodies that mention the profile folder
PY_VALUE_OPTS = {"-W", "-X", "-Q", "--check-hash-based-pycs"}
QAMEM_EXPLORER_OK = {"card", "hash", "code", "search", "annotate", "browser-open"}
QAMEM_VALUE_OPTS = {"browser-open": {"--session"}, "annotate": {"--box", "--label"}}  # as qa-mem parses them
# Other places playwright-cli takes its browser from (playwright-core resolveCLIConfigForCLI / configFromEnv):
# env vars, a config file (--config, ./.playwright/cli.config.json, ~/.playwright/cli.config.json), a real browser.
PW_ENV_RE = re.compile(r"(PLAYWRIGHT_MCP_|PWTEST_)\w*=")  # `PLAYWRIGHT_MCP_USER_DATA_DIR=x cmd`, env/export forms
PW_ENV_NAME_RE = _LazyRe(r"^(PLAYWRIGHT_MCP_|PWTEST_)")
EXPORTERS = {"export", "declare", "typeset", "readonly", "local"}
PW_SOURCE_OPTS = {"--config", "--extension", "--cdp", "--endpoint"}  # a config file or someone else's browser
CLI_CONFIG_RE = _LazyRe(r"cli\.config\.json")
QAMEM_RUNNERS = {"uv", "uvx", "poetry", "pipx", "pdm", "hatch", "rye", "conda", "mamba", "micromamba", "pixi"}
# The explorer always runs under its skill's marker; without one (deleted, moved, expired) its budget, deadline
# and faults setting would be gone, so its browser commands stop.
NO_RUN = ("no active run marker (<project>/qa-out/.active is missing or expired): the explorer's browser commands "
          "run only inside the calling skill's run; close the browser, write <out>/.data/run-<slug>.json (out = <project>/qa-out/<run>) and return to the caller")
MARKER_CODE_RE = _LazyRe(r"(?<![\w-])\.active(?:\.lock)?\b|\.config/+qa-sdet|qa-sdet/+config\.json|QA_SDET_CONFIG|"
                            + re.escape(str(CONFIG)))  # the config where QA_SDET_CONFIG puts it, too
QAMEM_NAME_OK = {"test", "[", "[[", "which", "type", "whereis"}  # the explorer may check qa-mem exists, nothing that prints or copies it


class Ctx:
    """State for one tool call: the active run marker and what the command would spend."""
    def __init__(self, cwd, explorer=False):
        self.explorer = explorer  # the qa-explorer subagent: observes the app through the browser only
        self.marker_path, self.marker, self.marker_error = read_marker(cwd)
        self.marker_files = {realpath(os.path.join(b, "qa-out", ".active"))
                             for b in filter(None, (cwd, os.environ.get("CLAUDE_PROJECT_DIR")))}
        self.marker_files |= {m + ".lock" for m in self.marker_files}
        self.marker_files.add(realpath(CONFIG))  # the explorer leaves the allowlist alone too
        self.spend = 0
        self.raw = ""
        self.loop = False  # a playwright-cli call runs inside a loop / xargs / watch: can't be counted
        self.loop_depth = 0
        self.pw_env = False  # the command sets a PLAYWRIGHT_MCP_* / PWTEST_* variable (or exports one it can't name)
        self.runs_pw = False  # ... and runs playwright-cli or qa-mem browser-open
        self.mentions = None  # does the whole call mention a guarded tool (computed once: $(...) bodies share it)


# ---------- config, hosts, files ----------

_RP = None  # real paths for one top-level check()/decide(); None outside one


def realpath(p):
    """os.path.realpath, remembered for the length of one hook decision: a sourced rc file asks for the same paths
    thousands of times, each an lstat per component (slow on a network home folder). Within one decision the guard
    changes no file before spend_budget at the very end. Outside one (helpers called directly), as it is now."""
    if _RP is None:
        return os.path.realpath(p)
    if p not in _RP:
        _RP[p] = os.path.realpath(p)
    return _RP[p]


def _one_decision(fn):
    """check() and decide() share one path cache per top-level call; a nested call joins the outer one."""
    @functools.wraps(fn)
    def run(*args, **kwargs):
        global _RP
        if _RP is not None:
            return fn(*args, **kwargs)
        _RP = {}
        try:
            return fn(*args, **kwargs)
        finally:
            _RP = None
    return run


_MEMO = {}  # per hook call: these folder sets cost a glob and several realpaths, and are asked for every word
_MEMO_ENV = ("HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "LOCALAPPDATA", "PLAYWRIGHT_MCP_USER_DATA_DIR",
             "PLAYWRIGHT_MCP_STORAGE_STATE")


def _memo(fn):
    def cached():
        cfg = None
        if fn.__name__ == "allowed_hosts":  # the config file may change between calls in one process (tests)
            try:
                st = CONFIG.stat()
                cfg = (st.st_mtime_ns, st.st_size, st.st_ino)
            except OSError:
                pass
        env = _RP.get(_MEMO_ENV) if _RP is not None else None  # read once per decision: a script asks thousands of times
        if env is None:
            env = tuple(map(os.environ.get, _MEMO_ENV))
            if _RP is not None:
                _RP[_MEMO_ENV] = env  # (a tuple key: never one of the real paths kept there)
        key = (fn.__name__, CONFIG, DATA_DIR, cfg) + env
        if key not in _MEMO:
            _MEMO[key] = frozenset(fn())
        return _MEMO[key]
    cached.__name__ = fn.__name__
    return cached


@_memo
def allowed_hosts():
    hosts = set()  # QA only: localhost counts only when listed (e.g. for the self-test fixture)
    if CONFIG.exists():
        try:
            listed = json.loads(CONFIG.read_text()).get("allowedHosts", [])
        except (json.JSONDecodeError, OSError, AttributeError):
            listed = []  # broken config: no navigation at all
        if isinstance(listed, list):
            hosts |= {h.lower().strip().rstrip(".").removeprefix("*.") for h in listed
                      if isinstance(h, str) and h.strip()}
    if hosts & {"localhost", "127.0.0.1", "::1"}:  # loopback aliases
        hosts |= {"localhost", "127.0.0.1", "::1"}
    return hosts


def host_ok(url, hosts):
    if url.strip().lower() == "about:blank":
        return True  # reaches no host
    if "://" in url and url.split("://", 1)[0].strip().lower() not in ("http", "https", "ws", "wss"):
        return False  # javascript://allowed.host/%0Acode runs code; data:, blob:, file:, view-source: reach no QA host
    authority = re.split(r"[/?#]", url.split("://", 1)[1] if "://" in url else url)[0]
    if "\\" in url or "@" in authority:
        return False  # user@host forms: browsers and urlparse disagree; don't guess (an @ in the query is fine)
    if "://" not in url:
        url = "http://" + url
    from urllib.parse import urlparse  # here: ~2 ms of start-up on every tool call, and only URL checks need it
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if not host or "%" in host:  # %23, %2f: browsers decode the host first, so what urlparse reads isn't where they go
        return False  # file://, about:, data: etc.
    return any(host == h or host.endswith("." + h) for h in hosts)


def expand(path):
    # `${XDG_DATA_HOME:-$HOME/.local/share}` (or `:=`): expandvars leaves it as is; take the value, else the default
    path = re.sub(r"\$\{(\w+):?[-=]([^{}]*)\}", lambda m: os.environ.get(m.group(1)) or m.group(2), path)
    return os.path.expandvars(os.path.expanduser(path))


def resolve(path, cwd):
    """Absolute real path; for a glob, its directory part."""
    path = expand(path)
    if path.startswith("file:"):
        path = path[5:].split("?")[0]
    head = re.split(r"[*?\[{]", path)[0]
    if head != path:
        head = head if head.endswith("/") else os.path.dirname(head)
    return realpath(os.path.join(cwd, head or "."))


@_memo
def session_files():
    files = {realpath(CONFIG.with_name("qa-state.json"))}
    if os.environ.get("PLAYWRIGHT_MCP_STORAGE_STATE"):  # the user's own browser cookie jar
        files.add(realpath(expand(os.environ["PLAYWRIGHT_MCP_STORAGE_STATE"])))
    return files


@_memo
def profile_dirs():
    """Playwright MCP browser profiles: with the MCP fallback the QA login lives there (cookies.sqlite etc.)."""
    cache = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    dirs = {f"{cache}/ms-playwright-mcp", "~/Library/Caches/ms-playwright-mcp", "$LOCALAPPDATA/ms-playwright-mcp"}
    dirs |= set(glob.glob(os.path.expanduser(f"{cache}/ms-playwright/mcp-*")))  # older MCP versions kept profiles here
    if os.environ.get("PLAYWRIGHT_MCP_USER_DATA_DIR"):
        dirs.add(os.environ["PLAYWRIGHT_MCP_USER_DATA_DIR"])
    home = realpath(os.path.expanduser("~"))
    return ({realpath(expand(d)) for d in dirs if "$" not in expand(d)} | profile_roots()) - {home, "/", ""}


@_memo
def profile_roots():
    """The qa-sdet browser profile folders (the persistent `main` profile holds the QA login). qa-mem keeps them
    beside its DB; an XDG_DATA_HOME location is protected too."""
    roots = {realpath(os.path.join(DATA_DIR, "profiles"))}
    if os.environ.get("XDG_DATA_HOME"):
        roots.add(realpath(os.path.join(os.path.expanduser(os.environ["XDG_DATA_HOME"]), "qa-sdet", "profiles")))
    return roots


def main_profiles():
    return {realpath(os.path.join(r, "main")) for r in profile_roots()}


_ASSIGNED = {}


def var_variants(word, raw):
    """`word` with each $VAR / ${VAR} replaced by the values assigned to it in the whole command (`P=~/x; cat $P`);
    unassigned variables stay for expandvars."""
    out = [word]
    if "$" not in word or not raw:
        return out
    known = _ASSIGNED.get(id(raw))
    if known is None or known[0] is not raw:  # every NAME=value of the text, found once (a long script asks per word)
        if len(_ASSIGNED) > 8:
            _ASSIGNED.clear()
        known = _ASSIGNED[id(raw)] = (raw, {})
        for n, v in re.findall(r"(?<![A-Za-z0-9_$])([A-Za-z_][A-Za-z0-9_]*)\+?=(\"[^\"]*\"|'[^']*'|[^\s;&|()]+)", raw):
            known[1].setdefault(n, []).append(re.sub(r"[\"']", "", v))
    for name in set(re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", word)):
        vals = known[1].get(name)
        if vals:
            # `${X:-d}` / `${X:=d}` stays too: d is what runs when the assignment never does (`… # X=…`, a skipped
            # branch); expand() fills it in
            keep = [w for w in out if re.search(rf"\$\{{{name}:?[-=]", w)]
            # with X assigned in the command, `${X:-default}` is the assigned value, and `${X:+alt}` is alt
            pat = re.compile(rf"\$(?:\{{{name}(?:(:?[-=+?])([^{{}}]*))?\}}|{name}(?![A-Za-z0-9_]))")
            out = (keep + [pat.sub(lambda m, v=v: m.group(2) if (m.group(1) or "").endswith("+") else v, w)
                           for w in out for v in vals])[:32]
    return out


def in_profile_root(token, cwds, raw=""):
    """Does a word (a path, `--opt=path`, `if=path`, a glob, a $VAR set earlier) point into a profile folder?"""
    roots = profile_roots()
    for cand in [token] + ([token.split("=", 1)[1]] if "=" in token else []):
        for t in var_variants(cand, raw):
            t = expand(t)
            if t.startswith("file:"):
                t = t[5:].split("?")[0]
            if not t or NOT_A_PATH_RE.search(t):
                continue
            for c in cwds:
                p = realpath(os.path.join(c, t))
                if any(p == r or p.startswith(r + os.sep) for r in roots):
                    return True
                if GLOB_RE.search(t):  # `cat ~/.local/share/qa-*/pro*/main/Default/Cookies`
                    for alt in brace_alts(os.path.normpath(os.path.join(c, t))):
                        parts = Path(alt).parts
                        if any(shell_glob_match(r, os.path.join(*parts[:k])) for r in roots for k in range(2, len(parts) + 1)):
                            return True
    return False


def is_main_profile(value, cwds, raw=""):
    """Is a --profile value exactly the main profile folder, however it is spelled (~, $HOME, ../, trailing /)?"""
    variants = var_variants(value, raw)
    for t in variants:
        t = expand(t)
        if not t or "$" in t or "SUBST" in t or GLOB_RE.search(t):
            return False
        if any(realpath(os.path.join(c, t)) not in main_profiles() for c in cwds):
            return False
    return True


def glob_reaches_profile(t, cwd):
    """Could a shell glob reach into a browser profile folder (`~/.cache/ms-play*/*/cookies.sqlite`)?"""
    parts = Path(os.path.normpath(os.path.join(cwd, t))).parts  # not resolve(): it stops at the first glob
    return any(shell_glob_match(d, os.path.join(*parts[:k])) for d in profile_dirs() for k in range(2, len(parts) + 1))


def in_profile_dir(path, cwd):
    p = realpath(os.path.join(cwd, expand(path)))
    return any(p == d or p.startswith(d + os.sep) for d in profile_dirs())


@_memo
def session_dirs():
    """The folders that directly hold a session file (never ~ or /). Their parents are protected only from
    tools that read recursively (is_session_ancestor), so ordinary commands in a project stay allowed."""
    home = realpath(os.path.expanduser("~"))
    return {os.path.dirname(f) for f in session_files()} - {home, "/", ""}


def shell_glob_match(path, pattern):
    """Like the shell: '*' and '?' stay inside one path component; only '**' spans folders."""
    ps, pp = path.strip("/").split("/"), pattern.strip("/").split("/")
    if len(pp) > 200:  # absurd pattern: assume it could match rather than recurse that deep
        return True
    memo = {}

    def match(i, j):  # memoised on positions: linear in len(ps) * len(pp), even for many '**'
        if (i, j) not in memo:
            if j == len(pp):
                memo[i, j] = i == len(ps)
            elif pp[j] == "**":
                memo[i, j] = match(i, j + 1) or (i < len(ps) and match(i + 1, j))
            else:
                memo[i, j] = (i < len(ps) and fnmatch(ps[i], pp[j]) and match(i + 1, j + 1)
                              and (pp[j].startswith(".") or not ps[i].startswith(".")))  # '*' skips dotfiles
        return memo[i, j]
    return match(0, 0)


def glob_hits(token, cwd, paths):
    """Paths (absolute, real) that the shell glob `token`, run in cwd, could expand to."""
    head = re.split(r"[*?\[{]", token)[0]
    base_rel = os.path.dirname(head) or "."
    base = realpath(os.path.join(cwd, base_rel))
    pat = os.path.join(base, os.path.relpath(os.path.join(cwd, token), os.path.join(cwd, base_rel)))
    return {p for p in paths for a in brace_alts(pat) if shell_glob_match(p, a)}


def brace_alts(pattern, limit=256):
    """'a/{b,c}/*.{json,y}' -> every brace expansion (bounded)."""
    alts, done = [pattern], []
    while alts and len(done) < limit:
        g = alts.pop()
        m = re.search(r"\{([^{}]*,[^{}]*)\}", g)  # bash expands only with a comma: `{}` is literal
        if m:
            alts += [g[:m.start()] + a + g[m.end():] for a in m.group(1).split(",")]
        else:
            done.append(g)
    return done


def is_session_file(path, cwd="/"):
    return realpath(os.path.join(cwd, expand(path))) in session_files()


def is_session_ref(token, cwds, raw=""):
    """raw = the whole command: a `$F` also counts with the values assigned there (`F=<session file>; cat $F`)."""
    return any(_is_session_ref(t, cwds) for t in {token, *var_variants(token, raw)})  # `echo HOME=/x; cat $HOME/…`


def _is_session_ref(token, cwds):
    t = expand(token)
    if t.startswith("file:"):
        t = t[5:].split("?")[0]
    files, code_like = session_files(), bool(NOT_A_PATH_RE.search(t))  # JS/CSS words aren't file globs
    if not code_like and os.path.basename(t).startswith("qa-state"):  # a copy or the file seen from anywhere
        return True
    for c in cwds:
        if realpath(os.path.join(c, t)) in files or (not code_like and in_profile_dir(t, c)):
            return True
        if code_like:
            continue
        if GLOB_RE.search(t):  # `cat ~/.config/*/*.json`, `tar c ~/.config/qa-*`: a session file or its folder?
            if glob_hits(t, c, files | session_dirs()) or glob_reaches_profile(t, c):
                return True
        elif realpath(os.path.join(c, t)) in session_dirs():
            return True
    return False


def is_session_ancestor(token, cwds):
    """A folder above a session file, at any level (~, /): a recursive content search from it reads the file."""
    for c in cwds:
        p = resolve(expand(token), c).rstrip(os.sep) + os.sep
        if any(f.startswith(p) for f in session_files() | {d + os.sep for d in profile_dirs()}):
            return True
    return False


def glob_may_match_session(glob, base):
    """Could a Grep glob like '*.json', '**/*.{ts,json}' or '.config/**' (relative to base) select a session file?"""
    if (glob or "").startswith("!"):  # an exclusion narrows nothing
        return True
    done = brace_alts(glob or "")
    targets = session_files() | {os.path.join(d, "x") for d in profile_dirs() if d.startswith(base.rstrip(os.sep) + os.sep)}
    return any(fnmatch(f, a) or fnmatch(os.path.basename(f), a) or fnmatch(os.path.relpath(f, base), a)
               or (f not in session_files() and a.startswith("**"))  # `**/cookies.sqlite` reaches into a profile
               for f in targets for a in done)


def recursive_content_search(name, rest):
    """grep -r / rg / ag / ack ... that prints matching lines (not just file names or counts)."""
    short = "".join(re.sub(r"\d+", "", a[1:]) for a in rest if re.match(r"^-[a-zA-Z][a-zA-Z0-9]*$", a))
    grep_like = name in ("grep", "egrep", "fgrep", "ugrep")
    recurse = (re.search(r"[rR]", short) or {"--recursive", "--dereference-recursive", "--directories=recurse"}
               & set(rest) or any(a == "-d" and b == "recurse" for a, b in zip(rest, rest[1:])))
    if (grep_like and not recurse) or name not in ("grep", "egrep", "fgrep", "ugrep", "rg", "ag", "ack"):
        return False
    names_only = r"[lc]" if name == "rg" else r"[lLc]"  # rg -L means --follow
    return not (re.search(names_only, short)
                or {"--files-with-matches", "--files-without-match", "--count", "--files"} & set(rest))


def db_names():
    try:
        return set(os.listdir(DATA_DIR)) | {"qa-sdet"}
    except OSError:
        return {"qa-sdet", "default.db"}


def touches_db(path, cwds, parent_counts, raw=""):
    """raw = the whole command: a `$X` in the path also counts with the values assigned there (`X=<db dir>; rm -rf $X`)."""
    return any(_touches_db(p, cwds, parent_counts) for p in {path, *var_variants(path, raw)})


def long_text(word):
    """A long word with spaces or quotes is text or code (JSON, SQL, a script), not a file glob: expanding its braces
    and compiling each as a pattern took seconds per hook call."""
    return len(word) > 512 and NOT_A_PATH_RE.search(word) is not None


def _touches_db(path, cwds, parent_counts):
    t = expand(path)
    if long_text(t):
        return False
    for cwd in cwds:
        if GLOB_RE.search(t):  # `mv ~/*.pdf x`: what the glob can match, not the folder it sits in
            head = resolve(t, cwd)
            if head == DATA_DIR or head.startswith(DATA_DIR + os.sep):
                return True
            targets, d = {DATA_DIR}, DATA_DIR
            while parent_counts and d not in ("/", ""):
                d = os.path.dirname(d)
                targets.add(d)
            parent = os.path.dirname(t.rstrip("/"))
            if glob_hits(t, cwd, targets) or (GLOB_RE.search(parent) and glob_hits(parent, cwd, {DATA_DIR})):
                return True  # also `~/.local/share/qa-*/*.db`: a globbed folder that is the DB folder
            continue
        p = resolve(path, cwd)
        if p == DATA_DIR or p.startswith(DATA_DIR + os.sep):
            return True
        if parent_counts and DATA_DIR.startswith(p.rstrip(os.sep) + os.sep):
            return True
    return False


# ---------- run marker and budget ----------

def read_marker(cwd):
    """(path, marker, error) of the active run. An expired marker counts as no run; a malformed one is an error."""
    for base in dict.fromkeys(filter(None, (cwd, os.environ.get("CLAUDE_PROJECT_DIR")))):
        path = Path(base) / "qa-out" / ".active"
        if not path.exists():
            continue
        try:
            marker = json.loads(path.read_text())
            if not isinstance(marker, dict):
                raise ValueError("not an object")
            expires = float(marker["expires"])
            for key in ("expires", "deadline"):  # epoch seconds; milliseconds would never expire
                if marker.get(key) is not None and float(marker[key]) > time.time() + 2 * 86400:
                    raise ValueError(f"{key} is more than 2 days ahead (use epoch seconds: date +%s)")
            for key in ("deadline", "budget", "used"):  # NaN/inf would silently lift a limit (or crash the count)
                if marker.get(key) is not None and not (math.isfinite(float(marker[key])) and float(marker[key]) >= 0):
                    raise ValueError(f"{key} must be a number of 0 or more")
            if marker.get("faults") not in (None, True, False):
                raise ValueError("faults must be true or false")
        except (OSError, ValueError, TypeError, KeyError) as e:
            return path, None, f"{path} is malformed ({e}); rewrite it per common.md C4"
        if expires > time.time():
            return path, marker, None
    return None, None, None


def spend_budget(path, n, explorer=False):
    """Atomically count n browser commands (playwright-cli or MCP browser_*) against the run; deny reason when used up."""
    # No truncation and no symlink following: a planted `.active.lock -> ~/.bashrc` must not empty that file.
    fd = os.open(path.with_name(".active.lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0), 0o600)  # nonblocking: a planted FIFO can't hang the hook
    lock = os.fdopen(fd, "r+")
    try:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        marker = json.loads(path.read_text())
        deadline, budget = float(marker.get("deadline") or 0), int(float(marker.get("budget") or 0))
        used = int(float(marker.get("used") or 0))
        if deadline and time.time() > deadline + 600 and not explorer:  # a crashed run left it behind (user hint)
            return f"a qa-sdet run marker expired over 10 minutes ago; if no run is active, delete {path}"
        if deadline and time.time() > deadline:
            return "the run's time budget is used up: close the browser (MCP: browser_tabs close on your tab), write your bug drafts and <out>/.data/run-<slug>.json now"
        if budget and used + n > budget:
            if used < budget:  # a batch bigger than what's left: the run can go on with fewer commands per call
                return (f"this call has {n} browser commands but only {budget - used} of the run's {budget} are left "
                        f"({used} used); send fewer per call, or close the browser and write <out>/.data/run-<slug>.json if done")
            return (f"the run's budget of {budget} browser commands is used up ({used} used): "
                    "close the browser (MCP: browser_tabs close on your tab), write your bug drafts and <out>/.data/run-<slug>.json now")
        marker["used"] = used + n
        import tempfile  # here, not at the top: it costs ~4 ms of start-up on every tool call, and only a run's budget needs it
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".active.")
        with os.fdopen(fd, "w") as f:
            json.dump(marker, f)
        os.replace(tmp, path)
        return None
    finally:
        lock.close()


# ---------- text pre-processing ----------

def normalize(cmd):
    """Quote-aware pass before tokenising: drop '#' comments that start a word, turn unquoted newlines
    into ';', mark quoted '<' '>' as data, and rewrite redirections whose '&' or '|' would split the
    command: `2>&1` -> `2>` FD_DUP `1`, `&>f` -> `>f`, `>|f` -> `>f`."""
    out, q, i = [], None, 0
    while i < len(cmd):
        c = cmd[i]
        if q:
            if c == "\\" and q == '"' and i + 1 < len(cmd):
                out.append(cmd[i:i + 2])
                i += 2
                continue
            if c == q:
                q = None
            elif c in "<>":
                c = QUOTED_GT if c == ">" else QUOTED_LT
        elif c == "$" and cmd[i + 1:i + 2] == '"':  # $"…" is "…" (locale translation)
            i += 1
            continue
        elif c == "$" and cmd[i + 1:i + 2] == "'":  # $'\x70laywright-cli' is playwright-cli: decode, re-quote
            j, raw = i + 2, []
            while j < len(cmd) and cmd[j] != "'":
                raw.append(cmd[j:j + 2] if cmd[j] == "\\" else cmd[j])
                j += 2 if cmd[j] == "\\" else 1
            try:
                text = codecs.decode("".join(raw).encode("latin-1", "backslashreplace"), "unicode_escape")
            except (UnicodeError, ValueError):
                text = "".join(raw)
            text = text.encode("utf-8", "replace").decode("utf-8").replace("\0", "")  # $'\\0': no path holds a NUL
            out.append(shlex.quote(text).replace(">", QUOTED_GT).replace("<", QUOTED_LT))
            i = j + 1
            continue
        elif c in "'\"":
            q = c
        elif c == "\\" and i + 1 < len(cmd):
            out.append(cmd[i:i + 2])
            i += 2
            continue
        elif c == "#" and (i == 0 or cmd[i - 1] in " \t\n;&|(") and not (out and out[-1][:1] == "\\" and len(out[-1]) == 2):
            while i < len(cmd) and cmd[i] != "\n":
                i += 1
            continue
        elif c == "\n":
            c = ";"
        elif c in "<>" and cmd[i + 1:i + 2] == "&":
            out.append(c + FD_DUP)
            i += 2
            continue
        elif c == ">" and cmd[i + 1:i + 2] == "|":
            out.append(">")
            i += 2
            continue
        elif c == "&" and cmd[i + 1:i + 2] == ">" and (i == 0 or cmd[i - 1] != "&"):
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


CASE_WORD_RE = _LazyRe(r"(case|esac|in)(?![\w.-])")


def match_paren(cmd, k):
    """Index of the ')' closing the '(' at cmd[k] (len(cmd) if unclosed). Parsed as bash does: parens inside quotes,
    and the ')' ending a `case … in PATTERN)` pattern, close nothing (`cat <( case x in x) cmd;; esac )`)."""
    depth, j, q, expect_in, pattern, cases = 0, k, None, False, False, 0
    while j < len(cmd):
        c = cmd[j]
        if q:
            if c == "\\" and q == '"':
                j += 2
                continue
            if c == q:
                q = None
        elif c == "\\":
            j += 2
            continue
        elif c in "'\"":
            q = c
        elif c.isalpha() and (j == 0 or not (cmd[j - 1].isalnum() or cmd[j - 1] in "_-./$")) \
                and (m := CASE_WORD_RE.match(cmd, j)):
            w = m.group(1)
            if w == "case":
                cases, expect_in = cases + 1, True
            elif w == "in" and expect_in:
                expect_in, pattern = False, True
            elif w == "esac" and cases:
                cases, pattern = cases - 1, False
            j += len(w)
            continue
        elif cases and c == ";" and cmd[j:j + 2] == ";;":
            pattern = True
            j += 2
            continue
        elif cases and pattern and c in "()":
            pattern = c == "("  # `(x)`: the optional opening paren; `x)`: the end of the pattern
            j += 1
            continue
        depth += {"(": 1, ")": -1}.get(c, 0) if not q else 0
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
            out.append(cmd[i:j + 1])
            i = j + 1
        elif c == "\\" and i + 1 < len(cmd):
            out.append(cmd[i:i + 2])
            i += 2
        elif c == '"':
            dq = not dq
            out.append(c)
            i += 1
        elif c == "$" and cmd[i + 1:i + 3] == "((":
            outer, inner = match_paren(cmd, i + 1), match_paren(cmd, i + 2)
            if inner + 1 == outer:  # `$((...))`: arithmetic, a number; nested $(...) in it still runs
                bodies += substitutions(cmd[i + 3:inner])[0]
                out.append("ARITH")
            else:  # `$((cmd) )` is a command substitution holding a subshell
                bodies.append(cmd[i + 2:outer])
                out.append("SUBST")
            i = outer + 1
        elif c in "$<>" and cmd[i + 1:i + 2] == "(":
            j = match_paren(cmd, i + 1)
            bodies.append(cmd[i + 2:j])
            out.append("SUBST")
            i = j + 1
        elif c == "`":
            j = cmd.find("`", i + 1)
            if j == -1:
                out.append(cmd[i:])
                break
            bodies.append(cmd[i + 1:j])
            out.append("SUBST")
            i = j + 1
        else:
            out.append(c)
            i += 1
    return bodies, "".join(out)


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


def unmark(text):
    """A quoted string a shell runs again (bash -c, eval, watch, env -S): its '<' '>' are redirections there."""
    return text.replace(QUOTED_GT, ">").replace(QUOTED_LT, "<")


def tokens(cmd):
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=PUNCT)
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def split_redirects(seg):
    """(words, output targets, input targets). Fd duplications (FD_DUP n) aren't files."""
    words, outs, ins, i = [], [], [], 0
    while i < len(seg):
        m = REDIRECT_RE.match(seg[i])
        if m:
            target = m.group(3)
            if not target and i + 1 < len(seg):
                i += 1
                target = seg[i]
            if target.startswith(FD_DUP) and not re.fullmatch(r"\d+-?|-", target[1:]):
                target = target[1:]  # `>&file` writes stdout and stderr to a file, not to an fd
            if target and not target.startswith(FD_DUP):
                if m.group(2) == "<" and target.startswith(">"):  # `<>file` opens it read-write (`exec 3<>f`)
                    ins.append(target[1:])
                    target, kind = target[1:], ">"
                else:
                    kind = m.group(2)
                (outs if kind.startswith(">") else ins if kind == "<" else []).append(target)
        else:
            words.append(seg[i])
        i += 1
    return words, outs, ins


def resume_at(seg, j):
    """The command from its j-th word on, every redirection of the segment kept (`chrt bash <<<'…'`, `X - < script`)."""
    out, pre, n, i = [], [], -1, 0
    while i < len(seg):
        m = REDIRECT_RE.match(seg[i])
        if m:
            part = seg[i:i + 2] if not m.group(3) and i + 1 < len(seg) else seg[i:i + 1]
            i += len(part)
            # an attached target keeps its quoting too (`<<<'qa-mem marker stop'` is one word, not three)
            first = m.group(1) + m.group(2) + shlex.quote(unmark(m.group(3))) if m.group(3) else part[0]
            (out if n >= j else pre).append(" ".join([first, *(shlex.quote(unmark(x)) for x in part[1:])]))
            continue
        n += 1
        if n >= j:
            out.append(shlex.quote(unmark(seg[i])))
        i += 1
    return " ".join(out + pre)


# ---------- per-command checks ----------

@functools.lru_cache(maxsize=4096)  # a script asks for the same few words thousands of times
def prog_name(tok):
    if tok in ("$SHELL", "${SHELL}"):  # the user's shell, whatever it is
        return "sh"
    if tok.startswith("@playwright/cli"):
        return "playwright-cli"
    name = re.sub(r"@[^/]*$", "", os.path.basename(tok)).removesuffix(".js")
    win = re.sub(r"\.(exe|cmd|bat|ps1)$", "", name, flags=re.I)  # Git Bash/WSL: `acli.exe` runs acli
    # macOS and Windows file systems ignore case: `ACLI` runs acli there
    return win.lower() if win != name or win.lower() in ("acli", "playwright-cli", "qa-mem") else name


def command_word(seg):
    """(index of the real program, wrapper names seen, env -C dir, env -S string)."""
    i, wrappers, env_dir, env_split, want_duration, positionals = 0, [], None, None, False, 0
    wrapper_at = []
    while i < len(seg):
        tok, name = seg[i], prog_name(seg[i])
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok) or (tok in SHELL_KEYWORDS and not wrappers):
            i += 1
        elif tok == "function" and not wrappers:  # `function name { body`
            i += 2
        elif tok == "--" and wrappers:
            i += 1
        elif tok.startswith("-") and wrappers:
            w = wrappers[-1]
            if w in ("node", "bun", "deno") and tok in ("-e", "-p", "--eval", "--print"):
                return wrapper_at[-1], wrappers[:-1], env_dir, env_split  # `node -e CODE`: node runs the code itself
            if w == "env" and tok in ("-C", "--chdir") and i + 1 < len(seg):
                env_dir, i = seg[i + 1], i + 2
            elif w == "env" and tok in ("-S", "--split-string") and i + 1 < len(seg):
                env_split, i = seg[i + 1], i + 2
            else:
                if tok in WRAPPERS[w] and w in WRAPPER_POSITIONALS:
                    positionals = 0  # the option took the positional value
                i += 2 if tok in WRAPPERS[w] else 1
        elif want_duration and re.match(r"^[\d.]+[smhd]?$", tok):
            i, want_duration = i + 1, False
        elif positionals:
            i, positionals = i + 1, positionals - 1
        elif name in PKG_SUBCOMMANDS and wrappers and wrappers[-1] in ("npm", "pnpm", "yarn", "bunx", "npx"):
            i += 1
        elif wrappers and wrappers[-1] == "perf" and name in ("stat", "record", "trace", "bench", "top"):
            i += 1  # `perf stat <cmd>`: a subcommand, not the `stat` tool
        elif name in WRAPPERS:
            wrappers.append(name)
            wrapper_at.append(i)
            want_duration = name == "timeout"
            positionals = WRAPPER_POSITIONALS.get(name, 0)
            i += 1
        else:
            return i, wrappers, env_dir, env_split
    return None, wrappers, env_dir, env_split


def pw_help(rest):
    """True when playwright-cli would only print help: -h/--help before `--` and not an option's value."""
    skip = False
    for k, a in enumerate(rest):
        if a == "--":
            return False
        if skip:
            skip = False
        elif a in ("--help", "-h"):
            if rest[k + 1:k + 2] != ["false"]:  # `--help false` switches help off: the command runs
                return True
        elif a.startswith("-"):
            skip = a in PW_VALUE_OPTS
    return False


def pw_args(rest):
    out, skip, flag = [], False, False
    for a in rest:
        if skip:
            skip = False
        elif a.startswith("-"):
            skip, flag = a in PW_VALUE_OPTS, "=" not in a
        elif flag and a in ("true", "false"):
            flag = False  # the CLI reads `--raw true` as the option's value, not as the command
        else:
            out.append(a)
            flag = False
    if out and prog_name(out[0]) == "playwright-cli":  # npx --package @playwright/cli playwright-cli ...
        out = out[1:]
    return out


def pw_file_args(rest, verb, args):
    """Paths playwright-cli would write or read: --filename values, video-start/upload/drop paths."""
    files = []
    for i, a in enumerate(rest):
        if a.startswith(("--filename=", "--path=")):
            files.append(a.split("=", 1)[1])
        elif a in ("--filename", "--path") and i + 1 < len(rest):
            files.append(rest[i + 1])
    if verb in ("video-start", "upload", "drop", "state-load", "state-save", "pdf"):
        files += args[1:]
    return files


def check_pw(rest, hosts, cwds, ctx, via_loop):
    args = pw_args(rest)
    verb = args[0] if args else ""
    if pw_help(rest):
        return None  # help text only
    ctx.runs_pw = True
    for a in rest:
        if a.split("=", 1)[0] in PW_SOURCE_OPTS:
            return (f"playwright-cli {a.split('=', 1)[0]} picks another browser, profile or config file; open the "
                    "browser with qa-mem browser-open [<url>]")
    if verb in PW_DENY:
        return f"playwright-cli {verb} blocked: {PW_DENY[verb]}"
    if verb == "show":
        binds = [a.split("=", 1)[1] for a in rest if a.startswith("--host=")]
        binds += [rest[k + 1] for k, a in enumerate(rest[:-1]) if a == "--host"]
        if any(h.strip("[]").lower() not in ("localhost", "127.0.0.1", "::1") for h in binds):
            return "playwright-cli show --host would expose the logged-in QA browser to the network; keep it on localhost"
    if ctx.explorer and verb == "eval" and PAGE_DUMP_RE.search(" ".join(args[1:])):
        return ("dumping the page's HTML reads app code and wastes tokens; use snapshot --depth=4 for structure, "
                "find, or eval on one element")
    if verb == "eval" and (SECRET_JS.search(" ".join(args[1:])) or storage_value_read(" ".join(args[1:]))):
        return ("playwright-cli eval reading cookies or web storage values blocked (they hold session tokens); "
                "to check what a page stores, read key names: Object.keys(localStorage)")
    if verb == "state-save":
        return ("playwright-cli state-save writes the session's cookies to a file; the login lives in the qa-sdet "
                "main profile (qa-mem browser-open), there is nothing to save")
    dropped = [a.split("=", 1)[1] for a in rest if a.startswith("--path=")] + [
        rest[k + 1] for k, a in enumerate(rest[:-1]) if a == "--path"] if verb == "drop" else []  # drop sends --path files
    for f in pw_file_args(rest, verb, args):
        sent = verb in ("upload", "drop") and (f in args[1:] or f in dropped)
        if sent and secret_upload(f, cwds):
            return SECRET_UPLOAD_REASON
        if ctx.explorer and not sent and verb != "state-load" and not run_output_ok(f, cwds, ctx.raw):
            return RUN_OUTPUT_REASON
        if touches_db(f, cwds, False, ctx.raw):  # `D=<data dir>; … --filename=$D/x.png` too
            return "playwright-cli writing into the qa-sdet memory DB directory is blocked"
        if verb != "state-load" and is_session_ref(f, cwds, ctx.raw):
            return "the saved browser session holds live cookies; only state-load may use it"
    for v in pw_profile_values(rest):
        if verb != "open" or not is_main_profile(v, cwds, ctx.raw):
            return ("playwright-cli --profile / --user-data-dir: only `open` with the qa-sdet main profile, "
                    "spelled out (run qa-mem browser-open [<url>]); other folders are blocked")
    if verb in NAV_VERBS:
        for target in args[1:]:  # every positional: unknown flags may shift which one is the URL
            if not host_ok(target, hosts):
                return f"playwright-cli {verb} to non-allowlisted host: {target} (allowed: {sorted(hosts)})"
        if len(args) == 1 and via_loop:
            return f"playwright-cli {verb} with a URL from xargs can't be verified; pass the URL directly"
    if verb in URL_SCAN_VERBS:
        text = " ".join(rest)
        for var in set(re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", text)):  # `R='js'; eval "$R"`: scan R's value
            text += " " + " ".join(m.group(1) for m in re.finditer(
                rf"(?<![A-Za-z0-9_]){var}=(\"[^\"]*\"|'[^']*'|\S+)", ctx.raw))
        hosts_ = [h for h in SCHEMELESS_URL_RE.findall(text) if "." in h or h.lower() == "localhost"]  # not XPath //td
        urls = [m.group(0) for m in URL_RE.finditer(text) if not compared_only(text, m.start(), m.group(0))]
        for url in urls + ["http://" + h for h in hosts_]:
            if not host_ok(url, hosts):
                return f"playwright-cli {verb} references non-allowlisted URL: {url}"
    if verb in FAULT_VERBS and not (ctx.marker and ctx.marker.get("faults") is True):
        return f"playwright-cli {verb} is fault injection; rerun the skill with --faults to allow it"
    if verb not in BUDGET_FREE:
        ctx.spend += 1
        ctx.loop |= via_loop or ctx.loop_depth > 0
    return None


def pw_profile_values(rest):
    """Values of --profile / --user-data-dir, in both `--opt value` and `--opt=value` forms."""
    vals = []
    for k, a in enumerate(rest):
        for opt in ("--profile", "--user-data-dir"):
            if a == opt:
                vals.append(rest[k + 1] if k + 1 < len(rest) else "")
            elif a.startswith(opt + "="):
                vals.append(a.split("=", 1)[1])
    return vals


def is_python(name):
    return re.fullmatch(r"(python|pypy)[0-9.]*", name) is not None


def script_word(rest):
    """The script an interpreter runs: its first non-option word (None for -c/-m/stdin)."""
    skip = False
    for a in rest:
        if skip:
            skip = False
        elif a in PY_VALUE_OPTS:
            skip = True
        elif a == "-" or (a.startswith("-") and not a.startswith("--") and re.search(r"[cm]", a[1:])):
            return None
        elif not a.startswith("-"):
            return a
    return None


def is_qamem(word, cwds=()):
    """Does this word name qa-mem, also as a shell glob that expands to it here (`~/.local/bin/qa-m*`, `qa-me?`)?"""
    if os.path.basename(word).lower() == "qa-mem":  # lower: `QA-MEM` runs it on a case-insensitive file system
        return True
    if not GLOB_RE.search(word) or "$" in word or "SUBST" in word or long_text(word):
        return False
    if "/" not in word and re.search(r"[a-z]", word) and any(fnmatch("qa-mem", a) for a in brace_alts(word)):
        return True  # a command word: the shell looks it up on PATH as well
    return any(os.path.basename(p) == "qa-mem" for c in cwds or (os.getcwd(),) for a in brace_alts(expand(word))
               if fnmatch("qa-mem", os.path.basename(a))  # only a pattern whose last part can be that name is listed
               for p in glob.glob(os.path.join(c, a)))


def qamem_args(name, rest, cwds=()):
    """Arguments after qa-mem when this command runs it (`qa-mem x`, `python3 -I /p/bin/qa-mem x`), else None."""
    if is_qamem(name, cwds):
        return rest
    if is_python(name):
        for k, a in enumerate(rest):
            if is_qamem(a, cwds):
                return rest[k + 1:]
    return None


def qamem_positionals(sub, rest):
    """Positionals as qa-mem reads them: only '--' words are options ('-x' is a positional, as in qa-mem's _opts)."""
    out, skip = [], False
    for a in rest:
        if skip:
            skip = False
        elif a in QAMEM_VALUE_OPTS.get(sub, ()):
            skip = True
        elif not a.startswith("--"):
            out.append(a)
    return out


def sets_pw_env(words, i, file_text=None):
    """Does this simple command set or export a variable that steers playwright-cli's browser? file_text (a sourced rc
    file on disk): a bare `local $2`, `typeset -ga ${x}` declares a variable there and sets nothing, unless the file
    gives that variable its value (`F=-x; declare $F $V` exports). A `$name=value` still counts: assigning to a name
    the shell already exports changes what playwright-cli sees."""
    if any(PW_ENV_RE.search(a) for a in words):
        return True
    if i is None:
        return False
    name, rest = prog_name(words[i]), words[i + 1:]
    if name == "set":  # `set -a`: every later assignment is exported
        return "allexport" in rest or any(re.match(r"^-[a-zA-Z]*a", a) for a in rest)
    if name in EXPORTERS:  # `export PLAYWRIGHT_MCP_CONFIG`, `export "$V"`
        bare_ok = bool(file_text) and name != "export" and not any(re.match(r"^-[a-zA-Z]*x", a) for a in rest)
        # shortcut: a value given by `for`, `read` or `set --` isn't seen; deny every variable argument if one is abused
        return any(PW_ENV_NAME_RE.match(n) or (("$" in n or "SUBST" in n) and not (
                       bare_ok and "=" not in a and var_variants(a, file_text) == [a]))
                   for a in rest if not a.startswith("-") for n in [a.split("=", 1)[0]])
    return False


def qa_bases(cwds):
    return {realpath(os.path.join(b, "qa-out")) for b in set(cwds) | {os.environ.get("CLAUDE_PROJECT_DIR")} if b}


def literal_path(path, cwds):
    """A path with [ ] { * ? that is a real folder name (a project called `app [staging]`), not a glob: its folder exists
    as written."""
    return all(os.path.isdir(os.path.dirname(os.path.join(c, expand(path)))) for c in cwds)


def evidence_png(path, cwds, raw=""):
    """Is the path a .png directly in <project>/qa-out/<run>/evidence/ (symlinks and ../ resolved)?"""
    if not path.endswith(".png"):
        return False
    for t in var_variants(path, raw):
        t = expand(t)
        if "$" in t or "SUBST" in t or (GLOB_RE.search(t) and not literal_path(t, cwds)):
            return False
        for c in cwds:
            p = realpath(os.path.join(c, t))
            ev = os.path.dirname(p)
            if not p.endswith(".png") or os.path.basename(ev) != "evidence" or \
                    os.path.dirname(os.path.dirname(ev)) not in qa_bases({c}):
                return False
    return True


def run_output_ok(path, cwds, raw=""):
    """Is the path a file directly in <project>/qa-out/<run>/evidence/ or <run>/.data/ (links and ../ resolved)?
    The explorer saves screenshots, snapshots and other browser output only there."""
    if not isinstance(path, str) or not path:
        return False
    for t in var_variants(path, raw):
        t = expand(t)
        if "$" in t or "SUBST" in t or (GLOB_RE.search(t) and not literal_path(t, cwds)):  # (after `${OUT}` is filled in)
            return False
        for c in cwds:
            d = os.path.dirname(realpath(os.path.join(c, t)))
            if os.path.basename(d) not in ("evidence", ".data") or os.path.dirname(os.path.dirname(d)) not in qa_bases({c}):
                return False
    return True


RUN_OUTPUT_REASON = ("the explorer saves browser output only in its run folder: <out>/evidence/BUG-<n>.png for bug "
                     "images, <out>/evidence/TC-<n>.png for case images, <out>/.data/ for map screenshots and snapshots "
                     "(absolute paths, out = <project>/qa-out/<run>)")
SECRET_UPLOAD_REASON = ("uploading this file would hand a secret (key, .env, credentials, qa-sdet data or config) to the "
                        "app; upload a test file you made for the run")


def glob_paths(path, c):
    """Real paths a word names in folder c: itself, or (a glob) what it expands to now (bounded)."""
    full = os.path.join(c, expand(path))
    hits = glob.glob(full)[:200] if GLOB_RE.search(path) and not long_text(path) else []
    return [realpath(x) for x in hits or [full]]


def secret_upload(path, cwds):
    return isinstance(path, str) and bool(path) and any(is_secret_path(p) for c in cwds for p in glob_paths(path, c))


def check_qamem(args, ctx, cwds, hosts, via_loop):
    sub = args[0] if args else ""
    if "$" in sub or "SUBST" in sub:
        return "the qa-mem subcommand comes from the shell ($VAR, $(...)); spell it out"
    if ctx.explorer and ((not sub and via_loop) or (sub and sub not in QAMEM_EXPLORER_OK)):
        return (f"qa-mem {sub or '(from xargs)'} belongs to the calling skill; the explorer runs only qa-mem "
                f"{', '.join(sorted(QAMEM_EXPLORER_OK))}")
    pos = qamem_positionals(sub, args[1:])
    if sub == "browser-open":
        sessions = [a.split("=", 1)[1] if "=" in a else (args[k + 2] if k + 2 < len(args) else "")
                    for k, a in enumerate(args[1:]) if a == "--session" or a.startswith("--session=")]
        if ctx.explorer and any(x != "qa" for x in sessions):
            return ("qa-mem browser-open --session qa-login is the sign-in window (it clears the sign-in record); "
                    "the explorer opens its own session: qa-mem browser-open '<target>'")
        for url in pos:
            if not host_ok(url, hosts):
                return f"qa-mem browser-open to non-allowlisted host: {url} (allowed: {sorted(hosts)})"
        if not pos and via_loop:
            return "qa-mem browser-open with a URL from xargs can't be verified; pass the URL directly"
        ctx.spend += 1  # it opens the browser: one budgeted command, like playwright-cli open
        ctx.runs_pw = True
        ctx.loop |= via_loop or ctx.loop_depth > 0
    if sub == "annotate":
        for png in pos:
            if not evidence_png(png, cwds, ctx.raw):
                return f"qa-mem annotate edits only <project>/qa-out/<run>/evidence/*.png, not {png}"
    return None


_KEY_READ_RE = _LazyRe(r"(Object\.keys\(\s*(window\.)?|\bin\s+(window\.)?|typeof\s+(window\.)?)$")
_QUOTE_END_RE, _BRACKET_QUOTE_END_RE = _LazyRe(r"['\"`]$"), _LazyRe(r"\[\s*['\"`]$")


def storage_value_read(js):
    """Any use of localStorage/sessionStorage except key names, .length and writes: returning the Storage
    object prints every value, and `localStorage.token` reads one."""
    for m in re.finditer(r"\b(local|session)Storage\b", js):
        # Look back at most 200 chars (pos/endpos keep \b honest at the window edge): a scan of the whole
        # prefix per match made long evals quadratic. More than 200 spaces before the word just counts as a read.
        lo, after = max(0, m.start() - 200), js[m.end():m.end() + 200]
        if _KEY_READ_RE.search(js, lo, m.start()):
            continue  # key names / existence checks
        if re.match(r"\s*\.\s*(length|key|clear|setItem|removeItem)\b", after):
            continue
        if _QUOTE_END_RE.search(js, lo, m.start()) and re.match(r"['\"`]", after) and \
                not _BRACKET_QUOTE_END_RE.search(js, lo, m.start()):
            continue  # the word inside a string literal, e.g. innerText.includes('localStorage'); not window['localStorage']
        return True
    return False


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
        out.append("list" if a == "type" and positional[k - 1:k] == ["link"] else a)
    return out


COPY_VALUE_OPTS = {"-t", "--target-directory", "-e", "--rsh", "-P", "-i", "-F", "-o", "-f", "--file", "-C",
                   "--directory", "--exclude", "--include", "-T", "--files-from"}


def copy_sources(name, rest):
    """(paths a copy/archive command reads, folder they are relative to). Destinations and extractions read
    nothing worth guarding: `cp -r x ~/.config/`, `tar -xzf a.tgz -C ~` are fine."""
    pos, base, skip, archive_next = [], None, None, False
    if name in ("tar", "bsdtar") and rest and not rest[0].startswith("-") and re.fullmatch(r"[A-Za-z]+", rest[0]):
        rest = ["-" + rest[0]] + rest[1:]  # old style `tar czf out.tgz dir`
    short = "".join(a[1:] for a in rest if re.match(r"^-[A-Za-z]+$", a))
    if name in ("tar", "bsdtar") and ({"-x", "--extract", "--get", "-t", "--list"} & set(rest) or re.search(r"[xt]", short)):
        return [], None
    for a in rest:
        if skip:
            if skip in ("-C", "--directory"):
                base = a
            skip = None
        elif archive_next:
            archive_next = False  # tar's bundled `f`: the next word is the archive
        elif a.startswith("--") and "=" in a:
            if a.startswith("--directory="):
                base = a.split("=", 1)[1]
        elif a in COPY_VALUE_OPTS:
            skip = a
        elif a.startswith("-"):
            archive_next = name in ("tar", "bsdtar") and re.match(r"^-[A-Za-z]*f$", a) is not None
        else:
            pos.append(a)
    if name in ("cp", "scp", "rsync"):
        return (pos if {"-t", "--target-directory"} & set(rest) else pos[:-1]), None
    if name == "zip":
        return pos[1:], None
    if name in ("7z", "7za"):
        return pos[2:], None
    if name == "rclone":
        return pos[1:-1], None
    if name in ("cpio", "pax"):
        return [], None  # they read file names from stdin
    return pos, base


XML_NAMESPACES = ("http://www.w3.org/2000/svg", "http://www.w3.org/1999/xhtml", "http://www.w3.org/1999/xlink",
                  "http://www.w3.org/2000/xmlns/", "http://www.w3.org/XML/1998/namespace", "http://www.w3.org/1998/Math/MathML")


def compared_only(js, pos, url):
    """A URL used as data, not fetched: an XML namespace, or the value in a string comparison
    (`a.href.startsWith('https://docs.x/')`, `=== 'https://...'`)."""
    if url.startswith(XML_NAMESPACES):
        return True
    return re.search(r"(\.(startsWith|endsWith|includes|indexOf|lastIndexOf)\s*\(|[!=]==?)\s*['\"`]$", js[max(0, pos - 40):pos]) is not None


def interpreter_code(rest, docs):
    """The inline code an interpreter will run: `-c`/`-e`/`-p`/`-r`/`--eval CODE`. Plain arguments are data for a
    script file (a session path there is caught as a path). shortcut: scripts on stdin (`python3 - <<EOF`) aren't
    read, because plugin development mentions qa-state.json in such scripts constantly; README Limits covers scripts."""
    for k, a in enumerate(rest[:-1]):
        if a in ("-c", "-e", "-p", "-r", "--eval", "--print", "eval"):
            return rest[k + 1]
    return ""


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


def touches_marker(name, rest, outs, cwds, markers, raw=""):
    """Would this command change or remove the run marker? Paths, globs, and folders that contain it."""
    reason = ("the run marker and the qa-sdet config belong to the calling skill and the user: read them if you like, "
              "but leave them unchanged and carry on testing")
    opts = set(rest)
    removes_folders = (name in ("mv", "rmdir", "trash", "trash-put", "gio")  # only these can take the folder with them
                       or (name == "rm" and any(re.match(r"^-[a-zA-Z]*[rR]", a) or a == "--recursive" for a in rest))
                       or (name == "find" and ("-delete" in opts or any(
                           prog_name((shlex.split(x) or [""])[0]) in REMOVERS for x in find_exec(rest))))
                       or (name == "rsync" and any(a.startswith(("--delete", "--remove-source")) for a in rest)))
    read_only = (name in READ_ONLY_TOOLS or (name == "find" and not removes_folders)  # `find -newer qa-out/.active`
                 or (name == "sed" and not any(x.startswith(("-i", "--in-place")) for x in rest)))  # `sed -n 1,5p`
    args = [] if read_only else [x.split("=", 1)[1] if re.match(r"^(-|[a-z]+=)", x) and "=" in x else x
                                 for x in rest]  # `--target=x`, dd's `of=x`
    if name in ("cp", "rsync", "scp") and not any(x.startswith("--remove-source") for x in rest):  # copies leave sources unchanged
        args = [x for x in args if x not in set(copy_sources(name, rest)[0])]
    # Copies and extractions into the marker's folder (or a folder above it) can put a forged marker there.
    into_folders = name in ("cp", "install", "ln", "rsync", "scp", "rclone", "ditto")
    if extracts(name, rest):
        into_folders, args = True, [extract_dir(name, rest)]
    removes_folders |= into_folders
    embedded = [m.group(1) for x in rest if (m := re.search(r">>?([^\s>]+)$", x))]  # `printf x>qa-out/.active`
    for a in [v for x in args + outs + embedded for v in var_variants(x, raw)]:  # `X=qa-out/.active; rm $X`
        if a.startswith("-") or CODE_TEXT_RE.search(a) or (len(a) > 512 and re.search(r"\s", a)):  # long text, not a path
            continue
        for c in cwds:
            t = expand(a)
            if GLOB_RE.search(t):
                parents = {m for m in markers for p in glob_hits(t, c, {os.path.dirname(m)}) if p} if removes_folders else set()
                if glob_hits(t, c, markers) or parents:
                    return reason
                continue
            p = realpath(os.path.join(c, t))
            if p in markers or (removes_folders and any(m.startswith(p.rstrip(os.sep) + os.sep) for m in markers)):
                return reason
    return None


def extracts(name, rest):
    """Does this archiver unpack files (tar x, unzip, 7z x/e, cpio -i)?"""
    if name in ("tar", "bsdtar"):
        first = next((a for a in rest if a != "--"), "")
        return any(a in ("--extract", "--get") or re.match(r"^-[A-Za-z]*x", a) for a in rest) or (
            not first.startswith("-") and "x" in first)  # `tar xf a.tar`
    if name in ("7z", "7za"):
        return next((a for a in rest if not a.startswith("-")), "") in ("x", "e")
    if name == "cpio":
        return any(a in ("--extract",) or re.match(r"^-[A-Za-z]*i", a) for a in rest)
    return name == "unzip"


def extract_dir(name, rest):
    """Where an extraction lands: tar -C / --directory, unzip -d, 7z -o<dir>; otherwise the current folder."""
    for k, a in enumerate(rest):
        if name in ("tar", "bsdtar") and a in ("-C", "--directory") and k + 1 < len(rest):
            return rest[k + 1]
        if name in ("tar", "bsdtar") and a.startswith("--directory="):
            return a.split("=", 1)[1]
        if name == "unzip" and a == "-d" and k + 1 < len(rest):
            return rest[k + 1]
        if name in ("7z", "7za") and a.startswith("-o") and len(a) > 2:
            return a[2:]
    return "."


_db = touches_db


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
        nxt = positional[positional.index("auth") + 1:positional.index("auth") + 2]
        if nxt and nxt[0] in ACLI_AUTH_OK:
            return None
    return "acli write command blocked: qa-sdet only reads Jira and drafts content; the user files it"


def check_db(words, outs, name, rest, cwds, raw=""):
    touches_db = lambda a, c, parent: _db(a, c, parent, raw)
    embedded = [m.group(1) for w in words if (m := re.search(r">>?(\S+)$", w))]  # `echo x>file`
    for target in outs + embedded:
        if touches_db(target, cwds, False):
            return "redirecting output onto the qa-sdet memory DB is blocked"
    opts = [a for a in rest if a.startswith("-")]
    paths = [a for a in rest if not a.startswith("-")]
    recursive = any(re.match(r"^-[a-zA-Z]*[rR]", o) or o == "--recursive" for o in opts)
    if extracts(name, rest) and touches_db(extract_dir(name, rest), cwds, False):
        return f"{name} unpacking into the qa-sdet memory DB directory is blocked (it can overwrite the DB)"
    if name == "find":
        start = []
        for a in rest:
            if a.startswith("-") or a in ("(", "!"):
                break
            start.append(a)
        names = [(a, rest[k + 1]) for k, a in enumerate(rest[:-1]) if a in ("-name", "-iname")]
        parent = not names or any(fnmatch(f.lower(), n.lower()) if o == "-iname" else fnmatch(f, n)
                                  for o, n in names for f in db_names())  # `-iname '*.DB'` matches memory.db
        if {"-delete", "-exec", "-execdir", "-ok", "-okdir"} & set(rest) and any(
                touches_db(a, cwds, parent) for a in start):
            return "find -delete/-exec on the qa-sdet memory DB directory is blocked"
        return None
    if name == "dd":
        paths = [a[3:] for a in rest if a.startswith("of=")]
    if name == "mv":  # moving the DB folder (or a parent) away destroys it; moving files into ~ doesn't
        tdir = [rest[k + 1] for k, a in enumerate(rest[:-1]) if a in ("-t", "--target-directory")]
        tdir += [a.split("=", 1)[1] for a in rest if a.startswith("--target-directory=")]
        sources, dests = (paths, tdir) if tdir else (paths[:-1], paths[-1:])
        if any(touches_db(a, cwds, True) for a in sources) or any(touches_db(a, cwds, False) for a in dests):
            return "mv on the qa-sdet memory DB directory is blocked (only qa-mem writes it)"
    elif name in DB_DESTRUCTIVE:
        if any(touches_db(a, cwds, name != "rm" or recursive) for a in paths):
            return f"{name} on the qa-sdet memory DB directory is blocked (only qa-mem writes it)"
    if name in INPLACE_EDITORS and any(re.match(r"^-[a-zA-Z]*i", o) or o.startswith("--in-place") for o in opts) \
            and any(touches_db(a, cwds, False) for a in paths):  # sed -i / perl -pi rewrite the file in place
        return f"{name} -i on the qa-sdet memory DB is blocked (only qa-mem writes it)"
    if name in DB_WRITERS:
        targets = paths if name == "tee" else paths[-1:]
        for i, a in enumerate(rest):
            if a in ("-t", "--target-directory") and i + 1 < len(rest):
                targets.append(rest[i + 1])
            elif a.startswith("--target-directory="):
                targets.append(a.split("=", 1)[1])
        mirror = name == "rsync" and any(a.startswith("--delete") for a in rest)  # deletes what the source lacks
        if any(touches_db(t, cwds, mirror) for t in targets):
            return f"{name} onto the qa-sdet memory DB is blocked"
    if name == "sqlite3" and "-readonly" not in rest and any(touches_db(a, cwds, False) for a in paths):
        return "sqlite3 on the qa-sdet DB must use -readonly (only qa-mem writes it)"
    return None


def in_profile_area(p):
    """A real path that is the qa-sdet data folder, inside it, or a folder between home and a profile folder."""
    home = realpath(os.path.expanduser("~"))
    return p == DATA_DIR or p.startswith(DATA_DIR + os.sep) or (
        p.startswith(home + os.sep) and any(r.startswith(p.rstrip(os.sep) + os.sep) for r in profile_roots()))


# Secret stores and keys: never read by the qa-mapper, never uploaded to the app. Same pattern as qa-mem's
# codemap-scan SECRET_FILE_RE (tests keep them equal), plus home folders that hold credentials.
SECRET_FILE_RE = _LazyRe(r"(?i)^\.env|\.(pem|key|p12|pfx|jks|keystore|crt|cer|kdbx)$|^secrets?\.(json|ya?ml|env|"
                            r"properties|ini|toml|txt|xml)$|^credentials?\.(json|xml|ya?ml|ini|toml|txt)$|"
                            r"^id_(rsa|ed25519|ecdsa|dsa)|^\.(npmrc|netrc|pgpass|pypirc|dockercfg)$|^appsettings\.\w+\.json$|"
                            r"^secrets?\.(py|rb|php|go|java|kt|cs)$|^credentials?\.(py|rb|php|go)$|^wp-config\.php$")
SECRET_HOME_PATHS = (".ssh", ".aws", ".azure", ".config/gcloud", ".kube", ".docker", ".gnupg", ".password-store",
                     ".config/gh", ".git-credentials", ".claude/.credentials.json", ".local/share/keyrings",
                     ".config/playwright-mcp", ".mozilla", ".config/google-chrome", ".config/chromium")
SCAN_SKIP = {"node_modules", ".git", "dist", "build", "vendor", "__pycache__", ".venv", "venv", "target", ".next",
             "coverage", "qa-out"}


@_memo
def secret_roots():
    home = realpath(os.path.expanduser("~"))
    roots = {realpath(os.path.join(home, p)) for p in SECRET_HOME_PATHS}
    return roots | {DATA_DIR, realpath(CONFIG.parent)} | profile_dirs()


def is_secret_path(p):
    """A real path that is a key/secret file by name, or sits in a credentials / qa-sdet data or config folder."""
    return bool(SECRET_FILE_RE.search(os.path.basename(p))) or any(
        p == r or p.startswith(r.rstrip(os.sep) + os.sep) for r in secret_roots())


# ripgrep's file types (rg --type-list) for the source languages a mapper searches. The Grep tool's `type` narrows
# the search like a glob; a type not listed here counts as no narrowing (sh selects .env, java secrets.properties).
RG_TYPES = {"js": "*.{cjs,js,jsx,mjs,vue}", "ts": "*.{cts,mts,ts,tsx}", "vue": "*.vue", "svelte": "*.{svelte,svelte.ts}",
            "html": "*.{ejs,htm,html}", "css": "*.{css,scss}", "py": "*.{py,pyi}", "cs": "*.cs", "csharp": "*.cs",
            "go": "*.go", "rust": "*.rs", "kotlin": "*.{kt,kts}", "swift": "*.swift", "dart": "*.dart",
            "php": "*.{php,php3,php4,php5,php7,php8,pht,phtml}", "ruby": "*.{gemspec,rake,rb,rbw}", "scala": "*.{sbt,scala}",
            "graphql": "*.{graphql,graphqls}", "protobuf": "*.proto", "md": "*.{md,mdx,markdown}"}


EXPLORER_WRITERS = {"cp", "tee", "install", "ln", "rsync", "dd", "truncate", "touch", "mkdir", "chmod", "chown"}
EXPLORER_WRITE_REASON = ("the explorer changes files only in its run folder's bugs/, repro/, evidence/ and .data/ "
                         "(<project>/qa-out/<run>/…); plan.json, reports and the rest of the project are the user's")


EXPLORER_VALUE_OPTS = {"truncate": {"-s", "--size", "-r", "--reference"}, "touch": {"-d", "-t", "-r", "--date", "--reference"},
                       "install": {"-m", "-o", "-g", "--mode", "--owner", "--group"}}  # `truncate -s 0 f`; but `rm -r dir` and `touch -m f` take none


def explorer_outside(name, rest, outs, cwds, raw, via_loop=False):
    """Does this explorer command write, change or remove a file outside <project>/qa-out (or /dev/null)?"""
    writer = name in REMOVERS | EXPLORER_WRITERS
    inplace = (name in INPLACE_EDITORS and any(re.match(r"^-[a-zA-Z]*i", a) or a.startswith("--in-place") for a in rest)) or (
        name in ("awk", "gawk") and "inplace" in rest)
    if via_loop and (writer or inplace):
        return True  # `find … | xargs rm`: the files come from the pipe
    targets = list(outs) + [m.group(1) for x in rest if (m := re.search(r">>?([^\s>]+)$", x)) and not x.startswith(">")]
    pos, skip = [], False
    for a in rest:  # positionals, without the values of `truncate -s 0`, `touch -d yesterday`, `chmod --reference f`
        if skip:
            skip = False
        elif a.startswith("-"):
            skip = a in EXPLORER_VALUE_OPTS.get(name, ())
        else:
            pos.append(a)
    if name == "dd":
        targets += [a[3:] for a in rest if a.startswith("of=")]
    elif name in ("cp", "install", "ln", "rsync"):
        targets += pos[-1:] + [rest[k + 1] for k, a in enumerate(rest[:-1]) if a in ("-t", "--target-directory")]
    elif name in ("chmod", "chown"):  # the first word is the mode or the owner, unless --reference gives them
        targets += pos if any(a.startswith("--reference") for a in rest) else pos[1:]
    elif writer:
        targets += pos
    elif inplace:
        targets += [a for a in pos for c in cwds if os.path.lexists(os.path.join(c, expand(a)))]  # files, not the script
    elif name == "find":
        progs = {prog_name((shlex.split(x) or [""])[0]) for x in find_exec(rest)}
        if "-delete" in rest or progs & (REMOVERS | EXPLORER_WRITERS | INPLACE_EDITORS):
            roots = []
            for a in rest:
                if a.startswith("-") or a in ("(", "!"):
                    break
                roots.append(a)
            targets += roots or ["."]
    elif extracts(name, rest):
        targets.append(extract_dir(name, rest))
    elif name in ("tar", "bsdtar"):  # creating an archive: the file after f
        targets += [rest[k + 1] for k, a in enumerate(rest[:-1]) if re.match(r"^-?[A-Za-z]*f$", a) and (k == 0 or a.startswith("-"))]
        targets += [a.split("=", 1)[1] for a in rest if a.startswith("--file=")]
    elif name in ("zip", "7z", "7za"):
        targets += pos[1:2] if name != "zip" else pos[:1]
    elif name == "sort":
        targets += [rest[k + 1] for k, a in enumerate(rest[:-1]) if a == "-o"] + [a.split("=", 1)[1] for a in rest if a.startswith("--output=")]
    bases = qa_bases(cwds)
    for a in targets:
        for t in [alt for v in var_variants(a, raw) for alt in brace_alts(expand(v))]:  # `mkdir -p <out>/{bugs,repro}`
            if "$" in t or "SUBST" in t:
                return True
            for c in cwds:
                plain = os.path.normpath(os.path.join(c, t))  # before links are followed: /dev/stderr links into /proc
                if plain in ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty") or plain.startswith("/dev/fd/"):
                    continue
                p = realpath(plain)
                if not any(explorer_run_path(p, b) or (name == "mkdir" and os.path.dirname(p) == b) for b in bases):
                    return True  # (mkdir of the run folder itself is fine: it only makes what is missing)
    return False


def explorer_run_path(p, base):
    """<qa-out>/<run>/{bugs,repro,evidence,.data}[/…]: the run's own files, not plan.json, a report, the index or the run
    folder itself (the same set the Write tool gets)."""
    if not p.startswith(base + os.sep):
        return False
    rel = p[len(base) + 1:].split(os.sep)
    return len(rel) >= 2 and rel[1] in ("bugs", "repro", "evidence", ".data") and rel[-1] != "plan.json" \
        and not rel[-1].endswith((".ingested", ".error"))


def secret_below(d, glob_="", type_=""):
    """Does a content search from folder d reach a secret (a secret folder inside it, or a secret-named file the
    glob, or else the ripgrep type, can select)? Fails closed on huge trees."""
    base = d.rstrip(os.sep) + os.sep
    if any(r.startswith(base) for r in secret_roots()):
        return True
    if not glob_ and type_:
        glob_ = RG_TYPES.get(type_, "")
    if glob_.startswith("!"):  # `!*.ts` leaves files out; what is left still holds the secret files
        glob_ = ""
    alts, seen = brace_alts(glob_) if glob_ else [], 0
    # as ripgrep reads them: a leading `/` anchors at the search folder, and `**/` also matches no folder at all
    alts = [a.lstrip("/") for a in alts]
    for _ in range(4):
        alts += [b for a in alts for b in (a[3:] if a.startswith("**/") else a, a.replace("/**/", "/", 1)) if b not in alts]
    for root, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x not in SCAN_SKIP]
        seen += len(files) + len(dirs)
        if seen > 50000:
            return True
        for f in files:
            if SECRET_FILE_RE.search(f) and (not alts or any(
                    fnmatch(f, a) or fnmatch(os.path.relpath(os.path.join(root, f), d), a) for a in alts)):
                return True
    return False


def names_profile_area(raw, cwds, cache={}):
    key = (raw, frozenset(cwds), DATA_DIR)  # asked once per segment: the answer depends on the whole command only
    if key not in cache:
        cache.clear()
        cache[key] = _names_profile_area(raw, cwds)
    return cache[key]


def _names_profile_area(raw, cwds):
    """Does the command name the profile folder, the qa-sdet data folder (or a path in it), or a folder between
    home and it (`find ~/.local/share -name Cookies`)? Paths built at runtime from such a word may land in it."""
    if mentions_profile(raw):
        return True
    for w in re.findall(r"[^\s'\"`();|&<>=]+", raw)[:300]:
        for t in var_variants(w, raw):
            t = expand(t)
            if "$" not in t and not t.startswith("-") and any(in_profile_area(realpath(os.path.join(c, t)))
                                                               for c in cwds):
                return True
    return False


def dynamic_word(a, raw):
    """A path whose value the shell computes ($(...), `...`, ${V:-x}, a variable not set in the command)."""
    return any("$" in expand(t) or "SUBST" in t for t in var_variants(a, raw))


def check_profile(name, args, rest, outs, ins, cwds, ctx, via_loop=False, docs=()):
    """Deny a command that names a path in the browser profile folder (except a playwright-cli --profile value
    that is the main profile: check_pw decides that), runs inside it, or lists the DB folder recursively."""
    if name in PROFILE_OK:
        return None
    sanctioned = {v for v in pw_profile_values(rest) if is_main_profile(v, cwds, ctx.raw)} if name == "playwright-cli" else set()
    cands = [a for a in args + outs + ins if a not in sanctioned
             and not (a.startswith(("--profile=", "--user-data-dir=")) and a.split("=", 1)[1] in sanctioned)]
    if name in GREP_TOOLS and not {"-e", "-f", "--regexp", "--file"} & set(rest):
        first = next((a for a in cands if not a.startswith("-")), None)
        cands = [a for a in cands if a is not first]  # `grep -n "<profile path>" README.md`: a pattern, not a file
    roots = profile_roots()
    if any(c == r or c.startswith(r + os.sep) for c in cwds for r in roots):  # `cd <profile> && cat *`
        return PROFILE_REASON
    if any(in_profile_root(a, cwds, ctx.raw) for a in cands):
        return PROFILE_REASON
    if name not in CODE_TEXT_OK and any(NOT_A_PATH_RE.search(t) and mentions_profile(t)
                                        for a in cands for t in var_variants(a, ctx.raw)):
        return PROFILE_REASON  # sqlite3 x.db "ATTACH '<profile>/Default/Cookies' AS c", awk getline < "<profile>/..."
    if name in STDIN_SCRIPT_TOOLS and any(mentions_profile(d) for d in docs):
        return PROFILE_REASON  # the same script in a heredoc
    if name == "ln":  # `ln -s <data dir> /tmp/q && cat /tmp/q/profiles/...`: the link doesn't exist yet when checked
        pos = [a for a in rest if not a.startswith("-")]
        srcs = pos if {"-t", "--target-directory"} & set(rest) or any(a.startswith("--target-directory=") for a in rest) \
            else pos[:-1]
        if any(any(r == p or r.startswith(p.rstrip(os.sep) + os.sep) for r in roots)
               for a in srcs for c in cwds for p in [realpath(os.path.join(c, expand(a)))]):
            return PROFILE_REASON
    if name == "git":  # `git -C <data dir> add -A`, `cd ~/.local/share && git add qa-sdet`: git reads whole trees
        dirs = [a for a in rest if not a.startswith("-")] + [rest[k + 1] for k, a in enumerate(rest[:-1]) if a == "-C"]
        dirs += [a.split("=", 1)[1] for a in rest if a.startswith(("--work-tree=", "--git-dir="))]
        if any(in_profile_area(c) for c in cwds) or any(in_profile_area(realpath(os.path.join(c, expand(a))))
                                                         for a in dirs for c in cwds):
            return PROFILE_REASON
    reads_list = via_loop or name in LIST_READERS or bool(LIST_READER_OPTS & {a.split("=", 1)[0] for a in rest})
    if name not in CODE_TEXT_OK and (reads_list or any(dynamic_word(a, ctx.raw) for a in cands if not NOT_A_PATH_RE.search(a))) \
            and names_profile_area(ctx.raw, cwds):  # `cat $(echo <profile>)/...`, `find <data dir> | xargs cat`
        return PROFILE_REASON
    listing = (name == "tree" or (name == "ls" and any(re.match(r"^-[a-zA-Z]*R", a) or a == "--recursive" for a in rest))
               or (name == "du" and any(re.match(r"^-[a-zA-Z]*a", a) or a == "--all" for a in rest)))
    if listing and any(realpath(os.path.join(c, expand(a))) == DATA_DIR
                       for a in [x for x in args if not x.startswith("-")] or ["."] for c in cwds):
        return PROFILE_REASON  # `ls -R ~/.local/share/qa-sdet` lists the profile's files
    return None


def find_exec(rest):
    """Commands run by find -exec/-execdir/-ok."""
    cmds, cur = [], None
    for a in rest:
        if a in ("-exec", "-execdir", "-ok", "-okdir"):
            cur = []
        elif cur is not None and a in (";", "+", "\\;"):
            cmds.append(" ".join(shlex.quote(x) for x in cur))
            cur = None
        elif cur is not None:
            cur.append(a)
    if cur:
        cmds.append(" ".join(shlex.quote(x) for x in cur))
    return cmds


@_one_decision
def check(cmd, cwd, ctx=None, depth=0, explorer=False):
    """Deny reason for a Bash command, or None. At the top level a passing command is charged to the
    active run's budget (only then: a denied command spends nothing)."""
    top = ctx is None
    if top:
        _MEMO.clear()
    ctx = ctx or Ctx(cwd, explorer)
    ctx.raw = ctx.raw or cmd
    reason = _check(cmd, cwd, ctx, depth)
    if top and not reason and ctx.pw_env and ctx.runs_pw:
        reason = ("setting PLAYWRIGHT_MCP_* / PWTEST_* variables changes which browser or profile playwright-cli uses; "
                  "open the browser with qa-mem browser-open [<url>] and no extra variables")
    if top and not reason and ctx.spend and ctx.explorer and not ctx.marker_path:
        return NO_RUN
    if top and not reason and ctx.spend and ctx.marker_path:
        if ctx.marker_error:
            return ctx.marker_error
        if ctx.marker:
            if ctx.loop:
                return ("playwright-cli in a loop, xargs or watch can't be counted against the run budget; "
                        "run commands one by one, or batch input values in one eval")
            reason = spend_budget(ctx.marker_path, ctx.spend, ctx.explorer)
    return reason


def _check(cmd, cwd, ctx, depth, carried=()):
    if depth > 4:  # deeper is not read: that matters only when something guarded is in play
        whole = (getattr(ctx, "scope", None) or ctx.raw or cmd)[:200000]  # in a sourced rc file: that file, not the caller
        deep = (GUARDED_ANY_RE.search(cmd) or GUARDED_TOOLS_RE.search(whole) or mentions_data_dir(whole)
                or mentions_data_dir(cmd) or mentions_session_path(whole) or ctx.explorer)
        return "nested shell too deep to verify" if deep else None
    cmd = re.sub(r"\\\n", "", cmd)  # line continuations: bash joins the lines (`play\<newline>wright-cli`)
    if len(cmd) - len(cmd.rstrip("\\")) & 1:  # a lone backslash at the very end: bash drops it, shlex refuses the command
        cmd = cmd[:-1]
    if ctx.mentions is None:  # whole call: `PW=playwright-cli; $($PW goto x)`
        ctx.mentions = bool(GUARDED_TOOLS_RE.search(ctx.raw or cmd))
    mentions = ctx.mentions
    cmd, docs = split_heredocs(cmd)
    docs += carried  # a launcher's re-check (`chrt 0 bash <<EOF`): the bodies were split off the whole command
    bodies, cmd = substitutions(normalize(cmd))
    for body in bodies:
        if r := _check(body.replace(QUOTED_GT, ">").replace(QUOTED_LT, "<"), cwd, ctx, depth + 1):
            return r
    try:
        toks = tokens(cmd)
    except ValueError:
        if GUARDED_ANY_RE.search(cmd) or DATA_DIR in expand(cmd) or mentions_data_dir(cmd) or mentions_session_path(cmd):
            return "could not parse a command that mentions a guarded tool, file or the DB directory"
        return None
    hosts = allowed_hosts()
    cwds, stack, seg = {cwd}, [], []
    for tok in toks + [";"]:  # sentinel flushes the last segment
        if tok and set(tok) <= set(PUNCT):
            if seg:
                if r := check_segment(seg, cwds, cwd, hosts, docs, depth, mentions, ctx):
                    return r
                seg = []
            for ch in tok:  # subshells: a cd inside ( ) doesn't leak out
                if ch == "(":
                    stack.append(set(cwds))
                elif ch == ")" and stack:
                    cwds = stack.pop()
        else:
            seg.append(tok)
    return None


def read_script(path, cwds, raw="", strict=False):
    """Text of the script file a shell or interpreter would run (None when it can't be known now). `$HOME/.bashrc` and
    `$NVM_DIR/nvm.sh` after `NVM_DIR=…` in the same command are read like any other path. strict (the explorer): a
    missing file is unknown too, since `python3 -c "open(…).write(…)"` can make it without a `>` or a copy tool."""
    paths = [expand(t) for t in var_variants(path, raw)]
    if "SUBST" in path or any("$" in t or GLOB_RE.search(t) for t in paths):
        return None
    reals = [realpath(os.path.join(c, t)) for c in cwds for t in paths]
    if not strict and not any(os.path.lexists(p) for p in reals) and not makes_file(
            raw, {os.path.basename(t) for t in paths + reals}):  # (a dangling link's target too)
        return ""  # `. "$HOME/.cargo/env"` where there is none, and nothing in the command makes it: nothing runs
    texts = []
    for p in reals:
        try:
            if not os.path.isfile(p) or os.path.getsize(p) > 1 << 20:
                return None
            with open(p, encoding="utf-8", errors="replace") as f:
                texts.append(f.read())
        except OSError:
            return None
    return "\n".join(texts)


def shell_script_texts(name, rest, seg, ins, cwds, docs, ctx):
    """(scripts a shell run without -c reads, why the rest is unknown: None | "file" | "stdin")."""
    raw, strict = scope_text(ctx), ctx.explorer
    if name == "busybox":
        name, rest = rest[0], rest[1:]
    here = []  # here-strings: `bash <<<'cmd'` (split_redirects drops them)
    for k, t in enumerate(seg):
        m = REDIRECT_RE.match(t)
        if m and m.group(2) == "<<<":
            here.append(unmark(m.group(3) or (seg[k + 1] if k + 1 < len(seg) else "")))
    script, skip = None, False
    if name not in ("su", "script", "at", "batch"):  # their positionals are a user / a typescript file / a time
        for a in rest:
            if skip:
                skip = False
            elif a in ("-o", "+o", "-O", "+O"):
                skip = True
            elif a == "-s":
                break  # the script comes from stdin; the rest are its arguments
            elif a == "/dev/stdin":
                break  # `source /dev/stdin <<< 'cmd'`: the script is on stdin too
            elif not a.startswith(("-", "+")):
                script = a
                break
    if script is not None:
        text = read_script(script, cwds, raw, strict)
        return ([text] if text else []), (None if text is not None else "file")
    texts, unknown = here + list(docs), None
    for f in ins:
        text = read_script(f, cwds, raw, strict)
        texts += [text] if text else []
        unknown = unknown or (None if text is not None else "file")
    if not here and not ins and not any(REDIRECT_RE.match(t) and REDIRECT_RE.match(t).group(2) == "<<" for t in seg):
        unknown = unknown or "stdin"  # a pipe: `echo 'cmd' | bash`
    return texts, unknown


# a package manager's arguments are package names (`apt-get install tar zsh`), not file tools at work; a `$(...)` or
# backquote in them still runs
PKG_CMD_RE = _LazyRe(r"\b(?:npm|pnpm|yarn|bun|pip3?|pipx|uv|apt(?:-get)?|brew|dnf|yum|zypper|pacman|apk|gem|cargo|go|"
                        r"composer|poetry|snap|flatpak|port|choco|scoop|winget)[ \t]+(?:install|add|i|-S\w*)\b[^;&|\n$`<>()'\"]*")
# (it stops at a quote: in `A='npm i' tee x.sh` the package manager is data and `tee` is the command)
_WRITES = {}


def makes_file(raw, names):
    """Could this command create one of these files? An output redirection to it, to a quoted, escaped or computed path,
    or a file tool or download (`tar x`, `cp a b`, `| tee f`, `curl -o f`) outside a package manager's arguments.
    Not `2>/dev/null` or `2>&1`. Worked out once per command text (an rc file may source dozens of missing files)."""
    if _WRITES.get("raw") is not raw:
        # normalize: a quoted `>` (`eval 'x => x'`) is data, and `2>&1` / `>&f` / `&>f` / `>|f` come out as `>` FD_DUP `1` / `>` FD_DUP `f` / `>f`.
        # Heredoc bodies stay as written: a shell may run one, so every `>` and file tool in them counts.
        cmd, docs = split_heredocs(raw.replace("\\\n", ""))
        text, targets, any_target = "\n".join([normalize(cmd), *docs]), set(), False
        for m in re.finditer(r">>?[ \t]*([^\s;&|()<>]+)", text):  # also `1<>f` and `y=>f`
            t = m.group(1)
            if t.startswith(FD_DUP):
                if re.fullmatch(r"\d+-?|-", t[1:]):
                    continue  # `2>&1`: an fd, not a file
                t = t[1:]
            any_target = any_target or bool(re.search(r"[\"'\\$`]", t) or GLOB_RE.search(t))
            targets.add(os.path.basename(t))
        _WRITES.update(raw=raw, targets=targets, any=any_target or bool(FILE_MAKER_RE.search(PKG_CMD_RE.sub(" ", text))))
    return _WRITES["any"] or bool(_WRITES["targets"] & names)


STDIN_FEED_RE = _LazyRe(r"(?<!\|)\|(?!\|)|<")  # a pipe or an input redirection anywhere in the command
WRITER_WORD_RE = _LazyRe(r"(?<![\w.-])(unzip|tar|bsdtar|7za?|cpio|cp|mv|ln|rsync|install|dd|tee)(?![\w.-])")
FILE_MAKER_RE = _LazyRe(WRITER_WORD_RE.pattern.replace("|tee)", "|tee|curl|wget|sponge|aria2c)"))
UNKNOWN_SCRIPT_REASON = ("a shell reading its script from a pipe, a variable or a file made in the same command can't be "
                         "checked; run the commands directly")


def unknown_script_matters(unknown, ctx, mentions_guarded):
    """A script the guard can't read, in a command that names something guarded: a guarded tool, the memory DB folder,
    the session files, or (the explorer) the run marker, the config, the qa-out folder, or a tool that writes files
    (an archive unpacked here could carry a forged marker)."""
    scope = getattr(ctx, "scope", None)  # inside a script file: what that file names, not the command that sourced it
    if scope is not None:
        return bool(GUARDED_ANY_RE.search(scope) or mentions_data_dir(scope) or mentions_session_path(scope))
    text = ctx.raw if unknown == "file" else split_heredocs(ctx.raw)[0]
    return bool((mentions_guarded if unknown == "file" else GUARDED_TOOLS_RE.search(text))
                or mentions_data_dir(ctx.raw) or mentions_session_path(ctx.raw)
                or (ctx.explorer and (MARKER_CODE_RE.search(ctx.raw) or "qa-out" in ctx.raw or WRITER_WORD_RE.search(ctx.raw))))


def scope_text(ctx):
    """The text the current command belongs to: the sourced file it is in, else the whole call."""
    return getattr(ctx, "scope", None) or ctx.raw


_SCOPE = {}


def scope_fact(ctx, what):
    """Answers about the whole scope text, computed once per text (a 500 KB configure asks hundreds of times):
    'tools' = names a guarded tool; 'bodies' = a `$(...)` body in it names a guarded tool, file or folder."""
    text = scope_text(ctx)
    if _SCOPE.get("text") is not text:
        _SCOPE.clear()
        _SCOPE["text"] = text
    if what not in _SCOPE:
        if what == "tools":
            _SCOPE[what] = bool(GUARDED_TOOLS_RE.search(text))
        else:
            bodies = substitutions(normalize(split_heredocs(text)[0]))[0]
            _SCOPE[what] = any(GUARDED_ANY_RE.search(x) or mentions_data_dir(x) or mentions_session_path(x) for x in bodies)
    return _SCOPE[what]


def eval_setup_only(rest, ctx):
    """An eval whose text comes only from `$(...)` or variables, none of which names a guarded tool, file or folder:
    `eval "$(fnm env)"`, `eval "$(pyenv init -)"`. `eval "$(echo acli … create)"` names one and is checked as usual."""
    values = [v for a in rest for v in var_variants(a, scope_text(ctx))]
    return not scope_fact(ctx, "bodies") and not any(
        GUARDED_ANY_RE.search(x) or mentions_data_dir(x) or mentions_session_path(x) for x in values)


def git_subcommand(rest):
    """`clean` from `git -C . -c a=b clean -fdx`: the first word that is neither an option nor an option's value."""
    skip = False
    for a in rest:
        if skip:
            skip = False
        elif a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env", "--super-prefix"):
            skip = True
        elif not a.startswith("-"):
            return a
    return ""


EXPLORER_GIT_READS = {"status", "log", "diff", "show", "rev-parse", "ls-files", "ls-tree", "blame", "describe", "grep", "cat-file",
                      "shortlog", "rev-list", "version", "help", "whatchanged", "name-rev", "merge-base", ""}


def explorer_git_ok(rest):
    """Read-only git for the explorer: the commands above, plus `stash list`, `config --get`, `worktree list`, `remote -v`
    and listing branches or tags. Never an alias defined on the command line (`-c alias.x='!cmd'`)."""
    if any(re.match(r"alias\.", a) for a in rest):
        return False
    sub = git_subcommand(rest)
    after = rest[rest.index(sub) + 1:] if sub in rest else []
    pos = [a for a in after if not a.startswith("-")]
    if sub in EXPLORER_GIT_READS:  # `git diff --output=file` writes one
        return not (sub in ("diff", "log", "show") and any(a.startswith("--output") for a in after))
    if sub in ("stash", "worktree"):
        return pos[:1] == ["list"]
    if sub == "config":
        return bool({"--get", "--get-all", "--list", "-l", "--get-regexp"} & set(after))
    if sub in ("branch", "tag", "remote"):
        return not pos and not any(re.match(r"^-[a-zA-Z]*[dDmMcCf]", a) or a.startswith(("--delete", "--move", "--copy", "--force"))
                                   for a in after)
    return False


def check_scripts(texts, unknown, here, ctx, depth, mentions_guarded, from_file=False):
    for body in texts:
        old = getattr(ctx, "scope", None)
        ctx.scope = body if from_file and not ctx.explorer else old  # (the explorer gets no such leeway)
        try:
            if r := _check(body, here, ctx, depth + 1):
                return r
        finally:
            ctx.scope = old
    return UNKNOWN_SCRIPT_REASON if unknown and unknown_script_matters(unknown, ctx, mentions_guarded) else None


def check_segment(seg, cwds, start, hosts, docs, depth, mentions_guarded, ctx):
    """Check one simple command. Mutates cwds on cd."""
    j = 0  # `then for ...`, `do for ...`, `{ for ...`, `time for ...`: the loop keyword after leading keywords
    while j < len(seg) and seg[j] in SHELL_KEYWORDS | {"time"} and seg[j] not in LOOP_KEYWORDS:
        j += 1
    if j < len(seg) and seg[j] in LOOP_KEYWORDS:
        ctx.loop_depth += 1
    elif seg and seg[0] == "done":
        ctx.loop_depth = max(0, ctx.loop_depth - 1)
    words, outs, ins = split_redirects(seg)
    if words[:1] == ["case"] and not outs and not ins:
        return None
    if words[:1] in (["for"], ["select"]) and not outs and not ins:
        return None  # `for t in git tar unzip`: a list of words (a `$(...)` in it was checked as its own command)  # `case WORD in PATTERN`: compares text, runs and reads nothing (the branches are their own segments)
    i, wrappers, env_dir, env_split = command_word(words)
    here = next(iter(cwds))
    exempt = READ_ONLY_TOOLS | NAME_ONLY_TOOLS | SHELLS | INTERPRETERS
    one_string = i is not None and bool(re.search(r"\s", unmark(words[i])) and set(wrappers) & STRING_RUNNERS)  # `watch "cmd …"`
    relaunch, ctx.relaunch_at = getattr(ctx, "relaunch_at", None) != depth, None  # False: this is a launcher's re-check
    # `install acli /usr/local/bin/`, `mv acli ~/bin/`, `git log -S acli`, `gh search code acli`: arguments, not programs
    no_launch = i is not None and prog_name(words[i]) in (LAUNCHED - {"playwright-cli", "acli"}) | {"gh"} and not one_string
    if relaunch and not no_launch and ((i is None and wrappers) or one_string or (
            i is not None and prog_name(words[i]) not in exempt and not is_qamem(words[i], cwds))):
        # An unknown launcher (`chrt -i 0 X`, `firejail X`, `gdb --args X`, `nsenter … X`) runs X: check X on its own.
        # Also a command handed over as one string (`ssh host "X …"`, `parallel "X {}"`, `flock f -c "X"`) or glued to
        # an option (`fd --exec=X`). Shells and interpreters resolve their own script/code; `.`/`source` are paths here.
        entry = LAUNCHED | (SHELLS - {".", "source"}) | INTERPRETERS | set(WRAPPERS)
        file_tool = i is not None and prog_name(words[i]) in LAUNCHED - {"playwright-cli", "acli", "git", "sqlite3"}
        # `cp bin/qa-mem ~/.local/bin/`, `chmod +x "$ROOT/bin/qa-mem"`: for a copy/remove/chmod tool the path is a file
        names = lambda w: prog_name(w) in entry or (not file_tool and "qa-mem" in w and is_qamem(w, cwds))
        rechecked = 0
        for j in range(i if i is not None else 0, len(words)):
            w, prev = unmark(words[j]), unmark(words[j - 1]) if j else ""
            head, _, tail = w.partition("=")
            runner = bool(set(wrappers) & STRING_RUNNERS) or (i is not None and prog_name(words[i]) in STRING_RUNNERS)
            # a string runner's words with a space are commands whatever they start with (`watch "X=1 acli …"`)
            if " " in w.strip() and (runner or (names(w.split()[0]) and (j == i or prev in ("-c", "--command", "-e")))):
                cand = w  # a command handed over as one string (`ssh host "acli …"`, `parallel "acli … {}" ::: x`)
            elif j == i:
                continue  # the program itself: checked below
            elif prog_name(w) in READ_ONLY_TOOLS | NAME_ONLY_TOOLS and not (prev.startswith("-") and prev not in EXEC_FLAGS):
                break  # `find . -exec grep -l acli {} +`, `ssh host grep -r acli`: the rest is that tool's arguments
            elif i is not None and names(w):
                cand = resume_at(seg, j)
                ctx.relaunch_at = depth + 1  # the words after it are checked by this loop, not again from inside
            elif w.startswith("-") and tail and names(tail.split()[0]):
                cand = " ".join([tail] + [shlex.quote(unmark(x)) for x in words[j + 1:]])  # `fd --exec=acli …`
                ctx.relaunch_at = depth + 1
            else:
                continue
            rechecked += 1
            if rechecked > 6:  # each re-check reads the rest again: bound it (a legit command has one or two)
                if (ctx.mentions if getattr(ctx, "scope", None) is None else scope_fact(ctx, "tools")) or DATA_DIR in " ".join(words):
                    return "too many program names after a launcher to verify; run the command directly"
                break
            if r := _check(cand, here, ctx, depth + 1, docs):
                return r
    if env_split and (r := _check(unmark(env_split), here, ctx, depth + 1)):
        return r
    if any(is_session_ref(a, cwds, ctx.raw) for a in ins):  # `cat < f`, `$(< f)`, `done < f`, bare `< f`: all print it
        return "the saved browser session holds live cookies; only playwright-cli state-load/state-save may use it"
    ctx.pw_env |= sets_pw_env(words, i, getattr(ctx, "scope", None))
    if ctx.explorer and any(CLI_CONFIG_RE.search(a) for a in words + outs + ins + docs) and not (
            i is not None and prog_name(words[i]) in READ_ONLY_TOOLS and not outs):
        return ("playwright-cli reads its browser and profile from .playwright/cli.config.json; the explorer leaves "
                "that file alone (reading it is fine)")
    if i is None:  # bare redirection, e.g. `: > file`, `exec 3>file`
        if ctx.explorer and (r := touches_marker("", [], outs, cwds, ctx.marker_files, ctx.raw)):
            return r
        if ctx.explorer and explorer_outside("", [], outs, cwds, ctx.raw):
            return EXPLORER_WRITE_REASON
        return check_db(words, outs, "", [], cwds, ctx.raw)
    scope = getattr(ctx, "scope", None)  # inside a sourced file: what that file names (`(( $+var ))` in a theme is harmless)
    if (words[i].startswith("$") or words[i] == "SUBST") and (
            mentions_guarded if scope is None else scope_fact(ctx, "tools")):
        return "program name comes from a variable or $(...) in a command that mentions a guarded tool; spell it out"
    if prog_name(words[i]) == "alias" and GUARDED_TOOLS_RE.search(" ".join(words[i + 1:])):
        return "aliases for playwright-cli/acli hide what runs; call the tool directly"
    expanded = lambda a: "$" in a or "SUBST" in a  # $VAR, ${V:-x}, go$X, $(...), `...`
    if prog_name(words[i]) == "playwright-cli" and not pw_help(words[i + 1:]):
        pos = pw_args(words[i + 1:])
        if (pos and expanded(pos[0])) or any(POS_PARAM_RE.match(a) for a in words[i + 1:]):
            return "the playwright-cli verb or arguments come from the shell ($VAR, $(...), \"$@\"); spell them out"
    if prog_name(words[i]) == "acli" and any(expanded(a) for a in acli_head(words[i + 1:])):
        return "the acli command words come from the shell ($VAR, $(...)); spell them out"
    name, rest = prog_name(words[i]), words[i + 1:]
    if name in ("cd", "pushd", "popd"):
        args = [a for a in rest if a not in ("-L", "-P", "-e", "-@", "--")]
        target = args[0] if args else "~"
        new = {start} if target == "-" or name == "popd" else {resolve(target, c) for c in cwds}
        if GLOB_RE.search(target):  # `cd ~/.config/qa-*` may land in a session folder
            new |= {d for c in cwds for d in glob_hits(expand(target), c, session_dirs())}
        if target == "-":
            new |= cwds  # unknown previous dir: consider both
        cwds.clear()
        cwds.update(new)
        return None
    local = {resolve(env_dir, c) for c in cwds} if env_dir else cwds
    if r := check_profile(name, words[i + 1:], rest, outs, ins, local, ctx, bool(LOOP_WRAPPERS & set(wrappers)), docs):
        return r
    sanctioned = set(pw_profile_values(rest)) if name == "playwright-cli" else set()
    sanctioned = {v for v in sanctioned if is_main_profile(v, local, ctx.raw)}  # check_pw allows it only on `open`
    pw_session_verb = name == "playwright-cli" and (pw_args(rest)[:1] or [""])[0] in ("state-load", "state-save")
    lists_names = name == "find" and not {"-exec", "-execdir", "-ok", "-okdir"} & set(rest)  # names, not contents
    checked = [a for a in words[i:] + outs + ins if not a.startswith("-") and a not in sanctioned]
    # `dd if=<file>`, `curl -F f=@<file>`, `curl -d @<file>`: the path behind `=` / `@`
    checked += [x for a in checked[1:] if not re.search(r"[\s()'\"`;|<>]", a)  # a word, not text or code
                for x in {a.split("=", 1)[-1].lstrip("@"), a.lstrip("@")} - {a, ""}]
    if name in COPY_TOOLS and checked[1:]:  # `cp x ~/.config/qa-sdet/`: a session folder as the destination is fine
        last = checked[-1]
        if not any(realpath(os.path.join(c, expand(t))) in session_files()
                   for t in {last, *var_variants(last, ctx.raw)} for c in local):  # `F=<session>; cp x $F` too
            checked = checked[:-1]
    if name in GREP_TOOLS and checked[1:] and not {"-e", "-f", "--regexp", "--file"} & set(rest):
        checked.pop(1)  # `grep -rn "qa-state" src`: the first word is the pattern, not a file
    if name == "git":  # search text, not files: `git grep -n qa-state`, `git grep -e qa-state`, `git log -S qa-state`
        pats = [rest[k + 1] for k, a in enumerate(rest[:-1]) if a in ("-S", "-G", "-e", "--regexp")]
        if "grep" in rest and not pats and not {"-f", "--file"} & set(rest):
            pats += [a for a in rest[rest.index("grep") + 1:] if not a.startswith("-")][:1]
        for pat in pats[:8]:
            if pat in checked[1:]:
                checked.remove(pat)  # one occurrence: `git grep -e x x` still checks the path x
    opt_values = [a.split("=", 1)[1] for a in words[i + 1:] if a.startswith("-") and "=" in a]  # --from-file=PATH
    if not (pw_session_verb or lists_names) and name not in SESSION_FILE_OK and (
            any(is_session_ref(a, local, ctx.raw) for a in checked)
            or any(realpath(os.path.join(c, expand(t))) in session_files()  # `--file=$F` with F set earlier too
                   for v in opt_values for t in {v, *var_variants(v, ctx.raw)} for c in local)):
        return "the saved browser session holds live cookies; only playwright-cli state-load/state-save may use it"
    if env_dir and any(is_session_ref(a, local, ctx.raw) for a in ins):  # `env -C dir cat < f`
        return "the saved browser session holds live cookies; only playwright-cli state-load/state-save may use it"
    recursive_copy = name in ARCHIVERS or (name in ("cp", "scp") and any(
        re.match(r"^-[a-zA-Z]*[rRa]", a) or a in ("--recursive", "--archive") for a in rest))
    if recursive_copy:
        srcs, base = copy_sources(name, rest)
        bases = {resolve(base, c) for c in local} if base else local
        if any(is_session_ancestor(a, bases) for a in srcs):
            return "an archive or recursive copy of this folder would include the saved browser session; pick a narrower folder"
    if recursive_content_search(name, rest):
        targets = [a for a in rest if not a.startswith("-")]
        if any(is_session_ancestor(a, local) for a in targets[1:] or ["."]):  # first positional is the pattern
            return ("a recursive content search from this folder would read the saved browser session; "
                    "search a narrower folder, or list matching files with -l")
    if ctx.explorer and name != "playwright-cli" and (r := touches_marker(name, rest, outs, local, ctx.marker_files, ctx.raw)):
        return r
    if ctx.explorer and explorer_outside(name, rest, outs, local, ctx.raw, bool(LOOP_WRAPPERS & set(wrappers))):
        return EXPLORER_WRITE_REASON
    code = interpreter_code(rest, docs)
    interp = name in INTERPRETERS or is_python(name)
    if interp and (re.search(r"qa-state\.json", code) or mentions_session_path(code)):
        return ("a script reading the saved browser session would print its cookies; check it with ls -l / stat, "
                "or re-run /qa-sdet:qa-login")  # python -c "...open('~/.config/qa-sdet/qa-state.json')..."
    if interp and (DATA_DIR in expand(code) or re.search(r"qa-sdet/[^'\"\s]*\.db\b", code)):
        return ("code that opens the qa-sdet memory DB is blocked (only qa-mem writes it); read it with "
                "qa-mem card/search/bugs or sqlite3 -readonly")
    if name == "expect" and code:  # `expect -c 'spawn cmd …'` runs cmd
        for m in re.finditer(r"\b(?:spawn|exec|system)\s+([^\n;]+)", unmark(code)):
            if r := _check(m.group(1), here, ctx, depth + 1):
                return r
    stdin_script = interp and script_word(rest) is None and not code  # `python3 - <<EOF`
    if stdin_script and any(mentions_profile(d) for d in docs):
        return PROFILE_REASON
    if is_python(name) and (sw := script_word(rest)) and ("$" in os.path.basename(sw) or "SUBST" in sw) \
            and re.search(r"qa-mem", ctx.raw):
        return "the script path comes from the shell ($VAR, $(...)) in a command that runs qa-mem; spell it out"
    if (interp and re.search(r"\bacli\b", code)) or (name in ("awk", "gawk", "mawk", "nawk") and
                                                      any(re.search(r"\bacli\b", a) for a in rest)):
        return ("run acli directly (acli jira workitem view/search ...), not from inline code: "
                "Jira stays read-only and only a command-position acli is checked")  # os.system('acli ... create')
    if ctx.explorer and interp and (GUARDED_TOOLS_RE.search(code) or
                                    (stdin_script and any(GUARDED_TOOLS_RE.search(d) for d in docs))):
        return "the explorer runs playwright-cli, acli and qa-mem directly (qa-mem <subcommand>), not from inline code"
    if ctx.explorer and interp and not code and qamem_args(name, rest, local) is None and (sw := script_word(rest)):
        text = read_script(sw, local, ctx.raw, strict=True)  # a script file the explorer wrote that runs a guarded tool or edits the marker
        if (text is None and (GUARDED_TOOLS_RE.search(ctx.raw) or MARKER_CODE_RE.search(ctx.raw))) or \
                (text and (GUARDED_TOOLS_RE.search(text) or MARKER_CODE_RE.search(text))):
            return "the explorer runs playwright-cli, acli and qa-mem directly (qa-mem <subcommand>), not from a script file"
    if ctx.explorer and name in COPY_TOOLS and any(is_qamem(a, local) or (expanded(a) and "qa-mem" in ctx.raw) for a in rest):
        return "the explorer runs qa-mem from the plugin; copies or links of it are blocked"
    if qamem_args(name, rest, local) is None and any(is_qamem(a, local) and not (name in SHELLS and re.search(r"\s", a))
                                              for a in words[i:] + ins):  # (a `bash -c 'script'` string is checked below)
        if interp or name in QAMEM_RUNNERS or name in SHELLS:  # `uv run qa-mem`, `python3 - x < qa-mem`
            return "run qa-mem as python3 <plugin>/bin/qa-mem <subcommand> (other launchers skip its checks)"
        if ctx.explorer and name not in QAMEM_NAME_OK:  # `cat qa-mem > /tmp/q`, `dd if=qa-mem`, `tee x < qa-mem`
            return "the explorer runs qa-mem from the plugin (python3 <refs>/../bin/qa-mem <subcommand>); copies, reads and other launchers are blocked"
    if (qargs := qamem_args(name, rest, local)) is not None and (
            r := check_qamem(qargs, ctx, local, allowed_hosts(), bool(LOOP_WRAPPERS & set(wrappers)))):
        return r
    if ctx.explorer and interp and MARKER_CODE_RE.search(" ".join(rest)):  # python -c "os.remove(...)"
        return ("the run marker belongs to the calling skill: read it if you like, but leave it unchanged and carry on "
                "testing (use cat to read it)")
    lookup_only = "command" in wrappers and bool({"-v", "-V"} & set(words[:i]))  # `command -v curl` runs nothing
    if ctx.explorer and name in NET_CLIS and not lookup_only:
        return (f"{name} reaches the network outside the browser (no host or budget check); observe through "
                "playwright-cli: requests, response-body, or --raw eval \"fetch('/api/x').then(r => r.status)\"")
    if ctx.explorer and name == "git" and not explorer_git_ok(rest):
        return (f"git {git_subcommand(rest)} can change the project (git clean also deletes the run folder): the explorer tests "
                "through the browser and runs only read-only git commands (status, log, diff, show, ls-files)")
    r = None
    via_loop = bool(LOOP_WRAPPERS & set(wrappers))
    if name in (".", "source") and not rest:
        pass  # the `.)` pattern of a `case`, or `source` with nothing: runs nothing (and reads no heredoc of the script)
    elif name in SHELLS or (name == "busybox" and rest[:1] and rest[0] in SHELLS):
        for j, a in enumerate(rest):
            if re.match(r"^-[a-z]*c[a-z]*$", a) and j + 1 < len(rest):
                r = _check(unmark(rest[j + 1]), here, ctx, depth + 1)
                break
        else:  # `bash <<EOF`, `sh -s <<EOF`, `bash <<<'..'`, `bash x.sh`, `bash < x.sh`, `... | bash`
            texts, unknown = shell_script_texts(name, rest, seg, ins, local, docs, ctx)
            if not relaunch and not texts and unknown == "stdin" and not rest and not ctx.explorer and \
                    not STDIN_FEED_RE.search(split_heredocs(scope_text(ctx))[0]):
                unknown = None  # `brew install bash`: found by the launcher re-check, nothing after it and nothing piped in
            # `source ~/.zshrc`: a file on disk, read as its own scope (its `. "$NVM_DIR/nvm.sh"` names nothing guarded)
            from_file = bool(texts) and unknown is None and not any(
                (m := REDIRECT_RE.match(t)) and m.group(2).startswith("<") for t in seg)  # `2>/dev/null` keeps it
            r = check_scripts(texts, unknown, here, ctx, depth, mentions_guarded, from_file)
    elif name in ("make", "gmake") and re.search(r"(^|\s)-f\s*(-|/dev/stdin)(\s|$)", " ".join(rest)):
        # `make -f - <<< $'x:\n\tcmd'`: the recipe lines of a makefile on stdin are shell commands
        texts, unknown = shell_script_texts("su", rest, seg, ins, local, docs, ctx)
        lines = [ln.strip() for t in texts for ln in codecs.decode(t.encode("latin-1", "backslashreplace"), "unicode_escape",
                                                                   "replace").splitlines() if ln[:1] in "\t " and ln.strip()]
        r = check_scripts(lines, unknown, here, ctx, depth, mentions_guarded)
    elif name in ("at", "batch"):  # `at now <<< 'cmd'`: the job is a shell script on stdin
        r = check_scripts(*shell_script_texts(name, rest, seg, ins, local, docs, ctx), here, ctx, depth, mentions_guarded)
    elif name == "eval" and not ctx.explorer and rest and all(expanded(a) for a in rest) and eval_setup_only(rest, ctx):
        r = None  # `eval "$(fnm env)"`, `eval "$__conda_setup"`: environment setup; what it prints names no guarded tool
    elif name == "eval":
        r = _check(unmark(" ".join(rest)), here, ctx, depth + 1)
        # the explorer only (page text may steer it): `eval "$(echo 'dd of=qa-out/.active')"`. The user's own session
        # keeps `eval "$(fnm env)" && playwright-cli …`
        if not r and ctx.explorer and any(expanded(a) for a in rest) and unknown_script_matters("stdin", ctx, mentions_guarded):
            r = UNKNOWN_SCRIPT_REASON
    elif name == "trap":  # `trap 'cmd' EXIT` runs cmd later
        r = _check(unmark(next((a for a in rest if not a.startswith("-")), "")), here, ctx, depth + 1)
    elif name == "watch":
        ctx.loop_depth += 1
        r = _check(unmark(" ".join(rest)), here, ctx, depth + 1)
        ctx.loop_depth -= 1
    elif name == "find":
        roots = []
        for a in rest:
            if a.startswith("-") or a in ("(", "!"):
                break
            roots.append(a)
        subs = find_exec(rest)
        if any(prog_name((shlex.split(x) or [""])[0]) not in SESSION_FILE_OK for x in subs) and any(
                is_session_ancestor(a, local) for a in roots or ["."]):
            return "find -exec from this folder would read the saved browser session; search a narrower folder"
        for sub in subs:
            if r := _check(sub, here, ctx, depth + 1):
                break
    elif name == "playwright-cli":
        r = check_pw(rest, hosts, local, ctx, via_loop)
    elif name == "acli":
        r = check_acli(rest)
    return r or check_db(words, outs, name, rest, local, ctx.raw)


# ---------- MCP fallbacks (Playwright MCP, Atlassian/Jira MCP) ----------

MCP_BROWSER_RE = re.compile(r"^mcp__.+__browser_[a-z_]+$")
MCP_JIRA_RE = re.compile(r"^mcp__.*(atlassian|jira|confluence)", re.I)  # server or tool name (mcp__corp__createJiraIssue)
MCP_JIRA_READ_RE = _LazyRe(r"^(batch_)?(get|search|lookup|list|fetch|read|view|download)|^atlassianuserinfo$", re.I)
MCP_WRITE_WORDS = {"create", "edit", "update", "delete", "remove", "transition", "add", "assign", "archive", "unarchive",
                   "move", "upload", "post", "put", "patch", "write", "set", "clone", "restore", "replace", "publish",
                   "rank", "invoke", "run", "execute", "send", "import", "bulk", "upsert", "modify", "change"}
MCP_BUDGET_FREE = {"browser_install", "browser_tab_close", "browser_tab_list"}
# The explorer inherits every tool (an agent's `tools:` list can't name "any server's browser_*"), so its
# allowlist lives here: working tools plus housekeeping; other MCPs (mail, Jira, docs) stay out of a test run.
MAPPER_TOOLS = {"Read", "Grep", "Glob", "LS", "Write", "ToolSearch", "TodoWrite", "TaskCreate", "TaskUpdate", "TaskGet",
                "TaskList"}
EXPLORER_TOOLS = {"Bash", "Read", "Write", "Grep", "Glob", "LS", "ToolSearch", "TodoWrite", "TaskCreate", "TaskUpdate",
                  "TaskGet", "TaskList", "TaskOutput", "BashOutput", "TaskStop", "KillShell", "KillBash"}
MCP_SECRET_TOOL_RE = _LazyRe(r"(cookie|(local|session)storage)s?_(list|get)|storage_state|request_headers|"
                                r"response_headers|get_config")  # set/delete/clear print nothing; config holds headers


def _strings(obj, depth=0):
    """Every string inside a tool input (nested dicts/lists), for scanning code wherever a version puts it."""
    if isinstance(obj, str):
        return [obj]
    if not isinstance(obj, (dict, list, tuple)):
        return []
    if depth > 8:  # no real tool input nests this deep: fail closed (the crash fallback denies browser tools)
        raise ValueError("tool input nested too deeply to check")
    items = obj.values() if isinstance(obj, dict) else obj
    return [x for v in items for x in _strings(v, depth + 1)]


def mcp_name_words(name):
    """getJiraIssueWithEdit -> [get, jira, issue, with, edit]; jira_batch_create_issues -> [jira, batch, create, issues]."""
    return [w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", name)]


def check_mcp(tool, tin, cwd, explorer):
    """Deny reason for an MCP tool call, or None: the browser_* rules mirror check_pw, Jira tools are read-only."""
    name = tool[len("mcp__"):].rpartition("__")[2]
    if MCP_BROWSER_RE.match(tool):
        if not explorer:
            path, marker, _ = read_marker(cwd)
            if path is None or (marker and marker.get("deadline")
                                and time.time() > float(marker["deadline"]) + 600):
                return None  # no run (or one left behind by an interrupted skill): the user's everyday browser
            reason = check_mcp_browser(name, tin, cwd, explorer)  # the user's own session: say why QA rules apply
            return reason and (f"{reason} (a qa-sdet run marker is active: {path}; if no run is going, "
                               f"qa-mem marker stop '{path.parent.parent}')")
        return check_mcp_browser(name, tin, cwd, explorer)
    if MCP_JIRA_RE.match(tool):
        bare = re.sub(r"^(jira|confluence)_", "", name, flags=re.I)
        if not MCP_JIRA_READ_RE.search(bare) or MCP_WRITE_WORDS & set(mcp_name_words(bare)[1:]):
            return (f"{name} writes to Jira/Confluence; Jira writes are yours: "
                    "the bug draft ends with the command for you to run")
    return None


def check_mcp_browser(name, tin, cwd, explorer):
    hosts, cwds = allowed_hosts(), {cwd}
    if name == "browser_close":  # it ends the MCP browser's login: later routes and runs would land on SSO
        return "browser_close would end the MCP browser's login; close your own tab: browser_tabs {action: \"close\"}"
    if "tracing" in name:  # browser_start_tracing / browser_stop_tracing (devtools capability)
        return f"{name} blocked: {PW_DENY['tracing-start']}"
    if name.startswith("browser_run_code"):  # browser_run_code_unsafe; older versions: browser_run_code
        return "browser_run_code_unsafe runs code in the Playwright server and can reach the host; use browser_evaluate"
    if MCP_SECRET_TOOL_RE.search(name) or (name == "browser_network_request" and tin.get("part") not in
                                           ("request-body", "response-body")):
        return (f"{name} prints session secrets into the conversation; use browser_network_request with "
                "part request-body or response-body, or read one header with browser_evaluate "
                "\"() => fetch(url).then(r => r.headers.get('name'))\"")
    if any(re.search("header", str(k), re.I) and v for k, v in tin.items()):  # browser_network_requests {requestHeaders: true}
        return f"{name} with headers prints session secrets into the conversation; leave that option out"
    if re.search(r"(?<!un)route(?!_list)|network_state|offline", name):  # unroute removes mocks, as CLI unroute
        marker = read_marker(cwd)[1]
        if not (marker and marker.get("faults") is True):
            return f"{name} is fault injection; rerun the skill with --faults to allow it"
    if name in ("browser_tabs", "browser_tab_select") and (name == "browser_tab_select" or tin.get("action") == "select"):
        return ("switching tabs is blocked in a QA run: other tabs hold the sign-in (its network log has the login "
                "request) or, on an MCP attached to a real browser, the user's own pages; work in your own tab and "
                "open new-tab links there with browser_navigate")
    url = tin.get("url")
    if url is not None:  # browser_navigate, browser_tabs new, older versions' browser_tab_new, ...
        if not isinstance(url, str) or not host_ok(url, hosts):
            return f"{name} to non-allowlisted host: {url} (allowed: {sorted(hosts)})"
    if re.search(r"eval|run_code|script", name):  # code tools: every string, whatever the version names its field
        js = " ".join(_strings({k: v for k, v in tin.items()  # descriptions and selectors aren't code
                                if k not in ("element", "target", "filename", "startElement", "endElement")}))
    else:  # elsewhere only code-carrying fields; typed text (browser_type, fill_form) is test data
        js = " ".join(str(tin.get(k) or "") for k in ("function", "code", "url"))  # a URL can carry script too
    if js.strip():
        if explorer and PAGE_DUMP_RE.search(js):
            return ("dumping the page's HTML reads app code and wastes tokens; use browser_snapshot for structure, "
                    "browser_find, or browser_evaluate on one element")
        if SECRET_JS.search(js) or storage_value_read(js):
            return ("browser_evaluate reading cookies or web storage values blocked (they hold session tokens); "
                    "to check what a page stores, read key names: Object.keys(localStorage)")
        hosts_ = [h for h in SCHEMELESS_URL_RE.findall(js) if "." in h or h.lower() == "localhost"]
        for u in [m.group(0) for m in URL_RE.finditer(js) if not compared_only(js, m.start(), m.group(0))] + \
                 ["http://" + h for h in hosts_]:
            if not host_ok(u, hosts):
                return f"{name} references non-allowlisted URL: {u}"
    files, sent = [], []  # every field that names a file, whatever the version calls it (filename, path, outputPath, ...)
    for key, v in tin.items():
        if re.search(r"file|path|dir|output|save", str(key), re.I):
            vals = _strings(v)  # also nested: files: [{path: …}]
            files += vals
            sent += vals if re.search(r"paths|files", str(key), re.I) else []  # uploads/drops send these to the page
    for f in files:
        if not f or not isinstance(f, str):
            continue  # a flag such as saveSession: true names no file
        if f in sent and secret_upload(f, cwds):
            return SECRET_UPLOAD_REASON
        if explorer and f not in sent and not run_output_ok(f, cwds):
            return RUN_OUTPUT_REASON
        if touches_db(f, cwds, False):
            return f"{name} writing into the qa-sdet memory DB directory is blocked"
        if is_session_ref(f, cwds):
            return "the saved browser session holds live cookies; it stays out of the browser tools"
        target = Path(realpath(os.path.join(cwd, expand(f))))  # realpath: a symlinked folder is the same
        if (target.name in (".active", ".active.lock") and target.parent.name == "qa-out") or \
                str(target) == realpath(CONFIG):  # a JSON result over config.json would widen allowedHosts
            return "the run marker and the qa-sdet config are not output files; save files under the run's out folder"
        if f in sent and is_session_ancestor(f, cwds):
            return f"{name} would send a folder that holds the saved browser session"
        if not os.path.isabs(expand(f)) and ".." in Path(f).parts:
            return f"{name}: a relative path with .. can land outside the run folder; pass an absolute path under <out>"
    if name not in MCP_BUDGET_FREE and not (name == "browser_tabs" and tin.get("action") in ("list", "close")):
        path, marker, error = read_marker(cwd)
        if error:
            return error
        if marker:
            return spend_budget(path, 1, explorer)
        if explorer:
            return NO_RUN
    return None


# ---------- hook entry point ----------

def mentions_session_path(text, folders=False):
    """Does the text name a session file (path, ~-path, $HOME-path or file name)? folders=True also counts its
    folder: right for the crash fallback, too wide for code (`open('~/.config/qa-sdet/config.json')` is fine)."""
    home = os.path.expanduser("~")
    if "ms-playwright-mcp" in text or any(d in text for d in profile_dirs()) or mentions_profile(text):
        return True  # a browser profile with the QA login
    for f in session_files():
        forms = {f, os.path.basename(f)} | ({os.path.dirname(f)} if folders else set())
        if f.startswith(home + os.sep):
            forms |= {"~" + f[len(home):], "$HOME" + f[len(home):]}
            if folders:
                forms.add("~" + os.path.dirname(f)[len(home):])
        if any(x and x in text for x in forms):
            return True
    return False


def mentions_profile(text):
    """Does text name the browser profile folder (absolute, ~, $HOME / ${HOME} forms, or .../qa-sdet/profiles)?"""
    if "qa-sdet/profiles" in text:
        return True
    home = realpath(os.path.expanduser("~"))
    for r in profile_roots():
        forms = {r}
        if r.startswith(home + os.sep):
            rel = r[len(home):]
            forms |= {"~" + rel, "$HOME" + rel, "${HOME}" + rel}
        if any(f in text for f in forms):
            return True
    return False


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": f"qa-sdet guard: {reason}",
    }}))


WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def explorer_write_ok(target, cwd):
    """Run files the explorer may write under <project>/qa-out: new layout <run>/bugs/*.md, <run>/repro/*.spec.ts,
    <run>/.data/run-*.json, <run>/.data/snapshot.md; old layout **/run.json, **/reports/*.md, **/bugs/, **/repro/."""
    for base in qa_bases({cwd}):
        if not target.startswith(base + os.sep):
            continue
        rel = target[len(base) + 1:].split(os.sep)
        name, parent = rel[-1], (rel[-2] if len(rel) > 1 else "")
        if (parent in ("bugs", "reports") and name.endswith(".md")) or (parent == "repro" and name.endswith(".spec.ts")) \
                or (name == "run.json" and len(rel) > 1 and ".data" not in rel):
            return True
        if parent == ".data" and len(rel) == 3 and (name == "snapshot.md" or re.fullmatch(r"run-[^/\\]+\.json", name)):  # any C3 slug (unicode, %, ~)
            return True
    return False


PATH_KEYS = ("file_path", "notebook_path", "path")  # every key a file tool may take its path from


def tool_paths(tin):
    """All path fields present: a decoy `file_path` next to the `path` Grep/Glob really use (or NotebookEdit's
    `notebook_path`) must not hide the real target, so every rule checks each of them."""
    return [str(tin[k]) for k in PATH_KEYS if tin.get(k)] or [""]


def agent_role(data):
    """qa-explorer / qa-mapper / "" from agent_type, tolerant of case and stray spaces ("QA-SDET:qa-explorer ")."""
    return str(data.get("agent_type") or "").strip().lower().split(":")[-1].strip()


@_one_decision
def decide(data):
    _MEMO.clear()
    tool, tin = data.get("tool_name"), data.get("tool_input")
    tin = {} if tin is None else tin
    if not isinstance(tin, dict):
        return "malformed tool input (not an object)"
    cwd = str(data.get("cwd") or os.getcwd())
    is_explorer = agent_role(data) == "qa-explorer"
    if agent_role(data) == "qa-mapper":  # reads source, writes its code map only
        if tool not in MAPPER_TOOLS:
            return f"the qa-mapper reads source files and writes its code map; {tool} is not one of its tools"
        if tool == "Write":
            dirs = {realpath(os.path.join(b, "qa-out", "init"))
                    for b in filter(None, (cwd, os.environ.get("CLAUDE_PROJECT_DIR")))}
            for raw in tool_paths(tin):
                target = realpath(os.path.join(cwd, expand(raw)))
                if os.path.dirname(target) not in dirs or not re.fullmatch(
                        r"code-map-(frontend|backend|tests)(-\d+)?\.json", os.path.basename(target)):
                    return "the qa-mapper writes only <project>/qa-out/init/code-map-<area>[-<n>].json"
        if tool in ("Read", "Grep", "Glob", "LS"):  # source code yes; keys, .env, the memory DB and logins no
            for raw in tool_paths(tin):
                raw = raw or "."
                target = resolve(raw, cwd) if tool == "Glob" else realpath(os.path.join(cwd, expand(raw)))
                if is_secret_path(target):
                    return ("the qa-mapper reads source code only; secret files (.env, keys, credentials, the qa-sdet "
                            "data and config folders) stay closed: map the code that uses them, write [secret] for values")
                if tool == "Grep" and str(tin.get("output_mode")).lower() == "content" and os.path.isdir(target) and \
                        secret_below(target, str(tin.get("glob") or ""), str(tin.get("type") or "")):
                    return ("a content search from this folder would print secret files (.env, keys); pass a glob or "
                            "type for source files, a narrower path, or use files_with_matches and Read the source file")
    if is_explorer:  # the budget it runs under isn't its own
        if tool not in EXPLORER_TOOLS and not MCP_BROWSER_RE.match(str(tool or "")):
            return (f"the qa-explorer tests through the browser only: {tool} is not one of its tools "
                    "(Bash, Read, Write, Grep, Glob and the browser_* MCP tools)")
        for raw in tool_paths(tin) if tool in WRITE_TOOLS else ():
            target = Path(realpath(os.path.join(cwd, expand(raw or "x"))))
            if (target.name == ".active" and target.parent.name == "qa-out") or str(target) == realpath(CONFIG):
                return "the run marker belongs to the calling skill: read it if you like, but leave it unchanged and carry on testing"
            if not explorer_write_ok(str(target), cwd):
                return ("the explorer writes only its run files: <out>/bugs/BUG-<n>.md, <out>/repro/BUG-<n>.spec.ts, "
                        "<out>/.data/run-<slug>.json and <out>/.data/snapshot.md (out = <project>/qa-out/<run>)")
    if tool in WRITE_TOOLS | {"Glob"}:
        for raw in tool_paths(tin):
            if tool == "Glob":  # the pattern up to its first `**`, joined to the search path
                base, pattern = raw or ".", str(tin.get("pattern") or "")
                raw = os.path.join(base, re.split(r"(?:^|/)\*\*", pattern)[0])
                if "**" in pattern and in_profile_area(realpath(os.path.join(cwd, expand(base)))):
                    return PROFILE_REASON  # `**/Cookies` from the data folder lists the profile
            if raw and in_profile_root(raw, {cwd}):
                return PROFILE_REASON
            if tool != "Glob" and raw and touches_db(raw, {cwd}, False):
                return "the qa-sdet memory DB directory is written only by qa-mem"
    if tool in ("Read", "Grep"):
        for target in tool_paths(tin):
            if target and (is_session_file(target, cwd) or in_profile_dir(target, cwd)
                           or (tool == "Grep" and is_session_ref(target, {cwd}))):
                return "the saved browser session holds live cookies; it stays out of the conversation"
            if (tool == "Grep" and str(tin.get("output_mode")).lower() == "content" and is_session_ancestor(target or ".", {cwd})
                    and (not tin.get("glob") or glob_may_match_session(str(tin["glob"]), resolve(target or ".", cwd)))):
                return ("a content search from this folder would read the saved browser session; "
                        "narrow the path or glob, or use files_with_matches")
        return None
    if tool == "Bash":
        return check(str(tin.get("command") or ""), cwd, explorer=is_explorer)
    if str(tool or "").startswith("mcp__"):
        return check_mcp(str(tool), tin, cwd, is_explorer)
    return None


def _clean(obj, depth=0):
    """Tool input with NUL and lone surrogates taken out of every string: no file name holds them, and os.path
    raises on them, which would turn any command into a guard crash (allowed unless it names a guarded tool)."""
    if isinstance(obj, str):
        return obj.encode("utf-8", "replace").decode("utf-8").replace("\0", "")
    if depth > 20:
        raise ValueError("tool input nested too deeply to check")
    if isinstance(obj, dict):
        return {_clean(k, depth + 1): _clean(v, depth + 1) for k, v in obj.items()}
    return [_clean(v, depth + 1) for v in obj] if isinstance(obj, list) else obj


def mentions_data_dir(text):
    home = realpath(os.path.expanduser("~"))
    rel = DATA_DIR[len(home):] if DATA_DIR.startswith(home + os.sep) else None
    return DATA_DIR in text or bool(rel) and any(f + rel in text for f in ("~", "$HOME", "${HOME}"))


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if not isinstance(data, dict):
        return 0
    text = json.dumps(data.get("tool_input") or {})
    if hasattr(signal, "SIGALRM"):  # stay well inside the hook timeout
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError("guard took too long")))
        signal.alarm(8)
    try:
        reason = decide(_clean(data))
    except TimeoutError:  # only thousands of nested shells or brace groups take this long: not checked means not run
        reason = "the command is too complex to check in time; split it into smaller commands"
    except Exception as e:  # noqa: BLE001 - a crashing guard must not mean "allowed"
        reason = (f"guard error ({type(e).__name__}) on a command that mentions a guarded tool or file"
                  if GUARDED_ANY_RE.search(text) or mentions_data_dir(text) or mentions_session_path(text, folders=True)
                  or MCP_BROWSER_RE.match(str(data.get("tool_name") or ""))
                  or MCP_JIRA_RE.match(str(data.get("tool_name") or ""))
                  or agent_role(data) in ("qa-explorer", "qa-mapper") else None)  # restricted agents fail closed
    if hasattr(signal, "SIGALRM"):
        signal.alarm(0)  # checked in time: the alarm must not fire later (in a process that goes on, such as a test run)
    if reason:
        deny(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
