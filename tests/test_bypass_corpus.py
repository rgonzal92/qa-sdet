import atexit, shutil
import itertools, json, os, tempfile, unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
T = Path(tempfile.mkdtemp()); (T / "data").mkdir()
atexit.register(shutil.rmtree, T, True)
_ENV = {"QA_SDET_CONFIG": str(T / "c.json"), "QA_SDET_DATA": str(T / "data")}
_SAVED_ENV = {}
os.environ.update(_ENV)  # set before load; again in setUpModule, as other test modules set their own


def setUpModule():
    _SAVED_ENV.update({k: os.environ.get(k) for k in _ENV})
    os.environ.update(_ENV)


def tearDownModule():
    for k, v in _SAVED_ENV.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


(T / "c.json").write_text(json.dumps({"allowedHosts": ["qa.example.com"]}))
g = SourceFileLoader("g", str(Path(__file__).resolve().parents[1] / "hooks/guard.py")).load_module()
D = str(T / "data")
evil = {"nav": "playwright-cli -s=qa goto https://evil.com", "nav2": "playwright-cli -s qa open evil.com",
        "acli": "acli jira workitem create --project P --summary x", "rm": f"rm -rf {D}",
        "acliflag": "acli --profile p jira workitem create --project P --summary x",
        "fault": "playwright-cli -s=qa route **/api/**", "sql": f"sqlite3 {D}/a.db .tables-not-readonly-DELETE",
        # v0.4: the persistent browser profile, cookie dumps, qa-mem browser-open hosts, annotate paths
        "prof": f"cat {D}/profiles/main/Default/Cookies", "profglob": f"tar c {D}/prof*/main",
        "ssave": "playwright-cli -s=qa state-save /tmp/s.json", "pflag": "playwright-cli -s=qa open --profile /tmp/other",
        "pflag2": "playwright-cli --session qa open --user-data-dir=/tmp/other",
        "bopen": "python3 /p/bin/qa-mem browser-open https://evil.com", "annot": "python3 /p/bin/qa-mem annotate /tmp/x.png",
        # review: other browser sources and qa-mem launchers that skip its checks
        "pwenv": "PLAYWRIGHT_MCP_USER_DATA_DIR=/tmp/o playwright-cli -s=qa open", "pwcfg": "playwright-cli -s=qa open --config /tmp/c.json",
        "pwext": "playwright-cli -s=qa open --extension", "pwcdp": "playwright-cli -s=qa open --cdp=http://localhost:9222",
        "bopenenv": "PLAYWRIGHT_MCP_CONFIG=/tmp/c.json python3 /p/bin/qa-mem browser-open",
        "uvopen": "uv run /p/bin/qa-mem browser-open https://evil.com", "stdinopen": "python3 - browser-open https://evil.com < /p/bin/qa-mem",
        "profsubst": f"cat $(printf %s {D}/profiles/main/Default/Cookies)", "profxargs": f"find {D} -name Cookies | xargs base64",
        "proflink": f"ln -s {D} /tmp/q", "profgit": f"git -C {D} add -A",
        "annotdash": "python3 /p/bin/qa-mem annotate --box 1,1,2,2 --label x -/../../o/qa-out/r/evidence/a.png"}
# Only the explorer is denied these; the main session runs them.
explorer_evil = {"marker": "python3 /p/bin/qa-mem marker stop /proj", "perm": "/usr/bin/python3 -I /p/bin/qa-mem setup-permissions --apply",
                 "rundir": "python3.12 /p/bin/qa-mem rundir /proj --keys STUDIO-1", "direct": "/p/bin/qa-mem prune /proj",
                 "uv": "uv run python3 /p/bin/qa-mem marker stop /proj", "poetry": "poetry run /p/bin/qa-mem marker stop /proj",
                 "stdin": "python3 - marker stop /proj < /p/bin/qa-mem", "devstdin": "python3 /dev/stdin marker stop . < /p/bin/qa-mem",
                 "catcopy": "cat /p/bin/qa-mem > /tmp/q", "pipe": "echo qa-mem marker stop . | sh",
                 "herestr": "bash <<<'qa-mem marker stop /proj'", "lnsub": "ln -s $(command -v qa-mem) /tmp/q", "cliconf": "printf x > .playwright/cli.config.json",
                 # r12: forging the run marker (budget 0 = unlimited, faults) or planting its lock file
                 "ddof": "dd of=qa-out/.active", "rw": "exec 3<>qa-out/.active", "dupfile": "echo x >&qa-out/.active",
                 "cpdir": "cp -r /tmp/e/. qa-out/", "cpinto": "cp /tmp/e/.active qa-out", "untar": "tar xf /tmp/m.tar -C qa-out",
                 "unzip": "unzip -o /tmp/m.zip", "lock": "ln -s /etc/passwd qa-out/.active.lock"}
prefixes = ["", "timeout 5 ", "env A=1 ", "nohup ", "sudo -u x ", "command ", "time ", "nice -n 5 ", "A=1 B=2 ", "stdbuf -o0 ",
            # audit 2: launchers the guard has no name for (it checks every guarded word after one)
            "chrt -i 0 ", "firejail ", "faketime 2020 "]
wraps = ["{}", 'bash -c "{}"', 'sh -lc "{}"', 'eval "{}"', 'echo "$({})"', "({})", "{} 2>&1 | cat", "true && {}",
         "false || {}", "x=1; {}", "{} &", "{} >/dev/null 2>&1", "ls\n{}", "if true; then {}; fi", "{ {}; }",
         "for i in 1; do {}; done", "cat <<EOF | bash\n{}\nEOF", "echo `{}`", "{}  # don't",
         # audit 3: commands kept for later or handed over as one string
         'trap "{}" EXIT', 'watch -n1 "{}"', 'parallel ::: "{}"', "hyperfine '{}'", 'at now <<< "{}"', "tmux new -d '{}'"]
def run(cases=evil, explorer=False):
  missed = []
  for (name, e), p, w in itertools.product(cases.items(), prefixes, wraps):
    if name == "sql": e = f"sqlite3 {D}/a.db 'DELETE FROM bugs'"
    if ("'{}'" in w and "'" in e) or ('"{}"' in w and '"' in e): continue  # the wrap's quotes would end early
    cmd = w.replace("{}", p + e, 1) if w != "{ {}; }" else "{ " + p + e + "; }"
    if not g.check(cmd, str(T), explorer=explorer): missed.append(cmd)
  return missed


class BypassCorpus(unittest.TestCase):
    def test_every_wrapped_forbidden_command_is_denied(self):
        self.assertEqual(run(), [])

    def test_every_wrapped_skill_only_qa_mem_command_is_denied_to_the_explorer(self):
        self.assertEqual(run(explorer_evil, explorer=True), [])
        self.assertIsNone(g.check(explorer_evil["marker"], str(T)))  # the main session may


if __name__ == "__main__":
    unittest.main()
