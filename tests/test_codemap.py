"""The code map (qa_sdet.codemap): a mapper's code-map file becomes rows; what is dropped, parked, capped or redacted.
Hints from source, never requirements."""

import json
import unittest

from support import FIX, CodeMapCase, core, git, git_repo

from qa_sdet import codemap, ingest, memory


class Apply(CodeMapCase):
    """A mapper's code-map file becomes rows; what is dropped, parked, capped or redacted."""

    def test_ingest_rules_scoping_and_redaction(self):
        self.scan(FIX / "frontend", FIX / "backend")
        fe, be = str((FIX / "frontend").resolve()), str((FIX / "backend").resolve())
        self.write_map(
            "frontend",
            [
                {
                    "repo": fe,
                    "page": "/form.html",
                    "kind": "route",
                    "label": "Speed zone",
                    "value": "SpeedZoneForm",
                    "file": "src/routes.js:4",
                },
                {
                    "repo": fe,
                    "page": "/form.html",
                    "kind": "locator",
                    "label": "password",  # not a secret: a field
                    "value": "getByLabel('Password')",
                    "file": "src/components/SpeedZoneForm.jsx:8",
                },
                {
                    "repo": fe,
                    "page": "/form.html",
                    "kind": "rule",
                    "label": "speed",
                    "value": "min 0, max 130",
                    "file": "src/components/SpeedZoneForm.jsx:8",
                },
                {
                    "repo": fe,
                    "page": "/index.html",
                    "kind": "api",
                    "label": "incidents",
                    "value": "GET api/incidents.json?api_key=fixture-key-1234567890abcdef",
                    "file": "src/api/client.js:2",
                },
                {
                    "repo": fe,
                    "page": "*",
                    "kind": "locator",
                    "label": "Save",
                    "value": "getByRole('button')",
                    "file": "src/components/SpeedZoneForm.jsx:1",
                },  # page * is for backend rows: dropped, said so
                {
                    "repo": fe,
                    "page": "/form.html",
                    "kind": "bogus",
                    "label": "x",
                    "value": "y",
                    "file": "src/routes.js:1",
                },  # unknown kind: dropped (and noted in the sentinel)
            ],
        )
        self.write_map(
            "backend",
            [
                {
                    "repo": be,
                    "page": "*",
                    "kind": "api",
                    "label": "GET /api/incidents",
                    "value": "list",
                    "file": "routes/zones.js:9",
                },
                {
                    "repo": be,
                    "page": "*",
                    "kind": "rule",
                    "label": "POST /api/zones speedLimit",
                    "value": "integer, min 0, max 130",
                    "file": "routes/zones.js:6",
                },
                {
                    "repo": be,
                    "page": "*",
                    "kind": "api",
                    "label": "GET /api/admin/stats",
                    "value": "no page calls it",
                    "file": "routes/admin.js:2",
                },
            ],
        )
        self.ingest_all()
        everything = json.dumps([dict(r) for r in self.con.execute("SELECT * FROM codemap")])
        self.assertNotIn("fixture-key-1234567890abcdef", everything)
        self.assertIn("getByLabel('Password')", everything)  # field locators named 'password' survive
        self.assertNotIn("bogus", everything)
        note = (self.out / "code-map-frontend.json.ingested").read_text()
        self.assertIn("'unknown kind': 1", note)
        self.assertIn("'page * outside backend (no card shows it)': 1", note)
        self.assertEqual(codemap.find_page(self.con, "speed zone")["route"], "/form.html")  # resolves by name now
        idx = codemap.code(self.con, "/index.html")
        self.assertIn("GET /api/incidents", idx)  # backend endpoint linked through the frontend call
        self.assertNotIn("admin/stats", idx)
        self.assertIn("admin/stats", memory.search(self.con, "admin stats"))  # findable, not in cards

    def test_ingest_rejects_maps_from_another_scan(self):
        self.scan(FIX / "frontend")
        self.write_map("frontend", [], scan_id="frontend-old")
        ingest.PARKED.clear()
        ingest.ingest_pending(self.dir / "qa-out")
        self.assertTrue(any("older scan" in p for p in ingest.PARKED), ingest.PARKED)
        ingest.PARKED.clear()

    def test_map_for_the_wrong_area_is_rejected(self):
        self.scan(FIX / "frontend", FIX / "backend")
        scan = json.loads((self.out / "scan-frontend.json").read_text())
        (self.out / "code-map-frontend.json").write_text(
            json.dumps({"scan_id": scan["scan_id"], "area": "backend", "rows": []})
        )
        ingest.ingest_pending(self.dir / "qa-out")
        self.assertTrue(ingest.PARKED)

    def test_code_maps_outside_init_are_ignored(self):
        self.scan(FIX / "frontend")
        stray = self.dir / "qa-out" / "form" / "code-map-frontend.json"
        stray.parent.mkdir(parents=True)
        stray.write_text(json.dumps({"scan_id": "x", "area": "frontend", "rows": []}))
        ingest.PARKED.clear()
        ingest.ingest_pending(self.dir / "qa-out")
        self.assertEqual(ingest.PARKED, [])
        self.assertFalse((stray.parent / "code-map-frontend.json.ingested").exists())

    def test_ingest_pending_accepts_the_init_folder(self):  # qa-out/init loaded nothing, silently
        api = git_repo(self.dir / "api", {"routes/zones.js": "router.post('/api/zones', h)\n"})
        self.scan(api)
        self.write_map(
            "backend",
            [{"page": "*", "kind": "api", "label": "POST /api/zones", "value": "x", "file": "routes/zones.js:1"}],
        )
        self.assertEqual(ingest.ingest_pending(self.out), 1)

    def test_removed_routes_drop_code_only_pages(self):
        self.scan(FIX / "frontend")
        row = {"page": "/old.html", "kind": "route", "label": "Old", "value": "Old", "file": "src/routes.js:1"}
        self.write_map("frontend", [row])
        self.ingest_all()
        self.assertIsNotNone(codemap.find_page(self.con, "/old.html"))
        self.scan(FIX / "frontend", full=True, stamp="s2")  # the route is gone from the code
        self.write_map("frontend", [])
        self.ingest_all()
        self.assertIsNone(codemap.find_page(self.con, "/old.html"))

    def test_a_route_moved_in_the_router_drops_its_old_page(self):
        ui = git_repo(
            self.dir / "ui",
            {
                "src/routes.js": "export const routes = [{ path: '/old' }, { path: '/home' }]",
                "src/Old.jsx": "<b data-testid='o'/>",
            },
        )
        clone = git_repo(self.dir / "clone", {"src/X.jsx": "<b/>"})
        self.scan(clone)  # a wrong folder: its map never lands
        self.scan(ui, stamp="s1")
        self.write_map(
            "frontend",
            [
                {"page": "/old", "kind": "route", "label": "Old", "value": "O", "file": "src/routes.js:1"},
                {"page": "/home", "kind": "route", "label": "Home", "value": "H", "file": "src/routes.js:1"},
                {"page": "/old", "kind": "locator", "label": "O", "value": "#o", "file": "src/Old.jsx:1"},
            ],
        )
        self.ingest_all()
        (ui / "src/routes.js").write_text("export const routes = [{ path: '/home' }, { path: '/new' }]")
        git(ui, "commit", "-qam", "move")
        self.scan(ui, stamp="s2")
        self.write_map(
            "frontend",
            [
                {"page": "/home", "kind": "route", "label": "Home", "value": "H", "file": "src/routes.js:1"},
                {"page": "/new", "kind": "route", "label": "New", "value": "N", "file": "src/routes.js:1"},
                {"page": "/new", "kind": "locator", "label": "O", "value": "#o", "file": "src/Old.jsx:1"},
            ],
        )
        self.ingest_all()
        self.assertIsNone(codemap.find_page(self.con, "/old"))
        self.assertIn(str(clone), codemap.code(self.con, "--repos"))  # listed, so the skill can offer to forget it

    def test_code_page_is_not_a_new_build_for_runs(self):
        """A page the code map created has no build: the first real run must not treat that as a build change."""
        self.scan(FIX / "frontend")
        self.write_map(
            "frontend",
            [{"page": "/form.html", "kind": "route", "label": "Speed zone", "value": "F", "file": "src/routes.js:4"}],
        )
        self.ingest_all()
        ingest.ingest(
            self.con,
            {
                "run_id": "p1",
                "build": "",
                "pages": [  # a plan: build unknown, ticket fact
                    {"route": "/form.html", "facts": [{"kind": "rule", "text": "speed max 120", "source": "PROJ-1"}]}
                ],
            },
        )
        self.con.execute("UPDATE pages SET build=NULL WHERE route='/form.html'")  # as if the map came last
        self.con.commit()
        ingest.ingest(self.con, {"run_id": "r1", "build": "1.0", "pages": [{"route": "/form.html"}]})
        self.assertEqual(self.con.execute("SELECT stale FROM facts").fetchone()[0], 0)

    def test_backend_rows_are_not_capped(self):
        repo = git_repo(self.dir / "api", {"routes/zones.js": "router.get('/api/zones', h)\n"})
        self.scan(repo)
        rows = [
            {"page": "*", "kind": "api", "label": f"GET /api/r{i}", "value": "x", "file": "routes/zones.js:1"}
            for i in range(240)
        ]
        self.write_map("backend", rows)
        self.ingest_all()
        self.assertEqual(self.con.execute("SELECT count(*) FROM codemap WHERE page='*'").fetchone()[0], 240)

    def test_rows_from_two_files_and_areas_coexist(self):
        repo = git_repo(
            self.dir / "ui",
            {
                "src/Form.jsx": "<input data-testid='a'/>",
                "src/Other.jsx": "<b/>",
                "e2e/form.spec.ts": "import { test } from '@playwright/test'; page.getByTestId('a')",
            },
        )
        self.scan(repo)
        same = {"page": "/form", "kind": "locator", "label": "Speed", "value": "getByTestId('a')"}
        self.write_map("frontend", [{**same, "file": "src/Form.jsx:1"}, {**same, "file": "src/Other.jsx:1"}])
        self.write_map("tests", [{**same, "file": "e2e/form.spec.ts:1"}])
        self.ingest_all()
        self.assertEqual(self.con.execute("SELECT count(*) FROM codemap WHERE label='Speed'").fetchone()[0], 3)

    def test_paths_are_normalised_and_wrong_paths_park_the_file(self):
        repo = git_repo(self.dir / "ui", {"src/Form.jsx": "<input data-testid='a'/>"})
        other = git_repo(self.dir / "api", {"r.js": "router.get('/api/x', h)"})
        self.scan(repo, other)
        rows = [
            {"page": "/form", "kind": "locator", "label": "A", "value": "#a", "file": "./src/Form.jsx:1"},
            {"page": "/form", "kind": "locator", "label": "B", "value": "#b", "file": f"{repo}/src/Form.jsx:2"},
        ]
        self.write_map("frontend", rows)  # no repo: the only repo with frontend files
        self.ingest_all()
        self.assertEqual(self.con.execute("SELECT count(*) FROM codemap").fetchone()[0], 2)
        self.scan(repo, other, full=True, stamp="s2")
        self.write_map(
            "frontend",
            [
                {
                    "repo": "/nowhere",
                    "page": "/form",
                    "kind": "locator",
                    "label": "A",
                    "value": "#a",
                    "file": "src/Form.jsx:1",
                }
            ],
        )
        ingest.PARKED.clear()
        ingest.ingest_pending(self.dir / "qa-out")
        self.assertTrue(any("isn't in the scan file" in p for p in ingest.PARKED), ingest.PARKED)
        ingest.PARKED.clear()
        self.assertEqual(self.con.execute("SELECT count(*) FROM codemap").fetchone()[0], 2)  # nothing lost

    def test_any_misplaced_row_parks_the_map(self):
        repo = git_repo(self.dir / "ui", {"src/A.jsx": "<b data-testid='a'/>"})
        self.scan(repo)
        rows = [
            {"page": "/a", "kind": "locator", "label": f"L{i}", "value": f"#l{i}", "file": "src/A.jsx:1"}
            for i in range(9)
        ]
        self.write_map(
            "frontend",
            rows + [{"page": "/a", "kind": "locator", "label": "X", "value": "#x", "file": "src/Other.jsx:1"}],
        )
        ingest.ingest_pending(self.dir / "qa-out")
        self.assertTrue(ingest.PARKED)
        self.assertEqual(self.con.execute("SELECT count(*) FROM codemap").fetchone()[0], 0)
        self.assertEqual(codemap.file_hashes(self.con, "frontend", str(repo)), {})  # not marked read

    def test_cap_evictions_dont_cause_endless_rereads(self):
        """Rows past a page's cap are dropped (documented), and their files are not re-read on every run."""
        repo = git_repo(self.dir / "ui", {f"src/C{i}.jsx": f"<b data-testid='c{i}'/>" for i in range(3)})
        self.scan(repo)
        rows = [
            {"page": "/big", "kind": "locator", "label": f"{i}-{k}", "value": f"#x{i}{k}", "file": f"src/C{i}.jsx:1"}
            for i in range(3)
            for k in range(90)
        ]
        self.write_map("frontend", rows)
        self.ingest_all()
        self.assertEqual(self.con.execute("SELECT count(*) FROM codemap WHERE page='/big'").fetchone()[0], 200)
        self.assertEqual(self.scan(repo, stamp="s2")["areas"]["frontend"]["files"], 0)

    def test_cap_never_evicts_route_rows(self):
        repo = git_repo(
            self.dir / "ui",
            {"src/routes.js": "export const routes = [{ path: '/big' }]", "src/Big.jsx": "<b data-testid='x'/>"},
        )
        self.scan(repo)
        self.write_map(
            "frontend", [{"page": "/big", "kind": "route", "label": "Big", "value": "B", "file": "src/routes.js:1"}]
        )
        self.ingest_all()
        self.scan(repo, full=True, stamp="s2")
        rows = [{"page": "/big", "kind": "route", "label": "Big", "value": "B", "file": "src/routes.js:1"}]
        rows += [
            {"page": "/big", "kind": "locator", "label": f"L{i}", "value": f"#l{i}", "file": "src/Big.jsx:1"}
            for i in range(250)
        ]
        self.write_map("frontend", rows)
        self.ingest_all()
        self.assertEqual(
            self.con.execute("SELECT count(*) FROM codemap WHERE page='/big' AND kind='route'").fetchone()[0], 1
        )

    def test_route_rows_only_come_from_the_frontend(self):
        ui = git_repo(self.dir / "ui", {"src/routes.js": "export const routes = [{ path: '/zones' }]"})
        api = git_repo(self.dir / "api", {"r.js": "router.get('/api/zones', h)"})
        self.scan(ui, api)
        self.write_map(
            "frontend",
            [
                {
                    "repo": str(ui),
                    "page": "/zones",
                    "kind": "route",
                    "label": "Z",
                    "value": "Z",
                    "file": "src/routes.js:1",
                }
            ],
        )
        self.write_map(
            "backend",
            [
                {
                    "repo": str(api),
                    "page": "*",
                    "kind": "route",
                    "label": "GET /api/zones",
                    "value": "x",
                    "file": "r.js:1",
                }
            ],
        )
        self.ingest_all()
        self.assertIn("route outside frontend", (self.out / "code-map-backend.json.ingested").read_text())
        self.assertNotIn("*", codemap.code(self.con).split("\n", 1)[1].split("\nTOTAL:")[0])
        self.assertEqual(self.scan(ui, api, stamp="s2")["total_files"], 0)  # no endless full re-maps

    def test_rules_about_passwords_and_tokens_keep_their_values(self):
        repo = git_repo(self.dir / "ui", {"src/Login.jsx": "export const L = () => <i/>;\n"})
        self.scan(repo)
        rule = lambda label, value: {
            "page": "/login",
            "kind": "rule",
            "label": label,
            "value": value,
            "file": "src/Login.jsx:1",
        }
        rules = [
            ("password", "string, min 8, max 64, required"),
            ("new password", "minLength=8"),
            ("password", "z.string().min(8).max(64)"),
            ("token", "StringLength(32)"),
            ("token", "Validators.required"),
            ("password", "passwordStrengthValidator"),
            ("token", "PasswordPolicy.STRONG"),
            ("password", "string|min:8|max:64"),
            ("otp token", "digits:6"),
            ("reset token", "uuid|size:36"),
            ("access token", "JWT,HS256,exp:15m"),
            ("session_id", "regex:/^[a-f0-9]{32}$/"),
            ("password", "confirmed"),
        ]
        self.write_map(
            "frontend",
            [
                {"page": "/login", "kind": "route", "label": "Login", "value": "L", "file": "src/Login.jsx:1"},
                {
                    "page": "/login",
                    "kind": "api",
                    "label": "reset password",
                    "value": "POST /api/auth/reset-password",
                    "file": "src/Login.jsx:1",
                },
                rule("jwt.secret", "abcDEF123456ghiJKL7890"),
                rule("Mapbox API key", "9f8a7b6c5d4e3f2a1b0c9f8a7b6c5d4e"),
                *[rule(k, v) for k, v in rules],
            ],
        )
        self.ingest_all()
        out = codemap.code(self.con, "/login")
        for _, value in rules:  # every rule kept, a repeated `password` label included
            self.assertIn(value, out)
        self.assertIn("POST /api/auth/reset-password", out)
        self.assertIn("reset-password", memory.search(self.con, "/api/auth/reset-password"))
        for secret in ("abcDEF123456", "9f8a7b6c"):  # a bare secret under a secret-named label is removed
            self.assertNotIn(secret, out)

    def test_secret_config_rows_are_redacted_by_label(self):
        repo = git_repo(self.dir / "api", {"r.js": "router.get('/x', h)"})
        self.scan(repo)
        self.write_map(
            "backend",
            [
                {"page": "*", "kind": "rule", "label": "jwt.secret", "value": "s3cr3t-Value-123456", "file": "r.js:1"},
                {
                    "page": "*",
                    "kind": "rule",
                    "label": "POST /login password",
                    "value": "min 8 chars",
                    "file": "r.js:1",
                },
            ],
        )
        self.ingest_all()
        everything = memory.search(self.con, "secret") + memory.search(self.con, "password")
        self.assertNotIn("s3cr3t-Value-123456", everything)
        self.assertIn("min 8 chars", everything)

    def test_long_file_field_is_fast(self):
        import time as _t

        t0 = _t.time()
        codemap._row_src("/r", "a" * 200000 + ":" + "1:" * 50000)
        self.assertLess(_t.time() - t0, 1)

    def test_bookkeeping_keys_of_the_old_orphan_tracking_are_deleted_once(self):
        keys = (
            "codemap_routes_at_scan",
            "codemap_lost",
            "codemap_scan_repos",
            "codemap_reread:frontend:/r",
            "codemap_pending:tests:/r",
        )
        keep = ("codemap_hashes:frontend:/r", "codemap_commit:frontend:/r", "build")
        with self.con:
            for k in keys + keep:
                core.set_meta(self.con, k, "x")
        self.con.close()
        self.con = core.connect()
        self.assertEqual(
            sorted(r[0] for r in self.con.execute("SELECT key FROM meta")), sorted(keep + ("fts",))
        )  # + core's own


if __name__ == "__main__":
    unittest.main()
