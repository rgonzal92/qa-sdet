"""`qa-mem doctor`, browser detection and `qa-mem setup-permissions`."""
import builtins, contextlib, io, json, os, stat, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock

from support import Base, cli, cli_mod, done, core
from qa_sdet import doctor, ops


class Doctor(Base):
    """`qa-mem doctor`: config and tool checks, each with a FIX line."""

    def doctor_out(self):
        out = io.StringIO()
        with mock.patch("shutil.which", lambda *a, **k: None), contextlib.redirect_stdout(out):
            doctor.doctor()
        return out.getvalue()

    def test_doctor_reports_config_problems(self):
        import io, contextlib
        cfg = Path(self.tmp.name) / "config.json"
        cfg.write_text(json.dumps({"app": "x", "qaBaseUrl": "https://qa.example.com", "allowedHosts": "qa.example.com"}))
        real = core.CONFIG
        core.CONFIG = cfg
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                problems = doctor.doctor()
        finally:
            core.CONFIG = real
        out = buf.getvalue()
        self.assertGreater(problems, 0)
        self.assertIn("FIX  allowedHosts is a list", out)
        self.assertIn("FIX  config has app", out)

    def test_doctor_empty_config_and_source_folders(self):
        import io, contextlib, shutil
        cfg = Path(self.tmp.name) / "config.json"
        real, real_which = core.CONFIG, shutil.which
        core.CONFIG = cfg
        shutil.which = lambda name, *a, **k: None  # no playwright-cli, no acli
        try:
            for body, want in (({}, "FIX  config has app"),
                               ({"sourcePaths": "/x"}, "FIX  sourcePaths is a list"),
                               ({"sourcePaths": [str(Path(self.tmp.name) / "gone")]}, "not found")):
                cfg.write_text(json.dumps(body))
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    doctor.doctor()
                self.assertIn(want, buf.getvalue(), body)
            self.assertIn("FIX  playwright-cli on PATH  -> npm i -g @playwright/cli", buf.getvalue())
            self.assertIn("info no acli: tickets are pasted", buf.getvalue())
        finally:
            core.CONFIG, shutil.which = real, real_which
        self.assertTrue(doctor.pillow_fix())

    def test_doctor_acli_signed_in_or_out(self):
        import io, contextlib, shutil, subprocess
        real_which, real_run = shutil.which, subprocess.run
        shutil.which = lambda name, *a, **k: "/x/acli" if name == "acli" else None
        try:
            for code, want in ((1, "FIX  acli signed in to Jira  -> acli jira auth login"), (0, "ok   acli signed in to Jira")):
                subprocess.run = lambda cmd, *a, code=code, **k: type("R", (), {"returncode": code, "stdout": ""})()
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    doctor.doctor()
                self.assertIn(want, buf.getvalue())
        finally:
            shutil.which, subprocess.run = real_which, real_run

    def test_odd_config_types_still_name_the_db_and_pass_the_host_check(self):
        import io, contextlib
        cfg = Path(self.tmp.name) / "config.json"
        cfg.write_text(json.dumps({"app": 123, "qaBaseUrl": "https://qa.Example.com", "ssoHost": "sso",
                                   "buildSource": {}, "allowedHosts": [8080, "*.example.com"]}))
        real, real_data = core.CONFIG, core.DATA_DIR
        core.CONFIG, core.DATA_DIR = cfg, Path(self.tmp.name) / "data"  # never touch the real data dir
        os.environ.pop("QA_MEM_DB")
        try:
            self.assertTrue(str(core.db_path()).endswith("123.db"))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                doctor.doctor()
            self.assertIn("ok   qaBaseUrl host is in allowedHosts", buf.getvalue())
        finally:
            core.CONFIG, core.DATA_DIR = real, real_data
            os.environ["QA_MEM_DB"] = str(Path(self.tmp.name) / "t.db")

    def test_doctor_agrees_with_the_guard_and_stops_at_a_json_error(self):
        good = dict(app="x", ssoHost="login.x.com", buildSource={"type": "selector", "value": "footer"})
        for cfg in (dict(qaBaseUrl="https://qa_app.example.com", allowedHosts=["qa_app.example.com"]),
                    dict(qaBaseUrl="http://127.0.0.1:8765", allowedHosts=["localhost"])):
            self.config(**good, **cfg)
            out = self.doctor_out()
            self.assertNotIn("FIX  allowedHosts", out, cfg)
            self.assertNotIn("FIX  qaBaseUrl", out, cfg)
        core.CONFIG.write_text('{"app": "x", "qaBaseUrl": "https://qa.example.com",}')
        out = self.doctor_out()
        self.assertIn("JSONDecodeError", out)
        self.assertNotIn("config has app", out)
        self.assertNotIn("FIX  buildSource", out)

    def test_allowed_hosts_with_scheme_or_port_and_sso_host_with_port(self):
        good = dict(app="x", qaBaseUrl="https://qa.example.com:8443/app", allowedHosts=["qa.example.com", "*.api.example.com"],
                    ssoHost="login.x.com", buildSource={"type": "selector", "value": "footer"})
        self.config(**good)
        self.assertNotIn("FIX  allowedHosts", self.doctor_out())
        for bad, want in [(dict(allowedHosts=["https://qa.example.com/"]), "FIX  allowedHosts entries are host names (https://qa.example.com/"),
                          (dict(allowedHosts=["qa.example.com:8443"]), "FIX  allowedHosts entries are host names (qa.example.com:8443"),
                          (dict(ssoHost="qa.example.com:8443"), "FIX  ssoHost differs from the qaBaseUrl host"),
                          (dict(buildSource={"type": "selector", "value": ["x"]}), "FIX  buildSource is")]:
            self.config(**{**good, **bad})
            self.assertIn(want, self.doctor_out(), bad)

    def test_odd_config_values_get_a_fix_line_not_a_crash(self):
        good = dict(app="x", qaBaseUrl="https://qa.example.com", allowedHosts=["qa.example.com"], ssoHost="login.x.com",
                    buildSource={"type": "selector", "value": "footer"})
        self.config(**good, sourcePaths=5)  # was: TypeError, no len()
        self.assertNotIn("source folders configured", self.doctor_out())
        for bad, want in [(dict(qaBaseUrl="qa.example.com"), 'FIX  qaBaseUrl is a full URL  -> e.g. "qaBaseUrl"'),
                          (dict(qaBaseUrl=5), "FIX  qaBaseUrl is a full URL"),
                          (dict(ssoHost="https://login.x.com/"), "FIX  ssoHost is a host name  -> just the host"),
                          (dict(ssoHost={"x": 1}), "FIX  ssoHost is a host name"),
                          (dict(jiraKeyPrefix=["PROJ"]), "FIX  jiraKeyPrefix is a Jira project key"),
                          (dict(jiraKeyPrefix="PROJ-1234"), "FIX  jiraKeyPrefix is a Jira project key")]:
            self.config(**{**good, **bad})
            out = self.doctor_out()
            self.assertIn(want, out, bad)
            self.assertNotIn("add the qaBaseUrl host to allowedHosts", out, bad)  # the wrong fix for a bad URL
        self.config(**good, jiraKeyPrefix="proj-")
        self.assertIn("ok   jiraKeyPrefix is a Jira project key", self.doctor_out())
        with self.assertRaisesRegex(core.BadInput, "jiraKeyPrefix .* must be a Jira project key"):
            ops.expand_keys(["1234"], "PROJ-1234")
        core.CONFIG.write_text("[1]")
        self.assertIn("must be one JSON object", self.doctor_out())
        core.CONFIG.write_text("{bad")
        self.assertIn("-> fix the JSON (", self.doctor_out())

    def test_profile_pillow_and_old_cookie_lines(self):
        self.config(app="x")
        core.CONFIG.with_name("qa-state.json").write_text("{}")
        with mock.patch("shutil.which", lambda *a, **k: None):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                doctor.doctor()
            self.assertIn("run /qa-sdet:qa-login", out.getvalue())
            self.assertIn(f"FIX  old cookie file {core.CONFIG.with_name('qa-state.json')} (nothing uses it any more)  -> delete "
                          f"{core.CONFIG.with_name('qa-state.json')} yourself", out.getvalue())
            self.assertIn("Pillow", out.getvalue())
            ops.ensure_profile_dir()  # browser-open made the folder, but nobody signed in: still FIX
            (ops.profile_dir() / "Default").mkdir()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                doctor.doctor()
            self.assertIn("run /qa-sdet:qa-login", out.getvalue())
            self.assertNotIn("signed-in profile present", out.getvalue())
            self.config(app="x", browser="chrome", qaBaseUrl="https://qa.example.com", allowedHosts=["qa.example.com"],
                        ssoHost="login.example.com")  # qa-login verifies the sign-in and records it in one call
            seq = [done(0), done(0), done(0, out='{"host":"qa.example.com","hasPassword":false}'), done(0)]
            with mock.patch.object(subprocess, "run", side_effect=seq):
                self.assertEqual(cli("session-check", "--record")[0], 0)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                doctor.doctor()
            self.assertIn("ok   signed-in profile present, from /qa-sdet:qa-login (expires with the app's session)",
                          out.getvalue())
            self.assertEqual(cli("login-ok")[0], 2)  # gone: it recorded a sign-in without checking one
            with mock.patch.object(subprocess, "run", return_value=done(0)):
                self.config(app="x", browser="chrome")
                cli_mod.main(["browser-open"])  # a test run keeps the sign-in
                self.assertTrue(ops.signed_in_stamp().exists())
                cli_mod.main(["browser-open", "--session", "qa-login", "--headed"])  # a new sign-in starts: forget it
                self.assertFalse(ops.signed_in_stamp().exists())

    @unittest.skipIf(os.geteuid() == 0, "root ignores permissions")
    def test_unreadable_folders_never_crash_and_cookie_file_is_never_read(self):
        self.config(app="x")
        core.CONFIG.with_name("qa-state.json").write_text("SECRET-COOKIE")
        real_open = builtins.open
        def no_cookie(f, *a, **k):
            if "qa-state" in str(f):
                raise AssertionError("doctor opened the cookie file")
            return real_open(f, *a, **k)
        with mock.patch("builtins.open", no_cookie):
            self.assertIn("FIX  old cookie file", self.doctor_out())
        for folder in (core.CONFIG.parent, core.DATA_DIR):  # Path.exists/is_file raised here on python < 3.12
            folder.mkdir(parents=True, exist_ok=True)
            os.chmod(folder, 0)
            try:
                out = self.doctor_out()
            finally:
                os.chmod(folder, 0o700)
            self.assertIn("python", out)
        self.assertIn("make it readable: chmod", (os.chmod(core.CONFIG.parent, 0), self.doctor_out(),
                                                  os.chmod(core.CONFIG.parent, 0o700))[1])

    def test_doctor_validates_jira_ac_field_and_inbox_jql(self):
        good = dict(app="x", qaBaseUrl="https://qa.example.com", allowedHosts=["qa.example.com"],
                    ssoHost="login.x.com", buildSource={"type": "selector", "value": "footer"})
        self.config(**good, jiraAcField="customfield_10042", inboxJql='status = "Ready for QA" ORDER BY updated DESC')
        out = self.doctor_out()
        self.assertIn("ok   jiraAcField is a Jira custom field id", out)
        self.assertIn("ok   inboxJql is a single-line JQL query", out)
        for bad, want in [(dict(jiraAcField="bad field"), "FIX  jiraAcField"),
                          (dict(jiraAcField=123), "FIX  jiraAcField"),
                          (dict(inboxJql="a\nb"), "FIX  inboxJql"),
                          (dict(inboxJql=["x"]), "FIX  inboxJql")]:
            self.config(**{**good, **bad})
            self.assertIn(want, self.doctor_out(), bad)


class Browsers(Base):
    """Which browsers are installed, and the chosen one."""

    def test_browser_choice_detection_and_doctor(self):
        """Runs use the browser the user chose from what's installed (config `browser`), never an assumed Firefox."""
        import io, contextlib, shutil, subprocess
        tmp = Path(self.tmp.name)
        chrome = tmp / "chrome"; chrome.write_text("")
        build = tmp / "webkit-1"; build.mkdir(); (build / "INSTALLATION_COMPLETE").write_text("")
        real_isfile, real_which, real_run = os.path.isfile, shutil.which, subprocess.run
        os.path.isfile = lambda x: x == "/opt/google/chrome/chrome" or (str(x).endswith("INSTALLATION_COMPLETE") and real_isfile(x))  # only the stub's browsers, not this machine's
        shutil.which = lambda name, *a, **k: "/x/playwright-cli" if name == "playwright-cli" else None
        def fake_run(cmd, *a, **k):
            out = f"Install location: {build if 'webkit' in cmd else tmp / 'firefox-missing'}\n" if "install-browser" in cmd else ""
            return type("R", (), {"returncode": 0, "stdout": out})()
        subprocess.run = fake_run
        try:
            got = doctor.installed_browsers()
            self.assertEqual({k: v[0] for k, v in got.items()}, {"chrome": True, "msedge": False, "firefox": False, "webkit": True})
            self.assertIn("playwright-cli install-browser firefox", got["firefox"][1])
            cfg = tmp / "config.json"
            real_cfg = core.CONFIG
            core.CONFIG = cfg
            try:
                for conf, want in (({}, "FIX  config has browser"), ({"browser": "opera"}, "FIX  config browser 'opera'"),
                                   ({"browser": "webkit"}, "ok   browser webkit installed"),
                                   ({"browser": "firefox"}, "FIX  browser firefox installed  -> playwright-cli install-browser firefox")):
                    cfg.write_text(json.dumps(conf))
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        doctor.doctor()
                    self.assertIn(want, buf.getvalue(), conf)
                    self.assertNotIn("Firefox installed", buf.getvalue())
            finally:
                core.CONFIG = real_cfg
        finally:
            os.path.isfile, shutil.which, subprocess.run = real_isfile, real_which, real_run

    def test_browser_detection_edge_cases(self):
        """Review findings: interrupted downloads, failed lookups, the browsers command."""
        import io, contextlib, shutil, subprocess
        tmp = Path(self.tmp.name)
        half = tmp / "firefox-9"; half.mkdir()  # no INSTALLATION_COMPLETE: an interrupted download
        real_isfile, real_which, real_run = os.path.isfile, shutil.which, subprocess.run
        def runner(mode):
            def fake(cmd, *a, **k):
                if mode == "timeout" and "install-browser" in cmd:  # only the lookup fails; --version works
                    raise subprocess.TimeoutExpired(cmd, 30)
                out = f"Install location: {half}\n" if mode == "half" else "nothing useful\n"
                return type("R", (), {"returncode": 0, "stdout": out})()
            return fake
        shutil.which = lambda name, *a, **k: "/x/playwright-cli" if name == "playwright-cli" else None
        try:
            for mode, want in (("half", "playwright-cli install-browser firefox"), ("timeout", "can't check: `playwright-cli"),
                               ("noloc", "gave no install location")):
                subprocess.run = runner(mode)
                ok, how = doctor.installed_browsers(("firefox",))["firefox"]
                self.assertFalse(ok, mode); self.assertIn(want, how, mode)
            os.path.isfile = lambda x: x == "/opt/microsoft/msedge/msedge"
            got = doctor.installed_browsers(("chrome", "msedge"))
            self.assertEqual((got["chrome"][0], got["msedge"]), (False, (True, "/opt/microsoft/msedge/msedge")))
            os.path.isfile = real_isfile
            subprocess.run = runner("timeout")
            cfg, real_cfg = tmp / "config.json", core.CONFIG
            core.CONFIG = cfg
            try:
                cfg.write_text(json.dumps({"browser": "firefox"}))
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    doctor.doctor()
                out = buf.getvalue()
                self.assertIn("info browser firefox: can't check", out)  # a failed lookup isn't a FIX
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(cli_mod.main(["browsers"]), 0)
                self.assertRegex(buf.getvalue(), r"(?m)^chrome +(installed|missing)")
            finally:
                core.CONFIG = real_cfg
        finally:
            os.path.isfile, shutil.which, subprocess.run = real_isfile, real_which, real_run


class SetupPermissions(Base):
    """`qa-mem setup-permissions`: print or merge the allow rules into Claude Code's settings."""

    def test_print(self):
        code, out, _ = cli("setup-permissions")
        self.assertEqual((code, out.splitlines()), (0, doctor.PERMISSION_RULES))
        self.assertFalse((self.t / "home/.claude").exists())  # printing changes nothing

    def test_merge_deep_json_and_same_second_backups(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "settings.json"
            f.write_text("[" * 200000 + "]" * 200000)
            with self.assertRaises(core.BadInput):
                doctor.merge_permissions(f)
            f.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
            doctor.merge_permissions(f, stamp="20261123000000")
            f.write_text(json.dumps({"permissions": {"allow": []}}))
            doctor.merge_permissions(f, stamp="20261123000000")
            self.assertEqual(len(list(Path(d).glob("settings.json.bak-20261123000000*"))), 2)  # first backup kept

    def test_merge_keeps_keys_no_dupes_backup_mode(self):
        s = self.t / "settings.json"
        s.write_text(json.dumps({"model": "x", "permissions": {"allow": ["Bash(ls)", "Skill(qa-sdet:*)"],
                                                                 "deny": ["Bash(rm *)"]}, "hooks": {"a": 1}}))
        os.chmod(s, 0o640)
        code, out, _ = cli("setup-permissions", "--settings", str(s), "--apply")
        self.assertEqual(code, 0)
        got = json.loads(s.read_text())
        self.assertEqual(got["model"], "x")
        self.assertEqual(got["hooks"], {"a": 1})
        self.assertEqual(got["permissions"]["deny"], ["Bash(rm *)"])
        allow = got["permissions"]["allow"]
        self.assertEqual(allow[:2], ["Bash(ls)", "Skill(qa-sdet:*)"])
        self.assertEqual(sorted(allow), sorted(set(allow)))
        self.assertEqual(set(allow), {"Bash(ls)"} | set(doctor.PERMISSION_RULES))
        self.assertNotIn("Skill(qa-sdet:*)", out)  # only what was added
        self.assertIn("Bash(qa-mem *)", out)
        self.assertFalse(any(r.startswith("Agent(") or "python" in r for r in doctor.PERMISSION_RULES))  # auto mode drops them
        # the config read every run is allowed, but never Edit/Write (allowedHosts is the guard's trust root)
        self.assertIn("Read(~/.config/qa-sdet/config.json)", doctor.PERMISSION_RULES)
        self.assertFalse(any(r.startswith(("Edit(", "Write(", "MultiEdit("))
                             for r in doctor.PERMISSION_RULES))
        self.assertEqual(stat.S_IMODE(s.stat().st_mode), 0o640)
        backups = list(self.t.glob("settings.json.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertRegex(backups[0].name, r"\.bak-\d{14}$")
        self.assertEqual(json.loads(backups[0].read_text())["permissions"]["allow"], ["Bash(ls)", "Skill(qa-sdet:*)"])
        code, out, _ = cli("setup-permissions", "--settings", str(s), "--apply")
        self.assertIn("nothing changed", out)
        self.assertEqual(json.loads(s.read_text())["permissions"]["allow"], allow)

    def test_missing_file_and_invalid_json(self):
        s = self.t / "home/.claude/settings.json"
        self.assertEqual(cli("setup-permissions", "--apply")[0], 0)  # default path under HOME
        self.assertEqual(json.loads(s.read_text()), {"permissions": {"allow": doctor.PERMISSION_RULES}})
        bad = self.t / "bad.json"
        bad.write_text('{"permissions": ')
        code, _, err = cli("setup-permissions", "--settings", str(bad), "--apply")
        self.assertEqual(code, 2)
        self.assertIn("isn't valid JSON", err)
        self.assertEqual(bad.read_text(), '{"permissions": ')
        self.assertEqual(list(self.t.glob("bad.json*")), [bad])  # no backup, no temp file
        bad.write_text('{"permissions": {"allow": "Bash(ls)"}}')
        self.assertEqual(cli("setup-permissions", "--settings", str(bad), "--apply")[0], 2)
        for text in ('{"x": NaN}', '{"x": -Infinity}', '{"x": 1e999}', '{"x": "\\ud800"}'):  # Claude Code can't read these back
            bad.write_text(text)
            code, _, err = cli("setup-permissions", "--settings", str(bad), "--apply")
            self.assertEqual(code, 2, text)
            self.assertIn("nothing changed", err)
            self.assertEqual(bad.read_text(), text)
            self.assertEqual(sorted(p.name for p in self.t.iterdir() if p.name.startswith((".bad", "bad"))), ["bad.json"])

    def test_unwritable_folder_changes_nothing(self):
        d = self.t / "cfg"
        d.mkdir()
        s = d / "settings.json"
        s.write_text("{}")
        os.chmod(d, 0o555)
        try:
            code, _, err = cli("setup-permissions", "--settings", str(s), "--apply")
        finally:
            os.chmod(d, 0o755)
        self.assertEqual((code, s.read_text()), (2, "{}"))
        self.assertIn("can't write", err)
        self.assertEqual([p.name for p in d.iterdir()], ["settings.json"])  # no backup, no temp file

    def test_empty_settings_path_is_an_error(self):
        self.assertEqual(cli("setup-permissions", "--apply", "--settings=")[0], 2)
        self.assertEqual(cli("setup-permissions", "--apply", "--settings", "")[0], 2)
        self.assertFalse((self.t / "home" / ".claude").exists())

    def test_round2_doctor_dangling_backup_prefix_and_decoding(self):
        # M6: dangling backup symlink is never written through
        s = self.t / "settings.json"
        s.write_text("{}")
        victim = self.t / "dangling-victim"
        (self.t / "settings.json.bak-20261124000000").symlink_to(victim)
        doctor.merge_permissions(s, stamp="20261124000000")
        self.assertFalse(victim.exists())
        # L4: non-ASCII jiraKeyPrefix 'ß' fails doctor check (matching ops.expand_keys)
        good = dict(app="x", qaBaseUrl="https://qa.example.com", allowedHosts=["qa.example.com"],
                    ssoHost="login.x.com", buildSource={"type": "selector", "value": "footer"})
        self.config(**good, jiraKeyPrefix="ß")
        out = io.StringIO()
        with mock.patch("shutil.which", lambda *a, **k: None), contextlib.redirect_stdout(out):
            doctor.doctor()
        self.assertIn("FIX  jiraKeyPrefix is a Jira project key", out.getvalue())
        # L6: installed_browsers passes errors='replace' so non-UTF-8 output never crashes
        with mock.patch("shutil.which", return_value="/x/playwright-cli"), \
                mock.patch("subprocess.run", return_value=done(0, out="")) as mrun:
            doctor.installed_browsers(("firefox",))
        self.assertEqual(mrun.call_args.kwargs.get("errors"), "replace")


if __name__ == "__main__":
    unittest.main()
