"""The guard's tool rules: Jira stays read-only (R1), the browser goes only to allowed hosts (R2), session secrets
stay out (R3)."""

import json
import os
import shlex
import unittest
from pathlib import Path
from unittest import mock

from guard_support import (
    ACLI_W,
    CFG,
    CK,
    DB,
    EVIL,
    EX,
    HOME,
    MAIN,
    PROF,
    PROJ,
    ROOT,
    RUN,
    TMP,
    Case,
    D,
    Q,
    bash,
    gt,
    project,
    restore_env,
    tool,
    use_env,
)

setUpModule, tearDownModule = use_env, restore_env  # this module's tests run in the guard's sandbox


class R1Jira(Case):
    DENY = [
        ACLI_W,
        "acli jira workitem transition --key P-1 --status Done",
        "acli jira workitem comment create --key P-1",
        "acli jira workitem create-bulk --from-csv x.csv",
        "acli jira workitem create view",
        "acli jira auth logout",
        "acli jira workitem create --project P --summary -h",
        "acli --profile p jira workitem create --summary x",
        "acli jira workitem --json create",
        "acli jira workitem create --summary list-sprints",
        "acli jira board create-sprint x",
        "acli jira workitem link create --out A-1 --in A-2 --type Blocks",
        "acli jira workitem link delete --id 5",
        "acli jira project restore --key P",
        "acli admin user delete",
        "acli jira filter change-owner --id 1",
        "acli rovodev run",
        "acli jira workitem cre --summary x",
        "acli jira workitem create --summary x -- --help",
        "acli jira workitem comment --key X-1",
        "acli jira workitem create auth login --summary s",
        "python3 -c \"import os; os.system('acli jira workitem create')\"",
        "node -e \"require('child_process').execSync('acli jira workitem delete --key X-1')\"",
        "awk 'BEGIN{system(\"acli jira workitem transition --key X-1\")}'",
    ]
    ALLOW = [
        "acli jira workitem view PROJ-1 --json",
        'acli jira workitem search --jql "project = P AND text ~ \\"x\\""',
        "acli jira workitem comment list --key P-1",
        "acli jira workitem view --help",
        "acli --version 2>&1 | head -1",
        "acli",
        "acli jira auth status",
        "acli jira auth login",
        'acli jira workitem view "$KEY"',
        "acli version",
        "acli jira board list-sprints --id 7",
        "acli jira sprint list-workitems --sprint 3 --json",
        "acli jira workitem link list --key T-1",
        "acli jira workitem link type",
        "acli help jira workitem create",
        'acli jira workitem search --jql "project = X" --json | qa-mem inbox .',
        "grep -rn 'acli jira workitem create' skills",
    ]

    def test_acli_reads_only(self):
        self.table(self.DENY, True)
        self.table(self.ALLOW, False)

    def test_jira_mcp_reads_only(self):
        for t in (
            "mcp__atlassian__getJiraIssue",
            "mcp__atlassian__searchJiraIssuesUsingJql",
            "mcp__atlassian__fetch",
            "mcp__atlassian__atlassianUserInfo",
            "mcp__mcp-atlassian__jira_batch_get_changelogs",
            "mcp__mcp-atlassian__jira_download_attachments",
            "mcp__corp__getJiraIssue",
            "mcp__grafana__query",
            "mcp__claude_ai_Gmail__send_message",
        ):
            self.assertIsNone(tool(t, issueIdOrKey="X-1"), t)
        for t in (
            "mcp__atlassian__createJiraIssue",
            "mcp__mcp-atlassian__jira_batch_create_issues",
            "mcp__atlassian__getJiraIssueWithEdit",
            "mcp__atlassian__lookupAndDelete",
            "mcp__corp__JIRA_create_issue",
            "mcp__jira__batch_get_changelogs_and_delete",
            "mcp__tools__confluence_update_page",
            "mcp__atlassian__addCommentToJiraIssue",
        ):
            for agent in ("", EX):
                self.assertIsNotNone(tool(t, agent, issueIdOrKey="X-1"), (t, agent))


class R2Hosts(Case):
    DENY = [
        "playwright-cli -s=qa goto https://prod.traffic.example/form",
        "playwright-cli open https://evil.com",
        "playwright-cli -s=qa tab-new evil.com",
        "npx playwright-cli goto https://evil.com",
        f"cd x && {EVIL}",
        "playwright-cli eval \"location.href='https://evil.com'\"",
        "playwright-cli goto https://qa.traffic.example.evil.com",
        "playwright-cli -s qa goto evil.com",
        "playwright-cli goto file:///etc/passwd",
        "npx @playwright/cli goto https://evil.com",
        "npx playwright-cli@latest goto https://evil.com",
        "playwright-cli goto 'https://evil.com\\@qa.traffic.example'",
        "playwright-cli goto https://evil.com@qa.traffic.example/",
        "playwright-cli goto https://qa.traffic.example@evil.com/",
        "playwright-cli goto https://u:p@evil.com",
        "playwright-cli goto about:srcdoc",
        "playwright-cli goto data:text/html,hi",
        "playwright-cli goto 'javascript://localhost/%0Alocation.href=1'",
        "playwright-cli goto view-source:http://localhost/",
        "playwright-cli goto 'https://evil.com%23.qa.traffic.example'",
        "playwright-cli -s=qa route x && " + EVIL,
        "playwright-cli tab-new --isolated-context anon https://evil.com",
        "playwright-cli tab-new --isolated-context localhost evil.com",
        "playwright-cli -s=qa eval \"location.href='//evil.com'\"",
        "playwright-cli -s=qa eval 'fetch(`https://evil.com/api`)'",
        "playwright-cli -s=qa eval \"window.open('https://evil.com')\"",
        "playwright-cli -s=qa eval 'location.assign(`//evil.com/x`)'",
        f"python3 {Q} browser-open https://evil.com",
        "qa-mem browser-open --headed evil.com",
        "qa-mem browser-open -s https://evil.com",
        "qa-mem browser-open 'https://evil.com@qa.traffic.example/'",
        "qa-mem browser-open -- javascript:alert(1)",
        "playwright-cli -s=qa open --config /tmp/c.json",
        "playwright-cli -s=qa open --config=/tmp/c.json",
        "playwright-cli -s=qa open --extension",
        "playwright-cli -s=qa open --cdp=http://localhost:9222",
        "playwright-cli -s=qa open --endpoint ws://localhost:1234",
        "npx @playwright/cli open --extension",
        "playwright-cli attach x",
        "playwright-cli install",
        "playwright-cli -s=qa install --skills --global",  # a workspace config, or skills in .claude
        "playwright-cli show --port 9000 --host 0.0.0.0",
        "playwright-cli show --host=192.168.1.5",
        "playwright-cli show --host 0.0.0.0 -s -h",
        "playwright-cli -s=qa open https://evil.example -- --help",
        "playwright-cli -s=qa goto https://evil.com --help false",
        "playwright-cli -s=qa --raw true goto https://evil.com",
        "playwright-cli -s -h goto https://evil.com",
        "playwright-cli --filename --help goto https://evil.com",
        "playwright-cli -s=qa --status 500 route '**'",
        # env vars playwright-cli reads, in a call that runs it
        "PLAYWRIGHT_MCP_USER_DATA_DIR=/tmp/o playwright-cli -s=qa open",
        "env PLAYWRIGHT_MCP_USER_DATA_DIR=/tmp/o playwright-cli open",
        "export PLAYWRIGHT_MCP_USER_DATA_DIR=/tmp/o; playwright-cli -s=qa open",
        "declare -x PLAYWRIGHT_MCP_EXTENSION=1; playwright-cli open",
        "export 'PLAYWRIGHT_MCP_CONFIG'=/tmp/c.json && playwright-cli -s=qa open",
        "PWTEST_CLI_GLOBAL_CONFIG=/tmp playwright-cli open",
        "set -a; PLAYWRIGHT_MCP_CDP_ENDPOINT=http://localhost:9222; playwright-cli -s=qa open",
        "bash -c 'PLAYWRIGHT_MCP_CONFIG=/tmp/c.json playwright-cli -s=qa open'",
        f"PLAYWRIGHT_MCP_USER_DATA_DIR=/tmp/o python3 {Q} browser-open https://qa.traffic.example/",
        # --profile: only `open`, only the main profile, spelled out
        "playwright-cli -s=qa open --profile /tmp/other",
        f"playwright-cli -s=qa open --profile={PROF}",
        f"playwright-cli -s=qa open --profile {MAIN}/Default",
        f"playwright-cli -s=qa goto --profile {MAIN} https://qa.traffic.example/",
        f"playwright-cli -s=qa open --profile {MAIN} https://evil.com",
        "playwright-cli -s=qa open --user-data-dir /tmp/x",
        "playwright-cli -s=qa open --user-data-dir=~/.config/google-chrome",
        'playwright-cli -s=qa open --profile "$X"',
        f"playwright-cli -s=qa open --profile {PROF}/ma*",
        "playwright-cli -s=qa open --profile",
        f"P={MAIN}; playwright-cli open --profile $P",
        f"cd {PROF} && playwright-cli -s=qa open --profile main",
        f"playwright-cli -s=qa open --profile {MAIN} --profile /tmp/x",
        "playwright-cli -s=qa open --profile ~/.local/share/qa-sdet/profiles/main/Default",
    ]
    EVAL_URL_DENY = [  # found in audit: a fetched URL dressed as a CSS attribute selector was skipped as one
        "fetch([u = 'https://evil.example.com/x'][0])",
        "document.querySelectorAll('a'); fetch([h = 'https://evil.example.com/'][0])",
        "document.querySelectorAll(`${fetch([u = 'https://evil.example.com/'][0])}`)",
    ]

    def test_a_url_dressed_as_a_selector_is_still_checked(self):
        self.table([f"playwright-cli -s=qa --raw eval {shlex.quote(js)}" for js in self.EVAL_URL_DENY], True)

    ALLOW = [
        "playwright-cli -s=qa goto https://qa.traffic.example/form",
        "playwright-cli -s=qa goto https://api.qa.traffic.example/x",
        "playwright-cli -s=qa goto http://localhost:8000/form.html",
        'playwright-cli --session qa --raw eval "document.title"',
        "playwright-cli -s=qa goto https://api.qa.traffic.example/x 2>&1",
        "playwright-cli goto http://[::1]:8765/",
        "playwright-cli goto https://qa.traffic.example./x",
        "playwright-cli -s=qa goto about:blank",
        "playwright-cli -s=qa fill e12 'https://example.org/x'",
        "playwright-cli -s=qa eval \"fetch('//qa.traffic.example/api')\"",
        "playwright-cli -s=qa --raw eval \"fetch('//localhost:8765/api/x')\"",
        "playwright-cli -s=qa --raw eval 'document.evaluate(\"//td[2]\", document, null, 9, null).singleNodeValue'",
        "playwright-cli -s=qa --raw eval \"document.createElementNS('http://www.w3.org/2000/svg','svg').namespaceURI\"",
        "playwright-cli -s=qa --raw eval \"[...document.links].filter(a => a.href.startsWith('https://docs.example.org/')).length\"",
        "playwright-cli -s=qa --raw eval \"location.origin === 'https://other.example.com'\"",
        "playwright-cli -s=qa --raw eval "
        + shlex.quote("[...document.querySelectorAll('a[href^=\"https://docs.x.org/\"]')].length"),
        "playwright-cli -s=qa --raw eval "
        + shlex.quote('document.querySelectorAll(\'a[target="_blank"][href*="https://docs.x.org"]\').length'),
        "playwright-cli -s=qa --raw eval " + shlex.quote("!!document.querySelector(`a[href$='https://docs.x.org/a']`)"),
        "playwright-cli -s=qa goto 'https://qa.traffic.example?email=a@b.com'",
        "playwright-cli -s=qa goto 'https://qa.traffic.example/a%20b'",
        "playwright-cli show",
        "playwright-cli show --port 9000 --host localhost",
        "playwright-cli -s=qa goto --help",
        "playwright-cli route --help | head -30",
        "playwright-cli --help open 2>&1 | head -30",
        "playwright-cli goto https://evil.com --help",
        "playwright-cli -s=qa --raw eval true",
        "playwright-cli -s=qa tab-new --isolated-context anon https://qa.traffic.example/admin",
        "playwright-cli install-browser firefox",
        "export FOO=1; playwright-cli -s=qa snapshot",
        "export PLAYWRIGHT_MCP_HEADLESS=1",
        f"python3 {Q} browser-open https://qa.traffic.example/form",
        "qa-mem browser-open --session=qa-login --headed https://qa.traffic.example/",
        "qa-mem browser-open",
        'echo https://github.com/x; playwright-cli -s=qa goto "https://qa.traffic.example/$P"',
        # --profile with the main profile, on open
        f"playwright-cli -s=qa open --browser=chrome --profile {MAIN} https://qa.traffic.example/",
        f"playwright-cli -s=qa open --profile={MAIN}",
        f"playwright-cli -s=qa open --profile {MAIN}/",
        f"playwright-cli --session qa open --profile '{MAIN}' --headed",
        f"playwright-cli -s=qa-login open --headed --profile {MAIN}/../main http://localhost:8765/",
        f"playwright-cli open --user-data-dir {MAIN}",
        "playwright-cli -s=qa open --profile ~/.local/share/qa-sdet/profiles/main",
        "playwright-cli open --profile=$HOME/.local/share/qa-sdet/profiles/main",
        'playwright-cli --session qa open --profile "${HOME}/.local/share/qa-sdet/profiles/main/"',
        f"env -u PLAYWRIGHT_MCP_STORAGE_STATE playwright-cli -s=qa open --profile {MAIN}",
    ]

    # a new browser without the main profile: the main session may, the explorer navigates its own browser with goto
    BARE_OPEN = [
        "playwright-cli -s=qa open --browser=firefox",
        "playwright-cli -s qa open --browser firefox http://localhost:8765/",
        "env -u PLAYWRIGHT_MCP_STORAGE_STATE playwright-cli -s=qa open --browser chrome",
    ]

    def test_navigation_and_browser_sources(self):
        self.table(self.DENY, True)
        self.table(self.ALLOW, False)
        self.table(self.BARE_OPEN, False, agents=(False,))
        self.table(self.BARE_OPEN, True, agents=(True,))

    def test_config_decides_hosts(self):
        try:
            for bad in ({"allowedHosts": None}, {"allowedHosts": "qa.traffic.example"}):  # broken: no navigation at all
                CFG.write_text(json.dumps(bad))
                self.assertIsNone(bash("ls"))
                self.assertIsNotNone(bash("playwright-cli goto https://qa.traffic.example/"), bad)
            CFG.write_text("{oops")
            self.assertIsNotNone(bash("playwright-cli goto https://qa.traffic.example/"))
            CFG.write_text(json.dumps({"allowedHosts": ["qa.traffic.example"]}))  # localhost only when listed
            for cmd in ("playwright-cli goto http://localhost:631/", "playwright-cli goto http://127.0.0.1:8080/"):
                self.assertIsNotNone(bash(cmd), cmd)
        finally:
            CFG.write_text(json.dumps({"allowedHosts": ["qa.traffic.example", "localhost"]}))

    def test_xdg_profile_folder(self):
        xdg = TMP / "xdg"
        with mock.patch.dict(os.environ, XDG_DATA_HOME=str(xdg)):
            self.assertIsNotNone(bash(f"cat {xdg}/qa-sdet/profiles/main/Default/Cookies"))
            self.assertIsNone(bash(f"playwright-cli -s=qa open --profile {xdg}/qa-sdet/profiles/main"))
            self.assertIsNotNone(bash(f"playwright-cli -s=qa open --profile {xdg}/qa-sdet/profiles/other"))


class R3Secrets(Case):
    PW_DENY = [
        "cookie-list",
        "cookie-get sid",
        "localstorage-list",
        "localstorage-get t",
        "sessionstorage-list",
        "sessionstorage-get t",
        "request 1",
        "request-headers 2",
        "response-headers 3",
        "tracing-start",
        "state-save",
        "state-save /tmp/s.json",
        "--session=qa state-save --filename=x.json",
        "run-code 'async page => 1'",
        "run-code --filename x.js",
        "run-code -- --help",
        "delete-data",
        "--raw true cookie-list",
    ]
    JS_DENY = [
        "document.cookie",
        "document['cookie']",
        "cookieStore.getAll()",
        "JSON.stringify(localStorage)",
        "localStorage.getItem('t')",
        "Object.entries(sessionStorage)",
        "({...localStorage})",
        "localStorage",
        "() => window.sessionStorage",
        "localStorage.authToken",
        "JSON.parse(localStorage.user).token",
        "Object.values(localStorage)",
        "localStorage['t']",
        "Reflect.get(document,'cookie')",
        "(({cookie}) => cookie)(document)",
        "const d=document; d.cookie",
        "with (document) { cookie }",  # found in audit: `with` names cookie without document.cookie
        "const d = document; with (d) fetch('/api?c=' + cookie)",
        "window['localStorage'].getItem('t')",
        "indexedDB.open('firebaseLocalStorageDb')",
        "Object.getOwnPropertyDescriptor(Document.prototype, 'cookie').get.call(document)",
        "var xin = 1; xin" + "\n" * 250 + "localStorage.token",
        "'k' in" + " " * 250 + "localStorage",
        " " * 5000 + "localStorage.t",
    ]
    JS_ALLOW = [
        "localStorage.clear()",
        "Object.keys(localStorage)",
        "localStorage.key(0) + sessionStorage.length",
        "document.body.innerText.includes('localStorage')",
        "localStorage.length",
        "localStorage.setItem('qa', '1')",
        "typeof localStorage",
        "for (const k in localStorage) {}",
        "document.cookie.length",
        "'token' in localStorage",
        "document.body.innerText.includes('cookie')",
        "document.querySelector('.cookie-banner').innerText",
        "document.title.startsWith('Sign in with (SSO)')",
        "!!document.querySelector('#cookie')",
        "indexedDB.databases()",
        "Object.keys(localStorage).length",
        "document.body.innerHTML",
    ]
    ck, rel = str(CK), "~/.local/share/qa-sdet/profiles/main"
    PATH_DENY = [
        f"cat {ck}",
        f"cat '{ck}'",
        f'head "{ck}"',
        f"cp -r {MAIN} /tmp/x",
        f"cp {ck} /tmp/c",
        f"tar czf /tmp/p.tgz {MAIN}",
        f"tar czf /tmp/p.tgz {D}",
        f"zip -r /tmp/p.zip {PROF}",
        f"rsync -a {D}/ /tmp/bak",
        f"sqlite3 {ck} .dump",
        f"sqlite3 -readonly {ck} 'select * from cookies'",
        f"find {PROF}",
        f"ls {MAIN}",
        f"ls -R {D}",
        f"tree {D}",
        f"du -a {D}",
        f"stat {ck}",
        f"xxd {ck}",
        f"base64 {ck}",
        f"grep -r token {D}",
        f"grep -a SID {ck}",
        f"cat {MAIN}/../main/Default/Cookies",
        f"cat {D}//profiles/./main/Default/Cookies",
        f"cat {D}/prof*/main/Default/Cookies",
        f"cat {D}/*/*/Default/Cookies",
        f"cat {D}/**/Cookies",
        f"cd {MAIN} && cat Default/Cookies",
        f"cd {MAIN} && ls",
        f"cd {PROF}; tar c .",
        f"env -C {MAIN} cat Default/Cookies",
        f"dd if={ck} of=/tmp/x",
        f"cat < {ck}",
        f"jq . --rawfile c {ck}",
        f"diff --from-file={ck} /dev/null",
        f"python3 -c \"print(open('{ck}').read())\"",
        f"python3 - <<'EOF'\nprint(open('{ck}').read())\nEOF",
        f"node -e \"require('fs').readFileSync('{ck}')\"",
        f"bash -c 'cat {ck}'",
        f"echo $(cat {ck})",
        f"rm -rf {MAIN}",
        f"mv {MAIN} /tmp/m",
        f"mkdir -p {MAIN}/x",
        f"cat {rel}/Default/Cookies",
        "cat $HOME/.local/share/qa-sdet/profiles/main/Default/Cookies",
        'cat "${HOME}/.local/share/qa-sdet/profiles/main/Default/Cookies"',
        "tar c ~/.local/share/qa-sdet",
        "grep -r sid ~/.local/share",
        f"cp -r {rel} .",
        f"ln -s {D} /tmp/q",
        f"ln -sf -t /tmp {D}",
        f"git -C {D} add -A",
        f"cd {D} && git init && git add .",
        "cd ~/.local/share && git add qa-sdet",
        f"python3 -c \"open(__import__('os').path.expanduser('{rel}/Default/Cookies'))\"",
        "find ~/.local -name Cookies -exec cat {} +",
        f"find {D.parent} -name Cookies -execdir base64 {{}} \\;",
        f"tar czf /tmp/x.tgz -C {HOME} .local",
        f"tar czf /tmp/x.tgz --directory={HOME} .local/share",
    ]
    PATH_ALLOW = [
        f"echo {MAIN}",
        f"test -d {MAIN} && echo yes",
        f"[ -d {MAIN} ]",
        f"ls {D}",
        f"ls -la {D}",
        f"find {D} -name '*.db'",
        f"sqlite3 -readonly {D}/app.db .tables",
        'grep -rn "qa-sdet/profiles" skills agents',
        f'grep -n "{MAIN}" README.md',
        "ls -R src",
        "cd ~ && git status",
        f"git commit -m 'guard: protect {PROF}'",
        "ln -s /tmp/a /tmp/b",
        f"echo $(printf %s {MAIN})",
        "find ~/.local -name '*.db'",
        "tar czf /tmp/s.tgz -C skills .",
    ]

    def test_playwright_secret_verbs_and_page_js(self):
        self.table([f"playwright-cli -s=qa {v}" for v in self.PW_DENY], True)
        self.table(
            [
                "playwright-cli -s=qa requests",
                "playwright-cli -s=qa response-body 3",
                "playwright-cli -s=qa tracing-stop",
                "playwright-cli -s=qa requests --filter /api/",
                "playwright-cli -s=qa screenshot --type png --filename=qa-out/r/evidence/x.png",
            ],
            False,
        )
        self.table([f"playwright-cli -s=qa --raw eval {shlex.quote(js)}" for js in self.JS_DENY], True)
        self.table([f"playwright-cli -s=qa --raw eval {shlex.quote(js)}" for js in self.JS_ALLOW], False)
        self.table(["playwright-cli -s=qa --raw eval '() => `${document.cookie}`'"], True)

    def test_value_options_of_video_show_actions_do_not_hide_the_verb(self):
        """`--cursor none` and the other values are options' values, not the verb: else `--duration 300 state-save` read as verb 300."""
        for opt in ("--cursor none", "--position top-right", "--duration 300", "--cursor=none", "--duration=300"):
            self.table([f"playwright-cli -s=qa {opt} state-save", f"playwright-cli -s=qa {opt} cookie-list"], True)
        self.table(
            [
                "playwright-cli -s=qa video-show-actions --position top-right --duration 800 --highlight-style 'outline: 3px solid red'"
            ],
            False,
        )
        self.assertEqual(
            gt.pw_args(["-s=qa", "--cursor", "none", "--position", "top", "--duration", "9", "video-show-actions"]),
            ["video-show-actions"],
        )

    EXPLORER_ONLY_DENY = [
        "webmcp-call lookup",
        "cookie-clear",
        "cookie-delete sid",
        "state-load /tmp/s.json",
        "--raw webmcp-call lookup",
        "--session=qa cookie-delete sid",
    ]

    def test_explorers_cannot_wipe_the_login_or_call_page_tools(self):
        cmds = [f"playwright-cli -s=qa {v}" for v in self.EXPLORER_ONLY_DENY]
        self.table(cmds, True, cwd=RUN, agents=(True,))
        self.table(cmds[:4], False, cwd=RUN, agents=(False,))  # the main session keeps them (the user's own call)
        self.table(
            ["playwright-cli -s=qa webmcp-list", "playwright-cli -s=qa cookie-set a b"], False, cwd=RUN, agents=(True,)
        )

    def test_a_module_frame_is_read_by_eval_on_a_frame_target_not_run_code(self):
        """An app shell loads its pages in a cross-origin iframe `document` can't reach: the way in is eval with a
        frame-piercing target (qa-mem crawl's too); the frame's own cookie stays closed and run-code stays denied."""
        frame = "'iframe >> internal:control=enter-frame >> :root'"
        check = "\"el => { const document = el.ownerDocument; return !!document.querySelector('[data-testid=x]'); }\""
        allow = [
            f"playwright-cli -s=qa --raw eval {check} {frame}",
            f"playwright-cli -s=qa goto 'https://qa.traffic.example/zones'; playwright-cli -s=qa --raw eval "
            f"\"!!document.querySelector('#menu')\"; playwright-cli -s=qa --raw eval {check} {frame}",
        ]
        deny = [
            f'playwright-cli -s=qa --raw eval "el => el.ownerDocument.cookie" {frame}',
            'playwright-cli -s=qa run-code "async page => page.frames()[1].content()"',
        ]
        for agent in ("qa-sdet:qa-explorer",):
            for cmd in allow:
                self.assertIsNone(tool("Bash", agent, RUN, command=cmd), (agent, cmd))
            for cmd in deny:
                self.assertIsNotNone(tool("Bash", agent, RUN, command=cmd), (agent, cmd))

    def test_profile_folder_stays_closed(self):
        self.table(self.PATH_DENY, True)
        self.table(self.PATH_ALLOW, False, cwd=ROOT)

    def test_file_tools_and_the_profile(self):
        for agent in ("", EX):
            for name, tin in [
                ("Read", {"file_path": str(CK)}),
                ("Grep", {"pattern": "sid", "path": str(MAIN)}),
                ("Glob", {"pattern": "**/*", "path": str(MAIN)}),
                ("Glob", {"pattern": "profiles/main/**", "path": str(D)}),
                ("Glob", {"pattern": "*/main/**", "path": str(D)}),
                ("Glob", {"pattern": "**/Cookies", "path": str(D)}),
                ("Glob", {"pattern": "p*/**", "path": str(D)}),
                ("Edit", {"file_path": str(CK), "old_string": "a"}),
                ("Write", {"file_path": f"{MAIN}/x.json"}),
                ("LS", {"path": str(MAIN)}),
                ("Grep", {"pattern": "sid", "path": str(D), "output_mode": "content"}),
                ("Grep", {"pattern": "sid", "path": str(HOME), "output_mode": "content"}),
                ("Glob", {"pattern": "**/Cookies", "path": str(HOME / ".local")}),
                ("Glob", {"pattern": "**/profiles/**", "path": str(HOME)}),
            ]:
                self.assertIsNotNone(tool(name, agent, **tin), (name, tin, agent))
        self.assertIsNone(tool("Glob", pattern="**/*.md", path=str(ROOT)))
        self.assertIsNone(tool("Read", file_path=str(DB)))
        self.assertIsNone(tool("Write", file_path=f"{PROJ}/notes.md", content="x"))

    def test_uploads_never_send_secrets(self):
        cwd = project()
        Path(cwd, ".env").write_text("X=1")
        Path(cwd, "qa-out/r/.data").mkdir(parents=True)
        Path(cwd, "notes.txt").write_text("x")
        self.table(
            [
                f"playwright-cli -s=qa upload {f}"
                for f in ("~/.ssh/id_rsa", f"{cwd}/.env", DB, CFG, "./.en*", "qa-out/*.csv")
            ]
            + ["playwright-cli -s=qa drop e5 --path .env", "playwright-cli -s=qa drop e5 --path=.env"],
            True,
            cwd,
        )
        self.table(
            ["playwright-cli -s=qa upload qa-out/r/.data/in.csv", "playwright-cli -s=qa drop e5 --path notes.txt"],
            False,
            cwd,
        )


if __name__ == "__main__":
    unittest.main()
