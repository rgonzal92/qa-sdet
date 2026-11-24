"""Redaction of secrets from text, JSON and files: tokens with a recognisable format, credentials in URLs, auth
headers, and the values of secret-named JSON keys. Nothing here guesses from prose."""
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path


# ---------- redaction: secrets must not reach drafts the user files, or the DB ----------
SECRET_HEADERS = ("authorization|proxy-authorization|cookie|set-cookie|x-api-key|x-auth-token|x-csrf-token|"
                  "x-xsrf-token|x-amz-security-token")
# Any name ending in one of these (PRIVATE_TOKEN, secret_key, bearerToken); not 'sid': site/segment IDs.
SECRET_KEYS = ("[A-Za-z0-9_-]{0,40}?(?:password|passwd|pwd|secret|token|api[ _-]?key|private[ _-]?key|secret[ _-]?key|"
               "access[ _-]?key|account[ _-]?key|subscription[ _-]?key)|session[ _-]?id")  # also 'API key', 'Session ID'
SECRET_NAME_RE = re.compile(rf"(?i)^({SECRET_HEADERS}|{SECRET_KEYS}|jsessionid|phpsessid|asp\.net_sessionid|jwt|"
                            rf"bearer)$")  # as a JSON key or header name only; in prose these are ordinary words
# Literal tokens: safe to replace anywhere, including inside code and JSON strings.
LITERAL_REDACTIONS = [
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), "[REDACTED-JWT]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED-AWS-KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "[REDACTED-GITHUB-TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"), "[REDACTED-GITHUB-TOKEN]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[REDACTED-SLACK-TOKEN]"),
    (re.compile(r"\bATATT[A-Za-z0-9_=-]{20,}"), "[REDACTED-ATLASSIAN-TOKEN]"),
    # sk-ant-…, sk-proj-…: a digit somewhere, so CSS classes like sk-folding-cube-container stay
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), lambda m: "[REDACTED-API-KEY]" if re.search(r"\d", m.group(0)) else m.group(0)),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), "[REDACTED-GITLAB-TOKEN]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:(?:(?!-----BEGIN )[\s\S]){0,20000}?-----END [A-Z ]*PRIVATE KEY-----|"
                r"(?:\s*[A-Za-z0-9+/=]{16,})*)"), "[REDACTED-PRIVATE-KEY]"),
    # a token, not a word: "Bearer acknowledgements" in prose stays
    (re.compile(r"(?i)\b(bearer)(\s+)(?!\[REDACTED)(?=[A-Za-z0-9\-._~+/]*[0-9._~+/-])[A-Za-z0-9\-._~+/]{16,}=*"),
     r"\1\2[REDACTED]"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,30}://)[^/\s:@\"'`]*:[^/\s@\"'`]+@"), r"\1[REDACTED]@"),  # user:pass@host
    # not ?code=: usually an ordinary value (road/segment codes); an OAuth code is single-use and short-lived
    (re.compile(r"(?i)([?&](?:access_token|id_token|token|sig|signature|api_key|apikey|"
                r"x-amz-signature|x-amz-credential|x-amz-security-token|x-goog-signature|x-goog-credential)=|"
                r"\b(?:access_token|id_token|api_key|apikey)=)"  # also outside a URL: `GET /x api_key=…`, `export API_KEY=…`
                r"(?!\[REDACTED|<|\$\{|\{|:\w)[^&\s\"'`#]+"), r"\1[REDACTED]"),
]
# `Authorization: <anything>`: up to the end of the line or the next quote, the value is the credential
HEADER_RE = re.compile(rf"(?im)\b((?:{SECRET_HEADERS}))\b([\"']?[ \t]*:[ \t]*[\"']?)(?!\[REDACTED)([^\s\"'][^\n\"']*)")


def redact_literals(text):
    for pattern, repl in LITERAL_REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def redact(text):
    """Prose and log text: literal tokens, then the value of an Authorization / Cookie / X-Api-Key style header."""
    return HEADER_RE.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", redact_literals(text))


def redact_json(obj):
    """Structured: values of secret-named keys (and HAR-style {name, value} headers) become "[REDACTED]"."""
    if isinstance(obj, dict):
        label = obj.get("name") if isinstance(obj.get("name"), str) else obj.get("key")  # {name|key: …, value: …}
        named = isinstance(label, str) and "value" in obj and SECRET_NAME_RE.match(label)
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() == "cookies" and isinstance(v, (list, dict)):  # a cookie jar: every value
                hide = lambda x: "[REDACTED]" if x not in (None, True, False, "") else x
                out[k] = {n: hide(x) for n, x in v.items()} if isinstance(v, dict) else [
                    {**redact_json(c), "value": hide(c["value"])} if isinstance(c, dict) and "value" in c else redact_json(c) for c in v]
                continue
            secret = (isinstance(k, str) and SECRET_NAME_RE.match(k)) or (named and k == "value")
            out[k] = "[REDACTED]" if secret and v not in (None, True, False, "") else redact_json(v)
        return out
    if isinstance(obj, list):
        if len(obj) == 2 and isinstance(obj[0], str) and isinstance(obj[1], str) and obj[1] and SECRET_NAME_RE.match(obj[0]):
            return [obj[0], "[REDACTED]"]  # a header as a [name, value] pair
        return [redact_json(v) for v in obj]
    return redact(obj) if isinstance(obj, str) else obj


PROSE_SUFFIXES = {".md", ".txt", ".log", ".html", ".htm", ".yml", ".yaml", ".csv"}
JSON_SUFFIXES = {".json", ".har"}
CODE_SUFFIXES = {".ts", ".js", ".mjs", ".cjs", ".py"}


def scrub_file(f):
    try:
        before = f.stat()
        text = f.read_text()
    except (OSError, UnicodeDecodeError):
        return
    same = lambda a: (a.st_ino, a.st_size, a.st_mtime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns)
    if f.suffix in JSON_SUFFIXES:
        try:
            obj = json.loads(text)
        except (ValueError, RecursionError):
            return  # leave unparseable JSON as is rather than corrupt it (ingest parks it with a message)
        clean_obj = redact_json(obj)
        clean = text if clean_obj == obj else json.dumps(clean_obj, indent=2, ensure_ascii=False) + "\n"
    elif f.suffix in CODE_SUFFIXES:
        clean = redact_literals(text)  # repro specs must stay runnable
    else:
        clean = redact(text)
    if clean != text:
        st = f.stat()
        # write + rename: a hook killed mid-write can't leave a truncated draft; a fresh temp file (O_EXCL), so a
        # planted .<name>.scrub link can't redirect the write outside qa-out
        fd, tmp = tempfile.mkstemp(dir=f.parent, prefix=f".{f.name}.", suffix=".scrub")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(clean)
            os.chmod(tmp, st.st_mode & 0o7777)
            os.utime(tmp, (st.st_atime, st.st_mtime))  # keep the run's finish time for late-ingest ordering
            if not same(f.stat()):  # an explorer rewrote it meanwhile: keep its write, the next hook scrubs that
                Path(tmp).unlink()
                return
            os.replace(tmp, f)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def scrub_tree(root, budget=4.0):
    """Scrub text files under qa-out changed since the last scrub (also folders without a run file), oldest first,
    for at most `budget` seconds per call: a large backlog (an upgrade, weeks of runs) must not push the Stop hook
    past its timeout, which would leave the stamp unwritten and stall every turn. The stamp is the start time of the
    last complete pass, or "<mtime> <path>" of the last file done when a pass stopped early (an exact cursor: a copied
    qa-out has thousands of files with one mtime)."""
    stamp = root / ".scrubbed"
    try:
        since, _, cursor = stamp.read_text().partition(" ")
        since = float(since)
    except (OSError, ValueError):
        since, cursor = 0.0, ""
    started = time.time()
    todo, suffixes = [], PROSE_SUFFIXES | JSON_SUFFIXES | CODE_SUFFIXES
    for folder, _, names in os.walk(root):  # no stat for folders, images or other files this never scrubs (sorted below)
        for name in names:
            # Path.suffix is '' or the text from the last dot: most names (images) fail the cheap test first
            if name[name.rfind("."):] not in suffixes or Path(name).suffix not in suffixes:
                continue
            f = os.path.join(folder, name)
            try:
                st = os.lstat(f)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):  # not a symlink, fifo or socket
                # ctime too: a draft copied in with its old mtime (cp -a, a restored backup) is new here
                key = (max(st.st_mtime, st.st_ctime), f)
                if (key > (since, cursor) if cursor else key[0] >= since - 2) and st.st_size < 20_000_000:
                    todo.append(key)
    todo.sort()
    done = None
    for mtime, name in todo:
        if done is not None and time.time() - started > budget:
            break
        try:
            scrub_file(Path(name))
        except (OSError, RecursionError):  # one odd file (nested past the stack) must not stop every ingest
            pass
        done = (mtime, name)
    else:
        done = None  # the whole backlog: the next pass starts from this one's start time
    try:
        stamp.write_text(f"{done[0]} {done[1]}" if done else str(started))
    except OSError:
        pass
