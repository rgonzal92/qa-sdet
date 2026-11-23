import atexit, json, os, shutil, subprocess, sys, tempfile, time, unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_mkdtemp = tempfile.mkdtemp


def _cleaned_mkdtemp(*a, **k):  # every temp folder these tests make is removed when the run ends
    d = _mkdtemp(*a, **k)
    atexit.register(shutil.rmtree, d, True)
    return d


tempfile.mkdtemp = _cleaned_mkdtemp
TMP = tempfile.mkdtemp()
os.environ["QA_SDET_CONFIG"] = str(Path(TMP) / "config.json")
os.environ["QA_SDET_DATA"] = str(Path(TMP) / "data")
Path(TMP, "config.json").write_text(json.dumps({"allowedHosts": ["qa.traffic.example", "localhost"]}))
g = SourceFileLoader("guard", str(ROOT / "hooks/guard.py")).load_module()


RUN = tempfile.mkdtemp()  # a project with an active run (no budget limit) for the MCP browser rules
Path(RUN, "qa-out").mkdir()
Path(RUN, "qa-out/.active").write_text(json.dumps({"expires": time.time() + 86400}))


class Guard(unittest.TestCase):
    def deny(self, cmd, cwd=TMP):
        self.assertIsNotNone(g.check(cmd, cwd), cmd)

    def allow(self, cmd, cwd=TMP):
        self.assertIsNone(g.check(cmd, cwd), cmd)

    def test_navigation(self):
        self.allow("playwright-cli -s=qa goto https://qa.traffic.example/form")
        self.allow("playwright-cli -s=qa goto https://api.qa.traffic.example/x")
        self.allow("playwright-cli -s=qa goto http://localhost:8000/form.html")
        self.allow("playwright-cli -s=qa open --browser=firefox")
        self.deny("playwright-cli -s=qa goto https://prod.traffic.example/form")
        self.deny("playwright-cli open https://evil.com")
        self.deny("playwright-cli -s=qa tab-new evil.com")
        self.deny("npx playwright-cli goto https://evil.com")
        self.deny("cd x && playwright-cli -s=qa goto https://evil.com")
        self.deny("bash -c 'playwright-cli goto https://evil.com'")
        self.deny("playwright-cli eval \"location.href='https://evil.com'\"")
        self.deny("playwright-cli goto https://qa.traffic.example.evil.com")
        # round-1 bypasses
        self.deny("playwright-cli -s qa goto evil.com")
        self.deny("timeout 60 playwright-cli -s=qa goto https://evil.com")
        self.deny("nohup playwright-cli open evil.com &")
        self.deny("playwright-cli goto file:///etc/passwd")
        self.allow("playwright-cli -s qa open --browser firefox http://localhost:8765/")
        self.allow("playwright-cli --session qa --raw eval \"document.title\"")
        self.allow("playwright-cli -s=qa goto https://api.qa.traffic.example/x 2>&1")

    def test_round2_parsing(self):
        cwd = tempfile.mkdtemp()
        self.deny("playwright-cli -s=qa goto https://qa.traffic.example/#/map && playwright-cli -s=qa route '**'", cwd)
        self.deny("playwright-cli goto https://qa.traffic.example/#/x; playwright-cli goto https://evil.com")
        self.deny("npx @playwright/cli goto https://evil.com")
        self.deny("npx playwright-cli@latest goto https://evil.com")
        self.deny("bash -lc 'playwright-cli goto https://evil.com'")
        self.deny("eval 'playwright-cli goto https://evil.com'")
        self.deny("playwright-cli goto 'https://evil.com\\@qa.traffic.example'")
        self.deny("playwright-cli goto https://evil.com@qa.traffic.example/")
        self.allow("playwright-cli -s=qa fill e12 'https://example.org/x'")
        self.allow("cat > notes.md <<EOF\nDon't forget qa-sdet and acli create\nEOF")
        self.allow("echo To file run: acli jira workitem create --summary x")

    def test_round5_bypasses(self):
        D = f"{TMP}/data"
        evil = "playwright-cli -s=qa goto https://evil.com"
        self.deny(f"playwright-cli -s=qa \\\n goto https://evil.com")
        self.deny(f"rm -rf \\\n {D}")
        self.deny(f"bash <<EOF\n{evil}\nEOF")
        self.deny(f"sh -s <<'X'\nrm -rf {D}\nX")
        self.deny(f"cat <<<hi\n{evil}")
        self.deny(f"echo $((1<<2))\n{evil}")
        self.deny(f"cat <<END-X\nhi\nEND-X\n{evil}")
        self.allow("cat > n.md <<'END-DOC'\nDon't run playwright-cli goto https://evil.com\nEND-DOC")
        self.deny(f"rm -rf {D} # it's fine")
        self.allow("# don't run playwright-cli here\nls")
        self.deny(f'echo "$({evil})"')
        self.deny(f"echo `{evil}`")
        self.deny(f"sudo -n rm -rf {D}")
        self.deny(f"time -p rm -rf {D}")
        self.deny("npx --package @playwright/cli playwright-cli goto https://evil.com")
        self.deny("npm exec -- playwright-cli goto https://evil.com")
        self.deny(f"env -C {TMP} rm -rf data")
        self.deny("echo evil.com | xargs playwright-cli -s=qa goto")
        self.deny(f"dd if=/dev/zero of={D}/app.db")
        self.deny(f"echo x>{D}/app.db")
        self.deny(f": >| {D}/app.db")
        self.deny(f"cp -t {D} /tmp/x")
        self.deny(f"echo x | tee {D}/app.db /dev/null")
        self.deny(f"sqlite3 'file:{D}/app.db' 'DELETE FROM bugs'")
        self.deny(f"cd {TMP} && (cd /tmp); rm -rf data")
        self.deny(f"cd -P {TMP} && rm -rf data")
        self.deny("acli jira workitem create --project P --summary -h")
        self.deny("acli jira auth logout")
        self.allow("acli jira auth status")
        self.deny("P=playwright-cli; $P goto https://evil.com")
        self.allow("CMD=ls; $CMD -la")

    def test_round5_false_positives(self):
        home = str(Path(TMP))  # TMP is a parent of the DB dir, like ~
        self.allow("rm -f *.tmp", cwd=home)
        self.allow(f"rm -f {home}/*.log")
        self.allow("find . -name '*.pyc' -delete", cwd=home)
        self.allow("playwright-cli goto http://[::1]:8765/")
        self.allow("playwright-cli goto https://qa.traffic.example./x")
        for cmd in ["git log --oneline -5 # recent", "npm run build && npm test", "docker ps -a",
                    "python3 -c 'print(1)'", "sed -i 's/a/b/' f.txt", "ssh host 'uptime'", "make -j4",
                    "grep -rn 'https://x.com' src", "echo 'a#b' user@host"]:
            self.allow(cmd)

    def test_crash_and_bad_config_fail_closed(self):
        Path(TMP, "config.json").write_text(json.dumps({"allowedHosts": None}))
        try:
            self.allow("ls")
            self.deny("playwright-cli goto https://qa.traffic.example/")  # broken config: nothing allowed
            Path(TMP, "config.json").write_text(json.dumps({"allowedHosts": "qa.traffic.example"}))
            self.deny("playwright-cli goto https://q/")
        finally:
            Path(TMP, "config.json").write_text(json.dumps({"allowedHosts": ["qa.traffic.example", "localhost"]}))
        Path(TMP, "config.json").write_text(json.dumps({"allowedHosts": ["qa.traffic.example"]}))
        try:
            self.deny("playwright-cli goto http://localhost:631/")  # not listed: QA only
            self.deny("playwright-cli goto http://127.0.0.1:8080/")
        finally:
            Path(TMP, "config.json").write_text(json.dumps({"allowedHosts": ["qa.traffic.example", "localhost"]}))
        cwd = tempfile.mkdtemp()
        Path(cwd, "qa-out").mkdir()
        Path(cwd, "qa-out/.active").write_text(json.dumps({"expires": None, "faults": True}))
        self.deny("playwright-cli route '**'", cwd)
        real = g.check
        g.check = lambda *a: 1 / 0
        try:
            for cmd, denied in (("playwright-cli goto x", True), ("ls", False)):
                r = subprocess.run([sys.executable, "-c", "import sys,runpy;sys.argv=['g'];"
                                    "m=runpy.run_path(sys.argv[0] if False else %r, run_name='m');"
                                    "m['check']=lambda *a:1/0;m['main']()" % str(ROOT / "hooks/guard.py")],
                                   input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
                                   capture_output=True, text=True, env=os.environ)
                self.assertEqual("deny" in r.stdout, denied, (cmd, r.stdout, r.stderr))
        finally:
            g.check = real

    def test_round7_secrets_stay_out_of_conversation(self):
        state = Path(TMP) / "qa-state.json"  # CONFIG lives in TMP, so the session file is TMP/qa-state.json
        for cmd in ["playwright-cli -s=qa response-headers 3", "playwright-cli -s=qa cookie-list", "playwright-cli -s=qa cookie-get sid", "playwright-cli -s=qa request 1",
                    "playwright-cli -s=qa request-headers 2", "playwright-cli -s=qa localstorage-list",
                    "playwright-cli -s=qa --raw eval \"document.cookie\"",
                    "playwright-cli -s=qa run-code \"async page => page.context().cookies()\"",
                    f"cat {state}", f"python3 -c 'print(1)' {state}", f"cp {state} /tmp/x",
                    "playwright-cli -s=qa state-save /tmp/stolen.json"]:
            self.deny(cmd)
        for cmd in ["playwright-cli -s=qa requests", "playwright-cli -s=qa response-body 3",
                    f"playwright-cli -s=qa state-load {state}",
                    f"playwright-cli -s=qa-login state-save {state}", f"chmod 600 {state}", f"ls -la {state}"]:
            self.allow(cmd)
        for tool, key in (("Read", "file_path"), ("Grep", "path")):
            r = subprocess.run([sys.executable, str(ROOT / "hooks/guard.py")],
                               input=json.dumps({"tool_name": tool, "cwd": TMP, "tool_input": {key: str(state)}}),
                               capture_output=True, text=True, env=os.environ)
            self.assertIn("deny", r.stdout, tool)
        r = subprocess.run([sys.executable, str(ROOT / "hooks/guard.py")],
                           input=json.dumps({"tool_name": "Read", "cwd": TMP, "tool_input": {"file_path": "/etc/hosts"}}),
                           capture_output=True, text=True, env=os.environ)
        self.assertEqual(r.stdout, "")

    def test_round9_secrets_and_session_file(self):
        state = str(Path(TMP) / "qa-state.json")
        for cmd in [f"cat < {state}", f"jq .cookies < {state}", f"cat {TMP}/qa-state.*", f"cat {TMP}/qa-state.{{json,x}}",
                    f"head {TMP}/q*", f"grep -r value {TMP}", f"playwright-cli -s=qa upload {state}",
                    f"playwright-cli -s=qa drop e5 --path {state}", f"find {TMP} -name 'qa-*' -exec cat {{}} ';'",
                    "playwright-cli -s=qa eval \"document['cookie']\"", "playwright-cli -s=qa eval \"cookieStore.getAll()\"",
                    "playwright-cli -s=qa eval \"JSON.stringify(localStorage)\"", "playwright-cli -s=qa eval \"localStorage.getItem('t')\"",
                    "playwright-cli -s=qa eval \"Object.entries(sessionStorage)\"", "playwright-cli -s=qa eval \"({...localStorage})\""]:
            self.deny(cmd)
        for cmd in [f"[[ -f {state} ]] && echo yes", f"echo {state}", f"wc -c {state}", f"realpath {state}",
                    "playwright-cli -s=qa eval \"localStorage.clear()\"", "playwright-cli -s=qa --raw eval \"Object.keys(localStorage)\"",
                    "playwright-cli -s=qa --raw eval \"localStorage.key(0) + sessionStorage.length\"",
                    "playwright-cli -s=qa eval \"document.body.innerText.includes('localStorage')\"",
                    "cat tests/auth/storage-state.json", "npx playwright test --storage-state=storage-state.json"]:
            self.allow(cmd)

    def test_help_after_double_dash_is_not_help(self):
        self.deny("playwright-cli -s=qa open https://evil.example -- --help")
        self.deny("playwright-cli -s=qa run-code -- --help")
        self.allow("playwright-cli -s=qa goto --help")
        self.deny("playwright-cli show --port 9000 --host 0.0.0.0")
        self.deny("playwright-cli show --port=0 --host=192.168.1.5")
        self.allow("playwright-cli show --port 9000 --host localhost")
        self.allow("playwright-cli show")

    def test_round12_aliases_functions_variables(self):
        for cmd in ["playwright-cli -s=qa eval \"location.href='//evil.com'\"",
                    "alias p=playwright-cli; p -s=qa goto https://evil.com",
                    "function p { playwright-cli \"$@\"; }; p -s=qa goto https://evil.com",
                    "p() { playwright-cli \"$@\"; }", "coproc playwright-cli -s=qa goto https://evil.com",
                    "node $(which playwright-cli) -s=qa goto https://evil.com", "acli jira workitem create view",
                    "playwright-cli -s=qa $V https://evil.com"]:
            self.deny(cmd)
        for cmd in ["playwright-cli -s=qa eval \"fetch('//qa.traffic.example/api')\"", "alias ll='ls -la'",
                    "playwright-cli -s=qa eval '() => `${document.title}`'", "acli jira workitem comment list --key X",
                    "for v in goto open; do playwright-cli $v --help; done", "acli jira auth login",
                    "R=\"document.title\"; playwright-cli -s=qa --raw eval \"$R\"", "acli jira workitem view \"$KEY\""]:
            self.allow(cmd, cwd=str(ROOT))
        self.deny("R=\"location.href='https://evil.com'\"; playwright-cli -s=qa eval \"$R\"")
        self.deny("A=create; acli jira workitem $A --summary x")

    def test_marker_in_milliseconds_is_malformed(self):
        cwd = tempfile.mkdtemp()
        Path(cwd, "qa-out").mkdir()
        ms = int(time.time() * 1000)
        Path(cwd, "qa-out", ".active").write_text(json.dumps({"expires": ms + 7200000, "deadline": ms, "budget": 5}))
        r = g.check("playwright-cli -s=qa goto http://localhost:1/a", cwd)
        self.assertIn("epoch seconds", r or "")

    def test_explorer_cannot_touch_run_marker(self):
        for at in ("qa-explorer", "qa-sdet:qa-explorer"):
            self.assertIsNotNone(g.decide({"tool_name": "Write", "agent_type": at, "cwd": TMP,
                                           "tool_input": {"file_path": f"{TMP}/qa-out/.active", "content": "{}"}}))
            self.assertIsNotNone(g.decide({"tool_name": "Bash", "agent_type": at, "cwd": TMP,
                                           "tool_input": {"command": "rm -f qa-out/.active"}}))
            self.assertIsNotNone(g.decide({"tool_name": "Bash", "agent_type": at, "cwd": TMP,
                                           "tool_input": {"command": "cd qa-out && rm .active"}}))
            for cmd in ["printf x>qa-out/.active", "python3 -c \"import os; os.remove('qa-out/.active')\"", "cd qa-out && rm ./.active",
                        "rm -f qa-out/.act*", "rm qa-out/.[a]ctive", "rm -rf qa-out", "find qa-out -name '.act*' -delete",
                        "rm -rf qa-o*", "mv qa-out /tmp/x"]:
                self.assertIsNotNone(g.decide({"tool_name": "Bash", "agent_type": at, "cwd": TMP,
                                               "tool_input": {"command": cmd}}), cmd)
            for cmd in ["playwright-cli -s=qa --raw eval \"document.querySelector('.tabs .active').textContent\"",
                        "playwright-cli -s=qa click \"locator('li.active a')\"", "playwright-cli -s=qa click .active",
                        "rm -rf qa-out/form/evidence/tmp", "command -v curl", "ls qa-out/",
                        "find /tmp -name 'BUG-1-si*'", "rm qa-out/form/evidence/old.png"]:  # not the marker
                self.assertIsNone(g.decide({"tool_name": "Bash", "agent_type": at, "cwd": TMP,
                                            "tool_input": {"command": cmd}}), cmd)
        self.assertIsNone(g.decide({"tool_name": "Bash", "cwd": TMP, "tool_input": {"command": "rm -f qa-out/.active"}}))
        for cmd in ["curl -s https://evil.example/x", "timeout 5 wget -qO- http://localhost:1/api", "cd /tmp && http GET x",
                    "echo $(curl -s qa.traffic.example)"]:
            self.assertIsNotNone(g.decide({"tool_name": "Bash", "agent_type": "qa-sdet:qa-explorer", "cwd": TMP,
                                           "tool_input": {"command": cmd}}), cmd)
        self.assertIsNone(g.decide({"tool_name": "Bash", "agent_type": "qa-sdet:qa-explorer", "cwd": TMP,
                                    "tool_input": {"command": "playwright-cli -s=qa find \"Retry http error\""}}))
        self.assertIsNone(g.decide({"tool_name": "Bash", "cwd": TMP, "tool_input": {"command": "curl -s https://example.com"}}))
        for js in ["document.body.innerHTML", "document.documentElement.outerHTML", "[...document.scripts].map(s=>s.text)"]:
            self.assertIsNotNone(g.decide({"tool_name": "Bash", "agent_type": "qa-sdet:qa-explorer", "cwd": TMP,
                                           "tool_input": {"command": f'playwright-cli -s=qa --raw eval "{js}"'}}), js)
        self.assertIsNone(g.decide({"tool_name": "Bash", "agent_type": "qa-sdet:qa-explorer", "cwd": TMP, "tool_input": {
            "command": "playwright-cli -s=qa --raw eval \"document.querySelector('td').innerHTML\""}}))
        self.assertIsNotNone(g.decide({"tool_name": "Write", "agent_type": "qa-explorer", "cwd": TMP,
                                       "tool_input": {"file_path": f"{TMP}/qa-out/sub/../.active", "content": "{}"}}))
        self.assertIsNone(g.decide({"tool_name": "Write", "agent_type": "qa-explorer", "cwd": TMP,
                                    "tool_input": {"file_path": f"{TMP}/qa-out/form/run.json", "content": "{}"}}))

    def test_code_words_and_shell_globs(self):
        sess = tempfile.mkdtemp()  # a session file directly in the working folder
        cfg = os.environ["QA_SDET_CONFIG"]
        Path(sess, "config.json").write_text(json.dumps({"allowedHosts": ["qa.traffic.example"]}))
        Path(sess, "qa-state.json").write_text("{}")
        os.environ["QA_SDET_CONFIG"] = str(Path(sess, "config.json"))
        g.CONFIG = Path(sess, "config.json")
        try:
            for cmd in ["playwright-cli -s=qa --raw eval \"document.querySelector('footer .version')?.textContent\"",
                        "playwright-cli -s=qa fill \"getByRole('textbox', { name: 'Speed' })\" \"131\"",
                        "playwright-cli -s=qa --raw eval \"[...document.querySelectorAll('td')].length\""]:
                self.allow(cmd, cwd=sess)
            self.allow("cat *.json", cwd=str(Path(sess).parent))  # '*' doesn't cross folders
            for cmd in [f"cat {sess}/*", f"cat {sess}/q*", f"cat {sess}/*.{{json,x}}", f"head {sess}/[q]a*",
                        f"cat {Path(sess).parent}/**/*.json"]:
                self.deny(cmd, cwd="/tmp")
        finally:
            os.environ["QA_SDET_CONFIG"] = cfg
            g.CONFIG = Path(cfg)

    def test_c4_marker_command_with_exclude_step(self):
        cwd = tempfile.mkdtemp()
        self.allow("for e in qa-out/ .playwright-cli/; do grep -qxF \"$e\" .git/info/exclude || echo \"$e\" >> .git/info/exclude; done; "
                   "now=$(date +%s); printf '{\"expires\": %d, \"deadline\": %d, \"budget\": %d, \"faults\": %s}\\n' "
                   "$((now+7200)) $((now+20*60)) 30 false > qa-out/.active", cwd)
        self.deny("V=playwright-cli; $V -s=qa goto https://evil.com", cwd)  # still: program from a variable
        self.deny("echo $(( $(playwright-cli -s=qa goto https://evil.com) ))", cwd)  # still: subst inside arithmetic

    def test_review3_findings(self):
        st = f"{TMP}/qa-state.json"
        # 1: input redirection with no command word
        for cmd in [f'echo "$(< {st})"', f'while IFS= read -r l; do echo "$l"; done < {st}', f"{{ cat; }} < {st}",
                    f"(cat) < {st}", f"< {st}"]:
            self.deny(cmd, cwd=str(ROOT))
        # 2: variable program name inside $(...) or bash -c, where the nested text alone has no tool name
        for cmd in ['PW="playwright-cli -s=qa"; out=$($PW goto https://evil.com)', "A=acli; r=$($A jira workitem create --summary x)",
                    'PW=playwright-cli; bash -c "$PW goto https://evil.com"']:
            self.deny(cmd, cwd=str(ROOT))
        # 3: single-quoted text is literal: JS template literals get scanned; prose about acli isn't a command
        for cmd in ["playwright-cli -s=qa eval 'fetch(`https://evil.com/api`)'",
                    "playwright-cli -s=qa eval '() => location.href = `https://evil.com`'",
                    "playwright-cli -s=qa eval 'location.assign(`//evil.com/x`)'",
                    "playwright-cli -s=qa --raw eval '() => `${document.cookie}`'",
                    "echo '$((' ; playwright-cli goto https://evil.com ; echo '))'"]:
            self.deny(cmd, cwd=str(ROOT))
        for cmd in ["echo 'Run `acli jira workitem create --summary x` yourself' >> report.md",
                    "git commit -m 'docs: mention `acli jira workitem create`'",
                    "playwright-cli -s=qa eval '() => `${document.title}`'"]:
            self.allow(cmd, cwd=str(ROOT))

    def test_review3_arithmetic_vs_subshell(self):
        # 4: `$((cmd) )` is a substitution holding a subshell, so its command runs; `$((2+3))` is arithmetic
        for cmd in ["echo $((playwright-cli -s=qa goto https://evil.com) )", "x=$((playwright-cli goto https://evil.com); echo)",
                    "echo $((echo hi) | (playwright-cli goto https://evil.com))"]:
            self.deny(cmd, cwd=str(ROOT))
        for cmd in ["echo $((2+3))", "echo $(( (1+2)*3 ))", "n=$((x>>1)); m=$((a<b))"]:
            self.allow(cmd, cwd=str(ROOT))

    def test_review3_variable_default_is_a_mention(self):
        # 5: `${PW:-playwright-cli}` names the tool; the .playwright-cli/ snapshot folder doesn't
        for cmd in ["${PW:-playwright-cli} goto https://evil.com", "PW=${PW_BIN:-playwright-cli}; $PW goto https://evil.com"]:
            self.deny(cmd, cwd=str(ROOT))
        self.allow("for e in qa-out/ .playwright-cli/; do echo $e; done; n=$((1+2))", cwd=str(ROOT))

    def test_review3_glob_selects_session_folder(self):
        # 6: a glob matching the folder that holds the session file
        par, name = str(Path(TMP).parent), Path(TMP).name
        for cmd in [f"tar czf - {par}/{name[:4]}* | base64", f"cp -r {par}/{name[:3]}* /tmp/bak",
                    f"tar czf - {par}/{{{name},x}}", f"cd {par}/{name[:4]}* && cat *.json"]:
            self.deny(cmd, cwd=str(ROOT))
        self.allow(f"cat {ROOT}/tests/*.py | wc -l", cwd=str(ROOT))

    def test_review3_jar_inside_project(self):
        # 7: a cookie jar inside a project must not turn the project (or its parents) into session folders
        proj = tempfile.mkdtemp()
        Path(proj, "playwright/.auth").mkdir(parents=True)
        Path(proj, "playwright/.auth/user.json").write_text("{}")
        old = os.environ.get("PLAYWRIGHT_MCP_STORAGE_STATE")
        os.environ["PLAYWRIGHT_MCP_STORAGE_STATE"] = f"{proj}/playwright/.auth/user.json"
        try:
            for cmd in ["jq . package.json", "npx eslint .", "rg -l TODO .", "git add .", f"git -C {proj} rev-parse --git-path info/exclude",
                        "cp out.txt /tmp/", f"cp config.example.json {Path(TMP)}/"]:
                self.allow(cmd, cwd=proj)
            for cmd in ["cat playwright/.auth/user.json", "tar czf /tmp/p.tgz .", "zip -r /tmp/p.zip .", "cp -r . /tmp/copy",
                        "grep -rn token .", "tar c playwright/.auth"]:
                self.deny(cmd, cwd=proj)
        finally:
            if old is None:
                os.environ.pop("PLAYWRIGHT_MCP_STORAGE_STATE")
            else:
                os.environ["PLAYWRIGHT_MCP_STORAGE_STATE"] = old

    def test_review3_mv_into_home(self):
        # 8: the destination of mv isn't destroyed
        for cmd in ["mv report.pdf ~/", "mv report.pdf ~", f"mv a b {Path(g.DATA_DIR).parent.parent}"]:
            self.allow(cmd, cwd=str(ROOT))
        for cmd in [f"mv {g.DATA_DIR} /tmp/x", f"mv {Path(g.DATA_DIR).parent} /tmp/x", f"mv x.db {g.DATA_DIR}/default.db",
                    f"mv -t /tmp {g.DATA_DIR}"]:
            self.deny(cmd, cwd=str(ROOT))

    def test_review3_option_values(self):
        # 10: a session path given as --opt=value
        for cmd in [f"diff --from-file={TMP}/qa-state.json /dev/null", f"jq --rawfile x {TMP}/qa-state.json -n '$x'"]:
            self.deny(cmd, cwd=str(ROOT))
        self.allow("npx playwright test --config=playwright.config.ts --reporter=line", cwd=str(ROOT))

    def test_grep_pattern_is_not_a_path(self):
        self.allow('grep -rn "qa-state" skills agents references', cwd=str(ROOT))
        self.allow("rg -n qa-state.json README.md", cwd=str(ROOT))
        self.deny(f"grep -n token {TMP}/qa-state.json", cwd=str(ROOT))
        self.deny(f"grep -e x {TMP}/qa-state.json", cwd=str(ROOT))

    def test_crash_fallback_knows_the_cookie_jar(self):
        jar = tempfile.mkdtemp()
        Path(jar, "jar-state.json").write_text("{}")
        old = os.environ.get("PLAYWRIGHT_MCP_STORAGE_STATE")
        os.environ["PLAYWRIGHT_MCP_STORAGE_STATE"] = f"{jar}/jar-state.json"
        try:
            env = dict(os.environ)
            r = subprocess.run([sys.executable, "-c",
                                "import sys, runpy; sys.argv=['guard']; g = runpy.run_path(sys.argv[1] if False else %r, run_name='g');"
                                "g['decide'].__globals__['check'] = lambda *a, **k: 1/0; sys.exit(g['main']())" % str(ROOT / "hooks/guard.py")],
                               input=json.dumps({"tool_name": "Bash", "cwd": "/tmp", "tool_input": {"command": f"cat {jar}/jar-state.json"}}),
                               capture_output=True, text=True, env=env)
            self.assertIn("deny", r.stdout)
            self.assertTrue(g.mentions_session_path(f"cat {jar}/jar-state.json"))
            self.assertFalse(g.mentions_session_path("cat README.md"))
        finally:
            if old is None:
                os.environ.pop("PLAYWRIGHT_MCP_STORAGE_STATE")
            else:
                os.environ["PLAYWRIGHT_MCP_STORAGE_STATE"] = old

    def test_review4_storage_values(self):
        for js in ["localStorage", "() => window.sessionStorage", "localStorage.authToken", "JSON.parse(localStorage.user).token",
                   "Object.values(localStorage)", "localStorage['t']"]:
            self.deny(f'playwright-cli -s=qa --raw eval "{js}"', cwd=str(ROOT))
        for js in ["Object.keys(localStorage)", "localStorage.length", "localStorage.key(0)", "localStorage.clear()",
                   "localStorage.setItem('qa', '1')", "typeof localStorage", "for (const k in localStorage) {}"]:
            self.allow(f'playwright-cli -s=qa --raw eval "{js}"', cwd=str(ROOT))

    def test_review4_currency_values(self):
        for cmd in ["playwright-cli -s=qa fill \"getByLabel('Toll amount')\" '$100'", "playwright-cli -s=qa fill e3 '$1,000.00'",
                    "playwright-cli -s=qa type '$0'"]:
            self.allow(cmd, cwd=str(ROOT))
        for cmd in ['p() { playwright-cli "$@"; }', 'p() { playwright-cli -s=qa eval "$1"; }', 'p() { playwright-cli ${*}; }']:
            self.deny(cmd, cwd=str(ROOT))

    def test_review4_qa_state_in_prose(self):
        for cmd in ['git commit -m "guard: deny Grep content search into qa-state.json"', "git log --grep=qa-state",
                    "sed -i 's/qa-state/x/' README.md"]:
            self.allow(cmd, cwd=str(ROOT))
        for cmd in ["cat qa-state.json", "cp /tmp/x/qa-state.json.bak .", f"cat {TMP}/qa-state.json", f"diff --from-file={TMP}/qa-state.json x"]:
            self.deny(cmd, cwd=str(ROOT))

    def test_review4_isolated_context_value(self):
        self.allow("playwright-cli -s=qa tab-new --isolated-context anon https://qa.traffic.example/admin", cwd=str(ROOT))
        self.deny("playwright-cli -s=qa tab-new --isolated-context anon https://evil.com", cwd=str(ROOT))
        self.deny("playwright-cli -s=qa tab-new --isolated-context localhost evil.com", cwd=str(ROOT))

    def test_review4_loop_scope(self):
        cwd = tempfile.mkdtemp()
        Path(cwd, "qa-out").mkdir()
        now = int(time.time())
        Path(cwd, "qa-out", ".active").write_text(json.dumps({"expires": now + 7200, "deadline": now + 600, "budget": 50}))
        self.allow("for i in 1 2; do echo $i; done; playwright-cli -s=qa requests", cwd)
        self.allow("watch -n 1 ls >/dev/null & playwright-cli -s=qa requests", cwd)
        for cmd in ["for b in ID Road; do playwright-cli -s=qa click e$b; done",
                    "while read u; do playwright-cli -s=qa goto http://localhost:1/$u; done < urls.txt",
                    "watch -n 1 playwright-cli -s=qa requests", "ls | xargs -n1 playwright-cli -s=qa click",
                    "if true; then for v in 1 2 3; do playwright-cli -s=qa click e$v; done; fi",
                    "{ for v in 1 2; do playwright-cli -s=qa click e$v; done; }", "time for v in 1 2; do playwright-cli -s=qa click e1; done",
                    "for a in 1; do for b in 1; do echo; done; playwright-cli -s=qa click e1; done"]:
            self.deny(cmd, cwd)

    def test_review4_explorer_read_only_and_drafts(self):
        ex = lambda cmd: g.decide({"tool_name": "Bash", "agent_type": "qa-sdet:qa-explorer", "cwd": TMP, "tool_input": {"command": cmd}})
        for cmd in ["ls qa-out/*", "du -sh qa-out/*", "cat qa-out/form/run.json",
                    "cat > qa-out/form/bugs/BUG-2.md <<'EOF'\nThe page's document.body.innerHTML is never read by the test.\nEOF"]:
            self.assertIsNone(ex(cmd), cmd)
        for cmd in ["rm qa-out/.act*", "echo x > qa-out/.active", "playwright-cli -s=qa --raw eval \"document.body.innerHTML\""]:
            self.assertIsNotNone(ex(cmd), cmd)

    def test_review4_acli_hyphenated_reads(self):
        for cmd in ["acli jira board list-sprints --id 7", "acli jira sprint list-workitems --sprint 3 --json"]:
            self.allow(cmd, cwd=str(ROOT))
        for cmd in ["acli jira workitem create --summary list-sprints", "acli jira board create-sprint x"]:
            self.deny(cmd, cwd=str(ROOT))

    def test_review4_about_blank(self):
        self.allow("playwright-cli -s=qa goto about:blank", cwd=str(ROOT))
        for cmd in ["playwright-cli -s=qa goto about:srcdoc", "playwright-cli -s=qa goto data:text/html,hi"]:
            self.deny(cmd, cwd=str(ROOT))

    def test_heredoc_marker_inside_quotes_is_text(self):
        self.allow('python3 g.py "python3 - <<\'EOF\'" "echo hi"\necho done', cwd=str(ROOT))
        self.deny("python3 x.py \"a <<EOF b\"\nplaywright-cli -s=qa goto https://evil.com", cwd=str(ROOT))
        self.deny("bash <<'EOF'\nplaywright-cli -s=qa goto https://evil.com\nEOF", cwd=str(ROOT))  # a real heredoc still checked

    def test_review5_copy_destinations(self):
        par = str(Path(TMP).parent)  # an ancestor of the session file
        for cmd in [f"cp -r fish {par}/", f"rsync -av dotfiles/.config/ {par}/", f"cp -r dist {par}", f"scp -r host:/var/log/app {par}",
                    f"tar -xzf nvim.tgz -C {par}", f"tar xzf nvim.tgz -C {par}", f"tar -C {par} -czf /tmp/p.tgz {ROOT.name}"]:
            self.allow(cmd, cwd=str(ROOT))
        for cmd in [f"cp -r {par} /tmp/bak", f"rsync -a {par}/ host:/bak", f"tar czf /tmp/p.tgz {par}", f"tar -C / -czf /tmp/p.tgz {par[1:]}",
                    f"zip -r /tmp/p.zip {par}", f"cp -r -t /tmp {par}"]:
            self.deny(cmd, cwd=str(ROOT))

    def test_review5_explorer_reads(self):
        ex = lambda cmd: g.decide({"tool_name": "Bash", "agent_type": "qa-sdet:qa-explorer", "cwd": TMP, "tool_input": {"command": cmd}})
        for cmd in ["find qa-out -name 'BUG-*.md' -exec grep -l Speed {} +", "find qa-out -name '*.spec.ts' -exec head -5 {} \\;",
                    "cat qa-out/.active"]:
            self.assertIsNone(ex(cmd), cmd)
        for cmd in ["find qa-out -name '.act*' -exec rm {} +", "find qa-out -delete", "python3 -c \"import os; os.remove('qa-out/.active')\""]:
            r = ex(cmd)
            self.assertIsNotNone(r, cmd)
            self.assertNotIn("budget", r)  # must not read like the run is over

    def test_review5_xpath_is_not_a_url(self):
        for js in ["document.evaluate('//button[text()=\"Save\"]', document, null, 9, null).singleNodeValue?.disabled",
                   "document.evaluate('//td[2]', document, null, 9, null).singleNodeValue.textContent"]:
            self.allow(f"playwright-cli -s=qa --raw eval '{js}'", cwd=str(ROOT))
        for js in ["location.assign('//evil.com/x')", "fetch('//localhost:9/a')"]:
            self.deny(f"playwright-cli -s=qa --raw eval \"{js}\"", cwd=str(ROOT)) if "evil" in js else None
        self.allow("playwright-cli -s=qa --raw eval \"fetch('//localhost:8765/api/x')\"", cwd=str(ROOT))

    def test_case_statement_head(self):
        self.allow(f'case "{TMP}/qa-state.json" in /tmp/*) echo tmp;; *) echo other;; esac', cwd=str(ROOT))
        self.deny(f'case x in a) cat {TMP}/qa-state.json;; esac', cwd=str(ROOT))  # a branch's command is still checked
        self.deny('case x in a) playwright-cli -s=qa goto https://evil.com;; esac', cwd=str(ROOT))

    def test_review6_interpreter_reads_session(self):
        for cmd in ["python3 -c \"import json,os; d=json.load(open(os.path.expanduser('~/.config/qa-sdet/qa-state.json'))); print(d['cookies'])\"",
                    "node -e \"console.log(require('fs').readFileSync(process.env.HOME + '/.config/qa-sdet/qa-state.json','utf8'))\"",
                    f"python3 -c \"print(open('{TMP}/qa-state.json').read())\""]:
            self.deny(cmd, cwd=str(ROOT))
        self.allow("python3 -c \"print('qa-sdet ok')\"", cwd=str(ROOT))
        self.allow(f"python3 -c \"import json; print(json.load(open('{TMP}/config.json'))['allowedHosts'])\"", cwd=str(ROOT))
        self.allow("python3 -c \"import json,os; print(json.load(open(os.path.expanduser('~/.config/qa-sdet/config.json')))['qaBaseUrl'])\"", cwd=str(ROOT))
        self.allow("python3 harness.py 'cat ~/.config/qa-sdet/qa-state.json' 'git log --grep=qa-state.json'", cwd=str(ROOT))
        self.deny(f"python3 dump.py {TMP}/qa-state.json", cwd=str(ROOT))

    def test_review6_acli_link_reads(self):
        for cmd in ["acli jira workitem link list --key TRAF-123", "acli jira workitem link type"]:
            self.allow(cmd, cwd=str(ROOT))
        for cmd in ["acli jira workitem link create --out TRAF-1 --in TRAF-2 --type Blocks", "acli jira workitem link delete --id 5",
                    "acli jira workitem create view"]:
            self.deny(cmd, cwd=str(ROOT))

    def test_review6_explorer_past_deadline_message(self):
        cwd = tempfile.mkdtemp()
        Path(cwd, "qa-out").mkdir()
        now = int(time.time())
        Path(cwd, "qa-out", ".active").write_text(json.dumps({"expires": now + 3600, "deadline": now - 700, "budget": 50}))
        r = g.decide({"tool_name": "Bash", "agent_type": "qa-sdet:qa-explorer", "cwd": cwd,
                      "tool_input": {"command": "playwright-cli -s=qa screenshot"}})
        self.assertIn("time budget is used up", r)
        self.assertIn("delete", g.check("playwright-cli -s=qa screenshot", cwd))  # the user still gets the cleanup hint

    def test_review6_urls_as_data_in_eval(self):
        for js in ["document.createElementNS('http://www.w3.org/2000/svg','svg').namespaceURI",
                   "[...document.querySelectorAll('a')].filter(a => a.href.startsWith('https://docs.example.org/')).length",
                   "location.origin === 'https://other.example.com'"]:
            self.allow(f'playwright-cli -s=qa --raw eval "{js}"', cwd=str(ROOT))
        for js in ["fetch('https://evil.com/x')", "location.href = 'https://evil.com'", "window.open('https://evil.com')"]:
            self.deny(f'playwright-cli -s=qa --raw eval "{js}"', cwd=str(ROOT))

    def test_review6_small_items(self):
        self.assertIsNone(g.decide({"tool_name": "Bash", "agent_type": "qa-sdet:qa-explorer", "cwd": TMP,
                                    "tool_input": {"command": "find qa-out -newer qa-out/.active -type f"}}))
        self.allow("playwright-cli -s=qa goto 'https://qa.traffic.example?email=a@b.com'", cwd=str(ROOT))
        for cmd in ["playwright-cli -s=qa goto https://qa.traffic.example@evil.com/", "playwright-cli -s=qa goto https://u:p@evil.com"]:
            self.deny(cmd, cwd=str(ROOT))

    def test_review7_globs_beside_the_db(self):
        home = tempfile.mkdtemp()
        db = os.path.realpath(os.path.join(home, ".local/share/qa-sdet"))
        os.makedirs(db)
        old, g.DATA_DIR = g.DATA_DIR, db
        try:
            for cmd in [f"mv {home}/*.pdf /tmp/docs/", f"trash {home}/*.zip", f"gio trash {home}/*.AppImage",
                        f"rm -r {home}/screenshot-*", f"rm -rf {home}/*"]:  # the shell's * skips .local
                self.allow(cmd, cwd=str(ROOT))
            for cmd in [f"rm -rf {home}/.*", f"rm -rf {home}/.local/share/qa-*", f"rm -rf {home}/.local/*",
                        f"rm {db}/*.db", f"mv {home}/.local/share/q* /tmp", f"rm {home}/.local/share/qa-*/*.db",
                        f"rm {home}/.local/share/*/*.db"]:
                self.deny(cmd, cwd=str(ROOT))
        finally:
            g.DATA_DIR = old

    def test_round23_review_findings(self):
        parent = str(Path(TMP).parent)
        for cmd in ["playwright-cli -s -h goto https://evil.com", "playwright-cli --filename --help goto https://evil.com",
                    "playwright-cli show --host 0.0.0.0 -s -h", "playwright-cli $(echo goto) https://evil.com",
                    "playwright-cli ${V:-goto} https://evil.com", "playwright-cli go$X https://evil.com",
                    "V=goto; playwright-cli $V https://evil.com -- --help",
                    f"grep -rm1 token {parent}", f"grep -rC3 token {parent}", f"grep -d recurse token {parent}",
                    f"rg -L token {parent}", f"find {parent} -type f -exec cat {{}} +"]:
            self.deny(cmd)
        for cmd in ["echo https://github.com/x; playwright-cli -s=qa goto \"https://qa.traffic.example/$P\"",
                    "acli jira workitem view \"$1\"", f"find {parent} -name '*.md' -exec ls -l {{}} +",
                    "playwright-cli -s=qa goto --help"]:
            self.allow(cmd, cwd=str(ROOT))
        self.assertTrue(g.glob_may_match_session(f"{Path(TMP).name}/**", parent))
        self.assertTrue(g.glob_may_match_session(f"{{{Path(TMP).name},x}}/*.{{json,y}}", parent))
        self.assertFalse(g.glob_may_match_session("*.py", parent))

    def test_recursive_search_from_above_session_folder(self):
        parent = str(Path(TMP).parent)  # TMP holds the session file, so its parent is an ancestor
        for cmd in [f"grep -rn token {parent}", f"cd {parent} && grep -R cookie .", f"rg token {parent}",
                    "grep -r value /", f"cd {parent} && rg secret"]:
            self.deny(cmd)
        for cmd in [f"grep -rl token {parent}", f"rg -c token {parent}", f"grep token {parent}/x.txt",
                    f"grep -rn token {ROOT}", "rg TODO src", f"rg --files-with-matches token {parent}"]:
            self.allow(cmd, cwd=str(ROOT))
        def grep_tool(**tin):
            r = subprocess.run([sys.executable, str(ROOT / "hooks/guard.py")],
                               input=json.dumps({"tool_name": "Grep", "cwd": parent, "tool_input": tin}),
                               capture_output=True, text=True, env=os.environ)
            return "deny" in r.stdout
        self.assertTrue(grep_tool(pattern="x", path=parent, output_mode="content"))
        self.assertTrue(grep_tool(pattern="x", output_mode="content", glob="**/*.{ts,json}"))
        self.assertFalse(grep_tool(pattern="x", path=parent, output_mode="content", glob="*.py"))
        self.assertFalse(grep_tool(pattern="x", path=parent))  # file names only
        self.assertFalse(grep_tool(pattern="x", path=str(ROOT), output_mode="content"))

    def test_round9_budget_counting(self):
        cwd = tempfile.mkdtemp()
        Path(cwd, "qa-out").mkdir()
        active = Path(cwd, "qa-out/.active")
        def used():
            return json.loads(active.read_text()).get("used", 0)
        active.write_text(json.dumps({"expires": time.time() + 600, "budget": 10}))
        self.deny("for i in 1 2 3; do playwright-cli -s=qa click e$i; done", cwd)
        self.deny("seq 1 5 | xargs -I{} playwright-cli -s=qa click e{}", cwd)
        self.assertEqual(used(), 0)
        self.allow("t=$(playwright-cli -s=qa --raw eval \"document.title\")", cwd)
        self.assertEqual(used(), 1)  # a substitution counts once
        self.deny(f"playwright-cli -s=qa click e1 && rm -rf {TMP}/data", cwd)
        self.assertEqual(used(), 1)  # denied commands spend nothing
        active.write_text(json.dumps({"expires": time.time() + 600, "budget": "150 (default)"}))
        self.deny("playwright-cli -s=qa click e1", cwd)  # malformed marker: fail closed
        active.write_text(json.dumps({"budget": 5}))
        self.deny("playwright-cli -s=qa click e1", cwd)  # no expires: malformed
        self.allow("ls", cwd)
        active.write_text(json.dumps({"expires": time.time() + 600, "budget": 1000}))
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(16) as ex:
            list(ex.map(lambda _: g.spend_budget(active, 1), range(64)))
        self.assertEqual(used(), 64)

    def test_round9_parsing_wrappers_db(self):
        self.deny("playwright-cli -s=qa tab-new --isolated-context localhost evil.com")
        t = time.time()
        self.deny("playwright-cli -s=qa eval \"'" + "a" * 60000 + "'; location.href='https://evil.com'\"")
        self.assertLess(time.time() - t, 2)
        evil = "playwright-cli -s=qa goto https://evil.com"
        for cmd in [f"find . -exec {evil} \\;", f"setsid {evil}", f"ionice -c3 {evil}", f"taskset -c 0 {evil}",
                    f"flock /tmp/l {evil}", f"watch {evil}", f"script -qc '{evil}'", f"unbuffer {evil}",
                    f"chronic {evil}", f"strace -o /dev/null {evil}", f"su -c '{evil}'", f"busybox sh -c '{evil}'",
                    "node /usr/lib/node_modules/@playwright/cli/playwright-cli.js goto https://evil.com"]:
            self.deny(cmd)
        D = f"{TMP}/data"
        Path(D).mkdir(exist_ok=True)
        Path(D, "traffic-ui.db").touch()
        for cmd in [f"find {TMP} -name 'traffic-ui.db*' -delete", f"trash-put {D}", f"gio trash {D}",
                    f"playwright-cli -s=qa screenshot --filename={D}/x.png", f"playwright-cli -s=qa video-start {D}/v.webm"]:
            self.deny(cmd)
        self.allow(f"grep -c '>' {D}/notes.txt")
        self.allow(f"find {TMP} -name '*.pyc' -delete")

    def test_acli(self):
        self.allow("acli jira workitem view PROJ-1 --json")
        self.allow('acli jira workitem search --jql "project = P AND text ~ \\"add closure\\""')
        self.deny("acli jira workitem create --project P --type Bug --summary x")
        self.deny("acli jira workitem transition --key P-1 --status Done")
        self.deny("acli jira workitem comment create --key P-1 --body hi")
        self.deny("acli jira workitem create-bulk --from-csv x.csv")
        self.allow("acli jira workitem comment list --key P-1")
        self.allow("acli jira workitem view --help")
        self.allow("acli --version 2>&1 | head -1")
        self.allow("acli")

    def test_faults_need_marker(self):
        cwd = tempfile.mkdtemp()
        active = Path(cwd, "qa-out/.active")
        self.deny("playwright-cli -s=qa route '**/api/**'", cwd)
        self.deny("playwright-cli -s=qa network-state-set offline", cwd)
        Path(cwd, "qa-out").mkdir()
        active.write_text(json.dumps({"expires": time.time() + 600, "faults": False}))
        self.deny("playwright-cli -s=qa route '**/api/**'", cwd)  # run without --faults
        active.write_text(json.dumps({"expires": time.time() - 1, "faults": True}))
        self.deny("playwright-cli -s=qa route '**/api/**'", cwd)  # expired
        active.write_text(json.dumps({"expires": time.time() + 600, "faults": True}))
        self.allow("playwright-cli -s=qa route '**/api/**'", cwd)
        self.deny("playwright-cli -s=qa run-code \"async page => page.route('**', r => r.abort())\"", cwd)
        os.environ["CLAUDE_PROJECT_DIR"] = cwd  # marker found via the project dir from another cwd
        try:
            self.allow("playwright-cli -s=qa route '**/api/**'", tempfile.mkdtemp())
        finally:
            del os.environ["CLAUDE_PROJECT_DIR"]

    def test_round4_redirects_help_and_run_code(self):
        self.allow("playwright-cli -s=qa open --browser=firefox 2>&1 | tail -5")
        self.allow("playwright-cli -s=qa goto http://localhost:8765/form.html 2>&1 | tail -8")
        self.allow("playwright-cli -s=qa open http://localhost:1/ &> /tmp/log; echo done")
        self.deny("playwright-cli -s=qa open https://evil.com 2>&1 | tail -5")
        self.deny("playwright-cli -s=qa goto https://evil.com >/dev/null 2>&1 &")
        self.allow("playwright-cli route --help | head -30")
        self.allow("playwright-cli --help open 2>&1 | head -30")
        self.deny("playwright-cli -s=qa --raw run-code \"async page => { const out = {}; for (const v of ['1','999']) "
                  "{ await page.getByLabel('Speed').fill(v); out[v] = await page.getByRole('status').textContent(); } return out; }\"")
        self.deny("playwright-cli -s=qa run-code \"async page => require('child_process').execSync('id')\"")
        self.deny("playwright-cli -s=qa run-code \"async page => page.goto('https://evil.com')\"")
        self.deny("playwright-cli -s=qa run-code --filename x.js")
        self.deny("playwright-cli -s=qa run-code \"async page => page.context().newPage()\"")
        self.deny("playwright-cli -s=qa run-code \"async page => page.route('**', r => r.abort())\"")
        self.deny(f"echo x 2>&1 >{TMP}/data/app.db")
        self.allow("make 2>&1 | tee build.log")
        self.deny("playwright-cli -s=qa run-code \"async page => page.constructor.constructor('return pro'+'cess')()\"")
        self.deny("playwright-cli -s=qa run-code \"async page => page.request.get('/api')\"")
        self.deny("playwright-cli -s=qa run-code \"async page => eval('1')\"")

    def test_budget_and_deadline_enforced(self):
        cwd = tempfile.mkdtemp()
        Path(cwd, "qa-out").mkdir()
        active = Path(cwd, "qa-out/.active")
        active.write_text(json.dumps({"expires": time.time() + 600, "budget": 2}))
        self.allow("playwright-cli -s=qa goto http://localhost:1/a", cwd)
        self.allow("playwright-cli -s=qa click e1", cwd)
        self.deny("playwright-cli -s=qa click e2", cwd)
        self.allow("playwright-cli -s=qa close", cwd)  # finishing is always allowed
        self.assertEqual(json.loads(active.read_text())["used"], 2)
        self.deny("playwright-cli -s=qa goto https://evil.com", cwd)  # denied for host, not counted
        active.write_text(json.dumps({"expires": time.time() + 600, "deadline": time.time() - 1}))
        self.deny("playwright-cli -s=qa click e1", cwd)
        self.allow("ls", cwd)

    def test_rm_db_dir(self):
        self.deny(f"rm -rf {TMP}/data")
        self.deny(f"rm {TMP}/data/app.db")
        self.allow("rm -rf qa-out/form/evidence")
        self.deny(f"sqlite3 {TMP}/data/app.db 'DELETE FROM bugs'")
        self.deny("rm -rf ../data", cwd=f"{TMP}/data/sub")
        self.deny(f"find {TMP}/data -delete")
        self.deny(f"rm -rf {TMP}")  # parent of the DB dir
        self.deny(f"rm -rf {TMP}/data*")
        self.deny(f"cp /dev/null {TMP}/data/app.db")
        self.deny(f": > {TMP}/data/app.db")
        os.environ["QA_TEST_DIR"] = TMP
        self.deny("rm -rf \"$QA_TEST_DIR/data\"")
        self.allow(f"sqlite3 -readonly {TMP}/data/app.db .tables")
        self.allow(f"find {TMP}/data -name '*.db'")
        self.allow(f"grep -c rm {TMP}/data/app.db")
        self.allow(f"cp {TMP}/data/app.db /tmp/backup.db")
        self.deny(f"sudo -u root rm -rf {TMP}/data")
        self.deny(f"timeout -s KILL 5 rm -rf {TMP}/data")
        self.deny(f"cd {TMP} && rm -rf data")
        self.allow(f"cd {TMP}/data && ls")

    def test_mcp_fallbacks(self):
        def mcp(tool, cwd=RUN, agent=None, **tin):  # inside a QA run: browser MCP rules apply
            data = {"tool_name": tool, "tool_input": tin, "cwd": cwd}
            if agent:
                data["agent_type"] = agent
            return g.decide(data)
        nav = "mcp__playwright__browser_navigate"
        self.assertIsNone(mcp(nav, url="http://localhost:8765/form.html"))
        self.assertIsNone(mcp("mcp__plugin_playwright_playwright__browser_navigate", url="https://qa.traffic.example/x"))
        self.assertIsNotNone(mcp(nav, url="https://evil.com"))
        self.assertIsNotNone(mcp(nav, url=["https://evil.com"]))
        self.assertIsNotNone(mcp("mcp__playwright__browser_tabs", action="new", url="https://evil.com"))
        self.assertIsNone(mcp("mcp__playwright__browser_tabs", action="list"))
        # finding 11b: no switching to tabs the explorer didn't open (sign-in tab, a real browser's own pages)
        self.assertIsNotNone(mcp("mcp__playwright__browser_tabs", action="select", index=0))
        self.assertIsNotNone(mcp("mcp__playwright__browser_tab_select", index=0))
        self.assertIsNotNone(mcp("mcp__playwright__browser_tabs", agent="qa-sdet:qa-explorer", cwd=TMP, action="select", index=1))
        self.assertIsNone(mcp("mcp__playwright__browser_tabs", action="close"))
        self.assertIsNone(g.decide({"tool_name": "mcp__playwright__browser_tabs", "cwd": TMP,
                                    "tool_input": {"action": "select", "index": 0}}))  # outside a run: the user's browser
        self.assertIsNotNone(mcp("mcp__playwright__browser_run_code_unsafe", code="1"))
        self.assertIsNotNone(mcp("mcp__playwright__browser_network_request", index=3))
        self.assertIsNotNone(mcp("mcp__playwright__browser_network_request", index=3, part="response-headers"))
        self.assertIsNone(mcp("mcp__playwright__browser_network_request", index=3, part="response-body"))
        self.assertIsNone(mcp("mcp__playwright__browser_network_requests"))
        self.assertIsNotNone(mcp("mcp__playwright__browser_cookie_list"))
        ev = "mcp__playwright__browser_evaluate"
        self.assertIsNotNone(mcp(ev, function="() => document.cookie"))
        self.assertIsNotNone(mcp(ev, function="() => localStorage.getItem('t')"))
        self.assertIsNone(mcp(ev, function="() => Object.keys(localStorage)"))
        self.assertIsNotNone(mcp(ev, function="() => fetch('https://evil.com/x')"))
        self.assertIsNone(mcp(ev, function="() => document.body.innerHTML.length"))
        self.assertIsNotNone(mcp(ev, agent="qa-sdet:qa-explorer", function="() => document.body.innerHTML"))
        self.assertIsNotNone(mcp("mcp__playwright__browser_file_upload", paths=[os.path.expanduser("~/.config/qa-sdet/qa-state.json")]))
        self.assertIsNotNone(mcp("mcp__playwright__browser_take_screenshot", filename=os.environ["QA_SDET_DATA"] + "/x.png"))
        self.assertIsNotNone(mcp("mcp__playwright__browser_route", pattern="**"))  # no --faults marker
        # Jira / Confluence MCPs: reads only
        for ok in ("mcp__claude_ai_Atlassian__getJiraIssue", "mcp__atlassian__searchJiraIssuesUsingJql",
                   "mcp__claude_ai_Atlassian__atlassianUserInfo", "mcp__mcp-atlassian__jira_get_issue",
                   "mcp__atlassian__lookupJiraAccountId", "mcp__serena__write_memory", "mcp__claude_ai_Gmail__send_message"):
            self.assertIsNone(mcp(ok), ok)
        for bad in ("mcp__claude_ai_Atlassian__createJiraIssue", "mcp__atlassian__editJiraIssue",
                    "mcp__atlassian__transitionJiraIssue", "mcp__atlassian__addCommentToJiraIssue",
                    "mcp__mcp-atlassian__jira_create_issue", "mcp__atlassian__updateConfluencePage"):
            self.assertIsNotNone(mcp(bad), bad)

    def test_mcp_audit_rounds(self):
        def mcp(tool, cwd=RUN, agent=None, **tin):  # inside a QA run: browser MCP rules apply
            data = {"tool_name": tool, "tool_input": tin, "cwd": cwd}
            if agent:
                data["agent_type"] = agent
            return g.decide(data)
        ex = "qa-sdet:qa-explorer"
        # R1 every url argument; R5 older run-code name; R6 unroute is cleanup
        self.assertIsNotNone(mcp("mcp__playwright__browser_tab_new", url="https://evil.com"))
        self.assertIsNone(mcp("mcp__playwright__browser_tab_new", url="http://localhost:8765/"))
        self.assertIsNotNone(mcp("mcp__playwright__browser_run_code", code="1"))
        self.assertIsNone(mcp("mcp__playwright__browser_unroute"))
        # R21 script and non-web schemes, even with an allowed host in them (CLI and MCP)
        for u in ("javascript://localhost/%0Alocation.href='https://evil.com'", "javascript://localhost/%0Adocument.cookie",
                  "JavaScript://localhost/x", "data:text/html,<script>1</script>", "view-source:http://localhost/",
                  "file:///etc/passwd", "blob:http://localhost/x"):
            self.assertIsNotNone(mcp("mcp__playwright__browser_navigate", url=u), u)
            self.assertIsNotNone(mcp("mcp__playwright__browser_tabs", action="new", url=u), u)
            self.deny(f"playwright-cli -s=qa goto '{u}'")
        self.assertIsNone(mcp("mcp__playwright__browser_navigate", url="http://localhost:8765/form.html?q=javascript:x"))
        self.assertIsNone(mcp("mcp__playwright__browser_evaluate", function="() => new WebSocket('wss://qa.traffic.example/s')"))
        # audit pass 4: file fields under any name
        sess = os.path.join(os.path.dirname(os.environ["QA_SDET_CONFIG"]), "qa-state.json")
        for key in ("path", "file", "outputPath", "saveAs", "filePath"):
            self.assertIsNotNone(mcp("mcp__playwright__browser_pdf_save", **{key: sess}), key)
            self.assertIsNotNone(mcp("mcp__playwright__browser_pdf_save", **{key: RUN + "/qa-out/.active"}), key)
        self.assertIsNotNone(mcp("mcp__playwright__browser_file_upload", files=[os.path.dirname(sess)]))
        self.assertIsNone(mcp("mcp__playwright__browser_pdf_save", path=RUN + "/qa-out/form/evidence/page.pdf"))
        # audit pass 18: flags in file-like fields, numbers in code tools, a custom MCP profile folder
        self.assertIsNone(mcp("mcp__playwright__browser_snapshot", saveSession=True, outputMaxSize=1000))
        self.assertIsNone(mcp("mcp__playwright__browser_evaluate", function="() => 1", timeout=5000))
        custom = tempfile.mkdtemp()
        os.environ["PLAYWRIGHT_MCP_USER_DATA_DIR"] = custom
        try:
            self.assertIsNotNone(g.decide({"tool_name": "Read", "tool_input": {"file_path": custom + "/Default/Cookies"}, "cwd": TMP}))
        finally:
            del os.environ["PLAYWRIGHT_MCP_USER_DATA_DIR"]
        # audit pass 3: code under any field name of an evaluate tool
        for tin in ({"expression": "document.cookie"}, {"script": "localStorage.getItem('t')"},
                    {"fn": "() => fetch('https://evil.com/x')"}, {"function": "() => 1", "args": ["https://evil.com"]},
                    {"params": {"code": "document.cookie"}}):
            self.assertIsNotNone(mcp("mcp__playwright__browser_evaluate", **tin), tin)
        self.assertIsNone(mcp("mcp__playwright__browser_type", target="e1", text="see https://example.org for details"))
        deep = "document.cookie"
        for _ in range(12):
            deep = {"x": deep}
        with self.assertRaises(ValueError):  # main() turns this into a deny for browser tools
            mcp("mcp__playwright__browser_evaluate", params=deep)
        # audit pass 1: traces hold every request's headers (cookies) on disk
        self.assertIsNotNone(mcp("mcp__playwright__browser_start_tracing"))
        self.deny("playwright-cli -s=qa tracing-start")
        self.allow("playwright-cli -s=qa tracing-stop")  # stopping one is harmless
        # R2 Jira tools on servers with other names, any case
        for bad in ("mcp__corp__createJiraIssue", "mcp__corp__JIRA_create_issue", "mcp__tools__confluence_update_page"):
            self.assertIsNotNone(mcp(bad), bad)
        self.assertIsNone(mcp("mcp__corp__getJiraIssue"))
        # R18 the read/write split across the Atlassian (Rovo) and mcp-atlassian tool sets
        for ok in ("getTransitionsForJiraIssue", "getVisibleJiraProjects", "searchConfluenceUsingCql", "search", "fetch",
                   "getAccessibleAtlassianResources", "jira_search_fields", "jira_get_sprint_issues",
                   "jira_batch_get_changelogs", "confluence_get_page"):
            self.assertIsNone(mcp("mcp__atlassian__" + ok), ok)
        for bad in ("addWorklogToJiraIssue", "createConfluenceInlineComment", "jira_batch_create_issues",
                    "jira_link_to_epic", "jira_remove_issue_link", "jira_create_sprint", "jira_delete_issue",
                    "confluence_add_label"):
            self.assertIsNotNone(mcp("mcp__atlassian__" + bad), bad)
        # R4 the explorer's allowlist
        for tool in ("mcp__claude_ai_Gmail__send_message", "mcp__claude_ai_Atlassian__getJiraIssue", "WebFetch",
                     "Agent", "Skill", "SendUserFile", "Edit"):
            self.assertIsNotNone(mcp(tool, agent=ex), tool)
        for tool in ("Glob", "ToolSearch", "TodoWrite"):
            self.assertIsNone(mcp(tool, agent=ex), tool)
        self.assertIsNone(mcp("mcp__pw__browser_snapshot", agent=ex))
        self.assertIsNone(mcp("mcp__claude_ai_Gmail__send_message"))  # the main session is the user's
        # R7 marker through file arguments; R8 relative paths escaping the MCP's output folder
        self.assertIsNotNone(mcp("mcp__playwright__browser_take_screenshot", filename="qa-out/.active"))
        self.assertIsNotNone(mcp("mcp__playwright__browser_snapshot", filename=TMP + "/qa-out/.active"))
        self.assertIsNotNone(mcp("mcp__playwright__browser_snapshot", filename="../../x.md"))
        self.assertIsNone(mcp("mcp__playwright__browser_snapshot", filename=TMP + "/qa-out/form/snap.md"))
        self.assertIsNone(mcp("mcp__playwright__browser_snapshot", filename="snap.md"))
        # R23 the MCP's browser profile holds the QA login; symlinked marker folders; uploading a session folder
        prof = "~/.cache/ms-playwright-mcp/mcp-firefox-abc/cookies.sqlite"
        self.assertIsNotNone(g.decide({"tool_name": "Read", "tool_input": {"file_path": os.path.expanduser(prof)}, "cwd": TMP}))
        self.assertIsNotNone(g.decide({"tool_name": "Grep", "tool_input": {"pattern": "x", "path": os.path.expanduser("~/.cache/ms-playwright-mcp")}, "cwd": TMP}))
        self.deny(f"sqlite3 {prof} 'select host,name,value from moz_cookies'")
        self.deny("cp -r ~/.cache/ms-playwright-mcp /tmp/x")
        self.deny("grep -r sid ~/.cache")
        self.allow("ls ~/.cache/ms-playwright")  # the CLI's browser builds are fine
        cwd = tempfile.mkdtemp()
        Path(cwd, "qa-out").mkdir()
        os.symlink(Path(cwd, "qa-out"), Path(cwd, "x"))
        self.assertIsNotNone(mcp("mcp__playwright__browser_evaluate", cwd=cwd, agent=ex, function="() => 1", filename=cwd + "/x/.active"))
        self.assertIsNotNone(g.decide({"tool_name": "Write", "agent_type": ex, "cwd": cwd,
                                       "tool_input": {"file_path": cwd + "/x/.active", "content": "{}"}}))
        self.assertIsNotNone(mcp("mcp__playwright__browser_file_upload", paths=[os.path.expanduser("~/.config")]))
        self.assertIsNone(mcp("mcp__playwright__browser_file_upload", paths=[TMP + "/fixture.csv"]))
        # R25 the explorer can't rewrite the config (allowedHosts) by any route
        cfg = os.environ["QA_SDET_CONFIG"]
        self.assertIsNotNone(mcp("mcp__playwright__browser_evaluate", function="() => ({allowedHosts: ['evil.com']})", filename=cfg))
        self.assertIsNotNone(g.decide({"tool_name": "Write", "agent_type": ex, "cwd": TMP,
                                       "tool_input": {"file_path": cfg, "content": "{}"}}))
        self.assertIsNotNone(g.check(f"echo '{{}}' > {cfg}", TMP, explorer=True))
        self.assertIsNone(g.check(f"cat {cfg}", TMP, explorer=True))  # reading it is fine
        self.assertIsNone(g.decide({"tool_name": "Write", "cwd": TMP, "tool_input": {"file_path": cfg + ".x", "content": "{}"}}))
        # R17 the C4 prune covers the MCP's snapshot folder too
        self.allow(f"find {TMP}/.playwright-cli {TMP}/.playwright-mcp -type f -mtime +7 -delete 2>/dev/null")

    def test_mcp_browser_rules_scope(self):
        """R24: a Playwright MCP is also the user's everyday browser; its rules apply inside QA runs only."""
        nav = {"tool_name": "mcp__playwright__browser_navigate", "tool_input": {"url": "https://playwright.dev"}}
        self.assertIsNone(g.decide({**nav, "cwd": TMP}))  # no run marker: not a QA run
        self.assertIsNotNone(g.decide({**nav, "cwd": RUN}))  # a run is active
        self.assertIsNotNone(g.decide({**nav, "cwd": TMP, "agent_type": "qa-sdet:qa-explorer"}))  # the explorer always
        self.assertIsNotNone(g.decide({"tool_name": "mcp__x__createJiraIssue", "tool_input": {}, "cwd": TMP}))  # Jira always
        bad = tempfile.mkdtemp()
        Path(bad, "qa-out").mkdir()
        Path(bad, "qa-out/.active").write_text("{oops")
        self.assertIsNotNone(g.decide({**nav, "cwd": bad}))  # malformed marker: fail closed
        # R24 narrower secret tools; route_list; get_config; ownerDocument.cookie
        call = lambda name, **tin: g.decide({"tool_name": "mcp__playwright__" + name, "tool_input": tin, "cwd": RUN})
        for ok in ("browser_cookie_set", "browser_cookie_delete", "browser_cookie_clear", "browser_localstorage_set",
                   "browser_localstorage_clear", "browser_route_list"):
            self.assertIsNone(call(ok), ok)
        for bad_tool in ("browser_cookie_list", "browser_cookie_get", "browser_localstorage_get",
                         "browser_sessionstorage_list", "browser_storage_state", "browser_get_config"):
            self.assertIsNotNone(call(bad_tool), bad_tool)
        self.assertIsNotNone(call("browser_evaluate", element="x", target="e1", function="(el) => el.ownerDocument.cookie"))

    def test_audit_pass24_review_findings(self):
        ex = "qa-sdet:qa-explorer"
        mcp = lambda tool, cwd=RUN, agent=ex, **tin: g.decide({"tool_name": tool, "tool_input": tin, "cwd": cwd,
                                                                **({"agent_type": agent} if agent else {})})
        # 2: profiles reached by globs, inline code and Grep globs; XDG cache
        for cmd in ("sqlite3 ~/.cache/ms-playwright-m*/mcp-*/cookies.sqlite 'select name,value from moz_cookies'",
                    "cat ~/.cache/ms-play*/*/cookies.sqlite",
                    "python3 -c \"import sqlite3; sqlite3.connect('$HOME/.cache/ms-playwright-mcp/p/cookies.sqlite')\""):
            self.deny(cmd)
        self.allow("ls ~/.cache/ms-playwright/")  # browser builds
        self.assertIsNotNone(g.decide({"tool_name": "Grep", "cwd": TMP, "tool_input": {
            "pattern": "x", "path": os.path.expanduser("~/.cache"), "glob": "**/cookies.sqlite", "output_mode": "content"}}))
        xdg = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = xdg
        try:
            self.assertIsNotNone(g.decide({"tool_name": "Read", "cwd": TMP, "tool_input": {"file_path": xdg + "/ms-playwright-mcp/p/Cookies"}}))
        finally:
            del os.environ["XDG_CACHE_HOME"]
        # 3: browser_close ends the login during runs; outside a run it's the user's browser
        self.assertIsNotNone(mcp("mcp__playwright__browser_close"))
        self.assertIsNone(mcp("mcp__playwright__browser_close", cwd=TMP, agent=None))
        # 4: a marker left by an interrupted run doesn't block the main session's everyday browsing
        stale = tempfile.mkdtemp()
        Path(stale, "qa-out").mkdir()
        Path(stale, "qa-out/.active").write_text(json.dumps({"expires": time.time() + 3600, "deadline": time.time() - 700}))
        self.assertIsNone(mcp("mcp__playwright__browser_navigate", cwd=stale, agent=None, url="https://github.com"))
        self.assertIsNotNone(mcp("mcp__playwright__browser_navigate", cwd=stale, url="https://github.com"))  # explorer: still guarded
        # 6: descriptions and selectors aren't code
        self.assertIsNone(mcp("mcp__playwright__browser_evaluate", element="Help link to https://docs.vendor.com",
                              target="a[href='https://docs.vendor.com/help']", function="(el) => el.textContent"))
        self.assertIsNone(mcp("mcp__playwright__browser_evaluate", element="the localStorage notice", function="(el) => el.hidden"))
        # minor: read-only config commands for the explorer; download is a Jira read
        cfg = os.environ["QA_SDET_CONFIG"]
        for cmd in (f"sed -n 1,5p {cfg}", f"cp {cfg} {TMP}/cfg-copy.json"):
            self.assertIsNone(g.check(cmd, TMP, explorer=True), cmd)
        for cmd in (f"sed -i s/x/y/ {cfg}", f"cp {TMP}/x.json {cfg}", f"rsync --remove-source-files {cfg} {TMP}/"):
            self.assertIsNotNone(g.check(cmd, TMP, explorer=True), cmd)
        self.assertIsNone(mcp("mcp__mcp-atlassian__jira_download_attachments", agent=None))

    def test_every_browser_choice_starts(self):
        """The browser is the user's choice; the guard must accept each value in the start and login commands."""
        for b in ("chrome", "msedge", "firefox", "webkit"):
            self.allow(f"env -u PLAYWRIGHT_MCP_STORAGE_STATE playwright-cli -s=qa open --browser {b}")
            self.allow(f"env -u PLAYWRIGHT_MCP_STORAGE_STATE playwright-cli -s=qa-login open --browser={b} --headed about:blank")
            self.allow(f"playwright-cli install-browser {b}")

    def test_batch_over_budget_says_what_is_left(self):
        """A 9-command batch with 8 left isn't 'used up': the explorer should split it, not stop the run."""
        cwd = tempfile.mkdtemp()
        Path(cwd, "qa-out").mkdir()
        Path(cwd, "qa-out/.active").write_text(json.dumps({"expires": time.time() + 600, "budget": 15, "used": 7}))
        r = g.check("; ".join(["playwright-cli -s=qa click e1"] * 9), cwd)
        self.assertIn("only 8 of the run's 15 are left", r)
        self.assertIsNone(g.check("playwright-cli -s=qa click e1", cwd))
        Path(cwd, "qa-out/.active").write_text(json.dumps({"expires": time.time() + 600, "budget": 15, "used": 15}))
        self.assertIn("is used up", g.check("playwright-cli -s=qa click e1", cwd))

    def test_mcp_budget(self):
        cwd = tempfile.mkdtemp()
        Path(cwd, "qa-out").mkdir()
        active = Path(cwd, "qa-out/.active")
        active.write_text(json.dumps({"expires": time.time() + 600, "budget": 2}))
        call = lambda name, **tin: g.decide({"tool_name": "mcp__playwright__" + name, "tool_input": tin, "cwd": cwd})
        self.assertIsNone(call("browser_navigate", url="http://localhost:1/a"))
        self.assertIsNone(call("browser_click", target="e1"))
        self.assertIsNotNone(call("browser_click", target="e2"))
        self.assertIsNotNone(call("browser_close"))  # pass 24: it would end the MCP browser's login
        self.assertIsNone(call("browser_tabs", action="close"))  # the explorer closes its own tab: free
        self.assertIsNone(call("browser_tab_close"))  # older versions' tab tools are free too
        self.assertIsNotNone(call("browser_tabs", action="select", index=0))
        self.assertIsNotNone(call("browser_navigate", url="https://evil.com"))
        self.assertEqual(json.loads(active.read_text())["used"], 2)
        active.write_text("{bad")
        self.assertIsNotNone(call("browser_snapshot"))  # malformed marker: fail closed

    def test_unrelated_commands_pass(self):
        self.allow("git status && npm test")
        self.allow("ls -la | grep foo")

    def test_hook_io(self):
        inp = json.dumps({"tool_name": "Bash", "cwd": TMP,
                          "tool_input": {"command": "playwright-cli goto https://evil.com"}})
        r = subprocess.run([sys.executable, str(ROOT / "hooks/guard.py")], input=inp,
                           capture_output=True, text=True, env=os.environ)
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        r = subprocess.run([sys.executable, str(ROOT / "hooks/guard.py")],
                           input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}),
                           capture_output=True, text=True, env=os.environ)
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
