"""Config, the DB connection and schema, constants, and helpers the other modules share."""
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


CONFIG = Path(os.environ.get("QA_SDET_CONFIG", "~/.config/qa-sdet/config.json")).expanduser()
DATA_DIR = Path(os.environ.get("QA_SDET_DATA", "~/.local/share/qa-sdet")).expanduser()
MAX_TEXT = 300
MAX_FACTS, MAX_COVERAGE, MAX_RUNS = 100, 400, 2000  # per page, per page, total
UNKNOWN = "unknown"
STATUSES = {"OPEN", "FILED", "FIXED", "INVALID", "REGRESSED"}
BROWSERS = ("chrome", "msedge", "firefox", "webkit")  # playwright-cli --browser values
VISIBLE = ("OPEN", "FILED", "REGRESSED")  # bugs the explorer must know about
# Must match the IDs in references/heuristics.md.
HEURISTICS = [
    "happy-path", "boundary", "empty-null", "invalid-format", "unicode-whitespace",
    "duplicate", "sequence-repeat", "back-refresh", "sort-filter-paginate",
    "error-handling", "loading-empty-states", "keyboard-a11y", "console-errors",
    "network-failures", "permissions",
]

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS pages(route TEXT PRIMARY KEY, name TEXT, build TEXT, last_verified TEXT);
CREATE TABLE IF NOT EXISTS facts(id INTEGER PRIMARY KEY, page TEXT NOT NULL, kind TEXT NOT NULL,
  text TEXT NOT NULL, source TEXT, build TEXT, ts TEXT, stale INTEGER DEFAULT 0, UNIQUE(page, kind, text));
CREATE INDEX IF NOT EXISTS facts_page ON facts(page, kind);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(text, content='facts', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
  INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, text) VALUES ('delete', old.id, old.text); END;
CREATE TABLE IF NOT EXISTS locators(page TEXT, name TEXT, locator TEXT, build TEXT,
  last_verified TEXT, stale INTEGER DEFAULT 0, PRIMARY KEY(page, name));
CREATE TABLE IF NOT EXISTS coverage(page TEXT, target TEXT, heuristic TEXT, result TEXT, build TEXT,
  run_id TEXT, ts TEXT, PRIMARY KEY(page, target, heuristic, build));
CREATE INDEX IF NOT EXISTS coverage_page ON coverage(page, build);
CREATE TABLE IF NOT EXISTS bugs(hash TEXT PRIMARY KEY, page TEXT, title TEXT, severity TEXT,
  classification TEXT, status TEXT DEFAULT 'OPEN', draft_path TEXT, first_seen TEXT, last_seen TEXT,
  seen_count INTEGER DEFAULT 1, last_run TEXT, symptom TEXT);
CREATE INDEX IF NOT EXISTS bugs_page ON bugs(page, status);
CREATE TABLE IF NOT EXISTS sightings(run_id TEXT, src TEXT, hash TEXT, PRIMARY KEY(run_id, src, hash));
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, mode TEXT, target TEXT, build TEXT, status TEXT,
  summary TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS codemap(page TEXT NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL, value TEXT,
  file TEXT, repo TEXT NOT NULL, area TEXT NOT NULL, ts TEXT, src TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(page, kind, label, repo, area, src));
CREATE INDEX IF NOT EXISTS codemap_page ON codemap(page, kind);
"""


# meta keys of the removed orphan-route bookkeeping of the code map
STALE_META = ("(key IN ('codemap_routes_at_scan', 'codemap_lost', 'codemap_scan_repos') "
              "OR key LIKE 'codemap_reread:%' OR key LIKE 'codemap_pending:%')")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def config(strict=False):
    """The config file as a dict. strict=False: {} when it is missing or broken. strict=True: raises OSError (missing,
    unreadable) or ValueError (not JSON, or not one JSON object) so the caller can say what is wrong."""
    try:
        cfg = json.loads(CONFIG.read_text())
        if not isinstance(cfg, dict):
            raise ValueError('the file must be one JSON object: {"app": ..., "qaBaseUrl": ...}')
    except (OSError, ValueError):
        if strict:
            raise
        return {}
    return cfg


def db_path():
    if os.environ.get("QA_MEM_DB"):
        return Path(os.environ["QA_MEM_DB"])
    app = "default"
    try:
        app = str(config(strict=True).get("app") or app)
    except FileNotFoundError:
        pass
    except (ValueError, OSError) as e:
        print(f"qa-mem: ignoring unreadable {CONFIG}: {e}", file=sys.stderr)
    return DATA_DIR / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', app)}.db"


def connect():
    p = db_path()
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # app facts and bugs: owner only
    con = sqlite3.connect(p, timeout=10)
    con.row_factory = sqlite3.Row
    for table, cols in (("bugs", {"last_run", "symptom"}), ("codemap", {"src"})):  # no migrations: refuse a pre-0.4 DB
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if have and not cols <= have:
            con.close()
            raise BadInput(f"{p} is from before qa-sdet 0.4 ({table} lacks {', '.join(sorted(cols - have))}): move it "
                           "away; qa-mem starts a new one and /qa-sdet:qa-init maps the code again")
    con.executescript(SCHEMA)
    if con.execute(f"SELECT 1 FROM meta WHERE {STALE_META} LIMIT 1").fetchone():  # one-time clean-up (read first: no write lock)
        with con:
            con.execute(f"DELETE FROM meta WHERE {STALE_META}")
    return con


def txt(v, field):
    """Scalar field as str (or None). Lists/dicts are a bad run.json, not a transient error."""
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise ValueError(f"'{field}' must be a string")
    return str(v)


def norm_route(r):
    """'/form.html', 'http://host/form.html?x=1', '/form.html/' -> '/form.html'. Keeps SPA hash routes (#/..., #!/...)."""
    r = str(r or "").strip()
    if not r:
        return ""
    try:
        parts = urlsplit(r if "://" in r else "http://x" + ("" if r.startswith(("/", "#")) else "/") + r)
    except ValueError:  # 'http://[x': no route in it (ingest then reports the missing route; card finds no page)
        return ""
    path = _ids(parts.path) or "/"
    f = parts.fragment.split("?")[0]
    bang = "!" if f.startswith("!/") else ""  # AngularJS hashbang routes: #!/incidents
    frag = _ids(f[len(bang):]) if f[len(bang):].startswith("/") else ""
    return path + (f"#{bang}{frag}" if frag else "")


ID_SEGMENT = re.compile(r"\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{16,}|"
                        r":\w+|\{\w+\}|\[\.{0,3}\w+\]|<[^/>]+>|\$\{[^}/]*\}", re.I)  # route params: :id, {id}, [id], <id>


def _ids(path):
    """'/incidents/4521//' -> '/incidents/:id': one card and one bug hash for every record's detail page."""
    segs = [":id" if ID_SEGMENT.fullmatch(x) else x for x in (y.strip() for y in path.split("/")) if x]
    return ("/" + "/".join(segs)) if segs else ""


def cap(s):
    return " ".join(str(s or "").split())[:MAX_TEXT]


def norm(s):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).split())


def bug_hash(route, symptom):
    return hashlib.sha1(f"{norm(route)}|{norm(symptom)}".encode()).hexdigest()[:12]


def meta(con, key):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(con, key, value):
    con.execute("INSERT OR REPLACE INTO meta VALUES(?, ?)", (key, value))


def dicts(obj, key):
    items = obj.get(key) or []
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise ValueError(f"'{key}' must be a list of objects")
    return items


def heuristic_id(h):
    h = norm(h).replace(" ", "-")
    return h if h in HEURISTICS else f"other:{h}"


def coverage_result(r):
    """pass / fail / blocked, whatever the agent wrote: 'BLOCKED (needs --faults)', NOT_RUN and skipped
    mean not done, so the heuristic stays UNTRIED; anything else is kept short as written."""
    w = norm(r).split()
    if not w or w[0] in ("pass", "passed", "ok", "covered", "done"):
        return "pass"
    if w[0] in ("fail", "failed", "failure", "bug"):
        return "fail"
    if w[0] in ("blocked", "skipped", "skip", "untested", "na", "not", "notrun", "none", "pending", "question"):
        return "blocked"
    return cap(r)[:20]


CASE_STATUSES = ("PASS", "FAIL", "BLOCKED", "NOT_RUN", "QUESTION")


def case_status(v):
    """'pass', 'Not run', 'not-run' -> PASS / NOT_RUN; None when it isn't one of CASE_STATUSES."""
    s = re.sub(r"[\s-]+", "_", str(v or "").strip().upper())
    return s if s in CASE_STATUSES else None
COMMIT_RE = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")


GIT_SLOW = []


def _git(repo, *args, timeout=30):
    """git output (text) or None. Paths stay unquoted (core.quotePath=false) so non-ASCII names round-trip."""
    try:
        r = subprocess.run(["git", "-c", "core.quotePath=false", "-C", str(repo), *args], capture_output=True,
                           encoding="utf-8", errors="surrogateescape", timeout=timeout)  # a file name may not be UTF-8
    except subprocess.TimeoutExpired:
        GIT_SLOW.append(repo)  # the card stops asking (one hung git is usually all of them: a network share)
        return None
    except OSError:
        return None
    return r.stdout if r.returncode == 0 else None


def short(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[:n - 1] + "…"


class BadInput(ValueError):
    """Bad command-line input: printed to stderr, exit 2."""


class BadOption(BadInput):
    """An unknown, repeated or empty --option: printed with the command's usage."""


def _strs(obj, key):
    v = obj.get(key) if isinstance(obj, dict) else None
    return [str(x) for x in v if isinstance(x, (str, int, float))] if isinstance(v, list) else []


def _write(path, text):
    """Atomic write via a fresh temp file (O_EXCL): a planted `.<name>.tmp` link can't redirect it outside qa-out."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as f:  # a lone surrogate in Jira or page text
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
