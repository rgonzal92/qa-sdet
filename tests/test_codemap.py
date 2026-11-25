"""Code map (qa_sdet.codemap): applying a mapper's rows, the card block, `qa-mem code`, forget and the app shell's menu.
Hints from source, never requirements."""
import json, os, shutil, unittest

from support import CodeMapCase, FIX, git, git_repo, core, memory
from qa_sdet import codemap


class Apply(CodeMapCase):
    """A mapper's code-map file becomes rows; what is dropped, parked, capped or redacted."""

    def test_ingest_rules_scoping_and_redaction(self):
        self.scan(FIX / "frontend", FIX / "backend")
        fe, be = str((FIX / "frontend").resolve()), str((FIX / "backend").resolve())
        self.write_map("frontend", [
            {"repo": fe, "page": "/form.html", "kind": "route", "label": "Speed zone", "value": "SpeedZoneForm",
             "file": "src/routes.js:4"},
            {"repo": fe, "page": "/form.html", "kind": "locator", "label": "password",  # not a secret: a field
             "value": "getByLabel('Password')", "file": "src/components/SpeedZoneForm.jsx:8"},
            {"repo": fe, "page": "/form.html", "kind": "rule", "label": "speed", "value": "min 0, max 130",
             "file": "src/components/SpeedZoneForm.jsx:8"},
            {"repo": fe, "page": "/index.html", "kind": "api", "label": "incidents",
             "value": "GET api/incidents.json?api_key=fixture-key-1234567890abcdef", "file": "src/api/client.js:2"},
            {"repo": fe, "page": "*", "kind": "locator", "label": "Save", "value": "getByRole('button')",
             "file": "src/components/SpeedZoneForm.jsx:1"},  # page * is for backend rows: dropped, said so
            {"repo": fe, "page": "/form.html", "kind": "bogus", "label": "x", "value": "y",
             "file": "src/routes.js:1"},  # unknown kind: dropped (and noted in the sentinel)
        ])
        self.write_map("backend", [
            {"repo": be, "page": "*", "kind": "api", "label": "GET /api/incidents", "value": "list",
             "file": "routes/zones.js:9"},
            {"repo": be, "page": "*", "kind": "rule", "label": "POST /api/zones speedLimit",
             "value": "integer, min 0, max 130", "file": "routes/zones.js:6"},
            {"repo": be, "page": "*", "kind": "api", "label": "GET /api/admin/stats", "value": "no page calls it",
             "file": "routes/admin.js:2"},
        ])
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
        memory.PARKED.clear()
        memory.ingest_pending(self.dir / "qa-out")
        self.assertTrue(any("older scan" in p for p in memory.PARKED), memory.PARKED)
        memory.PARKED.clear()

    def test_map_for_the_wrong_area_is_rejected(self):
        self.scan(FIX / "frontend", FIX / "backend")
        scan = json.loads((self.out / "scan-frontend.json").read_text())
        (self.out / "code-map-frontend.json").write_text(json.dumps({"scan_id": scan["scan_id"], "area": "backend", "rows": []}))
        memory.ingest_pending(self.dir / "qa-out")
        self.assertTrue(memory.PARKED)

    def test_code_maps_outside_init_are_ignored(self):
        self.scan(FIX / "frontend")
        stray = self.dir / "qa-out" / "form" / "code-map-frontend.json"
        stray.parent.mkdir(parents=True)
        stray.write_text(json.dumps({"scan_id": "x", "area": "frontend", "rows": []}))
        memory.PARKED.clear()
        memory.ingest_pending(self.dir / "qa-out")
        self.assertEqual(memory.PARKED, [])
        self.assertFalse((stray.parent / "code-map-frontend.json.ingested").exists())

    def test_ingest_pending_accepts_the_init_folder(self):  # qa-out/init loaded nothing, silently
        api = git_repo(self.dir / "api", {"routes/zones.js": "router.post('/api/zones', h)\n"})
        self.scan(api)
        self.write_map("backend", [{"page": "*", "kind": "api", "label": "POST /api/zones", "value": "x",
                                    "file": "routes/zones.js:1"}])
        self.assertEqual(memory.ingest_pending(self.out), 1)

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
        ui = git_repo(self.dir / "ui", {"src/routes.js": "export const routes = [{ path: '/old' }, { path: '/home' }]",
                                        "src/Old.jsx": "<b data-testid='o'/>"})
        clone = git_repo(self.dir / "clone", {"src/X.jsx": "<b/>"})
        self.scan(clone)  # a wrong folder: its map never lands
        self.scan(ui, stamp="s1")
        self.write_map("frontend", [
            {"page": "/old", "kind": "route", "label": "Old", "value": "O", "file": "src/routes.js:1"},
            {"page": "/home", "kind": "route", "label": "Home", "value": "H", "file": "src/routes.js:1"},
            {"page": "/old", "kind": "locator", "label": "O", "value": "#o", "file": "src/Old.jsx:1"}])
        self.ingest_all()
        (ui / "src/routes.js").write_text("export const routes = [{ path: '/home' }, { path: '/new' }]")
        git(ui, "commit", "-qam", "move")
        self.scan(ui, stamp="s2")
        self.write_map("frontend", [
            {"page": "/home", "kind": "route", "label": "Home", "value": "H", "file": "src/routes.js:1"},
            {"page": "/new", "kind": "route", "label": "New", "value": "N", "file": "src/routes.js:1"},
            {"page": "/new", "kind": "locator", "label": "O", "value": "#o", "file": "src/Old.jsx:1"}])
        self.ingest_all()
        self.assertIsNone(codemap.find_page(self.con, "/old"))
        self.assertIn(str(clone), codemap.code(self.con, "--repos"))  # listed, so the skill can offer to forget it

    def test_code_page_is_not_a_new_build_for_runs(self):
        """A page the code map created has no build: the first real run must not treat that as a build change."""
        self.scan(FIX / "frontend")
        self.write_map("frontend", [{"page": "/form.html", "kind": "route", "label": "Speed zone", "value": "F",
                                     "file": "src/routes.js:4"}])
        self.ingest_all()
        memory.ingest(self.con, {"run_id": "p1", "build": "", "pages": [  # a plan: build unknown, ticket fact
            {"route": "/form.html", "facts": [{"kind": "rule", "text": "speed max 120", "source": "PROJ-1"}]}]})
        self.con.execute("UPDATE pages SET build=NULL WHERE route='/form.html'")  # as if the map came last
        self.con.commit()
        memory.ingest(self.con, {"run_id": "r1", "build": "1.0", "pages": [{"route": "/form.html"}]})
        self.assertEqual(self.con.execute("SELECT stale FROM facts").fetchone()[0], 0)

    def test_backend_rows_are_not_capped(self):
        repo = git_repo(self.dir / "api", {"routes/zones.js": "router.get('/api/zones', h)\n"})
        self.scan(repo)
        rows = [{"page": "*", "kind": "api", "label": f"GET /api/r{i}", "value": "x", "file": "routes/zones.js:1"}
                for i in range(240)]
        self.write_map("backend", rows)
        self.ingest_all()
        self.assertEqual(self.con.execute("SELECT count(*) FROM codemap WHERE page='*'").fetchone()[0], 240)

    def test_rows_from_two_files_and_areas_coexist(self):
        repo = git_repo(self.dir / "ui", {"src/Form.jsx": "<input data-testid='a'/>", "src/Other.jsx": "<b/>",
                                          "e2e/form.spec.ts": "import { test } from '@playwright/test'; page.getByTestId('a')"})
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
        rows = [{"page": "/form", "kind": "locator", "label": "A", "value": "#a", "file": "./src/Form.jsx:1"},
                {"page": "/form", "kind": "locator", "label": "B", "value": "#b", "file": f"{repo}/src/Form.jsx:2"}]
        self.write_map("frontend", rows)  # no repo: the only repo with frontend files
        self.ingest_all()
        self.assertEqual(self.con.execute("SELECT count(*) FROM codemap").fetchone()[0], 2)
        commit = self.con.execute("SELECT value FROM meta WHERE key LIKE 'codemap_commit:frontend:%'").fetchone()
        self.scan(repo, other, full=True, stamp="s2")
        self.write_map("frontend", [{"repo": "/nowhere", "page": "/form", "kind": "locator", "label": "A", "value": "#a",
                                     "file": "src/Form.jsx:1"}])
        memory.PARKED.clear()
        memory.ingest_pending(self.dir / "qa-out")
        self.assertTrue(any("isn't in the scan file" in p for p in memory.PARKED), memory.PARKED)
        memory.PARKED.clear()
        self.assertEqual(self.con.execute("SELECT count(*) FROM codemap").fetchone()[0], 2)  # nothing lost

    def test_any_misplaced_row_parks_the_map(self):
        repo = git_repo(self.dir / "ui", {"src/A.jsx": "<b data-testid='a'/>"})
        self.scan(repo)
        rows = [{"page": "/a", "kind": "locator", "label": f"L{i}", "value": f"#l{i}", "file": "src/A.jsx:1"} for i in range(9)]
        self.write_map("frontend", rows + [{"page": "/a", "kind": "locator", "label": "X", "value": "#x", "file": "src/Other.jsx:1"}])
        memory.ingest_pending(self.dir / "qa-out")
        self.assertTrue(memory.PARKED)
        self.assertEqual(self.con.execute("SELECT count(*) FROM codemap").fetchone()[0], 0)
        self.assertEqual(codemap._hashes(self.con, "frontend", str(repo)), {})  # not marked read

    def test_cap_evictions_dont_cause_endless_rereads(self):
        """Rows past a page's cap are dropped (documented), and their files are not re-read on every run."""
        repo = git_repo(self.dir / "ui", {f"src/C{i}.jsx": f"<b data-testid='c{i}'/>" for i in range(3)})
        self.scan(repo)
        rows = [{"page": "/big", "kind": "locator", "label": f"{i}-{k}", "value": f"#x{i}{k}", "file": f"src/C{i}.jsx:1"}
                for i in range(3) for k in range(90)]
        self.write_map("frontend", rows)
        self.ingest_all()
        self.assertEqual(self.con.execute("SELECT count(*) FROM codemap WHERE page='/big'").fetchone()[0], 200)
        self.assertEqual(self.scan(repo, stamp="s2")["areas"]["frontend"]["files"], 0)

    def test_cap_never_evicts_route_rows(self):
        repo = git_repo(self.dir / "ui", {"src/routes.js": "export const routes = [{ path: '/big' }]",
                                          "src/Big.jsx": "<b data-testid='x'/>"})
        self.scan(repo)
        self.write_map("frontend", [{"page": "/big", "kind": "route", "label": "Big", "value": "B", "file": "src/routes.js:1"}])
        self.ingest_all()
        self.scan(repo, full=True, stamp="s2")
        rows = [{"page": "/big", "kind": "route", "label": "Big", "value": "B", "file": "src/routes.js:1"}]
        rows += [{"page": "/big", "kind": "locator", "label": f"L{i}", "value": f"#l{i}", "file": "src/Big.jsx:1"} for i in range(250)]
        self.write_map("frontend", rows)
        self.ingest_all()
        self.assertEqual(self.con.execute("SELECT count(*) FROM codemap WHERE page='/big' AND kind='route'").fetchone()[0], 1)

    def test_route_rows_only_come_from_the_frontend(self):
        ui = git_repo(self.dir / "ui", {"src/routes.js": "export const routes = [{ path: '/zones' }]"})
        api = git_repo(self.dir / "api", {"r.js": "router.get('/api/zones', h)"})
        self.scan(ui, api)
        self.write_map("frontend", [{"repo": str(ui), "page": "/zones", "kind": "route", "label": "Z", "value": "Z",
                                     "file": "src/routes.js:1"}])
        self.write_map("backend", [{"repo": str(api), "page": "*", "kind": "route", "label": "GET /api/zones", "value": "x",
                                    "file": "r.js:1"}])
        self.ingest_all()
        self.assertIn("route outside frontend", (self.out / "code-map-backend.json.ingested").read_text())
        self.assertNotIn("*", codemap.code(self.con).split("\n", 1)[1].split("\nTOTAL:")[0])
        self.assertEqual(self.scan(ui, api, stamp="s2")["total_files"], 0)  # no endless full re-maps

    def test_rules_about_passwords_and_tokens_keep_their_values(self):
        repo = git_repo(self.dir / "ui", {"src/Login.jsx": "export const L = () => <i/>;\n"})
        self.scan(repo)
        rule = lambda label, value: {"page": "/login", "kind": "rule", "label": label, "value": value, "file": "src/Login.jsx:1"}
        rules = [("password", "string, min 8, max 64, required"), ("new password", "minLength=8"),
                 ("password", "z.string().min(8).max(64)"), ("token", "StringLength(32)"), ("token", "Validators.required"),
                 ("password", "passwordStrengthValidator"), ("token", "PasswordPolicy.STRONG"),
                 ("password", "string|min:8|max:64"), ("otp token", "digits:6"), ("reset token", "uuid|size:36"),
                 ("access token", "JWT,HS256,exp:15m"), ("session_id", "regex:/^[a-f0-9]{32}$/"), ("password", "confirmed")]
        self.write_map("frontend", [
            {"page": "/login", "kind": "route", "label": "Login", "value": "L", "file": "src/Login.jsx:1"},
            {"page": "/login", "kind": "api", "label": "reset password", "value": "POST /api/auth/reset-password",
             "file": "src/Login.jsx:1"},
            rule("jwt.secret", "abcDEF123456ghiJKL7890"), rule("Mapbox API key", "9f8a7b6c5d4e3f2a1b0c9f8a7b6c5d4e"),
            *[rule(k, v) for k, v in rules]])
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
        self.write_map("backend", [
            {"page": "*", "kind": "rule", "label": "jwt.secret", "value": "s3cr3t-Value-123456", "file": "r.js:1"},
            {"page": "*", "kind": "rule", "label": "POST /login password", "value": "min 8 chars", "file": "r.js:1"}])
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
        keys = ("codemap_routes_at_scan", "codemap_lost", "codemap_scan_repos", "codemap_reread:frontend:/r",
                "codemap_pending:tests:/r")
        keep = ("codemap_hashes:frontend:/r", "codemap_commit:frontend:/r", "build")
        with self.con:
            for k in keys + keep:
                core.set_meta(self.con, k, "x")
        self.con.close()
        self.con = core.connect()
        self.assertEqual(sorted(r[0] for r in self.con.execute("SELECT key FROM meta")), sorted(keep))


class Card(CodeMapCase):
    """The FROM CODE block of a page's card, and `qa-mem search`."""

    def test_card_block_is_compact_and_separate(self):
        self.scan(FIX / "frontend")
        rows = [{"page": "/form.html", "kind": "route", "label": "Speed zone", "value": "F", "file": "src/routes.js:4"}]
        rows += [{"page": "/form.html", "kind": "locator", "label": f"field {i:02}", "value": f"#f{i}",
                  "file": "src/components/SpeedZoneForm.jsx:8"} for i in range(17)]
        rows += [{"page": "/form.html", "kind": "rule", "label": "speed", "value": "min 0, max 130",
                  "file": "src/components/SpeedZoneForm.jsx:8"}]
        self.write_map("frontend", rows)
        self.ingest_all()
        c = memory.card(self.con, "speed zone")
        self.assertIn("NOTE: known from source code only", c)
        self.assertNotIn("re-verify", c)
        self.assertIn("FROM CODE (hints from source, not requirements; unverified;", c)
        self.assertIn("speed: min 0, max 130", c)
        self.assertIn("+2 more", c)  # 17 locators, 15 shown
        self.assertNotIn("(from ", c.split("FROM CODE")[0])  # never shown as a stated requirement
        memory.ingest(self.con, {"run_id": "r1", "build": "1.0", "pages": [
            {"route": "/form.html", "locators": [{"name": "field 00", "locator": "#f0"}]}]})
        c = memory.card(self.con, "speed zone")
        self.assertIn("LOCATORS (fresh): field 00=#f0", c)
        self.assertNotIn("field 00=#f0;", c.split("FROM CODE")[1])  # confirmed: not repeated as a hint
        self.assertIn("+1 more", c)

    def test_card_says_when_the_map_is_behind(self):
        repo = self.dir / "ui"
        shutil.copytree(FIX / "frontend" / "src", repo / "src")
        git(repo, "init", "-q")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "one")
        self.scan(repo)
        self.write_map("frontend", [{"page": "/form.html", "kind": "route", "label": "Speed zone", "value": "F",
                                     "file": "src/routes.js:4"},
                                    {"page": "/form.html", "kind": "rule", "label": "speed", "value": "max 130",
                                     "file": "src/components/SpeedZoneForm.jsx:8"}])
        self.ingest_all()
        for i in range(2):
            (repo / f"n{i}.txt").write_text(str(i))
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", f"c{i}")
        self.assertIn("2 commits behind", memory.card(self.con, "/form.html"))

    def test_behind_note_covers_the_backend_rows_on_the_card(self):
        """the note named only the page's own repo, so a backend 3 commits on looked current beside its rules."""
        ui = git_repo(self.dir / "ui", {"src/routes.js": "const r = [{ path: '/zones', element: <Z/> }]\n"})
        api = git_repo(self.dir / "api", {"routes/zones.js": "router.post('/api/zones', h)\n"})
        self.scan(ui, api)
        self.write_map("frontend", [{"page": "/zones", "kind": "route", "label": "Zones", "value": "Z", "file": "src/routes.js:1"},
                                    {"page": "/zones", "kind": "api", "label": "save", "value": "POST /api/zones",
                                     "file": "src/routes.js:1"}])
        self.write_map("backend", [{"page": "*", "kind": "rule", "label": "POST /api/zones speed", "value": "max 130",
                                    "file": "routes/zones.js:1"}])
        self.ingest_all()
        for i in range(3):
            (api / "routes/zones.js").write_text(f"// {i}\n")
            git(api, "commit", "-qam", f"c{i}")
        (ui / "src/routes.js").write_text("// x\n")
        git(ui, "commit", "-qam", "u")
        c = memory.card(self.con, "/zones")
        self.assertIn("POST /api/zones speed: max 130", c)
        self.assertRegex(c, r"api [0-9a-f]{7}, 3 commits behind; ui [0-9a-f]{7}, 1 commits behind")
        self.assertIn("api ", codemap.code(self.con, "/zones").splitlines()[0])

    def test_behind_note_clears_and_ignores_other_folders(self):
        mono = self.dir / "mono"
        git_repo(mono, {"web/src/routes.js": "export const routes = [{ path: '/z' }]", "api/r.js": "x"})
        web = mono / "web"
        self.scan(web)
        self.write_map("frontend", [{"page": "/z", "kind": "route", "label": "Z", "value": "Z", "file": "src/routes.js:1"},
                                    {"page": "/z", "kind": "rule", "label": "z", "value": "max 3", "file": "src/routes.js:1"}])
        self.ingest_all()
        (mono / "api/r.js").write_text("y")  # a commit outside the mapped folder
        git(mono, "commit", "-qam", "api only")
        self.assertNotIn("behind", memory.card(self.con, "/z"))
        (web / "README.md").write_text("docs")  # inside the folder, but no mapped file changes
        git(mono, "add", "-A")
        git(mono, "commit", "-qm", "web docs")
        self.assertIn("1 commits behind", memory.card(self.con, "/z"))
        s = self.scan(web, stamp="s2")  # nothing to map: the scan settles it
        self.assertFalse(s["areas"]["frontend"]["needs_mapper"])
        self.assertNotIn("behind", memory.card(self.con, "/z"))

    def test_empty_map_tips(self):
        self.assertIn("tip: /qa-sdet:qa-init", memory.card(self.con, "closures"))
        self.assertIn("no code map yet", codemap.code(self.con))
        self.scan(FIX / "frontend")
        self.write_map("frontend", [{"page": "/form.html", "kind": "route", "label": "Speed zone", "value": "F",
                                     "file": "src/routes.js:4"}])
        self.ingest_all()
        self.assertNotIn("tip:", memory.card(self.con, "closures"))
        self.assertIn("/form.html  Speed zone", codemap.code(self.con))

    def test_route_params_meet_run_pages(self):
        repo = git_repo(self.dir / "ui", {"src/Detail.jsx": "<h1 data-testid='title'/>"})
        self.scan(repo)
        self.write_map("frontend", [{"page": "/incidents/:incidentId", "kind": "locator", "label": "Title",
                                     "value": "getByTestId('title')", "file": "src/Detail.jsx:1"}])
        self.ingest_all()
        memory.ingest(self.con, {"run_id": "r1", "build": "1.0", "pages": [{"route": "/incidents/4521"}]})
        c = memory.card(self.con, "/incidents/4521")
        self.assertIn("Title=getByTestId('title')", c)  # one page: /incidents/:id
        for r in ("/zones/{zoneId}", "/zones/[id]", "/zones/<int:pk>", "/zones/42"):
            self.assertEqual(core.norm_route(r), "/zones/:id", r)

    def test_page_known_only_from_locators_has_a_card(self):
        repo = git_repo(self.dir / "e2e", {"pages/Zones.ts": "import { test } from '@playwright/test'; page.getByRole('row')"})
        self.scan(repo)
        self.write_map("tests", [{"page": "/zones", "kind": "locator", "label": "Rows", "value": "getByRole('row')",
                                  "file": "pages/Zones.ts:1"}])
        self.ingest_all()
        self.assertIn("Rows=getByRole('row')", memory.card(self.con, "/zones"))

    def test_crawl_names_dont_break_lookup_by_code_name(self):
        repo = git_repo(self.dir / "ui", {"src/routes.js": "export const routes = [{ path: '/zones' }]"})
        self.scan(repo)
        self.write_map("frontend", [{"page": "/zones", "kind": "route", "label": "Speed zones", "value": "Z",
                                     "file": "src/routes.js:1"}])
        self.ingest_all()
        self.con.execute("UPDATE pages SET name='Traffic QA' WHERE route='/zones'")  # a crawl wrote document.title
        self.con.commit()
        self.assertEqual(codemap.find_page(self.con, "speed zones")["route"], "/zones")

    def test_search_lists_page_rows_before_backend_rows(self):
        repo = git_repo(self.dir / "ui", {"src/Z.jsx": "<input name='speed'/>"})
        api = git_repo(self.dir / "api", {"r.js": "router.get('/x', h)"})
        self.scan(repo, api)
        self.write_map("backend", [{"repo": str(api), "page": "*", "kind": "rule", "label": f"POST /r{i} speed",
                                    "value": "max 130", "file": "r.js:1"} for i in range(12)])
        self.write_map("frontend", [{"repo": str(repo), "page": "/zones", "kind": "rule", "label": "speed",
                                     "value": "max 130", "file": "src/Z.jsx:1"}])
        self.ingest_all()
        self.assertTrue(memory.search(self.con, "speed").splitlines()[0].startswith("/zones [code:rule]"))

    def test_a_visit_with_an_unreadable_build_counts_as_a_visit(self):
        repo = git_repo(self.dir / "ui", {"src/routes.js": "export const routes = [{ path: '/map' }]"})
        self.scan(repo)
        self.write_map("frontend", [{"page": "/map", "kind": "route", "label": "Map", "value": "M", "file": "src/routes.js:1"}])
        self.ingest_all()
        memory.ingest(self.con, {"run_id": "r1", "build": "", "pages": [{"route": "/map"}]})
        self.assertNotIn("no run has visited", memory.card(self.con, "/map"))


class Code(CodeMapCase):
    """`qa-mem code`, `--repos` and `codemap-forget`; linking a page's calls to backend rows."""

    def test_code_lists_totals_even_with_only_a_backend(self):
        """with only backend rows, `code` printed a bare header; qa-init's reply takes its counts from it."""
        api = git_repo(self.dir / "api", {"routes/zones.js": "router.post('/api/zones', h)\n"})
        self.scan(api)
        self.write_map("backend", [{"page": "*", "kind": "api", "label": "POST /api/zones", "value": "x",
                                    "file": "routes/zones.js:1"}])
        self.ingest_all()
        out = codemap.code(self.con)
        self.assertIn("(none: no frontend routes mapped yet)", out)
        self.assertIn("TOTAL: 0 routes, 0 locators, 0 rules, 1 endpoints (1 backend", out)

    def test_repos_listing_and_forget(self):
        gone = str(self.dir / "old-clone")
        self.scan(FIX / "frontend")
        self.write_map("frontend", [{"page": "/form.html", "kind": "route", "label": "Speed zone", "value": "F",
                                     "file": "src/routes.js:4"}])
        self.ingest_all()
        self.con.execute("INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) "
                         "VALUES('/old.html','route','Old','Old','a.js:1',?,'frontend','t','a.js')", (gone,))
        self.con.execute("INSERT INTO pages(route, name) VALUES('/old.html', 'Old')")
        self.con.commit()
        listing = codemap.code(self.con, "--repos")
        self.assertIn(str((FIX / "frontend").resolve()) + "  1 rows", listing)
        self.assertIn(gone + "  1 rows  (folder no longer exists", listing)
        self.assertEqual(codemap.codemap_forget(self.con, gone), 1)
        self.assertIsNone(codemap.find_page(self.con, "/old.html"))  # its code-only page went with it
        self.assertIsNotNone(codemap.find_page(self.con, "/form.html"))

    def test_forget_a_moved_folder_as_typed_and_say_when_unknown(self):
        """a folder that is gone was matched only byte-for-byte: `~/old-clone/` removed 0 rows, said nothing."""
        os.environ["HOME"] = str(self.dir)  # restored by the sandbox
        gone = str(self.dir / "old-clone")
        self.con.execute("INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) "
                         "VALUES('/old.html','route','Old','Old','a.js:1',?,'frontend','t','a.js')", (gone,))
        core.set_meta(self.con, f"codemap_commit:frontend:{gone}", "a" * 40)
        self.con.commit()
        self.assertIsNone(codemap.codemap_forget(self.con, "~/never-mapped"))
        self.assertEqual(codemap.codemap_forget(self.con, "~/old-clone/"), 1)
        self.assertNotIn("old-clone", codemap.code(self.con, "--repos"))  # its bookkeeping went too

    def test_backend_linking_is_method_aware_and_handles_more_path_shapes(self):
        ui = git_repo(self.dir / "ui", {"src/Z.jsx": "fetch('/zones')"})
        api = git_repo(self.dir / "api", {"r.js": "router.get('/zones', h)"})
        self.scan(ui, api)
        self.write_map("frontend", [{"repo": str(ui), "page": "/zones", "kind": "api", "label": "list",
                                     "value": "GET https://qa.example.com/zones", "file": "src/Z.jsx:1"}])
        self.write_map("backend", [
            {"repo": str(api), "page": "*", "kind": "api", "label": "GET /zones", "value": "list", "file": "r.js:1"},
            {"repo": str(api), "page": "*", "kind": "api", "label": "DELETE /zones", "value": "drop all", "file": "r.js:1"},
            {"repo": str(api), "page": "*", "kind": "rule", "label": "POST /zones name", "value": "max 80", "file": "r.js:1"}])
        self.ingest_all()
        c = codemap.code(self.con, "/zones")
        self.assertIn("GET /zones = list", c)  # single segment, absolute URL on the frontend side
        self.assertNotIn("DELETE /zones", c)  # different method
        self.assertNotIn("POST /zones name", c)
        self.assertEqual(codemap._api_ref("fetch(`${BASE}/zones/${id}`)"), ("", "zones/:id"))

    def test_backend_links_need_the_same_resource(self):
        ui = git_repo(self.dir / "ui", {"src/I.jsx": "fetch('/api/incidents/1/status', {method: 'PATCH'})"})
        api = git_repo(self.dir / "api", {"r.js": "router.patch('/:id/status', h)"})
        self.scan(ui, api)
        self.write_map("frontend", [
            {"repo": str(ui), "page": "/incidents/:id", "kind": "api", "label": "set status",
             "value": "PATCH /api/incidents/:id/status", "file": "src/I.jsx:1"},
            {"repo": str(ui), "page": "/zones", "kind": "api", "label": "list", "value": "fetch(`${API}/zones`)",
             "file": "src/I.jsx:1"}])
        self.write_map("backend", [
            {"repo": str(api), "page": "*", "kind": "rule", "label": "PATCH /:id/status status", "value": "one of: ACTIVE",
             "file": "r.js:1"},
            {"repo": str(api), "page": "*", "kind": "rule", "label": "PATCH /api/incidents/:id/status status",
             "value": "one of: OPEN, CLOSED", "file": "r.js:1"},
            {"repo": str(api), "page": "*", "kind": "api", "label": "GET /api/admin/zones", "value": "admin", "file": "r.js:1"},
            {"repo": str(api), "page": "*", "kind": "api", "label": "GET /api/v1/zones", "value": "list", "file": "r.js:1"}])
        self.ingest_all()
        i, z = codemap.code(self.con, "/incidents/:id"), codemap.code(self.con, "/zones")
        self.assertIn("OPEN, CLOSED", i)
        self.assertNotIn("ACTIVE", i)  # a router-relative tail could be any resource
        self.assertIn("GET /api/v1/zones", z)
        self.assertNotIn("admin", z)

    def test_api_path_shapes(self):
        p = codemap._api_path
        self.assertEqual(p("GET /api/incidents?x=1"), "api/incidents")
        self.assertEqual(p("fetch('api/incidents.json')"), "api/incidents")
        self.assertEqual(p("PUT /api/zones/{id}"), "api/zones/:id")
        self.assertEqual(p("getIncidents"), "")
        r = codemap._api_ref
        self.assertEqual(r("fetch('/api/zones/' + id + '/detours')"), ("", "api/zones/:id/detours"))
        self.assertEqual(r("api.get<Zone[]>('/zones')"), ("GET", "zones"))
        self.assertEqual(r("http.post<Zone>('/api/zones', z)"), ("POST", "api/zones"))

    def test_round1_codemap_stale_card_star_and_hash_routes(self):
        repo = git_repo(self.dir / "ui", {"src/A.jsx": "<button data-testid='save'/>"})
        self.scan(repo)
        self.write_map("frontend", [
            {"page": "/a", "kind": "route", "label": "A", "value": "A", "file": "src/A.jsx:1"},
            {"page": "/a", "kind": "locator", "label": "Save", "value": "getByTestId('save')", "file": "src/A.jsx:1"},
            {"page": "/*", "kind": "route", "label": "Catchall", "value": "C", "file": "src/A.jsx:1"},
            {"page": "/#/zones/:id", "kind": "route", "label": "Zone", "value": "Z", "file": "src/A.jsx:1"},
        ])
        api = git_repo(self.dir / "api", {"r.js": "router.get('/api/x', h)"})
        self.scan(repo, api, stamp="s2")
        self.write_map("frontend", [
            {"repo": str(repo), "page": "/a", "kind": "route", "label": "A", "value": "A", "file": "src/A.jsx:1"},
            {"repo": str(repo), "page": "/a", "kind": "locator", "label": "Save", "value": "getByTestId('save')", "file": "src/A.jsx:1"},
            {"repo": str(repo), "page": "/*", "kind": "route", "label": "Catchall", "value": "C", "file": "src/A.jsx:1"},
            {"repo": str(repo), "page": "/#/zones/:id", "kind": "route", "label": "Zone", "value": "Z", "file": "src/A.jsx:1"},
        ])
        self.write_map("backend", [
            {"repo": str(api), "page": "*", "kind": "api", "label": "GET /api/x", "value": "x", "file": "r.js:1"},
        ])
        self.ingest_all()
        # 1. Stale build card still prints FROM CODE locators when LOCATORS (fresh) was skipped
        memory.ingest(self.con, {"run_id": "r1", "build": "1.0", "pages": [
            {"route": "/a", "locators": [{"name": "Save", "locator": "getByTestId('save')"}]}]}, "2026-01-01T00:00:00Z")
        memory.ingest(self.con, {"run_id": "r2", "build": "2.0", "pages": [{"route": "/other"}]}, "2026-01-02T00:00:00Z")
        self.assertIn("getByTestId('save')", memory.card(self.con, "/a"))
        # 2. code('*') returns backend rows even when a '/*' frontend route exists
        self.assertTrue(codemap.code(self.con, "*").startswith("CODE MAP * "))
        # 3. find_page resolves SPA hash routes without a leading slash
        self.assertEqual(codemap.find_page(self.con, "#/zones/42")["route"], "/#/zones/:id")


class ShellMenu(CodeMapCase):
    """The app shell's menu config gives route rows written by code, not by a mapper."""

    SHELL_NAV = {"nav": [{"key": "home", "title": "Home", "module": {"id": "m0", "url": "/m/home"}},
                         {"key": "reports", "title": "Reports",
                          "children": [{"key": "daily", "title": "Daily", "module": {"id": "m1", "url": "/m/daily"}}]}]}

    def shell_repo(self):
        return git_repo(self.dir / "ui", {"src/routes.js": "const r = [{ path: '/m/daily', element: <D/> }]\n",
                                          "public/config.json": json.dumps(self.SHELL_NAV, indent=1)})

    def routes(self):
        return [tuple(r) for r in self.con.execute("SELECT page, value FROM codemap WHERE kind='route' ORDER BY page")]

    def test_shell_menu_routes_survive_a_full_frontend_map(self):
        ui = self.shell_repo()
        s = self.scan(ui)
        frontend = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]
        self.assertEqual(frontend["files"], ["src/routes.js"])  # the menu is not a mapper's file
        self.write_map("frontend", [{"page": "/m/daily", "kind": "route", "label": "Daily", "value": "D",
                                     "file": "src/routes.js:1"}])
        self.ingest_all()
        self.assertEqual(self.routes(), [("/home", "module m0 at /m/home"), ("/m/daily", "D"),
                                         ("/reports/daily", "module m1 at /m/daily")])
        self.assertFalse(s["incremental"])

    def test_unchanged_shell_menu_keeps_the_next_scan_incremental(self):
        ui = self.shell_repo()
        self.scan(ui)
        self.write_map("frontend", [{"page": "/m/daily", "kind": "route", "label": "Daily", "value": "D",
                                     "file": "src/routes.js:1"}])
        self.ingest_all()
        s = self.scan(ui, stamp="s2")
        self.assertTrue(s["incremental"])
        self.assertEqual(s["areas"]["frontend"]["files"], 0)  # a stored "" hash for the menu forced a full re-map
        self.assertEqual(len(self.routes()), 3)

    def test_menu_without_modules_drops_its_old_routes(self):  # rows of a module-less menu stayed forever
        ui = self.shell_repo()
        self.scan(ui)
        self.assertEqual(len(self.routes()), 2)
        (ui / "public/config.json").write_text(json.dumps({"nav": [{"key": "home", "module": None}]}))
        git(ui, "commit", "-qam", "no modules")
        self.scan(ui, stamp="s2")
        self.assertEqual(self.routes(), [])

    def test_deeply_nested_shell_menu_never_breaks_the_scan(self):  # RecursionError escaped "never raises"
        deep = {"key": "a", "title": "A", "module": {"id": "m", "url": "/m/a"}}
        for _ in range(3000):
            deep = {"key": "a", "children": [deep]}
        ui = git_repo(self.dir / "ui", {"src/routes.js": "const r = [{ path: '/m/a', element: <A/> }]\n",
                                        "public/config.json": json.dumps({"nav": [deep]})})
        s = self.scan(ui)
        self.assertEqual(s["areas"]["frontend"]["files"], 1)
        self.assertEqual(self.routes(), [])

    def test_renamed_menu_path_remaps_the_tests_in_full(self):  # a changed multi-route file moves routes
        ui = git_repo(self.dir / "ui", {"public/config.json": json.dumps(self.SHELL_NAV), "e2e/home.spec.ts":
                                        "import { test } from '@playwright/test';\n"
                                        "test('home', async ({ page }) => { await page.getByRole('button').click(); });\n"})
        self.scan(ui)
        self.write_map("tests", [{"page": "/home", "kind": "locator", "label": "Open", "value": "getByRole('button')",
                                  "file": "e2e/home.spec.ts:2"}])
        self.ingest_all()
        self.assertEqual(self.con.execute("SELECT count(*) FROM codemap WHERE page='/home' AND area='tests'").fetchone()[0], 1)
        nav = json.loads(json.dumps(self.SHELL_NAV))
        nav["nav"][0]["key"] = "start"
        (ui / "public/config.json").write_text(json.dumps(nav))
        git(ui, "commit", "-qam", "rename")
        self.scan(ui, stamp="s2")  # the menu holds several routes and changed: the tests are mapped in full
        t = json.loads((self.out / "scan-tests.json").read_text())["repos"][0]
        self.assertEqual((t["full"], t["files"]), (True, ["e2e/home.spec.ts"]))
        self.write_map("tests", [{"page": "/start", "kind": "locator", "label": "Open", "value": "getByRole('button')",
                                  "file": "e2e/home.spec.ts:2"}])
        self.ingest_all()
        self.assertEqual(self.con.execute("SELECT count(*) FROM codemap WHERE page='/home'").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT count(*) FROM codemap WHERE page='/start' AND area='tests'").fetchone()[0], 1)

    def test_a_menu_route_never_pushes_locators_off_a_full_page(self):
        ui = git_repo(self.dir / "ui", {"src/Main.jsx": "const Main = () => <form/>;\n"})
        self.scan(ui)
        self.write_map("frontend", [{"page": "/home", "kind": "locator", "label": f"Field {i}", "value": "x",
                                     "file": "src/Main.jsx:1"} for i in range(codemap.CODEMAP_PAGE_CAP)])
        self.ingest_all()
        (ui / "public").mkdir()
        (ui / "public/config.json").write_text(json.dumps({"nav": [{"key": "home", "title": "Home", "module": {"url": "/m"}}]}))
        git(ui, "add", "-A")
        git(ui, "commit", "-qm", "menu")
        self.scan(ui, stamp="s2")
        self.assertEqual(dict(self.con.execute("SELECT kind, count(*) FROM codemap WHERE page='/home' GROUP BY kind")
                              .fetchall()), {"locator": codemap.CODEMAP_PAGE_CAP, "route": 1})

    def test_a_renamed_menu_title_moves_the_page_name(self):  # the first title stayed the page's name
        nav = lambda a, b: json.dumps({"nav": [{"key": k, "title": n, "module": {"url": "/m/" + k}}
                                               for k, n in (("a", a), ("b", b))]})
        ui = git_repo(self.dir / "ui", {"public/config.json": nav("Alpha", "Beta")})
        self.scan(ui)
        (ui / "public/config.json").write_text(nav("Beta", "Alpha"))
        git(ui, "commit", "-qam", "swap")
        self.scan(ui, stamp="s2")
        self.assertEqual(codemap.find_page(self.con, "Alpha")["route"], "/b")

    def test_menu_urls_are_redacted_like_a_mappers_rows(self):
        token = "ghp_" + "a" * 36
        ui = git_repo(self.dir / "ui", {"public/config.json": json.dumps(
            {"nav": [{"key": "home", "title": "Home", "module": {"url": f"/m/home?sig={token}"}}]})})
        self.scan(ui)
        self.assertNotIn(token, json.dumps(self.routes()))
        self.assertEqual(sorted(p.name for p in self.out.glob("*-0.json*")), [])  # the menu is applied in memory: no files

    def test_a_broken_menu_drops_its_routes_and_settles(self):  # stale routes, a full re-map every scan
        ui = self.shell_repo()
        self.scan(ui)
        self.write_map("frontend", [{"page": "/m/daily", "kind": "route", "label": "Daily", "value": "D",
                                     "file": "src/routes.js:1"}])
        self.ingest_all()
        (ui / "public/config.json").write_text('{"nav": [{"key": "a", "module": {"url": "/x"} BROKEN')
        git(ui, "commit", "-qam", "broken")
        self.scan(ui, stamp="s2")  # the menu changed: one full pass, as for any changed multi-route menu
        self.assertEqual(self.routes(), [("/m/daily", "D")])  # the menu's two routes are gone, the router's stays
        self.write_map("frontend", [{"page": "/m/daily", "kind": "route", "label": "Daily", "value": "D",
                                     "file": "src/routes.js:1"}])
        self.ingest_all()
        s = self.scan(ui, stamp="s3")  # and not on every scan after it
        self.assertTrue(s["incremental"])
        self.assertEqual(s["areas"]["frontend"]["files"], 0)


if __name__ == "__main__":
    unittest.main()
