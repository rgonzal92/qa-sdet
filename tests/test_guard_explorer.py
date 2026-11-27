"""The guard's agent rules: the qa-explorer's tools, files and network (R4), the qa-mapper's (R7)."""

import os
import unittest
from pathlib import Path
from unittest import mock

from guard_support import (
    CFG,
    CK,
    DB,
    EX,
    MAPPER,
    PROJ,
    RUN,
    TMP,
    Case,
    D,
    Q,
    bash,
    gp,
    project,
    restore_env,
    tool,
    use_env,
)

from qa_sdet import scan

setUpModule, tearDownModule = use_env, restore_env  # this module's tests run in the guard's sandbox


class R4Explorer(Case):
    cfg = str(CFG)
    DENY = [
        f"printf x > {cfg}",
        f"rm {cfg}",
        f"python3 -c \"open('{cfg}', 'w').write('{{}}')\"",
        f"cp /tmp/c.json {cfg}",
        f"sed -i s/a/b/ {cfg}",
        f"echo '{{}}' | tee {cfg}",
        f"echo '{{}}' | dd of={cfg}",
        f"X={cfg}; rm $X",
        f"rsync --remove-source-files {cfg} {PROJ}/",
        "printf x > .playwright/cli.config.json",
        "mkdir -p ~/.playwright && cp /tmp/c.json ~/.playwright/cli.config.json",
        "tee .playwright/cli.config.json < /tmp/c",
        "ln -s /tmp/c.json .playwright/cli.config.json",
        "cat > ~/.playwright/cli.config.json <<EOF\n{}\nEOF",
        "python3 -c \"open('.playwright/cli.config.json', 'w').write('{}')\"",
        "mv /tmp/cli.config.json .playwright/",
        "playwright-cli -s=qa screenshot --filename=.playwright/cli.config.json",
        "cat /tmp/c >> .playwright/cli.config.json",
        # network: only HTTP clients, only to allowedHosts, no proxies or option files
        "curl -s https://evil.example/x",
        "cd /tmp && http GET x",
        "curl -s https://qa.traffic.example/a evil.example",
        "curl -x http://evil.example https://qa.traffic.example/",
        "curl --url evil.example https://qa.traffic.example/",
        "https_proxy=http://evil.example curl https://qa.traffic.example/",
        "ssh qa.traffic.example",
        "wget -i /tmp/urls.txt",
        "nc qa.traffic.example 80",
        "ssh host curl x",
        "wget --conf=/tmp/x https://qa.traffic.example/",
        "wget --input=/tmp/urls.txt https://qa.traffic.example/",
        "http --prox=https:http://evil.example https://qa.traffic.example/",
        "CURL_HOME=/tmp curl https://qa.traffic.example/",
        "WGETRC=/tmp/w wget https://qa.traffic.example/",
        "export XDG_CONFIG_HOME=/tmp; curl https://qa.traffic.example/",
        "curl --variable 'H=evil.example/' --expand-url 'https://{{H}}.qa.traffic.example/'",  # curl 8.3+: host from a variable
        "H=evil.example/ curl --variable %H --expand-url 'https://{{H}}.qa.traffic.example/'",
        "curl --alt-svc /tmp/alt.txt https://qa.traffic.example/",
        "curl --dns-servers 10.0.0.1 https://qa.traffic.example/",
        # a script file run by its path (written in the same call: it doesn't exist yet when the guard reads it)
        "./x.sh",
        "bin/run.sh --fast",
        "printf 'curl evil.example' > x.sh && chmod +x x.sh && ./x.sh",
        "/tmp/x.sh",
        # downloads over the config
        f"curl -so {cfg} https://qa.traffic.example/c",
        f"wget -O {cfg} https://qa.traffic.example/c",
        "curl -o.playwright/cli.config.json https://qa.traffic.example/c",
        f"curl --output={cfg} https://qa.traffic.example/c",
        f"wget -P {CFG.parent} https://qa.traffic.example/config.json",
        f"cd {CFG.parent} && curl -O https://qa.traffic.example/config.json",
        f"curl -O --output-dir {CFG.parent} https://qa.traffic.example/config.json",
        # script files (a page could steer it to write one)
        "bash /tmp/x.sh",
        "sh x",
        "source ./x",
        ". ./x",
        "python3 /tmp/x.py",
        "node x.js",
        "bash < /tmp/x.sh",
        # packages: downloaded from another host and run
        "npx cowsay hi",
        "timeout 60 npx x",
        "npm install left-pad",
        "npm i -g x",
        "pip install requests",
        "pip3 install --user x",
        "python3 -m pip install x",
        "pnpm dlx x",
        "yarn add x",
        "uvx ruff",
    ]
    ALLOW = [
        f"cat {cfg}",
        f"sed -n 1,5p {cfg}",
        f"cp {cfg} {RUN}/qa-out/r/.data/cfg-copy.json",
        "cat .playwright/cli.config.json",
        "ls -l ~/.playwright/cli.config.json",
        "jq . .playwright/cli.config.json",
        "timeout 5 wget -qO- http://localhost:1/api",
        "echo $(curl -s qa.traffic.example)",
        "curl -s -o /tmp/r.json -H 'Accept: application/json' https://qa.traffic.example/api/x | jq .",
        'playwright-cli -s=qa find "Retry http error"',
        "command -v curl",
        "rm -rf qa-out/r/.data/x",
        "cat qa-out/.active",
        "git status",
        "python3 -m unittest discover -s tests",
        f"python3 {Q} card /x",
        "qa-mem report qa-out/r",
        "echo hi | bash",
        "bash -c 'ls -la'",
        "playwright-cli kill-all",
        "curl -s https://qa.traffic.example/api/x",
        "/usr/bin/env ls",
        f"{Q} card /x",
        "playwright-cli -s=qa --raw eval 'document.body.innerHTML'",
    ]

    def test_explorer_rules(self):
        self.table(self.DENY, True, RUN, agents=(True,))
        self.table(self.ALLOW, False, RUN, agents=(True,))

    INLINE = [
        "python3 -c 'print(1)'",
        "python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])))' qa-out/r/.data/run-x.json",
        "python3 -c \"import json;p='qa-out/.active';d=json.load(open(p));d['api_writes']=True;json.dump(d,open(p,'w'))\"",
        "python3 -c \"import urllib.request as u; u.urlopen('https://evil.example.com')\"",
        "python3 -Ic 'x'",
        "echo 'print(1)' | python3",
        "python3 - <<'EOF'\nprint(1)\nEOF",
        "python3",
        "timeout 5 python3 -c 'x'",
        "/usr/bin/python3.12 -c 'x'",
        "env python3 -c 'x'",
        "node -e 'require(\"fs\")'",
        "node -p '1+1'",
        "node --eval 'x'",
        "perl -e 'print 1'",
        "perl -ne 'print' qa-out/r/.data/run-x.json",
        "ruby -e 'puts 1'",
        "php -r 'echo 1;'",
        "deno eval 'x'",
        "bun -e 'x'",
        "bash -c \"python3 -c 'x'\"",
        "cat x | node",
    ]
    INLINE_ALLOW = [
        "python3 --version",
        "node --version",
        "python3 -m json.tool qa-out/r/.data/run-x.json",
        "awk '{print $1}' qa-out/r/.data/requests-x.log",
        "jq . qa-out/r/.data/run-x.json",
    ]

    def test_explorer_runs_no_inline_code(self):  # found live: python3 -c can rewrite the run marker or reach any host
        for cmd in self.INLINE:
            with self.subTest(cmd=cmd):
                reason = bash(cmd, RUN, True)
                self.assertIsNotNone(reason, cmd)
        self.assertIn("inline code", bash("python3 -c 'print(1)'", RUN, True))
        self.table(self.INLINE_ALLOW, False, RUN, agents=(True,))
        self.table(
            ["python3 -c 'print(1)'", "node -e 'x'", "echo 'print(1)' | python3"], False, RUN, agents=(False,)
        )  # your session

    def test_explorer_navigates_in_its_browser_not_a_new_one(
        self,
    ):  # a bare open drops the sign-in and the signal history
        for cmd in (
            "playwright-cli -s=qa open 'https://qa.traffic.example/map?layer=closures'",
            "playwright-cli -s=qa open",
            "playwright-cli -s=qa --raw open https://qa.traffic.example/ && playwright-cli -s=qa snapshot",
        ):
            self.assertIn("goto", bash(cmd, RUN, True), cmd)
            self.assertIsNone(bash(cmd, RUN), cmd)  # the main session may (a user's own check)
        self.assertIsNone(bash("playwright-cli -s=qa goto 'https://qa.traffic.example/map?layer=closures'", RUN, True))

    def test_main_session_edits_config_fetches_and_runs_scripts(self):
        self.table(
            [
                f"printf x > {self.cfg}",
                "curl -s https://example.com",
                "ssh host uptime",
                "bash /tmp/x.sh",
                "python3 /tmp/x.py",
                "source ./x",
                "printf x > .playwright/cli.config.json",
                "npx cowsay hi",
                "pip install requests",
            ],
            False,
            RUN,
            agents=(False,),
        )

    def test_write_tools(self):
        for at in (EX, "qa-sdet:qa-explorer ", "QA-SDET:QA-EXPLORER", " qa-explorer"):
            self.assertIsNotNone(tool("Write", at, file_path=str(CFG), content="{}"), at)
            self.assertIsNotNone(tool("Edit", at, file_path=str(CFG), old_string="a", new_string="b"), at)
            self.assertIsNotNone(tool("Write", at, file_path=f"{PROJ}/.playwright/cli.config.json", content="{}"), at)
            for p in (
                f"{RUN}/qa-out/r/bugs/BUG-1.md",
                f"{RUN}/qa-out/r/.data/run-form.json",
                f"{RUN}/qa-out/r/evidence/TC-1.png",
                f"{RUN}/qa-out/r/repro/BUG-1.spec.ts",
                f"{RUN}/qa-out/r/.data/requests-form.log",
                f"{RUN}/qa-out/r/.data/map-1.png",
            ):
                self.assertIsNone(tool("Write", at, cwd=RUN, file_path=p, content="x"), (at, p))
        self.assertIsNone(tool("Write", file_path=str(CFG), content="{}"))  # the main session may (with the user's yes)

    def test_explorer_writes_only_its_run_files(self):  # page text could steer it to write source or the report
        for p in (
            f"{RUN}/qa-out/.active",
            f"{RUN}/qa-out/r/REPORT.md",
            f"{RUN}/qa-out/r/.data/plan.json",
            f"{RUN}/qa-out/README.md",
            f"{RUN}/qa-out/plugin-log.jsonl",
            f"{RUN}/qa-out/r/.data/run-form.json.ingested",
            f"{RUN}/qa-out/r/.data/trace-form.jsonl",  # the hooks' record of its calls, not the explorer's to write
            f"{RUN}/qa-out/r/.data/prior-form.json",  # the earlier results dispatch keeps for carry_prior
            f"{RUN}/qa-out/PROJ-1/.data/history.jsonl",  # the ticket's verdicts per date and build: the report's
            f"{RUN}/src/app.tsx",
            "/srv/app/x.ts",
            f"{PROJ}/qa-out/r/bugs/BUG-1.md",
            f"{RUN}/qa-out/r/bugs/sub/x.md",
            f"{RUN}/qa-out/r/x.md",
        ):
            self.assertIn("qa-explorer writes only", tool("Write", EX, cwd=RUN, file_path=p, content="x") or "", p)
        link = RUN / "qa-out/r/bugs/BUG-9.md"
        link.parent.mkdir(exist_ok=True)
        link.symlink_to(RUN / "app.tsx")  # a link out of the run folder is its target
        try:
            self.assertIn("qa-explorer writes only", tool("Write", EX, cwd=RUN, file_path=str(link), content="x") or "")
        finally:
            link.unlink()

    def test_explorers_cannot_run_crawl_or_discover(self):  # whole browser passes the guard can't count
        for cmd in (
            f"qa-mem crawl '{RUN}/qa-out/r'",
            f"qa-mem crawl '{RUN}/qa-out/r' /zones",
            f"qa-mem discover '{RUN}/qa-out/r' 'https://qa.traffic.example/'",
        ):
            self.assertIn("coordinator's", bash(cmd, RUN, True) or "", cmd)
            self.assertIsNone(bash(cmd, RUN), cmd)  # the main session runs them
        self.assertIsNone(
            tool("Bash", "qa-sdet:qa-crawler", RUN, command="bash /tmp/x.sh")
        )  # no such agent: not an explorer

    def test_the_reviewer_only_reads_run_files(self):  # a critic of untrusted drafts: no shell, no writes, no secrets
        RV = "qa-sdet:qa-reviewer"
        self.assertIsNone(tool("Read", RV, cwd=RUN, file_path=f"{RUN}/qa-out/r/bugs/BUG-1.md"))
        self.assertIsNone(
            tool("Read", RV, cwd=RUN, file_path=str(Path(gp.PLUGIN_QA_MEM).parent.parent / "references/bug-draft.md"))
        )
        for name, tin in (
            ("Bash", {"command": "ls"}),
            ("Write", {"file_path": f"{RUN}/qa-out/r/x.md", "content": "x"}),
            ("Read", {"file_path": f"{RUN}/src/app.tsx"}),
            ("Read", {"file_path": str(Path.home() / ".ssh/id_rsa")}),
            ("Grep", {"pattern": "x", "path": f"{RUN}/qa-out"}),
            ("mcp__github__create_issue", {}),
        ):
            self.assertIn("qa-reviewer", tool(name, RV, cwd=RUN, **tin) or "", name)

    def test_a_finishing_subagent_hands_back_its_report(self):  # denied, every mapper retried and lost its report
        for agent in (MAPPER, EX, "qa-sdet:qa-reviewer"):
            self.assertIsNone(tool("SubagentHandback", agent, cwd=RUN, message="done"), agent)
            for name in ("Agent", "Edit", "WebFetch", "mcp__github__create_issue"):
                self.assertIsNotNone(tool(name, agent, cwd=RUN), (agent, name))

    def test_the_users_manual_memory_writes_are_not_the_explorers(self):  # page text could steer it to hide a signal
        for cmd in (
            "qa-mem signal-ignore 3",
            "qa-mem bug-status abcd1234 INVALID",
            "qa-mem forget fact 2",
            "qa-mem remember /x rule 'no limit'",
            "qa-mem answer 1 yes",
            "qa-mem stale 9.9",
        ):
            self.assertIn("the user's", bash(cmd, RUN, explorer=True) or "", cmd)
            self.assertIsNone(bash(cmd, RUN), cmd)  # the main session (the user's request) may

    def test_text_filters_that_write_a_file_cannot_rewrite_the_marker_the_config_or_the_db(self):  # found in audit
        m = "qa-out/.active"
        for cmd in (
            f"sed 's/false/true/' {m} | sort -o {m}",
            f"sort -o{m} /tmp/x",
            f"sort --output={m} /tmp/x",
            f"sort -uo {m} /tmp/x",
            f"shuf -o {m} /tmp/x",
            f"uniq /tmp/x {m}",
            f"awk '{{print > FILENAME}}' {m}",
            f'awk \'BEGIN {{ print "x" > "{m}" }}\'',
            f"sed -n 'w {m}' /tmp/x",
            f"sed 's/a/b/w {m}' /tmp/x",
        ):
            self.assertIn("coordinator", bash(cmd, RUN, explorer=True) or "", cmd)
        for cmd in (f"sort -o {CFG} /tmp/x", f"uniq /tmp/x {CFG}", f"awk '{{print > FILENAME}}' {CFG}"):
            self.assertIsNotNone(bash(cmd, RUN, explorer=True), cmd)
        for cmd in (
            f"sort -o {DB} /dev/null",
            f"uniq /dev/null {DB}",
            f"split -b1G /dev/null {DB}",
            f"csplit -f {D}/x /tmp/x 1",
            f"sed -n 'w {DB}' /tmp/x",
        ):
            for explorer in (False, True):
                self.assertIsNotNone(bash(cmd, PROJ, explorer), cmd)
        for cmd in (  # reads, and writes elsewhere, stay allowed
            "sort -o out.txt in.txt",
            "uniq a.txt b.txt",
            "split -l 10 big.txt part-",
            f"sort {m}",
            f"sed -n 1p {m}",
            f"awk '{{print $1}}' {m}",
            "awk '$1 > 5' data.txt",
        ):
            self.assertIsNone(bash(cmd, RUN, explorer=True), cmd)

    def test_the_run_marker_is_the_coordinators(self):  # a steered explorer could turn on faults or API writes
        for cmd in (
            f"qa-mem dispatch '{RUN}/qa-out/r' --route /x --faults",
            f"qa-mem marker start '{RUN}' --faults",
            f"qa-mem marker stop '{RUN}'",
            f"python3 {Q} marker stop .",
            "rm -f qa-out/.active",
            "printf x>qa-out/.active",
            "echo '{\"expires\": 9}' > qa-out/.active",
            "cp /tmp/m.json qa-out/.active",
            "mv qa-out/.active /tmp/m",
            "tee qa-out/.active < /tmp/m",
            f"rm -f {RUN}/qa-out/.active",
        ):
            self.assertIn("coordinator", bash(cmd, RUN, explorer=True) or "", cmd)
            self.assertIsNone(bash(cmd, RUN), cmd)  # the skill's main session runs setup and teardown
        self.assertIsNone(bash("cat qa-out/.active", RUN, explorer=True))  # reading it is fine

    def test_the_marker_files_by_glob(self):  # found in review: a glob passed
        (RUN / "qa-out/.active.progress").touch()
        try:
            for cmd in (
                "rm qa-out/.active*",
                "rm -f qa-out/.act?ve",
                "rm qa-out/.[a]ctive",
                f"rm -f {RUN}/qa-out/.active*",
            ):
                self.assertIn("coordinator", bash(cmd, RUN, explorer=True) or "", cmd)
                self.assertIsNone(bash(cmd, RUN), cmd)
            for cmd in ("cat qa-out/.active*", "ls -la qa-out/.active*", "rm -rf qa-out/r/.data/x*"):
                self.assertIsNone(bash(cmd, RUN, explorer=True), cmd)
        finally:
            (RUN / "qa-out/.active.progress").unlink(missing_ok=True)

    def test_the_progress_file_beside_the_marker_is_the_coordinators_too(
        self,
    ):  # deleting it would reset the spin and failure counts
        for cmd in (
            "rm -f qa-out/.active.progress",
            "printf x>qa-out/.active.progress",
            "mv qa-out/.active.progress /tmp/m",
            "cp /tmp/m qa-out/.active.progress",
            f"rm -f {RUN}/qa-out/.active.progress",
            "truncate -s0 qa-out/.active.progress",
            "tee qa-out/.active.progress < /tmp/m",
            "ln -sf /tmp/m qa-out/.active.progress",
        ):
            self.assertIn("coordinator", bash(cmd, RUN, explorer=True) or "", cmd)
            self.assertIsNone(bash(cmd, RUN), cmd)  # the main session may
        self.assertIsNone(bash("cat qa-out/.active.progress", RUN, explorer=True))

    def test_explorer_writes_stay_in_its_run_files(
        self,
    ):  # as its Write tool: redirects, playwright-cli files, evidence
        out = f"{RUN}/qa-out/r"
        for cmd in (
            f"playwright-cli -s=qa requests --all > '{out}/.data/requests-form.log'",
            f"playwright-cli -s=qa console error --all > {out}/.data/console-form.log",
            f"playwright-cli -s=qa screenshot --filename={out}/evidence/BUG-1.png",
            f"playwright-cli -s=qa video-start {out}/evidence/BUG-1.webm",
            f"playwright-cli -s=qa requests --filter x > {out}/evidence/BUG-1-network.txt",
            "playwright-cli -s=qa snapshot > /dev/null 2>&1",
            f"qa-mem evidence {out}/evidence/TC-1.png --selector '#f' --label x",
        ):
            self.assertIsNone(bash(cmd, RUN, explorer=True), cmd)
        for cmd in (
            f"playwright-cli -s=qa requests --all > {out}/.data/plan.json",
            f"playwright-cli -s=qa requests --all >> {out}/.data/trace-form.jsonl",
            f"playwright-cli -s=qa console error --all > {out}/REPORT.md",
            f"playwright-cli -s=qa screenshot --filename={RUN}/qa-out/shot.png",
            "echo hi > /tmp/notes.txt",
            "qa-mem evidence /tmp/elsewhere/qa-out/r/evidence/BUG-1.png --selector '#f' --label x",
        ):  # another project
            self.assertIn("run files", bash(cmd, RUN, explorer=True) or "", cmd)
            self.assertIsNone(bash(cmd, RUN), cmd)  # the main session: the user's own commands

    def test_explorer_has_only_its_tools(self):  # it inherited every MCP server of the session (GitHub, Slack, ...)
        for name in (
            "mcp__github__create_issue",
            "mcp__slack__post_message",
            "Edit",
            "WebFetch",
            "Agent",
            "NotebookEdit",
        ):
            self.assertIn("not one of its tools", tool(name, EX, cwd=RUN) or "", name)
            self.assertIsNone(
                tool(name, "", cwd=RUN) if name.startswith("mcp__") else None
            )  # the main session keeps them
        for name in ("Read", "Grep", "Glob", "ToolSearch"):
            self.assertIsNone(tool(name, EX, cwd=RUN, file_path=str(RUN / "qa-out/r/.data/run-form.json")), name)


class R7Mapper(Case):
    def test_mapper_reads_source_and_writes_only_its_map(self):
        cwd = project()
        (cwd / "src").mkdir()
        (cwd / "src/routes.js").write_text("export default []\n")
        (cwd / ".env").write_text("DB_PASSWORD=hunter2\n")
        (cwd / "src/server.key").write_text("x")
        m = lambda name, **tin: tool(name, MAPPER, cwd, **tin)
        for name, tin in [
            ("Read", {"file_path": "src/routes.js"}),
            ("Grep", {"pattern": "path", "path": str(cwd), "output_mode": "content"}),
            ("Glob", {"pattern": "src/**/*.js"}),
            ("LS", {"path": str(cwd)}),
            ("Grep", {"pattern": "x", "glob": "*.{js,ts}"}),
        ]:
            self.assertIsNone(m(name, **tin), (name, tin))
        for name in (
            "Bash",
            "Edit",
            "WebFetch",
            "Agent",
            "NotebookEdit",
            "MultiEdit",
            "mcp__playwright__browser_navigate",
            "mcp__filesystem__read_file",
        ):
            self.assertIsNotNone(m(name, command="ls", path=str(cwd)), name)
        for f in [
            ".env",
            "src/.env.production",
            "src/server.key",
            "~/.ssh/id_ed25519",
            "~/.aws/credentials",
            "~/.kube/config",
            "~/.config/gcloud/application_default_credentials.json",
            "~/.netrc",
            str(DB),
            str(CFG),
            str(CK),
        ]:
            self.assertIsNotNone(m("Read", file_path=f), f)
        self.assertIsNotNone(m("Grep", pattern="x", path="~/.ssh", output_mode="content"))
        self.assertIsNotNone(m("Glob", pattern="**/*", path="~/.aws"))
        self.assertIsNotNone(m("LS", path=str(D)))
        ok = cwd / "qa-out/init/code-map-frontend.json"
        self.assertIsNone(m("Write", file_path=str(ok), content="{}"))
        self.assertIsNone(m("Write", file_path=f"{cwd}/qa-out/init/code-map-backend-2.json", content="{}"))
        with mock.patch.dict(os.environ, CLAUDE_PROJECT_DIR=str(cwd)):
            self.assertIsNone(tool("Write", MAPPER, TMP, file_path=str(ok), content="{}"))
        for bad in (
            "qa-out/init/scan-frontend.json",
            "qa-out/init/code-map-evil.json",
            "qa-out/.active",
            "src/routes.js",
            str(CFG),
            "qa-out/init/../init2/code-map-tests.json",
        ):
            self.assertIsNotNone(m("Write", file_path=bad, content="{}"), bad)
        (cwd / "qa-out/init").mkdir(parents=True)
        os.symlink(cwd / "src/routes.js", cwd / "qa-out/init/code-map-tests.json")
        self.assertIsNotNone(m("Write", file_path=f"{cwd}/qa-out/init/code-map-tests.json", content="{}"))  # a link out
        for at in ("QA-MAPPER ", " qa-mapper", "qa-sdet:QA-Mapper"):
            self.assertIsNotNone(tool("Bash", at, cwd, command="ls"), at)

    def test_secret_names_match_codemap_scan(self):
        self.assertEqual(gp.SECRET_FILE_RE.pattern, scan.SECRET_FILE_RE.pattern)


if __name__ == "__main__":
    unittest.main()
