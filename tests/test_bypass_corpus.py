import atexit, shutil
import itertools, json, os, tempfile, unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
T = Path(tempfile.mkdtemp()); (T / "data").mkdir()
atexit.register(shutil.rmtree, T, True)
os.environ["QA_SDET_CONFIG"] = str(T / "c.json"); os.environ["QA_SDET_DATA"] = str(T / "data")  # set before load
(T / "c.json").write_text(json.dumps({"allowedHosts": ["qa.example.com"]}))
g = SourceFileLoader("g", str(Path(__file__).resolve().parents[1] / "hooks/guard.py")).load_module()
D = str(T / "data")
evil = {"nav": "playwright-cli -s=qa goto https://evil.com", "nav2": "playwright-cli -s qa open evil.com",
        "acli": "acli jira workitem create --project P --summary x", "rm": f"rm -rf {D}",
        "fault": "playwright-cli -s=qa route **/api/**", "sql": f"sqlite3 {D}/a.db .tables-not-readonly-DELETE"}
prefixes = ["", "timeout 5 ", "env A=1 ", "nohup ", "sudo -u x ", "command ", "time ", "nice -n 5 ", "A=1 B=2 ", "stdbuf -o0 "]
wraps = ["{}", 'bash -c "{}"', 'sh -lc "{}"', 'eval "{}"', 'echo "$({})"', "({})", "{} 2>&1 | cat", "true && {}",
         "false || {}", "x=1; {}", "{} &", "{} >/dev/null 2>&1", "ls\n{}", "if true; then {}; fi", "{ {}; }",
         "for i in 1; do {}; done", "cat <<EOF | bash\n{}\nEOF", "echo `{}`", "{}  # don't"]
def run():
  missed = []
  for (name, e), p, w in itertools.product(evil.items(), prefixes, wraps):
    if name == "sql": e = f"sqlite3 {D}/a.db 'DELETE FROM bugs'"
    cmd = w.replace("{}", p + e, 1) if w != "{ {}; }" else "{ " + p + e + "; }"
    if not g.check(cmd, str(T)): missed.append(cmd)
  return missed


class BypassCorpus(unittest.TestCase):
    def test_every_wrapped_forbidden_command_is_denied(self):
        self.assertEqual(run(), [])


if __name__ == "__main__":
    unittest.main()
