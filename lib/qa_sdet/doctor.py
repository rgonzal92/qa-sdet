"""Browser checks, `doctor` and the `setup-permissions` rules."""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from . import core, ops


def installed_browsers(names=core.BROWSERS):
    """{name: (installed, where or how to install)}. chrome and msedge are the system browsers (Playwright's
    channel paths); firefox and webkit are Playwright's own builds: a system Firefox can't be driven."""
    import shutil
    import subprocess
    env = os.environ  # Windows roots in Playwright's own lookup order (playwright-core _createChromiumChannel)
    roots = [r for r in (env.get("LOCALAPPDATA"), env.get("PROGRAMFILES"), env.get("PROGRAMFILES(X86)")) if r]
    if env.get("HOMEDRIVE"):
        roots += [env["HOMEDRIVE"] + r"\Program Files", env["HOMEDRIVE"] + r"\Program Files (x86)"]
    system = {
        "chrome": ("Google Chrome", ["/opt/google/chrome/chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
                   + [r + r"\Google\Chrome\Application\chrome.exe" for r in roots]),
        "msedge": ("Microsoft Edge", ["/opt/microsoft/msedge/msedge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
                   + [r + r"\Microsoft\Edge\Application\msedge.exe" for r in roots]),
    }
    pw, out = shutil.which("playwright-cli"), {}
    for name in names:
        if name in system:
            label, paths = system[name]
            hit = next((x for x in paths if os.path.isfile(x)), None)
            out[name] = (bool(hit), hit or f"install {label} the usual way for this system")
            continue
        loc, why = None, "can't check: playwright-cli isn't working (npm i -g @playwright/cli), then install-browser " + name
        if pw:
            why = f"can't check: `playwright-cli install-browser --dry-run {name}` gave no install location"
            try:
                dry = subprocess.run([pw, "install-browser", "--dry-run", name], capture_output=True, text=True,
                                     timeout=30).stdout
                m = re.search(r"Install location:\s*(.+)", dry)
                loc = m.group(1).strip() if m else None
            except (subprocess.TimeoutExpired, OSError):
                pass
        # Playwright writes INSTALLATION_COMPLETE last: a folder without it is an interrupted download
        ok = bool(loc) and (Path(loc) / "INSTALLATION_COMPLETE").is_file()
        out[name] = (ok, f"Playwright build {Path(loc).name}" if ok else f"playwright-cli install-browser {name}" if loc
                     else why)
    return out


def pillow_fix():
    """The install command that works here: pip refuses an OS-managed python3 (PEP 668: Debian/Ubuntu, Arch, Fedora,
    Homebrew), so name the OS package instead."""
    import shutil, sysconfig
    if not os.path.exists(os.path.join(sysconfig.get_paths()["stdlib"], "EXTERNALLY-MANAGED")):
        return f"{sys.executable} -m pip install --user pillow"
    for tool, cmd in (("brew", "brew install pillow"), ("apt-get", "sudo apt-get install python3-pil"),
                      ("dnf", "sudo dnf install python3-pillow"), ("pacman", "sudo pacman -S python-pillow"),
                      ("zypper", "sudo zypper install python3-Pillow")):
        if shutil.which(tool):
            return f"{cmd} (this python3 is OS-managed, so pip refuses)"
    return f"install your OS's Pillow package for {sys.executable} (it is OS-managed, so pip refuses)"


def claude_mcp_list():
    """`claude mcp list` output, or None when Claude Code isn't callable (it health-checks each server)."""
    import subprocess
    try:
        r = subprocess.run(["claude", "mcp", "list"], capture_output=True, encoding="utf-8", errors="replace",
                           timeout=30)
        return r.stdout if r.returncode == 0 or r.stdout.strip() else "FAILED"  # an error is not "no servers"
    except subprocess.TimeoutExpired:
        return "TIMEOUT"  # a first `npx @playwright/mcp@latest` download can take this long
    except OSError:
        return None


def doctor():
    """Setup check for a new machine. Prints one line per item; returns the number of problems.
    Subprocess timeouts sum to under 2 minutes (15 + 30 + 30 + 30 s) so a skill's Bash call never cuts it off."""
    from urllib.parse import urlsplit
    import shutil
    import subprocess
    import time
    problems = 0

    def line(ok, what, fix=""):
        nonlocal problems
        problems += not ok
        print(f"{'ok  ' if ok else 'FIX '} {what}" + (f"  -> {fix}" if fix and not ok else ""))

    line(sys.version_info >= (3, 9), f"python {sys.version.split()[0]}", "install Python 3.9+ as python3 (hooks need it)")
    try:
        chosen = json.loads(core.CONFIG.read_text()).get("browser")
    except (OSError, ValueError, AttributeError):
        chosen = None
    listing = []

    def mcp_servers(*words):
        """(connected, others) server names whose line mentions one of `words`. Names only: a line's command or URL can
        carry keys. This script can't see the session's tools, so it asks `claude mcp list`."""
        if not listing:
            listing.append(claude_mcp_list())
        hits = [ln.strip() for ln in ("" if listing[0] in (None, "TIMEOUT", "FAILED") else listing[0]).splitlines()
                if ": " in ln and " - " in ln and "test-mcp" not in ln  # not the Playwright Test MCP
                and (any(w in ln.split(": ", 1)[0].lower() for w in words)  # the server's name,
                     or any(w in ln.lower() for w in words if "/" in w or "." in w))]  # or its package / URL
        status = lambda ln: next((label for key, label in (("Connected", "connected"), ("Pending", "pending approval"),
                                                            ("auth", "needs sign-in"), ("Failed", "failed to connect"))
                                  if key in ln.rsplit(" - ", 1)[-1]), "not connected")  # fixed labels: error text can hold keys
        connected = [ln.split(": ", 1)[0] for ln in hits if status(ln) == "connected"]
        others = [f"{ln.split(': ', 1)[0]} ({status(ln)})" for ln in hits if status(ln) != "connected"]
        return connected, others

    pw, on_path, mcp_browser = shutil.which("playwright-cli"), bool(shutil.which("playwright-cli")), False
    if pw:  # on PATH isn't enough: a broken install must fall back too
        try:
            pw = pw if subprocess.run([pw, "--version"], capture_output=True, timeout=15).returncode == 0 else None
        except (subprocess.TimeoutExpired, OSError):
            pw = None
    if pw:
        line(True, "playwright-cli runs (--version)")
    else:
        found, other = mcp_servers("playwright", "browser", "@playwright/mcp")
        mcp_browser = bool(found)
        unknown = [n for n in mcp_servers("")[0] if n not in found]  # connected servers this check can't classify
        cli = "playwright-cli runs (--version)" if on_path else "playwright-cli on PATH"
        cli_fix = "reinstall: npm i -g @playwright/cli" if on_path else "npm i -g @playwright/cli"
        if mcp_browser:  # a working fallback: the CLI is a recommendation, not a problem
            print(f"info {cli}: no, runs use the Playwright MCP instead (for qa-login and recordings: {cli_fix})")
        else:
            line(False, cli, cli_fix)
        if listing[0] in (None, "TIMEOUT", "FAILED"):  # can't tell from here
            why = ("`claude` not on PATH" if listing[0] is None else "`claude mcp list` failed; run it to see why"
                   if listing[0] == "FAILED" else "`claude mcp list` timed out; run doctor again")
            print(f"info fallback browser: couldn't list MCP servers ({why}); runs look for browser_* tools themselves")
        elif mcp_browser or other or not unknown:
            line(mcp_browser, "fallback browser: " + ("Playwright MCP " + ", ".join(found) if found else "a connected Playwright MCP"),
                 ("connect or approve it in Claude Code (/mcp): " + ", ".join(other)) if other else
                 f"claude mcp add playwright -- npx @playwright/mcp@latest --isolated --browser {chosen if chosen in core.BROWSERS else '<browser>'}"
             + (f", then npx @playwright/mcp@latest install-browser {chosen}" if chosen in ("firefox", "webkit") else "")
             + " (or install playwright-cli)")
        else:  # this script sees names, not tools; runs check for a browser_navigate tool (common.md C0)
            print("info fallback browser: none named playwright/browser; runs use any connected MCP with browser_* tools "
                  f"(connected: {', '.join(unknown)})")
    if pw:  # runs open the browser the user chose (config `browser`); C1 asks for it from what's installed
        if chosen is None:
            line(False, "config has browser (chrome, msedge, firefox or webkit)",
                 "qa-explore / qa-ticket / qa-login ask you which installed browser to use (`qa-mem browsers` lists them)")
        elif chosen not in core.BROWSERS:
            line(False, f"config browser {chosen!r} is one of {', '.join(core.BROWSERS)}", "`qa-mem browsers` lists what's installed")
        else:
            ok_b, how = installed_browsers((chosen,))[chosen]
            if how.startswith("can't check"):  # the CLI works; only the lookup failed
                print(f"info browser {chosen}: {how}")
            else:
                line(ok_b, f"browser {chosen} installed" + (f" ({how})" if ok_b else ""), how)
    acli, ok = shutil.which("acli"), False
    if acli:
        try:
            ok = subprocess.run([acli, "jira", "auth", "status"], capture_output=True, encoding="utf-8",
                                errors="replace", timeout=30).returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            ok = False
    if acli and ok:
        line(True, "acli signed in to Jira")
    else:
        found, other = mcp_servers("atlassian", "jira", "mcp.atlassian.com", "mcp-atlassian")
        if acli and found:  # a working fallback: signing in to acli is optional
            print("info acli not signed in: runs use the Jira MCP (to use acli: acli jira auth login)")
        elif acli:
            line(False, "acli signed in to Jira", "acli jira auth login")
        print(f"{'ok  ' if found else 'info'} Jira fallback: " + (
            "couldn't list MCP servers; runs look for Jira read tools, else you paste tickets"
            if listing[0] in (None, "TIMEOUT", "FAILED") else
            "Atlassian/Jira MCP " + ", ".join(found) if found else
            "Jira MCP not connected: " + ", ".join(other) + " (connect it in /mcp) or paste tickets" if other else
            "no acli or Jira MCP: tickets are pasted (optional: install acli or connect the Atlassian MCP)"))
    cfg, readable = {}, True
    try:
        cfg = json.loads(core.CONFIG.read_text())
        line(isinstance(cfg, dict), f"config {core.CONFIG}", 'the file must be one JSON object: {"app": ..., "qaBaseUrl": ...}')
    except (OSError, ValueError) as e:
        readable = not os.path.exists(core.CONFIG)  # a broken file: its keys can't be judged
        line(False, f"config {core.CONFIG} ({e.__class__.__name__})",
             f"fix the JSON ({e})" if isinstance(e, ValueError) else
             f"make it readable: chmod u+rwx {core.CONFIG.parent}; chmod u+rw {core.CONFIG}" if isinstance(e, PermissionError) else
             "run any qa-sdet skill once; it asks for the values")
    if isinstance(cfg, dict) and readable and (cfg or os.path.exists(core.CONFIG)):  # an existing `{}` still needs its keys named (os.path: Path.exists raises before 3.12)
        missing = [k for k in ("app", "qaBaseUrl", "allowedHosts", "ssoHost", "buildSource") if not cfg.get(k)]
        line(not missing, "config has app, qaBaseUrl, allowedHosts, ssoHost, buildSource", f"add {missing}")
        bs = cfg.get("buildSource")
        line(isinstance(bs, dict) and bs.get("type") in ("selector", "url", "header")
             and isinstance(bs.get("value"), str) and bool(bs["value"].strip()),
             "buildSource is {type: selector|url|header, value}", 'e.g. {"type": "selector", "value": "footer .version"}')
        hosts = cfg.get("allowedHosts")
        line(isinstance(hosts, list) and all(isinstance(h, str) for h in hosts), "allowedHosts is a list of hosts",
             'e.g. "allowedHosts": ["qa.example.com"]')
        norm_hosts = [h.lower().strip().rstrip(".").removeprefix("*.") for h in hosts
                      if isinstance(h, str)] if isinstance(hosts, list) else []  # same rules as the guard
        odd = [h for h in norm_hosts if h and not re.fullmatch(r"[a-z0-9._-]+|::1", h)]  # the guard matches host names only
        if {"localhost", "127.0.0.1", "::1"} & set(norm_hosts):  # loopback aliases, as in the guard
            norm_hosts += ["localhost", "127.0.0.1", "::1"]
        if odd:
            line(False, f"allowedHosts entries are host names ({', '.join(core.short(h, 40) for h in odd[:3])} can never match)",
                 'just the host, no https://, port or path: "https://qa.example.com:8443/app" -> "qa.example.com"')
        try:
            base_url = urlsplit(str(cfg.get("qaBaseUrl") or ""))
            base, base_host = (base_url.hostname or "").rstrip("."), base_url.netloc.rpartition("@")[2].lower()
        except ValueError:
            base = base_host = ""
        if cfg.get("qaBaseUrl") and not (base and str(cfg["qaBaseUrl"]).lower().startswith(("http://", "https://"))):
            line(False, "qaBaseUrl is a full URL", 'e.g. "qaBaseUrl": "https://qa.example.com"')
        elif base:
            line(any(base == h or base.endswith("." + h) for h in norm_hosts),
                 "qaBaseUrl host is in allowedHosts", "add the qaBaseUrl host to allowedHosts")
        sso = cfg.get("ssoHost")
        if sso and not (isinstance(sso, str) and re.fullmatch(r"[A-Za-z0-9.-]+(:\d+)?\.?", sso.strip())):
            line(False, "ssoHost is a host name", 'just the host, no https:// or path: e.g. "ssoHost": "login.microsoftonline.com"')
        elif sso:  # runs compare it with location.host
            line(sso.lower().strip().rstrip(".") not in (base.lower(), base_host), "ssoHost differs from the qaBaseUrl host",
                 "ssoHost is the login provider's host (e.g. login.microsoftonline.com); every run would end BLOCKED")
        prefix = cfg.get("jiraKeyPrefix")
        if prefix not in (None, ""):  # optional: lets the user type ticket numbers without the project key
            line(isinstance(prefix, str) and bool(re.fullmatch(r"[A-Z][A-Z0-9]+", prefix.strip().upper().rstrip("-"),
                                                               re.ASCII)),
                 "jiraKeyPrefix is a Jira project key", 'the letters before the ticket number: e.g. "jiraKeyPrefix": "STUDIO"')
        temps = cfg.get("releaseTemplates")
        if temps not in (None, []):  # optional: the Jira pass-comment draft fills {version} into these
            ok_t = isinstance(temps, list) and all(isinstance(t, str) and "{version}" in t for t in temps)
            line(ok_t, "releaseTemplates lists release names with {version}",
                 'a list such as ["Release-A-{version}"]: the pass-comment draft fills in the fix version')
    try:
        import PIL
        line(True, f"Pillow {PIL.__version__}")
    except ImportError:
        line(False, "Pillow (annotated bug images need it)", pillow_fix())
    if os.path.isfile(ops.signed_in_stamp()) and os.path.isdir(ops.profile_dir()):
        line(True, "signed-in profile present, from /qa-sdet:qa-login (expires with the app's session)")
    elif mcp_browser:
        print("info no signed-in profile: MCP runs ask you to sign in in the MCP's browser window")
    else:
        line(False, "signed-in browser profile", "run /qa-sdet:qa-login")
    if os.path.lexists(core.CONFIG.with_name("qa-state.json")):  # never opened: it holds cookies
        print("info old cookie file present: qa-setup can delete it")
    try:
        con = core.connect()
        n_pages, n_rows = con.execute("SELECT count(DISTINCT page), count(*) FROM codemap").fetchone()
        con.close()
        line(True, f"memory DB {core.db_path()}")
        paths = cfg.get("sourcePaths") if isinstance(cfg, dict) else None
        if paths is not None and not (isinstance(paths, list) and all(isinstance(x, str) and x for x in paths)):
            line(False, "sourcePaths is a list of folders", 'e.g. "sourcePaths": ["/home/me/work/app-ui"]')
        paths = [x for x in paths if isinstance(x, str) and x] if isinstance(paths, list) else []
        gone = [x for x in paths if not os.path.isdir(os.path.expanduser(x))]
        if gone:
            line(False, f"sourcePaths folders exist ({', '.join(core.short(x, 60) for x in gone)} not found)",
                 "fix the path in the config, or run /qa-sdet:qa-init with the right folders")
        print(f"ok   code map: {n_rows} rows on {n_pages} pages" if n_rows else
              "info no code map yet: /qa-sdet:qa-init maps routes, locators, rules and endpoints from your source"
              + (f" ({len(paths)} source folders configured)" if paths else ""))
        line(core.db_path().parent.stat().st_mode & 0o077 == 0, "memory DB folder readable only by you",
             f"chmod 700 {core.db_path().parent}")
    except (OSError, sqlite3.Error) as e:
        line(False, f"memory DB {core.db_path()} ({e})", "use a python3 whose sqlite3 has FTS5 (python.org or Homebrew builds do)"
             if "fts5" in str(e).lower() else f"check you can write to {core.db_path().parent}")
    return problems


# ---------- setup-permissions: allow rules so auto mode doesn't block the plugin's own commands ----------
# Auto mode drops Agent rules and wildcarded interpreter rules (`Bash(python3 *...)`), so qa-mem is allowed as the bare
# command: the plugin's bin/ folder is on the Bash tool's PATH while the plugin is enabled.
PERMISSION_RULES = [
    "Bash(qa-mem *)",
    "Bash(playwright-cli -s=qa *)", "Bash(playwright-cli -s=qa-login *)", "Bash(playwright-cli --version)",
    "Bash(command -v playwright-cli*)",
    "Bash(acli jira auth status*)", "Bash(acli jira workitem view *)", "Bash(acli jira workitem search *)",
    "Bash(acli jira workitem comment list *)",  # C8 reads a ticket's comments too
    "Bash(playwright-cli kill-all*)", "Bash(playwright-cli close-all*)",  # cleanup after a stuck run (C4)
    "mcp__playwright",  # the MCP fallback's tools; the guard still checks hosts, secrets and files on each call
    "Skill(qa-sdet:*)",
    # every skill/explorer reads it (hosts/URLs, no secrets). Read only: allowedHosts is the guard's trust root, so an
    # auto-allowed Edit/Write would let a prompt-injected agent widen it silently
    "Read(~/.config/qa-sdet/config.json)",
]


def merge_permissions(path, stamp=None):
    """Add missing PERMISSION_RULES to permissions.allow in a Claude Code settings file. Keeps every other key and the
    existing order, backs the file up first, writes atomically with its mode. Returns the rules added."""
    import tempfile
    path = Path(os.path.realpath(Path(path).expanduser()))  # a symlinked settings.json (dotfiles) stays a link
    def no_constant(c):  # NaN / Infinity: Python reads them, Claude Code can't
        raise ValueError(f"non-standard number {c}")
    try:
        before = path.read_text(encoding="utf-8") if path.exists() else None
        settings = json.loads(before, parse_constant=no_constant) if before is not None else {}
    except (OSError, ValueError, RecursionError) as e:  # RecursionError: JSON nested thousands deep
        raise core.BadInput(f"setup-permissions: {path} isn't valid JSON ({type(e).__name__}: {core.short(str(e), 120)}); "
                       f"fix it first, nothing changed")
    perms = settings.get("permissions", {}) if isinstance(settings, dict) else None
    allow = perms.get("allow", []) if isinstance(perms, dict) else None
    if not isinstance(allow, list):
        raise core.BadInput(f"setup-permissions: {path} has no usable permissions.allow list; nothing changed")
    same = lambda r: re.sub(r"\s*([()])\s*", r"\1", " ".join(str(r).split()))  # `Bash( qa-mem *)` is that rule too
    have = {same(r) for r in allow}
    added = [r for r in PERMISSION_RULES if same(r) not in have]
    if not added:
        return []
    settings["permissions"] = {**perms, "allow": allow + added}
    try:  # serialise first: nothing is touched when this fails (a lone surrogate can't be written as UTF-8)
        text = json.dumps(settings, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        text.encode("utf-8")
    except (ValueError, UnicodeEncodeError) as e:
        raise core.BadInput(f"setup-permissions: {path} can't be written back as valid JSON ({e}); nothing changed")
    if before is not None and not os.access(path, os.W_OK):
        raise core.BadInput(f"setup-permissions: {path} is read-only; make it writable or add the rules by hand, "
                       f"nothing changed")
    mode = path.stat().st_mode & 0o7777 if before is not None else 0o600
    tmp = None
    try:
        if before is not None:
            import shutil
            base = f"{path.name}.bak-{stamp or datetime.now().strftime('%Y%m%d%H%M%S')}"
            bak, n = path.with_name(base), 2
            while bak.exists() or bak.is_symlink():  # two applies in one second: never overwrite the first backup
                bak, n = path.with_name(f"{base}-{n}"), n + 1
            shutil.copy2(path, bak)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        # Claude Code may have saved the file meanwhile (/permissions, /config): never drop its edit
        now_text = path.read_text(encoding="utf-8") if path.exists() else None
        if now_text != before:
            raise core.BadInput(f"setup-permissions: {path} changed while merging; run it again, nothing changed")
        os.replace(tmp, path)
    except OSError as e:
        raise core.BadInput(f"setup-permissions: can't write {path} ({e}); nothing changed")
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)
    return added
