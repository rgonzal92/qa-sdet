"""The qa-mem command line: usage, error messages, and the bin/qa-mem launcher."""
import os, sqlite3, subprocess, sys, tempfile, unittest
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


class Launcher(unittest.TestCase):
    """bin/qa-mem finds lib/qa_sdet however it is started: isolated sys.path, through a symlink, from any folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {**os.environ, "QA_SDET_DATA": str(Path(self.tmp.name) / "data"),
                    "QA_SDET_CONFIG": str(Path(self.tmp.name) / "cfg/config.json")}
        for leaked in ("PYTHONPATH", "QA_MEM_DB"):  # a developer's shell may set them
            self.env.pop(leaked, None)

    def where(self, launcher, cwd, env=None, flags=()):
        r = subprocess.run([sys.executable, *flags, str(launcher), "where"], cwd=cwd, env=env or self.env,
                           capture_output=True, text=True)
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
        r = subprocess.run([sys.executable, str(ROOT / "bin/qa-mem"), "--help"], cwd=self.tmp.name, env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage: qa-mem <command>", r.stdout)

    def test_a_read_command_does_not_load_report_or_doctor(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from qa_sdet import cli; "
                "assert cli.main(['card', '/x']) == 0; "
                "print(sorted(m for m in sys.modules if m.startswith('qa_sdet.')))")
        r = subprocess.run([sys.executable, "-c", code, str(ROOT / "lib")], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        for loaded in ("report", "doctor", "ops", "scan"):
            self.assertNotIn(f"qa_sdet.{loaded}'", r.stdout)


if __name__ == "__main__":
    unittest.main()
