"""What a command does to the guarded folders: reads of the browser profile (R3), writes to the data folder (R5),
the run marker and the config (R4)."""

import os
import re
from fnmatch import fnmatch
from itertools import takewhile

import guard_paths
import guard_shell
import guard_tools

PRINTERS, GREP_TOOLS = set("echo printf test [ [[ true :".split()), {"grep", "egrep", "fgrep", "rg", "ag"}
TREE_READERS = {"tar", "rsync", "rg", "ag", "tree", "git", "ln", "7z"}  # walk a folder: always, or with these flags
RECURSIVE = {"cp": "rRa", "scp": "r", "ls": "R", "du": "a", "grep": "rR", "egrep": "rR", "fgrep": "rR", "zip": "r"}
FIND_ACTIONS = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
REMOVERS = {"rm", "rmdir", "unlink", "mv", "shred", "truncate", "dd", "trash", "trash-put", "gio"}
WRITERS, INPLACE = {"cp", "tee", "install", "ln", "rsync"}, {"sed", "perl", "ruby", "gawk"}
PROFILE_REASON = (
    "the qa-sdet browser profile folder holds the signed-in QA session (cookies); only qa-mem browser-open "
    "(playwright-cli open --profile <main profile>) may use it"
)


# ---------- paths: the profile (R3), the data folder (R5), the config (R4) ----------
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


def grep_parse(rest):
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
        dirs = guard_shell.opt_values(rest, "-d") + [a[2:] for a in rest if a.startswith("-d") and len(a) > 2]
    elif name.startswith("7z"):
        dirs = guard_shell.opt_values(rest, "-o") + [a[2:] for a in rest if a.startswith("-o") and len(a) > 2]
    else:
        dirs = tar_dirs(rest)
        if old_tar and "C" in first:
            dirs += [a for a in rest[rest.index(first) + 1 :] if not a.startswith("-")]
    return dirs or ["."]


def check_profile(name, rest, outs, ins, cwds, skip=()):
    """R3: only echo/printf/test name a path in a profile folder or run there; no folder walker (tar, cp -r, grep -r,
    git, ...) starts in or above the data folder in HOME. skip: the --profile values check_pw allowed."""
    if name in PRINTERS:
        return (
            PROFILE_REASON
            if any(map(guard_paths.in_profile, cwds))
            or any(map(guard_paths.in_profile, guard_paths.paths(outs + ins, cwds)))
            else None
        )
    args = [a for a in rest if a not in skip and a.split("=", 1)[-1] not in skip]
    grep_rec = False
    if name in GREP_TOOLS:
        has_pat, pos, pat_files, grep_rec, _ = grep_parse(args)
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
        cwds | {guard_paths.real(d, c) for d in tar_dirs(rest) for c in cwds} if name in ("tar", "bsdtar") else cwds
    )  # `-C ~ .local`
    if (
        any(map(guard_paths.in_profile, cwds))
        or any(map(guard_paths.in_profile, guard_paths.paths(args + (udirs or []) + outs + ins, cwds)))
        or (walks and any(map(guard_paths.in_area, guard_paths.paths(srcs, src_cwds))))
    ):  # `tar c ~/.local/share/qa-sdet`, `grep -r x ~/.local`
        return PROFILE_REASON


_COPY_VAL_OPTS = set(
    "-S --suffix -t --target-directory -m --mode -o --owner -g --group -Z --context "
    "--backup-dir --compare-dest --copy-dest --link-dest -e --rsh --exclude --include --filter "
    "--chmod --chown --bwlimit --log-file".split()
)


def pos_args(rest, val_opts):
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
        return guard_shell.opt_values(rest, "--output") + [
            m.group(1) or (rest[k + 1 : k + 2] or [""])[0]
            for k, a in enumerate(rest)
            if (m := re.fullmatch(r"-[a-zA-Z]*o(.*)", a, re.S))
        ]
    if name == "uniq":
        return pos_args(rest, {"-f", "-s", "-w", "--skip-fields", "--skip-chars", "--check-chars"})[1:2]
    if name == "split":
        vals = {"-a", "-b", "-C", "-l", "-n", "-t", "--bytes", "--line-bytes", "--lines", "--number", "--separator"}
        return pos_args(rest, vals | {"--suffix-length", "--additional-suffix", "--filter"})[1:2]
    if name == "csplit":
        return guard_shell.opt_values(rest, "-f", "--prefix") + [a[2:] for a in rest if re.fullmatch(r"-f.+", a, re.S)]
    words = [guard_shell.unmark(a) for a in rest]  # a quoted `>` is marked: here it is the program's own redirection
    if re.fullmatch(r"g?sed", name):  # its script: the -e values, else the first operand (the rest are files)
        prog = SED_WRITES
        code = (
            guard_shell.opt_values(words, "-e", "--expression")
            or pos_args(words, {"-f", "--file", "-l", "--line-length"})[:1]
        )
    elif re.fullmatch(r"[gmn]?awk", name):
        prog, code = AWK_WRITES, pos_args(words, {"-F", "-v", "-f", "-W", "--file", "--assign"})[:1]
    else:
        return []
    if any(prog.search(c) for c in code):
        return [w.strip("\"'{}();") for a in words for w in (a, *re.split(r"[\s>|]+", a))]
    return []


def changes(name, rest, outs, cwds, root):
    """R5/R4: does it change root (the data folder, a config file) or remove a folder above it? Reads and copies don't."""

    def hit(w, up=False):
        return any(
            guard_paths.under(p, root) or (up and guard_paths.ancestor(p, root))
            for c in cwds
            for p in guard_paths.path_hits(w, c)
        )

    opts, pos = [a for a in rest if a.startswith("-")], [a for a in rest if not a.startswith("-")]
    if any(hit(o) for o in outs) or any(hit(w) for w in filter_writes(name, rest) if w):
        return True
    if name in guard_tools.HTTP_CLIENTS and any(hit(w) for w in http_writes(rest, cwds)):
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
    cpos = pos_args(rest, _COPY_VAL_OPTS) if name in WRITERS | {"mv"} else pos
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
    dirs = set(cwds) | {p for w in words for c in cwds if os.path.isdir(p := guard_paths.real(w, c))}
    return words + [os.path.join(d, n) for d in dirs for n in names]


def marker_files(cwds):
    """R6: the run marker the explorer runs under, as read_marker finds it (cwd or project, then qa-out/.active), and the
    hook's count of its calls beside it (the spin and failure nudges read it)."""
    bases = {b for b in (*cwds, os.environ.get("CLAUDE_PROJECT_DIR")) if b}
    names = (".active", ".active.progress")
    return {guard_paths.real(os.path.join(b, "qa-out", n), "/") for b in bases for n in names} | {
        guard_paths.real(os.path.join(b, n), "/")
        for b in bases
        for n in names
        if os.path.basename(os.path.normpath(b)) == "qa-out"
    }


def config_files(cwds):
    """R4: the qa-sdet config (allowedHosts) and the playwright-cli config files that would pick its browser."""
    return {os.path.realpath(guard_paths.CONFIG), guard_paths.real("~/.playwright/cli.config.json", "/")} | {
        guard_paths.real(".playwright/cli.config.json", c) for c in cwds
    }


_GREP_VAL_OPTS = set(
    "-e --regexp -f --file -m --max-count -A -B -C --after-context --before-context --context "
    "--include --exclude --exclude-dir --label --binary-files --devices --directories "
    "--color --colour -d -D".split()
)
