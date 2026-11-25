"""Browser checks, `doctor` and the `setup-permissions` rules."""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from . import core, ops


def installed_browsers(names=core.BROWSERS):
    """{name: (installed, where or how to install)}. chrome and msedge are the system browsers (Playwright's
    channel paths); firefox and webkit are Playwright's own builds: a system Firefox can't be driven."""
    system = {"chrome": ("Google Chrome", "/opt/google/chrome/chrome"), "msedge": ("Microsoft Edge", "/opt/microsoft/msedge/msedge")}
    pw, out = shutil.which("playwright-cli"), {}
    for name in names:
        if name in system:
            label, path = system[name]
            ok = os.path.isfile(path)
            out[name] = (ok, path if ok else f"install {label} the usual way for this system")
            continue
        loc, why = None, "can't check: playwright-cli isn't working (npm i -g @playwright/cli), then install-browser " + name
        if pw:
            why = f"can't check: `playwright-cli install-browser --dry-run {name}` gave no install location"
            try:
                dry = subprocess.run([pw, "install-browser", "--dry-run", name], capture_output=True, text=True,
                                     errors="replace", timeout=30).stdout
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
    """The install command that works here: pip refuses an OS-managed python3 (PEP 668, Ubuntu), so name the apt package."""
    if not os.path.exists(os.path.join(sysconfig.get_paths()["stdlib"], "EXTERNALLY-MANAGED")):
        return f"{sys.executable} -m pip install --user pillow"
    return "sudo apt-get install python3-pil (this python3 is OS-managed, so pip refuses)"


def doctor():
    """Setup check for a new machine. Prints one line per item; returns the number of problems.
    Subprocess timeouts sum to under 2 minutes (15 + 30 + 30 s) so a skill's Bash call never cuts it off."""
    problems = 0

    def line(ok, what, fix=""):
        nonlocal problems
        problems += not ok
        print(f"{'ok  ' if ok else 'FIX '} {what}" + (f"  -> {fix}" if fix and not ok else ""))

    line(sys.version_info >= (3, 9), f"python {sys.version.split()[0]}", "install Python 3.9+ as python3 (hooks need it)")
    cfg, cfg_err = {}, None
    try:
        cfg = core.config(strict=True)
    except (OSError, ValueError) as e:
        cfg_err = e
    chosen = cfg.get("browser")
    pw = shutil.which("playwright-cli")
    on_path = bool(pw)
    if pw:  # on PATH isn't enough: a broken install must be fixed too
        try:
            pw = pw if subprocess.run([pw, "--version"], capture_output=True, timeout=15).returncode == 0 else None
        except (subprocess.TimeoutExpired, OSError):
            pw = None
    line(bool(pw), "playwright-cli runs (--version)" if on_path else "playwright-cli on PATH",
         "reinstall: npm i -g @playwright/cli" if on_path else "npm i -g @playwright/cli")
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
    acli = shutil.which("acli")
    if acli:
        try:
            ok = subprocess.run([acli, "jira", "auth", "status"], capture_output=True, encoding="utf-8",
                                errors="replace", timeout=30).returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            ok = False
        line(ok, "acli signed in to Jira", "acli jira auth login")
    else:
        print("info no acli: tickets are pasted (optional: install acli)")
    if cfg_err:
        line(False, f"config {core.CONFIG} ({cfg_err.__class__.__name__})",
             f"fix the JSON ({cfg_err})" if isinstance(cfg_err, ValueError) else
             f"make it readable: chmod u+rwx {core.CONFIG.parent}; chmod u+rw {core.CONFIG}" if isinstance(cfg_err, PermissionError) else
             "run any qa-sdet skill once; it asks for the values")
    else:
        line(True, f"config {core.CONFIG}")
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
                      if isinstance(h, str) and h.strip()] if isinstance(hosts, list) else []  # same rules as the guard
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
            line(isinstance(prefix, str) and prefix.isascii() and bool(re.fullmatch(
                 r"[A-Z][A-Z0-9]+", prefix.strip().upper().rstrip("-"), re.ASCII)),
                 "jiraKeyPrefix is a Jira project key", 'the letters before the ticket number: e.g. "jiraKeyPrefix": "PROJ"')
        temps = cfg.get("releaseTemplates")
        if temps not in (None, []):  # optional: the Jira pass-comment draft fills {version} into these
            ok_t = isinstance(temps, list) and all(isinstance(t, str) and "{version}" in t for t in temps)
            line(ok_t, "releaseTemplates lists release names with {version}",
                 'a list such as ["Release-A-{version}"]: the pass-comment draft fills in the fix version')
        ac_field = cfg.get("jiraAcField")
        if ac_field not in (None, ""):
            line(isinstance(ac_field, str) and bool(re.fullmatch(r"customfield_\d+", ac_field.strip())),
                 "jiraAcField is a Jira custom field id", 'e.g. "jiraAcField": "customfield_10042"')
        jql = cfg.get("inboxJql")
        if jql not in (None, ""):
            line(isinstance(jql, str) and bool(jql.strip()) and "\n" not in jql and "\r" not in jql,
                 "inboxJql is a single-line JQL query", 'e.g. "inboxJql": "status = \\"Ready for QA\\" ORDER BY updated DESC"')
    try:
        import PIL
        line(True, f"Pillow {PIL.__version__}")
    except ImportError:
        line(False, "Pillow (annotated bug images need it)", pillow_fix())
    if os.path.isfile(ops.signed_in_stamp()) and os.path.isdir(ops.profile_dir()):
        line(True, "signed-in profile present, from /qa-sdet:qa-login (expires with the app's session)")
    else:
        line(False, "signed-in browser profile", "run /qa-sdet:qa-login")
    old_state = core.CONFIG.with_name("qa-state.json")  # never opened: it holds cookies
    if os.path.lexists(old_state):
        line(False, f"old cookie file {old_state} (nothing uses it any more)", f"delete {old_state} yourself")
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
    except core.BadInput as e:  # a DB from before 0.4
        line(False, f"memory DB {core.db_path()}", str(e))
    except (OSError, sqlite3.Error) as e:
        line(False, f"memory DB {core.db_path()} ({e})", "use a python3 whose sqlite3 has FTS5 (the distro's python3 normally does)"
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
    "Skill(qa-sdet:*)",
    # every skill/explorer reads it (hosts/URLs, no secrets). Read only: allowedHosts is the guard's trust root, so an
    # auto-allowed Edit/Write would let a prompt-injected agent widen it silently
    "Read(~/.config/qa-sdet/config.json)",
]


def merge_permissions(path, stamp=None):
    """Append the missing PERMISSION_RULES to permissions.allow of a Claude Code settings file; every other key stays.
    Backs the file up first and writes atomically. A file that isn't usable JSON is refused untouched.
    Returns the rules added."""
    if (expanded := os.path.expanduser(str(path))).startswith("~"):
        raise core.BadInput(f"can't find the home folder in {path}; give the full path")
    path = Path(os.path.realpath(expanded))  # a symlinked settings.json (dotfiles) stays a link
    try:
        old = path.read_text(encoding="utf-8") if path.exists() else None
        settings = json.loads(old) if old is not None else {}
        allow = settings.get("permissions", {}).get("allow", [])
        if not isinstance(allow, list):
            raise ValueError("permissions.allow is not a list")
        added = [r for r in PERMISSION_RULES if r not in allow]
        if not added:
            return []
        settings.setdefault("permissions", {})["allow"] = allow + added
        data = (json.dumps(settings, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    except (OSError, ValueError, AttributeError, RecursionError) as e:  # not JSON, not an object, no allow list, too deep
        raise core.BadInput(f"setup-permissions: {path} isn't valid JSON with a permissions.allow list "
                            f"({type(e).__name__}: {core.short(str(e), 120)}); fix it first, nothing changed")
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if old is not None:
            bak = path.with_name(f"{path.name}.bak-{stamp or datetime.now().strftime('%Y%m%d%H%M%S')}")
            while os.path.lexists(bak):  # two applies in one second, or a planted dangling symlink: never write through
                bak = bak.with_name(bak.name + "x")
            bak_fd = os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_EXCL, path.stat().st_mode & 0o7777)
            with os.fdopen(bak_fd, "wb") as bf:
                bf.write(old.encode("utf-8"))
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, path.stat().st_mode & 0o7777 if old is not None else 0o600)
        os.replace(tmp, path)
    except OSError as e:
        if tmp:
            Path(tmp).unlink(missing_ok=True)
        raise core.BadInput(f"setup-permissions: can't write {path} ({e}); nothing changed")
    return added
