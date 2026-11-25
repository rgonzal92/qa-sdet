"""qa-mem operations: keys, rundir, the run marker, browser-open and prune."""
import json, os, stat, subprocess, time, unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

from support import Base, cli, cli_mod, done, core, ROOT
from qa_sdet import ops


class Keys(Base):
    """`qa-mem keys`: ticket numbers and keys become full Jira keys."""

    def test_expansion_validation_and_order(self):
        self.assertEqual(ops.expand_keys(["1234", "ops-7,1250", "PROJ-1234"], "proj-"),
                         ["PROJ-1234", "OPS-7", "PROJ-1250"])
        self.assertEqual(ops.expand_keys(["1 2,3"], "PROJ"), ["PROJ-1", "PROJ-2", "PROJ-3"])
        for bad in ("abc", "PROJ-", "1-PROJ", "PROJ-12a", "S-1", "ß-12", "ﬀ-1"):  # ß.upper() is SS
            with self.assertRaises(core.BadInput) as e:
                ops.expand_keys([bad], "PROJ")
            self.assertIn(repr(bad), str(e.exception))
        with self.assertRaisesRegex(core.BadInput, "jiraKeyPrefix"):
            ops.expand_keys(["1234"], None)
        self.assertEqual(ops.expand_keys(["OPS-7"], None), ["OPS-7"])  # full keys need no prefix

    def test_cli(self):
        self.config(jiraKeyPrefix="PROJ")
        code, out, _ = cli("keys", "1234", "proj-1250")
        self.assertEqual((code, json.loads(out)), (0, ["PROJ-1234", "PROJ-1250"]))
        code, out, err = cli("keys", "12;rm")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("12;rm", err)
        self.assertEqual(cli("keys")[0], 2)
        self.config()
        self.assertIn("set jiraKeyPrefix", cli("keys", "5")[2])

    def test_only_ascii_digits(self):  # OPS-١٢٣ was accepted and would go to Jira as is
        for bad in ("OPS-\u0661\u0662", "\u0661\u0662"):
            with self.assertRaises(core.BadInput):
                ops.expand_keys([bad], "OPS")


class RunDir(Base):
    """`qa-mem rundir`: the qa-out/<date>-<label> folder of one run."""

    def test_dot_leading_last_segment_keeps_its_name(self):
        self.assertEqual([ops.route_slug(r) for r in ("/.well-known", "/form.html", "/", "/a/b.c.d")],
                         [".well-known", "form", "home", "a-b.c"])

    def test_labels(self):
        self.assertEqual(ops.run_label(["A-1", "B-2"]), "A-1+B-2")
        self.assertEqual(ops.run_label(["A-1", "A-2", "A-3", "A-4", "A-5"]), "A-1+A-2+A-3+2more")
        for route, slug in (("/form.html", "form"), ("/incidents/list", "incidents-list"), ("/incidents/4521", "incidents-id"),
                            ("/#/zones", "zones"), ("/#!/zones", "zones"), ("/", "home"), ("/a?x=1", "a")):
            self.assertEqual(ops.run_label(route=route), slug, route)
        self.assertEqual(ops.run_label(keys=["A-1"], prefix="plan-"), "plan-A-1")
        self.assertEqual(ops.run_label(label="init crawl/../x"), "init-crawl-..-x")

    def test_folders_suffix_and_git_exclude(self):
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True)
        exclude = self.proj / ".git/info/exclude"
        exclude.write_text("# mine\nqa-out")  # one entry already there, no trailing newline
        code, out, _ = cli("rundir", str(self.proj), "--keys", "AB-1,ab-2")
        run = Path(out.strip())
        self.assertEqual(code, 0)
        self.assertTrue(run.is_absolute())
        self.assertRegex(run.name, r"^\d{4}-\d{2}-\d{2}-AB-1\+AB-2$")
        self.assertEqual(run.parent, self.proj.resolve() / "qa-out")
        self.assertEqual(sorted(p.name for p in run.iterdir()), [".data", "bugs", "evidence", "repro"])
        self.assertEqual(stat.S_IMODE(run.stat().st_mode), 0o700)
        second = Path(cli("rundir", str(self.proj), "--keys", "AB-1,ab-2")[1].strip())
        third = Path(cli("rundir", str(self.proj), "--keys", "AB-1,ab-2")[1].strip())
        self.assertEqual((second.name, third.name), (run.name + "-2", run.name + "-3"))
        lines = exclude.read_text().splitlines()
        self.assertEqual(lines, ["# mine", "qa-out", ".playwright-cli/"])  # added once, no dupes

    def test_non_git_and_bad_input(self):
        code, out, _ = cli("rundir", str(self.proj), "--route", "/form.html", "--prefix", "plan-")
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().endswith("-plan-form"))
        self.assertEqual(cli("rundir", str(self.proj))[0], 2)  # no label source
        self.assertEqual(cli("rundir", str(self.proj), "--keys", "AB-1", "--route", "/x")[0], 2)
        self.assertEqual(cli("rundir", str(self.t / "nope"), "--label", "x")[0], 2)
        self.assertEqual(cli("rundir", str(self.proj), "--keys", "bad key")[0], 2)

    def test_long_label_and_repeated_flag(self):  # a 300-char label crashed (name too long); --keys twice kept one
        code, out, err = cli("rundir", str(self.proj), "--label", "speed limit form " * 20)
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(Path(out.strip()).is_dir())
        self.assertLessEqual(len(ops.run_label(route="/" + "a/" * 200, prefix="plan-")), 80)
        code, _, err = cli("rundir", str(self.proj), "--keys", "AB-1", "--keys", "AB-2")
        self.assertEqual(code, 2)
        self.assertIn("--keys given twice", err)


class Marker(Base):
    """`qa-mem marker`: the run marker the guard reads."""

    def test_start_unlimited_and_limited_then_stop(self):
        code, out, _ = cli("marker", "start", str(self.proj))
        m = json.loads((self.proj / "qa-out/.active").read_text())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), m)
        self.assertEqual((m["budget"], m["deadline"], m["faults"], m["used"]), (0, 0, False, 0))
        self.assertAlmostEqual(m["expires"], time.time() + 8 * 3600, delta=5)
        cli("marker", "start", str(self.proj), "--budget", "40", "--minutes", "10", "--faults")
        m = json.loads((self.proj / "qa-out/.active").read_text())
        self.assertEqual((m["budget"], m["faults"]), (40, True))
        self.assertAlmostEqual(m["deadline"], time.time() + 600, delta=5)
        self.assertEqual([p.name for p in (self.proj / "qa-out").iterdir()], [".active"])  # no temp file left
        (self.proj / "qa-out/.active.lock").write_text("")
        self.assertEqual(cli("marker", "stop", str(self.proj))[0], 0)
        self.assertEqual(list((self.proj / "qa-out").iterdir()), [])
        self.assertEqual(cli("marker", "stop", str(self.proj))[0], 0)  # already gone: fine

    def test_bad_input(self):
        for argv in (("marker",), ("marker", "go", str(self.proj)), ("marker", "start", str(self.proj), "--budget", "-1"),
                     ("marker", "start", str(self.proj), "--budget", "x"), ("marker", "start", str(self.proj), "--budget", ""), ("marker", "start", str(self.proj), "--minutes", ""), ("marker", "start", str(self.proj), "--bogus")):
            self.assertEqual(cli(*argv)[0], 2, argv)
        self.assertFalse((self.proj / "qa-out/.active").exists())

    def test_marker_the_guard_accepts(self):  # --minutes 5000 wrote a deadline the guard reads as malformed
        guard = SourceFileLoader("guard_r7", str(ROOT / "hooks/guard.py")).load_module()
        cli("marker", "start", str(self.proj), "--minutes", "480", "--budget", "0")
        with mock.patch.dict(os.environ, CLAUDE_PROJECT_DIR=""):
            path, marker = guard.read_marker(str(self.proj))
        self.assertEqual(marker["budget"], 0)  # read as a run (a malformed one would be no run at all)
        code, _, err = cli("marker", "start", str(self.proj), "--minutes", "5000")
        self.assertEqual(code, 2)
        self.assertIn("at most 480", err)
        self.assertEqual(guard.spend_budget(path, 3), None)  # the earlier marker is untouched and still spendable

    def test_stop_on_a_missing_project_is_an_error(self):  # `marker stop /typo` said "marker removed"
        code, out, err = cli("marker", "stop", str(self.t / "nope"))
        self.assertEqual(code, 2)
        self.assertNotIn("marker removed", out)

    def test_missing_project_is_not_created(self):  # marker start /typo made /typo/qa-out
        self.assertEqual(cli("marker", "start", str(self.t / "nope" / "proj"))[0], 2)
        self.assertFalse((self.t / "nope").exists())


class BrowserOpen(Base):
    """`qa-mem browser-open`: playwright-cli on the qa-sdet profile."""

    def test_command_env_and_profile(self):
        self.config(browser="msedge")
        with mock.patch.object(subprocess, "run", return_value=done(3)) as run, \
                mock.patch.dict(os.environ, PLAYWRIGHT_MCP_STORAGE_STATE="/tmp/s.json", PLAYWRIGHT_MCP_ISOLATED="true"):
            code = cli_mod.main(["browser-open", "--session", "qa-login", "--headed", "https://qa.example/"])
        self.assertEqual(code, 3)  # playwright-cli's exit code
        profile = core.DATA_DIR / "profiles" / "main"
        argv, kw = run.call_args
        self.assertEqual(argv[0], ["playwright-cli", "-s=qa-login", "open", "--browser=msedge", "--profile", str(profile),
                                   "--headed", "https://qa.example/"])
        self.assertNotIn("PLAYWRIGHT_MCP_STORAGE_STATE", kw["env"])
        self.assertNotIn("PLAYWRIGHT_MCP_ISOLATED", kw["env"])  # made the profile launch fail live
        close = run.call_args_list[0][0][0]
        self.assertEqual(close, ["playwright-cli", "-s=qa", "close"])  # the other session on profiles/main stops
        self.assertEqual(run.call_count, 2)
        for d in (profile, profile.parent):
            self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)
        with mock.patch.object(subprocess, "run", return_value=done(0)) as run:
            self.assertEqual(cli_mod.main(["browser-open"]), 0)
        self.assertEqual(run.call_args[0][0], ["playwright-cli", "-s=qa", "open", "--browser=msedge", "--profile", str(profile)])
        self.assertEqual(run.call_args_list[0][0][0], ["playwright-cli", "-s=qa-login", "close"])

    def test_profile_in_use_says_close_the_other_browser(self):  # playwright-cli's hint was --isolated (no login)
        self.config(browser="chrome")
        msg = "Error: Browser is already in use for /x/profiles/main, use --isolated to run multiple instances"
        with mock.patch.object(subprocess, "run", side_effect=[done(0), done(0, out=msg)]):
            code, out, err = cli("browser-open", "https://qa.example/")
        self.assertEqual(code, 1)  # an error even if playwright-cli exited 0
        self.assertTrue(err.startswith("browser profile in use: another qa-sdet browser holds the main profile"), err)
        self.assertIn("Close the other qa-sdet browser", err)
        self.assertIn(msg, out)  # the original output is still shown
        with mock.patch.object(subprocess, "run", side_effect=[OSError("gone"), done(0, out="opened")]):
            self.assertEqual(cli("browser-open")[:2], (0, "opened"))  # a failing close never blocks the open

    def test_errors_start_with_their_category(self):  # the explorer and qa-login report these three as they are
        self.config(browser="chrome")
        for out, err, code, first in (
                ("### Error\nbrowserType.launchPersistentContext: Chromium distribution 'chrome' is not found at "
                 "/opt/google/chrome/chrome\n", "", 1,
                 "browser chrome can't start: browserType.launchPersistentContext: Chromium distribution 'chrome' is not found"),
                ("", "Error: Executable doesn't exist at /x/ms-playwright/chrome-1/chrome\n", 1,
                 "browser chrome can't start: Error: Executable doesn't exist"),
                ("", "Error: daemon is not responding\n", 1, "browser tool failed: Error: daemon is not responding"),
                ("", "", 7, "browser tool failed: playwright-cli exited 7")):
            with mock.patch.object(subprocess, "run", side_effect=[done(0), done(code, out=out, err=err)]):
                got, _, e = cli("browser-open", "https://qa.example/")
            self.assertEqual(got, code)
            self.assertTrue(e.startswith(first), (e, first))
            self.assertTrue(e.splitlines()[0].startswith(first), e)  # the first line, not just somewhere
            self.assertIn(err.strip(), e)  # playwright-cli's own text follows the category line
        with mock.patch.object(subprocess, "run", side_effect=[done(0), OSError("gone")]):
            code, _, e = cli("browser-open")
        self.assertEqual(code, 2)
        self.assertTrue(e.startswith("browser tool failed: can't run playwright-cli (gone)"), e)
        with mock.patch.object(subprocess, "run", side_effect=[done(0), done(0, out="opened", err="warning: x\n")]):
            self.assertEqual(cli("browser-open"), (0, "opened", "warning: x\n"))  # success: no category line

    def test_refusals(self):
        with mock.patch.object(subprocess, "run") as run:
            self.config(browser="safari")
            self.assertEqual(cli("browser-open", "https://x")[0], 2)
            self.config(browser="chrome")
            self.assertEqual(cli("browser-open", "--session", "other")[0], 2)
            self.assertEqual(cli("browser-open", "--profile", "/tmp/x")[0], 2)
            self.assertEqual(cli("browser-open", "a", "b")[0], 2)
        run.assert_not_called()

    def test_config_errors_say_where_and_signal_exit(self):  # "got None" named no file; -15 became exit 241
        with mock.patch.object(subprocess, "run") as run:
            core.CONFIG.unlink(missing_ok=True)
            code, _, err = cli("browser-open")
            self.assertEqual(code, 2)
            self.assertIn(f"{core.CONFIG} is missing", err)
            core.CONFIG.parent.mkdir(parents=True, exist_ok=True)
            core.CONFIG.write_text("{bad")
            self.assertIn("isn't valid JSON", cli("browser-open")[2])
            self.config(browser="opera")
            self.assertIn(f'set "browser" in {core.CONFIG}', cli("browser-open")[2])
        run.assert_not_called()
        self.config(browser="chrome")
        with mock.patch.object(subprocess, "run", return_value=done(-15)):
            self.assertEqual(cli_mod.main(["browser-open"]), 143)


class Prune(Base):
    """`qa-mem prune`: old snapshot files and a run's temp files."""

    def test_old_regular_files_only(self):
        cli_dir = self.proj / ".playwright-cli"
        cli_dir.mkdir()
        old = time.time() - 8 * 86400
        (cli_dir / "old.yml").write_text("x")
        os.utime(cli_dir / "old.yml", (old, old))
        (cli_dir / "new.yml").write_text("x")
        outside = self.t / "outside.txt"
        outside.write_text("keep")
        os.utime(outside, (old, old))
        (cli_dir / "link").symlink_to(outside)
        os.utime(cli_dir / "link", (old, old), follow_symlinks=False)
        code, out, _ = cli("prune", str(self.proj))
        self.assertEqual((code, out.strip()), (0, "pruned 1 old snapshot files"))
        self.assertEqual(sorted(p.name for p in cli_dir.iterdir()), ["link", "new.yml"])
        self.assertTrue(outside.exists())
        self.assertEqual(cli("prune", str(self.t / "empty"))[0], 0)  # nothing there: fine
        # with the run folder: this run's temp files go, what the report needs stays
        d = self.proj / "qa-out" / "2026-11-24-r"
        for rel in (".data/plan.json", ".data/run-form.json", ".data/snapshot.md", ".data/map-1.png", "evidence/TC-1.png",
                    "bugs/BUG-1.md", "repro/BUG-1.spec.ts", "REPORT.md"):
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text("x")
        start = time.time() - 600
        os.utime(d / ".data/plan.json", (start, start))  # the run began here
        before = time.time() - 3600
        os.utime(cli_dir / "new.yml", (before, before))  # a snapshot from an earlier run
        (cli_dir / "during.yml").write_text("x")
        code, out, _ = cli("prune", str(self.proj), str(d))
        self.assertEqual((code, out.strip()), (0, "pruned 3 snapshot and temp files of this run"))
        self.assertEqual(sorted(p.name for p in cli_dir.iterdir()), ["link", "new.yml"])
        self.assertEqual(sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()),
                         [".data/plan.json", ".data/run-form.json", "REPORT.md", "bugs/BUG-1.md", "evidence/TC-1.png",
                          "repro/BUG-1.spec.ts"])
        self.assertEqual(cli("prune", str(self.proj), str(self.t))[0], 2)  # not a run folder
        # a transient temp file disappearing between os.walk and os.lstat must not crash prune
        real_lstat = os.lstat
        vanished = d / ".data/tmp.scrub"
        vanished.write_text("x")
        def disappearing(p, *a, **k):
            if str(p) == str(vanished):
                raise FileNotFoundError(str(p))
            return real_lstat(p, *a, **k)
        with mock.patch.object(os, "lstat", side_effect=disappearing):
            self.assertEqual(ops.prune(self.proj, run=d), 0)

    def test_round2_ops_browser_open_prune_and_annotate(self):
        # M3: page title containing 'already in use' on exit 0 must not trigger profile-in-use error
        self.config(browser="chrome")
        ok_out = "### Page\n- Page Title: Port already in use\n"
        with mock.patch.object(subprocess, "run", side_effect=[done(0), done(0, out=ok_out)]):
            code, out, err = cli("browser-open", "https://qa.example/")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Port already in use", out)
        # L3: empty crashed run folder still prunes snapshots created since folder creation
        cli_dir = self.proj / ".playwright-cli"
        cli_dir.mkdir(exist_ok=True)
        old_snap = cli_dir / "before.yml"
        old_snap.write_text("old")
        t0 = time.time() - 3600
        os.utime(old_snap, (t0, t0))
        empty_run = self.proj / "qa-out/2026-11-24-crashed"
        empty_run.mkdir(parents=True)
        os.utime(empty_run, (time.time() - 60, time.time() - 60))
        (cli_dir / "after.yml").write_text("new")
        self.assertEqual(ops.prune(self.proj, run=empty_run), 1)
        self.assertTrue(old_snap.exists())
        self.assertFalse((cli_dir / "after.yml").exists())
        # L6: Pillow ValueError (e.g. huge zTXt chunk) becomes BadInput, not uncaught traceback
        from PIL import Image
        png = empty_run / "evidence/BUG-1.png"
        png.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (20, 20)).save(png)
        with mock.patch.object(Image, "open", side_effect=ValueError("Decompressed data too large")):
            with self.assertRaises(core.BadInput):
                ops.annotate(png, [(0, 0, 10, 10)], "x")


class SessionCheck(Base):
    """`qa-mem session-check`: headless SSO check, optional --record and --build."""

    CFG = dict(app="x", browser="chrome", qaBaseUrl="https://qa.example.com/app",
               allowedHosts=["qa.example.com"], ssoHost="login.microsoftonline.com",
               buildSource={"type": "selector", "value": "footer .version"})

    def test_valid_session_record_and_build(self):
        self.config(**self.CFG)
        # browser_open (close qa-login, open qa) -> eval host/form -> eval build -> close qa
        seq = [done(0), done(0, out="opened"),
               done(0, out='{"host":"qa.example.com","hasPassword":false}\n'),
               done(0, out="1.43.0\n"), done(0)]
        with mock.patch.object(subprocess, "run", side_effect=seq) as mrun:
            code, out, err = cli("session-check", "--record", "--build")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("ok", out)
        self.assertIn("build=1.43.0", out)
        self.assertTrue(ops.signed_in_stamp().exists())
        self.assertEqual(mrun.call_args_list[-1][0][0], ["playwright-cli", "-s=qa", "close"])

    def test_expired_sso_host_or_password_form_returns_1_without_stamp(self):
        self.config(**self.CFG)
        for payload in ('{"host":"login.microsoftonline.com","hasPassword":false}',
                        '{"host":"LOGIN.MICROSOFTONLINE.COM:443","hasPassword":false}',
                        '{"host":"qa.example.com","hasPassword":true}'):
            ops.signed_in_stamp().unlink(missing_ok=True)
            seq = [done(0), done(0), done(0, out=payload), done(0)]
            with mock.patch.object(subprocess, "run", side_effect=seq) as mrun:
                code, out, err = cli("session-check", "--record")
            self.assertEqual(code, 1, payload)
            self.assertIn("session expired", err)
            self.assertFalse(ops.signed_in_stamp().exists())
            self.assertEqual(mrun.call_args_list[-1][0][0], ["playwright-cli", "-s=qa", "close"])

    def test_error_paths_profile_in_use_non_allowlisted_and_close_on_eval_error(self):
        self.config(**{**self.CFG, "allowedHosts": ["other.example.com"]})
        with mock.patch.object(subprocess, "run") as mrun:
            code, _, err = cli("session-check")
        self.assertEqual(code, 2)
        self.assertIn("allowedHosts", err)
        mrun.assert_not_called()

        self.config(**self.CFG)
        in_use = "Error: Browser is already in use for /x/profiles/main"
        with mock.patch.object(subprocess, "run", side_effect=[done(0), done(0, out=in_use)]):
            code, _, err = cli("session-check")
        self.assertEqual(code, 2)
        self.assertIn("browser profile in use", err)

        # Eval failure still closes the browser and returns 2
        with mock.patch.object(subprocess, "run", side_effect=[done(0), done(0), done(1, err="boom"), done(0)]) as mrun:
            code, _, err = cli("session-check")
        self.assertEqual(code, 2)
        self.assertEqual(mrun.call_args_list[-1][0][0], ["playwright-cli", "-s=qa", "close"])


class PageStart(Base):
    """`qa-mem page-start <url>`: the explorer's steps 1-3 in one call (open, sign-in check, build); the browser stays open."""

    CFG = SessionCheck.CFG

    def start(self, seq, url="https://qa.example.com/app/form"):
        with mock.patch.object(subprocess, "run", side_effect=seq) as mrun:
            code, out, err = cli("page-start", url)
        return code, (json.loads(out) if out.strip() else None), mrun

    def test_open_signed_in_page_reads_the_build_and_stays_open(self):
        self.config(**self.CFG)
        code, out, mrun = self.start([done(0), done(0, out="opened"), done(0, out='{"host":"qa.example.com","hasPassword":false}'),
                                      done(0, out="1.43.0\n")])
        self.assertEqual((code, out), (0, {"status": "ok", "build": "1.43.0", "url": "https://qa.example.com/app/form"}))
        self.assertNotIn(["playwright-cli", "-s=qa", "close"], [c[0][0] for c in mrun.call_args_list])

    def test_sign_in_page_is_a_session_blocker_and_closes(self):
        self.config(**self.CFG)
        code, out, mrun = self.start([done(0), done(0), done(0, out='{"host":"login.microsoftonline.com","hasPassword":false}'),
                                      done(0)])
        self.assertEqual((code, out["status"], out["block_scope"]), (1, "blocked", "session"))
        self.assertIn("/qa-sdet:qa-login", out["reason"])
        self.assertEqual(mrun.call_args_list[-1][0][0], ["playwright-cli", "-s=qa", "close"])

    def test_browser_trouble_is_a_browser_blocker_and_bad_hosts_are_refused(self):
        self.config(**self.CFG)
        code, out, _ = self.start([done(0), done(0, out="Error: Browser is already in use for /x/profiles/main")])
        self.assertEqual((code, out["block_scope"]), (1, "browser"))
        self.assertTrue(out["reason"].startswith("browser profile in use"), out)
        code, out, mrun = self.start([], url="https://evil.example.com/")
        self.assertEqual((code, out), (2, None))
        mrun.assert_not_called()


class Evidence(Base):
    """`qa-mem evidence <png> (--selector <css> | --ref <ref>) --label <text>`: the evidence image in one call."""

    def setUp(self):
        super().setUp()
        self.png = self.proj / "qa-out/r/evidence/TC-1.png"
        self.png.parent.mkdir(parents=True)

    def run_evidence(self, *args, outputs=(), annotate=True):
        calls, outs = [], list(outputs)
        def fake(cmd, **kw):
            calls.append(cmd)
            if "screenshot" in cmd:
                self.png.write_bytes(b"png")
            return outs.pop(0) if outs else done(0)
        with mock.patch.object(subprocess, "run", side_effect=fake), \
                mock.patch.object(ops, "annotate", return_value=annotate) as ann:
            code, out, err = cli("evidence", str(self.png), *args)
        return code, (json.loads(out) if out.strip() else None), calls, ann, err

    def test_selector_box_is_drawn_with_pillow(self):
        sel = 'a[title="it\'s"]'
        code, out, calls, ann, _ = self.run_evidence("--selector", sel, "--label", "TC-1 PASS: 130 saved",
                                                     outputs=[done(0, out='"10,20,30,40"\n')])
        self.assertEqual((code, out), (0, {"path": str(self.png), "annotated": True, "method": "pillow"}))
        self.assertIn(json.dumps(sel), calls[0][-1])  # the selector reaches the page as a JS string, never as code
        self.assertEqual(calls[1], ["playwright-cli", "-s=qa", "screenshot", f"--filename={self.png}"])
        self.assertEqual(ann.call_args[0][1:], ([(10, 20, 30, 40)], "TC-1 PASS: 130 saved"))

    def test_without_pillow_or_with_a_ref_the_browser_draws_the_box_and_always_hides_it(self):
        code, out, calls, _, _ = self.run_evidence("--selector", "#msg", "--label", "x", outputs=[done(0, out="1,2,3,4")],
                                                   annotate=False)
        self.assertEqual((code, out["method"], out["annotated"]), (0, "highlight", True))
        self.assertEqual(calls[-1], ["playwright-cli", "-s=qa", "highlight", "--hide"])
        self.assertIn(["playwright-cli", "-s=qa", "highlight", "#msg", "--style", "outline: 3px solid red"], calls)
        code, out, calls, _, err = self.run_evidence("--ref", "f1e15", "--label", "x",
                                                     outputs=[done(0), done(0), done(1, err="screenshot failed")])
        self.assertEqual(code, 1)
        self.assertIn("screenshot failed", err)
        self.assertEqual(calls[-1], ["playwright-cli", "-s=qa", "highlight", "--hide"])  # no red box left for the next image

    def test_refusals(self):
        code, _, calls, _, err = self.run_evidence("--selector", "#none", "--label", "x", outputs=[done(0, out='""')])
        self.assertEqual(code, 1)
        self.assertIn("no element matches", err)
        outside = self.proj / "qa-out/r/bugs/x.png"
        for args in (["--selector", "#a"], ["--selector", "#a", "--ref", "e1", "--label", "x"]):
            self.assertEqual(cli("evidence", str(self.png), *args)[0], 2, args)
        self.assertEqual(cli("evidence", str(outside), "--selector", "#a", "--label", "x")[0], 2)


if __name__ == "__main__":
    unittest.main()
