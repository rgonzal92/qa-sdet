import json, os, re, subprocess, sys, tempfile, time, unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
qm = SourceFileLoader("qa_mem", str(ROOT / "bin/qa-mem")).load_module()


def run(build="1.0", bugs=None, **page):
    return {"run_id": f"r-{build}-{time.time_ns()}", "mode": "explore", "target": "/form", "build": build,
            "status": "COMPLETED", "summary": "s",
            "pages": [{"route": "/form", "name": "Form", **page}], "bugs": bugs or []}


qm.claude_mcp_list = lambda: ""  # never run the real `claude mcp list` in tests


class QaMem(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["QA_MEM_DB"] = str(Path(self.tmp.name) / "t.db")
        # never the user's real config or profile folder (doctor checks both)
        self.real_paths = (qm.CONFIG, qm.DATA_DIR)
        qm.CONFIG, qm.DATA_DIR = Path(self.tmp.name) / "cfg" / "config.json", Path(self.tmp.name) / "data"
        self.con = qm.connect()

    def tearDown(self):
        self.con.close()
        qm.CONFIG, qm.DATA_DIR = self.real_paths
        self.tmp.cleanup()

    def test_card_shows_facts_locators_coverage_untried(self):
        qm.ingest(self.con, run(facts=[{"kind": "rule", "text": "Speed 0-130"}],
                                locators=[{"name": "Save", "locator": "getByRole('button')"}],
                                coverage=[{"target": "speed", "heuristic": "boundary", "result": "fail"}]))
        c = qm.card(self.con, "form")
        self.assertIn("Speed 0-130", c)
        self.assertIn("Save=getByRole('button')", c)
        self.assertIn("boundary(speed:fail)", c)
        untried = c.split("UNTRIED: ")[1].splitlines()[0]
        self.assertNotIn("boundary", untried)
        self.assertIn("happy-path", untried)

    def test_unknown_page_card(self):
        self.assertIn("NO CARD", qm.card(self.con, "/nope"))

    def test_redaction_is_linear_on_long_whitespace_and_knows_more_tokens(self):  # audit 3 r4
        t0 = time.time()
        for text in ("password" + " " * 200000 + "x", "token" + "\t " * 100000, "'apiKey'" + " " * 200000 + "'x'"):
            qm.redact(text)
        self.assertLess(time.time() - t0, 5)  # was quadratic: over a minute, past the Stop hook's timeout
        for secret in ("sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789", "glpat-abcdefghijklmnopqrst",
                       "__Host-session=abcdef0123456789abcdef"):
            self.assertNotIn(secret[-12:], qm.redact(f"saw {secret} in the log"), secret)
        for keep in ("class sk-folding-cube-container-large", "session: morning shift", 'password : "x"',
                     "password \\: hunter2x9"):
            self.assertEqual(qm.redact(keep), keep) if "hunter" not in keep else self.assertNotIn("hunter2x9", qm.redact(keep))
        self.assertEqual(qm.redact('"password" : "hunter2x9"'), '"password" : "[REDACTED]"')
        self.assertEqual(qm.redact("password\\\": \\\"hunter2x9"), "password\\\": \\\"[REDACTED]")  # JSON inside a string

    def test_empty_or_numeric_draft_path(self):  # audit 3 r6
        bug = {"route": "/form", "title": "t", "symptom": "s", "draft_path": "/abs/bugs/BUG-1.md"}
        qm.ingest(self.con, run(bugs=[bug]))
        qm.ingest(self.con, run(bugs=[{**bug, "draft_path": ""}]), run_dir=Path(self.tmp.name))  # a re-sighting without one
        self.assertEqual(self.con.execute("SELECT draft_path FROM bugs").fetchone()[0], "/abs/bugs/BUG-1.md")
        qm.ingest(self.con, run(bugs=[{**bug, "symptom": "other", "draft_path": 7}]), run_dir=Path(self.tmp.name))
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 2)

    def test_late_run_keeps_newer_coverage_and_bug_dates(self):  # audit 3 r15
        cov = lambda r: [{"target": "speed", "heuristic": "boundary", "result": r}]
        bug = {"route": "/form", "title": "t", "symptom": "s"}
        qm.ingest(self.con, run(coverage=cov("fail"), bugs=[bug]), ts="2026-02-01T00:00:00Z")
        qm.ingest(self.con, run(coverage=cov("pass"), bugs=[bug]), ts="2026-01-01T00:00:00Z")  # an older file, ingested late
        self.assertEqual(self.con.execute("SELECT result, ts FROM coverage").fetchall()[0][:], ("fail", "2026-02-01T00:00:00Z"))
        self.assertEqual(tuple(self.con.execute("SELECT first_seen, last_seen, seen_count FROM bugs").fetchone()),
                         ("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", 2))
        qm.ingest(self.con, run(coverage=cov("pass")), ts="2026-03-01T00:00:00Z")
        self.assertEqual(self.con.execute("SELECT result FROM coverage").fetchone()[0], "pass")

    def test_repro_spec_loses_literal_tokens_only(self):  # audit 3 r24: this branch had no test
        f = Path(self.tmp.name) / "BUG-1.spec.ts"
        f.write_text("const password = 'hunter2secret';\nconst t = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijk';\n")
        qm.scrub_file(f)
        self.assertEqual(f.read_text(), "const password = 'hunter2secret';\nconst t = '[REDACTED-JWT]';\n")

    def test_redact_json_cookie_lists_header_pairs_and_key_value_objects(self):  # audit 3 r34
        sec = "s3cr3tV4lue9x"
        for obj in ({"cookies": [{"name": "session", "value": sec}]}, {"cookies": {"session": sec}}, {"headers": [["Authorization", sec]]},
                    {"key": "password", "value": sec}, {"jwt": sec}):
            self.assertNotIn(sec, json.dumps(qm.redact_json(obj)), obj)
        for obj in ({"key": "STUDIO-1", "summary": "Bearer of bad news"}, {"name": "Speed limit", "value": "130"},
                    {"key": "speed", "value": "130"}, ["boundary", "pass"], {"notes": "the jwt cookies banner"}):
            self.assertEqual(qm.redact_json(obj), obj)

    def test_command_line_and_stated_passwords_are_redacted(self):  # audit 3, after r50
        for text, secret in (("curl -u admin:hunter2 https://x", "hunter2"), ("curl --user admin:hunter2 https://x", "hunter2"),
                             ("mysql -u root -phunter2 db", "hunter2"), ("the password is hunter2", "hunter2"),
                             ("headers: { Authorization: `Bearer ${'abc123def456ghi789'}` }", "abc123def456")):
            self.assertNotIn(secret, qm.redact(text), text)
        for keep in ("the password is required", "password is wrong", "mysql -P 3306 -u root db", "curl -u https://x",
                     "docker run -u 1000:1000 img"):
            self.assertEqual(qm.redact(keep), keep)

    def test_reopening_a_bug_no_run_lists_removes_it(self):  # audit 3, after r50
        bug = {"route": "/form", "title": "t", "symptom": "s"}
        r = run(bugs=[bug])
        qm.ingest(self.con, r, src="a")
        h = self.con.execute("SELECT hash FROM bugs").fetchone()[0]
        qm.run_cmd(self.con, "bug-status", [h, "FILED"])
        qm.ingest(self.con, {**r, "bugs": []}, src="a")  # the run file was rewritten without it; triaged, so kept
        self.assertEqual(self.con.execute("SELECT status, seen_count FROM bugs").fetchone()[:], ("FILED", 0))
        qm.run_cmd(self.con, "bug-status", [h, "OPEN"])
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 0)

    def test_fresh_review_findings(self):  # audit 3: independent review after round 50
        # limits and selectors survive; an assigned password goes whatever its characters
        for keep in ("Password: 8-64 characters", "Session token: 30min expiry", "PUT /api/profile?key=password", "pwd: 8–64",
                     "Token: 3600s lifetime", "input#password:nth-child(2)", "session: /incidents/list/details/panel",
                     "password = getPassword()", "token = await login()", "password=required", "token=1.42.0"):
            self.assertEqual(qm.redact(keep), keep)
        for text in ("username=qa.tester&password=Sommerregen", "Server=db;User Id=qa;Password=Sommerregen;",
                     "client_secret=AbCdEfGhIjKl", "PGPASSWORD=Sommerregen psql", "export API_KEY=abcdefgh",
                     "X-Api-Key: abcdefghijkl", "Authorization: Bearer abcdefghijkl", "GET /x?key=AbCd1234EfGh5678"):
            out = qm.redact(text)
            self.assertIn("[REDACTED]", out, text)
            self.assertNotRegex(out, "Sommerregen|AbCdEfGhIjKl|abcdefgh|AbCd1234", text)
        # a bug with its draft id in `hash` is still stored
        qm.ingest(self.con, run(facts=[{"kind": "api", "text": "GET /api/zones returns 500 on empty filter"},
                                       {"kind": "note", "text": "speed chart loads slowly"}],
                                bugs=[{"route": "/form", "title": "Save accepts 131", "symptom": "saved at 131", "hash": "BUG-2"}]))
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 1)
        # search: terms with symbols
        self.assertIn("returns 500", qm.search(self.con, "/api/zones"))
        self.assertIn("no facts match", qm.search(self.con, "speed-limit"))
        self.assertIn("speed chart", qm.search(self.con, "speed -/api/zones"))

    def test_verification_pass_findings(self):  # audit 3: second independent pass
        for text, secret in (("2. Sign in as `qa.tester` with password: `Winter2026!`", "Winter2026"),
                             ("- **Cookie:** session=abcdef123456; Path=/", "abcdef123456"), ("- **API token:** abcd1234efgh5678ijkl", "abcd1234"),
                             ("token=`abc123def456`", "abc123def456"), ("**Password:** Winter2026!", "Winter2026"),
                             ("__Token:__ abc123def456ghi7", "abc123def456"), ("Log in with user=qa and password=Sommerregen.", "Sommerregen"),
                             ("password=Summer.Rain", "Summer.Rain"), ("curl -uadmin:hunter2 https://qa.example.com/api", "hunter2"),
                             ("curl -u 'admin':'hunter2' https://x", "hunter2"), ("password: hunter2", "hunter2")):
            self.assertNotIn(secret, qm.redact(text), text)
        for keep in ("Password: maxlength=64", "Save is enabled only when password == confirmPassword", "password: min=8 max=64",
                     "password: minLength(8)", "password: >=8 chars", "token: uuid4", "token: HS256 signed", "session_id: int64",
                     "Cookie: SameSite=Lax missing on session cookie", "Session token: HttpOnly=false", "if (token == null) return",
                     "z.object({ password: z.string().min(8).max(64) })", "password=user.password", "**Speed limit:** 130"):
            self.assertEqual(qm.redact(keep), keep)

    def test_third_pass_findings(self):  # audit 3: third independent pass
        for text, secret in (("request header Authorization: ApiKey 9f8e7d6c5b4a39281706aabbccdd) → 500", "9f8e7d6c5b4a"),
                             ("Authorization: SSWS 00QCjAl4MlV-WPXM1ABCDEFGHIJKLmnopqrstuv", "00QCjAl4"),
                             ("authorization: Api-Key abcdefghijkl0123", "abcdefghijkl0123"),
                             ("Sign in as qa.tester with password: Tr0ub4dor&3", "Tr0ub4dor"), ("password: Wint3r,Sturm!", "Sturm"),
                             ("password=Sommer&Regen2026", "Regen2026"), ("**Password:** Winter;2026!", "2026!"),
                             ("Password: P@ss;w0rd", "w0rd"), ("redis://:Sommerregen@cache.qa:6379/0", "Sommerregen"),
                             ("AccountKey=abcdEFGH1234ijklMNOP5678==;EndpointSuffix=core.windows.net", "abcdEFGH1234"),
                             ("a=1&password=hunter2x&next=/home", "hunter2x")):
            self.assertNotIn(secret, qm.redact(text), text)
        self.assertEqual(qm.redact("a=1&password=hunter2x&next=/home"), "a=1&password=[REDACTED]&next=/home")
        self.assertEqual(qm.redact("(Authorization: Bearer abcdefghijklmnop0123456789), then the page reloaded."),
                         "(Authorization: Bearer [REDACTED]), then the page reloaded.")
        for keep in ("6. Open https://qa.example.com/reset-password?token=<the token from the email>", "GET /api/reset?token=${token}",
                     "GET /api/reset?token=:token", "password: pattern=^[A-Za-z0-9]{8,64}$", "password: 'required|min:8|max:64'",
                     "cookie: maxAge=1800000, httpOnly, sameSite=lax", "Authorization: header missing on the request",
                     "Authorization: permissions enforced server-side"):
            self.assertEqual(qm.redact(keep), keep)
        # a sighting older than the last one (ingested late) predates the fix
        bug = {"route": "/form", "title": "t", "symptom": "s"}
        qm.ingest(self.con, run(bugs=[bug]), ts="2026-11-10T00:00:00Z")
        h = self.con.execute("SELECT hash FROM bugs").fetchone()[0]
        self.con.execute("UPDATE bugs SET status='FIXED'")
        newest = self.con.execute("SELECT last_run FROM bugs").fetchone()[0]
        qm.ingest(self.con, run(bugs=[bug]), ts="2026-11-05T00:00:00Z")
        self.assertEqual(tuple(self.con.execute("SELECT status, last_run FROM bugs").fetchone()), ("FIXED", newest))
        qm.ingest(self.con, run(bugs=[bug]), ts="2026-11-20T00:00:00Z")
        self.assertEqual(self.con.execute("SELECT status FROM bugs WHERE hash=?", (h,)).fetchone()[0], "REGRESSED")

    def test_fourth_pass_findings(self):  # audit 3: fourth independent pass
        line = ("Request headers: Authorization: Bearer 9f8c7d6e5b4a3f2e1d0c9f8c, Cookie: _traffic_session=Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFi"
                "%3D%3D--a1b2c3d4e5; XSRF=1")
        out = qm.redact(line)
        self.assertNotIn("9f8c7d6e", out)
        self.assertNotIn("Zm9vYmFy", out)
        out = qm.redact("Authorization: Bearer abc123def456ghi789jk | Cookie: session=abc123def456 | X-Api-Key: zonesservicekey")
        for secret in ("abc123def456ghi789jk", "session=abc123def456", "zonesservicekey"):
            self.assertNotIn(secret, out)
        for text, secret in (("API key: 9f8c7d6e5b4a3f2e1d0c", "9f8c7d6e"), ("- **API key:** `9f8c7d6e5b4a3f2e1d0c`", "9f8c7d6e"),
                             ("Secret key: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtn"),
                             ("Session ID: 7f3a9b2c4d5e6f708192a3b4c5d6e7f8", "7f3a9b2c"), ("Access key: AbCdEf1234567890", "AbCdEf12")):
            self.assertNotIn(secret, qm.redact(text), text)
        self.assertEqual(qm.redact_json({"API Key": "9f8c7d6e5b4a3f2e1d0c"}), {"API Key": "[REDACTED]"})
        for keep in ('Response 400: {"errors":{"password":"must be at least 8 characters"}}',
                     'Actual: 422 {"password": "Password is too short", "token": "Token has expired"}',
                     'Error toast: password: "The password field is required."', 'token: "invalid or expired"',
                     'i18n key errors.password = "Password is required"', "password: this.loginForm.value.password",
                     "token: response.data.token", "Authorization: Bearer expected but missing"):
            self.assertEqual(qm.redact(keep), keep)
        self.assertEqual(qm.redact("password: 'letmein'"), "password: '[REDACTED]'")

    def test_fifth_pass_findings(self):  # audit 3: fifth independent pass
        tok = "sk." "eyJ1IjoidHJhZmZpY3FhIiwiYSI6ImNrOXh5ejEyMzQ1Njc4OWFiY2RlZiJ9.Xk3mP9qR7sT2vW5yZ8aB1c"
        for text, secret in ((f"access_token: {tok}", "eyJ1Ijoi"), (f"Mapbox access token: {tok}", "eyJ1Ijoi"), (f"MAPBOX_TOKEN={tok}", "eyJ1Ijoi"),
                             ("API key: live.a1b2c3d4e5f6a7b8c9d0e1f2", "a1b2c3d4"), ("Session ID: s.abcdef0123456789abcdef", "abcdef0123"),
                             ("Password: qa.tester2026", "tester2026")):
            self.assertNotIn(secret, qm.redact(text), text)
        for keep in ('Response 400: {"errors":{"password":"Too short"}}', 'Response 400: {"errors":{"password":"Invalid password"}}',
                     'Response 401: {"error":"invalid_token","token":"Token expired"}', 'Toast text: password: "Wrong password"',
                     'Placeholder for password: "Enter your password"', "X-CSRF-Token: mismatch gives 419 instead of 403",
                     "X-Auth-Token: returned by POST /api/login, sent on every call", "X-Api-Key: mandatory for /api/zones, optional elsewhere",
                     "headers: { 'X-Api-Key': process.env.API_KEY }", "password: this.loginForm.value.password"):
            self.assertEqual(qm.redact(keep), keep)
        self.assertEqual(qm.redact("Headers: Authorization: Bearer abc123def456ghi789jkl012, Cookie: sid=abcdef0123456789abcdef, X-Api-Key: live_9f8e7d6c5b4a"),
                         "Headers: Authorization: Bearer [REDACTED], Cookie: [REDACTED], X-Api-Key: [REDACTED]")
        self.assertEqual(qm.redact("X-Api-Key: abcdefghijkl"), "X-Api-Key: [REDACTED]")

    def test_sixth_pass_findings(self):  # audit 3: sixth independent pass
        for keep in ("headers: { Authorization: `Bearer ${accessToken}` }", "'Authorization': `Bearer ${session.accessToken}`,",
                     "Authorization: Bearer {{authToken}}", "Authorization: Bearer <access-token>", "Authorization: Bearer $ACCESS_TOKEN",
                     "headers: { 'x-api-key': environment.apiKey }", "'X-Api-Key': this.config.apiKey,", "X-Auth-Token: req.headers['x-auth-token']",
                     "X-Auth-Token: localStorage.getItem('token')", "X-CSRF-Token: Mismatch error shown", "X-Api-Key: Mandatory header",
                     "token: Validators.required", "password: Validators.required,", "token: DataTypes.STRING", "apiKey: ConfigService.apiKey",
                     '{"errors":{"password":"Minimum 8 characters"}}', '{"errors":{"token":"Session timed out"}}',
                     '{"errors":{"password":"Bad credentials"}}', '"token": "Access denied"', "password: 'Use 8+ characters'",
                     "getByLabel('Password:').fill('x')", "| password | required, min 8 |", "| Speed limit | 130 |"):
            self.assertEqual(qm.redact(keep), keep)
        for text, secret in (("| X-Api-Key | 4f3c2b1a0d9e8f7a6b5c |", "4f3c2b1a"), ("| api_key | 4f3c2b1a0d9e8f7a6b5c1234 |", "4f3c2b1a"),
                             ("| password | Passw0rd! |", "Passw0rd"), ("X-Api-Key: abcdefghijkl", "abcdefghijkl"),
                             ('{"apiKey": "correct horse battery"}', "horse")):
            self.assertNotIn(secret, qm.redact(text), text)

    def test_paired_labels_and_values(self):  # known issue in 0.4.0: `user / password: qa / Passw0rd!`
        for text, secret, kept in (("user / password: qa / Passw0rd!", "Passw0rd", "qa /"),
                                   ("Login / Password: qa@x.com / Hunter22", "Hunter22", "qa@x.com"),
                                   ("username/password = qa/Sommerregen", "Sommerregen", "username/password"),
                                   ("Account: user / API key: qa / 4f3c2b1a0d9e", "4f3c2b1a", "Account")):
            out = qm.redact(text)
            self.assertNotIn(secret, out, text)
            self.assertIn(kept, out, text)
        for keep in ("pass/fail: 3/1", "user / password: required / required", "src/api/token: x/y", "min / max: 8 / 64",
                     "user / role: qa / admin", "Date / time: 2026-11-23 / 10:00", "Email / Password: valid / invalid",
                     "login/password: success/failure", "token/scope: read/write", "token / expiry: JWT / 15 min",
                     "Username / Password: email / text", "/auth/password: GET/POST"):
            self.assertEqual(qm.redact(keep), keep)
        # values KEY_VALUE_RE redacted before stay redacted; multi-word and bold labels; punctuation after the value stays
        for text, secret in (("Password/Confirm password: Passw0rd!/Winter2024!", "Winter2024"),
                             ("token/refresh token: 8a7b6c5d4e3f2a1b0c9d/1f2e3d4c5b6a79880a1b", "1f2e3d4c"),
                             ("Client secret/password: 9f8e7d6c5b4a39281706/Passw0rd!", "9f8e7d6c"),
                             ("**Username / Password:** qa / Passw0rd!", "Passw0rd"), ("admin user / admin password: qa / Hunter22", "Hunter22")):
            self.assertNotIn(secret, qm.redact(text), text)
        self.assertEqual(qm.redact("(user / password: qa / Passw0rd!)."), "(user / password: qa / [REDACTED]).")
        self.assertEqual(qm.redact("user / password: qa / Passw0rd!, then click Login"), "user / password: qa / [REDACTED], then click Login")
        # linear on long hyphenated, base64url and slash runs
        for big in ("-".join(["550e8400e29b41d4a716446655440000"] * 3000), "a-" * 50000, "foo-bar-baz-" * 9000, "a/" * 100000,
                    "x" * 60 + ("Ab3_-" * 20000), "user / password " * 20000):
            t0 = time.time()
            qm.redact(big)
            self.assertLess(time.time() - t0, 2, big[:40])

    def test_unparseable_url_is_no_route_not_a_traceback(self):  # audit 3 r1
        self.assertEqual(qm.norm_route("http://[x"), "")
        self.assertIn("NO CARD", qm.card(self.con, "http://[x"))
        with self.assertRaises(ValueError):
            qm.ingest(self.con, {"run_id": "r", "pages": [{"route": "http://[x"}]})

    def test_bug_dedup_increments_seen_count(self):
        bug = {"route": "/form", "title": "Speed accepts 999", "symptom": "speed accepts 999",
               "severity": "HIGH", "classification": "CONFIRMED"}
        qm.ingest(self.con, run(bugs=[bug]))
        qm.ingest(self.con, run(bugs=[{**bug, "symptom": "Speed  accepts 999!"}]))
        rows = self.con.execute("SELECT seen_count FROM bugs").fetchall()
        self.assertEqual([r[0] for r in rows], [2])

    def test_reingesting_same_run_does_not_inflate_seen_count(self):
        bug = {"route": "/form", "title": "t", "symptom": "s"}
        r = run(bugs=[bug])
        qm.ingest(self.con, r)
        qm.ingest(self.con, r)
        self.assertEqual(self.con.execute("SELECT seen_count FROM bugs").fetchone()[0], 1)

    def test_rewritten_run_replaces_its_bugs_and_counts_runs_once(self):
        """A run.json rewritten mid-run (or ingested by a sibling explorer's hook first) replaces its earlier version."""
        bugs = lambda: [tuple(r) for r in self.con.execute("SELECT title, seen_count FROM bugs ORDER BY title")]
        x, y = {"route": "/f", "title": "X", "symptom": "x"}, {"route": "/f", "title": "Y", "symptom": "y"}
        a = lambda *b: {"run_id": "explore-f-1", "build": "1", "bugs": list(b)}
        qm.ingest(self.con, a({**x, "symptom": "first guess"}))
        qm.ingest(self.con, a(x))  # symptom corrected: the first guess was never a separate bug
        self.assertEqual(bugs(), [("X", 1)])
        qm.ingest(self.con, {"run_id": "explore-g-1", "build": "1", "bugs": [x]})
        qm.ingest(self.con, a(x, y))  # run A rewritten after run B saw X: still two runs
        self.assertEqual(bugs(), [("X", 2), ("Y", 1)])
        qm.ingest(self.con, a())  # A drops both: Y was only A's, X stays B's sighting
        self.assertEqual(bugs(), [("X", 1)])
        qm.ingest(self.con, a(x))  # and back again
        self.assertEqual(bugs(), [("X", 2)])
        self.con.execute("UPDATE bugs SET status='FILED'")
        qm.ingest(self.con, {"run_id": "explore-h-1", "build": "1", "bugs": [y]})
        qm.ingest(self.con, {"run_id": "explore-h-1", "build": "1", "bugs": []})
        self.assertEqual(bugs(), [("X", 2)])
        # two files that reuse one run id are separate files, not rewrites of each other
        qm.ingest(self.con, {"run_id": "same", "build": "1", "bugs": [y]}, src="qa-out/r/.data/run-a.json")
        qm.ingest(self.con, {"run_id": "same", "build": "1", "bugs": [x]}, src="qa-out/r/.data/run-b.json")
        self.assertEqual(bugs(), [("X", 3), ("Y", 1)])

    def test_unknown_build_plan_does_not_stale_real_knowledge(self):
        qm.ingest(self.con, run("1.0", facts=[{"kind": "rule", "text": "real"}],
                                coverage=[{"target": "x", "heuristic": "boundary"}]))
        qm.ingest(self.con, run("unknown", facts=[{"kind": "rule", "text": "from ticket"}]))
        c = qm.card(self.con, "/form")
        self.assertIn("build=1.0", c)
        self.assertNotIn("real *", c)
        self.assertIn("boundary(", c)

    def test_stale_command_resets_coverage_view(self):
        qm.ingest(self.con, run("1.0", coverage=[{"target": "x", "heuristic": "boundary"}]))
        qm.main(["stale", "2.0"])
        c = qm.card(self.con, "/form")
        self.assertIn("re-verify", c)
        self.assertIn("boundary", c.split("UNTRIED: ")[1])

    def test_bug_status_requires_prefix_and_valid_status(self):
        qm.ingest(self.con, run(bugs=[{"route": "/form", "title": "t"}]))
        self.assertEqual(qm.main(["bug-status", "", "FIXED"]), 2)
        self.assertEqual(qm.main(["bug-status", "abcd", "WHATEVER"]), 2)
        self.assertEqual(self.con.execute("SELECT status FROM bugs").fetchone()[0], "OPEN")

    def test_search_survives_fts_syntax(self):
        qm.ingest(self.con, run(facts=[{"kind": "rule", "text": "I-405 closure"}]))
        self.assertIn("closure", qm.search(self.con, "I-405"))

    def test_bad_shapes_rejected(self):
        for bad in ([], {"run_id": "x", "build": "1", "pages": {"route": "/a"}},
                    {"run_id": "x", "build": "1", "bugs": ["oops"]}):
            with self.assertRaises(ValueError):
                qm.ingest(self.con, bad)

    def test_card_prefers_exact_then_shortest_route(self):
        for rt in ("/form/advanced", "/form"):
            qm.ingest(self.con, {"run_id": rt, "build": "1", "pages": [{"route": rt}]})
        self.assertIn("PAGE /form ", qm.card(self.con, "form"))

    def test_new_build_marks_old_knowledge_stale(self):
        qm.ingest(self.con, run("1.0", facts=[{"kind": "rule", "text": "old rule"}],
                                locators=[{"name": "Save", "locator": "x"}]))
        qm.ingest(self.con, run("2.0", facts=[{"kind": "rule", "text": "new rule"}]))
        c = qm.card(self.con, "/form")
        self.assertIn("old rule *", c)
        self.assertNotIn("new rule *", c)
        self.assertNotIn("LOCATORS", c)  # stale locator hidden
        # coverage from build 1.0 no longer counts
        self.assertIn("boundary", c.split("UNTRIED: ")[1])

    def test_text_capped_and_fts_search(self):
        qm.ingest(self.con, run(facts=[{"kind": "rule", "text": "closure schedule " + "x" * 500}]))
        t = self.con.execute("SELECT text FROM facts").fetchone()[0]
        self.assertEqual(len(t), qm.MAX_TEXT)
        self.assertIn("closure", qm.search(self.con, "closure"))
        self.assertFalse(qm.search(self.con, "closure").endswith("*"))
        qm.ingest(self.con, run("2.0", facts=[{"kind": "rule", "text": "other"}]))  # new build stales old facts
        self.assertTrue(qm.search(self.con, "closure").endswith(" *"))

    def test_ingest_pending_is_idempotent_and_records_errors(self):
        out = Path(self.tmp.name) / "qa-out"
        (out / "a").mkdir(parents=True)
        (out / "b").mkdir()
        (out / "a/run.json").write_text(json.dumps(run(facts=[{"kind": "note", "text": "hi"}])))
        (out / "b/run.json").write_text("{not json")
        self.assertEqual(qm.ingest_pending(out), 1)
        self.assertTrue((out / "a/run.json.ingested").exists())
        self.assertTrue((out / "b/run.json.error").exists())
        self.assertEqual(qm.ingest_pending(out), 0)

    def test_hook_in_unrelated_project_creates_no_db(self):
        os.environ["QA_MEM_DB"] = str(Path(self.tmp.name) / "fresh" / "x.db")
        self.assertEqual(qm.ingest_pending(Path(self.tmp.name) / "nope"), 0)
        self.assertFalse((Path(self.tmp.name) / "fresh").exists())

    def test_fixed_bug_seen_again_regresses_and_symptom_shown(self):
        bug = {"route": "/form", "title": "t", "symptom": "speed 999 saved"}
        qm.ingest(self.con, run(bugs=[bug]))
        h = qm.bug_hash("/form", "speed 999 saved")
        self.assertEqual(qm.main(["bug-status", h[:6], "fixed"]), 0)
        self.assertIn(f"PREVIOUSLY FIXED (seen again = regression: put its hash in run.json):\n- {h} FIXED",
                      qm.card(self.con, "/form"))
        qm.ingest(self.con, run("1.1", bugs=[{"route": "/form", "title": "other words", "hash": h}]))
        c = qm.card(self.con, "/form")
        self.assertIn(f"{h} REGRESSED", c)
        self.assertIn("symptom: speed 999 saved", c)

    def test_sid_site_ids_survive_session_sids_go(self):
        self.assertEqual(qm.redact("sid: SEG-1042 shown twice"), "sid: SEG-1042 shown twice")
        self.assertEqual(qm.redact("sent sid=s%3Aabcdefghijklmnop123"), "sent sid=[REDACTED]")
        self.assertIn("[REDACTED]", qm.redact("Cookie: sid=abc123"))

    def test_blocked_stays_untried_and_paraphrased_facts_collapse(self):
        qm.ingest(self.con, run(coverage=[{"target": "api", "heuristic": "error-handling", "result": "blocked"}],
                                facts=[{"kind": "rule", "text": "ID, Road and Type buttons sort ascending"}]), "2026-11-22T10:00:00Z")
        qm.ingest(self.con, run(facts=[{"kind": "rule", "text": "ID, Road and Type header buttons sort ascending"}]),
                  "2026-11-22T11:00:00Z")
        c = qm.card(self.con, "/form")
        self.assertIn("error-handling", c.split("UNTRIED")[1])
        self.assertIn("header buttons", c)
        self.assertEqual(c.count("sort ascending"), 1)

    def test_round23_review_findings(self):
        long = "a" * 40
        self.assertEqual(qm.redact_json({"cookies": [{"name": "sid", "value": long}]})["cookies"][0]["value"], "[REDACTED]")
        self.assertEqual(qm.redact_json({"sid": long, "x": {"sid": "SEG-1"}}), {"sid": "[REDACTED]", "x": {"sid": "SEG-1"}})
        self.assertNotIn(long, qm.redact('{"sid": "%s"}' % long))
        out = Path(self.tmp.name) / "qa-out" / "a"
        out.mkdir(parents=True)
        f = out / "run.json"
        f.write_text(json.dumps(run(facts=[{"kind": "note", "text": "login uses password=Hunter2pass99"}])))
        os.utime(f, (1, 1))
        (out.parent / ".scrubbed").write_text(str(time.time()))  # scrub skips the old file
        self.con.close()
        qm.ingest_pending(out.parent)
        self.con = qm.connect()
        self.assertNotIn("Hunter2", " ".join(r[0] for r in self.con.execute("SELECT text FROM facts")))
        qm.ingest(self.con, {"run_id": "u", "build": "unknown", "pages": [{"route": "/u",
                  "coverage": [{"target": "t", "heuristic": "boundary"}]}]}, "2020-01-01T00:00:00Z")
        self.assertNotIn("boundary(", qm.card(self.con, "/u"))

    def test_detail_routes_share_one_card(self):
        self.assertEqual(qm.norm_route("http://h/incidents/4521/?x=1"), "/incidents/:id")
        self.assertEqual(qm.norm_route("/#/zones/12/"), "/#/zones/:id")
        self.assertEqual(qm.norm_route("/v2/a//b"), "/v2/a/b")
        self.assertEqual(qm.norm_route("https://qa.example.com/#!/incidents/4521"), "/#!/incidents/:id")
        self.assertNotEqual(qm.norm_route("/#!/incidents"), qm.norm_route("/#!/zones"))
        self.assertEqual(qm.norm_route("/app#section"), "/app")
        for r in ['"I #5ge', "a b ?x", "/x/ 12 /y#/z ?q"]:  # idempotent, so a stored route finds itself
            self.assertEqual(qm.norm_route(qm.norm_route(r)), qm.norm_route(r))
        qm.ingest(self.con, {"run_id": "d", "build": "1", "pages": [{"route": "/incidents/4521",
                  "facts": [{"kind": "rule", "text": "detail shows road"}]}]})
        self.assertIn("detail shows road", qm.card(self.con, "/incidents/77"))
        self.assertEqual(qm.bug_hash(qm.norm_route("/incidents/1"), "x"), qm.bug_hash(qm.norm_route("/incidents/2"), "x"))

    def test_size_caps(self):
        qm.MAX_FACTS = 5
        try:
            for n in range(8):
                qm.ingest(self.con, run(facts=[{"kind": "note", "text": f"fact number {n} " + "w" * n}]), f"2026-11-22T10:0{n}:00Z")
            texts = [r[0] for r in self.con.execute("SELECT text FROM facts")]
            self.assertEqual(len(texts), 5)
            self.assertTrue(any("fact number 7" in t for t in texts))
            self.assertEqual(self.con.execute("SELECT count(*) FROM facts_fts").fetchone()[0], 5)
        finally:
            qm.MAX_FACTS = 100

    def test_prefixed_secret_names(self):
        S = "Zk9sLmQ3cXR5UzRmVjJh"
        for t in [f"PRIVATE_TOKEN: {S}", f"secret_key={S}", f"privateKey: {S}", f"bearerToken={S}", f"sessionToken: {S}"]:
            self.assertNotIn(S, qm.redact(t), t)
        for t in ["password_hint: 123 shown", "tokenizer: abc12345", "csrfToken: required", "primary key: id"]:
            self.assertEqual(qm.redact(t), t)
        self.assertEqual(qm.redact_json({"tokenCount": 5, "sessionToken": S}), {"tokenCount": 5, "sessionToken": "[REDACTED]"})

    def test_jira_key_in_hash_creates_no_local_bug(self):
        qm.ingest(self.con, run(bugs=[{"route": "/form", "title": "Known jira TRAF-9 bug", "hash": "TRAF-9"}]))
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 0)

    def test_na_build_is_unknown(self):
        qm.ingest(self.con, run("1.0", facts=[{"kind": "rule", "text": "real"}]))
        qm.ingest(self.con, run("N/A", facts=[{"kind": "rule", "text": "other"}]))
        self.assertIn("build=1.0", qm.card(self.con, "/form"))
        self.assertNotIn("real *", qm.card(self.con, "/form"))

    def test_search_with_broken_fts_syntax_falls_back_to_words(self):
        qm.ingest(self.con, run(facts=[{"kind": "rule", "text": "I-5 N closure: lane 2 (a-b) 50% done"}]))
        for q in ["closure AND", "NEAR(closure", "a-b", "I-5", "closure lane"]:
            self.assertIn("closure", qm.search(self.con, q), q)
        self.assertIn("no facts match", qm.search(self.con, "*"))

    def test_scrub_leaves_symlinks_alone(self):
        root = Path(self.tmp.name)
        (root / "qa-out" / "a").mkdir(parents=True)
        (root / "outside.txt").write_text("password=Hunter2pass99\n")
        (root / "qa-out" / "a" / "notes.md").symlink_to(root / "outside.txt")
        qm.ingest_pending(root / "qa-out")
        self.assertTrue((root / "qa-out" / "a" / "notes.md").is_symlink())
        self.assertIn("Hunter2", (root / "outside.txt").read_text())  # files outside qa-out are never rewritten

    def test_build_whitespace_is_not_a_new_deploy(self):
        qm.ingest(self.con, run("Build:\n   1.42", facts=[{"kind": "rule", "text": "real"}]))
        qm.ingest(self.con, run("Build: 1.42", facts=[{"kind": "rule", "text": "other"}]))
        self.assertNotIn("real *", qm.card(self.con, "/form"))
        qm.ingest(self.con, run("x" * 5000, bugs=[{"route": "/form", "title": "t", "severity": "H" * 500}]))
        self.assertLessEqual(len(qm.meta(self.con, "build")), qm.MAX_TEXT)
        self.assertLessEqual(len(self.con.execute("SELECT severity FROM bugs").fetchone()[0]), 20)

    def test_card_size_is_bounded(self):
        qm.ingest(self.con, run(facts=[{"kind": "rule", "text": f"fact {i} " + "y" * 280} for i in range(30)],
                                locators=[{"name": f"loc{i} " + "n" * 250, "locator": "x" * 290} for i in range(30)],
                                coverage=[{"heuristic": h, "target": f"t{i} " + "z" * 280} for h in qm.HEURISTICS for i in range(6)],
                                bugs=[{"route": "/form", "title": "b" * 300, "symptom": f"s{i} " + "q" * 280} for i in range(12)]))
        self.assertLess(len(qm.card(self.con, "/form")), 15000)  # ~3.5k tokens at worst

    def test_page_name_lookup_prefers_segment_and_exact_name(self):
        for route, name in [("/roadmap", "Roadmap"), ("/map.html", "Live map"), ("/zones", "Speed zones"), ("/z", "Zones")]:
            qm.ingest(self.con, {"run_id": route, "build": "1", "pages": [{"route": route, "name": name}]})
        self.assertIn("PAGE /map.html", qm.card(self.con, "map"))
        self.assertIn("PAGE /zones ", qm.card(self.con, "zones"))  # the route itself first
        self.assertIn("PAGE /map.html", qm.card(self.con, "live map"))  # then the exact page name

    def test_header_prose_survives_scrub(self):
        S = "Zk9sLmQ3cXR5UzRmVjJh"
        for t in ["| TC-07 | Authorization: viewer cannot approve; gets 403 | AC-3 | P1 |",
                  "Cookie: consent banner covers the Save button on /closures/new/details",
                  "Authorization: Basic auth is not supported", "Cookie: none"]:
            self.assertEqual(qm.redact(t), t)
        for t in [f"Authorization: Bearer {S}", "Authorization: Basic dXNlcjpwYXNz", f"Cookie: a=1; session={S}",
                  f"set-cookie: SESSION={S}; Path=/; HttpOnly", f"X-Api-Key: {S} was sent"]:
            self.assertIn("[REDACTED]", qm.redact(t), t)
        self.assertEqual(qm.redact(f"X-Api-Key: {S} was sent"), "X-Api-Key: [REDACTED] was sent")

    def test_basic_credentials_outside_a_header_line(self):  # r20: survived in JSON values, prose and specs
        self.assertEqual(qm.redact_json({"auth": "Basic dXNlcjpwYXNz"}), {"auth": "Basic [REDACTED]"})
        self.assertNotIn("YWRtaW46aHVudGVyMg", qm.redact_literals("headers({authorization: 'Basic YWRtaW46aHVudGVyMg=='})"))
        for t in ["basic validation works", "Basic information panel", "Basic auth required"]:
            self.assertEqual(qm.redact(t), t)

    def test_unreadable_build_spellings(self):
        qm.ingest(self.con, run("1.42.0", facts=[{"kind": "rule", "text": "real"}]))
        for b in ["undefined", "unknown (footer .version not found)", "Unknown", "UNKNOWN", "null"]:
            qm.ingest(self.con, run(b, coverage=[{"target": "t", "heuristic": "duplicate"}]))
        c = qm.card(self.con, "/form")
        self.assertIn("build=1.42.0", c)
        self.assertNotIn("real *", c)
        self.assertEqual({r[0] for r in self.con.execute("SELECT DISTINCT build FROM coverage")}, {"unknown"})
        self.assertIn("duplicate(", c)  # unknown-build coverage visible (within the week)

    def test_pre_fts_database_gets_indexed(self):
        old = Path(self.tmp.name) / "prefts.db"
        c = qm.sqlite3.connect(old)
        c.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY, page TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,"
                  " source TEXT, build TEXT, ts TEXT, stale INTEGER DEFAULT 0, UNIQUE(page, kind, text))")
        c.executemany("INSERT INTO facts(page,kind,text,ts) VALUES('/form','rule',?, '2026-01-01T00:00:00Z')",
                      [(f"old closure fact {i}",) for i in range(120)])
        c.commit()
        c.close()
        os.environ["QA_MEM_DB"] = str(old)
        con = qm.connect()
        try:
            self.assertIn("closure", qm.search(con, "closure"))
            qm.ingest(con, {"run_id": "r", "build": "1", "pages": [{"route": "/form", "facts": [{"kind": "rule", "text": "new"}]}]})
            con.execute("INSERT INTO facts_fts(facts_fts) VALUES('integrity-check')")  # raises if the index is corrupt
        finally:
            con.close()

    def test_coverage_results_normalised(self):
        cov = [{"target": "t", "heuristic": h, "result": r} for h, r in
               [("boundary", "BLOCKED"), ("duplicate", "blocked (needs --faults)"), ("empty-null", "NOT_RUN"),
                ("keyboard-a11y", "skipped"), ("happy-path", "PASS"), ("invalid-format", "Failed")]]
        qm.ingest(self.con, run(coverage=cov))
        untried = qm.card(self.con, "/form").split("UNTRIED: ")[1].splitlines()[0]
        for h in ("boundary", "duplicate", "empty-null", "keyboard-a11y"):
            self.assertIn(h, untried)
        for h in ("happy-path", "invalid-format"):
            self.assertNotIn(h, untried)
        self.assertEqual({r[0] for r in self.con.execute("SELECT result FROM coverage")}, {"pass", "fail", "blocked"})

    def test_distinct_rules_not_collapsed(self):
        facts = ["Name field is required", "Email field is required", "Start date must be before end date",
                 "Start time must be before end time", "Speed limit must be between 0 and 130 km/h",
                 "Speed limit must be between 0 and 120 km/h"]
        qm.ingest(self.con, run(facts=[{"kind": "rule", "text": t} for t in facts]))
        c = qm.card(self.con, "/form")
        for t in facts:
            self.assertIn(t, c)

    def test_code_query_param_kept_in_specs(self):
        self.assertEqual(qm.redact_literals("page.goto('/closures?code=I5N&x=1')"), "page.goto('/closures?code=I5N&x=1')")
        self.assertIn("[REDACTED]", qm.redact_literals("page.goto('/x?access_token=abc123def456')"))

    def test_hash_field_variants(self):
        for i, junk in enumerate(["n/a", "none", "new", "TBD", ""]):
            qm.ingest(self.con, run(bugs=[{"route": "/form", "title": f"t{i}", "symptom": f"s{i}", "hash": junk}]))
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 5)
        h = qm.bug_hash("/form", "s0")
        self.con.execute("UPDATE bugs SET status='FIXED' WHERE hash=?", (h,))
        self.con.commit()
        for variant in [f"{h} ", f"#{h}", f"{h} (known)"]:
            qm.ingest(self.con, run(bugs=[{"route": "/form", "title": "x", "hash": variant}]))
        self.assertEqual(self.con.execute("SELECT status, seen_count FROM bugs WHERE hash=?", (h,)).fetchone()[:], ("REGRESSED", 4))
        qm.ingest(self.con, run(bugs=[{"route": "/form", "title": "jira dup", "hash": "TRAF-9"}]))
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 5)

    def test_invalid_bugs_listed_as_not_bugs(self):
        qm.ingest(self.con, run(bugs=[{"route": "/form", "title": "t", "symptom": "plural wording"}]))
        h = qm.bug_hash("/form", "plural wording")
        self.con.close()
        qm.main(["bug-status", h, "INVALID"])
        self.con = qm.connect()
        self.assertIn(f"NOT BUGS (the user marked these INVALID: don't report them):\n- {h} INVALID", qm.card(self.con, "/form"))

    def test_unknown_build_page_keeps_its_card(self):
        qm.ingest(self.con, {"run_id": "a", "build": "1.42.0", "pages": [{"route": "/incidents"}]})
        qm.ingest(self.con, {"run_id": "b", "build": "unknown", "pages": [{"route": "/map", "facts": [{"kind": "rule", "text": "layers switch"}],
                  "locators": [{"name": "Layer", "locator": "getByLabel('Layer')"}]}]})
        c = qm.card(self.con, "/map")
        self.assertIn("LOCATORS (fresh): Layer=getByLabel('Layer')", c)
        self.assertIn("- [rule] layers switch\n", c + "\n")  # not marked stale
        self.assertIn("never readable", c)

    def test_ticket_fact_becomes_observed(self):
        qm.ingest(self.con, run("unknown", facts=[{"kind": "rule", "text": "End date after start", "source": "TRAF-1"}]))
        self.assertIn("(from TRAF-1)", qm.card(self.con, "/form"))
        qm.ingest(self.con, run("1.0", facts=[{"kind": "rule", "text": "End date after start", "source": "observed"}]))
        self.assertNotIn("(from TRAF-1)", qm.card(self.con, "/form"))
        qm.ingest(self.con, run("unknown", facts=[{"kind": "rule", "text": "x", "source": "TRAF-2"}]))  # unverified stays 'from'
        self.assertIn("(from TRAF-2)", qm.card(self.con, "/form"))

    def test_bug_status_refuses_ambiguous_prefix(self):
        with self.con:
            for h in ("abcd11111111", "abcd22222222"):
                self.con.execute("INSERT INTO bugs(hash,page,title) VALUES(?,?,?)", (h, "/form", "t"))
        self.assertEqual(qm.main(["bug-status", "abcd", "FIXED"]), 1)
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs WHERE status='FIXED'").fetchone()[0], 0)
        self.assertEqual(qm.main(["bug-status", "abcd1", "FIXED"]), 0)
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs WHERE status='FIXED'").fetchone()[0], 1)

    def test_filed_bugs_stay_visible(self):
        qm.ingest(self.con, run(bugs=[{"route": "/form", "title": "t"}]))
        h = self.con.execute("SELECT hash FROM bugs").fetchone()[0]
        qm.main(["bug-status", h, "FILED"])
        self.assertIn("FILED", qm.card(self.con, "/form"))

    def test_route_query_is_exact(self):
        qm.ingest(self.con, {"run_id": "a", "build": "1", "pages": [{"route": "/incidents/list"}]})
        self.assertIn("NO CARD", qm.card(self.con, "/incidents"))
        self.assertIn("matched from 'incidents'", qm.card(self.con, "incidents"))

    def test_transient_error_retries(self):
        out = Path(self.tmp.name) / "qa-out" / "a"
        out.mkdir(parents=True)
        (out / "run.json").write_text(json.dumps(run()))
        real = qm.ingest
        qm.ingest = lambda *a: (_ for _ in ()).throw(qm.sqlite3.OperationalError("database is locked"))
        try:
            self.assertEqual(qm.ingest_pending(out.parent), 0)
        finally:
            qm.ingest = real
        self.assertFalse((out / "run.json.error").exists())
        self.assertEqual(qm.ingest_pending(out.parent), 1)

    def test_old_run_does_not_roll_back_build(self):
        qm.ingest(self.con, run("1.1"), "2026-11-22T10:00:00Z")
        qm.ingest(self.con, run("1.0"), "2026-11-21T10:00:00Z")
        self.assertEqual(qm.meta(self.con, "build"), "1.1")

    def test_plan_does_not_mark_verified(self):
        qm.ingest(self.con, {"run_id": "p", "build": "unknown", "pages": [{"route": "/new"}]})
        self.assertIn("verified=never", qm.card(self.con, "/new"))

    def test_heuristic_ids_normalised(self):
        qm.ingest(self.con, run(coverage=[{"target": "x", "heuristic": "Boundary"},
                                          {"target": "y", "heuristic": "Traffic time zone"}]))
        c = qm.card(self.con, "/form")
        self.assertIn("other:traffic-time-zone", c)
        self.assertNotIn("boundary", c.split("UNTRIED: ")[1].splitlines()[0])

    def test_bug_status_wildcards_and_stale_extra_args(self):
        qm.ingest(self.con, run(bugs=[{"route": "/form", "title": "a"}, {"route": "/form", "title": "b"}]))
        self.assertEqual(qm.main(["bug-status", "____", "INVALID"]), 2)
        self.assertEqual(qm.main(["stale", "2.0", "x"]), 0)

    def test_round5_bad_scalars_are_parked_not_retried(self):
        out = Path(self.tmp.name) / "qa-out"
        for i, bad in enumerate([{"pages": [{"route": ["/a"]}]}, {"mode": {"x": 1}},
                                 {"bugs": [{"route": "/a", "title": "t", "severity": ["HIGH"]}]},
                                 {"status": 10 ** 30, "pages": [{"route": "/a", "facts": [{"text": {"a": 1}}]}]}]):
            d = out / str(i)
            d.mkdir(parents=True)
            (d / "run.json").write_text(json.dumps({"run_id": f"r{i}", "build": "1", **bad}))
        qm.ingest_pending(out)
        errors = sorted(p.parent.name for p in out.rglob("run.json.error"))
        self.assertEqual(errors[:3], ["0", "1", "2"])

    def test_round5_late_old_run_does_not_roll_back_page(self):
        qm.ingest(self.con, run("1.1", facts=[{"kind": "rule", "text": "fresh"}]), "2026-11-22T10:00:00Z")
        qm.ingest(self.con, run("1.0", facts=[{"kind": "rule", "text": "old"}]), "2026-11-21T10:00:00Z")
        c = qm.card(self.con, "/form")
        self.assertIn("build=1.1", c)
        self.assertIn("fresh\n", c + "\n")
        self.assertNotIn("fresh *", c)

    def test_late_run_of_the_current_build_still_adds_what_it_saw(self):  # audit 2 r12
        qm.ingest(self.con, run("2.0", locators=[{"name": "Add", "locator": "getByRole('button', { name: 'Add' })"}]),
                  "2026-11-22T10:00:00Z")
        qm.ingest(self.con, run("2.0", facts=[{"kind": "rule", "text": "code max 6"}],
                                locators=[{"name": "Code", "locator": "getByLabel('Code')"}]), "2026-11-22T09:00:00Z")
        names = {r[0] for r in self.con.execute("SELECT name FROM locators")}
        self.assertEqual(names, {"Add", "Code"})
        self.assertEqual(self.con.execute("SELECT stale FROM facts WHERE text='code max 6'").fetchone()[0], 0)
        qm.ingest(self.con, run("1.0", locators=[{"name": "Old", "locator": "#old"}]), "2026-11-21T09:00:00Z")
        self.assertNotIn("Old", {r[0] for r in self.con.execute("SELECT name FROM locators")})  # older build: skipped

    def test_card_says_when_verified_locators_did_not_fit(self):  # audit 2 r13
        locs = [{"name": f"btn{i}", "locator": f"getByRole('button', {{ name: 'Button number {i} {'x' * 60}' }})"}
                for i in range(25)]
        qm.ingest(self.con, run("1.0", locators=locs))
        c = qm.card(self.con, "/form")
        self.assertRegex(c, r"\(\+\d+ more verified")

    def test_round5_plan_does_not_refresh_stale_fact(self):
        qm.ingest(self.con, run("1.0", facts=[{"kind": "rule", "text": "r"}]), "2026-11-20T10:00:00Z")
        qm.ingest(self.con, run("1.1"), "2026-11-21T10:00:00Z")  # r now stale
        qm.ingest(self.con, run("unknown", facts=[{"kind": "rule", "text": "r"}]), "2026-11-22T10:00:00Z")
        self.assertEqual(self.con.execute("SELECT stale, build FROM facts").fetchone()[:], (1, "1.0"))

    def test_round5_route_normalisation(self):
        qm.ingest(self.con, run())
        for q in ("/form/", "/form?x=1", "http://localhost:8765/form"):
            self.assertIn("PAGE /form ", qm.card(self.con, q), q)
        self.assertEqual(qm.norm_route("/#/map"), "/#/map")
        with self.assertRaises(ValueError):
            qm.ingest(self.con, run(bugs=[{"title": "no route"}]))

    def test_round5_short_hash_prefix_and_unknown_coverage(self):
        qm.ingest(self.con, run(bugs=[{"route": "/form", "title": "t", "symptom": "s"}]))
        h = qm.bug_hash("/form", "s")
        qm.ingest(self.con, run(bugs=[{"route": "/form", "title": "x", "hash": h[:8]}]))
        self.assertEqual(self.con.execute("SELECT count(*), max(seen_count) FROM bugs").fetchone()[:], (1, 2))
        qm.ingest(self.con, run("unknown", coverage=[{"target": "t", "heuristic": "duplicate"}]))
        self.assertIn("duplicate(", qm.card(self.con, "/form"))
        qm.ingest(self.con, run("unknown", coverage=[{"target": "t", "heuristic": "boundary"}]), "2020-01-01T00:00:00Z")
        self.assertNotIn("boundary(", qm.card(self.con, "/form"))  # unknown-build coverage expires after a week

    def test_round5_migrates_old_db(self):
        old = Path(self.tmp.name) / "old.db"
        c = qm.sqlite3.connect(old)
        c.execute("CREATE TABLE bugs(hash TEXT PRIMARY KEY, page TEXT, title TEXT, severity TEXT, classification TEXT,"
                  " status TEXT DEFAULT 'OPEN', draft_path TEXT, first_seen TEXT, last_seen TEXT, seen_count INTEGER DEFAULT 1)")
        c.commit()
        c.close()
        os.environ["QA_MEM_DB"] = str(old)
        con = qm.connect()
        qm.ingest(con, run(bugs=[{"route": "/form", "title": "t"}]))
        self.assertIn("KNOWN BUGS", qm.card(con, "/form"))
        con.close()

    def test_ingest_pending_cli_dedupes_dirs_and_content_hash(self):
        out = Path(self.tmp.name) / "qa-out" / "a"
        out.mkdir(parents=True)
        (out / "run.json").write_text(json.dumps(run(bugs=[{"route": "/form", "title": "t"}])))
        qm.main(["ingest-pending", str(out.parent), str(out.parent) + "/", ""])
        self.assertEqual(self.con.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
        future = time.time() + 3600  # skewed mtime must not cause re-ingest loops
        os.utime(out / "run.json", (future, future))
        self.assertEqual(qm.ingest_pending(out.parent), 0)
        self.assertLessEqual(self.con.execute("SELECT max(ts) FROM runs").fetchone()[0], qm.now())

    def test_hash_cli_matches_ingest_normalisation(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            qm.main(["hash", "http://localhost:8765/form/", "speed", "999"])
        qm.ingest(self.con, run(bugs=[{"route": "/form", "title": "t", "symptom": "speed 999"}]))
        self.assertEqual(buf.getvalue().strip(), self.con.execute("SELECT hash FROM bugs").fetchone()[0])

    def test_doctor_reports_config_problems(self):
        import io, contextlib
        cfg = Path(self.tmp.name) / "config.json"
        cfg.write_text(json.dumps({"app": "x", "qaBaseUrl": "https://qa.example.com", "allowedHosts": "qa.example.com"}))
        real = qm.CONFIG
        qm.CONFIG = cfg
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                problems = qm.doctor()
        finally:
            qm.CONFIG = real
        out = buf.getvalue()
        self.assertGreater(problems, 0)
        self.assertIn("FIX  allowedHosts is a list", out)
        self.assertIn("FIX  config has app", out)

    def test_doctor_empty_config_failed_mcp_list_and_source_folders(self):  # audit 2 r19
        import io, contextlib, shutil
        cfg = Path(self.tmp.name) / "config.json"
        real, real_list, real_which = qm.CONFIG, qm.claude_mcp_list, shutil.which
        qm.CONFIG, qm.claude_mcp_list = cfg, lambda: "FAILED"
        shutil.which = lambda name, *a, **k: None  # no playwright-cli, no acli: the fallback lines print
        try:
            for body, want in (({}, "FIX  config has app"),
                               ({"sourcePaths": "/x"}, "FIX  sourcePaths is a list"),
                               ({"sourcePaths": [str(Path(self.tmp.name) / "gone")]}, "not found")):
                cfg.write_text(json.dumps(body))
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    qm.doctor()
                self.assertIn(want, buf.getvalue(), body)
            self.assertIn("`claude mcp list` failed", buf.getvalue())
        finally:
            qm.CONFIG, qm.claude_mcp_list, shutil.which = real, real_list, real_which
        self.assertTrue(qm.pillow_fix())

    def test_redaction_audit2_tokens_and_false_positives(self):
        secrets = ["github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz0123456789ABCD",
                   "ATATT3xFfGF0abcdefghijklmnopqrstuvwxyz0123456789=ABCDEF12",
                   "AIzaSyA1b2C3d4E5f6G7h8I9j0KlMnOpQrStUvW", "sk_" "live_4eC39HqLyjWDarjtT1zdp7dc",
                   "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun\n"
                   "-----END RSA PRIVATE KEY-----", "https://b.s3.amazonaws.com/x?X-Amz-Signature=abcdef0123456789abcdef"]
        for sec in secrets:
            self.assertNotIn(sec.splitlines()[-1][-12:], qm.redact(f"value: {sec} end"), sec[:20])
        self.assertNotIn("letmein", qm.redact("const password = 'letmein';"))
        self.assertNotIn("horse", qm.redact('{"apiKey": "correct horse battery"}'))
        for keep in ("Bearer acknowledgements are shown", "token=1.42.0 released", "password: required",
                     "the password field max 64 chars", "Token expired message shown"):
            self.assertEqual(qm.redact(keep), keep)

    def test_search_terms_and_stale_correction(self):  # audit 2 r29
        qm.ingest(self.con, run("1.43.0", facts=[{"kind": "rule", "text": "speed max 130"},
                                                  {"kind": "rule", "text": "closure AND detour shown"}]))
        self.assertNotIn("max 130", qm.search(self.con, "speed -130"))
        self.assertIn("detour", qm.search(self.con, "closure NEAR detour"))
        self.assertIn("max 130", qm.search(self.con, 'speed "130'))
        qm.main(["stale", "1.44.0"])
        qm.main(["stale", "1.43.0"])  # corrected: what was verified on 1.43.0 is current again
        self.assertEqual(self.con.execute("SELECT max(stale) FROM facts").fetchone()[0], 0)

    def test_scrub_backlog_is_worked_off_in_bounded_passes(self):  # audit 2 r36: hook timeout safety
        root = Path(self.tmp.name) / "qa-out"
        files = []
        for i in range(5):
            f = root / f"r{i}" / "bugs" / "BUG-1.md"
            f.parent.mkdir(parents=True)
            f.write_text("Authorization: Bearer abcdef0123456789abcdef")
            os.utime(f, (1_700_000_000, 1_700_000_000))  # one mtime for all: a copied qa-out
            files.append(f)
        passes = 0
        while any("abcdef0123" in f.read_text() for f in files):
            qm.scrub_tree(root, budget=0)  # one file per pass
            passes += 1
            self.assertLess(passes, 10)
        self.assertEqual(passes, 5)
        for _ in range(5):  # each rewritten file is looked at once more (its change time is new), then the pass ends
            qm.scrub_tree(root, budget=0)
        self.assertNotIn(" ", (root / ".scrubbed").read_text())  # a finished pass stores a plain time again
        late = root / "r0" / "bugs" / "BUG-2.md"  # audit 3 r36: copied in with an old mtime (cp -a, a restored backup)
        late.write_text("Authorization: Bearer abcdef0123456789abcdef")
        os.utime(late, (1_600_000_000, 1_600_000_000))
        qm.scrub_tree(root)
        self.assertNotIn("abcdef0123", late.read_text())
        self.assertEqual(late.stat().st_mtime, 1_600_000_000)

    def test_ingest_redacts_drafts_and_run_json(self):
        out = Path(self.tmp.name) / "qa-out" / "a"
        (out / "bugs").mkdir(parents=True)
        (out / "bugs/BUG-1.md").write_text("Network: GET /api -> 500\nAuthorization: Bearer abcdefghijklmnopqrst\n")
        (out / "run.json").write_text(json.dumps(run(facts=[{"kind": "api", "text": "API needs cookie: sid=abc123def"}])))
        self.assertEqual(qm.ingest_pending(out.parent), 1)
        draft = (out / "bugs/BUG-1.md").read_text()
        self.assertIn("[REDACTED]", draft)
        self.assertNotIn("abcdefghijklmnopqrst", draft)
        self.assertNotIn("abc123def", self.con.execute("SELECT group_concat(text) FROM facts").fetchone()[0])
        self.assertEqual(qm.ingest_pending(out.parent), 0)  # redaction is idempotent: no re-ingest loop

    def test_draft_paths_stored_absolute(self):
        proj = Path(self.tmp.name) / "proj"
        out = proj / "qa-out" / "form"
        (out / "bugs").mkdir(parents=True)
        (out / "bugs/BUG-1.md").write_text("x")
        (out / "run.json").write_text(json.dumps(run(bugs=[
            {"route": "/form", "title": "a", "draft_path": "qa-out/form/bugs/BUG-1.md"},
            {"route": "/form", "title": "b", "draft_path": "bugs/BUG-1.md"}])))
        qm.ingest_pending(proj / "qa-out")
        paths = {r[0] for r in self.con.execute("SELECT draft_path FROM bugs")}
        self.assertEqual(paths, {str((out / "bugs/BUG-1.md").resolve())})

    def test_round9_redaction_keeps_mtime_json_and_prose(self):
        out = Path(self.tmp.name) / "qa-out" / "old"
        (out / "bugs").mkdir(parents=True)
        qm.ingest(self.con, run("1.1", facts=[{"kind": "rule", "text": "new"}]), "2026-11-22T10:00:00Z")
        (out / "run.json").write_text(json.dumps(run("1.0", facts=[{"kind": "rule", "text": "cookie: sid=abc123"}])))
        old = time.time() - 3 * 86400
        os.utime(out / "run.json", (old, old))
        (out / "bugs/BUG-1.md").write_text("Open Basic Settings; password: min 8 chars\n")
        (out / "evidence.json").write_text(json.dumps({"sessionId": 12345, "ok": True}))
        qm.ingest_pending(out.parent)
        self.assertEqual(qm.meta(self.con, "build"), "1.1")  # redaction kept the old mtime: no rollback
        self.assertEqual((out / "bugs/BUG-1.md").read_text(), "Open Basic Settings; password: min 8 chars\n")
        self.assertEqual(json.loads((out / "evidence.json").read_text())["sessionId"], "[REDACTED]")

    def test_round9_scrubs_folders_without_run_json(self):
        out = Path(self.tmp.name) / "qa-out" / "crashed" / "bugs"
        out.mkdir(parents=True)
        (out / "BUG-1.md").write_text("Authorization: Bearer abcdefghijklmnopqrst\n")
        os.environ["QA_MEM_DB"] = str(Path(self.tmp.name) / "none" / "x.db")
        qm.ingest_pending(out.parent.parent)
        self.assertNotIn("abcdefghijklmnopqrst", (out / "BUG-1.md").read_text())
        self.assertFalse((Path(self.tmp.name) / "none").exists())  # no run.json: no DB touched

    def test_doctor_reports_mcp_fallbacks(self):
        import io, contextlib, shutil
        real_which, real_list = shutil.which, qm.claude_mcp_list
        shutil.which = lambda name, *a, **k: None  # no playwright-cli, no acli
        qm.claude_mcp_list = lambda: ("playwright: npx @playwright/mcp@latest --secret-token-abc - ✔ Connected\n"
                                      "claude.ai Atlassian: https://mcp.atlassian.com/v1?key=k123 - ✔ Connected\n"
                                      "jira-old: npx jira-mcp - ✗ Failed to connect — https://x/?apikey=SECRET42\n")
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                qm.doctor()
        finally:
            shutil.which, qm.claude_mcp_list = real_which, real_list
        out = buf.getvalue()
        self.assertIn("ok   fallback browser: Playwright MCP playwright", out)
        self.assertIn("ok   Jira fallback: Atlassian/Jira MCP claude.ai Atlassian", out)
        self.assertNotIn("secret-token-abc", out)  # names only: commands and URLs can carry keys
        self.assertNotIn("FIX  playwright-cli", out)  # a working MCP fallback: the CLI is advice, not a problem
        self.assertIn("info no signed-in profile: MCP runs", out)
        self.assertNotIn("k123", out)
        self.assertNotIn("SECRET42", out)  # error text after the status can hold keys too
        qm.claude_mcp_list = lambda: "playwright: npx @playwright/mcp - ⏸ Pending approval (run `claude` to approve)\n"
        shutil.which = lambda name, *a, **k: None
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                qm.doctor()
        finally:
            shutil.which, qm.claude_mcp_list = real_which, real_list
        self.assertIn("FIX  fallback browser", buf.getvalue())
        self.assertIn("playwright (pending approval)", buf.getvalue())
        # finding 8: an HTTP server named "browser" counts; a connected server with an unknown name is "can't tell"
        for listing, expect in (("browser: http://localhost:8931/mcp (HTTP) - ✔ Connected\n", "ok   fallback browser: Playwright MCP browser"),
                                ("work-tools: http://localhost:9000/mcp (HTTP) - ✔ Connected\n", "info fallback browser: none named playwright/browser"),
                                ("", "FIX  fallback browser: a connected Playwright MCP"),
                                (None, "info fallback browser: couldn't list MCP servers"),
                                (None, "info Jira fallback: couldn't list MCP servers"),
                                ("TIMEOUT", "`claude mcp list` timed out"),
                                ("chrome-devtools: npx chrome-devtools-mcp@latest --browser-url=http://127.0.0.1:9222 - ✔ Connected\n",
                                 "info fallback browser: none named playwright/browser"),
                                ("qa: npx @playwright/mcp@latest --isolated - ✔ Connected\n", "ok   fallback browser: Playwright MCP qa"),
                                ("pw-test: npx playwright run-test-mcp-server - ✔ Connected\n", "FIX  fallback browser")):
            qm.claude_mcp_list = lambda listing=listing: listing
            shutil.which = lambda name, *a, **k: None
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    qm.doctor()
            finally:
                shutil.which, qm.claude_mcp_list = real_which, real_list
            self.assertIn(expect, buf.getvalue(), listing)

    def test_browser_choice_detection_and_doctor(self):
        """Runs use the browser the user chose from what's installed (config `browser`), never an assumed Firefox."""
        import io, contextlib, shutil, subprocess
        tmp = Path(self.tmp.name)
        chrome = tmp / "chrome"; chrome.write_text("")
        build = tmp / "webkit-1"; build.mkdir(); (build / "INSTALLATION_COMPLETE").write_text("")
        real_isfile, real_which, real_run = os.path.isfile, shutil.which, subprocess.run
        os.path.isfile = lambda x: x == "/opt/google/chrome/chrome" or (str(x).endswith("INSTALLATION_COMPLETE") and real_isfile(x))  # only the stub's browsers, not this machine's
        shutil.which = lambda name, *a, **k: "/x/playwright-cli" if name == "playwright-cli" else None
        def fake_run(cmd, *a, **k):
            out = f"Install location: {build if 'webkit' in cmd else tmp / 'firefox-missing'}\n" if "install-browser" in cmd else ""
            return type("R", (), {"returncode": 0, "stdout": out})()
        subprocess.run = fake_run
        try:
            got = qm.installed_browsers()
            self.assertEqual({k: v[0] for k, v in got.items()}, {"chrome": True, "msedge": False, "firefox": False, "webkit": True})
            self.assertIn("playwright-cli install-browser firefox", got["firefox"][1])
            old_local = os.environ.get("LOCALAPPDATA")
            os.environ["LOCALAPPDATA"] = r"C:\Users\me\AppData\Local"  # audit 1: per-user Edge on Windows
            os.path.isfile = lambda x: x == r"C:\Users\me\AppData\Local\Microsoft\Edge\Application\msedge.exe"
            try:
                self.assertTrue(qm.installed_browsers(("msedge",))["msedge"][0])
            finally:
                if old_local is None:
                    del os.environ["LOCALAPPDATA"]
                else:
                    os.environ["LOCALAPPDATA"] = old_local
                os.path.isfile = lambda x: x == "/opt/google/chrome/chrome" or (str(x).endswith("INSTALLATION_COMPLETE") and real_isfile(x))
            cfg = tmp / "config.json"
            real_cfg = qm.CONFIG
            qm.CONFIG = cfg
            try:
                for conf, want in (({}, "FIX  config has browser"), ({"browser": "opera"}, "FIX  config browser 'opera'"),
                                   ({"browser": "webkit"}, "ok   browser webkit installed"),
                                   ({"browser": "firefox"}, "FIX  browser firefox installed  -> playwright-cli install-browser firefox")):
                    cfg.write_text(json.dumps(conf))
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        qm.doctor()
                    self.assertIn(want, buf.getvalue(), conf)
                    self.assertNotIn("Firefox installed", buf.getvalue())
            finally:
                qm.CONFIG = real_cfg
        finally:
            os.path.isfile, shutil.which, subprocess.run = real_isfile, real_which, real_run

    def test_browser_detection_edge_cases(self):
        """Review findings: interrupted downloads, failed lookups, HOMEDRIVE paths, the browsers command, the MCP hint."""
        import io, contextlib, shutil, subprocess
        tmp = Path(self.tmp.name)
        half = tmp / "firefox-9"; half.mkdir()  # no INSTALLATION_COMPLETE: an interrupted download
        real_isfile, real_which, real_run = os.path.isfile, shutil.which, subprocess.run
        saved = {k: os.environ.get(k) for k in ("HOMEDRIVE", "PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")}
        def runner(mode):
            def fake(cmd, *a, **k):
                if mode == "timeout" and "install-browser" in cmd:  # only the lookup fails; --version works
                    raise subprocess.TimeoutExpired(cmd, 30)
                out = f"Install location: {half}\n" if mode == "half" else "nothing useful\n"
                return type("R", (), {"returncode": 0, "stdout": out})()
            return fake
        shutil.which = lambda name, *a, **k: "/x/playwright-cli" if name == "playwright-cli" else None
        try:
            for mode, want in (("half", "playwright-cli install-browser firefox"), ("timeout", "can't check: `playwright-cli"),
                               ("noloc", "gave no install location")):
                subprocess.run = runner(mode)
                ok, how = qm.installed_browsers(("firefox",))["firefox"]
                self.assertFalse(ok, mode); self.assertIn(want, how, mode)
            for k in saved:
                os.environ.pop(k, None)
            os.environ["HOMEDRIVE"] = "D:"
            os.path.isfile = lambda x: x == r"D:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
            self.assertTrue(qm.installed_browsers(("chrome",))["chrome"][0])
            mac = {"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                   "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"}  # macOS app bundles
            os.path.isfile = lambda x: x in mac
            got = qm.installed_browsers(("chrome", "msedge"))
            self.assertTrue(got["chrome"][0] and got["msedge"][0]); self.assertIn(".app/", got["msedge"][1])
            os.path.isfile = real_isfile
            subprocess.run = runner("timeout")
            cfg, real_cfg = tmp / "config.json", qm.CONFIG
            qm.CONFIG = cfg
            try:
                cfg.write_text(json.dumps({"browser": "firefox"}))
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    qm.doctor()
                out = buf.getvalue()
                self.assertIn("info browser firefox: can't check", out)  # a failed lookup isn't a FIX
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(qm.main(["browsers"]), 0)
                self.assertRegex(buf.getvalue(), r"(?m)^chrome +(installed|missing)")
                shutil.which = lambda name, *a, **k: None  # no CLI: the MCP hint follows the chosen browser
                for conf, want in (({"browser": "webkit"}, "--browser webkit, then npx @playwright/mcp@latest install-browser webkit"),
                                   ({}, "--browser <browser> (or install")):
                    cfg.write_text(json.dumps(conf))
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        qm.doctor()
                    self.assertIn(want, buf.getvalue(), conf)
            finally:
                qm.CONFIG = real_cfg
        finally:
            os.path.isfile, shutil.which, subprocess.run = real_isfile, real_which, real_run
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v

    def test_doctor_acli_signed_out_with_jira_mcp(self):
        import io, contextlib, shutil, subprocess
        real_which, real_list, real_run = shutil.which, qm.claude_mcp_list, subprocess.run
        shutil.which = lambda name, *a, **k: "/x/acli" if name == "acli" else None
        subprocess.run = lambda cmd, *a, **k: type("R", (), {"returncode": 1, "stdout": ""})()
        try:
            for listing, want, unwanted in (
                    ("claude.ai Atlassian: https://mcp.atlassian.com/v1/sse - ✔ Connected\n", "info acli not signed in", "FIX  acli"),
                    ("", "FIX  acli signed in to Jira", "info acli not signed in")):
                qm.claude_mcp_list = lambda listing=listing: listing
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    qm.doctor()
                self.assertIn(want, buf.getvalue()); self.assertNotIn(unwanted, buf.getvalue())
        finally:
            shutil.which, qm.claude_mcp_list, subprocess.run = real_which, real_list, real_run

    def test_round9_doctor_and_config_robustness(self):
        import io, contextlib
        cfg = Path(self.tmp.name) / "config.json"
        cfg.write_text(json.dumps({"app": 123, "qaBaseUrl": "https://qa.Example.com", "ssoHost": "sso",
                                   "buildSource": {}, "allowedHosts": [8080, "*.example.com"]}))
        real, real_data = qm.CONFIG, qm.DATA_DIR
        qm.CONFIG, qm.DATA_DIR = cfg, Path(self.tmp.name) / "data"  # never touch the real data dir
        os.environ.pop("QA_MEM_DB")
        try:
            self.assertTrue(str(qm.db_path()).endswith("123.db"))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                qm.doctor()
            self.assertIn("ok   qaBaseUrl host is in allowedHosts", buf.getvalue())
        finally:
            qm.CONFIG, qm.DATA_DIR = real, real_data
            os.environ["QA_MEM_DB"] = str(Path(self.tmp.name) / "t.db")

    def test_round9_hash_cli_redacts_like_ingest(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            qm.main(["hash", "/form", "500 with cookie: sid=abc123"])
        qm.ingest(self.con, qm.redact_json(run(bugs=[{"route": "/form", "title": "t", "symptom": "500 with cookie: sid=abc123"}])))
        self.assertEqual(buf.getvalue().strip(), self.con.execute("SELECT hash FROM bugs").fetchone()[0])

    def test_hooks_config_matches_plugin_and_agent_names(self):
        """SubagentStop matchers are exact agent types (<plugin>:<agent>), verified with a live probe."""
        hooks = json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"]
        plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["name"]
        agent = re.search(r"^name:\s*(\S+)", (ROOT / "agents/qa-explorer.md").read_text(), re.M).group(1)
        self.assertIn(f"{plugin}:{agent}", hooks["SubagentStop"][0]["matcher"].split("|"))
        # every tool: MCP names vary in case and server name, and the explorer's tool allowlist is enforced here
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "*")
        for event in ("SubagentStop", "Stop"):
            self.assertIn('"$CLAUDE_PROJECT_DIR/qa-out"', hooks[event][0]["hooks"][0]["command"])

    def test_bad_run_rejected(self):
        with self.assertRaises(ValueError):
            qm.ingest(self.con, {"build": "1"})  # no run_id
        qm.ingest(self.con, {"run_id": "x", "build": "", "pages": [{"route": "/e"}]})  # empty build: unknown
        self.assertIn("build=unknown", qm.card(self.con, "/e"))

    def test_parked_run_is_shown_to_the_agent_once(self):
        out = Path(self.tmp.name) / "qa-out" / "a"
        out.mkdir(parents=True)
        (out / "run.json").write_text(json.dumps({"run_id": "r", "build": "1", "pages": [{"route": "/x", "facts": "oops"}]}))
        env = dict(os.environ, QA_MEM_DB=str(Path(self.tmp.name) / "hook.db"))
        r = subprocess.run([sys.executable, str(ROOT / "bin/qa-mem"), "ingest-pending", str(out.parent)],
                           input='{"stop_hook_active": false}', capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("could not save this", r.stderr)
        self.assertIn(str(ROOT / "references" / "run-json.md"), r.stderr)  # r20: absolute, the project has no references/
        self.assertIn("did not write", r.stderr)  # a sibling explorer's parked file is not this agent's to fix
        (out / "run.json").write_text(json.dumps({"run_id": "r2", "build": "1", "pages": [{"route": "/x", "facts": 5}]}))
        r = subprocess.run([sys.executable, str(ROOT / "bin/qa-mem"), "ingest-pending", str(out.parent)],
                           input='{"stop_hook_active": true}', capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0)  # already blocked once this turn: no loop

    def test_hook_entrypoint_never_fails(self):
        r = subprocess.run([sys.executable, str(ROOT / "bin/qa-mem"), "ingest-pending", "/nonexistent"],
                           capture_output=True)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
