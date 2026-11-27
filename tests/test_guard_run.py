"""The guard's run rules: nobody changes the data folder (R5); the run marker gates faults and API writes (R6)."""

import json
import os
import shlex
import time
import unittest
from pathlib import Path
from unittest import mock

from guard_support import (
    DB,
    EVIL,
    EX,
    HOME,
    PROJ,
    RUN,
    Case,
    D,
    Q,
    bash,
    decision,
    gp,
    marker,
    project,
    restore_env,
    tool,
    use_env,
)

setUpModule, tearDownModule = use_env, restore_env  # this module's tests run in the guard's sandbox


class R5DataFolder(Case):
    share = D.parent
    DENY = [
        f"rm -rf {D}",
        f"rm {DB}",
        f"sqlite3 {DB} 'DELETE FROM bugs'",
        f"find {D} -delete",
        f"rm -rf {share}",
        f"rm -rf {D}*",
        f"cp /dev/null {DB}",
        f": > {DB}",
        f"sudo -u root rm -rf {D}",
        f"timeout -s KILL 5 rm -rf {D}",
        f"cd {share} && rm -rf qa-sdet",
        f"dd if=/dev/zero of={DB}",
        f"echo x>{DB}",
        f": >| {DB}",
        f"cp -t {D} /tmp/x",
        f"echo x | tee {DB} /dev/null",
        f"sqlite3 'file:{DB}' 'DELETE FROM bugs'",
        f"cd {share} && (cd /tmp); rm -rf qa-sdet",
        f"cd -P {share} && rm -rf qa-sdet",
        f"find {share} -name 'app.db*' -delete",
        f"find {share} -iname '*.DB' -delete",
        f"trash-put {D}",
        f"gio trash {D}",
        f"playwright-cli -s=qa screenshot --filename={D}/x.png",
        f"playwright-cli -s=qa video-start {D}/v.webm",
        f"mv {D} /tmp/x",
        f"mv {share} /tmp/x",
        f"mv x.db {D}/default.db",
        f"mv -t /tmp {D}",
        f"sed -i 's/a/b/' {DB}",
        f"perl -pi -e 's/a/b/' {DB}",
        f"python3 -c \"open('{DB}', 'w')\"",
        f"rsync -a --delete /tmp/empty/ {share}/",
        f"tar xzf a.tgz -C {D}",
        f"unzip -o a.zip -d {D}",
        f"cd {D} && tar xf /tmp/a.tar",
        f"echo x 2>&1 >{DB}",
        "playwright-cli -s=qa delete-data",
        f"rm -rf ${{QA_NO_SUCH_VAR:-{D}}}",
        f"rm -rf {HOME}/.*",
        f"rm -rf {share}/qa-*",
        f"rm {D}/*.db",
        f"mv {share}/q* /tmp",
        f"rm {share}/*/*.db",
        f"rm -rf {D} \\",
        f"curl -o {DB} https://qa.traffic.example/x",
        f"wget -qO {DB} https://qa.traffic.example/x",
        f"curl -sc {DB} https://qa.traffic.example/x",
    ]
    ALLOW = [
        "rm -rf qa-out/form/evidence",
        f"sqlite3 -readonly {DB} .tables",
        f"find {D} -name '*.db'",
        f"grep -c rm {DB}",
        f"cp {DB} /tmp/backup.db",
        f"cd {D} && ls",
        "mv report.pdf ~/",
        "mv report.pdf ~",
        f"mv a b {HOME}",
        f"rm -f {HOME}/*.log",
        f"find {HOME} -name '*.pyc' -delete",
        f"grep -c '>' {D}/notes.txt",
        f"mv {HOME}/*.pdf /tmp/docs/",
        f"trash {HOME}/*.zip",
        f"rm -r {HOME}/screenshot-*",
        f"rm -rf {HOME}/*",
        "sed -i 's/a/b/' README.md",
        "rsync -a --delete build/ /tmp/site/",
        "find . -iname '*.pyc' -delete",
        "tar xzf a.tgz -C /tmp/out",
        "unzip -o a.zip -d out",
        "dd if=/dev/zero of=/tmp/x count=1",
        "rm -rf ${QA_NO_SUCH_VAR:-/tmp/qa-x}/build",
        "X=/tmp/build; rm -rf $X",
        "rm -rf /tmp/qa-x \\",
    ]

    def test_data_folder_stays_intact(self):
        self.table(self.DENY, True)
        self.table(self.ALLOW, False)
        self.table(["rm -f *.tmp", "find . -name '*.pyc' -delete"], False, cwd=HOME)
        self.table(["rm -rf ../qa-sdet"], True, cwd=D / "sub")
        with mock.patch.dict(os.environ, QA_TEST_DIR=str(self.share)):
            self.table(['rm -rf "$QA_TEST_DIR/qa-sdet"'], True)

    def test_write_tools(self):
        for agent in ("", EX):
            self.assertIsNotNone(tool("Write", agent, file_path=f"{D}/x.db", content="x"), agent)
            self.assertIsNotNone(tool("Edit", agent, file_path=str(DB), old_string="a", new_string="b"), agent)


class R6Marker(Case):
    def test_browser_commands_are_never_counted_or_capped(self):  # every run goes until the agent's verdict
        cwd = project(budget=2, used=2, deadline=time.time() - 5, max_turns=1)  # keys an older marker may still hold
        before = Path(cwd, "qa-out/.active").read_text()
        for cmd in (
            "playwright-cli -s=qa goto http://localhost:1/a",
            " && ".join(["playwright-cli -s=qa click e1"] * 9),
            "for b in ID Road; do playwright-cli -s=qa click e$b; done",
            "snap() { playwright-cli -s=qa snapshot; }; snap; snap",
            "for u in a b; do qa-mem browser-open https://qa.traffic.example/$u; done",
            "qa-mem api-probe GET https://qa.traffic.example/api/zones",
        ):
            for explorer in (False, True):
                self.assertIsNone(bash(cmd, cwd, explorer), cmd)
        self.assertEqual(Path(cwd, "qa-out/.active").read_text(), before)
        self.assertIsNotNone(bash(EVIL, cwd))  # the host check holds

    def test_a_call_with_a_denied_part_is_denied(self):
        self.assertIsNotNone(bash(f"playwright-cli -s=qa click e1 && rm -rf {D}", RUN))
        self.assertIsNotNone(bash("qa-mem browser-open https://evil.com", RUN))
        self.assertIsNone(bash('t=$(playwright-cli -s=qa --raw eval "document.title")', RUN))
        self.assertIsNone(bash(f"python3 {Q} browser-open https://qa.traffic.example/", RUN))

    def test_the_explorer_saves_its_signals_into_its_run_folder(self):  # step 9
        for cmd in (
            f"playwright-cli -s=qa requests --all > '{RUN}/qa-out/r/.data/requests-form.log'",
            f"playwright-cli -s=qa console error --all > '{RUN}/qa-out/r/.data/console-form.log'",
        ):
            self.assertIsNone(bash(cmd, RUN, True), cmd)

    def test_the_explorer_has_a_scratch_folder_in_its_run(self):
        """A snapshot to grep, a throwaway screenshot: explorers kept saving those to /tmp or .data/ and were denied.
        <run>/scratch/ takes any file; teardown empties it."""
        for cmd in (
            f"playwright-cli -s=qa snapshot > '{RUN}/qa-out/r/scratch/page.yml'",
            f"playwright-cli -s=qa screenshot --filename='{RUN}/qa-out/r/scratch/look.png'",
            f"playwright-cli -s=qa snapshot > '{RUN}/qa-out/r/scratch/page.yml'; grep -n Save '{RUN}/qa-out/r/scratch/page.yml'",
        ):
            self.assertIsNone(bash(cmd, RUN, True), cmd)
            self.assertEqual(decision(cmd, agent_type="qa-explorer"), "allow", cmd)
        for cmd in (
            "playwright-cli -s=qa snapshot > /tmp/page.yml",
            f"playwright-cli -s=qa screenshot --filename='{RUN}/qa-out/r/.data/look.png'",
            f"playwright-cli -s=qa snapshot > '{RUN}/qa-out/r/scratch/deep/page.yml'",  # one level, like the others
            f"playwright-cli -s=qa snapshot > '{RUN}/qa-out/scratch/page.yml'",  # in a run, not beside the runs
        ):
            self.assertIn("writes only", bash(cmd, RUN, True) or "", cmd)
        self.assertIsNone(tool("Write", "qa-explorer", RUN, file_path=f"{RUN}/qa-out/r/scratch/notes.txt", content="x"))

    def test_page_start_and_evidence_are_hosts_checked(self):
        cwd = project()
        self.assertIn("non-allowlisted host", bash("qa-mem page-start https://evil.com/", cwd, True))
        self.assertIsNone(bash("qa-mem page-start https://qa.traffic.example/form", cwd, True))
        self.assertIsNone(
            bash(f"qa-mem evidence '{cwd}/qa-out/r/evidence/TC-1.png' --selector '#msg' --label 'ok'", cwd, True)
        )
        self.assertIsNone(bash(f"qa-mem evidence {cwd}/qa-out/r/evidence/BUG-2.png --ref e5 --label x", cwd))

    def test_faults_gate(self):
        cwd = project()
        for cmd in ("playwright-cli -s=qa route '**/api/**'", "playwright-cli -s=qa network-state-set offline"):
            self.assertIsNotNone(bash(cmd, cwd), cmd)  # no marker: no faults
        for m, ok in (
            ({"faults": False}, False),
            ({"faults": True, "expires": time.time() - 1}, False),
            ({"faults": True}, True),
        ):
            marker(cwd, **m)
            self.assertEqual(bash("playwright-cli -s=qa route '**/api/**'", cwd) is None, ok, m)
        with mock.patch.dict(os.environ, CLAUDE_PROJECT_DIR=str(cwd)):  # the marker found via the project folder
            self.assertIsNone(bash("playwright-cli -s=qa route '**/api/**'", project()))

    def test_api_probe_is_hosts_checked_and_header_checked(self):
        cwd = project()
        url = "https://qa.traffic.example/api/zones"
        for explorer in (False, True):
            self.assertIn(
                "non-allowlisted host", bash("qa-mem api-probe GET https://evil.com/api/zones", cwd, explorer)
            )
            self.assertIn(
                "non-allowlisted host",
                bash("qa-mem api-probe GET https://qa.traffic.example@evil.com/api", cwd, explorer),
            )
            for h in ("Authorization: Bearer x", "cookie: sid=1", "  Proxy-Authorization : x", "AUTHORIZATION:x"):
                for form in (f"--header '{h}'", f"--header='{h}'"):
                    self.assertIn("refused", bash(f"qa-mem api-probe GET {url} {form}", cwd, explorer) or "", h)
            self.assertIsNotNone(bash("qa-mem api-probe GET", cwd, explorer))  # no URL to check
            self.assertIsNotNone(bash("qa-mem api-probe GET $URL", cwd, explorer))
        for cmd in (
            f"qa-mem api-probe GET {url}",
            f"qa-mem api-probe head {url}/1",
            f"qa-mem api-probe OPTIONS {url}",
            f"qa-mem api-probe GET {url} --header 'Accept: application/json' --header 'X-Trace: 1'",
            f"python3 {Q} api-probe GET {url}",
        ):
            self.assertIsNone(bash(cmd, cwd, True), cmd)
        for ok in (
            '--json \'{"name": "qa-sdet-a"}\'',
            '--json \'{"url": "https://evil.com/x"}\'',  # a body is data, not a URL
            "--content-type text/plain",
        ):
            self.assertIsNone(bash(f"qa-mem api-probe GET {url} {ok}", cwd, True), ok)

    def test_api_probe_writes_need_the_run_flag(self):
        url, body = "https://qa.traffic.example/api/zones", '--json \'{"name": "qa-sdet-z"}\''
        writes = ("POST", "put", "PATCH", "DELETE")
        for why, m in (
            ("no marker", None),
            ("flag off", {"api_writes": False}),
            ("flag absent", {}),
            ("expired", {"api_writes": True, "expires": time.time() - 5}),
        ):
            cwd = project()
            if m is not None:
                marker(cwd, **m)
            for explorer in (False, True):
                for method in writes:
                    self.assertIn(
                        "--api-writes",
                        bash(f"qa-mem api-probe {method} {url}/1 {body}", cwd, explorer) or "",
                        (why, method),
                    )
            for method in ("GET", "HEAD", "OPTIONS"):
                self.assertIsNone(bash(f"qa-mem api-probe {method} {url}", cwd, True), (why, method))
        cwd = project(api_writes=True)
        for method in writes:
            self.assertIsNone(bash(f"qa-mem api-probe {method} {url}/1 {body}", cwd, True), method)
        self.assertIsNotNone(
            bash(f"qa-mem api-probe DELETE https://evil.com/api/zones/1 {body}", cwd, True)
        )  # the host check holds
        self.assertIsNotNone(bash(f"qa-mem api-probe POST {url} --header 'Cookie: a=b' {body}", cwd, True))

    def test_the_explorer_cannot_turn_on_api_writes(self):  # only the coordinator's marker may say it
        for cmd in (
            f"qa-mem dispatch '{RUN}/qa-out/r' --route /x --api-writes",
            f"qa-mem marker start '{RUN}' --api-writes",
            f"python3 {Q} marker start . --api-writes",
            'echo \'{"expires": 9, "api_writes": true}\' > qa-out/.active',
            'sed -i \'s/"api_writes": false/"api_writes": true/\' qa-out/.active',
            "tee qa-out/.active < /tmp/m",
        ):
            self.assertIsNotNone(bash(cmd, RUN, explorer=True), cmd)
        cwd = project(api_writes=False)  # a marker the explorer can't change keeps writes off
        self.assertIn(
            "--api-writes", bash("qa-mem api-probe DELETE https://qa.traffic.example/api/zones/1", cwd, True) or ""
        )
        self.assertIsNone(bash(f"qa-mem marker start '{RUN}' --api-writes", RUN))  # the main session may

    EVAL_WRITES = [
        "fetch('/api/zones', {method: 'POST', body: '{}'})",
        'fetch(u,{method:"put"})',
        "fetch(u, { 'method' : `PATCH` })",
        'fetch(u, {"method":"DELETE", credentials: \'include\'})',
        "fetch (u, {headers: h, method: 'Post'})",
        "fetch(new Request(u, {method: 'DELETE'}))",
        "(async()=>{ const o = {}; o.method = 'POST'; await fetch(u, o) })()",
        "fetch(u, {method\n:\n'post'})",
        "var x = new XMLHttpRequest(); x.open('GET', u); x.send()",
        "navigator.sendBeacon('/api/log', 'x')",
        "navigator . sendBeacon(u)",
        "new window.XMLHttpRequest()",
    ]
    EVAL_OK = [
        "fetch(u).then(r => r.headers.get('content-type'))",
        "fetch(u, {method: 'GET'}).then(r => r.status)",
        "fetch(u, {method: 'head'}).then(r => r.headers.get('etag'))",
        "fetch(u, {method: 'OPTIONS'}).then(r => r.headers.get('allow'))",
        "document.querySelector('form').requestSubmit()",
        "document.querySelector('form').noValidate = true",
        "document.querySelector('[name=speed]').removeAttribute('max')",
        "document.body.innerText.includes('POST')",
        "[...document.forms].map(f => f.method)",
    ]

    def test_raw_eval_writes_are_denied_for_every_role_even_with_api_writes(self):
        cwd = project(api_writes=True)
        for js in self.EVAL_WRITES:
            for verb in ("eval", "--raw eval"):
                cmd = f"playwright-cli -s=qa {verb} {shlex.quote(js)}"
                for explorer in (False, True):
                    self.assertIn("qa-mem api-probe", bash(cmd, cwd, explorer) or "", cmd)
        self.assertIn(
            "qa-mem api-probe", bash(f"playwright-cli -s=qa --raw eval {shlex.quote(self.EVAL_WRITES[0])}", PROJ) or ""
        )
        self.table([f"playwright-cli -s=qa --raw eval {shlex.quote(js)}" for js in self.EVAL_OK], False, cwd=cwd)

    def test_malformed_markers_are_no_run(self):
        cwd = project(faults=True)
        ms = int(time.time() * 1000)
        for text in (
            json.dumps({"expires": ms + 7200000}),  # epoch milliseconds: it would never expire
            "{oops",
            json.dumps({"faults": True}),
            json.dumps({"expires": "soon"}),
            "[1]",
            '{"expires": Infinity}',
            '{"expires": NaN}',
            json.dumps({"expires": -1}),
            json.dumps({"expires": time.time() + 600, "faults": "yes"}),
            json.dumps({"expires": time.time() + 600, "api_writes": "yes"}),
            json.dumps({"expires": time.time() + 600, "api_writes": 1}),
        ):
            Path(cwd, "qa-out/.active").write_text(text)
            self.assertEqual(gp.read_marker(str(cwd)), (None, None), text)
            self.assertIsNotNone(bash("playwright-cli -s=qa route '**/api/**'", cwd), text)  # no run: no faults


if __name__ == "__main__":
    unittest.main()
