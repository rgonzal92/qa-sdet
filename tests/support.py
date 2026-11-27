"""Shared test helpers: a sandbox (config, data folder, HOME and memory DB in a temp folder), the qa-mem runner, the
sample plan and run files, and the git helpers of the code-map tests. Not a test module."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from qa_sdet import cli as cli_mod
from qa_sdet import core, ingest

FIX = ROOT / "tests/fixture-src"


def load_hook(name, path=ROOT / "hooks/guard.py"):
    """A hook script (the guard by default) as a fresh module named `name`, registered in sys.modules as an import
    would. SourceFileLoader.load_module, which this replaces, is gone in Python 3.15."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def cli(*argv, stdin=None):
    """(exit code, stdout, stderr) of qa-mem main() in-process."""
    out, err = io.StringIO(), io.StringIO()
    with (
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
        mock.patch.object(sys, "stdin", io.StringIO(stdin or "")),
    ):
        code = cli_mod.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def done(code, out="", err=""):
    """A finished playwright-cli run, as subprocess.run returns it."""
    return mock.Mock(returncode=code, stdout=out, stderr=err)


def runfile(folder):
    """qa-out/<run>/.data/run-x.json: where an explorer writes its run JSON."""
    (folder / ".data").mkdir(parents=True, exist_ok=True)
    return folder / ".data" / "run-x.json"


def run(build="1.0", bugs=None, **page):
    """A one-page explore run on `build`; page fields (facts, locators, coverage) go on the page."""
    return {
        "run_id": f"r-{build}-{time.time_ns()}",
        "mode": "explore",
        "target": "/form",
        "build": build,
        "status": "COMPLETED",
        "summary": "s",
        "pages": [{"route": "/form", "name": "Form", **page}],
        "bugs": bugs or [],
    }


PLAN = {
    "kind": "ticket",
    "title": "PROJ-1, PROJ-2",
    "created": "2026-11-23T18:00:00Z",
    "env": {"url": "https://qa.example", "browser": "chrome"},
    "tickets": [
        {
            "key": "PROJ-1",
            "summary": "Speed limit form",
            "acs": [{"id": "AC-1", "text": "Saves 0-130"}, {"id": "AC-2", "text": "Rejects 131"}],
            "plan": {"scope": "speed field", "risks": ["rounding"], "out_of_scope": ["mobile"]},
        },
        {"key": "PROJ-2", "summary": "Incident list", "acs": [{"id": "AC-1", "text": "Sorted by date"}]},
    ],
    "cases": [
        {
            "id": "TC-1",
            "ticket": "PROJ-1",
            "ac": "AC-1",
            "title": "Save 130",
            "route": "/form",
            "priority": "P1",
            "steps": ["Open form", "Type 130 | save"],
            "expected": "Saved",
        },
        {
            "id": "TC-2",
            "ticket": "PROJ-1",
            "ac": "AC-2",
            "title": "Reject 131",
            "route": "/form",
            "steps": ["Type 131"],
            "expected": "Error shown",
        },
        {
            "id": "TC-3",
            "ticket": "PROJ-2",
            "ac": "AC-1",
            "title": "Sort",
            "route": "/list",
            "steps": ["Open"],
            "expected": "Newest first",
        },
        {
            "id": "TC-4",
            "ticket": "PROJ-2",
            "ac": "AC-1",
            "title": "Ties",
            "route": "/list",
            "steps": ["Open"],
            "expected": "Stable",
        },
    ],
}
RUN_FORM = {
    "run_id": "ticket-form-1",
    "mode": "ticket",
    "target": "/form",
    "build": "1.42",
    "status": "COMPLETED",
    "summary": "s",
    "notes": "Explored the speed field.",
    "env": {"url": "https://qa.example", "browser": "chrome"},
    "pages": [
        {
            "route": "/form",
            "coverage": [
                {"target": "speed", "heuristic": "boundary", "result": "fail"},
                {"target": "speed", "heuristic": "happy-path", "result": "pass"},
            ],
        }
    ],
    "cases": [
        {"id": "TC-1", "status": "PASS", "actual": "Saved"},
        {"id": "TC-2", "status": "FAIL", "actual": "131 saved", "bug": "BUG-1"},
    ],
    "questions": ["Is 0 a valid speed?"],
    "not_tested": ["Keyboard only: out of time"],
    "bugs": [
        {
            "route": "/form",
            "title": "Speed accepts 131",
            "severity": "HIGH",
            "classification": "CONFIRMED",
            "draft_path": "bugs/BUG-1.md",
            "evidence": "/x/evidence/BUG-1.png",
            "annotated": True,
            "ticket": "PROJ-1",
        }
    ],
}
RUN_LIST = {
    "run_id": "ticket-list-1",
    "mode": "ticket",
    "target": "/list",
    "build": "1.42",
    "status": "COMPLETED",
    "summary": "s",
    "cases": [
        {"id": "TC-3", "status": "pass"},
        {"id": "TC-4", "status": "QUESTION", "notes": "Order of ties unspecified"},
    ],
    "bugs": [
        {
            "route": "/list",
            "title": "Header misaligned",
            "severity": "LOW",
            "classification": "CONFIRMED",
            "draft_path": "bugs/BUG-2.md",
            "annotated": False,
        }
    ],
}


class Base(unittest.TestCase):
    """A sandbox: config, data folder, memory DB and HOME inside a temp folder `self.t`, a project folder `self.proj`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.saved = (core.CONFIG, core.DATA_DIR, dict(os.environ))
        core.CONFIG = self.t / "config" / "config.json"
        core.DATA_DIR = self.t / "data" / "qa-sdet"
        os.environ.update(
            HOME=str(self.t / "home"),
            QA_MEM_DB=str(self.t / "m.db"),
            XDG_DATA_HOME=str(self.t / "xdg"),
            XDG_CONFIG_HOME=str(self.t / "xdgc"),
        )
        self.proj = self.t / "proj"
        self.proj.mkdir()

    def tearDown(self):
        core.CONFIG, core.DATA_DIR, env = self.saved
        os.environ.clear()
        os.environ.update(env)
        self.tmp.cleanup()

    def config(self, **cfg):
        assert str(core.CONFIG).startswith(self.tmp.name), f"a test must not write the real config: {core.CONFIG}"
        core.CONFIG.parent.mkdir(parents=True, exist_ok=True)
        core.CONFIG.write_text(json.dumps(cfg))

    def rundir(self, name="2026-11-23-PROJ-1+PROJ-2", plan=None, runs=()):
        d = self.proj / "qa-out" / name
        for sub in ("bugs", "evidence", "repro", ".data"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        if plan is not None:
            (d / ".data/plan.json").write_text(json.dumps(plan))
        for i, (slug, r) in enumerate(runs):
            f = d / f".data/run-{slug}.json"
            f.write_text(json.dumps(r))
            os.utime(f, (time.time() - 100 + i, time.time() - 100 + i))
        return d


class MemBase(Base):
    """A sandbox with the memory DB open as `self.con`."""

    def setUp(self):
        super().setUp()
        self.con = core.connect()

    def tearDown(self):
        self.con.close()
        super().tearDown()


def git(repo, *args):  # isolated from the developer's git config (signing, hooks, templates)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )


def git_repo(path, files):
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    git(path, "init", "-q")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "one")
    return path


class CodeMapCase(MemBase):
    """Code-map tests: `self.dir` holds the repos and qa-out, `self.out` is qa-out/init where scan and map files go."""

    def setUp(self):
        super().setUp()
        self.dir = self.t
        self.out = self.dir / "qa-out" / "init"
        ingest.PARKED.clear()  # a module-level list: one test's parked file must not leak into the next

    def scan(self, *paths, full=False, stamp="s1"):
        from qa_sdet import scan

        return scan.codemap_scan(self.con, [str(p) for p in paths], self.out, full, stamp)

    def write_map(self, area, rows, scan_id=None):
        scan = json.loads((self.out / f"scan-{area}.json").read_text())
        f = self.out / f"code-map-{area}.json"
        f.write_text(json.dumps({"scan_id": scan_id or scan["scan_id"], "area": area, "rows": rows}))
        return f

    def ingest_all(self):
        self.assertEqual(ingest.PARKED, [], ingest.PARKED)
        ingest.ingest_pending(self.dir / "qa-out")
        self.con.close()
        self.con = core.connect()

    def map_chunks(self, s, rows_for):
        """Write each frontend chunk's map: `rows_for(file)` gives the rows for one file."""
        for m in s["areas"]["frontend"]["mappers"]:
            scan = json.loads(Path(m["scan_file"]).read_text())
            files = [f for r in scan["repos"] for f in r["files"]]
            Path(m["out"]).write_text(
                json.dumps(
                    {"scan_id": m["scan_id"], "area": "frontend", "rows": [x for f in files for x in rows_for(f)]}
                )
            )
        return s["areas"]["frontend"]["mappers"]
