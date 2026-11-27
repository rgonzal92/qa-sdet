"""`qa-mem api-probe` (qa_sdet.probe): one request from the signed-in page, its write, header and body rules."""

import json
import os
import subprocess
import time
import unittest
from unittest import mock

from support import SESSION_CFG, Base, cli, done

from qa_sdet import probe


class ApiProbe(Base):
    """`qa-mem api-probe <METHOD> <url>`: one same-origin fetch in the signed-in browser, scrubbed and capped."""

    CFG = SESSION_CFG
    URL = "https://qa.example.com/api/zones"

    def setUp(self):
        super().setUp()
        self.config(**self.CFG)
        self.env = mock.patch.dict(os.environ, CLAUDE_PROJECT_DIR=str(self.proj))
        self.env.start()

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def marker(self, **fields):
        (self.proj / "qa-out").mkdir(exist_ok=True)
        m = {"expires": int(time.time()) + 3600, "faults": False, **fields}
        (self.proj / "qa-out/.active").write_text(json.dumps(m))

    def page(self, status=200, body="", headers=None, url=None, contains=None, **extra):
        """What the in-page script prints (--raw eval shows a string as a JSON string)."""
        r = {
            "status": status,
            "ok": 200 <= status < 300,
            "headers": headers or {},
            "body": body,
            "url": url or self.URL,
            **extra,
        }
        if contains is not None:
            r["contains"] = contains
        return done(0, out=json.dumps(json.dumps(r)) + "\n")

    def probe(self, *args, seq=()):
        with mock.patch.object(subprocess, "run", side_effect=list(seq)) as mrun:
            code, out, err = cli("api-probe", *args)
        return code, (json.loads(out) if out.strip() else None), err, mrun

    def test_a_probe_in_a_run_is_noted_for_the_reports_signals(self):
        """The browser logs a probe's request as it logs the page's: the run folder says which were the probe's."""
        failed = done(0, out=json.dumps(json.dumps({"error": "TypeError: Failed to fetch"})) + "\n")
        self.assertEqual(self.probe("GET", self.URL, seq=[failed])[0], 1)  # no run: nothing to note, no crash
        run = self.proj / "qa-out/2026-11-25-PROJ-1"
        (run / ".data").mkdir(parents=True)
        self.marker(run_dir=str(run), slug="form")
        self.assertEqual(self.probe("GET", self.URL, seq=[failed])[0], 1)
        self.assertEqual(self.probe("GET", self.URL, seq=[self.page(404)])[0], 0)
        noted = [json.loads(x) for x in (run / ".data/probes-form.jsonl").read_text().splitlines()]
        self.assertEqual(
            noted,
            [
                {"method": "GET", "url": self.URL, "status": "FAILED"},
                {"method": "GET", "url": self.URL, "status": 404},
            ],
        )

    def test_get_prints_status_safe_headers_and_a_scrubbed_body(self):
        body = json.dumps({"name": "qa-sdet-zone-1", "apiToken": "abc123", "speed": 130})
        page = self.page(
            200,
            body,
            {
                "content-type": "application/json",
                "etag": '"7"',
                "set-cookie": "sid=SECRET",
                "x-ratelimit-remaining": "9",
                "x-internal": "nope",
                "location": "/api/zones?access_token=zzz",
            },
        )
        code, out, _, mrun = self.probe("GET", self.URL, seq=[page])
        self.assertEqual(code, 0)
        self.assertEqual((out["status"], out["ok"]), (200, True))
        self.assertEqual(
            out["headers"],
            {
                "content-type": "application/json",
                "etag": '"7"',
                "x-ratelimit-remaining": "9",
                "location": "/api/zones?access_token=[REDACTED]",
            },
        )  # no set-cookie, no unknown name
        self.assertEqual(json.loads(out["body"]), {"name": "qa-sdet-zone-1", "apiToken": "[REDACTED]", "speed": 130})
        cmd = mrun.call_args[0][0]
        self.assertEqual(cmd[:4], ["playwright-cli", "-s=qa", "--raw", "eval"])
        for needle in ('"credentials": "include"', json.dumps(self.URL), '"method": "GET"'):
            self.assertIn(needle, cmd[4])
        for secret in ("cookie", "localStorage", "sessionStorage"):
            self.assertNotIn(secret, cmd[4].replace("set-cookie", ""))

    def test_every_value_reaches_the_page_as_a_json_string(self):
        evil = "\");alert(1);//\u2028'`${x}"
        url = "https://qa.example.com/api/zones?q=%22);alert(1);//"
        body = '{"name":"qa-sdet-' + evil + '"}'
        self.marker(api_writes=True)
        code, _, _, mrun = self.probe("POST", url, "--json", body, "--header", "X-Note: " + evil, seq=[self.page()])
        js = mrun.call_args[0][0][4]
        self.assertEqual(code, 0)
        for v in (url, body, evil, "X-Note"):
            self.assertIn(json.dumps(v)[1:-1], js)  # as the JSON-escaped text of a string literal
        self.assertNotIn(evil, js)  # never as raw text

    def test_a_non_json_body_is_scrubbed_and_capped(self):
        html = "<p>Authorization: Bearer abc.def.ghi</p>\n" + "x" * 6000
        code, out, _, _ = self.probe("GET", self.URL, seq=[self.page(500, html, {"content-type": "text/html"})])
        self.assertEqual((code, out["status"], out["ok"]), (0, 500, False))
        self.assertNotIn("abc.def.ghi", out["body"])
        self.assertLessEqual(len(out["body"]), probe.API_BODY_CAP)
        self.assertTrue(out["body_truncated"])
        self.assertEqual(out["headers"], {"content-type": "text/html"})

    def test_a_redirect_off_the_allowed_hosts_shows_no_body(self):
        code, out, _, _ = self.probe(
            "GET", self.URL, seq=[self.page(200, "<form>sign in</form>", url="https://login.example.org/x?a=1")]
        )
        self.assertEqual(code, 0)
        self.assertNotIn("body", out)
        self.assertEqual(out["redirected_to"], "login.example.org")

    def test_refusals_run_no_browser_command(self):
        cases = [
            ("a host that isn't allowed", ("GET", "https://evil.example.org/api/zones")),
            ("not http(s)", ("GET", "file:///etc/passwd")),
            ("a relative url", ("GET", "/api/zones")),
            ("a method it doesn't send", ("TRACE", self.URL)),
            ("a body on a GET", ("GET", self.URL, "--json", "{}")),
        ]
        for path in (
            "login",
            "logout",
            "signin",
            "sign-out",
            "oauth/token",
            "api/v1/session",
            "Auth",
            "sso",
            "saml",
            "password",
            "connect/token",
            "api/auth/me",
            "api/login.json",
        ):
            cases.append((f"the auth-like path {path}", ("GET", f"https://qa.example.com/{path}")))
        for name in ("Authorization", "cookie", "PROXY-AUTHORIZATION"):
            cases.append((f"a {name} header", ("GET", self.URL, "--header", f"{name}: x")))
        cases.append(("a header without a name", ("GET", self.URL, "--header", "no colon")))
        cases.append(("a header with a line break", ("GET", self.URL, "--header", "X-A: b\nC: d")))
        cases.append(
            (
                "two content types",
                ("POST", self.URL, "--json", "qa-sdet-", "--content-type", "text/plain", "--header", "content-type: x"),
            )
        )
        self.marker(api_writes=True)
        for why, args in cases:
            code, _, err, mrun = self.probe(*args)
            self.assertEqual(code, 2, why)
            mrun.assert_not_called()
        for ok in ("https://qa.example.com/api/authors", "https://qa.example.com/api/zones/sessions-report"):
            self.assertEqual(self.probe("GET", ok, seq=[self.page()])[0], 0, ok)

    def test_writes_need_the_run_flag_in_a_live_marker(self):
        body = '{"name":"qa-sdet-zone-1"}'
        for why, setup in (
            ("no marker", None),
            ("flag off", dict(api_writes=False)),
            ("flag absent", {}),
            ("expired", dict(api_writes=True, expires=int(time.time()) - 5)),
            ("a string", dict(api_writes="yes")),
        ):
            (self.proj / "qa-out/.active").unlink(missing_ok=True)
            if setup is not None:
                self.marker(**setup)
            for m in ("POST", "PUT", "PATCH", "DELETE"):
                code, _, err, mrun = self.probe(m, self.URL + "/1", "--json", body)
                self.assertEqual(code, 2, (why, m))
                self.assertIn("--api-writes", err)
                mrun.assert_not_called()
        for m in ("GET", "HEAD", "OPTIONS"):  # reads never need it
            self.assertEqual(self.probe(m, self.URL, seq=[self.page()])[0], 0)

    def test_a_post_body_must_hold_a_qa_sdet_value(self):
        self.marker(api_writes=True)
        code, _, err, mrun = self.probe("POST", self.URL, "--json", '{"name":"Main street"}')
        self.assertEqual(code, 2)
        self.assertIn("qa-sdet-", err)
        self.assertEqual(self.probe("POST", self.URL)[0], 2)  # no body at all
        mrun.assert_not_called()
        code, out, _, mrun = self.probe(
            "POST", self.URL, "--json", '{"name":"qa-sdet-zone-1","approved":true}', seq=[self.page(201)]
        )
        self.assertEqual((code, out["status"]), (0, 201))
        self.assertEqual(mrun.call_count, 1)
        self.assertIn('"Content-Type": "application/json"', mrun.call_args[0][0][4])  # the default for a body
        code, _, _, mrun = self.probe(
            "POST", self.URL, "--json", "qa-sdet-{broken", "--content-type", "text/plain", seq=[self.page(415)]
        )
        self.assertEqual(code, 0)  # a malformed body is a probe, not a refusal
        self.assertIn('"Content-Type": "text/plain"', mrun.call_args[0][0][4])

    def test_put_patch_delete_first_read_the_record_and_need_qa_sdet_in_it(self):
        self.marker(api_writes=True)
        url = self.URL + "/7"
        for m in ("PUT", "PATCH", "DELETE"):
            args = (m, url) + (("--json", '{"speed": 5}') if m != "DELETE" else ())
            code, out, _, mrun = self.probe(
                *args, seq=[self.page(200, '{"name":"qa-sdet-zone-1"}', contains=True), self.page(204)]
            )
            self.assertEqual((code, out["status"]), (0, 204), m)
            first, second = (c[0][0][4] for c in mrun.call_args_list)
            self.assertIn('"method": "GET"', first)
            self.assertIn('"qa-sdet-"', first)  # the page does the search over the whole text
            self.assertIn(f'"method": "{m}"', second)
            for why, pre in (
                ("someone else's record", self.page(200, '{"name":"Main street"}', contains=False)),
                ("no such record", self.page(404, "qa-sdet- not found", contains=True)),
                ("a collection", self.page(200, '[{"name":"qa-sdet-a"},{"name":"B"}]', contains=True)),
                (  # found in audit: most APIs wrap a list in an object
                    "an enveloped list",
                    self.page(200, '{"items": [{"name": "qa-sdet-a"}, {"name": "Main"}], "total": 2}', contains=True),
                ),
                (  # a real record holding a qa-sdet- child
                    "a parent record",
                    self.page(200, '{"id": 17, "name": "Main St", "segments": [{"name": "qa-sdet-s"}]}', contains=True),
                ),
                ("not JSON", self.page(200, "<p>qa-sdet-a</p>", contains=True)),
            ):
                code, _, err, mrun = self.probe(*args, seq=[pre])
                self.assertEqual(code, 2, (m, why))
                self.assertIn("qa-sdet-", err)
                self.assertEqual(mrun.call_count, 1, "the write itself never ran")

    def test_a_record_inside_a_data_envelope_is_still_one_record(self):  # {"data": {...}}, JSON:API attributes
        self.marker(api_writes=True)
        for body in (
            '{"data": {"id": 7, "name": "qa-sdet-zone-1"}}',
            '{"data": {"id": "7", "type": "zones", "attributes": {"name": "qa-sdet-zone-1"}}}',
        ):
            code, out, _, _ = self.probe(
                "DELETE", self.URL + "/7", seq=[self.page(200, body, contains=True), self.page(204)]
            )
            self.assertEqual((code, out["status"]), (0, 204), body)

    def test_a_write_whose_record_check_fails_is_never_sent(self):  # no proof the record is the run's: no write
        self.marker(api_writes=True)
        for m in ("PUT", "PATCH", "DELETE"):
            args = (m, self.URL + "/7") + (("--json", '{"speed": 5}') if m != "DELETE" else ())
            code, out, err, mrun = self.probe(*args, seq=[done(1, err="Error: the browser 'qa' is not open")])
            self.assertEqual((code, out), (1, None), m)
            self.assertIn("api-probe", err)
            self.assertEqual(mrun.call_count, 1, "only the record check ran")

    def test_browser_trouble_is_exit_1_without_a_stack_trace(self):
        for seq in (
            [done(1, err="Error: the browser 'qa' is not open")],
            [done(0, out="garbage")],
            [subprocess.TimeoutExpired("playwright-cli", 30)],
            [OSError("no playwright-cli")],
        ):
            code, out, err, _ = self.probe("GET", self.URL, seq=seq)
            self.assertEqual((code, out), (1, None))
            self.assertIn("api-probe", err)
            self.assertNotIn("Traceback", err)
        code, out, err, _ = self.probe(
            "GET", self.URL, seq=[done(0, out=json.dumps(json.dumps({"error": "TypeError: Failed to fetch"})))]
        )
        self.assertEqual((code, out), (1, None))
        self.assertIn("Failed to fetch", err)


if __name__ == "__main__":
    unittest.main()
