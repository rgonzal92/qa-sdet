"""Shell text as the guard reads it: quotes and substitutions normalised, heredocs and redirects split off, and
the program each segment runs."""

import os
import re
import shlex

import guard_paths

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
HEREDOC_RE = re.compile(
    r"""(?<![<$(\d])<<(?!<)(-?)[ \t]*((?:[^\s<>;&|()'"\\]|\\.|'[^']*'|"[^"]*")+)"""
)  # <<'E"OF' ends at EOF
REDIR_OP_RE = re.compile(r"<<<?|<>|>>?|<")  # unquoted redirection operators (quoted ones are QUOTED_GT/LT)


# ---------- text pre-processing ----------
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


# ---------- programs ----------
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
    if any(guard_paths.real(script, c) == guard_paths.PLUGIN_QA_MEM for c in (cwds or (os.getcwd(),))) or (
        allow_var and _PLUGIN_QAMEM_VAR_RE.fullmatch(script) and "$" in guard_paths.expand(script)
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
