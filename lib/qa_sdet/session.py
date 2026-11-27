"""The `qa` browser session: `browser-open`, `session-check`, `page-start`, the sign-in state and the build read."""

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from urllib.parse import urlsplit

from . import core


def profile_dir():
    """The persistent `main` browser profile (the QA sign-in lives here; the guard protects it)."""
    return core.DATA_DIR / "profiles" / "main"


def signed_in_stamp():
    """Written by `qa-mem session-check --record` once qa-login's sign-in is verified; removed when a new qa-login starts."""
    return core.DATA_DIR / "profiles" / ".signed-in"


def ensure_profile_dir():
    p = profile_dir()
    core.DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    for d in (p.parent, p):
        d.mkdir(mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    return p


def browser_cmd(session, headed, url, browser, profile):
    cmd = ["playwright-cli", f"-s={session}", "open", f"--browser={browser}", "--profile", str(profile)]
    return cmd + (["--headed"] if headed else []) + ([url] if url else [])


def browser_open(args):
    session, headed, url = "qa", False, None
    it = iter(args)
    for a in it:
        if a == "--headed":
            headed = True
        elif a == "--session" or a.startswith("--session="):
            session = a.split("=", 1)[1] if "=" in a else next(it, "")
        elif a.startswith("-") or url:
            raise core.BadOption(f"browser-open: unexpected argument {a!r}")
        else:
            url = a
    if session not in ("qa", "qa-login"):
        raise core.BadInput("browser-open: --session must be qa or qa-login")
    browser, where = None, f'set "browser" in {core.CONFIG}'
    try:
        browser = core.config(strict=True).get("browser")
    except OSError:
        where = f'{core.CONFIG} is missing; create it with a "browser" key'
    except ValueError as e:
        where = f"{core.CONFIG} isn't valid JSON ({e}); fix it"
    if browser not in core.BROWSERS:
        raise core.BadInput(
            f"browser-open: config browser must be one of {', '.join(core.BROWSERS)} (got {browser!r}): "
            f"{where} (`qa-mem browsers` lists what's installed)"
        )
    # every PLAYWRIGHT_MCP_* var (storage state, isolated, browser, user data dir…) would override the profile or break the launch
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PLAYWRIGHT_MCP_", "PWTEST_"))}
    profile = ensure_profile_dir()
    if (
        session == "qa-login"
    ):  # a new sign-in starts: until it is verified (session-check --record), doctor says to sign in
        signed_in_stamp().unlink(missing_ok=True)
    other = "qa-login" if session == "qa" else "qa"
    try:  # both sessions use profiles/main, which opens in one browser only: stop the other one (no-op if it's closed)
        subprocess.run(["playwright-cli", f"-s={other}", "close"], env=env, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pass  # the open below reports a missing playwright-cli or a profile still in use
    try:  # captured (the daemon gets its own stdio, so this returns) to translate the profile-in-use error
        # not a vulnerability (S6350): an argument list (no shell) from the user's own config, which agents can't write
        r = subprocess.run(  # NOSONAR
            browser_cmd(session, headed, url, browser, profile),
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
        )
    except OSError as e:
        print(f"browser tool failed: can't run playwright-cli ({e}); npm i -g @playwright/cli", file=sys.stderr)
        return 2
    code_ = r.returncode if r.returncode >= 0 else 128 - r.returncode  # killed by signal N: exit 128+N like a shell
    text = f"{r.stderr or ''}\n{r.stdout or ''}"
    # the category is the first stderr line, as the explorer and the skills report it
    if "browser is already in use" in text.lower():
        # playwright-cli's hint (--isolated) would drop the sign-in; say what actually holds the profile
        print(
            "browser profile in use: another qa-sdet browser holds the main profile (a /qa-sdet:qa-login window not "
            "finished, or a run in another project folder). Close the other qa-sdet browser (or run `playwright-cli "
            "close-all` in that folder) and retry; never use --isolated, it has no login.",
            file=sys.stderr,
        )
        code_ = code_ or 1
    elif code_:
        first = next(
            (x.strip() for x in text.splitlines() if x.strip() and not x.lstrip().startswith("#")),
            f"playwright-cli exited {code_}",
        )  # (not a Markdown heading such as "### Error")
        kind = (
            f"browser {browser} can't start" if browser in text.lower() else "browser tool failed"
        )  # the error names it
        print(f"{kind}: {core.short(first, 200)}", file=sys.stderr)
    sys.stdout.write(r.stdout or "")
    sys.stderr.write(r.stderr or "")
    return code_


def _norm_host(raw):
    s = str(raw or "").strip().lower()
    if not s:
        return "", ""
    try:
        # not a vulnerability (S5332): a scheme added only to parse a bare host; nothing is fetched
        u = urlsplit(s if "://" in s else "http://" + s)  # NOSONAR
    except ValueError:
        return "", ""
    hostname = (u.hostname or "").rstrip(".")
    netloc = u.netloc.rpartition("@")[2].rstrip(".")
    return netloc, hostname


def unwrap_raw(text):
    t = (text or "").strip()
    try:
        v = json.loads(t)
        return v if isinstance(v, (dict, str)) else t
    except ValueError:
        return t


def pw_env():
    """The environment playwright-cli runs with: no PLAYWRIGHT_MCP_* / PWTEST_* (they would override the profile)."""
    return {k: v for k, v in os.environ.items() if not k.startswith(("PLAYWRIGHT_MCP_", "PWTEST_"))}


def close_qa(env):
    try:
        subprocess.run(["playwright-cli", "-s=qa", "close"], env=env, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pass


PAGE_STATE_JS = (
    "{host: location.host, hasPassword: Boolean(document.querySelector('input[type=\"password\"]')), "
    "status: (performance.getEntriesByType('navigation')[0] || {}).responseStatus || 0}"
)


def page_signed_in(cfg, parsed):
    """(signed_in, status) of a PAGE_STATE_JS result: signed_in is False on ssoHost or a page with a password field."""
    sso_netloc, sso_host = _norm_host(cfg.get("ssoHost"))
    cur_netloc, cur_host = _norm_host(parsed.get("host"))
    is_sso = bool(sso_host) and (
        (":" in sso_netloc and cur_netloc == sso_netloc) or (":" not in sso_netloc and cur_host == sso_host)
    )
    status = parsed.get("status") if isinstance(parsed.get("status"), int) else 0
    return not (is_sso or parsed.get("hasPassword")), status


def _sign_in_state(cfg, env):
    """(signed_in, problem, status) of the page the `qa` session shows: problem is a 'browser tool failed: …' line when
    the check itself failed; signed_in is False on ssoHost or a page with a password field; status is the HTTP status of
    the page's own document (0 when the browser doesn't expose it)."""
    js = f"JSON.stringify({PAGE_STATE_JS})"
    try:
        r = subprocess.run(
            ["playwright-cli", "-s=qa", "--raw", "eval", js],
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"browser tool failed: session-check eval failed ({e})", 0
    if r.returncode != 0:
        detail = core.short((r.stderr or r.stdout or f"exited {r.returncode}").strip(), 200)
        return None, f"browser tool failed: session-check eval failed ({detail})", 0
    parsed = unwrap_raw(r.stdout)
    if isinstance(parsed, str):
        parsed = unwrap_raw(parsed)
    if not isinstance(parsed, dict) or "host" not in parsed:
        return None, f"browser tool failed: unexpected session-check output ({core.short(r.stdout.strip(), 120)})", 0
    signed_in, status = page_signed_in(cfg, parsed)
    return signed_in, None, status


def stored_build():
    """The build the user last recorded with `qa-mem stale`, or None. Read-only: never creates the DB."""
    p = core.db_path()
    if not p.is_file():
        return None
    try:
        con = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            row = con.execute("SELECT value FROM meta WHERE key='build'").fetchone()
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return None
    build = core.norm_build(row[0]) if row else core.UNKNOWN
    return None if build == core.UNKNOWN else build


BUILD_PART = 60  # characters of one buildSource's text kept in a joined build; a longer one goes in as its digest


def _build_js(bs, hosts):
    """The eval that reads one buildSource ({type: selector|url|header, value}), or None for anything else."""
    bs = bs if isinstance(bs, dict) else {}
    btype, bval = bs.get("type"), str(bs.get("value") or "").strip()
    if btype == "selector" and bval:
        return f"document.querySelector({json.dumps(bval)})?.textContent || ''"
    if btype == "url" and bval and (bval.startswith("/") or core.url_allowed(bval, hosts)):
        return f"fetch({json.dumps(bval)}).then(r => r.text())"
    if btype == "header" and bval:
        return f"fetch(location.href).then(r => r.headers.get({json.dumps(bval)}) || '')"
    return None


def _build_notice(bs, error):
    """A `build_unreadable` notice in the project's plugin log: the run goes on with build `unknown`, which hides
    nothing on the page but makes every page look stale; the eval's first line says why. Best effort."""
    try:
        from . import plugin_log

        first = next((x.strip() for x in str(error).splitlines() if x.strip()), "")
        plugin_log.append_event(
            plugin_log.resolve_project() / "qa-out",
            "notice",
            {"kind": "build_unreadable", "source": str(bs.get("type")), "detail": plugin_log.scrub_text(first, 200)},
        )
    except Exception:  # noqa: BLE001, S110 - a notice must never break the page start
        pass


def _read_build(cfg, hosts, env):
    """The build per config buildSource (selector, url or header), or core.UNKNOWN. With no buildSource, the build
    recorded by `qa-mem stale`; with one, only the live read, so a broken buildSource isn't hidden by an old build.
    A list of sources (an app shell and the modules deployed on their own) is read in order and joined with '+': a
    deploy of any part is a new build, and a part that can't be read leaves the build unknown.
    shortcut: one build for the whole app, so a module's deploy marks every page stale; keep a build per module
    (compared per page in memory) when re-verifying the other modules costs too much."""
    listed = cfg.get("buildSource")
    scripts = [_build_js(bs, hosts) for bs in (listed if isinstance(listed, list) else [listed])]
    if not scripts or not all(scripts):
        return stored_build() or core.UNKNOWN
    parts = []
    for bs, bjs in zip(listed if isinstance(listed, list) else [listed], scripts):
        try:
            br = subprocess.run(
                ["playwright-cli", "-s=qa", "--raw", "eval", bjs],
                env=env,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            _build_notice(bs, str(e))
            return core.UNKNOWN
        if br.returncode != 0:
            _build_notice(bs, br.stderr or br.stdout or f"the eval exited {br.returncode}")
            return None
        raw_b = unwrap_raw(br.stdout)
        parts.append(core.norm_build(raw_b if isinstance(raw_b, str) else br.stdout))
        if parts[-1] == core.UNKNOWN:
            _build_notice(bs, "the source read as empty (no such element, or no text)")
    if len(parts) == 1 or core.UNKNOWN in parts:
        return parts[0] if len(parts) == 1 else core.UNKNOWN
    digest = lambda p: hashlib.sha1(p.encode(), usedforsecurity=False).hexdigest()[:10]
    return "+".join(p if len(p) <= BUILD_PART else digest(p) for p in parts)


def qa_config(what):
    try:
        cfg = core.config(strict=True)
    except (OSError, ValueError) as e:
        raise core.BadInput(f"{what}: unreadable {core.CONFIG} ({e})") from e
    return cfg, core.allowed_hosts(cfg)


def session_check(record=False, read_build=False):
    """Headless check that the `main` browser profile is signed in at qaBaseUrl (not redirected to ssoHost or a login
    form). Exit 0 = signed in, 1 = session expired, 2 = config/browser/tool error."""
    cfg, hosts = qa_config("session-check")
    base_url = str(cfg.get("qaBaseUrl") or "").strip()
    if not core.url_allowed(base_url, hosts):
        raise core.BadInput(
            f"session-check: qaBaseUrl {base_url!r} must be an http(s) URL in allowedHosts ({sorted(hosts)})"
        )
    with contextlib.redirect_stdout(io.StringIO()):
        rc = browser_open([base_url])
    if rc:
        return 2
    env = pw_env()
    try:
        signed_in, problem, _ = _sign_in_state(cfg, env)
        if problem:
            print(problem, file=sys.stderr)
            return 2
        if not signed_in:
            print("session expired, run /qa-sdet:qa-login", file=sys.stderr)
            return 1
        build = _read_build(cfg, hosts, env) if read_build else None
        if record:
            ensure_profile_dir()
            core.write(signed_in_stamp(), datetime.now().strftime("%Y-%m-%d %H:%M") + "\n")  # noqa: DTZ005 - the user's local date (stored times use core.now(), UTC)
        print(f"ok  build={build}" if read_build else "ok")
        return 0
    finally:
        close_qa(env)


def page_start(url):
    """The explorer's start in one call: open the `qa` session at `url` in the signed-in profile, check the sign-in, read
    the build. Prints one JSON line: {status: ok, build, url} with the browser left open (exit 0), or {status: blocked,
    block_scope: browser|session|page, reason} (exit 1; a sign-in page closes the browser, an HTTP error page leaves it open
    for the explorer's finish)."""
    cfg, hosts = qa_config("page-start")
    if not core.url_allowed(url, hosts):
        raise core.BadInput(f"page-start: {url!r} must be an http(s) URL in allowedHosts ({sorted(hosts)})")
    started = start_at(cfg, hosts, url)
    print(json.dumps(started))
    return 0 if started["status"] == "ok" else 1


def start_at(cfg, hosts, url):
    """page-start's result for an allowed url: {status: ok, build, url} or {status: blocked, block_scope, reason}, plus
    the browser it opens (a run file records it)."""
    out = lambda d: d | {"browser": cfg.get("browser")}
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        rc = browser_open([url])
    if rc:
        first = next((x.strip() for x in err.getvalue().splitlines() if x.strip()), f"browser-open exited {rc}")
        return out({"status": "blocked", "block_scope": "browser", "reason": core.short(first, 300)})
    env = pw_env()
    signed_in, problem, status = _sign_in_state(cfg, env)
    if problem or not signed_in:
        close_qa(env)
        return out(
            {
                "status": "blocked",
                "block_scope": "browser" if problem else "session",
                "reason": problem or "session expired, run /qa-sdet:qa-login",
            }
        )
    if status >= 400:  # this page only (a 403, 404 or 500): the coordinator goes on with the other pages
        return out({"status": "blocked", "block_scope": "page", "reason": f"the page returned HTTP {status}"})
    return out({"status": "ok", "build": _read_build(cfg, hosts, env) or core.UNKNOWN, "url": url})
