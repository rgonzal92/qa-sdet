#!/usr/bin/env python3
"""PreToolUse guard for qa-sdet (Bash, Read, Grep, Write, Edit). Denies, by default:
1. playwright-cli navigation to hosts not in config allowedHosts; run-code and attach (they reach
   outside the page: playwright-cli itself calls run-code RCE-equivalent); show bound to a non-local host
2. acli anything except read verbs (Jira stays read-only; bugs are drafts)
3. during a run (marker qa-out/.active): playwright-cli commands past the command budget or deadline,
   and route / network-state-set unless the run allows faults
4. commands that print session secrets, and any read of the saved browser session file
5. destructive commands / redirections onto the qa-sdet DB directory
6. for the qa-explorer subagent only: touching the run marker, network CLIs (curl, wget, ...), and dumping
   the page's HTML
Only words in command position are checked, so ordinary commands that merely mention these tools
pass. If the guard itself fails, it denies commands that mention a guarded tool or file.
Limit: it checks command text, not intent (curl, scripts, JS that builds URLs at runtime, etc.).
"""
import json
import os
import re
import shlex
import signal
import sys
import tempfile
import time
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import urlparse

try:
    import fcntl
except ImportError:  # shortcut: no lock on Windows; budget counting may race there
    fcntl = None

CONFIG = Path(os.environ.get("QA_SDET_CONFIG", "~/.config/qa-sdet/config.json")).expanduser()
DATA_DIR = os.path.realpath(Path(os.environ.get("QA_SDET_DATA", "~/.local/share/qa-sdet")).expanduser())
PUNCT = ";&|()\n"
QUOTED_GT, QUOTED_LT = "\x01", "\x02"  # quoted '>' / '<' are data, not redirections

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
}
WRAPPER_POSITIONALS = {"flock": 1, "taskset": 1}  # `flock FILE cmd`, `taskset MASK cmd` (unless -c/-p took it)
LOOP_WRAPPERS = {"xargs", "parallel", "watch"}
LOOP_KEYWORDS = {"for", "while", "until", "select"}
SHELL_KEYWORDS = {"if", "then", "elif", "else", "do", "while", "until", "!", "{", "}", "fi", "done", "esac", "coproc"}
PKG_SUBCOMMANDS = {"exec", "x", "run", "dlx"}  # npm exec / pnpm dlx / yarn run ...
SHELLS = {"bash", "sh", "zsh", "fish", "dash", "ksh", "ash", "source", ".", "su", "script"}
PW_VALUE_OPTS = {"-s", "--session", "--browser", "--config", "--device", "--profile", "--idle-timeout",
                 "--filename", "--depth", "--domain", "--path", "--host", "--port", "--isolated-context"}
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
# Page-side reads of cookies / web storage (eval runs in the page).
SECRET_JS = re.compile(
    r"document\s*(\.\s*cookie|\[\s*['\"`]cookie)|cookieStore|"
    r"(local|session)Storage\s*(\.\s*getItem\b|\[)|\.\.\.\s*(local|session)Storage|"
    r"(JSON\.stringify|Object\.(values|entries|assign|fromEntries))\s*\(\s*(local|session)Storage")
# Key names (Object.keys(localStorage), localStorage.key(i), .length, for..in) are allowed: they show what a page stores.
ACLI_READ = {"view", "search", "list", "get", "help", "version", "whoami"}
ACLI_AUTH_OK = {"login", "status"}
ACLI_WRITE = {"create", "edit", "delete", "transition", "assign", "update", "clone", "archive", "unarchive",
              "link", "unlink", "move", "add", "remove", "set", "import", "upload", "logout", "bulk"}
DB_DESTRUCTIVE = {"rm", "mv", "shred", "truncate", "dd", "unlink", "rmdir", "trash", "trash-put", "gio",
                  "rmtrash", "kioclient", "kioclient5"}
DB_WRITERS = {"cp", "tee", "install", "ln", "rsync"}
SESSION_FILE_OK = {"chmod", "ls", "stat", "test", "[", "[[", "rm", "touch", "echo", "printf", "file", "wc",
                   "realpath", "readlink", "du", "mkdir", "dirname", "basename"}
URL_RE = re.compile(r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,30}://[^\s'\"`)]+", re.I)
HEREDOC_RE = re.compile(r"(?<![<$(\d])<<(?!<)(-?)\s*(['\"]?)([^\s'\"<>;&|()]+)\2")
REDIRECT_RE = re.compile(r"^(\d*)(>>?|<<?<?)(.*)$", re.S)
GUARDED_TOOLS_RE = re.compile(r"(?<![.\w])playwright-cli|@playwright/cli|\bacli\b")  # not the .playwright-cli/ folder
VAR_ARG_RE = re.compile(r"^\$(\{?[@*#0-9]|\{?[A-Za-z_][A-Za-z0-9_]*\}?$)")  # a whole word from a variable
POS_PARAM_RE = re.compile(r"^\$(\{?[@*]\}?|\{?[1-9]\}?)$")  # exactly "$@", $1: a wrapper passing its args through ('$100' is data)
SCHEMELESS_URL_RE = re.compile(r"[\"'`(]\s*//([a-z0-9][a-z0-9.-]*)", re.I)  # '//host/path' in JS
PAGE_DUMP_RE = re.compile(r"(documentElement|document\.body|document\.head|\bdocument)\s*\.\s*(outer|inner)HTML"
                          r"|XMLSerializer|document\.scripts|querySelectorAll\(\s*['\"`]script")
NOT_A_PATH_RE = re.compile(r"[\s()'\"=;`|<>]")  # page JS/CSS in an argument, not a file path
GLOB_RE = re.compile(r"[*?\[{]")
NET_CLIS = {"curl", "wget", "http", "https", "xh", "httpie", "nc", "ncat", "netcat", "telnet", "socat", "aria2c",
            "lynx", "w3m", "links", "elinks"}
GREP_TOOLS = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "ugrep", "zgrep"}
ARCHIVERS = {"tar", "bsdtar", "zip", "7z", "7za", "rsync", "rclone", "cpio", "pax"}
COPY_TOOLS = {"cp", "mv", "rsync", "scp", "install", "ln"}
READ_ONLY_TOOLS = {"ls", "du", "stat", "cat", "head", "tail", "wc", "file", "grep", "rg", "egrep", "fgrep", "test", "[",
                   "[[", "realpath", "readlink", "jq", "less", "diff", "tree"}
REMOVERS = {"rm", "rmdir", "unlink", "mv", "shred", "trash", "trash-put", "gio", "truncate"}
INTERPRETERS = {"python", "python3", "node", "perl", "ruby", "php", "deno", "bun", "osascript", "pwsh"}
GUARDED_ANY_RE = re.compile(r"playwright-cli|@playwright/cli|\bacli\b|qa-state")


class Ctx:
    """State for one tool call: the active run marker and what the command would spend."""
    def __init__(self, cwd, explorer=False):
        self.explorer = explorer  # the qa-explorer subagent: observes the app through the browser only
        self.marker_path, self.marker, self.marker_error = read_marker(cwd)
        self.marker_files = {os.path.realpath(os.path.join(b, "qa-out", ".active"))
                             for b in filter(None, (cwd, os.environ.get("CLAUDE_PROJECT_DIR")))}
        self.spend = 0
        self.raw = ""
        self.loop = False  # a playwright-cli call runs inside a loop / xargs / watch: can't be counted
        self.loop_depth = 0


# ---------- config, hosts, files ----------

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
    authority = re.split(r"[/?#]", url.split("://", 1)[1] if "://" in url else url)[0]
    if "\\" in url or "@" in authority:
        return False  # user@host forms: browsers and urlparse disagree; don't guess (an @ in the query is fine)
    if "://" not in url:
        url = "http://" + url
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if not host:
        return False  # file://, about:, data: etc.
    return any(host == h or host.endswith("." + h) for h in hosts)


def expand(path):
    return os.path.expandvars(os.path.expanduser(path))


def resolve(path, cwd):
    """Absolute real path; for a glob, its directory part."""
    path = expand(path)
    if path.startswith("file:"):
        path = path[5:].split("?")[0]
    head = re.split(r"[*?\[{]", path)[0]
    if head != path:
        head = head if head.endswith("/") else os.path.dirname(head)
    return os.path.realpath(os.path.join(cwd, head or "."))


def session_files():
    files = {os.path.realpath(CONFIG.with_name("qa-state.json"))}
    if os.environ.get("PLAYWRIGHT_MCP_STORAGE_STATE"):  # the user's own browser cookie jar
        files.add(os.path.realpath(expand(os.environ["PLAYWRIGHT_MCP_STORAGE_STATE"])))
    return files


def session_dirs():
    """The folders that directly hold a session file (never ~ or /). Their parents are protected only from
    tools that read recursively (is_session_ancestor), so ordinary commands in a project stay allowed."""
    home = os.path.realpath(os.path.expanduser("~"))
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
    base = os.path.realpath(os.path.join(cwd, base_rel))
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
    return os.path.realpath(os.path.join(cwd, expand(path))) in session_files()


def is_session_ref(token, cwds):
    t = expand(token)
    if t.startswith("file:"):
        t = t[5:].split("?")[0]
    files, code_like = session_files(), bool(NOT_A_PATH_RE.search(t))  # JS/CSS words aren't file globs
    if not code_like and os.path.basename(t).startswith("qa-state"):  # a copy or the file seen from anywhere
        return True
    for c in cwds:
        if os.path.realpath(os.path.join(c, t)) in files:
            return True
        if code_like:
            continue
        if GLOB_RE.search(t):  # `cat ~/.config/*/*.json`, `tar c ~/.config/qa-*`: a session file or its folder?
            if glob_hits(t, c, files | session_dirs()):
                return True
        elif os.path.realpath(os.path.join(c, t)) in session_dirs():
            return True
    return False


def is_session_ancestor(token, cwds):
    """A folder above a session file, at any level (~, /): a recursive content search from it reads the file."""
    for c in cwds:
        p = resolve(expand(token), c).rstrip(os.sep) + os.sep
        if any(f.startswith(p) for f in session_files()):
            return True
    return False


def glob_may_match_session(glob, base):
    """Could a Grep glob like '*.json', '**/*.{ts,json}' or '.config/**' (relative to base) select a session file?"""
    done = brace_alts(glob or "")
    return any(fnmatch(f, a) or fnmatch(os.path.basename(f), a) or fnmatch(os.path.relpath(f, base), a)
               for f in session_files() for a in done)


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


def touches_db(path, cwds, parent_counts):
    t = expand(path)
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
            for key in ("deadline", "budget", "used"):
                if marker.get(key) is not None:
                    float(marker[key])
            if marker.get("faults") not in (None, True, False):
                raise ValueError("faults must be true or false")
        except (OSError, ValueError, TypeError, KeyError) as e:
            return path, None, f"{path} is malformed ({e}); rewrite it per common.md C4"
        if expires > time.time():
            return path, marker, None
    return None, None, None


def spend_budget(path, n, explorer=False):
    """Atomically count n playwright-cli commands against the run; deny reason when it's used up."""
    lock = open(path.with_name(".active.lock"), "w")
    try:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        marker = json.loads(path.read_text())
        deadline, budget = float(marker.get("deadline") or 0), int(float(marker.get("budget") or 0))
        used = int(float(marker.get("used") or 0))
        if deadline and time.time() > deadline + 600 and not explorer:  # a crashed run left it behind (user hint)
            return f"a qa-sdet run marker expired over 10 minutes ago; if no run is active, delete {path}"
        if deadline and time.time() > deadline:
            return "the run's time budget is used up: close the browser, write the report and run.json now"
        if budget and used + n > budget:
            return (f"the run's budget of {budget} playwright-cli commands is used up ({used} used): "
                    "close the browser, write the report and run.json now")
        marker["used"] = used + n
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
    command: `2>&1` -> `2>FD:1`, `&>f` -> `>f`, `>|f` -> `>f`."""
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
        elif c in "'\"":
            q = c
        elif c == "\\" and i + 1 < len(cmd):
            out.append(cmd[i:i + 2])
            i += 2
            continue
        elif c == "#" and (i == 0 or cmd[i - 1] in " \t\n;&|("):
            while i < len(cmd) and cmd[i] != "\n":
                i += 1
            continue
        elif c == "\n":
            c = ";"
        elif c in "<>" and cmd[i + 1:i + 2] == "&":
            out.append(c + "FD:")
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


def match_paren(cmd, k):
    """Index of the ')' closing the '(' at cmd[k] (len(cmd) if unclosed)."""
    depth, j = 0, k
    while j < len(cmd):
        depth += {"(": 1, ")": -1}.get(cmd[j], 0)
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
            elif c == "#" and (i == 0 or line[i - 1] in " \t;&|("):
                break  # comment to end of line
            elif line.startswith("<<", i) and (m := HEREDOC_RE.match(line, i)):
                pending.append((m.group(1) == "-", m.group(3), []))
                i = m.end()
                continue
            i += 1
    docs += ["\n".join(body) for _, _, body in pending]  # unterminated: bash reads to EOF
    return "\n".join(out), docs


def tokens(cmd):
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=PUNCT)
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def split_redirects(seg):
    """(words, output targets, input targets). Fd duplications (FD:n) aren't files."""
    words, outs, ins, i = [], [], [], 0
    while i < len(seg):
        m = REDIRECT_RE.match(seg[i])
        if m:
            target = m.group(3)
            if not target and i + 1 < len(seg):
                i += 1
                target = seg[i]
            if target and not target.startswith("FD:"):
                (outs if m.group(2).startswith(">") else ins if m.group(2) == "<" else []).append(target)
        else:
            words.append(seg[i])
        i += 1
    return words, outs, ins


# ---------- per-command checks ----------

def prog_name(tok):
    if tok.startswith("@playwright/cli"):
        return "playwright-cli"
    return re.sub(r"@[^/]*$", "", os.path.basename(tok)).removesuffix(".js")


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
    for a in rest:
        if a == "--":
            return False
        if skip:
            skip = False
        elif a in ("--help", "-h"):
            return True
        elif a.startswith("-"):
            skip = a in PW_VALUE_OPTS
    return False


def pw_args(rest):
    out, skip = [], False
    for a in rest:
        if skip:
            skip = False
        elif a.startswith("-"):
            skip = a in PW_VALUE_OPTS
        else:
            out.append(a)
    if out and prog_name(out[0]) == "playwright-cli":  # npx --package @playwright/cli playwright-cli ...
        out = out[1:]
    return out


def pw_file_args(rest, verb, args):
    """Paths playwright-cli would write or read: --filename values, video-start/upload/drop paths."""
    files = []
    for i, a in enumerate(rest):
        if a.startswith("--filename="):
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
    for f in pw_file_args(rest, verb, args):
        if touches_db(f, cwds, False):
            return "playwright-cli writing into the qa-sdet memory DB directory is blocked"
        if verb not in ("state-load", "state-save") and is_session_ref(f, cwds):
            return "the saved browser session holds live cookies; only state-load/state-save may use it"
    if verb == "state-save" and not (len(args) > 1 and is_session_file(args[1], next(iter(cwds)))):
        return "playwright-cli state-save only to ~/.config/qa-sdet/qa-state.json (via /qa-sdet:qa-login)"
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


def storage_value_read(js):
    """Any use of localStorage/sessionStorage except key names, .length and writes: returning the Storage
    object prints every value, and `localStorage.token` reads one."""
    for m in re.finditer(r"\b(local|session)Storage\b", js):
        before, after = js[:m.start()], js[m.end():]
        if re.search(r"(Object\.keys\(\s*(window\.)?|\bin\s+(window\.)?|typeof\s+(window\.)?)$", before):
            continue  # key names / existence checks
        if re.match(r"\s*\.\s*(length|key|clear|setItem|removeItem)\b", after):
            continue
        if re.search(r"['\"`]$", before) and re.match(r"['\"`]", after):
            continue  # the word inside a string literal, e.g. innerText.includes('localStorage')
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


def touches_marker(name, rest, outs, cwds, markers):
    """Would this command change or remove the run marker? Paths, globs, and folders that contain it."""
    reason = "the run marker belongs to the calling skill: read it if you like, but leave it unchanged and carry on testing"
    opts = set(rest)
    removes_folders = (name in ("mv", "rmdir", "trash", "trash-put", "gio")  # only these can take the folder with them
                       or (name == "rm" and any(re.match(r"^-[a-zA-Z]*[rR]", a) or a == "--recursive" for a in rest))
                       or (name == "find" and ("-delete" in opts or any(
                           prog_name((shlex.split(x) or [""])[0]) in REMOVERS for x in find_exec(rest))))
                       or (name == "rsync" and any(a.startswith(("--delete", "--remove-source")) for a in rest)))
    read_only = name in READ_ONLY_TOOLS or (name == "find" and not removes_folders)  # `find -newer qa-out/.active`
    args = [] if read_only else [x.split("=", 1)[1] if x.startswith("-") and "=" in x else x for x in rest]
    embedded = [m.group(1) for x in rest if (m := re.search(r">>?([^\s>]+)$", x))]  # `printf x>qa-out/.active`
    for a in args + outs + embedded:
        if a.startswith("-") or NOT_A_PATH_RE.search(a):
            continue
        for c in cwds:
            t = expand(a)
            if GLOB_RE.search(t):
                parents = {m for m in markers for p in glob_hits(t, c, {os.path.dirname(m)}) if p} if removes_folders else set()
                if glob_hits(t, c, markers) or parents:
                    return reason
                continue
            p = os.path.realpath(os.path.join(c, t))
            if p in markers or (removes_folders and any(m.startswith(p.rstrip(os.sep) + os.sep) for m in markers)):
                return reason
    return None


def check_acli(rest):
    positional = []
    for a in rest:
        if a.startswith("-"):
            break
        positional.append(a)
    # Help only when it isn't the value of a preceding flag (`--summary -h` would create the issue).
    if rest and rest[-1] in ("--help", "-h") and (len(rest) < 2 or not rest[-2].startswith("-")):
        return None
    action = next((a for a in acli_words(positional) if is_acli_read(a) or a in ACLI_WRITE), None)
    if not positional or (action and is_acli_read(action)):  # `acli --version`, reads; the first action word decides
        return None
    if "auth" in positional:
        nxt = positional[positional.index("auth") + 1:positional.index("auth") + 2]
        if nxt and nxt[0] in ACLI_AUTH_OK:
            return None
    return "acli write command blocked: qa-sdet only reads Jira and drafts content; the user files it"


def check_db(words, outs, name, rest, cwds):
    embedded = [m.group(1) for w in words if (m := re.search(r">>?(\S+)$", w))]  # `echo x>file`
    for target in outs + embedded:
        if touches_db(target, cwds, False):
            return "redirecting output onto the qa-sdet memory DB is blocked"
    opts = [a for a in rest if a.startswith("-")]
    paths = [a for a in rest if not a.startswith("-")]
    recursive = any(re.match(r"^-[a-zA-Z]*[rR]", o) or o == "--recursive" for o in opts)
    if name == "find":
        start = []
        for a in rest:
            if a.startswith("-") or a in ("(", "!"):
                break
            start.append(a)
        names = [rest[k + 1] for k, a in enumerate(rest[:-1]) if a in ("-name", "-iname")]
        parent = not names or any(fnmatch(f, n) for n in names for f in db_names())
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
    if name in DB_WRITERS:
        targets = paths if name == "tee" else paths[-1:]
        for i, a in enumerate(rest):
            if a in ("-t", "--target-directory") and i + 1 < len(rest):
                targets.append(rest[i + 1])
            elif a.startswith("--target-directory="):
                targets.append(a.split("=", 1)[1])
        if any(touches_db(t, cwds, False) for t in targets):
            return f"{name} onto the qa-sdet memory DB is blocked"
    if name == "sqlite3" and "-readonly" not in rest and any(touches_db(a, cwds, False) for a in paths):
        return "sqlite3 on the qa-sdet DB must use -readonly (only qa-mem writes it)"
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


def check(cmd, cwd, ctx=None, depth=0, explorer=False):
    """Deny reason for a Bash command, or None. At the top level a passing command is charged to the
    active run's budget (only then: a denied command spends nothing)."""
    top = ctx is None
    ctx = ctx or Ctx(cwd, explorer)
    ctx.raw = ctx.raw or cmd
    reason = _check(cmd, cwd, ctx, depth)
    if top and not reason and ctx.spend and ctx.marker_path:
        if ctx.marker_error:
            return ctx.marker_error
        if ctx.marker:
            if ctx.loop:
                return ("playwright-cli in a loop, xargs or watch can't be counted against the run budget; "
                        "run commands one by one, or batch input values in one eval")
            reason = spend_budget(ctx.marker_path, ctx.spend, ctx.explorer)
    return reason


def _check(cmd, cwd, ctx, depth):
    if depth > 4:
        return "nested shell too deep to verify"
    cmd = re.sub(r"\\\n", " ", cmd)  # line continuations
    mentions = bool(GUARDED_TOOLS_RE.search(ctx.raw or cmd))  # whole call: `PW=playwright-cli; $($PW goto x)`
    cmd, docs = split_heredocs(cmd)
    bodies, cmd = substitutions(normalize(cmd))
    for body in bodies:
        if r := _check(body.replace(QUOTED_GT, ">").replace(QUOTED_LT, "<"), cwd, ctx, depth + 1):
            return r
    try:
        toks = tokens(cmd)
    except ValueError:
        if GUARDED_ANY_RE.search(cmd) or DATA_DIR in expand(cmd):
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
        return None  # `case WORD in PATTERN`: compares text, runs and reads nothing (the branches are their own segments)
    i, wrappers, env_dir, env_split = command_word(words)
    here = next(iter(cwds))
    if env_split and (r := _check(env_split, here, ctx, depth + 1)):
        return r
    if any(is_session_ref(a, cwds) for a in ins):  # `cat < f`, `$(< f)`, `done < f`, bare `< f`: all print it
        return "the saved browser session holds live cookies; only playwright-cli state-load/state-save may use it"
    if i is None:
        return check_db(words, outs, "", [], cwds)  # bare redirection, e.g. `: > file`
    if (words[i].startswith("$") or words[i] == "SUBST") and mentions_guarded:
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
    pw_session_verb = name == "playwright-cli" and (pw_args(rest)[:1] or [""])[0] in ("state-load", "state-save")
    lists_names = name == "find" and not {"-exec", "-execdir", "-ok", "-okdir"} & set(rest)  # names, not contents
    checked = [a for a in words[i:] + outs + ins if not a.startswith("-")]
    if name in COPY_TOOLS and checked[1:]:  # `cp x ~/.config/qa-sdet/`: a session folder as the destination is fine
        last = checked[-1]
        if not any(os.path.realpath(os.path.join(c, expand(last))) in session_files() for c in local):
            checked = checked[:-1]
    if name in GREP_TOOLS and checked[1:] and not {"-e", "-f", "--regexp", "--file"} & set(rest):
        checked.pop(1)  # `grep -rn "qa-state" src`: the first word is the pattern, not a file
    opt_values = [a.split("=", 1)[1] for a in words[i + 1:] if a.startswith("-") and "=" in a]  # --from-file=PATH
    if not (pw_session_verb or lists_names) and name not in SESSION_FILE_OK and (
            any(is_session_ref(a, local) for a in checked)
            or any(os.path.realpath(os.path.join(c, expand(v))) in session_files() for v in opt_values for c in local)):
        return "the saved browser session holds live cookies; only playwright-cli state-load/state-save may use it"
    if env_dir and any(is_session_ref(a, local) for a in ins):  # `env -C dir cat < f`
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
    if ctx.explorer and name != "playwright-cli" and (r := touches_marker(name, rest, outs, local, ctx.marker_files)):
        return r
    code = interpreter_code(rest, docs)
    if name in INTERPRETERS and (re.search(r"qa-state\.json", code) or mentions_session_path(code)):
        return ("a script reading the saved browser session would print its cookies; check it with ls -l / stat, "
                "or re-run /qa-sdet:qa-login")  # python -c "...open('~/.config/qa-sdet/qa-state.json')..."
    if ctx.explorer and name in INTERPRETERS and re.search(r"qa-out/+\.active\b", " ".join(rest)):  # python -c "os.remove(...)"
        return ("the run marker belongs to the calling skill: read it if you like, but leave it unchanged and carry on "
                "testing (use cat to read it)")
    lookup_only = "command" in wrappers and bool({"-v", "-V"} & set(words[:i]))  # `command -v curl` runs nothing
    if ctx.explorer and name in NET_CLIS and not lookup_only:
        return (f"{name} reaches the network outside the browser (no host or budget check); observe through "
                "playwright-cli: requests, response-body, or --raw eval \"fetch('/api/x').then(r => r.status)\"")
    r = None
    via_loop = bool(LOOP_WRAPPERS & set(wrappers))
    if name in SHELLS or (name == "busybox" and rest[:1] and rest[0] in SHELLS):
        for j, a in enumerate(rest):
            if re.match(r"^-[a-z]*c[a-z]*$", a) and j + 1 < len(rest):
                r = _check(rest[j + 1], here, ctx, depth + 1)
                break
        else:  # `bash <<EOF`, `sh -s <<EOF`, `... | bash`: heredoc bodies are the script
            for body in docs:
                if r := _check(body, here, ctx, depth + 1):
                    break
    elif name == "eval":
        r = _check(" ".join(rest), here, ctx, depth + 1)
    elif name == "watch":
        ctx.loop_depth += 1
        r = _check(" ".join(rest), here, ctx, depth + 1)
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
    return r or check_db(words, outs, name, rest, local)


# ---------- hook entry point ----------

def mentions_session_path(text, folders=False):
    """Does the text name a session file (path, ~-path, $HOME-path or file name)? folders=True also counts its
    folder: right for the crash fallback, too wide for code (`open('~/.config/qa-sdet/config.json')` is fine)."""
    home = os.path.expanduser("~")
    for f in session_files():
        forms = {f, os.path.basename(f)} | ({os.path.dirname(f)} if folders else set())
        if f.startswith(home + os.sep):
            forms |= {"~" + f[len(home):], "$HOME" + f[len(home):]}
            if folders:
                forms.add("~" + os.path.dirname(f)[len(home):])
        if any(x and x in text for x in forms):
            return True
    return False


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": f"qa-sdet guard: {reason}",
    }}))


def decide(data):
    tool, tin = data.get("tool_name"), data.get("tool_input") or {}
    cwd = str(data.get("cwd") or os.getcwd())
    is_explorer = str(data.get("agent_type") or "").split(":")[-1] == "qa-explorer"
    if is_explorer:  # the budget it runs under isn't its own
        target = Path(os.path.normpath(os.path.join(cwd, str(tin.get("file_path") or "x"))))
        if tool in ("Write", "Edit", "MultiEdit") and target.name == ".active" and target.parent.name == "qa-out":
            return "the run marker belongs to the calling skill: read it if you like, but leave it unchanged and carry on testing"
    if tool in ("Read", "Grep"):
        target = str(tin.get("file_path") or tin.get("path") or "")
        if target and (is_session_file(target, cwd) or (tool == "Grep" and is_session_ref(target, {cwd}))):
            return "the saved browser session holds live cookies; it stays out of the conversation"
        if (tool == "Grep" and tin.get("output_mode") == "content" and is_session_ancestor(target or ".", {cwd})
                and (not tin.get("glob") or glob_may_match_session(str(tin["glob"]), resolve(target or ".", cwd)))):
            return ("a content search from this folder would read the saved browser session; "
                    "narrow the path or glob, or use files_with_matches")
        return None
    if tool == "Bash":
        return check(str(tin.get("command") or ""), cwd, explorer=is_explorer)
    return None


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
        reason = decide(data)
    except Exception as e:  # noqa: BLE001 - a crashing guard must not mean "allowed"
        reason = (f"guard error ({type(e).__name__}) on a command that mentions a guarded tool or file"
                  if GUARDED_ANY_RE.search(text) or DATA_DIR in text or mentions_session_path(text, folders=True) else None)
    if reason:
        deny(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
