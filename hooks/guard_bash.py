"""The guard's check of one Bash command: the odd forms it won't read, each segment's rules, and the calls it
approves at once."""

from __future__ import annotations

import os
import re
from itertools import takewhile

import guard_files
import guard_paths
import guard_shell
import guard_tools

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
NET_CLIS = set(
    "curl wget http https xh httpie nc ncat netcat telnet socat aria2c ssh scp sftp ftp lynx w3m links elinks".split()
)
PW_ENV_RE = re.compile(
    r"(PLAYWRIGHT_MCP_|PWTEST_)\w*=|\b(export|declare|typeset|readonly|local)\b[^;&|\n]*?\b(PLAYWRIGHT_MCP_|PWTEST_)"
)
MSG_OPT_RE = re.compile(r"(-m|--message|--body|--title)(=|$)")  # its value is text (a commit message), not a path
POS_PARAM_RE = re.compile(r"^\$(\{?[@*]\}?|\{?[1-9]\}?)$")  # "$@", $1: a function passing its arguments through
ONLY_VARS_RE = re.compile(r"(?:\s*(?:\$\{?\w+\}?|\x04))+\s*")  # wholly $VAR / $(...); one way to split spaces: linear
ODD = "{} can't be checked; run the guarded command directly (spell out program, verb and paths)"
CONFIG_REASON = (
    "the qa-sdet config holds allowedHosts (cli.config.json picks the browser): read them, but leave them unchanged"
)
DB_REASON = "{} on the qa-sdet memory DB directory is blocked (only qa-mem writes it)"


# ---------- odd forms ----------
INLINE_REASON = (
    "the explorer runs no inline code (python -c, node -e, perl -e, a pipe or heredoc into an interpreter): a "
    "page it read could have steered it, and the guard can't read what the code does (rewrite the run marker, "
    "reach another host); read files with Read or jq, and use qa-mem and playwright-cli for the rest"
)


@guard_paths.record
class Command:
    """One simple command of a Bash call, as check_segment read it."""

    words: list[str]  # every word of it, the wrappers in front included
    i: int | None  # where its program is in `words`; None: it runs none
    name: str  # the program, without its folder; "" for none
    rest: list[str]  # the words after the program
    wrappers: list[str]  # the wrappers in front of the program: env, xargs, npx, ...
    outs: list[str]  # the files it redirects output to; check_pw adds the ones playwright-cli reads and writes
    ins: list[str]  # the files it redirects input from
    feeds: list[str]  # what it reads on stdin from here-strings and heredocs
    ndocs: int  # how many heredocs it reads
    run: tuple[str | None, str | None]  # how a shell or an interpreter gets its code: guard_shell.how_run
    q: list[str] | None  # the arguments after qa-mem when it runs qa-mem
    cwds: set[str]  # the folders it may run in


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


def explorer_runs_code(cmd: Command, ctx):
    """Deny reason when the explorer runs a script file, inline code or a package (a page it read could have steered it to
    write or name one); None otherwise."""
    kind = cmd.run[0]
    by_path = (
        cmd.i is not None
        and "/" in cmd.words[cmd.i]
        and not (
            any(guard_paths.real(cmd.words[cmd.i], c) == guard_paths.PLUGIN_QA_MEM for c in cmd.cwds)
            or re.match(r"/(usr|bin|sbin|opt|snap)/", cmd.words[cmd.i])
        )
    )
    if ctx.explorer and (kind == "file" or (kind == "stdin" and cmd.ins) or by_path):  # ./x.sh: may not exist yet
        return "the explorer runs no script files (a page it read could have steered it to write one); run the commands directly"
    if ctx.explorer and inline_code(cmd.name, cmd.rest, kind):
        return INLINE_REASON
    if ctx.explorer and (
        PKG_RE.fullmatch(cmd.name)
        or "npx" in cmd.wrappers
        or (
            re.fullmatch(r"python[\d.]*", cmd.name)  # npx is a wrapper
            and re.search(r"(^|\s)-m\s*(pip[\d.]*|pipx|uv|ensurepip)\b", " ".join(cmd.rest))
        )
    ):
        return (
            "the explorer installs and runs no packages (npm, npx, pip, ...: they download code from other hosts and run "
            "it); test through the browser, or curl to an allowed host"
        )


def odd_form(cmd: Command, ctx):
    """Deny reason for a form the guard doesn't read (O1-O9, in a guarded call), or the explorer running a script file or inline code."""
    name, rest, words, i, q, wrappers = cmd.name, cmd.rest, cmd.words, cmd.i, cmd.q, cmd.wrappers
    kind, val = cmd.run
    reason = explorer_runs_code(cmd, ctx)
    if reason is not None:
        return reason
    if not ctx.guarded:
        return None
    pos = guard_tools.pw_args(rest)
    runs_pkg = name in ("bunx", "pnpx") or bool({"exec", "x", "dlx"} & set(rest))  # `npm i @playwright/cli` installs it
    hidden = lambda t: guard_paths.mentions_guarded(t, ctx.explorer) or (ctx.area and guard_shell.dynamic(t))
    forms = [
        (i is not None and guard_shell.dynamic(words[i]), "a program name from $VAR or $(...)"),  # O1
        (
            name == "playwright-cli"
            and not guard_tools.pw_help(rest)
            and (
                (pos and guard_shell.dynamic(pos[0]))
                or any(map(POS_PARAM_RE.match, rest))  # O2
                or (pos[:1] == ["eval"] and any(ONLY_VARS_RE.fullmatch(a) for a in pos[1:]))
            ),
            'a playwright-cli verb, eval text or arguments from $VAR, $(...) or "$@"',
        ),
        (
            name == "acli" and any(map(guard_shell.dynamic, guard_tools.acli_head(rest))),
            "acli command words from $VAR or $(...)",
        ),
        (q and guard_shell.dynamic(q[0]), "a qa-mem subcommand from $VAR or $(...)"),
        (name == "alias" and guard_paths.GUARDED_TOOL_RE.search(" ".join(rest)), "an alias for a guarded tool"),
        (name == "eval" and any(map(guard_shell.dynamic, rest)), "eval of $VAR or $(...)"),  # O3
        (
            ("env" in wrappers and any(re.match(r"-[A-Za-z0-9]*S|--split-string", w) for w in words[:i]))
            or ("npx" in wrappers and any(w.split("=", 1)[0] in ("-c", "--call") for w in words[:i])),
            "a command string given to env -S or npx -c",
        ),  # O4
        (
            name in guard_shell.SHELLS and kind in ("file", "stdin"),
            f"{name} running a script file, a pipe, a heredoc or stdin",
        ),
        (
            INTERP_RE.fullmatch(name) and q is None and kind == "file" and guard_shell.dynamic(val),
            f"{name} running a script from $VAR",
        ),
        (
            INTERP_RE.fullmatch(name)
            and q is None
            and guard_paths.mentions_guarded(  # O5: inline code; stdin: the whole call
                ctx.raw if kind == "stdin" else " ".join(rest + cmd.feeds), ctx.explorer
            ),
            f"{name} code that names a guarded tool or path",
        ),
        ("xargs" in wrappers and (name in GUARDED or q is not None or ctx.area), f"{name} run by xargs"),  # O6
        (
            name
            and name not in TEXT_TOOLS | guard_shell.SHELLS | GUARDED | {"eval", "alias"}
            and not INTERP_RE.fullmatch(name)
            and any(  # O7
                (
                    guard_shell.prog_name(a.split("=", 1)[-1]) in GUARDED
                    and (a[:1] != "@" or runs_pkg)
                    and k + 1 < len(rest)
                    and (rest[k + 1].startswith("-") or "/" not in rest[k + 1])
                )
                or (re.search(r"\s", a) and hidden(a))
                or guard_shell.prog_name(a) in guard_shell.SHELLS - {".", "source"}
                for k, a in enumerate(rest)
            ),
            f"a guarded tool or command string run by {name}",
        ),
        (
            name not in TEXT_TOOLS and any(map(hidden, cmd.feeds)),  # O8
            f"{name or 'a command'} reading a heredoc or here-string",
        ),
        (
            ctx.area
            and any(
                map(
                    guard_shell.dynamic,
                    cmd.outs
                    + cmd.ins
                    + (
                        []
                        if name in guard_files.PRINTERS
                        else [
                            a
                            for k, a in enumerate(rest)
                            if not (
                                guard_paths.NOT_A_PATH_RE.search(a)
                                or MSG_OPT_RE.match(a)
                                or (k and MSG_OPT_RE.match(rest[k - 1]))
                            )
                        ]
                    ),
                )
            ),
            "a path from $VAR or $(...) in a call that names the qa-sdet data folder",
        ),
    ]  # O9
    return next((ODD.format(form) for hit, form in forms if hit), None)


# ---------- Bash commands ----------
class Ctx:
    """One Bash call: its run marker, what it names (worked out once), whether it can be approved."""

    def __init__(self, cwd, explorer=False, raw=""):
        self.explorer, self.raw = explorer, raw
        self.marker = guard_paths.read_marker(cwd)[1]
        self.guarded = guard_paths.mentions_guarded(raw, explorer)
        self.area = (
            guard_paths.mentions_data_dir(raw)
            or guard_paths.mentions_profile(raw)
            or (explorer and guard_paths.mentions_config(raw))
        )
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
    if len(cmd) > guard_shell.MAX_TEXT:
        ctx.block("its length")
        return "the command is too long to check; split it" if ctx.guarded else None
    if not depth and guard_shell.brace_expands(cmd):
        ctx.block("a brace expansion")  # the words checked below are not the words the shell runs
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
    cmd, docs = guard_shell.split_heredocs(cmd)
    docs += carried  # a substitution's heredoc bodies were split off the text around it
    bodies, cmd = guard_shell.substitutions(guard_shell.normalize(cmd))
    if docs or bodies:
        ctx.block("$(...) or a heredoc")
    for body in bodies:
        if r := _check(guard_shell.unmark(body), cwd, ctx, depth + 1, docs):
            return r
    try:
        toks = guard_shell.tokens(cmd)
    except ValueError:
        ctx.block("an unclosed quote")
        return ODD.format("text the shell can't parse (an unclosed quote)") if ctx.guarded else None
    cwds, stack, seg = Dirs({cwd}), [], []
    for tok in toks + [";"]:  # sentinel flushes the last segment
        if not tok or not set(tok) <= set(guard_shell.PUNCT):
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
_PLAIN_RE = re.compile(rf"[^$`{guard_shell.SUBST}]*")  # a word the shell passes on as written, quoted or not
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
        and not guard_paths.GLOB_RE.search(f)
        and any(
            guard_paths.real(f, cwd).startswith(b + os.sep) or (not strict and guard_paths.real(f, cwd) == b)
            for b in roots
        )
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
        return plain(rest) and not any(guard_paths.GLOB_RE.search(a) for a in rest)
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
    text = guard_shell.unmark(code[0])
    if name == "sed":
        return bool(_SED_PRINT.fullmatch(text) or _SED_SUBST.fullmatch(text))
    return (  # awk: `$1` and `$NF` are fields; any other `$` could be the shell's inside double quotes
        plain([text.replace("$", "")])
        and not re.search(r"\$(?!\d|NF\b)", text)
        and not guard_files.AWK_WRITES.search(text)
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
        files = guard_files.pos_args(rest, _HEAD_VAL_OPTS)
    else:
        has_pat, pos, pat_files, rec, unknown_long = guard_files.grep_parse(rest)
        if unknown_long:
            return False
        files = (pos if has_pat else pos[1:]) + pat_files
        if not files and rec:
            return False  # `grep -r` / `-d recurse` with no path walks the whole cwd
    return all(
        "$" not in f
        and not guard_paths.GLOB_RE.search(f)
        and any(guard_paths.real(f, cwd).startswith(b + os.sep) for b in roots)
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


RISKY_CLICK = {"delete", "remove"}  # a control's name holds one of them, as a word
# the words of a name or an id: zone-remove-button, btn_delete, deleteButton, DELETE_ROW ("Removed" is another word)
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")
RISKY_WORD = "Delete or Remove click"  # Ctx.block's word for it: PostToolUse tells the explorer, the trace counts it
SNAPSHOTS, SNAPSHOT_CAP = 5, 2 << 20  # the newest page-*.yml files read for a ref's name, and how much of each


def ref_label(ref, cwd):
    """The line the newest snapshot playwright-cli saved (.playwright-cli/page-*.yml, under the call's folder or the
    project's) has for `ref`, without the ref, or "": the element's role and name, and its text when it has no name
    (`- generic [ref=e15]: Delete`). After another page loads the refs start again, so the newest file that holds the
    ref stands. Never through a link, never more than SNAPSHOT_CAP of a file; any trouble is no line."""
    files = []
    for base in dict.fromkeys(b for b in (cwd, os.environ.get("CLAUDE_PROJECT_DIR")) if b):
        try:
            with os.scandir(os.path.join(base, ".playwright-cli")) as found:
                files += [
                    (e.stat(follow_symlinks=False).st_mtime, e.path)
                    for e in found
                    if e.name.startswith("page-") and e.name.endswith(".yml") and e.is_file(follow_symlinks=False)
                ]
        except OSError:
            continue
    mark = f"[ref={ref}]"
    for _, path in sorted(files, reverse=True)[:SNAPSHOTS]:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as f:
                text = f.read(SNAPSHOT_CAP).decode("utf-8", "replace")
        except OSError:
            continue
        line = next((x for x in text.splitlines() if mark in x), "")
        if line:
            return line.replace(mark, " ")
    return ""


def risky_click(rest, cwd):
    """Whether a playwright-cli click or dblclick is on a control named Delete or Remove: its locator says so, or a
    saved snapshot gives its ref that name (ref_label). Such a call is never approved, so Claude Code's own permission
    check decides it (found live: an approved click opened a Delete dialog on another user's record). A ref no saved
    snapshot holds is not held back: `find` prints refs and saves none."""
    args = guard_tools.pw_args(rest)
    if len(args) < 2 or args[0] not in ("click", "dblclick"):
        return False
    target = guard_shell.unmark(args[1])
    named = ref_label(target, cwd) if re.fullmatch(r"(?:f\d+)?e\d+", target) else target
    return any(w.lower() in RISKY_CLICK for w in _WORD_RE.findall(named))


def approvable(
    cmd: Command, cwd, explorer=False
):  # R8: playwright-cli, qa-mem, read-only acli, head/tail/grep, sleep <=30; for the explorer also _read_only
    bases = {os.path.realpath(os.path.join(b, "qa-out")) for b in (cwd, os.environ.get("CLAUDE_PROJECT_DIR")) if b}
    if (
        cmd.i != 0
        or cmd.ins
        or cmd.feeds
        or cmd.ndocs
        or any(
            o not in ("/dev/null", "/dev/stdout", "/dev/stderr")
            and ("$" in o or not any(guard_paths.real(o, cwd).startswith(b + os.sep) for b in bases))
            for o in map(guard_shell.unmark, cmd.outs)
        )
    ):
        return False
    q = guard_shell.qamem_args(cmd.name, cmd.rest, {cwd}, allow_var=False)
    return (
        cmd.name == "playwright-cli"
        or (cmd.name == "acli" and _acli_approvable(cmd.rest))
        or (cmd.name in ("head", "tail", "grep") and _filter_ok(cmd.name, cmd.rest, cwd, bases))
        or (
            cmd.name == "sleep"
            and len(cmd.rest) == 1
            and bool(re.fullmatch(r"\d+(?:\.\d+)?", cmd.rest[0]))
            and float(cmd.rest[0]) <= 30
        )
        or (q is not None and not (q[:1] == ["setup-permissions"] and "--apply" in q))
        or (  # never with a file redirect: `echo … > run-x.json` would be a write the guard hasn't read
            explorer
            and all(guard_shell.unmark(o) in ("/dev/null", "/dev/stdout", "/dev/stderr") for o in cmd.outs)
            and _read_only(cmd.name, cmd.rest, cwd, bases)
        )
    )


class Dirs(set):
    """The folders a call's later commands may run in, and the ones `cd -` returns to after a cd (None: unknown, as
    the shell's OLDPWD before this call). Without it `cd a; cd -` grew the set, and every path in it, pair by pair."""

    old: set[str] | None = None

    def snapshot(self):
        """A copy for a subshell's `( … )`: a cd inside it doesn't leak out."""
        d = Dirs(self)
        d.old = self.old
        return d


def check_segment(seg, cwds, start, ctx, depth, docs):
    """Check one simple command. Mutates cwds on cd."""
    words, outs, ins, here, ndocs = guard_shell.split_redirects(seg)
    redirects = list(outs)  # check_pw adds the files playwright-cli reads and writes to outs
    feeds = here + (list(docs) if ndocs else [])  # what it reads on stdin from here-strings and heredocs
    if words[:1] in (["case"], ["for"], ["select"]) and not (outs or ins or feeds):
        ctx.block(words[0])
        return None  # a list of words (a $(...) in it was checked as its own command)
    i, wrappers, lookup, env_dir = guard_shell.command_word(words)
    name, rest = (guard_shell.prog_name(words[i]), words[i + 1 :]) if i is not None else ("", [])
    local = {guard_paths.real(env_dir, c) for c in cwds} if env_dir else cwds
    q = guard_shell.qamem_args(name, rest, local)
    run = (
        guard_shell.how_run(name, rest)
        if name in guard_shell.SHELLS or (INTERP_RE.fullmatch(name) and q is None)
        else (None, None)
    )
    cmd = Command(
        words=words,
        i=i,
        name=name,
        rest=rest,
        wrappers=wrappers,
        outs=outs,
        ins=ins,
        feeds=feeds,
        ndocs=ndocs,
        run=run,
        q=q,
        cwds=local,
    )
    if (r := odd_form(cmd, ctx)) or lookup:
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
            else {guard_paths.real(target, c) for c in cwds}
        )
        here = {os.path.realpath(start)}  # the explorer's `cd <where the call already runs> && …` changes nothing
        if not (ctx.explorer and i == 0 and name == "cd" and target != "-" and new == here and set(cwds) == {start}):
            ctx.block(name)
        if isinstance(cwds, Dirs):
            cwds.old = set(cwds)
        return cwds.clear() or cwds.update(new)
    here_cwd = next(iter(local))  # `bash -c '…'` and `eval '…'`: their text is a command
    r = (
        _check(guard_shell.unmark(run[1]), here_cwd, ctx, depth + 1, docs)
        if run[0] == "c"
        else _check(guard_shell.unmark(" ".join(rest)), here_cwd, ctx, depth + 1, docs)
        if name == "eval"
        else guard_tools.check_pw(rest, ctx, local, outs)
        if name == "playwright-cli"
        else guard_tools.check_acli(rest)
        if name == "acli"
        else guard_tools.check_qamem(q, ctx, local)
        if q is not None
        else None
    ) or (guard_tools.explorer_net(name, rest, ctx.raw) if ctx.explorer and name in NET_CLIS else None)
    if name == "playwright-cli" and risky_click(rest, start):
        ctx.block(RISKY_WORD)
    if not approvable(cmd, start, ctx.explorer):
        ctx.block(name if i == 0 and name else "a prefix or wrapper")
    return (
        r
        or guard_files.check_profile(
            name, rest, outs, ins, local, guard_tools.pw_profile_values(rest) if name == "playwright-cli" else ()
        )
        or (
            DB_REASON.format(name or "a redirection")
            if guard_files.changes(name, rest, outs, local, guard_paths.DATA_DIR)
            else None
        )
        or (
            CONFIG_REASON
            if ctx.explorer
            and any(guard_files.changes(name, rest, outs, local, f) for f in guard_files.config_files(local))
            else None
        )
        or (
            guard_tools.MARKER_REASON
            if ctx.explorer
            and any(guard_files.changes(name, rest, outs, local, f) for f in guard_files.marker_files(local))
            else None
        )
        or (
            guard_paths.run_files_reason()
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
    args = guard_tools.pw_args(rest)
    return guard_shell.opt_values(rest, "--filename") + (
        args[1:] if (args or [""])[0] in ("video-start", "pdf") else []
    )


def explorer_out_ok(out, cwds):
    """A redirection or playwright-cli file of the explorer: a /dev stream or one of its run files."""
    o = guard_shell.unmark(out)
    return o in ("/dev/null", "/dev/stdout", "/dev/stderr") or (
        "$" not in o and all(guard_paths.run_file(guard_paths.real(o, c), cwds) for c in cwds)
    )
