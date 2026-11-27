"""Browser signals (qa_sdet.signals): `signals-triage`, `body-check` and the report's unexplained signals."""

import json
import subprocess
import unittest
from unittest import mock

from support import PLAN1, RUN1, TEMPLATES, Rendering, cli, done

from qa_sdet import memory, report, signals

# verbatim playwright-cli 0.1.24 output of `requests --all` and `console error --all` (fixture map.html, then form.html)
REQUESTS_LOG = """### Result
2. [GET] http://localhost:8765/api/incidents.json => [200] OK
3. [GET] http://localhost:8765/api/closures.json => [404] File not found
4. [GET] http://localhost:8765/api/incidents.json => [200] OK
5. [GET] http://localhost:8765/api/closures.json => [404] File not found
6. [GET] http://localhost:9/api/x?token=abc123secret => [FAILED] net::ERR_UNSAFE_PORT

Note: 2 static requests not shown, run with --static option to see them.
"""
CONSOLE_LOG = """### Result
Total messages: 7 (Errors: 6, Warnings: 1)
Returning 6 messages for level "error"

[ERROR] Failed to load resource: the server responded with a status of 404 (File not found) @ http://localhost:8765/favicon.ico:1
[ERROR] Failed to load resource: the server responded with a status of 404 (File not found) @ http://localhost:8765/api/closures.json:1
[ERROR] Zone save failed: 500 @ :1
[ERROR] Failed to load resource: net::ERR_UNSAFE_PORT @ http://localhost:9/api/x?token=abc123secret:1
TypeError: rows is undefined
    at eval (eval at <anonymous> (eval at evaluate (:370:30)), <anonymous>:1:35)
"""


class Signals(Rendering):
    """Console errors and failed requests the explorer saved before closing the browser (.data/*-<slug>.log)."""

    def signals_run(self, run=RUN1, requests=REQUESTS_LOG, console=CONSOLE_LOG, drafts=(), probes=None):
        self.config(releaseTemplates=TEMPLATES)
        d = self.rundir(
            "2026-11-23-PROJ-1", plan={**PLAN1, "questions": []}, runs=[("form", run)]
        )  # nothing else to act on
        for kind, text in (("requests", requests), ("console", console)):
            if text is not None:
                (d / f".data/{kind}-form.log").write_text(text)
        for i, text in enumerate(drafts, 1):
            (d / f"bugs/BUG-{i}.md").write_text(text)
        if probes is not None:  # what `qa-mem api-probe` notes of its own requests
            (d / ".data/probes-form.jsonl").write_text("".join(json.dumps(x) + "\n" for x in probes))
        for c in run.get("cases") or []:  # every checked case has its image: only signals are under test
            (d / f"evidence/{c['id']}.png").write_bytes(b"png")
        report.write_report(d)
        return (d / "REPORT.md").read_text()

    def test_unexplained_signals_are_listed_once_with_counts(self):
        r = self.signals_run()
        self.assertIn(
            "## Signals no bug explains (4)\n\n"
            "- /form: GET /api/closures.json → 404 (×2)\n"
            "- /form: GET /api/x → FAILED net::ERR_UNSAFE_PORT\n"
            "- /form: console: Zone save failed: 500\n"
            "- /form: console: TypeError: rows is undefined\n",
            r,
        )
        for gone in ("incidents.json", "Failed to load resource", "favicon", "at eval", "abc123secret"):
            self.assertNotIn(gone, r)  # 2xx, network lines the requests log carries, stack lines, a token
        self.assertIn("\n\n✅ PASS · ", r)  # signals never change a verdict
        self.assertIn("1. Look into the 1 severe browser signal no bug explains", r)  # the failed request

    def test_a_request_the_explorers_own_probe_sent_is_no_app_signal(self):
        """Found live: `qa-mem api-probe` to another allowed host failed on CORS, and the report listed its request and
        its console error as new severe app signals; the page's own 26 calls of that endpoint had all answered 200."""
        url = "https://api.qa.example/lock/status"
        ok, failed = f"[GET] {url} => [200] OK", f"[GET] {url} => [FAILED] net::ERR_FAILED"
        console = f"[ERROR] Access to fetch at '{url}' from origin 'https://qa.example' has been blocked by CORS policy: x @ https://qa.example/form:0\n"
        probe = {"method": "GET", "url": url, "status": "FAILED"}
        log = lambda *lines: "".join(f"{i}. {x}\n" for i, x in enumerate(lines, 1))
        r = self.signals_run(requests=log(ok, failed, ok), console=console, probes=[probe])
        self.assertNotIn("Signals no bug explains", r)
        self.assertNotIn("severe browser signal", r)
        r = self.signals_run(requests=log(ok, failed, ok), console=console, probes=[])  # the page's own failure
        self.assertIn("- /form: GET /lock/status → FAILED net::ERR_FAILED\n", r)
        self.assertIn("- /form: console: Access to fetch at", r)
        r = self.signals_run(requests=log(failed, failed, failed), console="", probes=[probe])
        self.assertIn(
            "- /form: GET /lock/status → FAILED net::ERR_FAILED (×2)\n", r
        )  # one was the probe's, two the page's

    def test_a_signal_the_run_or_a_bug_draft_names_is_explained(self):
        r = self.signals_run(
            run={**RUN1, "notes": "The toast says zone save failed: 500, as the ticket expects."},
            drafts=["## Evidence\n- Network: `GET /api/closures.json` → 404"],
        )
        sec = r[r.index("## Signals no bug explains") : r.index("## Exploration notes")]
        self.assertIn(
            "## Signals no bug explains (2)\n\n"
            "- /form: GET /api/x → FAILED net::ERR_UNSAFE_PORT\n"
            "- /form: console: TypeError: rows is undefined\n",
            sec,
        )

    def test_a_path_alone_explains_no_failed_request(self):
        """The run's own target, or a path a passing note mentions, must not hide another call failing on it."""
        requests = (
            "1. [POST] http://localhost:8765/form => [500] Internal Server Error\n"
            "2. [DELETE] http://localhost:8765/api/zones => [500] Internal Server Error\n"
            "3. [PUT] http://localhost:8765/api/zones => [500] Internal Server Error\n"
            "4. [GET] http://localhost:8765/api/zones/7 => [404] Not Found\n"
        )
        r = self.signals_run(
            run={**RUN1, "notes": "GET /api/zones returns the list."},
            requests=requests,
            console=None,
            drafts=["## Evidence\n- Network: PUT http://localhost:8765/api/zones → 500"],
        )
        self.assertIn("- /form: POST /form → 500", r)
        self.assertIn("- /form: DELETE /api/zones → 500", r)
        self.assertIn("- /form: GET /api/zones/7 → 404", r)
        self.assertNotIn("PUT /api/zones", r)

    def test_passing_facts_and_locators_explain_no_failed_request(self):
        """A passing fact that mentions POST /form (or a locator for it) must not hide POST /form → 500."""
        requests = (
            "1. [POST] http://localhost:8765/form => [500] Internal Server Error\n"
            "2. [GET] http://localhost:8765/api/closures.json => [404] Not Found\n"
            "3. [GET] http://localhost:8765/api/old.json => [404] Not Found\n"
        )
        run = {
            **RUN1,
            "facts": [{"kind": "api", "text": "POST /form saves the zone"}],
            "locators": [{"label": "GET /api/closures.json", "locator": "getByText('GET /api/closures.json')"}],
            "cases": [
                {"id": "TC-1", "status": "PASS", "actual": "POST /form returned 200 earlier"},
                {"id": "TC-2", "status": "FAIL", "actual": "GET /api/old.json gives 404, as the case saw"},
            ],
            "bugs": [{"id": "BUG-1", "title": "x"}],
        }
        r = self.signals_run(run=run, requests=requests, console=None)
        self.assertIn("- /form: POST /form → 500", r)
        self.assertIn("- /form: GET /api/closures.json → 404", r)
        self.assertNotIn("→ 404 (\u00d71)", r)
        self.assertNotIn("/form: GET /api/old.json", r)  # a failed case names it: explained
        run["notes"] = "GET /api/closures.json is known to 404."
        self.assertNotIn("/form: GET /api/closures.json", self.signals_run(run=run, requests=requests, console=None))

    def test_a_draft_naming_an_id_placeholder_explains_the_records_request(self):
        requests = (
            "1. [GET] http://localhost:8765/api/zones/42 => [404] Not Found\n"
            "2. [DELETE] http://localhost:8765/api/zones/42 => [500] Error\n"
        )
        r = self.signals_run(
            requests=requests, console=None, drafts=["## Evidence\n- Network: GET /api/zones/:id → 404"]
        )
        self.assertIn("- /form: DELETE /api/zones/42 → 500", r)
        self.assertNotIn("GET /api/zones", r)

    def test_only_a_repeated_get_is_a_request_loop(self):
        self.config(releaseTemplates=TEMPLATES, allowedHosts=["localhost"])
        d = self.rundir("2026-11-23-PROJ-1", plan={**PLAN1, "questions": []}, runs=[("form", RUN1)])
        posts = "".join(f"{i}. [POST] http://localhost:8765/api/zones => [201] Created\n" for i in range(1, 8))
        gets = "".join(f"{i}. [GET] http://localhost:8765/api/zones/{i} => [200] OK\n" for i in range(8, 14))
        (d / ".data/requests-form.log").write_text(posts + gets)
        keys = [k for k, _, _ in signals.triage_signals(d, "form", RUN1)["app"]]
        self.assertEqual(keys, ["GET /api/zones/:id ×6 (the same call 6 times: a request loop?)"])

    def triage_dir(self, requests, console=None, run=RUN1, bodies=None, drafts=()):
        self.config(releaseTemplates=TEMPLATES, allowedHosts=["localhost"])
        d = self.rundir("2026-11-23-PROJ-1", plan={**PLAN1, "questions": []}, runs=[("form", run)])
        (d / ".data/requests-form.log").write_text(requests)
        if console is not None:
            (d / ".data/console-form.log").write_text(console)
        for i, text in enumerate(drafts, 1):
            (d / f"bugs/BUG-{i}.md").write_text(text)
        return d

    def test_body_verdict_reads_error_envelopes_html_and_leaks(self):
        v = signals.body_verdict
        self.assertEqual(v('{"data": {"zones": []}}'), [])
        self.assertEqual(v('{"errors": [{"message": "Forbidden"}], "data": null}'), ["error body: errors: Forbidden"])
        self.assertEqual(
            v('{"success": false, "message": "Zone name taken"}'), ["error body: success false: Zone name taken"]
        )
        self.assertEqual(v('{"error": "invalid input"}'), ["error body: error: invalid input"])
        self.assertEqual(v('{"ok": false}'), ["error body: ok false"])
        self.assertEqual(v('{"status": "error", "detail": "x"}'), ["error body: status error: x"])
        self.assertEqual(v('{"errors": [], "error": null, "success": true}'), [])  # empty or null means none
        self.assertEqual(v("<!doctype html><html><title>Sign in</title>", "/api/zones"), ["HTML on an API endpoint"])
        self.assertEqual(v("<!DOCTYPE html><html>", "/zones"), [])  # a page may be HTML
        self.assertEqual(v("<html>", "/graphql"), ["HTML on an API endpoint"])
        self.assertEqual(v('{"error": "x"}', status=422), [])  # a 4xx saying so is the right answer: leaks only
        for text, label in (
            ('Traceback (most recent call last):\n  File "/srv/app.py", line 4', "stack trace"),
            ("java.lang.NullPointerException\n\tat com.acme.Zone.save(Zone.java:42)", "stack trace"),
            ("at Object.<anonymous> (/srv/app/zones.js:42:11)", "stack trace"),
            ('{"error": "invalid input syntax for type integer: \\"x\\" SQLSTATE 22P02"}', "SQL error"),
            ("ORA-00942: table or view does not exist", "SQL error"),
            ("You have an error in your SQL syntax near", "SQL error"),
            ("Whitelabel Error Page", "framework debug page"),
            ("You're seeing this error because you have DEBUG = True", "framework debug page"),
            ("Server Error in '/' Application.", "framework debug page"),
        ):
            self.assertIn(f"leak: {label}", v(text, status=500), text)
        self.assertEqual(
            v("at https://app.example/main.js:10:5", status=500), []
        )  # browser frames are the app's own JS

    def test_body_verdict_flags_a_mutations_errors_under_data(self):
        v = signals.body_verdict  # a GraphQL mutation reports its failure inside its own payload, in a 200
        failed = '{"data": {"saveNote": {"__typename": "SaveNotePayload", "note": null, "errors": [{"message": "Too long"}]}}}'
        self.assertEqual(v(failed), ["error body: data.saveNote.errors: Too long"])
        self.assertEqual(v('{"data": {"saveNote": {"errors": [{"message": "Too long"}]}}}'), v(failed))
        self.assertEqual(
            v('{"data": {"zoneCreate": {"zone": {"id": 1}, "userErrors": [{"field": ["name"], "message": "Taken"}]}}}'),
            ["error body: data.zoneCreate.userErrors: Taken"],
        )
        self.assertEqual(v(failed, status=422), [])  # a 4xx saying so is the right answer
        for fine in (
            '{"data": {"importJob": {"id": 7, "status": "done", "errors": ["row 3: bad date"]}}}',  # a record's column
            '{"data": {"saveNote": {"note": {"id": 1}, "errors": []}}}',
            '{"data": {"saveNote": {"note": {"id": 1}, "userErrors": []}}}',
            '{"data": {"saveNote": null}}',
            '{"data": {"notes": [{"errors": ["x"]}]}}',
            '{"data": [1, 2]}',
        ):
            self.assertEqual(v(fine), [], fine)

    def test_a_partial_failure_counts_only_on_a_write(self):
        v = signals.body_verdict
        bulk = '{"data": {"bulkUpdate": {"updated": 2, "failed": 1, "results": [{"ok": true}, {"ok": false, "reason": "Locked"}]}}}'
        self.assertEqual(v(bulk), [])  # in a read, failed is data (a count of failed jobs)
        self.assertEqual(v(bulk, write=True), ["partial failure: data.bulkUpdate.failed 1"])
        self.assertEqual(
            v('{"imported": 3, "items": [{"ok": true}, {"success": false, "message": "Duplicate"}]}', write=True),
            ["partial failure: items: 1 of 2 failed: Duplicate"],
        )
        self.assertEqual(v('{"failedCount": 2}', write=True), ["partial failure: failedCount 2"])
        for fine in (
            '{"failed": 0, "results": [{"ok": true}]}',
            '{"failed": true}',
            '{"failures": [1]}',
            '{"items": [1]}',
        ):
            self.assertEqual(v(fine, write=True), [], fine)

    def test_a_rest_write_is_checked_for_a_partial_failure_and_a_read_is_not(self):
        d = self.triage_dir(
            "1. [POST] http://localhost:8765/api/zones/bulk => [200] OK\n"
            "2. [GET] http://localhost:8765/api/jobs/stats => [200] OK\n"
        )
        _, out, _ = cli("body-check", str(d), "--slug", "form", "1", stdin='{"updated": 2, "failed": 1}')
        self.assertIn("partial failure: failed 1", out)
        _, out, _ = cli("body-check", str(d), "--slug", "form", "2", stdin='{"succeeded": 40, "failed": 3}')
        self.assertIn("nothing suspicious", out)

    GQL_LOG = "".join(f"{i}. [POST] http://localhost:8765/graphql => [200] OK\n" for i in range(1, 5))

    def test_a_clean_graphql_check_clears_only_that_call(self):  # every operation shares one method and path
        d = self.triage_dir(self.GQL_LOG)
        cli("body-check", str(d), "--slug", "form", "1", stdin='{"data": {"notes": []}}')
        self.assertEqual(signals.triage_signals(d, "form", RUN1)["suspect_200"], [("POST /graphql", "2")])
        for n in "234":
            cli("body-check", str(d), "--slug", "form", n, stdin='{"data": {"notes": []}}')
        self.assertEqual(signals.triage_signals(d, "form", RUN1)["suspect_200"], [])

    def sweep(self, d, responses, requests=None, log=None):
        """(exit code, stdout, the playwright-cli calls) of `qa-mem body-check --all` with a fake playwright-cli that
        holds these bodies by request number."""
        calls = []

        def fake(cmd, **kw):
            if cmd[2:] == ["requests", "--all"]:  # the sweep saves the log itself: None = the browser is closed
                return done(1, err="Browser 'qa' is not open") if log is None else done(0, out="### Result\n" + log)
            self.assertEqual(cmd[:3], ["playwright-cli", "-s=qa", "--raw"])
            calls.append(f"{cmd[3]} {cmd[4]}")
            held = responses if cmd[3] == "response-body" else (requests or {})
            if cmd[4] not in held:
                return done(1, err=f"Request #{cmd[4]} not found. Run requests to see available numbers.")
            return done(0, out=held[cmd[4]])

        with mock.patch.object(subprocess, "run", side_effect=fake):
            code, out, err = cli("body-check", str(d), "--slug", "form", "--all")
        return code, out + err, calls

    SENT = {
        "1": '{"operationName": "Notes", "query": "query Notes { notes { id } }"}',
        "2": '{"operationName": "SaveNote", "query": "mutation SaveNote { saveNote { note { id } errors { message } } }"}',
        "3": '{"query": "mutation BulkUpdate { bulkUpdate { updated failed } }"}',
        "9": '{"operationName": "RefreshToken", "query": "mutation RefreshToken { refreshToken { jwt } }"}',
    }
    GOT = {
        "1": '{"data": {"notes": []}}',
        "2": '{"data": {"saveNote": {"note": null, "errors": [{"message": "Too long"}]}}}',
        "3": '{"data": {"bulkUpdate": {"updated": 2, "failed": 1}}}',
        "4": '{"access_token": "abcdefabcdefabcdef"}',
        "5": '{"error": "a read is no candidate"}',
        "6": '{"ok": false}',
        "7": "PG::SyntaxError: ERROR: syntax error at or near",
        "8": '{"error": "another host"}',
        "9": '{"data": {"refreshToken": {"jwt": "abcdefabcdefabcdef"}}}',
    }
    MIXED_LOG = (
        "1. [POST] http://localhost:8765/graphql => [200] OK\n"
        "2. [POST] http://localhost:8765/graphql => [200] OK\n"
        "3. [POST] http://localhost:8765/graphql => [200] OK\n"
        "4. [POST] http://localhost:8765/api/auth/token => [200] OK\n"
        "5. [GET] http://localhost:8765/api/zones => [200] OK\n"
        "6. [PUT] http://localhost:8765/api/zones/7 => [200] OK\n"
        "7. [POST] http://localhost:8765/api/zones => [500] Internal Server Error\n"
        "8. [POST] https://cdn.example.com/rpc => [200] OK\n"
        "9. [POST] http://localhost:8765/graphql => [200] OK\n"
    )

    def test_body_check_all_reads_every_call_triage_would_ask_for(self):
        d = self.triage_dir(self.MIXED_LOG)
        code, out, calls = self.sweep(d, self.GOT, self.SENT)
        self.assertEqual(code, 0, out)
        # never a sign-in path, a read, another host, or the response of an operation named like a sign-in
        self.assertEqual(
            sorted(c for c in calls if c.startswith("response-body")), [f"response-body {n}" for n in "12367"]
        )
        self.assertEqual(
            sorted(c for c in calls if c.startswith("request-body")), [f"request-body {n}" for n in "1239"]
        )
        self.assertIn("#2 POST /graphql#SaveNote → 200: error body: data.saveNote.errors: Too long", out)
        self.assertIn("#3 POST /graphql#BulkUpdate → 200: partial failure: data.bulkUpdate.failed 1", out)
        t = signals.triage_signals(d, "form", RUN1)
        self.assertEqual(
            [k for k, _, _ in t["app"]],
            [
                "POST /graphql#SaveNote → 200 [error body: data.saveNote.errors: Too long]",
                "POST /graphql#BulkUpdate → 200 [partial failure: data.bulkUpdate.failed 1]",
                "PUT /api/zones/7 → 200 [error body: ok false]",
                "POST /api/zones → 500 [leak: SQL error]",
            ],
        )
        self.assertEqual(t["suspect_200"], [])  # the sign-in operation is settled unread
        self.assertNotIn("abcdefabcdef", (d / ".data/bodies-form.json").read_text())
        # the baseline keeps one row per operation; a note explains the operation it names, not every GraphQL call
        self.assertEqual(memory.signal_key(t["app"][0][0]), ("request", "POST /graphql#SaveNote 200"))
        run = {**RUN1, "notes": "POST /graphql#SaveNote refuses a long text: expected."}
        self.assertEqual(len(signals.triage_signals(d, "form", run)["app"]), 3)
        self.assertEqual(len(signals.triage_signals(d, "form", {**RUN1, "notes": "POST /graphql is fine"})["app"]), 4)

    def test_body_check_all_is_capped_and_says_what_is_left(self):
        d = self.triage_dir("".join(f"{i}. [POST] http://localhost:8765/graphql => [200] OK\n" for i in range(1, 36)))
        code, out, calls = self.sweep(d, {str(i): '{"data": {}}' for i in range(1, 36)})
        self.assertEqual((code, sum(c.startswith("response-body") for c in calls)), (0, signals.SWEEP_CALLS))
        self.assertIn("5 left", out)
        self.assertEqual(signals.triage_signals(d, "form", RUN1)["suspect_200"], [("POST /graphql", "31")])

    def test_body_check_all_leaves_a_request_the_browser_dropped_as_a_suspect(self):
        d = self.triage_dir(self.GQL_LOG)
        code, out, _ = self.sweep(d, {"1": '{"data": {}}', "3": '{"data": {}}', "4": '{"data": {}}'})
        self.assertEqual(code, 0, out)
        self.assertIn("1 not read (Request #2 not found", out)
        self.assertEqual(signals.triage_signals(d, "form", RUN1)["suspect_200"], [("POST /graphql", "2")])

    def test_a_binary_body_playwright_saved_to_a_file_is_removed_not_read(self):
        dump = self.proj / ".playwright-cli" / "response-2026-11-25T10-00-00-000Z.png"
        dump.parent.mkdir()
        dump.write_bytes(b"\x89PNG")
        self.assertEqual(signals._saved_body(f"{dump}\n"), "")
        self.assertFalse(dump.exists())
        keep = self.proj / "response-notes.txt"
        keep.write_text("x")
        for text in ('{"data": {}}', str(keep), f"see {dump}", ""):
            self.assertEqual(signals._saved_body(text), text)
        self.assertTrue(keep.exists())

    def test_an_empty_body_is_not_judged_clean(self):  # the browser drops a response's body when the page navigates
        d = self.triage_dir(self.GQL_LOG + "5. [DELETE] http://localhost:8765/api/zones/9 => [204] No Content\n")
        bad = '{"data": {"saveNote": {"note": null, "errors": [{"message": "Too long"}]}}}'
        code, out, _ = self.sweep(d, {"1": "\n", "2": "", "3": bad, "4": '{"data": {}}', "5": ""})
        self.assertEqual(code, 0, out)
        self.assertIn("3 bodies read, 1 with a finding, 2 with no body left", out)  # the 204 has none to read: read
        recorded = json.loads((d / ".data/bodies-form.json").read_text())
        self.assertEqual((sorted(k for k in recorded if k.isdigit()), recorded["gone"]), (["3", "4", "5"], ["1", "2"]))
        t = signals.triage_signals(d, "form", RUN1)
        self.assertEqual((len(t["app"]), t["suspect_200"], t["unchecked"]), (1, [], []))  # nothing left to ask for
        code, out, _ = cli("body-check", str(d), "--slug", "form", "1", stdin="\n")  # by hand: the same answer
        self.assertEqual(code, 0)
        self.assertIn("no body left", out)
        self.assertNotIn("nothing suspicious", out)

    def test_body_check_all_saves_the_request_log_itself(self):  # one command right after a write, before a reload
        d = self.rundir("2026-11-23-PROJ-1", plan={**PLAN1, "questions": []}, runs=[("form", RUN1)])
        self.config(releaseTemplates=TEMPLATES, allowedHosts=["localhost"])
        code, out, calls = self.sweep(
            d, {"1": '{"ok": false}'}, log="1. [PUT] http://localhost:8765/api/zones/7 => [200] OK\n"
        )
        self.assertEqual((code, calls), (0, ["response-body 1"]), out)
        self.assertIn("#1 PUT /api/zones/7 → 200: error body: ok false", out)
        self.assertIn("1. [PUT]", (d / ".data/requests-form.log").read_text())

    def test_body_check_all_needs_the_requests_log(self):
        d = self.rundir("2026-11-23-PROJ-1", plan={**PLAN1, "questions": []}, runs=[("form", RUN1)])
        code, out, calls = self.sweep(d, {})
        self.assertEqual((code, calls), (2, []))
        self.assertIn("requests-form.log", out)

    def test_suspect_200s_are_graphql_rpc_and_writes_without_auth_paths(self):
        d = self.triage_dir(
            "1. [POST] http://localhost:8765/graphql => [200] OK\n"
            "2. [POST] http://localhost:8765/api/auth/token => [200] OK\n"
            "3. [GET] http://localhost:8765/api/zones => [200] OK\n"
            "4. [PUT] http://localhost:8765/api/zones/7 => [200] OK\n"
            "5. [PUT] http://localhost:8765/api/zones/8 => [200] OK\n"
            "6. [POST] http://localhost:8765/api/profile => [200] OK\n"
            "7. [DELETE] http://localhost:8765/api/zones/9 => [204] No Content\n"
            "8. [POST] http://localhost:8765/api/jobs => [200] OK\n"
            "9. [POST] https://cdn.example.com/rpc => [200] OK\n"
        )
        t = signals.triage_signals(d, "form", RUN1)
        self.assertEqual(t["app"], [])  # nothing is a signal until a body says so
        self.assertEqual(
            t["suspect_200"], [("POST /graphql", "1"), ("PUT /api/zones/:id", "4"), ("DELETE /api/zones/:id", "7")]
        )
        run = {**RUN1, "notes": "PUT /api/zones/:id saves the zone, body checked."}
        self.assertEqual(
            [k for k, _ in signals.triage_signals(d, "form", run)["suspect_200"]],
            ["POST /graphql", "DELETE /api/zones/:id", "POST /api/jobs"],
        )  # a named call is cleared, the next one fills in

    def test_a_checked_body_makes_its_call_a_severe_signal_and_a_clean_one_clears_it(self):
        d = self.triage_dir(
            "1. [POST] http://localhost:8765/graphql => [200] OK\n"
            "2. [POST] http://localhost:8765/api/zones => [200] OK\n"
            "3. [POST] http://localhost:8765/api/zones => [500] Internal Server Error\n"
        )
        code, out, _ = cli("body-check", str(d), "--slug", "form", "1", stdin='{"errors": [{"message": "Forbidden"}]}')
        self.assertEqual(code, 0, out)
        self.assertIn("error body: errors: Forbidden", out)
        code, out, _ = cli("body-check", str(d), "--slug", "form", "2", stdin='{"id": 7, "name": "qa-sdet-z"}')
        self.assertIn("nothing suspicious", out)
        cli("body-check", str(d), "--slug", "form", "3", stdin="PG::SyntaxError: ERROR: syntax error at or near")
        t = signals.triage_signals(d, "form", RUN1)
        self.assertEqual(
            [k for k, _, _ in t["app"]],
            ["POST /graphql → 200 [error body: errors: Forbidden]", "POST /api/zones → 500 [leak: SQL error]"],
        )
        self.assertEqual(t["suspect_200"], [])
        self.assertEqual([signals.signal_severity(k) for k, _, _ in t["app"]], ["severe", "severe"])
        code, out, _ = cli("signals-triage", str(d), "--slug", "form")
        self.assertLess(out.index("SEVERE"), out.index("#1 POST /graphql → 200"))
        self.assertNotIn("SUSPECT 200", out)

    def test_body_check_text_is_scrubbed_and_a_missing_request_still_checks(self):
        d = self.triage_dir("1. [POST] http://localhost:8765/graphql => [200] OK\n")
        code, out, _ = cli(
            "body-check",
            str(d),
            "--slug",
            "form",
            "1",
            stdin='{"errors": [{"message": "bad apiKey=sk-12345678901234567890 for x"}]}',
        )
        self.assertNotIn("12345678901234567890", out)
        self.assertNotIn("12345678901234567890", (d / ".data/bodies-form.json").read_text())
        code, out, _ = cli("body-check", str(d), "--slug", "form", "99", stdin="<!doctype html>")
        self.assertEqual(code, 0)
        self.assertIn("nothing suspicious", out)  # no such request in the log: no path to call it an API
        code, _, err = cli("body-check", str(d), "--slug", "form", "x", stdin="{}")
        self.assertEqual(code, 2)

    def test_triage_groups_severe_signals_before_client_errors(self):
        d = self.triage_dir(
            "1. [GET] http://localhost:8765/api/closures.json => [404] Not Found\n"
            "2. [POST] http://localhost:8765/form => [500] Error\n"
            "3. [POST] http://localhost:8765/graphql => [200] OK\n",
            console='[ERROR] column "x" does not exist SQLSTATE 42703 @ :1\n[ERROR] plain failure @ :1\n',
        )
        code, out, _ = cli("signals-triage", str(d), "--slug", "form")
        self.assertEqual(code, 0)
        sev, client, other, suspect = (out.index(w) for w in ("SEVERE", "CLIENT 4xx", "OTHER", "SUSPECT 200"))
        self.assertTrue(
            sev
            < out.index("#2 POST /form → 500")
            < client
            < out.index("#1 GET /api/closures.json → 404")
            < other
            < suspect
            < out.index("#3 POST /graphql")
        )
        self.assertIn('console: column "x" does not exist SQLSTATE 42703 [leak: SQL error]', out.split("CLIENT 4xx")[0])
        self.assertIn("qa-mem body-check", out)

    def pass_draft(self, requests, console=None, run=RUN1, **kw):
        d = self.triage_dir(requests, console, run=run, **kw)
        for c in run.get("cases") or []:
            (d / f"evidence/{c['id']}.png").write_bytes(b"png")
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        return d, r.split("## What to do")[1].split("\n## ")[0], (d / "jira-comment.txt").read_text()

    def test_a_pass_draft_warns_about_unexplained_severe_signals_and_the_verdict_stays(self):
        d, check, txt = self.pass_draft(
            "1. [POST] http://localhost:8765/form => [500] Internal Server Error\n"
            "2. [GET] http://localhost:8765/api/closures.json => [404] Not Found\n",
            console="[ERROR] TypeError: rows is undefined @ :1\n",
        )
        self.assertIn(
            "   - 1 severe browser signal on its pages no bug explains (a 5xx, a failed request, an error body or a "
            "leak): a pass over them may hide a bug. Read Signals no bug explains first.\n",
            check,
        )
        self.assertIn(
            "   - 2 other unexplained browser signals (4xx, console errors, request loops): see Signals no bug explains.\n",
            check,
        )
        r = (d / "REPORT.md").read_text()
        self.assertIn("- /form: POST /form → 500\n", r.split("## Signals no bug explains (3)")[1])  # named there, once
        self.assertNotIn("signal", txt)  # the comment stays clean
        self.assertIn("\n\n✅ PASS · ", r)  # signals never change a verdict

    def test_a_pass_draft_has_no_signal_warning_when_the_run_names_the_call_or_the_user_ignored_it(self):
        _, check, _ = self.pass_draft(
            "1. [POST] http://localhost:8765/form => [500] Error\n",
            run={**RUN1, "notes": "POST /form 500 is the known fixture error, bug BUG-9."},
        )
        self.assertNotIn("signal", check)
        ignored = {("/form", "request", "POST /form 500"): ("IGNORED", 2, "r0")}
        with mock.patch.object(signals, "signal_baseline", return_value=ignored):
            _, check, _ = self.pass_draft("1. [POST] http://localhost:8765/form => [500] Error\n")
        self.assertNotIn("signal", check)

    def test_a_checked_error_body_and_a_leak_reach_the_pass_draft(self):
        d = self.triage_dir("1. [POST] http://localhost:8765/graphql => [200] OK\n")
        cli("body-check", str(d), "--slug", "form", "1", stdin='{"errors": [{"message": "Forbidden"}]}')
        for c in RUN1.get("cases") or []:
            (d / f"evidence/{c['id']}.png").write_bytes(b"png")
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        self.assertIn("- /form: POST /graphql → 200 [error body: errors: Forbidden]\n", r)
        self.assertIn("   - 1 severe browser signal on its pages no bug explains", r)  # under Post the Jira comment

    def test_third_party_failures_fold_into_one_line_and_request_loops_show(self):
        self.config(releaseTemplates=TEMPLATES, allowedHosts=["localhost"])
        d = self.rundir("2026-11-23-PROJ-1", plan={**PLAN1, "questions": []}, runs=[("form", RUN1)])
        loop = "".join(f"{i}. [GET] http://localhost:8765/api/zones/{i} => [200] OK\n" for i in range(4, 10))
        (d / ".data/requests-form.log").write_text(
            "1. [POST] http://localhost:8765/form => [500] Error\n"
            "2. [GET] https://cdn.example.com/lib.js => [404] Not Found\n"
            "3. [GET] https://tiles.example.org/1/2/3.png => [FAILED] net::ERR\n" + loop
        )
        report.write_report(d)
        sec = (d / "REPORT.md").read_text().split("## Signals no bug explains")[1].split("\n## ")[0]
        self.assertIn("- /form: POST /form → 500", sec)
        self.assertIn("- /form: GET /api/zones/:id ×6 (the same call 6 times: a request loop?)", sec)
        self.assertNotIn("cdn.example.com", sec.split("<details>")[0])
        self.assertIn("<summary>2 third-party signal(s) (hosts outside allowedHosts)</summary>", sec)
        code, out, _ = cli("signals-triage", str(d), "--slug", "form")
        self.assertEqual(code, 0)
        self.assertIn("#1 POST /form → 500", out)  # the index for `response-body 1`
        self.assertIn("2 third-party", out)

    def test_no_logs_or_only_explained_ones_leave_nothing_to_act_on(self):
        for requests, console, drafts in (
            (None, None, ()),
            ("3. [GET] http://h/api/closures.json => [404] Not found\n", None, ["GET /api/closures.json → 404"]),
        ):
            r = self.signals_run(requests=requests, console=console, drafts=drafts)
            self.assertNotIn("Signals no bug explains", r)
            self.assertNotIn("signal", r.split("## What to do")[1].split("\n## ")[0])
            # a draft no run file lists is still shown (the signal it explains is not)
            self.assertEqual("Earlier bugs this run did not see" in r, bool(drafts))
            self.assertNotIn("Problems reading this folder", r)

    def test_page_text_in_a_message_stays_text(self):
        r = self.signals_run(
            requests=None, console="[ERROR] <img src=x onerror=alert(1)> [go](https://evil.example) | x @ :1\n"
        )
        self.assertIn("- /form: console: &lt;img src=x onerror=alert(1)> [go]\\(https://evil.example) \\| x\n", r)

    def test_a_long_list_is_cut_and_says_where_the_rest_is(self):
        r = self.signals_run(requests=None, console="".join(f"[ERROR] row {i} failed @ :1\n" for i in range(30)))
        self.assertIn("## Signals no bug explains (30)\n\n", r)
        self.assertIn(
            "- /form: console: row 14 failed\n- +15 more: see `requests-*.log` and `console-*.log` in `.data/`\n", r
        )
        self.assertNotIn("row 15 failed", r)


if __name__ == "__main__":
    unittest.main()
