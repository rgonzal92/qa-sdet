"""What the guard's test modules share: a sandbox (HOME, the config and the data folder in temp folders), one guard
loaded in it, the calls that ask it for a decision, and the table-driven base class. Not a test module."""

import atexit
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
GUARD = Path(os.environ.get("GUARD") or ROOT / "hooks/guard.py").resolve()
TMP = Path(os.path.realpath(tempfile.mkdtemp()))
atexit.register(shutil.rmtree, TMP, True)
HOME = TMP / "home"
D = HOME / ".local/share/qa-sdet"  # the data folder: memory DB and browser profiles
PROF, MAIN = D / "profiles", D / "profiles/main"
CK, DB = MAIN / "Default/Cookies", D / "app.db"
CFG = HOME / ".config/qa-sdet/config.json"
PROJ, RUN = TMP / "proj", TMP / "run"  # a project without a run marker; one with a marker
Q = ROOT / "bin/qa-mem"
sys.path.insert(0, str(ROOT / "lib"))
from support import load_hook

EVIL, ACLI_W = "playwright-cli -s=qa goto https://evil.com", "acli jira workitem create --summary x"
EX, MAPPER = "qa-sdet:qa-explorer", "qa-sdet:qa-mapper"
_ENV = {"HOME": str(HOME), "QA_SDET_DATA": str(D), "QA_SDET_CONFIG": str(CFG)}
_DROP = ("CLAUDE_PROJECT_DIR", "XDG_DATA_HOME")  # a developer's own session may set these
_SAVED = {}


def use_env():  # set while the guard loads too (it reads them on load); a test module's setUpModule
    _SAVED.update({k: os.environ.get(k) for k in (*_ENV, *_DROP)})
    for k in _DROP:
        os.environ.pop(k, None)
    os.environ.update(_ENV)


def restore_env():
    for k, v in _SAVED.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


for _p in (CK.parent, CFG.parent, PROJ, RUN / "qa-out/r/evidence", RUN / "qa-out/r/.data"):
    _p.mkdir(parents=True, exist_ok=True)
CK.write_text("x")
DB.write_text("")
CFG.write_text(json.dumps({"allowedHosts": ["qa.traffic.example", "localhost"]}))
(RUN / "qa-out/.active").write_text(json.dumps({"expires": time.time() + 86400}))
use_env()
g = load_hook("guard_under_test", GUARD)
gb, gf, gl, gp, gt = g.guard_bash, g.guard_files, g.guard_log, g.guard_paths, g.guard_bash.guard_tools  # its modules
restore_env()


def bash(cmd, cwd=PROJ, explorer=False):
    return gb.check(cmd, str(cwd), explorer=explorer)


def tool(name, agent="", cwd=PROJ, **tin):
    return g.decide({"tool_name": name, "tool_input": tin, "cwd": str(cwd), "agent_type": agent})


def hook(data, script=None, project="", **env):
    """stdout of the hook process: GUARD itself, or a script in hooks/."""
    path = GUARD if script is None else ROOT / "hooks" / script
    return subprocess.run(
        [sys.executable, str(path)],
        input=data if isinstance(data, str) else json.dumps(data),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": project, **env},
    ).stdout


def decision(cmd, cwd=RUN, **data):
    """What main() prints for a Bash call, in-process: 'allow', 'deny' or None."""
    out = io.StringIO()
    stdin = io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd), **data}))
    with mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(out):
        g.main()
    return json.loads(out.getvalue() or "{}").get("hookSpecificOutput", {}).get("permissionDecision")


def marker(cwd, **m):
    Path(cwd, "qa-out").mkdir(parents=True, exist_ok=True)
    Path(cwd, "qa-out/.active").write_text(json.dumps({"expires": time.time() + 3600, **m}))
    return Path(cwd, "qa-out/.active")


def project(**m):
    cwd = Path(tempfile.mkdtemp(dir=TMP))
    if m:
        marker(cwd, **m)
    return cwd


class Case(unittest.TestCase):
    def table(self, cmds, denied, cwd=PROJ, agents=(False, True)):
        for explorer in agents:
            for cmd in cmds:
                with self.subTest(cmd=cmd, explorer=explorer):
                    (self.assertIsNotNone if denied else self.assertIsNone)(bash(cmd, cwd, explorer), cmd)
