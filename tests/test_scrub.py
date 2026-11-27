"""Redaction (qa_sdet.scrub): secrets never reach the memory DB, the drafts or the reports."""

import contextlib
import json
import locale
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import MemBase, core, run, runfile

from qa_sdet import ingest, scrub

BEARER = "Authorization: Bearer abcdefghijklmnop1234\n"  # a draft line with a planted token
HIDDEN = "Authorization: [REDACTED]\n"


@contextlib.contextmanager
def c_locale():
    """The C locale for the block: a file opened without an encoding is then read and written as ASCII."""
    before = locale.setlocale(locale.LC_CTYPE)
    locale.setlocale(locale.LC_CTYPE, "C")
    try:
        yield
    finally:
        locale.setlocale(locale.LC_CTYPE, before)


class Redact(unittest.TestCase):
    """scrub.redact, redact_literals and redact_json on text."""

    def test_clear_token_formats_are_redacted_and_look_alikes_stay(self):
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun\n"
            "-----END RSA PRIVATE KEY-----"
        )
        secrets = [
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijk",
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
            "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz0123456789ABCD",
            "xoxb-123456789012-abcdefghijkl",
            "ATATT3xFfGF0abcdefghijklmnopqrstuvwxyz0123456789=ABCDEF12",
            "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789",
            "glpat-abcdefghijklmnopqrst",
            pem,
            "Bearer 9f8c7d6e5b4a3f2e1d0c9f8c",
            "https://qa:Sommerregen@cache.example.com:6379/0",
            "https://b.s3.amazonaws.com/x?X-Amz-Signature=abcdef0123456789abcdef",
            "GET /api/zones?access_token=abc123def456",
            "GET /api/zones?api_key=abc123def456&x=1",
            "export API_KEY=abcdefgh1234",
        ]
        for sec in secrets:
            out = scrub.redact(f"value: {sec} end")
            self.assertIn("[REDACTED", out, sec[:20])
            self.assertNotIn("Sommerregen" if "Sommerregen" in sec else sec.splitlines()[-1][-12:], out, sec[:20])
            self.assertEqual(scrub.redact_literals(f"value: {sec} end"), out, sec[:20])  # code files keep these too
        for keep in (
            "class sk-folding-cube-container-large",
            "Bearer acknowledgements are shown",
            "token=1.42.0 released",
            "password: required",
            "Token expired message shown",
            "session: morning shift",
            "GET /x?code=I5N&x=1",
            "GET /reset?token=<the token from the email>",
            "GET /reset?token=${token}",
            "GET /reset?token=:token",
            "the password field max 64 chars",
            "GET /x?key=password",
            "mailto:qa@example.com",
            "see https://qa.example.com:8443/app",
        ):
            self.assertEqual(scrub.redact(keep), keep)
        self.assertEqual(
            scrub.redact(scrub.redact(f"a {secrets[0]} b {secrets[10]}")),
            scrub.redact(f"a {secrets[0]} b {secrets[10]}"),
        )

    def test_auth_header_values_go_and_ordinary_words_stay(self):
        for text, secret in (
            ("Authorization: Bearer 9f8c7d6e5b4a3f2e1d0c9f8c", "9f8c7d6e"),
            ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
            ("Authorization: SSWS 00QCjAl4MlV-WPXM1ABCDEFGHIJKLmnopqrstuv", "00QCjAl4"),
            ("Cookie: a=1; session=abcdef123456", "abcdef123456"),
            ("set-cookie: SESSION=abc123; Path=/; HttpOnly", "abc123"),
            ("Request headers: Authorization: Bearer 9f8c7d6e5b4a3f2e1d0c9f8c, Cookie: sid=1a2b3c", "1a2b3c"),
            ("X-Api-Key: zonesservicekey", "zonesservicekey"),
        ):
            out = scrub.redact(text)
            self.assertNotIn(secret, out, text)
            self.assertIn("[REDACTED]", out, text)
        self.assertEqual(
            scrub.redact('curl -H "X-Api-Key: zonesservicekey" https://x'), 'curl -H "X-Api-Key: [REDACTED]" https://x'
        )
        self.assertEqual(
            scrub.redact('{"Authorization":"Bearer abc","Accept":"x"}'), '{"Authorization":"[REDACTED]","Accept":"x"}'
        )
        self.assertEqual(
            scrub.redact("Cookie: sid=1\nUser-Agent: x"), "Cookie: [REDACTED]\nUser-Agent: x"
        )  # one line only
        once = scrub.redact("Authorization: Bearer abc123def456ghi789jk\nCookie: sid=1")
        self.assertEqual(scrub.redact(once), once)  # idempotent: no re-ingest loop
        for keep in (
            "the Authorization header is checked",
            "no cookie banner",
            "Authorization",
            "password: this.form.value.password",
        ):
            self.assertEqual(scrub.redact(keep), keep)

    def test_redaction_is_linear_on_long_runs(self):  # was quadratic: over a minute, past the Stop hook's timeout
        t0 = time.time()
        for text in (
            "authorization" + " " * 200000 + "x",
            "cookie" + "\t " * 100000,
            "Bearer" + " " * 200000 + "x",
            "a-" * 50000,
            "a/" * 100000,
            "x" * 60 + ("Ab3_-" * 20000),
            "https://" + "a" * 100000,
            "?token=" * 20000,
        ):
            scrub.redact(text)
        self.assertLess(time.time() - t0, 5)

    def test_code_query_param_kept_in_specs(self):
        self.assertEqual(
            scrub.redact_literals("page.goto('/closures?code=I5N&x=1')"), "page.goto('/closures?code=I5N&x=1')"
        )
        self.assertIn("[REDACTED]", scrub.redact_literals("page.goto('/x?access_token=abc123def456')"))

    def test_redact_json_cookie_lists_header_pairs_and_key_value_objects(self):
        sec = "s3cr3tV4lue9x"
        for obj in (
            {"cookies": [{"name": "session", "value": sec}]},
            {"cookies": {"session": sec}},
            {"headers": [["Authorization", sec]]},
            {"key": "password", "value": sec},
            {"jwt": sec},
            {"cookies": [{"name": "session", "value": "ok", "token": sec, "url": f"https://u:{sec}@host/"}]},
        ):
            self.assertNotIn(sec, json.dumps(scrub.redact_json(obj)), obj)
        for obj in (
            {"key": "PROJ-1", "summary": "Bearer of bad news"},
            {"name": "Speed limit", "value": "130"},
            {"key": "speed", "value": "130"},
            ["boundary", "pass"],
            {"notes": "the jwt cookies banner"},
        ):
            self.assertEqual(scrub.redact_json(obj), obj)

    def test_redact_json_keeps_the_shape_and_marks_each_secret(self):  # exact output: mutation testing showed gaps
        sec, R = "s3cr3tV4lue9x", "[REDACTED]"
        for obj, want in (
            (  # a cookie jar as an object, then more keys: every one still read, the empty value kept
                {"cookies": {"session": sec, "empty": ""}, "token": sec, "n": 1},
                {"cookies": {"session": R, "empty": ""}, "token": R, "n": 1},
            ),
            (
                {"cookies": [{"name": "session", "value": sec}, {"name": "x"}]},
                {"cookies": [{"name": "session", "value": R}, {"name": "x"}]},
            ),
            (  # HAR headers: {name, value}, the name says what the value is
                {
                    "headers": [
                        {"name": "Authorization", "value": f"Bearer {sec}"},
                        {"name": "Accept", "value": "text/html"},
                    ]
                },
                {"headers": [{"name": "Authorization", "value": R}, {"name": "Accept", "value": "text/html"}]},
            ),
            ({"headers": [["Cookie", sec], ["Accept", "x"]]}, {"headers": [["Cookie", R], ["Accept", "x"]]}),
            ({"password": "", "apiKey": None, "secret": False}, {"password": "", "apiKey": None, "secret": False}),
        ):
            with self.subTest(obj=obj):
                self.assertEqual(scrub.redact_json(obj), want)
        self.assertEqual(
            scrub.redact_json_text('{"password": "", "token": "abc", "n": 1}'),
            '{"password": "", "token": "[REDACTED]", "n": 1}',
        )

    def test_secret_named_json_keys(self):
        S = "Zk9sLmQ3cXR5UzRmVjJh"
        obj = {
            "PRIVATE_TOKEN": S,
            "secret_key": S,
            "privateKey": S,
            "bearerToken": S,
            "sessionToken": S,
            "API Key": S,
            "x-api-key": S,
            "tokenCount": 5,
            "password_hint": "123 shown",
            "primary key": "id",
            "csrf": "required",
        }
        got = scrub.redact_json(obj)
        for k in list(obj)[:7]:
            self.assertEqual(got[k], "[REDACTED]", k)
        for k in list(obj)[7:]:
            self.assertEqual(got[k], obj[k], k)
        self.assertEqual(
            scrub.redact_json({"sid": S, "x": {"sid": "SEG-1"}}), {"sid": S, "x": {"sid": "SEG-1"}}
        )  # site ids stay


class ScrubFiles(MemBase):
    """Drafts, repro specs, reports and run files under qa-out are scrubbed in place (`ingest-pending`)."""

    def test_repro_spec_loses_literal_tokens_only(self):
        f = Path(self.tmp.name) / "BUG-1.spec.ts"
        f.write_text(
            "const password = 'hunter2secret';\nconst t = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijk';\n"
        )
        scrub.scrub_file(f)
        self.assertEqual(f.read_text(), "const password = 'hunter2secret';\nconst t = '[REDACTED-JWT]';\n")

    def test_a_scrubbed_file_keeps_its_mode_and_leaves_no_temp_file(self):  # mutation testing: mode was unchecked
        f = Path(self.tmp.name) / "BUG-2.md"
        f.write_text("Authorization: Bearer abcdefghijklmnop1234\n")
        os.chmod(f, 0o640)
        scrub.scrub_file(f)
        self.assertEqual(f.read_text(), "Authorization: [REDACTED]\n")
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o640)  # never wider (world-writable, setuid) than it was
        self.assertEqual(sorted(p.name for p in f.parent.iterdir() if p.name.startswith(".BUG-2")), [])

    def test_network_evidence_text_loses_json_secret_values(self):
        out = Path(self.tmp.name) / "evidence"
        out.mkdir()
        f = out / "BUG-1-network.txt"
        body = (
            '{"user":"ann","password":"hunter2pw","clientSecret": "c1ientS3cretv","apiKey":"k3yV4lue99",'
            '"session_id":"sess1234abcd","retries":3,"password_hint":"first pet","nested":{"token":"t0kenV4lue"}}'
        )
        f.write_text(f"[1] POST /api/login => [500]\n{body}\nresponse: {body[:60]}")  # a truncated body too
        scrub.scrub_file(f)
        got = f.read_text()
        for secret in ("hunter2pw", "c1ientS3cretv", "k3yV4lue99", "sess1234abcd", "t0kenV4lue"):
            self.assertNotIn(secret, got)
        for keep in ('"user":"ann"', '"retries":3', '"password_hint":"first pet"', "POST /api/login => [500]"):
            self.assertIn(keep, got)
        scrub.scrub_file(f)
        self.assertEqual(f.read_text(), got)  # idempotent
        (out / "a.log").write_text('{"password": "hunter2pw"}')
        scrub.scrub_file(out / "a.log")
        self.assertEqual((out / "a.log").read_text(), '{"password": "[REDACTED]"}')

    def test_ingest_redacts_drafts_and_run_json(self):
        out = Path(self.tmp.name) / "qa-out" / "a"
        (out / "bugs").mkdir(parents=True)
        (out / "bugs/BUG-1.md").write_text("Network: GET /api -> 500\nAuthorization: Bearer abcdefghijklmnopqrst\n")
        runfile(out).write_text(json.dumps(run(facts=[{"kind": "api", "text": "API needs Cookie: sid=abc123def"}])))
        self.assertEqual(ingest.ingest_pending(out.parent), 1)
        draft = (out / "bugs/BUG-1.md").read_text()
        self.assertIn("[REDACTED]", draft)
        self.assertNotIn("abcdefghijklmnopqrst", draft)
        self.assertNotIn("abc123def", self.con.execute("SELECT group_concat(text) FROM facts").fetchone()[0])
        self.assertEqual(ingest.ingest_pending(out.parent), 0)  # redaction is idempotent: no re-ingest loop

    def test_redaction_keeps_the_run_files_mtime_and_plain_prose(self):
        out = Path(self.tmp.name) / "qa-out" / "old"
        (out / "bugs").mkdir(parents=True)
        newer = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400)
        )  # newer than the file, whatever today is
        ingest.ingest(self.con, run("1.1", facts=[{"kind": "rule", "text": "new"}]), newer)
        runfile(out).write_text(json.dumps(run("1.0", facts=[{"kind": "rule", "text": "cookie: sid=abc123"}])))
        old = time.time() - 3 * 86400
        os.utime(runfile(out), (old, old))
        (out / "bugs/BUG-1.md").write_text("Open Basic Settings; password: min 8 chars\n")
        (out / "evidence.json").write_text(json.dumps({"sessionId": 12345, "ok": True}))
        ingest.ingest_pending(out.parent)
        self.assertEqual(core.meta(self.con, "build"), "1.1")  # redaction kept the old mtime: no rollback
        self.assertEqual((out / "bugs/BUG-1.md").read_text(), "Open Basic Settings; password: min 8 chars\n")
        self.assertEqual(json.loads((out / "evidence.json").read_text())["sessionId"], "[REDACTED]")

    def test_a_folder_without_a_run_file_is_scrubbed_and_touches_no_db(self):
        out = Path(self.tmp.name) / "qa-out" / "crashed" / "bugs"
        out.mkdir(parents=True)
        (out / "BUG-1.md").write_text("Authorization: Bearer abcdefghijklmnopqrst\n")
        os.environ["QA_MEM_DB"] = str(Path(self.tmp.name) / "none" / "x.db")
        ingest.ingest_pending(out.parent.parent)
        self.assertNotIn("abcdefghijklmnopqrst", (out / "BUG-1.md").read_text())
        self.assertFalse((Path(self.tmp.name) / "none").exists())  # no run.json: no DB touched

    def test_scrub_leaves_symlinks_alone(self):
        root = Path(self.tmp.name)
        (root / "qa-out" / "a").mkdir(parents=True)
        (root / "outside.txt").write_text("Authorization: Bearer Hunter2pass99abcdef\n")
        (root / "qa-out" / "a" / "notes.md").symlink_to(root / "outside.txt")
        ingest.ingest_pending(root / "qa-out")
        self.assertTrue((root / "qa-out" / "a" / "notes.md").is_symlink())
        self.assertIn("Hunter2", (root / "outside.txt").read_text())  # files outside qa-out are never rewritten

    def test_scrub_backlog_is_worked_off_in_bounded_passes(self):  # hook timeout safety
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
        late = root / "r0" / "bugs" / "BUG-2.md"  # copied in with an old mtime (cp -a, a restored backup)
        late.write_text("Authorization: Bearer abcdef0123456789abcdef")
        os.utime(late, (1_600_000_000, 1_600_000_000))
        scrub.scrub_tree(root)
        self.assertNotIn("abcdef0123", late.read_text())
        self.assertEqual(late.stat().st_mtime, 1_600_000_000)

    def test_scrub_ignores_planted_temp_link(self):  # .<draft>.scrub was opened following links
        d = self.rundir("c")
        victim = self.t / "victim"
        victim.write_text("keep")
        draft = d / "bugs/BUG-1.md"
        draft.write_text("Authorization: Bearer abcdefghijklmnop1234\n")
        (d / "bugs/.BUG-1.md.scrub").symlink_to(victim)
        (d / "REPORT.md").write_text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz\n")
        (self.proj / "qa-out/INBOX.md").write_text("| NEW | AB-1 | api_key=abcdefghij123456 |\n")
        scrub.scrub_tree(self.proj / "qa-out")
        self.assertEqual(victim.read_text(), "keep")
        self.assertFalse(draft.is_symlink())
        self.assertEqual(draft.read_text(), "Authorization: [REDACTED]\n")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", (d / "REPORT.md").read_text())
        self.assertNotIn("abcdefghij123456", (self.proj / "qa-out/INBOX.md").read_text())
        self.assertEqual(sorted(p.name for p in (d / "bugs").iterdir()), [".BUG-1.md.scrub", "BUG-1.md"])

    def test_scrub_keeps_an_explorers_rewrite(self):
        f = self.t / "BUG-1.md"
        f.write_text("Authorization: Bearer abcdefabcdefabcdef1234\n")
        real = scrub.redact

        def rewrite_meanwhile(text):
            time.sleep(0.01)
            f.write_text("NEW draft\n")
            return real(text)

        with mock.patch.object(scrub, "redact", rewrite_meanwhile):
            scrub.scrub_file(f)
        self.assertEqual(f.read_text(), "NEW draft\n")
        self.assertEqual([p.name for p in self.t.iterdir() if p.name.startswith(".BUG")], [])

    def test_round2_scrub_surrogates_url_query_case_and_symlink(self):
        # H4: lone surrogate in JSON must not crash scrub_file or abort scrub_tree
        d = self.rundir("sur")
        jf = d / ".data/run-sur.json"
        jf.write_text('{"note": "\\ud800", "token": "secret123456"}', encoding="utf-8")
        scrub.scrub_tree(self.proj / "qa-out")
        self.assertNotIn("secret123456", jf.read_text(encoding="utf-8"))
        # M1: URL with port + @ in query/fragment stays intact
        self.assertEqual(
            scrub.redact("http://localhost:8765?email=qa@example.com"), "http://localhost:8765?email=qa@example.com"
        )
        self.assertEqual(scrub.redact("https://qa.example.com:8443#user@host"), "https://qa.example.com:8443#user@host")
        # M6: planted symlink at qa-out/.scrubbed is not written through
        victim = self.t / "scrub-victim"
        victim.write_text("keep")
        stamp = self.proj / "qa-out/.scrubbed"
        stamp.unlink(missing_ok=True)
        stamp.symlink_to(victim)
        scrub.scrub_tree(self.proj / "qa-out")
        self.assertEqual(victim.read_text(), "keep")
        # L6 + case-insensitive suffixes: prefixed session_id, numeric 0/1 secrets, uppercase .MD/.JSON
        got = scrub.redact_json(
            {"user_session_id": "abc12345", "authSessionId": "xyz98765", "secret": 0, "token": 1, "api_key": False}
        )
        self.assertEqual(
            got,
            {
                "user_session_id": "[REDACTED]",
                "authSessionId": "[REDACTED]",
                "secret": "[REDACTED]",
                "token": "[REDACTED]",
                "api_key": False,
            },
        )
        up = d / "bugs/BUG-9.MD"
        up.write_text("Authorization: Bearer abcdefghijklmnop1234\n", encoding="utf-8")
        upj = d / "evidence.JSON"
        upj.write_text('{"password": "hunter2secret"}', encoding="utf-8")
        stamp.unlink(missing_ok=True)
        scrub.scrub_tree(self.proj / "qa-out")
        self.assertNotIn("abcdefghijklmnop1234", up.read_text(encoding="utf-8"))
        self.assertNotIn("hunter2secret", upj.read_text(encoding="utf-8"))


class ScrubGaps(MemBase):
    """What mutation testing found unchecked in scrub_file and scrub_tree (`make mutation`): each test fails for a
    change to one of the two that the suite let through before."""

    def tree(self, *names, text=BEARER):
        """qa-out with these files in its folder `a`, each holding `text`: (qa-out, the files)."""
        root = self.proj / "qa-out"
        files = [root / "a" / n for n in names]
        for f in files:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        return root, files

    def test_a_json_file_is_rewritten_only_to_hide_a_secret(self):  # its layout and its encoding
        plain = self.t / "plan.json"
        plain.write_text('{"note":"café","n":1}', encoding="utf-8")
        scrub.scrub_file(plain)
        self.assertEqual(plain.read_text(encoding="utf-8"), '{"note":"café","n":1}')  # as written: not re-indented
        secret = self.t / "run-x.json"
        secret.write_bytes(b"\xef\xbb\xbf" + '{"note":"café","token":"secret123456"}'.encode())  # a byte order mark
        with c_locale():  # read and written as UTF-8, whatever the system's locale
            scrub.scrub_file(secret)
        self.assertEqual(secret.read_bytes(), '{\n  "note": "café",\n  "token": "[REDACTED]"\n}\n'.encode())

    def test_a_json_key_no_encoding_can_write_does_not_keep_the_secret_beside_it(self):
        root, (f,) = self.tree("run-x.json", text='{"\\ud800": 1, "token": "secret123456"}')  # a lone surrogate
        scrub.scrub_tree(root)
        self.assertEqual(json.loads(f.read_text(encoding="utf-8"))["token"], "[REDACTED]")

    def test_the_rewrite_is_made_beside_the_file_not_in_the_systems_temp_folder(self):
        root, (draft,) = self.tree("BUG-1.md")
        with mock.patch.object(tempfile, "tempdir", str(self.t / "another-disk")):  # not there: nothing can use it
            scrub.scrub_tree(root)
        self.assertEqual(draft.read_text(), HIDDEN)
        self.assertTrue((root / ".scrubbed").is_file())

    def test_a_rename_that_fails_or_is_interrupted_leaves_no_temp_file(self):
        root, (draft,) = self.tree("BUG-1.md")
        names = lambda: sorted(f.name for folder in (root, draft.parent) for f in folder.iterdir())
        with mock.patch.object(os, "replace", side_effect=PermissionError("read-only")):
            with self.assertRaises(PermissionError):
                scrub.scrub_file(draft)
            scrub.scrub_tree(root)  # neither a draft nor a stamp that can't be written stops the hook
        self.assertEqual((draft.read_text(), names()), (BEARER, ["BUG-1.md", "a"]))
        real = os.replace

        def renamed_then_interrupted(src, dst):
            real(src, dst)
            raise KeyboardInterrupt  # Ctrl-C lands right after the rename

        with mock.patch.object(os, "replace", renamed_then_interrupted):
            with self.assertRaises(KeyboardInterrupt):  # passed on as it is: no "file not found" in its place
                scrub.scrub_file(draft)
            with self.assertRaises(KeyboardInterrupt):
                scrub.scrub_tree(root)
        self.assertEqual((draft.read_text(), names()), (HIDDEN, [".scrubbed", "BUG-1.md", "a"]))

    def test_a_finished_pass_spares_the_files_it_saw(self):  # the stamp was written, and nothing checked it was read
        root, (draft,) = self.tree("BUG-1.md")
        changed = max(draft.stat().st_mtime, draft.stat().st_ctime)
        stamp = root / ".scrubbed"
        stamp.write_text(str(changed + 2.5))  # the last pass started 2.5 s after the draft changed
        with mock.patch.object(scrub, "scrub_file") as read_again:
            scrub.scrub_tree(root)
        read_again.assert_not_called()
        stamp.write_text(str(changed + 1))  # within 2 s: the draft may have been written while that pass ran
        started = time.time()
        scrub.scrub_tree(root)
        self.assertEqual(draft.read_text(), HIDDEN)
        self.assertTrue(started <= float(stamp.read_text()) <= time.time())  # a finished pass stores its start time

    def test_a_pass_that_stopped_early_goes_on_after_the_file_it_reached(self):  # the cursor
        root = self.proj / "qa-out"
        clean, dirty = root / "café" / "BUG-1.md", root / "café" / "BUG-2.md"
        clean.parent.mkdir(parents=True)
        clean.write_text("nothing to hide\n")  # first in line and never rewritten: only the cursor gets past it
        dirty.write_text(BEARER)
        with c_locale():  # the stamp is UTF-8, whatever the system's locale
            scrub.scrub_tree(root, budget=0)  # one file, then out of time
            self.assertEqual((root / ".scrubbed").read_text(encoding="utf-8").partition(" ")[2], str(clean))
            scrub.scrub_tree(root, budget=0)
        self.assertEqual(dirty.read_text(), HIDDEN)

    def test_a_file_name_that_is_not_utf8_is_scrubbed_and_does_not_break_the_stamp(self):
        root = self.proj / "qa-out"
        odd = [root / "a" / (b"BUG-%d\xff.md" % n).decode("utf-8", "surrogateescape") for n in (1, 2)]
        odd[0].parent.mkdir(parents=True)
        for f in odd:
            f.write_text(BEARER)
        scrub.scrub_tree(root, budget=0)  # stopped after one: the stamp names a file no encoding can write
        self.assertIn(" ", (root / ".scrubbed").read_text(encoding="utf-8"))
        scrub.scrub_tree(root, budget=0)
        self.assertEqual([f.read_text() for f in odd], [HIDDEN, HIDDEN])

    def test_a_repro_spec_is_scrubbed_by_the_last_part_of_its_name(self):  # no test had a name with two dots
        root, (spec,) = self.tree(
            "repro/BUG-1.spec.ts", text="const t = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijk';\n"
        )
        scrub.scrub_tree(root)
        self.assertEqual(spec.read_text(), "const t = '[REDACTED-JWT]';\n")

    def test_a_file_that_vanished_during_the_scan_does_not_hide_the_others(self):
        root, drafts = self.tree("BUG-1.md", "BUG-2.md", "BUG-3.md")
        real, gone = os.lstat, []

        def lstat(path, *args, **kw):
            if str(path).endswith(".md") and not gone:
                gone.append(Path(path))
                raise FileNotFoundError(path)  # deleted between the folder's listing and the look at it
            return real(path, *args, **kw)

        with mock.patch.object(os, "lstat", lstat):
            scrub.scrub_tree(root)
        self.assertEqual(
            sorted(f.name for f in drafts if f.read_text() == HIDDEN), sorted(f.name for f in drafts if f != gone[0])
        )

    def test_a_file_of_twenty_million_bytes_or_more_is_left_alone(self):  # the limit itself
        root, (log,) = self.tree("requests.log", text="")
        os.truncate(log, 20_000_000)
        with mock.patch.object(scrub, "scrub_file") as read:
            scrub.scrub_tree(root)
            read.assert_not_called()
            os.truncate(log, 19_999_999)
            scrub.scrub_tree(root)
        read.assert_called_once_with(log)


if __name__ == "__main__":
    unittest.main()
