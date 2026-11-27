"""The guard's rules for the guarded tools: playwright-cli (R2, R3), acli (R1), qa-mem with its api-probe, and other
network clients."""

import re
from itertools import takewhile

import guard_paths
import guard_shell

PW_VALUE_OPTS = set(
    "-s --session --browser --config --device --profile --idle-timeout --filename --depth --domain --path "
    "--host --port --isolated-context --user-data-dir --type --filter --data --size --fps "
    "--status --body --content-type --header --remove-header --cursor --position --duration".split()
)  # route's and
# video-show-actions': else their values pass as the verb
NAV_VERBS, FAULT_VERBS = {"goto", "open", "tab-new"}, {"route", "network-state-set"}
MANUAL_WRITES = {
    "bug-status",
    "stale",
    "answer",
    "remember",
    "forget",
    "signal-ignore",
    "signal-unignore",
    "codemap-forget",
    "plugin-log",
}
PW_SOURCE_OPTS = ("--config", "--extension", "--cdp", "--endpoint")  # a config file or someone else's browser
_SECRET_HINT = "it prints session secrets; use requests / response-body, or one header: --raw eval \"fetch(url).then(r => r.headers.get('n'))\""  # noqa: S105 - a pattern of secret names, not a secret
PW_DENY = {
    "attach": "attach connects to an unverified browser",
    "run-code": "run-code runs Node with the page object and can reach the host; use eval (page sandbox)",
    "delete-data": "it deletes the browser profile and with it the QA sign-in; to sign out for good see README Privacy",
    "tracing-start": "a trace records every request with its headers and cookies into a file; use requests / response-body",
    "state-save": "it writes the session's cookies to a file; the login lives in the main profile (qa-mem browser-open)",
    "install": "it writes a workspace config that picks the browser (.playwright/cli.config.json) or skills into .claude; "
    "run it yourself (`! playwright-cli install`), or install a browser: playwright-cli install-browser <name>",
    **dict.fromkeys(
        (
            "cookie-list cookie-get localstorage-list localstorage-get sessionstorage-list sessionstorage-get "
            "request request-headers response-headers"
        ).split(),
        _SECRET_HINT,
    ),
}  # response: Set-Cookie
# denied to the explorer only (pages steer it); the user's own session may still run these
PW_DENY_EXPLORER = {
    "webmcp-call": "webmcp-call runs a tool the page registered; untrusted page text must not act in the QA session",
    "cookie-clear": "cookie-clear would sign the QA browser out; the login lives in the main profile (qa-mem browser-open)",
    "cookie-delete": "cookie-delete would sign the QA browser out; the login lives in the main profile (qa-mem browser-open)",
    "state-load": "state-load replaces the QA session's cookies and storage with a file's",
}
API_WRITE_METHODS, API_FORBIDDEN_HEADERS = (
    {"POST", "PUT", "PATCH", "DELETE"},
    {"authorization", "cookie", "proxy-authorization"},
)
EVAL_FETCH_RE = re.compile(r"\bfetch\s*\(")
EVAL_METHOD_RE = re.compile(
    r"(?i)\bmethod\W*[:=]\s*['\"`]\s*(post|put|patch|delete)\b"
)  # {method: 'POST'}, "method" : `put`, o.method = 'Patch'
EVAL_WRITE_RE = re.compile(
    r"\bXMLHttpRequest\b|\bsendBeacon\b"
)  # pattern-based: a method built at run time (['PO','ST'].join('')) is not seen
EVAL_WRITE_REASON = (
    "playwright-cli eval sending a write (fetch with POST/PUT/PATCH/DELETE, XMLHttpRequest, sendBeacon) blocked: "
    "qa-mem api-probe is the way to send one (with --api-writes, on qa-sdet- records); GET fetches and UI form submits are fine"
)
SECRET_JS = re.compile(
    r"[dD]ocument\s*(\.\s*cookie|\[\s*['\"`]cookie)|cookieStore|"  # also el.ownerDocument.cookie
    r"\.\s*cookie\b(?![-\w'\"`])|['\"`]cookie['\"`]|[{,]\s*cookie\s*[,}:]|"  # d=document; d.cookie / Reflect.get(document,'cookie') / ({cookie}) =>
    r"(local|session)Storage\s*(\.\s*getItem\b|\[)|\.\.\.\s*(local|session)Storage|"
    r"\bindexedDB\s*\.\s*open\b|"  # auth libraries (Firebase) keep tokens there; indexedDB.databases() names stay allowed
    r"(JSON\.stringify|Object\.(values|entries|assign|fromEntries))\s*\(\s*(local|session)Storage"
)
ACLI_READ, ACLI_AUTH_OK = {"view", "search", "list", "get", "help", "version", "whoami"}, {"login", "status"}
ACLI_WRITE = {
    "create",
    "edit",
    "delete",
    "transition",
    "assign",
    "update",
    "clone",
    "archive",
    "unarchive",
    "link",
    "unlink",
    "move",
    "add",
    "remove",
    "set",
    "import",
    "upload",
    "logout",
    "bulk",
}
HTTP_CLIENTS = {"curl", "wget", "http", "https", "xh", "httpie", "aria2c"}  # the explorer may fetch from allowedHosts
HTTP_REROUTE_OPTS = set(
    "-x --proxy --proxy1.0 --preproxy --socks4 --socks4a --socks5 --socks5-hostname "
    "--resolve --connect-to --doh-url --alt-svc -K --config --unix-socket "  # elsewhere,
    "--abstract-unix-socket --variable".split()
)  # or URLs / options from a file or variable the guard doesn't see
WGET_FILE_OPTS = {"-i", "--input-file", "-e", "--execute"}  # wget only (curl -i/-e: headers, referer)
HTTP_VALUE_OPTS = set(
    "-o -O -d -H -X -u -A -e -b -c -F -T -w -m -r -C -D -E -P -U -t -a --output --data --data-raw "
    "--data-binary --data-urlencode --header --request --user --user-agent --referer --cookie --cookie-jar "
    "--form --upload-file --write-out --max-time --connect-timeout --range --dump-header --cert --cacert --key "
    "--output-document --directory-prefix --post-data --post-file --password --tries --timeout".split()
)  # not hosts
URL_RE = re.compile(r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,30}://[^\s'\"`)]+", re.I)
SCHEMELESS_URL_RE = re.compile(r"[\"'`(]\s*//([a-z0-9][a-z0-9.-]*)", re.I)  # '//host/path' in JS
PROXY_ENV_RE = re.compile(
    r"(?i)\b((https?|all|ftp)_proxy|CURL_HOME|(SYSTEM_)?WGETRC|XDG_CONFIG_HOME)\s*="
)  # or an rc file
HOSTISH_RE = re.compile(
    r"(?i)(localhost|\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-f:.]+\]|[\w-]+(\.[\w-]+)*\.[a-z]{2,})(:\d+)?(/\S*)?"
)
MARKER_REASON = (
    "the run marker (qa-out/.active) is the coordinator's: it sets fault injection and API writes; "
    "finish with what you have (step 9)"
)
MARKER_WRITES = {"dispatch", "marker"}  # qa-mem commands that (re)write or remove the run marker
CRAWLS = {"crawl", "discover"}  # whole browser passes run from Python, out of the guard's sight
UPLOAD_REASON = (
    "an upload of a secret (key, .env, credentials, qa-sdet data or config) or a glob; upload a test file, by name"
)
OPEN_REASON = (
    "playwright-cli open starts a new browser without the QA sign-in and drops the request and console history "
    "step 9 saves; navigate with goto <full URL> (qa-mem browser-open starts the run's browser)"
)


# ---------- playwright-cli, acli, qa-mem, network ----------
def pw_help(rest):
    """True when playwright-cli would only print help: -h/--help before `--`, not an option's value, not `--help false`."""
    skip = False
    for k, a in enumerate(rest):
        if a == "--":
            return False
        if not skip and a in ("--help", "-h") and rest[k + 1 : k + 2] != ["false"]:
            return True
        skip = not skip and a in PW_VALUE_OPTS
    return False


def pw_args(rest):
    """playwright-cli's positional words (the verb first), as it parses them."""
    out, skip, flag, end = [], False, False, False
    for a in rest:
        if end:
            out.append(a)
        elif skip:
            skip = False
        elif a == "--":
            end = True
        elif a.startswith("-"):
            skip, flag = a in PW_VALUE_OPTS, "=" not in a
        else:
            out += [] if flag and a in ("true", "false") else [a]  # the CLI reads `--raw true` as the option's value
            flag = False
    return (
        out[1:] if out and guard_shell.prog_name(out[0]) == "playwright-cli" else out
    )  # npx --package @playwright/cli playwright-cli


def pw_profile_values(rest):
    return guard_shell.opt_values(rest, "--profile", "--user-data-dir")


def pw_file_args(
    rest, verb, args
):  # paths it writes or reads: --filename/--path values, video-start/upload/drop/pdf paths
    return guard_shell.opt_values(rest, "--filename", "--path") + (
        args[1:] if verb in ("video-start", "upload", "drop", "state-load", "pdf") else []
    )


def check_pw(rest, ctx, cwds, outs):
    """R2, R3, R6 for one playwright-cli command; the files it writes go to outs (R4, R5 check them)."""
    if pw_help(rest):
        return None  # help text only
    ctx.runs_pw, args = True, pw_args(rest)
    verb, hosts = (args or [""])[0], guard_paths.allowed_hosts()
    if opt := next((o for o in PW_SOURCE_OPTS if any(a.split("=", 1)[0] == o for a in rest)), None):
        return f"playwright-cli {opt} picks another browser, profile or config file; open the browser with qa-mem browser-open [<url>]"
    if verb in PW_DENY:
        return f"playwright-cli {verb} blocked: {PW_DENY[verb]}"
    if verb in PW_DENY_EXPLORER and ctx.explorer:
        return f"playwright-cli {verb} blocked for the explorer: {PW_DENY_EXPLORER[verb]}"
    if verb == "open" and ctx.explorer and not pw_profile_values(rest):
        return OPEN_REASON
    if verb == "show" and any(
        h.strip("[]").lower() not in ("localhost", "127.0.0.1", "::1") for h in guard_shell.opt_values(rest, "--host")
    ):
        return "playwright-cli show --host would expose the logged-in QA browser to the network; keep it on localhost"
    if verb == "eval" and secret_js(" ".join(rest)):
        return "playwright-cli eval reading cookie or web storage values blocked; read key names: Object.keys(localStorage)"
    if (
        verb == "eval"
        and (js := " ".join(rest))
        and (EVAL_WRITE_RE.search(js) or (EVAL_FETCH_RE.search(js) and EVAL_METHOD_RE.search(js)))
    ):
        return EVAL_WRITE_REASON
    if any(
        verb != "open"
        or not v
        or guard_shell.dynamic(v)
        or any(guard_paths.real(v, c) not in guard_paths.main_profile() for c in cwds)
        for v in pw_profile_values(rest)
    ):
        return "playwright-cli --profile/--user-data-dir: only `open` with the qa-sdet main profile, spelled out (qa-mem browser-open)"
    sent = args[1:] + guard_shell.opt_values(rest, "--path") if verb in ("upload", "drop") else []
    if any(guard_paths.GLOB_RE.search(f) for f in sent) or any(
        map(guard_paths.is_secret_path, guard_paths.paths(sent, cwds))
    ):
        return UPLOAD_REASON
    outs += pw_file_args(rest, verb, args)
    if bad := next(
        (t for t in args[1:] if verb in NAV_VERBS and not guard_paths.host_ok(t, hosts)), None
    ):  # unknown flags may shift the URL
        return f"playwright-cli {verb} to non-allowlisted host: {bad} (allowed: {sorted(hosts)})"
    if bad := next(
        (u for u in js_urls(" ".join(rest)) if verb in NAV_VERBS | {"eval"} and not guard_paths.host_ok(u, hosts)), None
    ):
        return f"playwright-cli {verb} references non-allowlisted URL: {bad}"
    if verb in FAULT_VERBS and not (ctx.marker and ctx.marker.get("faults") is True):
        return f"playwright-cli {verb} is fault injection; rerun the skill with --faults to allow it"


def js_urls(text):
    """Full URLs (not ones only compared) and `//host` references in page JS or arguments."""
    hosts = [h for h in SCHEMELESS_URL_RE.findall(text) if "." in h or h.lower() == "localhost"]  # not XPath //td
    return [m.group(0) for m in URL_RE.finditer(text) if not compared_only(text, m.start(), m.group(0))] + [
        # not a vulnerability (S5332): a scheme added only to parse a bare host; nothing is fetched
        "http://" + h  # NOSONAR
        for h in hosts
    ]


XML_NAMESPACES = (
    "http://www.w3.org/2000/svg",
    "http://www.w3.org/1999/xhtml",
    "http://www.w3.org/1999/xlink",
    "http://www.w3.org/2000/xmlns/",
    "http://www.w3.org/XML/1998/namespace",
    "http://www.w3.org/1998/Math/MathML",
)


_SELECTOR_CALL_RE = re.compile(r"(?:querySelector(?:All)?|matches|closest|\$\$?)\s*\(\s*(['\"`])")


def in_selector(js, pos):
    """Is js[pos] inside a CSS attribute selector a selector call is given (`querySelectorAll('a[href^="…`)? A URL in
    `[u = '…']` outside such a string is JS (an array holding an assignment), and fetching it is a fetch."""
    w = js[max(0, pos - 300) : pos]
    calls = list(_SELECTOR_CALL_RE.finditer(w))
    if not calls:
        return False
    call = calls[-1]  # the nearest selector call before the URL
    inside = w[call.end() :]  # still in the call's string: no closing quote, no ${…} code in a template
    return call.group(1) not in inside and "${" not in inside and bool(SELECTOR_ATTR_RE.search(inside))


SELECTOR_ATTR_RE = re.compile(r"\[\s*[\w:-]+\s*[~|^$*]?=\s*['\"]?$")


def compared_only(js, pos, url):
    """A URL used as data, not fetched: an XML namespace, a string comparison (`.startsWith('https://…')`, `=== '…'`),
    or a CSS attribute selector (`a[href^="https://docs.x/"]` matches, fetches nothing)."""
    return (
        url.startswith(XML_NAMESPACES)
        or re.search(
            r"(\.(startsWith|endsWith|includes|indexOf|lastIndexOf)\s*\(|[!=]==?)\s*['\"`]$",
            js[max(0, pos - 40) : pos],
        )
        is not None
        or in_selector(js, pos)
    )


_KEY_READ_RE = re.compile(r"(Object\.keys\(\s*(window\.)?|\bin\s+(window\.)?|typeof\s+(window\.)?)$")
_QUOTE_END_RE = re.compile(r"['\"`]$")
_TEXT_SEARCH_END_RE = re.compile(
    r"(\.(includes|indexOf|lastIndexOf|startsWith|endsWith|match|search|split)\s*\(|[!=]==?)\s*$"
)


def storage_value_read(js):
    """Any use of localStorage/sessionStorage except key names, .length and writes: returning the Storage
    object prints every value, and `localStorage.token` reads one."""
    for m in re.finditer(r"\b(local|session)Storage\b", js):
        # Look back at most 200 chars (pos/endpos keep \b honest at the window edge): a scan of the whole
        # prefix per match made long evals quadratic. More than 200 spaces before the word just counts as a read.
        lo, after = max(0, m.start() - 200), js[m.end() : m.end() + 200]
        if _KEY_READ_RE.search(js, lo, m.start()):
            continue  # key names / existence checks
        if re.match(r"\s*\.\s*(length|key|clear|setItem|removeItem)\b", after):
            continue
        if (
            _QUOTE_END_RE.search(js, lo, m.start())
            and re.match(r"['\"`]", after)
            and _TEXT_SEARCH_END_RE.search(js, max(0, m.start() - 41), m.start() - 1)
        ):
            continue  # the word searched for in text, e.g. innerText.includes('localStorage'); not Reflect.get / window['localStorage']
        return True
    return False


def secret_js(js):
    """Page JS that reads cookie or web-storage values. A length (`document.cookie.length`) or the word searched for
    in page text (`body.innerText.includes('cookie')`, `x === 'cookie'`) reads no value."""
    if re.search(r"(?<![\w$.])with\s*\(", js) and re.search(r"(?<![\w$])cookie\b", js):
        return True  # `with (document) cookie` reads it without naming document.cookie
    for m in SECRET_JS.finditer(js):
        if "ookie" in m.group(0) and re.match(r"\s*\.\s*length\b", js[m.end() : m.end() + 40]):
            continue
        if re.fullmatch(r"['\"`]cookie['\"`]", m.group(0)) and _TEXT_SEARCH_END_RE.search(
            js[max(0, m.start() - 40) : m.start()]
        ):
            continue
        return True
    return storage_value_read(js)


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
        out.append("list" if a == "type" and not nxt and positional[k - 1 : k] == ["link"] else a)
    return out


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
        nxt = positional[positional.index("auth") + 1 : positional.index("auth") + 2]
        if nxt and nxt[0] in ACLI_AUTH_OK:
            return None
    return "acli write command blocked: qa-sdet only reads Jira and drafts content; the user files it"


def check_qamem(args, ctx, cwds=()):
    """qa-mem browser-open and page-start: allowedHosts only (only '--' words are browser-open's options); qa-mem
    evidence and annotate: the image is one of the explorer's run files."""
    if ctx.explorer and args[:1] and args[0] in MANUAL_WRITES:
        return f"qa-mem {args[0]} is the user's call (a manual memory write); put what you saw in the run JSON's notes"
    if ctx.explorer and args[:1] and args[0] in MARKER_WRITES:
        return MARKER_REASON
    if ctx.explorer and args[:1] and args[0] in CRAWLS:
        return (
            f"qa-mem {args[0]} drives a whole browser pass the guard can't see; it is the coordinator's. Test your "
            "page with playwright-cli"
        )
    if ctx.explorer and args[:1] in (["evidence"], ["annotate"]):  # the image it writes: one of its run files
        png = next((a for a in args[1:] if not a.startswith("-")), "")
        if not png or "$" in png or not any(guard_paths.run_file(guard_paths.real(png, c), cwds) for c in cwds):
            return guard_paths.run_files_reason()
    if args[:1] == ["evidence"]:  # it runs playwright-cli
        ctx.runs_pw = True
        return None
    if args[:1] == ["api-probe"]:
        return check_api_probe(args[1:], ctx)
    if args[:1] not in (["browser-open"], ["page-start"]):
        return None
    hosts, pos = (
        guard_paths.allowed_hosts(),
        [a for k, a in enumerate(args[1:]) if not a.startswith("--") and args[k] != "--session"],
    )
    if bad := next((u for u in pos if not guard_paths.host_ok(u, hosts)), None):
        return f"qa-mem {args[0]} to non-allowlisted host: {bad} (allowed: {sorted(hosts)})"
    ctx.runs_pw = True


def check_api_probe(args, ctx):
    """qa-mem api-probe <METHOD> <url> [--json b] [--content-type t] [--header 'N: v']: allowedHosts only, no credential
    headers, writes only in a run with api_writes."""
    pos, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a.split("=", 1)[0] in ("--json", "--content-type", "--header"):
            skip = "=" not in a
        elif not a.startswith("--"):
            pos.append(a)
    if len(pos) != 2 or any(guard_shell.dynamic(w) for w in pos):
        return "qa-mem api-probe <METHOD> <url>: spell out the method and the URL (they can't be checked otherwise)"
    method, url, hosts = pos[0].upper(), pos[1], guard_paths.allowed_hosts()
    if not guard_paths.host_ok(url, hosts):
        return f"qa-mem api-probe to non-allowlisted host: {url} (allowed: {sorted(hosts)})"
    if bad := next(
        (
            h
            for h in guard_shell.opt_values(args, "--header")
            if h.split(":", 1)[0].strip().lower() in API_FORBIDDEN_HEADERS
        ),
        None,
    ):
        return (
            f"qa-mem api-probe header {bad.split(':', 1)[0].strip()} refused: the browser's own session authenticates the "
            "request; credentials are never sent by hand"
        )
    if method in API_WRITE_METHODS and not (ctx.marker and ctx.marker.get("api_writes") is True):
        return (
            f"qa-mem api-probe {method} writes data: only a run started with --api-writes may (and only on qa-sdet- records); "
            "GET, HEAD and OPTIONS are always allowed"
        )
    ctx.runs_pw = True


_HTTP_SHORT_VAL = {o[1] for o in HTTP_VALUE_OPTS if len(o) == 2 and o[0] == "-"}


def explorer_net(name, rest, raw):
    """Deny reason for a network tool the explorer runs, or None: an HTTP client may fetch from the allowed hosts
    (an API check), nothing else reaches out. shortcut: a proxy set in ~/.curlrc or ~/.wgetrc by an earlier command
    isn't seen; check for one in curl/wget calls if the explorer is ever found writing those files."""
    if name not in HTTP_CLIENTS:
        return (
            f"{name} reaches other machines outside the browser; the explorer fetches only from the QA app "
            "(the browser, or curl/wget to an allowed host)"
        )
    opts = HTTP_REROUTE_OPTS | (WGET_FILE_OPTS if name in ("wget", "aria2c") else set())
    bad_short = {o[1] for o in opts if len(o) == 2 and o[0] == "-"}  # glued/bundled short flags: -x, -sx, -sK, -qi
    reroute = PROXY_ENV_RE.search(raw) is not None
    for a in takewhile(lambda x: x != "--", rest):
        if a.startswith("--"):
            opt = a.split("=", 1)[0]
            if opt in opts or (name != "curl" and len(opt) > 3 and any(o.startswith(opt) for o in opts)):
                reroute = True
        elif a.startswith("-") and len(a) > 1:
            for c in a[1:]:
                if c in bad_short:
                    reroute = True
                    break
                if c in _HTTP_SHORT_VAL:
                    break  # rest of cluster is a value (e.g. -dx is --data x, not -x proxy)
    if reroute:
        return f"{name} with a proxy, a resolve override or an option file can send the request elsewhere; call the URL directly"
    hosts, urls, end = guard_paths.allowed_hosts(), URL_RE.findall(" ".join(rest)), False
    for k, a in enumerate(
        rest
    ):  # bare hosts/IPs too (`curl -s qa.example/api`, `curl --url=evil.example`, `169.254.169.254`)
        if not end and a == "--":
            end = True
            continue
        if not end and a.startswith("--url=") and not URL_RE.search(a):
            urls.append(a.split("=", 1)[1])
        elif (
            (end or not a.startswith("-"))
            and not (not end and k and rest[k - 1] in HTTP_VALUE_OPTS)
            and not URL_RE.search(a)
            and HOSTISH_RE.fullmatch(a)
        ):
            urls.append(a)
    if not urls:
        return f"{name} without a URL the guard can check; pass the full URL (https://<allowed host>/…)"
    for u in urls:
        if not guard_paths.host_ok(u, hosts):
            return f"{name} to non-allowlisted host: {u} (allowed: {sorted(hosts)}); the explorer fetches only from the QA app"
    return None
