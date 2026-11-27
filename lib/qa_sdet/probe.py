"""`api-probe`: one request sent from inside the signed-in `qa` browser, with its write, header and body rules."""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import core, ops, scrub, session

API_READS, API_WRITES = ("GET", "HEAD", "OPTIONS"), ("POST", "PUT", "PATCH", "DELETE")
API_FORBIDDEN_HEADERS = (
    "authorization",
    "cookie",
    "proxy-authorization",
)  # the browser holds the session: never sent by hand
API_SAFE_HEADERS = (
    "content-type",
    "etag",
    "location",
    "cache-control",
    "allow",
    "content-length",
    "last-modified",
    "content-disposition",
    "retry-after",
    "vary",
    "x-request-id",
)  # plus x-ratelimit-*; never set-cookie
API_AUTH_SEGMENTS = {
    "login",
    "logout",
    "signin",
    "signout",
    "oauth",
    "oauth2",
    "token",
    "tokens",
    "session",
    "sessions",
    "auth",
    "sso",
    "saml",
    "password",
    "passwords",
}  # a path segment (case, - and _ ignored): credentials
API_BODY_CAP = 4000  # characters of the scrubbed body printed
API_PAGE_CAP = 200_000  # characters the page hands back (the record-scope check reads them)
API_SCOPE = "qa-sdet-"  # the prefix of every record a run creates (agents/qa-explorer.md)
_HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def _api_writes_on():
    """Whether the live run marker (qa-out/.active under the working folder or CLAUDE_PROJECT_DIR) has api_writes: true.
    An expired, malformed or missing marker is no run: writes stay off."""
    for base in dict.fromkeys(b for b in (os.getcwd(), os.environ.get("CLAUDE_PROJECT_DIR")) if b):
        try:
            m = json.loads((Path(base) / "qa-out" / ".active").read_text())
            if isinstance(m, dict) and float(m.get("expires") or 0) > time.time():
                return m.get("api_writes") is True
        except (OSError, ValueError, TypeError):
            continue
    return False


def _auth_segment(url):
    """The login/session/token word a path segment of the URL is, or None (`/api/Sign-In.json` -> signin)."""
    parts = [unquote(x).lower() for x in urlsplit(url).path.split("/") if x]
    if parts:
        parts[-1] = parts[-1].rsplit(".", 1)[0]  # an extension
    return next((w for w in (re.sub(r"[-_]", "", x) for x in parts) if w in API_AUTH_SEGMENTS), None)


def _safe_headers(headers):
    return {
        k.lower(): scrub.redact(str(v))
        for k, v in (headers if isinstance(headers, dict) else {}).items()
        if isinstance(k, str) and (k.lower() in API_SAFE_HEADERS or k.lower().startswith("x-ratelimit-"))
    }


def _scrub_body(text):
    """The response body as printed: values of secret-named keys hidden, tokens redacted, at most API_BODY_CAP characters."""
    try:
        v = json.loads(text)
    except (ValueError, RecursionError):
        v = None
    out = (
        json.dumps(scrub.redact_json(v), ensure_ascii=False, separators=(",", ":"))
        if isinstance(v, (dict, list))
        else scrub.redact_json_text(scrub.redact(text))
    )
    return out[:API_BODY_CAP], len(out) > API_BODY_CAP


def _note_probe(method, url, status):
    """One line in <run>/.data/probes-<slug>.jsonl for a request api-probe sent in an explorer's run: the browser logs
    it as it logs the page's own, and the report's signals leave it out (found live: a probe that failed on CORS was
    listed as a new severe app signal). Best effort: no run, or a file that can't be written, changes nothing."""
    for base in dict.fromkeys(b for b in (os.getcwd(), os.environ.get("CLAUDE_PROJECT_DIR")) if b):
        run = ops._run_of(Path(base) / "qa-out")
        if run:
            try:
                with open(run[1] / ".data" / f"probes-{run[2]}.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps({"method": method, "url": scrub.redact(url), "status": status}) + "\n")
            except OSError:
                pass
            return


def _probe_page(method, url, headers, body, needle=None):
    """One fetch inside the signed-in `qa` browser: (result, None), or (None, 'api-probe: …' problem). Every value
    is a JSON string in the script, so page or caller text can't break out of it."""
    init = {
        "method": method,
        "credentials": "include",
        "headers": headers,
        **({"body": body} if body is not None else {}),
    }
    js = (
        f"(async () => {{ try {{ const r = await fetch({json.dumps(url)}, {json.dumps(init)}), h = {{}}, "
        f"ok = {json.dumps(list(API_SAFE_HEADERS))}, needle = {json.dumps(needle)}; "
        "r.headers.forEach((v, k) => { if (ok.includes(k) || k.startsWith('x-ratelimit-')) h[k] = v; }); "
        "const t = await r.text(), out = {status: r.status, ok: r.ok, url: r.url, headers: h, "
        f"body: t.slice(0, {API_PAGE_CAP})}}; if (needle) out.contains = t.includes(needle); "
        "return JSON.stringify(out); } catch (e) { return JSON.stringify({error: String(e)}); } })()"
    )
    hint = "is the browser open? (qa-mem browser-open or page-start)"
    try:
        r = subprocess.run(
            ["playwright-cli", "-s=qa", "--raw", "eval", js],
            env=session._pw_env(),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"api-probe: browser tool failed ({e}); {hint}"
    if r.returncode != 0:
        detail = core.short((r.stderr or r.stdout or f"exited {r.returncode}").strip(), 200)
        return None, f"api-probe: browser tool failed ({detail}); {hint}"
    res = session._unwrap_raw(r.stdout)
    if isinstance(res, str):
        res = session._unwrap_raw(res)
    if isinstance(res, dict) and res.get("error"):
        _note_probe(method, url, "FAILED")
        return None, f"api-probe: the request failed in the browser: {core.short(str(res['error']), 200)}"
    if not isinstance(res, dict) or not isinstance(res.get("status"), int):
        return None, f"api-probe: unexpected browser output ({core.short(r.stdout.strip(), 120)})"
    _note_probe(method, url, res["status"])
    return res, None


def _probe_headers(headers, content_type, body, method):
    """The request headers from --header 'Name: value' words and --content-type, or BadInput."""
    out = {}
    for h in headers:
        name, colon, value = str(h).partition(":")
        name = name.strip()
        if not colon or not _HEADER_NAME_RE.fullmatch(name) or re.search(r"[\r\n]", value):
            raise core.BadInput(f"api-probe: --header takes 'Name: value' on one line (got {core.short(str(h), 60)!r})")
        if name.lower() in API_FORBIDDEN_HEADERS:
            raise core.BadInput(
                f"api-probe: the {name} header is refused: the browser's own session authenticates the request"
            )
        if name.lower() in (k.lower() for k in out):
            raise core.BadInput(f"api-probe: header {name} given twice")
        out[name] = value.strip()
    has_type = lambda: "content-type" in (k.lower() for k in out)
    if content_type is not None:
        if has_type():
            raise core.BadInput("api-probe: give the content type once (--content-type or --header 'Content-Type: …')")
        out["Content-Type"] = content_type
    if body is not None:
        if method in API_READS:
            raise core.BadInput(f"api-probe: a {method} takes no body")
        if not has_type():
            out["Content-Type"] = "application/json"
    return out


def _own_record(text):
    """Is this GET body one record of the run: a JSON object (or the object in its `data`, or that object's JSON:API
    `attributes`) with a text field of its own named qa-sdet-…? An enveloped list or a real record holding a qa-sdet-
    child is not."""
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):
        return False
    doc = doc["data"] if isinstance(doc, dict) and isinstance(doc.get("data"), dict) else doc
    if not isinstance(doc, dict):
        return False
    fields = list(doc.values()) + (list(doc["attributes"].values()) if isinstance(doc.get("attributes"), dict) else [])
    return any(isinstance(v, str) and v.startswith(API_SCOPE) for v in fields)


def api_probe(method, url, body=None, content_type=None, headers=()):
    """One authenticated request from inside the signed-in `qa` browser (the session stays in the browser: no cookie or
    token is read or printed). Prints {status, ok, headers, body}: only contract headers, the body scrubbed and cut at
    API_BODY_CAP characters. GET/HEAD/OPTIONS always; POST/PUT/PATCH/DELETE only under a run marker with api_writes, on
    qa-sdet- records: a POST body must hold one; for PUT/PATCH/DELETE a GET of the same URL must show one (and not be a
    list). Exit 0 = the request ran (any HTTP status), 1 = browser trouble, 2 = refused."""
    method = str(method).upper()
    if method not in API_READS + API_WRITES:
        raise core.BadInput(f"api-probe: method must be one of {', '.join(API_READS + API_WRITES)} (got {method!r})")
    _, hosts = session._qa_config("api-probe")
    if not core.url_allowed(url, hosts):
        raise core.BadInput(f"api-probe: {url!r} must be a full http(s) URL in allowedHosts ({sorted(hosts)})")
    if word := _auth_segment(url):
        raise core.BadInput(
            f"api-probe: {urlsplit(url).path!r} looks like a login, session or token endpoint ({word}); "
            "credentials stay out of agent output, so it is not probed"
        )
    hdrs = _probe_headers(headers, content_type, body, method)
    if method in API_WRITES:
        if not _api_writes_on():
            raise core.BadInput(
                f"api-probe: {method} writes data: only a run started with --api-writes may send it "
                "(rerun /qa-sdet:qa-explore or /qa-sdet:qa-ticket with --api-writes)"
            )
        if method == "POST" and API_SCOPE not in (body or ""):
            raise core.BadInput(
                f"api-probe: POST refused: the body must hold a {API_SCOPE}… value (only records this run creates)"
            )
        if method != "POST":  # the record it names must be one of the run's, and one record, not a list
            before, problem = _probe_page(
                "GET", url, {k: v for k, v in hdrs.items() if k.lower() != "content-type"}, None, API_SCOPE
            )
            if problem:
                print(problem, file=sys.stderr)
                return 1
            try:
                is_list = isinstance(json.loads(before.get("body") or ""), list)
            except (ValueError, RecursionError):
                is_list = False
            own = _own_record(str(before.get("body") or ""))
            if not (before.get("ok") and before.get("contains") is True and own) or is_list:
                raise core.BadInput(
                    f"api-probe: {method} refused: a GET of {url} (HTTP {before['status']}) doesn't show one "
                    f"{API_SCOPE}… record"
                    + (" (it is a list: name one record)" if is_list else "")
                    + "; only records this run created are changed"
                )
    res, problem = _probe_page(method, url, hdrs, body)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    out = {"status": res["status"], "ok": bool(res.get("ok"))}
    final = str(res.get("url") or url)
    if core.url_allowed(final, hosts):
        text, cut = _scrub_body(str(res.get("body") or ""))
        out |= {"headers": _safe_headers(res.get("headers")), "body": text}
        if cut:
            out["body_truncated"] = True
    else:  # redirected off the QA hosts (an SSO page): its content is not the app's
        out |= {"headers": {}, "redirected_to": urlsplit(final).hostname or "?"}
    print(json.dumps(out, ensure_ascii=False))
    return 0
