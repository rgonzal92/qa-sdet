import json, os, re, sqlite3, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from qa_sdet import core, scrub, memory, doctor, cli


def runfile(folder):
    """qa-out/<run>/.data/run-x.json: where an explorer writes its run JSON."""
    (folder / ".data").mkdir(parents=True, exist_ok=True)
    return folder / ".data" / "run-x.json"


def run(build="1.0", bugs=None, **page):
    return {"run_id": f"r-{build}-{time.time_ns()}", "mode": "explore", "target": "/form", "build": build,
            "status": "COMPLETED", "summary": "s",
            "pages": [{"route": "/form", "name": "Form", **page}], "bugs": bugs or []}



class QaMem(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = mock.patch.dict(os.environ)  # tests set and pop QA_MEM_DB: restore the whole environment afterwards
        env.start()
        self.addCleanup(env.stop)
        os.environ["QA_MEM_DB"] = str(Path(self.tmp.name) / "t.db")
        # never the user's real config or profile folder (doctor checks both)
        self.real_paths = (core.CONFIG, core.DATA_DIR)
        core.CONFIG, core.DATA_DIR = Path(self.tmp.name) / "cfg" / "config.json", Path(self.tmp.name) / "data"
        self.con = core.connect()

    def tearDown(self):
        self.con.close()
        core.CONFIG, core.DATA_DIR = self.real_paths
        self.tmp.cleanup()

    def test_card_shows_facts_locators_coverage_untried(self):
        memory.ingest(self.con, run(facts=[{"kind": "rule", "text": "Speed 0-130"}],
                                locators=[{"name": "Save", "locator": "getByRole('button')"}],
                                coverage=[{"target": "speed", "heuristic": "boundary", "result": "fail"}]))
        c = memory.card(self.con, "form")
        self.assertIn("Speed 0-130", c)
        self.assertIn("Save=getByRole('button')", c)
        self.assertIn("boundary(speed:fail)", c)
        untried = c.split("UNTRIED: ")[1].splitlines()[0]
        self.assertNotIn("boundary", untried)
        self.assertIn("happy-path", untried)

    def test_unknown_page_card(self):
        self.assertIn("NO CARD", memory.card(self.con, "/nope"))

    def test_clear_token_formats_are_redacted_and_look_alikes_stay(self):
        pem = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun\n"
               "-----END RSA PRIVATE KEY-----")
        secrets = ["eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijk", "AKIAIOSFODNN7EXAMPLE",
                   "ghp_" "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
                   "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz0123456789ABCD",
                   "xoxb-123456789012-abcdefghijkl", "ATATT3xFfGF0abcdefghijklmnopqrstuvwxyz0123456789=ABCDEF12",
                   "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789", "glpat-abcdefghijklmnopqrst", pem,
                   "Bearer 9f8c7d6e5b4a3f2e1d0c9f8c", "https://qa:Sommerregen@cache.example.com:6379/0",
                   "https://b.s3.amazonaws.com/x?X-Amz-Signature=abcdef0123456789abcdef",
                   "GET /api/zones?access_token=abc123def456", "GET /api/zones?api_key=abc123def456&x=1",
                   "export API_KEY=abcdefgh1234"]
        for sec in secrets:
            out = scrub.redact(f"value: {sec} end")
            self.assertIn("[REDACTED", out, sec[:20])
            self.assertNotIn("Sommerregen" if "Sommerregen" in sec else sec.splitlines()[-1][-12:], out, sec[:20])
            self.assertEqual(scrub.redact_literals(f"value: {sec} end"), out, sec[:20])  # code files keep these too
        for keep in ("class sk-folding-cube-container-large", "Bearer acknowledgements are shown", "token=1.42.0 released",
                     "password: required", "Token expired message shown", "session: morning shift", "GET /x?code=I5N&x=1",
                     "GET /reset?token=<the token from the email>", "GET /reset?token=${token}", "GET /reset?token=:token",
                     "the password field max 64 chars", "GET /x?key=password", "mailto:qa@example.com", "see https://qa.example.com:8443/app"):
            self.assertEqual(scrub.redact(keep), keep)
        self.assertEqual(scrub.redact(scrub.redact(f"a {secrets[0]} b {secrets[10]}")), scrub.redact(f"a {secrets[0]} b {secrets[10]}"))

    def test_auth_header_values_go_and_ordinary_words_stay(self):
        for text, secret in (("Authorization: Bearer 9f8c7d6e5b4a3f2e1d0c9f8c", "9f8c7d6e"),
                             ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
                             ("Authorization: SSWS 00QCjAl4MlV-WPXM1ABCDEFGHIJKLmnopqrstuv", "00QCjAl4"),
                             ("Cookie: a=1; session=abcdef123456", "abcdef123456"),
                             ("set-cookie: SESSION=abc123; Path=/; HttpOnly", "abc123"),
                             ("Request headers: Authorization: Bearer 9f8c7d6e5b4a3f2e1d0c9f8c, Cookie: sid=1a2b3c", "1a2b3c"),
                             ("X-Api-Key: zonesservicekey", "zonesservicekey")):
            out = scrub.redact(text)
            self.assertNotIn(secret, out, text)
            self.assertIn("[REDACTED]", out, text)
        self.assertEqual(scrub.redact('curl -H "X-Api-Key: zonesservicekey" https://x'), 'curl -H "X-Api-Key: [REDACTED]" https://x')
        self.assertEqual(scrub.redact('{"Authorization":"Bearer abc","Accept":"x"}'), '{"Authorization":"[REDACTED]","Accept":"x"}')
        self.assertEqual(scrub.redact("Cookie: sid=1\nUser-Agent: x"), "Cookie: [REDACTED]\nUser-Agent: x")  # one line only
        once = scrub.redact("Authorization: Bearer abc123def456ghi789jk\nCookie: sid=1")
        self.assertEqual(scrub.redact(once), once)  # idempotent: no re-ingest loop
        for keep in ("the Authorization header is checked", "no cookie banner", "Authorization", "password: this.form.value.password"):
            self.assertEqual(scrub.redact(keep), keep)

    def test_redaction_is_linear_on_long_runs(self):  # was quadratic: over a minute, past the Stop hook's timeout
        t0 = time.time()
        for text in ("authorization" + " " * 200000 + "x", "cookie" + "\t " * 100000, "Bearer" + " " * 200000 + "x",
                     "a-" * 50000, "a/" * 100000, "x" * 60 + ("Ab3_-" * 20000), "https://" + "a" * 100000, "?token=" * 20000):
            scrub.redact(text)
        self.assertLess(time.time() - t0, 5)

    def test_empty_or_numeric_draft_path(self):  # audit 3 r6
        bug = {"route": "/form", "title": "t", "symptom": "s", "draft_path": "/abs/bugs/BUG-1.md"}
        memory.ingest(self.con, run(bugs=[bug]))
        memory.ingest(self.con, run(bugs=[{**bug, "draft_path": ""}]), run_dir=Path(self.tmp.name))  # a re-sighting without one
        self.assertEqual(self.con.execute("SELECT draft_path FROM bugs").fetchone()[0], "/abs/bugs/BUG-1.md")
        memory.ingest(self.con, run(bugs=[{**bug, "symptom": "other", "draft_path": 7}]), run_dir=Path(self.tmp.name))
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 2)

    def test_late_run_keeps_newer_coverage_and_bug_dates(self):  # audit 3 r15
        cov = lambda r: [{"target": "speed", "heuristic": "boundary", "result": r}]
        bug = {"route": "/form", "title": "t", "symptom": "s"}
        memory.ingest(self.con, run(coverage=cov("fail"), bugs=[bug]), ts="2026-02-01T00:00:00Z")
        memory.ingest(self.con, run(coverage=cov("pass"), bugs=[bug]), ts="2026-01-01T00:00:00Z")  # an older file, ingested late
        self.assertEqual(self.con.execute("SELECT result, ts FROM coverage").fetchall()[0][:], ("fail", "2026-02-01T00:00:00Z"))
        self.assertEqual(tuple(self.con.execute("SELECT first_seen, last_seen, seen_count FROM bugs").fetchone()),
                         ("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", 2))
        memory.ingest(self.con, run(coverage=cov("pass")), ts="2026-03-01T00:00:00Z")
        self.assertEqual(self.con.execute("SELECT result FROM coverage").fetchone()[0], "pass")

    def test_repro_spec_loses_literal_tokens_only(self):  # audit 3 r24: this branch had no test
        f = Path(self.tmp.name) / "BUG-1.spec.ts"
        f.write_text("const password = 'hunter2secret';\nconst t = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijk';\n")
        scrub.scrub_file(f)
        self.assertEqual(f.read_text(), "const password = 'hunter2secret';\nconst t = '[REDACTED-JWT]';\n")

    def test_redact_json_cookie_lists_header_pairs_and_key_value_objects(self):  # audit 3 r34
        sec = "s3cr3tV4lue9x"
        for obj in ({"cookies": [{"name": "session", "value": sec}]}, {"cookies": {"session": sec}}, {"headers": [["Authorization", sec]]},
                    {"key": "password", "value": sec}, {"jwt": sec}):
            self.assertNotIn(sec, json.dumps(scrub.redact_json(obj)), obj)
        for obj in ({"key": "PROJ-1", "summary": "Bearer of bad news"}, {"name": "Speed limit", "value": "130"},
                    {"key": "speed", "value": "130"}, ["boundary", "pass"], {"notes": "the jwt cookies banner"}):
            self.assertEqual(scrub.redact_json(obj), obj)

    def test_reopening_a_bug_no_run_lists_removes_it(self):  # audit 3, after r50
        bug = {"route": "/form", "title": "t", "symptom": "s"}
        r = run(bugs=[bug])
        memory.ingest(self.con, r, src="a")
        h = self.con.execute("SELECT hash FROM bugs").fetchone()[0]
        cli.run_cmd(self.con, "bug-status", [h, "FILED"])
        memory.ingest(self.con, {**r, "bugs": []}, src="a")  # the run file was rewritten without it; triaged, so kept
        self.assertEqual(self.con.execute("SELECT status, seen_count FROM bugs").fetchone()[:], ("FILED", 0))
        cli.run_cmd(self.con, "bug-status", [h, "OPEN"])
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 0)

    def test_draft_id_in_hash_and_search_terms_with_symbols(self):  # audit 3: independent review after round 50
        # a bug with its draft id in `hash` is still stored
        memory.ingest(self.con, run(facts=[{"kind": "api", "text": "GET /api/zones returns 500 on empty filter"},
                                       {"kind": "note", "text": "speed chart loads slowly"}],
                                bugs=[{"route": "/form", "title": "Save accepts 131", "symptom": "saved at 131", "hash": "BUG-2"}]))
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 1)
        # search: terms with symbols
        self.assertIn("returns 500", memory.search(self.con, "/api/zones"))
        self.assertIn("no facts match", memory.search(self.con, "speed-limit"))
        self.assertIn("speed chart", memory.search(self.con, "speed -/api/zones"))

    def test_a_sighting_older_than_the_last_one_predates_the_fix(self):  # audit 3: third independent pass
        # a sighting older than the last one (ingested late) predates the fix
        bug = {"route": "/form", "title": "t", "symptom": "s"}
        memory.ingest(self.con, run(bugs=[bug]), ts="2026-11-10T00:00:00Z")
        h = self.con.execute("SELECT hash FROM bugs").fetchone()[0]
        self.con.execute("UPDATE bugs SET status='FIXED'")
        newest = self.con.execute("SELECT last_run FROM bugs").fetchone()[0]
        memory.ingest(self.con, run(bugs=[bug]), ts="2026-11-05T00:00:00Z")
        self.assertEqual(tuple(self.con.execute("SELECT status, last_run FROM bugs").fetchone()), ("FIXED", newest))
        memory.ingest(self.con, run(bugs=[bug]), ts="2026-11-20T00:00:00Z")
        self.assertEqual(self.con.execute("SELECT status FROM bugs WHERE hash=?", (h,)).fetchone()[0], "REGRESSED")

    def test_unparseable_url_is_no_route_not_a_traceback(self):  # audit 3 r1
        self.assertEqual(core.norm_route("http://[x"), "")
        self.assertIn("NO CARD", memory.card(self.con, "http://[x"))
        with self.assertRaises(ValueError):
            memory.ingest(self.con, {"run_id": "r", "pages": [{"route": "http://[x"}]})

    def test_bug_dedup_increments_seen_count(self):
        bug = {"route": "/form", "title": "Speed accepts 999", "symptom": "speed accepts 999",
               "severity": "HIGH", "classification": "CONFIRMED"}
        memory.ingest(self.con, run(bugs=[bug]))
        memory.ingest(self.con, run(bugs=[{**bug, "symptom": "Speed  accepts 999!"}]))
        rows = self.con.execute("SELECT seen_count FROM bugs").fetchall()
        self.assertEqual([r[0] for r in rows], [2])

    def test_reingesting_same_run_does_not_inflate_seen_count(self):
        bug = {"route": "/form", "title": "t", "symptom": "s"}
        r = run(bugs=[bug])
        memory.ingest(self.con, r)
        memory.ingest(self.con, r)
        self.assertEqual(self.con.execute("SELECT seen_count FROM bugs").fetchone()[0], 1)

    def test_rewritten_run_replaces_its_bugs_and_counts_runs_once(self):
        """A run.json rewritten mid-run (or ingested by a sibling explorer's hook first) replaces its earlier version."""
        bugs = lambda: [tuple(r) for r in self.con.execute("SELECT title, seen_count FROM bugs ORDER BY title")]
        x, y = {"route": "/f", "title": "X", "symptom": "x"}, {"route": "/f", "title": "Y", "symptom": "y"}
        a = lambda *b: {"run_id": "explore-f-1", "build": "1", "bugs": list(b)}
        memory.ingest(self.con, a({**x, "symptom": "first guess"}))
        memory.ingest(self.con, a(x))  # symptom corrected: the first guess was never a separate bug
        self.assertEqual(bugs(), [("X", 1)])
        memory.ingest(self.con, {"run_id": "explore-g-1", "build": "1", "bugs": [x]})
        memory.ingest(self.con, a(x, y))  # run A rewritten after run B saw X: still two runs
        self.assertEqual(bugs(), [("X", 2), ("Y", 1)])
        memory.ingest(self.con, a())  # A drops both: Y was only A's, X stays B's sighting
        self.assertEqual(bugs(), [("X", 1)])
        memory.ingest(self.con, a(x))  # and back again
        self.assertEqual(bugs(), [("X", 2)])
        self.con.execute("UPDATE bugs SET status='FILED'")
        memory.ingest(self.con, {"run_id": "explore-h-1", "build": "1", "bugs": [y]})
        memory.ingest(self.con, {"run_id": "explore-h-1", "build": "1", "bugs": []})
        self.assertEqual(bugs(), [("X", 2)])
        # two files that reuse one run id are separate files, not rewrites of each other
        memory.ingest(self.con, {"run_id": "same", "build": "1", "bugs": [y]}, src="qa-out/r/.data/run-a.json")
        memory.ingest(self.con, {"run_id": "same", "build": "1", "bugs": [x]}, src="qa-out/r/.data/run-b.json")
        self.assertEqual(bugs(), [("X", 3), ("Y", 1)])

    def test_unknown_build_plan_does_not_stale_real_knowledge(self):
        memory.ingest(self.con, run("1.0", facts=[{"kind": "rule", "text": "real"}],
                                coverage=[{"target": "x", "heuristic": "boundary"}]))
        memory.ingest(self.con, run("unknown", facts=[{"kind": "rule", "text": "from ticket"}]))
        c = memory.card(self.con, "/form")
        self.assertIn("build=1.0", c)
        self.assertNotIn("real *", c)
        self.assertIn("boundary(", c)

    def test_stale_command_resets_coverage_view(self):
        memory.ingest(self.con, run("1.0", coverage=[{"target": "x", "heuristic": "boundary"}]))
        cli.main(["stale", "2.0"])
        c = memory.card(self.con, "/form")
        self.assertIn("re-verify", c)
        self.assertIn("boundary", c.split("UNTRIED: ")[1])

    def test_bug_status_requires_prefix_and_valid_status(self):
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "t"}]))
        self.assertEqual(cli.main(["bug-status", "", "FIXED"]), 2)
        self.assertEqual(cli.main(["bug-status", "abcd", "WHATEVER"]), 2)
        self.assertEqual(self.con.execute("SELECT status FROM bugs").fetchone()[0], "OPEN")

    def test_search_survives_fts_syntax(self):
        memory.ingest(self.con, run(facts=[{"kind": "rule", "text": "I-405 closure"}]))
        self.assertIn("closure", memory.search(self.con, "I-405"))

    def test_bad_shapes_rejected(self):
        for bad in ([], {"run_id": "x", "build": "1", "pages": {"route": "/a"}},
                    {"run_id": "x", "build": "1", "bugs": ["oops"]}):
            with self.assertRaises(ValueError):
                memory.ingest(self.con, bad)

    def test_card_prefers_exact_then_shortest_route(self):
        for rt in ("/form/advanced", "/form"):
            memory.ingest(self.con, {"run_id": rt, "build": "1", "pages": [{"route": rt}]})
        self.assertIn("PAGE /form ", memory.card(self.con, "form"))

    def test_new_build_marks_old_knowledge_stale(self):
        memory.ingest(self.con, run("1.0", facts=[{"kind": "rule", "text": "old rule"}],
                                locators=[{"name": "Save", "locator": "x"}]))
        memory.ingest(self.con, run("2.0", facts=[{"kind": "rule", "text": "new rule"}]))
        c = memory.card(self.con, "/form")
        self.assertIn("old rule *", c)
        self.assertNotIn("new rule *", c)
        self.assertNotIn("LOCATORS", c)  # stale locator hidden
        # coverage from build 1.0 no longer counts
        self.assertIn("boundary", c.split("UNTRIED: ")[1])

    def test_text_capped_and_fts_search(self):
        memory.ingest(self.con, run(facts=[{"kind": "rule", "text": "closure schedule " + "x" * 500}]))
        t = self.con.execute("SELECT text FROM facts").fetchone()[0]
        self.assertEqual(len(t), core.MAX_TEXT)
        self.assertIn("closure", memory.search(self.con, "closure"))
        self.assertFalse(memory.search(self.con, "closure").endswith("*"))
        memory.ingest(self.con, run("2.0", facts=[{"kind": "rule", "text": "other"}]))  # new build stales old facts
        self.assertTrue(memory.search(self.con, "closure").endswith(" *"))

    def test_ingest_pending_is_idempotent_and_records_errors(self):
        out = Path(self.tmp.name) / "qa-out"
        (out / "a").mkdir(parents=True)
        (out / "b").mkdir()
        runfile(out / "a").write_text(json.dumps(run(facts=[{"kind": "note", "text": "hi"}])))
        runfile(out / "b").write_text("{not json")
        self.assertEqual(memory.ingest_pending(out), 1)
        self.assertTrue((out / "a/.data/run-x.json.ingested").exists())
        self.assertTrue((out / "b/.data/run-x.json.error").exists())
        self.assertEqual(memory.ingest_pending(out), 0)

    def test_stop_hook_command_skips_python_without_qa_out(self):  # perf r2: no Python start in unrelated projects
        hooks = json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"]
        cmd = hooks["Stop"][0]["hooks"][0]["command"]
        self.assertEqual(cmd, hooks["SubagentStop"][0]["hooks"][0]["command"])
        base = Path(self.tmp.name)
        proj, other = base / "proj", base / "other"
        for d in (proj, other):
            d.mkdir()
        stub = base / "bin"  # a python3 that notes it was started (audit pi-11: no db alone can't tell)
        stub.mkdir()
        (stub / "python3").write_text(f'#!/bin/sh\ntouch "{base}/python-started"\nexec "{sys.executable}" "$@"\n')
        (stub / "python3").chmod(0o755)
        env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(ROOT), "CLAUDE_PROJECT_DIR": str(proj), "QA_MEM_DB": str(base / "db/x.db"),
               "PATH": f"{stub}{os.pathsep}{os.environ.get('PATH', '')}"}
        sh = lambda cwd: subprocess.run(["bash", "-c", cmd], cwd=cwd, env=env, input="{}", capture_output=True, text=True)
        self.assertEqual(sh(other).returncode, 0)
        self.assertFalse((base / "db").exists())
        self.assertFalse((base / "python-started").exists())
        (proj / "qa-out/a").mkdir(parents=True)  # the project's folder, reached from another cwd
        runfile(proj / "qa-out/a").write_text(json.dumps(run(facts=[{"kind": "note", "text": "hi"}])))
        self.assertEqual(sh(other).returncode, 0)
        self.assertTrue((proj / "qa-out/a/.data/run-x.json.ingested").exists())
        (other / "qa-out/b").mkdir(parents=True)  # the cwd's folder alone
        runfile(other / "qa-out/b").write_text("{not json")
        r = sh(other)
        self.assertEqual(r.returncode, 2, r.stderr)  # the agent still hears about a run JSON it must fix
        self.assertTrue((other / "qa-out/b/.data/run-x.json.error").exists())

    def test_hook_in_unrelated_project_creates_no_db(self):
        os.environ["QA_MEM_DB"] = str(Path(self.tmp.name) / "fresh" / "x.db")
        self.assertEqual(memory.ingest_pending(Path(self.tmp.name) / "nope"), 0)
        self.assertFalse((Path(self.tmp.name) / "fresh").exists())

    def test_fixed_bug_seen_again_regresses_and_symptom_shown(self):
        bug = {"route": "/form", "title": "t", "symptom": "speed 999 saved"}
        memory.ingest(self.con, run(bugs=[bug]))
        h = core.bug_hash("/form", "speed 999 saved")
        self.assertEqual(cli.main(["bug-status", h[:6], "fixed"]), 0)
        self.assertIn(f"PREVIOUSLY FIXED (seen again = regression: put its hash in run.json):\n- {h} FIXED",
                      memory.card(self.con, "/form"))
        memory.ingest(self.con, run("1.1", bugs=[{"route": "/form", "title": "other words", "hash": h}]))
        c = memory.card(self.con, "/form")
        self.assertIn(f"{h} REGRESSED", c)
        self.assertIn("symptom: speed 999 saved", c)

    def test_blocked_stays_untried_and_paraphrased_facts_collapse(self):
        memory.ingest(self.con, run(coverage=[{"target": "api", "heuristic": "error-handling", "result": "blocked"}],
                                facts=[{"kind": "rule", "text": "ID, Road and Type buttons sort ascending"}]), "2026-11-22T10:00:00Z")
        memory.ingest(self.con, run(facts=[{"kind": "rule", "text": "ID, Road and Type header buttons sort ascending"}]),
                  "2026-11-22T11:00:00Z")
        c = memory.card(self.con, "/form")
        self.assertIn("error-handling", c.split("UNTRIED")[1])
        self.assertIn("header buttons", c)
        self.assertEqual(c.count("sort ascending"), 1)

    def test_round23_review_findings(self):
        out = Path(self.tmp.name) / "qa-out" / "a"
        out.mkdir(parents=True)
        f = runfile(out)
        f.write_text(json.dumps(run(facts=[{"kind": "note", "text": "login sets eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijk"}])))
        os.utime(f, (1, 1))
        (out.parent / ".scrubbed").write_text(str(time.time()))  # scrub skips the old file
        self.con.close()
        memory.ingest_pending(out.parent)
        self.con = core.connect()
        self.assertNotIn("eyJhbGci", " ".join(r[0] for r in self.con.execute("SELECT text FROM facts")))
        memory.ingest(self.con, {"run_id": "u", "build": "unknown", "pages": [{"route": "/u",
                  "coverage": [{"target": "t", "heuristic": "boundary"}]}]}, "2020-01-01T00:00:00Z")
        self.assertNotIn("boundary(", memory.card(self.con, "/u"))

    def test_detail_routes_share_one_card(self):
        self.assertEqual(core.norm_route("http://h/incidents/4521/?x=1"), "/incidents/:id")
        self.assertEqual(core.norm_route("/#/zones/12/"), "/#/zones/:id")
        self.assertEqual(core.norm_route("/v2/a//b"), "/v2/a/b")
        self.assertEqual(core.norm_route("https://qa.example.com/#!/incidents/4521"), "/#!/incidents/:id")
        self.assertNotEqual(core.norm_route("/#!/incidents"), core.norm_route("/#!/zones"))
        self.assertEqual(core.norm_route("/app#section"), "/app")
        for r in ['"I #5ge', "a b ?x", "/x/ 12 /y#/z ?q"]:  # idempotent, so a stored route finds itself
            self.assertEqual(core.norm_route(core.norm_route(r)), core.norm_route(r))
        memory.ingest(self.con, {"run_id": "d", "build": "1", "pages": [{"route": "/incidents/4521",
                  "facts": [{"kind": "rule", "text": "detail shows road"}]}]})
        self.assertIn("detail shows road", memory.card(self.con, "/incidents/77"))
        self.assertEqual(core.bug_hash(core.norm_route("/incidents/1"), "x"), core.bug_hash(core.norm_route("/incidents/2"), "x"))

    def test_size_caps(self):
        core.MAX_FACTS = 5
        try:
            for n in range(8):
                memory.ingest(self.con, run(facts=[{"kind": "note", "text": f"fact number {n} " + "w" * n}]), f"2026-11-22T10:0{n}:00Z")
            texts = [r[0] for r in self.con.execute("SELECT text FROM facts")]
            self.assertEqual(len(texts), 5)
            self.assertTrue(any("fact number 7" in t for t in texts))
            self.assertEqual(self.con.execute("SELECT count(*) FROM facts_fts").fetchone()[0], 5)
        finally:
            core.MAX_FACTS = 100

    def test_secret_named_json_keys(self):
        S = "Zk9sLmQ3cXR5UzRmVjJh"
        obj = {"PRIVATE_TOKEN": S, "secret_key": S, "privateKey": S, "bearerToken": S, "sessionToken": S, "API Key": S,
               "x-api-key": S, "tokenCount": 5, "password_hint": "123 shown", "primary key": "id", "csrf": "required"}
        got = scrub.redact_json(obj)
        for k in list(obj)[:7]:
            self.assertEqual(got[k], "[REDACTED]", k)
        for k in list(obj)[7:]:
            self.assertEqual(got[k], obj[k], k)
        self.assertEqual(scrub.redact_json({"cookies": [{"name": "sid", "value": S}]})["cookies"][0]["value"], "[REDACTED]")
        self.assertEqual(scrub.redact_json({"sid": S, "x": {"sid": "SEG-1"}}), {"sid": S, "x": {"sid": "SEG-1"}})  # site ids stay

    def test_jira_key_in_hash_creates_no_local_bug(self):
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "Known jira TRAF-9 bug", "hash": "TRAF-9"}]))
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 0)

    def test_na_build_is_unknown(self):
        memory.ingest(self.con, run("1.0", facts=[{"kind": "rule", "text": "real"}]))
        memory.ingest(self.con, run("N/A", facts=[{"kind": "rule", "text": "other"}]))
        self.assertIn("build=1.0", memory.card(self.con, "/form"))
        self.assertNotIn("real *", memory.card(self.con, "/form"))

    def test_search_with_broken_fts_syntax_falls_back_to_words(self):
        memory.ingest(self.con, run(facts=[{"kind": "rule", "text": "I-5 N closure: lane 2 (a-b) 50% done"}]))
        for q in ["closure AND", "NEAR(closure", "a-b", "I-5", "closure lane"]:
            self.assertIn("closure", memory.search(self.con, q), q)
        self.assertIn("no facts match", memory.search(self.con, "*"))

    def test_scrub_leaves_symlinks_alone(self):
        root = Path(self.tmp.name)
        (root / "qa-out" / "a").mkdir(parents=True)
        (root / "outside.txt").write_text("Authorization: Bearer Hunter2pass99abcdef\n")
        (root / "qa-out" / "a" / "notes.md").symlink_to(root / "outside.txt")
        memory.ingest_pending(root / "qa-out")
        self.assertTrue((root / "qa-out" / "a" / "notes.md").is_symlink())
        self.assertIn("Hunter2", (root / "outside.txt").read_text())  # files outside qa-out are never rewritten

    def test_build_whitespace_is_not_a_new_deploy(self):
        memory.ingest(self.con, run("Build:\n   1.42", facts=[{"kind": "rule", "text": "real"}]))
        memory.ingest(self.con, run("Build: 1.42", facts=[{"kind": "rule", "text": "other"}]))
        self.assertNotIn("real *", memory.card(self.con, "/form"))
        memory.ingest(self.con, run("x" * 5000, bugs=[{"route": "/form", "title": "t", "severity": "H" * 500}]))
        self.assertLessEqual(len(core.meta(self.con, "build")), core.MAX_TEXT)
        self.assertLessEqual(len(self.con.execute("SELECT severity FROM bugs").fetchone()[0]), 20)

    def test_card_size_is_bounded(self):
        memory.ingest(self.con, run(facts=[{"kind": "rule", "text": f"fact {i} " + "y" * 280} for i in range(30)],
                                locators=[{"name": f"loc{i} " + "n" * 250, "locator": "x" * 290} for i in range(30)],
                                coverage=[{"heuristic": h, "target": f"t{i} " + "z" * 280} for h in core.HEURISTICS for i in range(6)],
                                bugs=[{"route": "/form", "title": "b" * 300, "symptom": f"s{i} " + "q" * 280} for i in range(12)]))
        self.assertLess(len(memory.card(self.con, "/form")), 15000)  # ~3.5k tokens at worst

    def test_page_name_lookup_prefers_segment_and_exact_name(self):
        for route, name in [("/roadmap", "Roadmap"), ("/map.html", "Live map"), ("/zones", "Speed zones"), ("/z", "Zones")]:
            memory.ingest(self.con, {"run_id": route, "build": "1", "pages": [{"route": route, "name": name}]})
        self.assertIn("PAGE /map.html", memory.card(self.con, "map"))
        self.assertIn("PAGE /zones ", memory.card(self.con, "zones"))  # the route itself first
        self.assertIn("PAGE /map.html", memory.card(self.con, "live map"))  # then the exact page name

    def test_unreadable_build_spellings(self):
        memory.ingest(self.con, run("1.42.0", facts=[{"kind": "rule", "text": "real"}]))
        for b in ["undefined", "unknown (footer .version not found)", "Unknown", "UNKNOWN", "null"]:
            memory.ingest(self.con, run(b, coverage=[{"target": "t", "heuristic": "duplicate"}]))
        c = memory.card(self.con, "/form")
        self.assertIn("build=1.42.0", c)
        self.assertNotIn("real *", c)
        self.assertEqual({r[0] for r in self.con.execute("SELECT DISTINCT build FROM coverage")}, {"unknown"})
        self.assertIn("duplicate(", c)  # unknown-build coverage visible (within the week)

    def test_a_database_from_before_0_4_is_refused_untouched(self):
        import io, contextlib
        for ddl, missing in (("CREATE TABLE bugs(hash TEXT PRIMARY KEY, page TEXT, title TEXT, seen_count INTEGER)", "bugs lacks"),
                             ("CREATE TABLE codemap(page TEXT, kind TEXT, label TEXT, repo TEXT, area TEXT)", "codemap lacks src")):
            old = Path(self.tmp.name) / f"{missing.split()[0]}.db"
            c = sqlite3.connect(old)
            c.execute(ddl)
            c.commit()
            c.close()
            os.environ["QA_MEM_DB"] = str(old)
            with self.assertRaisesRegex(core.BadInput, f"from before qa-sdet 0.4 \\({missing}"):
                core.connect()
            c = sqlite3.connect(old)
            self.assertEqual(len(c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()), 1)  # nothing added
            c.close()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["card", "/x"]), 2)
            self.assertIn("from before qa-sdet 0.4", err.getvalue())

    def test_coverage_results_normalised(self):
        cov = [{"target": "t", "heuristic": h, "result": r} for h, r in
               [("boundary", "BLOCKED"), ("duplicate", "blocked (needs --faults)"), ("empty-null", "NOT_RUN"),
                ("keyboard-a11y", "skipped"), ("happy-path", "PASS"), ("invalid-format", "Failed")]]
        memory.ingest(self.con, run(coverage=cov))
        untried = memory.card(self.con, "/form").split("UNTRIED: ")[1].splitlines()[0]
        for h in ("boundary", "duplicate", "empty-null", "keyboard-a11y"):
            self.assertIn(h, untried)
        for h in ("happy-path", "invalid-format"):
            self.assertNotIn(h, untried)
        self.assertEqual({r[0] for r in self.con.execute("SELECT result FROM coverage")}, {"pass", "fail", "blocked"})

    def test_distinct_rules_not_collapsed(self):
        facts = ["Name field is required", "Email field is required", "Start date must be before end date",
                 "Start time must be before end time", "Speed limit must be between 0 and 130 km/h",
                 "Speed limit must be between 0 and 120 km/h"]
        memory.ingest(self.con, run(facts=[{"kind": "rule", "text": t} for t in facts]))
        c = memory.card(self.con, "/form")
        for t in facts:
            self.assertIn(t, c)

    def test_code_query_param_kept_in_specs(self):
        self.assertEqual(scrub.redact_literals("page.goto('/closures?code=I5N&x=1')"), "page.goto('/closures?code=I5N&x=1')")
        self.assertIn("[REDACTED]", scrub.redact_literals("page.goto('/x?access_token=abc123def456')"))

    def test_hash_field_variants(self):
        for i, junk in enumerate(["n/a", "none", "new", "TBD", ""]):
            memory.ingest(self.con, run(bugs=[{"route": "/form", "title": f"t{i}", "symptom": f"s{i}", "hash": junk}]))
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 5)
        h = core.bug_hash("/form", "s0")
        self.con.execute("UPDATE bugs SET status='FIXED' WHERE hash=?", (h,))
        self.con.commit()
        for variant in [f"{h} ", f"#{h}", f"{h} (known)"]:
            memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "x", "hash": variant}]))
        self.assertEqual(self.con.execute("SELECT status, seen_count FROM bugs WHERE hash=?", (h,)).fetchone()[:], ("REGRESSED", 4))
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "jira dup", "hash": "TRAF-9"}]))
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 5)

    def test_invalid_bugs_listed_as_not_bugs(self):
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "t", "symptom": "plural wording"}]))
        h = core.bug_hash("/form", "plural wording")
        self.con.close()
        cli.main(["bug-status", h, "INVALID"])
        self.con = core.connect()
        self.assertIn(f"NOT BUGS (the user marked these INVALID: don't report them):\n- {h} INVALID", memory.card(self.con, "/form"))

    def test_unknown_build_page_keeps_its_card(self):
        memory.ingest(self.con, {"run_id": "a", "build": "1.42.0", "pages": [{"route": "/incidents"}]})
        memory.ingest(self.con, {"run_id": "b", "build": "unknown", "pages": [{"route": "/map", "facts": [{"kind": "rule", "text": "layers switch"}],
                  "locators": [{"name": "Layer", "locator": "getByLabel('Layer')"}]}]})
        c = memory.card(self.con, "/map")
        self.assertIn("LOCATORS (fresh): Layer=getByLabel('Layer')", c)
        self.assertIn("- [rule] layers switch\n", c + "\n")  # not marked stale
        self.assertIn("never readable", c)

    def test_ticket_fact_becomes_observed(self):
        memory.ingest(self.con, run("unknown", facts=[{"kind": "rule", "text": "End date after start", "source": "TRAF-1"}]))
        self.assertIn("(from TRAF-1)", memory.card(self.con, "/form"))
        memory.ingest(self.con, run("1.0", facts=[{"kind": "rule", "text": "End date after start", "source": "observed"}]))
        self.assertNotIn("(from TRAF-1)", memory.card(self.con, "/form"))
        memory.ingest(self.con, run("unknown", facts=[{"kind": "rule", "text": "x", "source": "TRAF-2"}]))  # unverified stays 'from'
        self.assertIn("(from TRAF-2)", memory.card(self.con, "/form"))

    def test_bug_status_refuses_ambiguous_prefix(self):
        with self.con:
            for h in ("abcd11111111", "abcd22222222"):
                self.con.execute("INSERT INTO bugs(hash,page,title) VALUES(?,?,?)", (h, "/form", "t"))
        self.assertEqual(cli.main(["bug-status", "abcd", "FIXED"]), 1)
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs WHERE status='FIXED'").fetchone()[0], 0)
        self.assertEqual(cli.main(["bug-status", "abcd1", "FIXED"]), 0)
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs WHERE status='FIXED'").fetchone()[0], 1)

    def test_filed_bugs_stay_visible(self):
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "t"}]))
        h = self.con.execute("SELECT hash FROM bugs").fetchone()[0]
        cli.main(["bug-status", h, "FILED"])
        self.assertIn("FILED", memory.card(self.con, "/form"))

    def test_route_query_is_exact(self):
        memory.ingest(self.con, {"run_id": "a", "build": "1", "pages": [{"route": "/incidents/list"}]})
        self.assertIn("NO CARD", memory.card(self.con, "/incidents"))
        self.assertIn("matched from 'incidents'", memory.card(self.con, "incidents"))

    def test_transient_error_retries(self):
        out = Path(self.tmp.name) / "qa-out" / "a"
        out.mkdir(parents=True)
        runfile(out).write_text(json.dumps(run()))
        real = memory.ingest
        memory.ingest = lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))
        try:
            self.assertEqual(memory.ingest_pending(out.parent), 0)
        finally:
            memory.ingest = real
        self.assertFalse((out / ".data/run-x.json.error").exists())
        self.assertEqual(memory.ingest_pending(out.parent), 1)

    def test_old_run_does_not_roll_back_build(self):
        memory.ingest(self.con, run("1.1"), "2026-11-22T10:00:00Z")
        memory.ingest(self.con, run("1.0"), "2026-11-21T10:00:00Z")
        self.assertEqual(core.meta(self.con, "build"), "1.1")

    def test_plan_does_not_mark_verified(self):
        memory.ingest(self.con, {"run_id": "p", "build": "unknown", "pages": [{"route": "/new"}]})
        self.assertIn("verified=never", memory.card(self.con, "/new"))

    def test_heuristic_ids_normalised(self):
        memory.ingest(self.con, run(coverage=[{"target": "x", "heuristic": "Boundary"},
                                          {"target": "y", "heuristic": "Traffic time zone"}]))
        c = memory.card(self.con, "/form")
        self.assertIn("other:traffic-time-zone", c)
        self.assertNotIn("boundary", c.split("UNTRIED: ")[1].splitlines()[0])

    def test_bug_status_wildcards_and_stale_extra_args(self):
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "a"}, {"route": "/form", "title": "b"}]))
        self.assertEqual(cli.main(["bug-status", "____", "INVALID"]), 2)
        self.assertEqual(cli.main(["stale", "2.0", "x"]), 0)

    def test_round5_bad_scalars_are_parked_not_retried(self):
        out = Path(self.tmp.name) / "qa-out"
        for i, bad in enumerate([{"pages": [{"route": ["/a"]}]}, {"mode": {"x": 1}},
                                 {"bugs": [{"route": "/a", "title": "t", "severity": ["HIGH"]}]},
                                 {"status": 10 ** 30, "pages": [{"route": "/a", "facts": [{"text": {"a": 1}}]}]}]):
            d = out / str(i)
            d.mkdir(parents=True)
            runfile(d).write_text(json.dumps({"run_id": f"r{i}", "build": "1", **bad}))
        memory.ingest_pending(out)
        errors = sorted(p.parent.parent.name for p in out.rglob("run-x.json.error"))
        self.assertEqual(errors[:3], ["0", "1", "2"])

    def test_round5_late_old_run_does_not_roll_back_page(self):
        memory.ingest(self.con, run("1.1", facts=[{"kind": "rule", "text": "fresh"}]), "2026-11-22T10:00:00Z")
        memory.ingest(self.con, run("1.0", facts=[{"kind": "rule", "text": "old"}]), "2026-11-21T10:00:00Z")
        c = memory.card(self.con, "/form")
        self.assertIn("build=1.1", c)
        self.assertIn("fresh\n", c + "\n")
        self.assertNotIn("fresh *", c)

    def test_late_run_of_the_current_build_still_adds_what_it_saw(self):  # audit 2 r12
        memory.ingest(self.con, run("2.0", locators=[{"name": "Add", "locator": "getByRole('button', { name: 'Add' })"}]),
                  "2026-11-22T10:00:00Z")
        memory.ingest(self.con, run("2.0", facts=[{"kind": "rule", "text": "code max 6"}],
                                locators=[{"name": "Code", "locator": "getByLabel('Code')"}]), "2026-11-22T09:00:00Z")
        names = {r[0] for r in self.con.execute("SELECT name FROM locators")}
        self.assertEqual(names, {"Add", "Code"})
        self.assertEqual(self.con.execute("SELECT stale FROM facts WHERE text='code max 6'").fetchone()[0], 0)
        memory.ingest(self.con, run("1.0", locators=[{"name": "Old", "locator": "#old"}]), "2026-11-21T09:00:00Z")
        self.assertNotIn("Old", {r[0] for r in self.con.execute("SELECT name FROM locators")})  # older build: skipped

    def test_card_says_when_verified_locators_did_not_fit(self):  # audit 2 r13
        locs = [{"name": f"btn{i}", "locator": f"getByRole('button', {{ name: 'Button number {i} {'x' * 60}' }})"}
                for i in range(25)]
        memory.ingest(self.con, run("1.0", locators=locs))
        c = memory.card(self.con, "/form")
        self.assertRegex(c, r"\(\+\d+ more verified")

    def test_round5_plan_does_not_refresh_stale_fact(self):
        memory.ingest(self.con, run("1.0", facts=[{"kind": "rule", "text": "r"}]), "2026-11-20T10:00:00Z")
        memory.ingest(self.con, run("1.1"), "2026-11-21T10:00:00Z")  # r now stale
        memory.ingest(self.con, run("unknown", facts=[{"kind": "rule", "text": "r"}]), "2026-11-22T10:00:00Z")
        self.assertEqual(self.con.execute("SELECT stale, build FROM facts").fetchone()[:], (1, "1.0"))

    def test_round5_route_normalisation(self):
        memory.ingest(self.con, run())
        for q in ("/form/", "/form?x=1", "http://localhost:8765/form"):
            self.assertIn("PAGE /form ", memory.card(self.con, q), q)
        self.assertEqual(core.norm_route("/#/map"), "/#/map")
        with self.assertRaises(ValueError):
            memory.ingest(self.con, run(bugs=[{"title": "no route"}]))

    def test_round5_short_hash_prefix_and_unknown_coverage(self):
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "t", "symptom": "s"}]))
        h = core.bug_hash("/form", "s")
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "x", "hash": h[:8]}]))
        self.assertEqual(self.con.execute("SELECT count(*), max(seen_count) FROM bugs").fetchone()[:], (1, 2))
        memory.ingest(self.con, run("unknown", coverage=[{"target": "t", "heuristic": "duplicate"}]))
        self.assertIn("duplicate(", memory.card(self.con, "/form"))
        memory.ingest(self.con, run("unknown", coverage=[{"target": "t", "heuristic": "boundary"}]), "2020-01-01T00:00:00Z")
        self.assertNotIn("boundary(", memory.card(self.con, "/form"))  # unknown-build coverage expires after a week

    def test_ingest_pending_cli_dedupes_dirs_and_content_hash(self):
        out = Path(self.tmp.name) / "qa-out" / "a"
        out.mkdir(parents=True)
        runfile(out).write_text(json.dumps(run(bugs=[{"route": "/form", "title": "t"}])))
        cli.main(["ingest-pending", str(out.parent), str(out.parent) + "/", ""])
        self.assertEqual(self.con.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
        future = time.time() + 3600  # skewed mtime must not cause re-ingest loops
        os.utime(runfile(out), (future, future))
        self.assertEqual(memory.ingest_pending(out.parent), 0)
        self.assertLessEqual(self.con.execute("SELECT max(ts) FROM runs").fetchone()[0], core.now())

    def test_hash_cli_matches_ingest_normalisation(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["hash", "http://localhost:8765/form/", "speed", "999"])
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "t", "symptom": "speed 999"}]))
        self.assertEqual(buf.getvalue().strip(), self.con.execute("SELECT hash FROM bugs").fetchone()[0])

    def test_doctor_reports_config_problems(self):
        import io, contextlib
        cfg = Path(self.tmp.name) / "config.json"
        cfg.write_text(json.dumps({"app": "x", "qaBaseUrl": "https://qa.example.com", "allowedHosts": "qa.example.com"}))
        real = core.CONFIG
        core.CONFIG = cfg
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                problems = doctor.doctor()
        finally:
            core.CONFIG = real
        out = buf.getvalue()
        self.assertGreater(problems, 0)
        self.assertIn("FIX  allowedHosts is a list", out)
        self.assertIn("FIX  config has app", out)

    def test_doctor_empty_config_and_source_folders(self):  # audit 2 r19
        import io, contextlib, shutil
        cfg = Path(self.tmp.name) / "config.json"
        real, real_which = core.CONFIG, shutil.which
        core.CONFIG = cfg
        shutil.which = lambda name, *a, **k: None  # no playwright-cli, no acli
        try:
            for body, want in (({}, "FIX  config has app"),
                               ({"sourcePaths": "/x"}, "FIX  sourcePaths is a list"),
                               ({"sourcePaths": [str(Path(self.tmp.name) / "gone")]}, "not found")):
                cfg.write_text(json.dumps(body))
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    doctor.doctor()
                self.assertIn(want, buf.getvalue(), body)
            self.assertIn("FIX  playwright-cli on PATH  -> npm i -g @playwright/cli", buf.getvalue())
            self.assertIn("info no acli: tickets are pasted", buf.getvalue())
        finally:
            core.CONFIG, shutil.which = real, real_which
        self.assertTrue(doctor.pillow_fix())

    def test_redaction_audit2_tokens_and_false_positives(self):
        secrets = ["github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz0123456789ABCD",
                   "ATATT3xFfGF0abcdefghijklmnopqrstuvwxyz0123456789=ABCDEF12",
                   "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun\n"
                   "-----END RSA PRIVATE KEY-----", "https://b.s3.amazonaws.com/x?X-Amz-Signature=abcdef0123456789abcdef"]
        for sec in secrets:
            self.assertNotIn(sec.splitlines()[-1][-12:], scrub.redact(f"value: {sec} end"), sec[:20])
        for keep in ("Bearer acknowledgements are shown", "token=1.42.0 released", "password: required",
                     "the password field max 64 chars", "Token expired message shown"):
            self.assertEqual(scrub.redact(keep), keep)

    def test_search_terms_and_stale_correction(self):  # audit 2 r29
        memory.ingest(self.con, run("1.43.0", facts=[{"kind": "rule", "text": "speed max 130"},
                                                  {"kind": "rule", "text": "closure AND detour shown"}]))
        self.assertNotIn("max 130", memory.search(self.con, "speed -130"))
        self.assertIn("detour", memory.search(self.con, "closure NEAR detour"))
        self.assertIn("max 130", memory.search(self.con, 'speed "130'))
        cli.main(["stale", "1.44.0"])
        cli.main(["stale", "1.43.0"])  # corrected: what was verified on 1.43.0 is current again
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
            scrub.scrub_tree(root, budget=0)  # one file per pass
            passes += 1
            self.assertLess(passes, 10)
        self.assertEqual(passes, 5)
        for _ in range(5):  # each rewritten file is looked at once more (its change time is new), then the pass ends
            scrub.scrub_tree(root, budget=0)
        self.assertNotIn(" ", (root / ".scrubbed").read_text())  # a finished pass stores a plain time again
        late = root / "r0" / "bugs" / "BUG-2.md"  # audit 3 r36: copied in with an old mtime (cp -a, a restored backup)
        late.write_text("Authorization: Bearer abcdef0123456789abcdef")
        os.utime(late, (1_600_000_000, 1_600_000_000))
        scrub.scrub_tree(root)
        self.assertNotIn("abcdef0123", late.read_text())
        self.assertEqual(late.stat().st_mtime, 1_600_000_000)

    def test_ingest_redacts_drafts_and_run_json(self):
        out = Path(self.tmp.name) / "qa-out" / "a"
        (out / "bugs").mkdir(parents=True)
        (out / "bugs/BUG-1.md").write_text("Network: GET /api -> 500\nAuthorization: Bearer abcdefghijklmnopqrst\n")
        runfile(out).write_text(json.dumps(run(facts=[{"kind": "api", "text": "API needs Cookie: sid=abc123def"}])))
        self.assertEqual(memory.ingest_pending(out.parent), 1)
        draft = (out / "bugs/BUG-1.md").read_text()
        self.assertIn("[REDACTED]", draft)
        self.assertNotIn("abcdefghijklmnopqrst", draft)
        self.assertNotIn("abc123def", self.con.execute("SELECT group_concat(text) FROM facts").fetchone()[0])
        self.assertEqual(memory.ingest_pending(out.parent), 0)  # redaction is idempotent: no re-ingest loop

    def test_draft_paths_stored_absolute(self):
        proj = Path(self.tmp.name) / "proj"
        out = proj / "qa-out" / "form"
        (out / "bugs").mkdir(parents=True)
        (out / "bugs/BUG-1.md").write_text("x")
        runfile(out).write_text(json.dumps(run(bugs=[
            {"route": "/form", "title": "a", "draft_path": "qa-out/form/bugs/BUG-1.md"},
            {"route": "/form", "title": "b", "draft_path": "bugs/BUG-1.md"}])))
        memory.ingest_pending(proj / "qa-out")
        paths = {r[0] for r in self.con.execute("SELECT draft_path FROM bugs")}
        self.assertEqual(paths, {str((out / "bugs/BUG-1.md").resolve())})

    def test_round9_redaction_keeps_mtime_json_and_prose(self):
        out = Path(self.tmp.name) / "qa-out" / "old"
        (out / "bugs").mkdir(parents=True)
        memory.ingest(self.con, run("1.1", facts=[{"kind": "rule", "text": "new"}]), "2026-11-22T10:00:00Z")
        runfile(out).write_text(json.dumps(run("1.0", facts=[{"kind": "rule", "text": "cookie: sid=abc123"}])))
        old = time.time() - 3 * 86400
        os.utime(runfile(out), (old, old))
        (out / "bugs/BUG-1.md").write_text("Open Basic Settings; password: min 8 chars\n")
        (out / "evidence.json").write_text(json.dumps({"sessionId": 12345, "ok": True}))
        memory.ingest_pending(out.parent)
        self.assertEqual(core.meta(self.con, "build"), "1.1")  # redaction kept the old mtime: no rollback
        self.assertEqual((out / "bugs/BUG-1.md").read_text(), "Open Basic Settings; password: min 8 chars\n")
        self.assertEqual(json.loads((out / "evidence.json").read_text())["sessionId"], "[REDACTED]")

    def test_round9_scrubs_folders_without_run_json(self):
        out = Path(self.tmp.name) / "qa-out" / "crashed" / "bugs"
        out.mkdir(parents=True)
        (out / "BUG-1.md").write_text("Authorization: Bearer abcdefghijklmnopqrst\n")
        os.environ["QA_MEM_DB"] = str(Path(self.tmp.name) / "none" / "x.db")
        memory.ingest_pending(out.parent.parent)
        self.assertNotIn("abcdefghijklmnopqrst", (out / "BUG-1.md").read_text())
        self.assertFalse((Path(self.tmp.name) / "none").exists())  # no run.json: no DB touched

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
            got = doctor.installed_browsers()
            self.assertEqual({k: v[0] for k, v in got.items()}, {"chrome": True, "msedge": False, "firefox": False, "webkit": True})
            self.assertIn("playwright-cli install-browser firefox", got["firefox"][1])
            cfg = tmp / "config.json"
            real_cfg = core.CONFIG
            core.CONFIG = cfg
            try:
                for conf, want in (({}, "FIX  config has browser"), ({"browser": "opera"}, "FIX  config browser 'opera'"),
                                   ({"browser": "webkit"}, "ok   browser webkit installed"),
                                   ({"browser": "firefox"}, "FIX  browser firefox installed  -> playwright-cli install-browser firefox")):
                    cfg.write_text(json.dumps(conf))
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        doctor.doctor()
                    self.assertIn(want, buf.getvalue(), conf)
                    self.assertNotIn("Firefox installed", buf.getvalue())
            finally:
                core.CONFIG = real_cfg
        finally:
            os.path.isfile, shutil.which, subprocess.run = real_isfile, real_which, real_run

    def test_browser_detection_edge_cases(self):
        """Review findings: interrupted downloads, failed lookups, the browsers command."""
        import io, contextlib, shutil, subprocess
        tmp = Path(self.tmp.name)
        half = tmp / "firefox-9"; half.mkdir()  # no INSTALLATION_COMPLETE: an interrupted download
        real_isfile, real_which, real_run = os.path.isfile, shutil.which, subprocess.run
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
                ok, how = doctor.installed_browsers(("firefox",))["firefox"]
                self.assertFalse(ok, mode); self.assertIn(want, how, mode)
            os.path.isfile = lambda x: x == "/opt/microsoft/msedge/msedge"
            got = doctor.installed_browsers(("chrome", "msedge"))
            self.assertEqual((got["chrome"][0], got["msedge"]), (False, (True, "/opt/microsoft/msedge/msedge")))
            os.path.isfile = real_isfile
            subprocess.run = runner("timeout")
            cfg, real_cfg = tmp / "config.json", core.CONFIG
            core.CONFIG = cfg
            try:
                cfg.write_text(json.dumps({"browser": "firefox"}))
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    doctor.doctor()
                out = buf.getvalue()
                self.assertIn("info browser firefox: can't check", out)  # a failed lookup isn't a FIX
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(cli.main(["browsers"]), 0)
                self.assertRegex(buf.getvalue(), r"(?m)^chrome +(installed|missing)")
            finally:
                core.CONFIG = real_cfg
        finally:
            os.path.isfile, shutil.which, subprocess.run = real_isfile, real_which, real_run

    def test_doctor_acli_signed_in_or_out(self):
        import io, contextlib, shutil, subprocess
        real_which, real_run = shutil.which, subprocess.run
        shutil.which = lambda name, *a, **k: "/x/acli" if name == "acli" else None
        try:
            for code, want in ((1, "FIX  acli signed in to Jira  -> acli jira auth login"), (0, "ok   acli signed in to Jira")):
                subprocess.run = lambda cmd, *a, code=code, **k: type("R", (), {"returncode": code, "stdout": ""})()
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    doctor.doctor()
                self.assertIn(want, buf.getvalue())
        finally:
            shutil.which, subprocess.run = real_which, real_run

    def test_round9_doctor_and_config_robustness(self):
        import io, contextlib
        cfg = Path(self.tmp.name) / "config.json"
        cfg.write_text(json.dumps({"app": 123, "qaBaseUrl": "https://qa.Example.com", "ssoHost": "sso",
                                   "buildSource": {}, "allowedHosts": [8080, "*.example.com"]}))
        real, real_data = core.CONFIG, core.DATA_DIR
        core.CONFIG, core.DATA_DIR = cfg, Path(self.tmp.name) / "data"  # never touch the real data dir
        os.environ.pop("QA_MEM_DB")
        try:
            self.assertTrue(str(core.db_path()).endswith("123.db"))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                doctor.doctor()
            self.assertIn("ok   qaBaseUrl host is in allowedHosts", buf.getvalue())
        finally:
            core.CONFIG, core.DATA_DIR = real, real_data
            os.environ["QA_MEM_DB"] = str(Path(self.tmp.name) / "t.db")

    def test_round9_hash_cli_redacts_like_ingest(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["hash", "/form", "500 with cookie: sid=abc123"])
        memory.ingest(self.con, scrub.redact_json(run(bugs=[{"route": "/form", "title": "t", "symptom": "500 with cookie: sid=abc123"}])))
        self.assertEqual(buf.getvalue().strip(), self.con.execute("SELECT hash FROM bugs").fetchone()[0])

    def test_hooks_config_matches_plugin_and_agent_names(self):
        """SubagentStop matchers are exact agent types (<plugin>:<agent>), verified with a live probe."""
        hooks = json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"]
        plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["name"]
        agent = re.search(r"^name:\s*(\S+)", (ROOT / "agents/qa-explorer.md").read_text(), re.M).group(1)
        self.assertIn(f"{plugin}:{agent}", hooks["SubagentStop"][0]["matcher"].split("|"))
        # every tool: MCP names vary in case and server name, and the explorer's tool allowlist is enforced here
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "*")
        self.assertIn("/hooks/pretooluse.py", hooks["PreToolUse"][0]["hooks"][0]["command"])  # guard.py, bytecode cached
        for event in ("SubagentStop", "Stop"):
            self.assertIn('"$CLAUDE_PROJECT_DIR/qa-out"', hooks[event][0]["hooks"][0]["command"])

    def test_bad_run_rejected(self):
        with self.assertRaises(ValueError):
            memory.ingest(self.con, {"build": "1"})  # no run_id
        memory.ingest(self.con, {"run_id": "x", "build": "", "pages": [{"route": "/e"}]})  # empty build: unknown
        self.assertIn("build=unknown", memory.card(self.con, "/e"))

    def test_parked_run_is_shown_to_the_agent_once(self):
        out = Path(self.tmp.name) / "qa-out" / "a"
        out.mkdir(parents=True)
        runfile(out).write_text(json.dumps({"run_id": "r", "build": "1", "pages": [{"route": "/x", "facts": "oops"}]}))
        env = dict(os.environ, QA_MEM_DB=str(Path(self.tmp.name) / "hook.db"))
        r = subprocess.run([sys.executable, str(ROOT / "bin/qa-mem"), "ingest-pending", str(out.parent)],
                           input='{"stop_hook_active": false}', capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("could not save this", r.stderr)
        self.assertIn(str(ROOT / "references" / "run-json.md"), r.stderr)  # r20: absolute, the project has no references/
        self.assertIn("did not write", r.stderr)  # a sibling explorer's parked file is not this agent's to fix
        runfile(out).write_text(json.dumps({"run_id": "r2", "build": "1", "pages": [{"route": "/x", "facts": 5}]}))
        r = subprocess.run([sys.executable, str(ROOT / "bin/qa-mem"), "ingest-pending", str(out.parent)],
                           input='{"stop_hook_active": true}', capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0)  # already blocked once this turn: no loop

    def test_hook_entrypoint_never_fails(self):
        r = subprocess.run([sys.executable, str(ROOT / "bin/qa-mem"), "ingest-pending", "/nonexistent"],
                           capture_output=True)
        self.assertEqual(r.returncode, 0)


class Launcher(unittest.TestCase):
    """bin/qa-mem finds lib/qa_sdet however it is started: isolated sys.path, through a symlink, from any folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {**os.environ, "QA_SDET_DATA": str(Path(self.tmp.name) / "data"),
                    "QA_SDET_CONFIG": str(Path(self.tmp.name) / "cfg/config.json")}
        for leaked in ("PYTHONPATH", "QA_MEM_DB"):  # a developer's shell may set them
            self.env.pop(leaked, None)

    def where(self, launcher, cwd, env=None, flags=()):
        r = subprocess.run([sys.executable, *flags, str(launcher), "where"], cwd=cwd, env=env or self.env,
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(Path(r.stdout.strip()).parent, Path(self.tmp.name) / "data")

    def test_launcher_runs_with_a_safe_path(self):
        self.where(ROOT / "bin/qa-mem", self.tmp.name, {**self.env, "PYTHONSAFEPATH": "1"})
        self.where(ROOT / "bin/qa-mem", self.tmp.name, flags=("-I",))

    def test_launcher_runs_through_a_symlink(self):
        link = Path(self.tmp.name) / "linked" / "qa-mem"
        link.parent.mkdir()
        link.symlink_to(ROOT / "bin/qa-mem")
        self.where(link, self.tmp.name)

    def test_launcher_runs_from_another_folder(self):
        self.where(ROOT / "bin/qa-mem", "/")
        r = subprocess.run([sys.executable, str(ROOT / "bin/qa-mem"), "--help"], cwd=self.tmp.name, env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage: qa-mem <command>", r.stdout)


if __name__ == "__main__":
    unittest.main()
