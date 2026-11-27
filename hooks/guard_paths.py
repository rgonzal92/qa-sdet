"""The guard's hosts, paths and hook input: the allowed hosts, the folders it protects (the browser profile, the
data folder, the config), the run marker, an explorer's run files, and the caller's role and working folder."""

import glob
import json
import math
import os
import re
import time
from fnmatch import fnmatch
from itertools import islice
from pathlib import Path

# ---------- constants ----------
CONFIG = Path(os.environ.get("QA_SDET_CONFIG") or "~/.config/qa-sdet/config.json").expanduser()
DATA_DIR = os.path.realpath(Path(os.environ.get("QA_SDET_DATA") or "~/.local/share/qa-sdet").expanduser())
PLUGIN_QA_MEM = os.path.realpath(Path(__file__).resolve().parent.parent / "bin" / "qa-mem")
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
NOT_A_PATH_RE, GLOB_RE = re.compile(r"[\s()'\"`;|<>]"), re.compile(r"[*?\[]")  # code or text in an argument; a glob
EXPLORERS = {"qa-explorer"}  # the agent that reads untrusted pages: R4 holds for it
EXPLORER_FILES = {
    "bugs": r"[^/]+",
    "repro": r"[^/]+",
    "evidence": r"[^/]+",  # agents/qa-explorer.md "Files you write"
    "scratch": r"[^/]+",  # a snapshot to grep, a throwaway screenshot: the teardown empties it (ops.prune)
    ".data": r"run-[^/]+\.json|(requests|console)-[^/]+\.log|map-\d+\.png",
}
WRITE_TOOLS, PATH_KEYS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}, ("file_path", "notebook_path", "path")


# ---------- hosts and paths ----------
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


# ---------- run marker ----------
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


# ---------- an explorer's run files (R4) ----------
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


# ---------- the hook's input ----------
def clean(v):
    """A string from the input without NULs and lone surrogates: no path holds them, and os.path raises on them."""
    return str(v).encode("utf-8", "replace").decode("utf-8").replace("\0", "")


def tool_paths(tin):
    """Every path field present: a decoy `file_path` beside the `path` Grep uses must not hide the real target."""
    return [clean(tin[k]) for k in PATH_KEYS if tin.get(k)] or [""]


def agent_role(data):
    """qa-explorer / qa-mapper / "" from agent_type, tolerant of case and stray spaces ("QA-SDET:qa-explorer ")."""
    return str(data.get("agent_type") or "").strip().lower().split(":")[-1].strip()


def cwd_of(data):
    if data.get("cwd"):
        return clean(data["cwd"])
    try:
        return clean(os.getcwd())
    except OSError:
        return "/"
