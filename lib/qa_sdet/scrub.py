"""Redaction of secrets from text, JSON and files."""
import json
import os
import re
import stat
import time
from pathlib import Path
from . import core


# ---------- redaction: secrets must not reach drafts the user files, or the DB ----------
# (?=[...]): the first letters of the names, so (?i) passes skip most positions without trying every name (~25% faster)
SECRET_HEADERS = ("(?=[acpsx])(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|x-auth-token|x-csrf-token|"
                  "x-xsrf-token|x-amz-security-token)")
# Any name ending in one of these (PRIVATE_TOKEN, secret_key, bearerToken); not 'sid': site/segment IDs.
SECRET_KEYS = ("[A-Za-z0-9_-]{0,40}?(?=[apst])(?:password|passwd|pwd|secret|token|api[ _-]?key|private[ _-]?key|secret[ _-]?key|"
               "access[ _-]?key|account[ _-]?key|subscription[ _-]?key)|session[ _-]?id")  # also 'API key', 'Session ID'
SECRET_NAME_RE = core._LazyRe(rf"(?i)^({SECRET_HEADERS}|{SECRET_KEYS}|jsessionid|phpsessid|asp\.net_sessionid|jwt|"
                            rf"bearer)$")  # as a JSON key or header name only; in prose these are ordinary words
# Literal tokens: safe to replace anywhere, including inside code and JSON strings. A new pattern here, in
# CLI_SECRET_RES or in the secret names needs a word of its match in REDACT_NEEDLES.
LITERAL_REDACTIONS = [
    (core._LazyRe(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), "[REDACTED-JWT]"),
    (core._LazyRe(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED-AWS-KEY]"),
    (core._LazyRe(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "[REDACTED-GITHUB-TOKEN]"),
    (core._LazyRe(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[REDACTED-SLACK-TOKEN]"),
    (core._LazyRe(r"\bgithub_pat_[A-Za-z0-9_]{30,}"), "[REDACTED-GITHUB-TOKEN]"),
    (core._LazyRe(r"\bATATT[A-Za-z0-9_=-]{20,}"), "[REDACTED-ATLASSIAN-TOKEN]"),
    (core._LazyRe(r"\bAIza[0-9A-Za-z_-]{30,}"), "[REDACTED-GOOGLE-KEY]"),
    (core._LazyRe(r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{16,}"), "[REDACTED-STRIPE-KEY]"),
    # sk-ant-…, sk-proj-…: a digit somewhere, so CSS classes like sk-folding-cube-container stay
    (core._LazyRe(r"\bsk-[A-Za-z0-9_-]{20,}"), lambda m: "[REDACTED-API-KEY]" if re.search(r"\d", m.group(0)) else m.group(0)),
    (core._LazyRe(r"\bglpat-[A-Za-z0-9_-]{20,}"), "[REDACTED-GITLAB-TOKEN]"),
    (core._LazyRe(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:(?:(?!-----BEGIN )[\s\S]){0,20000}?-----END [A-Z ]*PRIVATE KEY-----|(?:\s*[A-Za-z0-9+/=]{16,})*)"),
     "[REDACTED-PRIVATE-KEY]"),
    # a token, not a word: "Bearer acknowledgements" in prose stays
    (core._LazyRe(r"(?i)\b(bearer)(\s+)(?!\[REDACTED)(?=[A-Za-z0-9\-._~+/]*[0-9._~+/-])[A-Za-z0-9\-._~+/]{16,}=*"),
     r"\1\2[REDACTED]"),
    (core._LazyRe(r"(?i)\b(bearer\s+\$\{\s*[\"'])(?!\[REDACTED)[^\"'\n]{8,}(?=[\"'])"), r"\1[REDACTED]"),  # `Bearer ${'tok'}`
    (core._LazyRe(r"(?i)\b([a-z][a-z0-9+.-]{0,30}://)[^/\s:@\"'`]*:[^/\s@\"'`]+@"), r"\1[REDACTED]@"),
    (core._LazyRe(r"(?i)\b(jsessionid|phpsessid|asp\.net_sessionid|connect\.sid)=(?!\[REDACTED)[^;\s\"'`&]+"),
     r"\1=[REDACTED]"),
    # ?key=: only a key-looking value (a digit, 12+ chars); `?key=password` sorts a table
    (core._LazyRe(r"(?i)([?&]key=)(?!\[REDACTED)(?=[^&\s\"'`#]*\d)[^&\s\"'`#]{12,}"), r"\1[REDACTED]"),
    (core._LazyRe(r"(?i)(\b(?:sid|session)[\"']?\s*[:=]\s*[\"']?)(?!\[REDACTED|/)[A-Za-z0-9%._~+/-]{16,}"), r"\1[REDACTED]"),  # session-length only
    # not ?code=: usually an ordinary value (road/segment codes); an OAuth code is single-use and short-lived
    (core._LazyRe(r"(?i)([?&](?:access_token|id_token|token|sig|signature|api_key|apikey|"
                r"x-amz-signature|x-amz-credential|x-amz-security-token|x-goog-signature|x-goog-credential)=)(?!\[REDACTED|<|\$\{|\{|:\w)"
                r"[^&\s\"'`#]+"), r"\1[REDACTED]"),
]
# one whitespace run unless a quote or backslash splits it: two adjacent runs made a long run of spaces quadratic
# (`**Password:** x`, `password: \`x\``: Markdown's bold marks and backticks sit around the separator)
_SEP = r"([ \t]*[*_]{0,3}(?:(?:\\[\"']?|[\"'])[ \t]*)?[:=][*_]{0,3}[ \t]*\\?[\"'`]?)"
HEADER_VALUE_RE = core._LazyRe(rf"(?im)\b({SECRET_HEADERS})\b{_SEP}(?!\[REDACTED)"
                             rf"((?:(?!\b(?:{SECRET_HEADERS})\b[ \t]*[:=])[^\n\"'`\\}}])+)")
KEY_VALUE_RE = core._LazyRe(rf"(?i)\b({SECRET_KEYS})\b{_SEP}(?!\[REDACTED|[=<>!~])([^\s\"'\\`]+)")  # not `token == null`
VALUE_END_RE = core._LazyRe(r"[&;,](?=[\w.-]+=)|\}")  # where the value stops: `&next=`, `; Path=`, a closing `,` or `}`
# credentials on a command line or stated in a sentence ("the password is …"): redacted when the value looks like one
CLI_SECRET_RES = [
    core._LazyRe(r"(?i)(\bcurl\b[^\n|;&]{0,300}?\s(?:-u|--user)[ =]*[\"']?[^\s:\"'/]+[\"']?:[\"']?)()(?!\[REDACTED|//)([^\s\"']+)"),
    core._LazyRe(r"(?i)(\b(?:mysql|mysqldump|mariadb)\b[^\n|;&]{0,300}?\s-p)()(?!\[REDACTED)([^\s\"']+)"),
    core._LazyRe(r"(?i)(\b(?:password|passwd|passcode|pwd))(\s+(?:is|was)\s+[\"']?)(?!\[REDACTED)([^\s\"'.,;]+)"),
]
HARMLESS = {"null", "none", "true", "false", "undefined", "required", "optional", "n/a", "na", "empty", "hidden"}


TYPE_WORD_RE = core._LazyRe(r"(?i)(u?int|float|sha|hs|rs|es|uuidv?|utf-?|md|oauth|ipv|http|base|h)\d+")  # uuid4, HS256, int64
COOKIE_ATTRS = {"samesite", "httponly", "secure", "path", "domain", "maxage", "expires", "priority", "partitioned"}


def _tokenish(x):
    """A value that reads as a token: a long run of token characters, or letters and digits mixed (6+ chars)."""
    return bool(re.search(r"[A-Za-z0-9._~+/-]{16,}", x)) or (
        len(x) >= 6 and bool(re.search(r"\d", x)) and bool(re.search(r"[A-Za-z]", x)) and not TYPE_WORD_RE.fullmatch(x))


def _secret_looking(v):
    """Prose like 'consent banner covers Save' or 'min' stays; tokens, k=v cookies and digits-in-a-word go. Limits,
    comparisons, calls and type names are rules about the field, not its value: 'maxlength=64', '>=8', 'min(8)', 'uuid4'."""
    v = v.strip()
    if not v or v.lower().strip(".,:;") in HARMLESS or v.startswith(("process.", "${", "env.", "<", "[REDACTED", "=", ">", "!", "~")):
        return False
    if re.fullmatch(r"v?\d+(\.\d+){1,3}[.,;]?", v):  # a version (token=1.42.0 in a changelog), not a secret
        return False
    # a limit or a CSS pseudo-class, not a value: 'Password: 8-64 characters', 'token: 30min', 'input#password:nth-child(2)'
    if re.fullmatch(r"\d+([-–.,:/]\d+)*[A-Za-z%]{0,5}[.,;]?|[@\w.-]+\(.*", v) or TYPE_WORD_RE.fullmatch(v.strip(".,;")):
        return False
    if re.fullmatch(r"[A-Za-z_]+(\.[A-Za-z_]+)+[,;]?", v):  # code: this.form.value.password, Validators.required (no digits)
        return False
    if "=" in v:  # k=v pairs: a secret when a value in them reads as a token ('sid=abc123def'), not 'max=64'
        return any(_tokenish(x.partition("=")[2] or x) for x in re.split(r"[;&]", v) if x.partition("=")[0].lower().replace("-", "") not in COOKIE_ATTRS)
    if re.search(r"[A-Za-z0-9._~+/-]{16,}", v):
        return True
    return " " not in v and bool(re.search(r"\d", v)) and len(v) >= 4 and (_tokenish(v) or not v.isalnum())


BASIC_RE = core._LazyRe(r"(?i)\b(basic)(\s+)(?!\[REDACTED)([A-Za-z0-9+/]{6,}={0,2})(?![A-Za-z0-9+/=])")


def redact_literals(text):
    for pattern, repl in LITERAL_REDACTIONS:
        text = pattern.sub(repl, text)
    # 'Basic <base64 of user:password>' outside a header line ("auth": "Basic dXNlcjpwYXNz", prose, a spec)
    return BASIC_RE.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]" if _basic_credentials(m.group(3))
                        else m.group(0), text)


AUTH_SCHEME_RE = core._LazyRe(r"(\s*(?:bearer|basic|token|digest|negotiate|ntlm|aws4-hmac-sha256)\s+)(\S+)", re.I)
COOKIE_PAIRS_RE = core._LazyRe(r"\s*(?:[^\s;=]+=[^\s;]*;?\s*)+")


def _basic_credentials(tok):
    """Base64 of 'user:password' (it can be all letters, like dXNlcjpwYXNz)."""
    import base64
    import binascii
    try:
        return ":" in base64.b64decode(tok + "=" * (-len(tok) % 4), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False


def _header_value(val, name=""):
    """Redact only the credential in a header line's value, so prose after it survives:
    'Bearer <tok>' -> the token; 'a=1; sid=x' -> the pairs; otherwise a secret-looking first word."""
    m = AUTH_SCHEME_RE.match(val)
    other = None if m or "authorization" not in name.lower() else re.match(r"(\s*[A-Za-z][\w-]{1,20}\s+)(\S+)", val)
    if m or other:  # `Authorization: ApiKey <tok>`, `SSWS <tok>`: any scheme, when the word after it reads as a token
        m = m or other
        tok = m.group(2).rstrip("),.;")
        trail = m.group(2)[len(tok):]
        prose = tok.isalpha() and tok.islower() and re.match(r"\s+[A-Za-z]", val[m.end():])  # 'Bearer expected but missing'
        if tok.startswith(("process.", "${", "$", "env.", "<", "{{", "{")):  # `Bearer ${accessToken}`, `Bearer {{authToken}}`, `Bearer <access-token>`, `Bearer $TOKEN`
            return val
        secret = _secret_looking(tok) or _basic_credentials(tok) or (
            not other and not prose and len(tok) >= 8 and tok.lower().strip(".,:;") not in HARMLESS)
        return m.group(1) + ("[REDACTED]" if secret else tok) + trail + val[m.end():]
    m = COOKIE_PAIRS_RE.match(val)
    if m and "=" in m.group(0) and not all(x.split("=")[0].strip(" ,").lower().replace("-", "") in COOKIE_ATTRS
                                           for x in re.split(r"[;\s]+", m.group(0).strip()) if x):  # not 'SameSite=Lax missing'
        tail = m.group(0)[len(m.group(0).rstrip(" ,")):]  # the ", " or space before whatever follows (a next header)
        return " " * (m.group(0)[:1] == " ") + "[REDACTED]" + (tail or (" " if val[m.end():] else "")) + val[m.end():]
    first = (val.split() or [""])[0]
    # an x-api-key / x-auth-token value is the key itself, letters only or not
    prose = (first.isalpha() and len(val.split()) > 1) or bool(  # 'X-Api-Key: mandatory for /api/zones', 'Mismatch error shown'
        re.match(r"[A-Za-z_]\w*([.\[(]|$)", first) and re.search(r"[.\[(]", first))  # code: environment.apiKey, req.headers['x']
    if first and (_secret_looking(first) or (name.lower().startswith("x-") and len(first) >= 8 and not prose
                                             and first.lower().strip(".,:;") not in HARMLESS
                                             and not first.startswith(("process.", "${", "$", "env.", "<", "{{", "{")))):
        return val.replace(first, "[REDACTED]", 1)
    return val


QUOTED_SECRET_RE = core._LazyRe(rf"(?i)\b({SECRET_KEYS})\b([ \t]*(?:[\"'][ \t]*)?[:=][ \t]*)([\"'])(?!\[REDACTED)([^\"'\n]{{4,}})\3")


# a quoted sentence after a secret name is the app's message ("Password is too short"), not a passphrase ("correct horse battery")
MESSAGE_WORD_RE = core._LazyRe(r"(?i)\b(is|are|was|has|have|must|be|or|not|the|too|at|to|required|invalid|expired|missing|cannot|"
                             r"can't|should|does|did|field|wrong|incorrect|failed|match|least|most|your|enter|please|short|long|weak)\b")


def _quoted_secret(m):
    """`password: 'letmein'`, `"apiKey": "correct horse"`: a quoted literal after a secret name is the secret itself."""
    v = m.group(4)
    keep = v.strip().lower() in HARMLESS or v.startswith(("process.", "${", "env.", "<", "{{", "%")) or bool(
        re.search(r"\||^(required|nullable|string|min|max|pattern)\b", v)) or (  # a validation rule: 'required|min:8|max:64'
        len(v.split()) >= 2 and not re.search(r"[A-Za-z0-9._~+/-]{16,}", v) and bool(
            MESSAGE_WORD_RE.search(v) or v[:1].isupper())) or bool(  # a sentence: "Minimum 8 characters", "Bad credentials"
        re.match(r"\s*[).,\]]", v))  # getByLabel('Password:').fill('x'): the text between two other strings
    return m.group(0) if keep else f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED]{m.group(3)}"


def _assigned_secret(v):
    """The value in `password=<v>`: an assignment's value is the secret whatever its characters (Sommerregen), unless
    it is a word like `required`, a limit, a placeholder or code (`getPassword()`, `await`, `this.x`)."""
    v = v.strip()
    return _secret_looking(v) or (
        len(v) >= 3 and v.lower().strip(".,:;") not in HARMLESS | {"await", "new", "true", "false", "nil", "this", "self"}
        and not v.startswith(("process.", "${", "$", "env.", "<", "[", "{", "(", "this.", "self.", "/", ":"))
        and not re.fullmatch(r"v?\d+([-–.,:/]\d+)*[A-Za-z%]{0,5}[.,;]?|[@\w.]+\(.*|[a-z_][A-Za-z_]*(\.[A-Za-z_]+)+[,;]?", v))  # user.password


# `user / password: qa / Passw0rd!`: up to six labels and as many values, each joined by `/`; a value goes with its
# label. A label starts a word (not after `-`), so a long hyphenated or base64url run is scanned once, not from every `-`.
PAIRED_RE = core._LazyRe(r"(?i)(?<![\w-])((?:[\w-]+(?:[ \t][\w-]+)?[ \t]*/[ \t]*){1,5}[\w-]+(?:[ \t][\w-]+)?)"
                       r"([ \t]*[*_]{0,3}[ \t]*[:=][*_]{0,3}[ \t]*)((?:[^\s/|]+[ \t]*/[ \t]*){1,5}[^\s/|]+)")
SECRET_LABEL_RE = core._LazyRe(rf"(?i)(?:{SECRET_KEYS})$")  # a label ending in a secret name: `Confirm password`, `API key`


def _paired(m):
    labels, values = m.group(1).split("/"), m.group(3).split("/")
    if len(labels) != len(values):
        return m.group(0)
    judge = _assigned_secret if "=" in m.group(2) else _secret_looking  # as KEY_VALUE_RE: `password: invalid` is a word
    out = []
    for label, v in zip(labels, values):
        core = v.strip().rstrip("&;,.})")  # closing punctuation is not part of the value
        out.append(v.replace(core, "[REDACTED]", 1) if core and SECRET_LABEL_RE.search(label.strip()) and judge(core) else v)
    return m.group(1) + m.group(2) + "/".join(out)


TABLE_ROW_RE = core._LazyRe(rf"(?im)(\|[ \t]*`?(?:{SECRET_HEADERS}|{SECRET_KEYS})`?[ \t]*\|[ \t]*`?)(?!\[REDACTED)([^|\n`]+?)(`?[ \t]*\|)")


# Every pass of redact() needs one of these, in lower case, inside its match (a secret name, a token prefix, '://' of
# URL credentials, 'key=' of a query key). ASCII text with none of them comes back as it is, skipping ~30 regex passes:
# most strings of a run JSON or HAR ('PASS', '/zones', '200'). Non-ASCII text always takes the passes: (?i) matches
# 'ſ' for 's' and the Kelvin sign for 'k'.
KEY_WORDS = ("passw", "pwd", "secret", "token", "api", "private", "access", "account", "subscription", "session")  # SECRET_KEYS
HEADER_WORDS = ("authorization", "cookie", "api", "token")  # SECRET_HEADERS
REDACT_NEEDLES = (*KEY_WORDS, *HEADER_WORDS, "passcode", "sid", "bearer", "basic", "credential", "sig", "key=", "://",
                  "eyj", "akia", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "xox", "github_pat_", "atatt", "aiza",
                  "k_live_", "k_test_", "sk-", "glpat-", "-----begin", "curl", "mysql", "mariadb")


def _sub_lines(pattern, repl, text, words):
    """pattern.sub on only the lines that hold one of `words` (lower case), for the single-line passes of redact():
    test_qa_mem checks those patterns can't match a newline. A big log names a secret on a few lines."""
    if "\n" not in text or not text.isascii():  # (?i) folds 'ſ' and the Kelvin sign: the word test is exact for ASCII
        return pattern.sub(repl, text)
    lines = text.split("\n")
    for k, line in enumerate(lines):
        low = line.lower()
        if any(w in low for w in words):
            lines[k] = pattern.sub(repl, line)
    return "\n".join(lines)


def redact(text):
    """Prose and log text: literal tokens plus secret-looking values of secret-named headers and keys."""
    if text.isascii():
        low = text.lower()
        if not any(n in low for n in REDACT_NEEDLES):
            return text
    def sub(m):  # `password=Tr0ub4dor&3` is one value; `password=x&user=qa` and `password: x,` end at the mark
        if m.group(2)[-1:] in "\"'`" and m.string[m.end():m.end() + 1] == " ":
            return m.group(0)  # the first word of a quoted sentence: QUOTED_SECRET_RE has judged the whole of it
        core = m.group(3).rstrip("&;,.})")  # closing punctuation is not part of the value
        end = VALUE_END_RE.search(core)
        v = core[:end.start()] if end else core
        rest = m.group(3)[len(v):]
        if re.search(r"[\^\[\]|*]", v):  # a pattern or a rule about the field: `pattern=^[A-Z]{8,64}$`, `required|min:8`
            return m.group(0)
        secret = (_assigned_secret if "=" in m.group(2) else _secret_looking)(v)
        return m.group(1) + m.group(2) + ("[REDACTED]" if secret else v) + rest
    for _ in range(5):  # a value ends early now, so a second header on the same line needs another pass
        before = text
        text = redact_literals(text)
        text = _sub_lines(QUOTED_SECRET_RE, _quoted_secret, text, KEY_WORDS)
        text = _sub_lines(HEADER_VALUE_RE, lambda m: m.group(1) + m.group(2) + _header_value(m.group(3), m.group(1)), text,
                          HEADER_WORDS)
        text = _sub_lines(KEY_VALUE_RE, sub, text, KEY_WORDS)
        text = _sub_lines(PAIRED_RE, _paired, text, KEY_WORDS)  # (_paired changes only values under a secret label)
        text = TABLE_ROW_RE.sub(lambda m: m.group(1) + ("[REDACTED]" if (_secret_looking if " " in m.group(2).strip()
                                                                          else _assigned_secret)(m.group(2)) else m.group(2))
                                + m.group(3), text)  # `| api_key | 4f3c… |`
        for pattern in CLI_SECRET_RES:
            text = pattern.sub(lambda m: m.group(1) + m.group(2) + ("[REDACTED]" if pattern is not CLI_SECRET_RES[2]
                                                                    or _secret_looking(m.group(3)) else m.group(3)), text)
        if text == before:
            break
    return text


def redact_json(obj):
    """Structured: values of secret-named keys (and HAR-style {name, value} headers) become "[REDACTED]"."""
    if isinstance(obj, dict):
        label = obj.get("name") if isinstance(obj.get("name"), str) else obj.get("key")  # {name|key: …, value: …}
        named = isinstance(label, str) and "value" in obj and (SECRET_NAME_RE.match(label) or (
            label.lower() == "sid" and len(str(obj["value"])) >= 16))  # sid: session cookie vs site id
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() == "cookies" and isinstance(v, (list, dict)):  # a cookie jar: every value
                hide = lambda x: "[REDACTED]" if x not in (None, True, False, "") else x
                out[k] = {n: hide(x) for n, x in v.items()} if isinstance(v, dict) else [
                    {**c, "value": hide(c["value"])} if isinstance(c, dict) and "value" in c else redact_json(c) for c in v]
                continue
            secret = (isinstance(k, str) and (SECRET_NAME_RE.match(k) or (k.lower() == "sid" and len(str(v)) >= 16))
                      or (named and k == "value"))
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
    import tempfile
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
    """Scrub text files under qa-out changed since the last scrub (also folders without a run.json), oldest first,
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
