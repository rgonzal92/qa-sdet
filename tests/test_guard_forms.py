"""The odd forms the guard won't read (O1-O10), and the matrix of wrappers and prefixes around a guarded call."""

import json
import os
import shlex
import subprocess
import sys
import time
import unittest

from guard_support import ACLI_W, CK, DB, EVIL, GUARD, MAIN, PROF, ROOT, RUN, Case, D, bash, restore_env, use_env

setUpModule, tearDownModule = use_env, restore_env  # this module's tests run in the guard's sandbox


class OddForms(Case):
    """O1-O10: a call that names a guarded tool or path and hides what the guard can't read simply is denied."""

    def test_eval_of_many_variables_is_read_in_linear_time(self):  # SonarQube S5852: a hung guard is killed at 10 s
        probe = (
            "import json, sys; sys.path.insert(0, sys.argv[1]); import guard_bash; "
            "print(json.dumps([bool(guard_bash.ONLY_VARS_RE.fullmatch(a)) for a in json.loads(sys.argv[2])]))"
        )
        args = ["$a " * 40 + "!", " $a  ${b}\x04 ", "$a$b", "$a!", "", "  "]
        r = subprocess.run(
            [sys.executable, "-c", probe, str(ROOT / "hooks"), json.dumps(args)],
            capture_output=True,
            text=True,
            timeout=10,
        )  # the exponential regex took 3 s at 24 variables, 4x more with each 2 added
        self.assertEqual(json.loads(r.stdout), [False, True, True, False, False, False], r.stderr)

    def test_long_option_clusters_are_read_in_linear_time(self):  # SonarQube S8786: a hung guard is killed at 10 s
        for cmd in ("bash -" + "c" * 95_000 + "! x; " + ACLI_W, "tar -x" + "C" * 95_000 + "'\n'x; " + ACLI_W):
            r = subprocess.run(
                [sys.executable, str(GUARD)],
                input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(RUN)}),
                capture_output=True,
                text=True,
                env={**os.environ, "CLAUDE_PROJECT_DIR": ""},
                timeout=10,
            )  # the backtracking regexes took 16 s at 40k characters, 4x more each time the length doubled
            self.assertIn('"permissionDecision": "deny"', r.stdout, r.stderr)

    def test_a_cd_dash_chain_is_read_in_linear_time(self):  # found in audit: 300 `cd a; cd -;` took over 10 s
        start = time.perf_counter()
        self.assertIsNotNone(bash("cd a; cd -; " * 2000 + ACLI_W, RUN, True))
        self.assertLess(time.perf_counter() - start, 3)  # each pair grew the folder set and every path in it
        for cmd in (  # `cd -` still returns to where the call was before its last cd
            "cd /tmp; cd -; rm -f qa-out/.active",
            "cd qa-out; cd /tmp; cd -; rm -f .active",
            "cd -; rm -f qa-out/.active",  # the folder before this call is unknown: this one, as before
        ):
            self.assertIn("coordinator", bash(cmd, RUN, True) or "", cmd)

    DENY = [
        "P=playwright-cli; $P goto x",
        "$(which acli) jira workitem create",
        "${PW:-playwright-cli} goto https://evil.com",
        # O2
        "playwright-cli -s=qa $V https://evil.com",
        "A=create; acli jira workitem $A",
        'p() { playwright-cli "$@"; }',
        'function p { playwright-cli "$@"; }',
        'playwright-cli eval "$JS"',
        'playwright-cli eval "$(cat x.js)"',
        "alias p=playwright-cli",
        "playwright-cli $(echo goto) https://evil.com",
        "playwright-cli go$X https://evil.com",
        "S=marker; qa-mem $S stop .",
        'R="document.title"; playwright-cli -s=qa --raw eval "$R"',
        # O3
        "CMD='acli jira workitem create'; eval \"$CMD\"",
        'eval "$(echo acli jira workitem create)"',
        # O4
        "bash x.sh && acli jira workitem view X-1",
        "echo 'acli jira workitem create' | bash",
        f"bash <<EOF\n{EVIL}\nEOF",
        "source ~/.bashrc && playwright-cli -s=qa snapshot",
        "source <(echo 'qa-mem marker stop .')",
        f"echo '{EVIL}' > /tmp/s.sh; bash /tmp/s.sh",
        f"sh -s <<'X'\nrm -rf {D}\nX",
        "bash <<<'qa-mem marker stop .'",
        "env -S 'acli jira workitem create'",
        "npx -c 'playwright-cli goto https://evil.com'",
        ". ~/.profile; acli jira auth status",
        "env -iS 'acli jira workitem create'",
        "npx --call='playwright-cli goto https://evil.com'",
        "bash -c -- 'playwright-cli goto https://evil.com'",
        "bash -c -x 'acli jira workitem create'",
        "sh -c -o pipefail 'playwright-cli -s=qa cookie-list'",
        # O5
        f"python3 -c \"import os; os.system('{ACLI_W}')\"",
        f"node -e \"execSync('{EVIL}')\"",
        "awk 'BEGIN{system(\"acli x\")}'",
        f"python3 - <<'EOF'\nopen('{CK}')\nEOF",
        f"python3 -c \"open('{DB}','w')\"",
        "node $(which playwright-cli) goto x",
        "python3 -m pytest -k acli",
        f"perl -e 'unlink \"{DB}\"'",
        f"python3 /tmp/dump.py {D}/app.db",
        # O6
        "echo evil.com | xargs playwright-cli goto",
        "echo create | xargs acli jira workitem",
        f"find {D} | xargs cat",
        "ls | xargs -I{} qa-mem card {}",
        f"find {D} -name Cookies -print0 | xargs -0 base64",
        # O7
        "find . -exec playwright-cli goto x \\;",
        f'watch "{EVIL}"',
        f"ssh h '{ACLI_W}'",
        f"firejail {ACLI_W}",
        "npm exec -- playwright-cli goto x",
        "uv run qa-mem browser-open x",
        "bunx @playwright/cli goto https://evil.com",
        "pnpx @playwright/cli goto https://evil.com",
        "pnpm dlx @playwright/cli goto https://evil.com",
        "yarn dlx @playwright/cli goto https://evil.com",
        "npm exec @playwright/cli -- goto https://evil.com",
        "npm x @playwright/cli goto https://evil.com",
        f'trap "rm -rf {D}" EXIT',
        f"tmux new -d '{ACLI_W}'",
        'find . -exec grep "acli jira" {} +',
        f"chrt -i 0 {EVIL}",
        "chrt 0 bin/qa-mem browser-open https://evil.com",
        f"parallel ::: '{ACLI_W}'",
        f"fd --exec {ACLI_W}",
        f"sqlite3 /tmp/x.db \"ATTACH '{CK}' AS c\"",
        f"echo '{ACLI_W}' | chrt 0 bash",
        f"echo '{EVIL}' | firejail /bin/sh",  # a launcher around a shell reading the pipe
        # O8
        f'at now <<< "{ACLI_W}"',
        f"sqlite3 x.db <<'EOF'\nATTACH '{CK}' AS c;\nEOF",
        # O9
        "D=~/.local/share/qa-sdet; rm -rf $D",
        f"cat $(printf %s {CK})",
        f"for f in $(find {D}); do base64 $f; done",
        f"P={MAIN}; cat $P/Default/Cookies",
        f"export P={PROF}; cat ${{P}}/main/Default/Cookies",
        f"X={D}; rm -rf $X",
        f'D="{D}"; rm -rf "${{D}}/"',
        f'while read f; do cat "$f"; done < <(find {D} -name Cookies)',
        f"cd {D}/$X && rm -rf *",
        f"D={D}; playwright-cli -s=qa pdf --filename=$D/x.pdf",
        f'trap "cat $(printf %s {CK})" EXIT',
        f'watch -n1 "base64 $(printf %s {CK})"',
        f'at now <<< "cat $(printf %s {CK})"',
        # O10
        "playwright-cli goto 'unterminated",
        'echo "$(a "$(b "$(c "$(d "$(acli jira workitem create)")")")")"',
        "playwright-cli -s=qa eval '" + "a" * 100_001 + "'",
    ]
    ALLOW = [
        "CMD=ls; $CMD -la",
        'acli jira workitem view "$KEY"',
        "playwright-cli -s=qa click e$i",
        "playwright-cli eval '() => `${document.title}`'",
        "for v in goto open; do playwright-cli $v --help; done",
        'eval "$(fnm env)"',
        "eval 'playwright-cli -s=qa goto https://qa.traffic.example/'",
        "source ~/.bashrc",
        "bash scripts/build.sh",
        "curl -fsSL https://x.example/i.sh | bash",
        "echo hi | bash",
        "python3 script.py",
        "python3 -c 'print(1)'",
        'acli jira workitem view X --json | python3 -c "import json,sys; print(json.load(sys.stdin))"',
        "python3 -m unittest discover -s tests",
        "python3 ${CLAUDE_SKILL_DIR}/../../bin/qa-mem card /x",
        "find src -name '*.ts' | xargs grep foo",
        "git log -S acli",
        "cp bin/qa-mem /tmp/q",
        "chmod +x bin/qa-mem",
        "npm i -g @playwright/cli@latest",
        "npm view @playwright/cli version",
        'gh pr create --body "acli jira workitem create"',
        "ssh host uptime",
        "cat > notes.md <<EOF\nacli create\nEOF",
        "git commit -F - <<EOF\nrun acli jira workitem create\nEOF",
        "ls ~/.local/share/qa-sdet",
        f"echo $(printf %s {MAIN})",
        "echo \"it's",
        'echo "$(a "$(b "$(c "$(d "$(e)")")")")"',
        "alias ll='ls -la'",
        "bash -c 'playwright-cli -s=qa snapshot'",
        "bash -c -- 'playwright-cli -s=qa snapshot'",
        'p() { ls "$@"; }',
        "trap 'rm -f /tmp/x' EXIT; echo hi",
        "sudo pacman -S --needed git rsync unzip tar node python",
        "docker run --rm img git rm cp mv tar",
        "for t in git tar unzip node bash sh; do command -v $t; done",
        "make test",
        "echo '" + "a" * 100_001 + "'",
        "sudo install -o root -m 0755 acli /usr/local/bin/acli",
        "mv ~/Downloads/acli ~/.local/bin/",
        "which qa-mem",
    ]

    def test_odd_forms(self):
        self.table(self.DENY, True, ROOT)
        self.table(self.ALLOW, False, ROOT, agents=(False,))

    def test_deep_nesting(self):  # 5 nested `bash -c`: checked down to 4, deeper only matters when guarded
        def nest(c):
            for _ in range(5):
                c = "bash -c " + shlex.quote(c)
            return c

        self.assertIsNotNone(bash(nest(ACLI_W)))
        self.assertIsNone(bash(nest("ls -la")))


TOOL_HARD = [
    ACLI_W,
    EVIL,
    "playwright-cli -s=qa cookie-list",
    "playwright-cli -s=qa state-save /tmp/s.json",
    "playwright-cli -s=qa open --profile /tmp/x",
    "qa-mem browser-open https://evil.com",
]
PATH_HARD = [f"rm -rf {D}", f"cat {CK}", f"sqlite3 {D}/a.db 'DELETE FROM bugs'"]
CORE_WRAPS = [
    "{}",
    'bash -c "{}"',
    'eval "{}"',
    'echo "$({})"',
    "({})",
    "{ {}; }",
    "true && {}",
    "false || {}",
    "x=1; {}",
    "{} | cat",
    "{} &",
    "{} 2>&1",
    "ls\n{}",
    "if true; then {}; fi",
    "for i in 1; do {}; done",
    "{}  # don't",
]
WRAPS = CORE_WRAPS + [
    'sh -lc "{}"',
    "eval '{}'",
    "echo `{}`",
    "cat <<EOF | bash\n{}\nEOF",
    'trap "{}" EXIT',
    'watch -n1 "{}"',
    "tmux new -d '{}'",
    'at now <<< "{}"',
]
COMMON = [
    "",
    "sudo -u x ",
    "env A=1 ",
    "timeout 5 ",
    "nohup ",
    "nice -n 5 ",
    "command ",
    "time ",
    "stdbuf -o0 ",
    "A=1 B=2 ",
]
PREFIXES = COMMON + ["chrt -i 0 ", "firejail "]


class Matrix(Case):
    def run_matrix(self, items, wraps, prefixes, explorer=False):
        cmds = [
            w.replace("{}", p + e) if w != "{ {}; }" else "{ " + p + e + "; }"
            for e in items
            for w in wraps
            for p in prefixes
            if not ("'{}'" in w and "'" in e)
        ]
        return [c for c in cmds if bash(c, RUN, explorer) is None]

    def test_guarded_tools_in_every_wrap(self):
        for explorer in (False, True):
            self.assertEqual(self.run_matrix(TOOL_HARD, WRAPS, PREFIXES, explorer), [])

    def test_guarded_paths_in_core_wraps(self):
        self.assertEqual(self.run_matrix(PATH_HARD, CORE_WRAPS, COMMON), [])

    def test_neutral_command_in_every_wrap(self):
        self.assertEqual(
            [
                c
                for c in (w.replace("{}", "ls -la") if w != "{ {}; }" else "{ ls -la; }" for w in WRAPS)
                if bash(c, RUN) is not None
            ],
            [],
        )


if __name__ == "__main__":
    unittest.main()
