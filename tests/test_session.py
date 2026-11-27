"""The `qa` browser session (qa_sdet.session): `browser-open`, `session-check` and `page-start`."""

import json
import os
import stat
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from support import SESSION_CFG, Base, cli, cli_mod, core, done

from qa_sdet import session


class BrowserOpen(Base):
    """`qa-mem browser-open`: playwright-cli on the qa-sdet profile."""

    def test_command_env_and_profile(self):
        self.config(browser="msedge")
        with (
            mock.patch.object(subprocess, "run", return_value=done(3)) as run,
            mock.patch.dict(os.environ, PLAYWRIGHT_MCP_STORAGE_STATE="/tmp/s.json", PLAYWRIGHT_MCP_ISOLATED="true"),
        ):
            code = cli_mod.main(["browser-open", "--session", "qa-login", "--headed", "https://qa.example/"])
        self.assertEqual(code, 3)  # playwright-cli's exit code
        profile = core.DATA_DIR / "profiles" / "main"
        argv, kw = run.call_args
        self.assertEqual(
            argv[0],
            [
                "playwright-cli",
                "-s=qa-login",
                "open",
                "--browser=msedge",
                "--profile",
                str(profile),
                "--headed",
                "https://qa.example/",
            ],
        )
        self.assertNotIn("PLAYWRIGHT_MCP_STORAGE_STATE", kw["env"])
        self.assertNotIn("PLAYWRIGHT_MCP_ISOLATED", kw["env"])  # made the profile launch fail live
        close = run.call_args_list[0][0][0]
        self.assertEqual(close, ["playwright-cli", "-s=qa", "close"])  # the other session on profiles/main stops
        self.assertEqual(run.call_count, 2)
        for d in (profile, profile.parent):
            self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)
        with mock.patch.object(subprocess, "run", return_value=done(0)) as run:
            self.assertEqual(cli_mod.main(["browser-open"]), 0)
        self.assertEqual(
            run.call_args[0][0], ["playwright-cli", "-s=qa", "open", "--browser=msedge", "--profile", str(profile)]
        )
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
            (
                "### Error\nbrowserType.launchPersistentContext: Chromium distribution 'chrome' is not found at "
                "/opt/google/chrome/chrome\n",
                "",
                1,
                "browser chrome can't start: browserType.launchPersistentContext: Chromium distribution 'chrome' is not found",
            ),
            (
                "",
                "Error: Executable doesn't exist at /x/ms-playwright/chrome-1/chrome\n",
                1,
                "browser chrome can't start: Error: Executable doesn't exist",
            ),
            ("", "Error: daemon is not responding\n", 1, "browser tool failed: Error: daemon is not responding"),
            ("", "", 7, "browser tool failed: playwright-cli exited 7"),
        ):
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


class SessionCheck(Base):
    """`qa-mem session-check`: headless SSO check, optional --record and --build."""

    CFG = SESSION_CFG

    def test_valid_session_record_and_build(self):
        self.config(**self.CFG)
        # browser_open (close qa-login, open qa) -> eval host/form -> eval build -> close qa
        seq = [
            done(0),
            done(0, out="opened"),
            done(0, out='{"host":"qa.example.com","hasPassword":false}\n'),
            done(0, out="1.43.0\n"),
            done(0),
        ]
        with mock.patch.object(subprocess, "run", side_effect=seq) as mrun:
            code, out, err = cli("session-check", "--record", "--build")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("ok", out)
        self.assertIn("build=1.43.0", out)
        self.assertTrue(session.signed_in_stamp().exists())
        self.assertEqual(mrun.call_args_list[-1][0][0], ["playwright-cli", "-s=qa", "close"])

    def test_expired_sso_host_or_password_form_returns_1_without_stamp(self):
        self.config(**self.CFG)
        for payload in (
            '{"host":"login.microsoftonline.com","hasPassword":false}',
            '{"host":"LOGIN.MICROSOFTONLINE.COM:443","hasPassword":false}',
            '{"host":"qa.example.com","hasPassword":true}',
        ):
            session.signed_in_stamp().unlink(missing_ok=True)
            seq = [done(0), done(0), done(0, out=payload), done(0)]
            with mock.patch.object(subprocess, "run", side_effect=seq) as mrun:
                code, out, err = cli("session-check", "--record")
            self.assertEqual(code, 1, payload)
            self.assertIn("session expired", err)
            self.assertFalse(session.signed_in_stamp().exists())
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

    CFG = SESSION_CFG

    def start(self, seq, url="https://qa.example.com/app/form"):
        with mock.patch.object(subprocess, "run", side_effect=seq) as mrun:
            code, out, err = cli("page-start", url)
        return code, (json.loads(out) if out.strip() else None), mrun

    def test_open_signed_in_page_reads_the_build_and_stays_open(self):
        self.config(**self.CFG)
        code, out, mrun = self.start(
            [
                done(0),
                done(0, out="opened"),
                done(0, out='{"host":"qa.example.com","hasPassword":false}'),
                done(0, out="1.43.0\n"),
            ]
        )
        self.assertEqual(
            (code, out),
            (0, {"status": "ok", "build": "1.43.0", "url": "https://qa.example.com/app/form", "browser": "chrome"}),
        )
        self.assertNotIn(["playwright-cli", "-s=qa", "close"], [c[0][0] for c in mrun.call_args_list])

    def test_without_build_source_the_build_from_qa_mem_stale_is_used(self):  # no browser-readable build
        cfg = {k: v for k, v in self.CFG.items() if k != "buildSource"}
        self.config(**cfg)
        signed_in = [done(0), done(0, out="opened"), done(0, out='{"host":"qa.example.com","hasPassword":false}')]
        db = Path(os.environ["QA_MEM_DB"])
        code, out, _ = self.start(list(signed_in))
        self.assertEqual((code, out["build"]), (0, "unknown"))
        self.assertFalse(db.exists())  # reading the build never creates the DB
        self.assertEqual(cli("stale", "2.14.0")[0], 0)
        code, out, _ = self.start(list(signed_in))
        self.assertEqual((code, out["build"]), (0, "2.14.0"))
        with mock.patch.object(subprocess, "run", side_effect=[*signed_in, done(0)]):
            code, out, _ = cli("session-check", "--build")
        self.assertEqual(code, 0)
        self.assertIn("build=2.14.0", out)

    def test_a_configured_build_source_is_not_masked_by_the_stored_build(self):  # a broken buildSource stays visible
        self.config(**self.CFG)
        self.assertEqual(cli("stale", "2.14.0")[0], 0)
        signed_in = [done(0), done(0, out="opened"), done(0, out='{"host":"qa.example.com","hasPassword":false}')]
        code, out, _ = self.start([*signed_in, done(0, out="1.43.0\n")])
        self.assertEqual((code, out["build"]), (0, "1.43.0"))
        code, out, _ = self.start([*signed_in, done(1, err="boom")])
        self.assertEqual((code, out["build"]), (0, "unknown"))

    def test_a_build_source_that_cant_be_read_leaves_a_notice_in_the_plugin_log(self):
        """The run goes on with build `unknown`; the notice keeps why, so a broken buildSource gets fixed."""
        self.config(**self.CFG)
        (self.proj / "qa-out").mkdir()
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.proj)
        log = self.proj / "qa-out/plugin-log.jsonl"
        notices = lambda: [r for r in map(json.loads, log.read_text().splitlines()) if r["type"] == "notice"]
        signed_in = [done(0), done(0, out="opened"), done(0, out='{"host":"qa.example.com","hasPassword":false}')]
        self.start([*signed_in, done(0, out="1.43.0\n")])
        self.assertFalse(log.exists())  # read: nothing to say
        err = "Error: no element at https://qa.example.com/x?token=s3cr3tvalue " + "x" * 300 + "\n    at stack line"
        code, out, _ = self.start([*signed_in, done(1, err=err)])
        self.assertEqual((code, out["build"]), (0, "unknown"))
        (n,) = notices()
        self.assertEqual((n["kind"], n["source"]), ("build_unreadable", "selector"))
        self.assertTrue(n["detail"].startswith("Error: no element at"))
        self.assertLessEqual(len(n["detail"]), 200)
        for leak in ("s3cr3tvalue", "stack line"):
            self.assertNotIn(leak, n["detail"])
        self.start([*signed_in, done(0, out="\n")])  # read, but empty: the selector found nothing
        self.assertEqual(notices()[-1]["kind"], "build_unreadable")
        self.assertIn("empty", notices()[-1]["detail"])
        with mock.patch.object(subprocess, "run", side_effect=[*signed_in, subprocess.TimeoutExpired("x", 20)]):
            cli("page-start", "https://qa.example.com/app/form")
        self.assertEqual(len(notices()), 3)

    def test_a_list_of_build_sources_is_one_build_that_any_part_changes(self):  # a shell and its own modules
        sources = [
            {"type": "selector", "value": "footer .version"},
            {"type": "url", "value": "/modules/schedules/version.txt"},
        ]
        self.config(**{**self.CFG, "buildSource": sources})
        signed_in = [done(0), done(0, out="opened"), done(0, out='{"host":"qa.example.com","hasPassword":false}')]
        code, out, mrun = self.start([*signed_in, done(0, out="1.43.0\n"), done(0, out="mod-1.0\n")])
        self.assertEqual((code, out["build"]), (0, "1.43.0+mod-1.0"))
        evals = [c[0][0][-1] for c in mrun.call_args_list if "eval" in c[0][0]][1:]  # after the sign-in check
        self.assertEqual(["footer .version" in evals[0], "version.txt" in evals[1]], [True, True])
        code, out, _ = self.start([*signed_in, done(0, out="1.43.0\n"), done(0, out="mod-1.1\n")])
        self.assertEqual(out["build"], "1.43.0+mod-1.1")  # the module was deployed alone: a new build
        long = "window.__version = " + "x" * 80
        code, out, _ = self.start([*signed_in, done(0, out="1.43.0\n"), done(0, out=long)])
        self.assertRegex(out["build"], r"^1\.43\.0\+[0-9a-f]{10}$")  # a long part goes in as its digest
        for unreadable in (done(0, out="\n"), done(1, err="boom")):  # a part that can't be read: no build is claimed
            code, out, _ = self.start([*signed_in, done(0, out="1.43.0\n"), unreadable])
            self.assertEqual((code, out["build"]), (0, "unknown"))
        self.config(**{**self.CFG, "buildSource": [sources[0], {"type": "nope", "value": "x"}]})
        code, out, mrun = self.start(list(signed_in))
        self.assertEqual(out["build"], "unknown")  # a source that can't be read as configured: none is read

    def test_sign_in_page_is_a_session_blocker_and_closes(self):
        self.config(**self.CFG)
        code, out, mrun = self.start(
            [done(0), done(0), done(0, out='{"host":"login.microsoftonline.com","hasPassword":false}'), done(0)]
        )
        self.assertEqual((code, out["status"], out["block_scope"], out["browser"]), (1, "blocked", "session", "chrome"))
        self.assertIn("/qa-sdet:qa-login", out["reason"])
        self.assertEqual(mrun.call_args_list[-1][0][0], ["playwright-cli", "-s=qa", "close"])

    def test_an_error_status_is_a_page_blocker_that_leaves_the_browser_open(self):  # the explorer's step 9 closes it
        self.config(**self.CFG)
        code, out, mrun = self.start(
            [done(0), done(0), done(0, out='{"host":"qa.example.com","hasPassword":false,"status":404}')]
        )
        self.assertEqual(
            (code, out),
            (
                1,
                {
                    "status": "blocked",
                    "block_scope": "page",
                    "reason": "the page returned HTTP 404",
                    "browser": "chrome",
                },
            ),
        )  # a crawl's run file names the browser from here
        self.assertNotIn(["playwright-cli", "-s=qa", "close"], [c[0][0] for c in mrun.call_args_list])
        code, out, _ = self.start(
            [
                done(0),
                done(0),
                done(0, out='{"host":"qa.example.com","hasPassword":false,"status":0}'),
                done(0, out="1.43.0\n"),
            ]
        )  # 0: the browser doesn't expose it, so no blocker
        self.assertEqual((code, out["status"]), (0, "ok"))

    def test_browser_trouble_is_a_browser_blocker_and_bad_hosts_are_refused(self):
        self.config(**self.CFG)
        code, out, _ = self.start([done(0), done(0, out="Error: Browser is already in use for /x/profiles/main")])
        self.assertEqual((code, out["block_scope"]), (1, "browser"))
        self.assertTrue(out["reason"].startswith("browser profile in use"), out)
        code, out, mrun = self.start([], url="https://evil.example.com/")
        self.assertEqual((code, out), (2, None))
        mrun.assert_not_called()


if __name__ == "__main__":
    unittest.main()
