"""The qa-mem command line: usage, error messages, and the bin/qa-mem launcher."""

import json, os, sqlite3, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

from support import Base, cli, cli_mod, core, ROOT


class Cli(Base):
    """Usage, help and the error messages of the commands."""

    def test_every_command_answers_help_and_bad_options_with_usage(self):
        code, out, _ = cli()
        self.assertEqual(code, 2)
        for name in cli_mod.COMMANDS:
            self.assertIn(f"  qa-mem {name}", out)
        self.assertEqual(cli("--help")[0], 2)
        self.assertEqual(cli("nope")[0], 2)
        for name in cli_mod.COMMANDS:
            for args in (["--help"], ["-h"], ["--bogus"], ["x", "--help"]):
                code, out, err = cli(name, *args)
                self.assertEqual(code, 2, (name, args, out, err))
                self.assertIn(f"usage: qa-mem {name}", err + out, (name, args))
        self.assertEqual(cli("stale", "--help")[0], 2)
        con = core.connect()
        self.addCleanup(con.close)
        self.assertIsNone(core.meta(con, "build"))  # was: the build became "--help"
        self.assertEqual(cli("doctor", "now")[0], 2)
        self.assertEqual(cli("hash", "/form", "shows", "--", "instead")[0], 0)  # a symptom may hold "--"
        self.assertEqual(cli("hash", "/form", "-h")[0], 0)
        self.assertEqual(cli("rundir", str(self.proj), "--label", "--flag-test")[0], 0)
        self.assertEqual(cli("rundir", str(self.proj), "--label", "-h")[0], 0)
        self.assertEqual(cli("code", "--repos")[0], 0)

    def test_ingest_of_a_bad_file_is_a_message(self):
        code, _, err = cli("ingest", str(self.t / "missing.json"))
        self.assertEqual(code, 2)
        self.assertIn("can't save", err)
        (self.t / "r.json").write_text("{}")
        code, _, err = cli("ingest", str(self.t / "r.json"))
        self.assertEqual((code, "run_id" in err), (2, True))

    def test_busy_db_is_a_message(self):
        with mock.patch.object(cli_mod, "run_cmd", side_effect=sqlite3.OperationalError("database is locked")):
            code, _, err = cli("stale", "1.2")
        self.assertEqual(code, 1)
        self.assertIn("stayed busy", err)

    def test_round2_cli_option_validation_and_stale_norm(self):
        # H5: codemap-scan rejects empty --out, dangling --out, --full=false
        for bad in (
            ("codemap-scan", "--out", "", str(self.proj)),
            ("codemap-scan", "--out=", str(self.proj)),
            ("codemap-scan", str(self.proj), "--out"),
            ("codemap-scan", "--full=false", str(self.proj)),
        ):
            self.assertEqual(cli(*bad)[0], 2, bad)
        # M4: stale uses norm_build and rejects n/a, none, unknown (...)
        for unk in ("n/a", "none", "undefined", "unknown (missing)"):
            self.assertEqual(cli("stale", unk)[0], 2, unk)
        # L6: marker stop rejects flags, code rejects --repos=1 / --repos + route, bugs/ingest reject extra args
        for bad in (
            ("marker", "stop", str(self.proj), "--budget", "10"),
            ("marker", "stop", str(self.proj), "--faults"),
            ("marker", "stop", str(self.proj), "--api-writes"),
            ("code", "--repos=1"),
            ("code", "--repos", "/form"),
            ("bugs", "OPEN", "extra"),
            ("ingest", "a.json", "b.json"),
        ):
            self.assertEqual(cli(*bad)[0], 2, bad)
        # L5: hook_input catches RecursionError on deeply nested stdin
        import io

        deep = io.StringIO("[" * 3000 + "]" * 3000)
        deep.isatty = lambda: False
        with mock.patch.object(sys, "stdin", deep), mock.patch("select.select", return_value=([deep], [], [])):
            self.assertEqual(cli_mod.hook_input(), {})


class BrokenDb(Base):
    def test_a_db_sqlite_cannot_open_is_one_line_not_a_traceback(self):
        Path(os.environ["QA_MEM_DB"]).write_bytes(b"not a database at all" * 100)
        code, _, err = cli("card", "/x")
        self.assertEqual(code, 1)
        self.assertIn("qa-mem:", err)
        self.assertNotIn("Traceback", err)

    def test_a_data_folder_that_cannot_be_made_is_one_line(self):
        ro = self.t / "ro"
        ro.mkdir(mode=0o500)
        os.environ["QA_MEM_DB"] = str(ro / "sub" / "m.db")
        try:
            code, _, err = cli("card", "/x")
        finally:
            ro.chmod(0o700)
        self.assertEqual(code, 2, err)
        self.assertIn("qa-mem:", err)

    def test_ingest_and_settings_take_odd_input(self):
        f = self.t / "run.json"
        f.write_text(
            "\ufeff" + json.dumps({"run_id": "r1", "build": "1", "pages": [{"route": "/a"}]}), encoding="utf-8"
        )
        self.assertEqual(cli("ingest", str(f))[0], 0)  # a BOM, as Windows editors write it
        code, _, err = cli("setup-permissions", "--settings", "~nosuchuser9/settings.json", "--apply")
        self.assertEqual(code, 2, err)


class Launcher(unittest.TestCase):
    """bin/qa-mem finds lib/qa_sdet however it is started: isolated sys.path, through a symlink, from any folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {
            **os.environ,
            "QA_SDET_DATA": str(Path(self.tmp.name) / "data"),
            "QA_SDET_CONFIG": str(Path(self.tmp.name) / "cfg/config.json"),
        }
        for leaked in ("PYTHONPATH", "QA_MEM_DB"):  # a developer's shell may set them
            self.env.pop(leaked, None)

    def where(self, launcher, cwd, env=None, flags=()):
        r = subprocess.run(
            [sys.executable, *flags, str(launcher), "where"],
            cwd=cwd,
            env=env or self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(Path(r.stdout.strip()).parent, Path(self.tmp.name) / "data")

    def test_launcher_runs_with_a_safe_path(self):
        self.where(ROOT / "bin/qa-mem", self.tmp.name, {**self.env, "PYTHONSAFEPATH": "1"})
        self.where(ROOT / "bin/qa-mem", self.tmp.name, flags=("-I",))

    def test_launcher_runs_through_a_symlink(self):
        link = Path(self.tmp.name) / "linked" / "qa-mem"
        link.parent.mkdir()
        link.symlink_to(ROOT / "bin/qa-mem")
        self.where(link, self.tmp.name)

    def test_launcher_runs_from_another_folder(self):
        self.where(ROOT / "bin/qa-mem", "/")
        r = subprocess.run(
            [sys.executable, str(ROOT / "bin/qa-mem"), "--help"],
            cwd=self.tmp.name,
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage: qa-mem <command>", r.stdout)

    def test_a_read_command_does_not_load_report_or_doctor(self):
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); from qa_sdet import cli; "
            "assert cli.main(['card', '/x']) == 0; "
            "print(sorted(m for m in sys.modules if m.startswith('qa_sdet.')))"
        )
        r = subprocess.run(
            [sys.executable, "-c", code, str(ROOT / "lib")], env=self.env, capture_output=True, text=True
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        for loaded in ("report", "doctor", "ops", "scan"):
            self.assertNotIn(f"qa_sdet.{loaded}'", r.stdout)


if __name__ == "__main__":
    unittest.main()
