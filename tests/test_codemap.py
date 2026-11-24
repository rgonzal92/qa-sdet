"""Code map (/qa-sdet:qa-init): scan, ingest, card block, `code`, search. Hints from source, never requirements."""
import json, os, shutil, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "lib"))
from qa_sdet import core, codemap, scan as scan_mod, memory, cli
FIX = ROOT / "tests/fixture-src"


def git(repo, *args):  # isolated from the developer's git config (signing, hooks, templates)
    subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
                   check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})


def git_repo(path, files):
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    git(path, "init", "-q")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "one")
    return path


class CodeMap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        env = mock.patch.dict(os.environ)  # restore QA_MEM_DB (and the rest) afterwards
        env.start()
        self.addCleanup(env.stop)
        os.environ["QA_MEM_DB"] = str(self.dir / "t.db")
        self.con = core.connect()
        self.out = self.dir / "qa-out" / "init"
        memory.PARKED.clear()  # a module-level list: one test's parked file must not leak into the next

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def scan(self, *paths, full=False, stamp="s1"):
        return scan_mod.codemap_scan(self.con, [str(p) for p in paths], self.out, full, stamp)

    def test_root_and_home_are_refused_not_walked(self):
        with mock.patch.dict(os.environ, HOME=str(self.dir)):  # a real folder, whatever HOME the run has
            out = self.scan("/", "~")
        self.assertEqual(out["total_files"], 0)
        self.assertEqual(len(out["skipped"]), 2)
        self.assertTrue(all("too broad" in x for x in out["skipped"]), out["skipped"])

    def write_map(self, area, rows, scan_id=None):
        scan = json.loads((self.out / f"scan-{area}.json").read_text())
        f = self.out / f"code-map-{area}.json"
        f.write_text(json.dumps({"scan_id": scan_id or scan["scan_id"], "area": area, "rows": rows}))
        return f

    def ingest_all(self):
        self.assertEqual(memory.PARKED, [], memory.PARKED)
        memory.ingest_pending(self.dir / "qa-out")
        self.con.close()
        self.con = core.connect()

    # ---- scan ----
    def test_scan_classifies_areas_and_skips_secrets_and_generated(self):
        s = self.scan(FIX / "frontend", FIX / "backend", FIX / "e2e")
        files = {a: [f for r in json.loads((self.out / f"scan-{a}.json").read_text())["repos"] for f in r["files"]]
                 for a in codemap.CODEMAP_AREAS}
        self.assertEqual(sorted(files["frontend"]), ["src/api/client.js", "src/components/IncidentMap.jsx",
                                                     "src/components/IncidentsTable.jsx",
                                                     "src/components/SpeedZoneForm.jsx", "src/routes.js"])
        self.assertEqual(sorted(files["backend"]), ["routes/admin.js", "routes/zones.js"])
        self.assertEqual(files["tests"], ["pages/SpeedZonePage.ts"])
        listed = json.dumps(files)
        for never in (".env", "node_modules", "dist", "bundle.min.js", "app.js", "secrets.json", "README.md"):
            self.assertNotIn(never, listed)
        self.assertEqual(s["total_files"], 8)
        self.assertFalse(s["incremental"])
        self.assertGreater(s["tokens"], 0)
        self.assertTrue(s["areas"]["tests"]["needs_mapper"])

    def test_classify_source(self):
        c = scan_mod.classify_source
        self.assertEqual(c("src/app/app.routes.ts", "export const routes: Routes = [{ path: 'zones' }]"), "frontend")
        self.assertEqual(c("src/Main.java", "@GetMapping(\"/api/zones\") public List<Zone> all()"), "backend")
        self.assertEqual(c("api/openapi.yaml", "openapi: 3.0.0\npaths:"), "backend")
        self.assertEqual(c("e2e/zones.spec.ts", "await page.getByRole('button').click()"), "tests")
        self.assertIsNone(c("src/util.test.ts", "expect(add(1, 2)).toBe(3)"))  # a unit test: nothing about pages
        self.assertEqual(c("src/auth/PasswordReset.tsx", "export const P = () => <form/>"), "frontend")  # a page, not a secret
        for secret in ("config/secrets.json", ".env.local", "certs/server.pem", "appsettings.Production.json", "credentials.yaml"):
            self.assertIsNone(c(secret, "data-testid password=hunter2"), secret)
        # review findings: components by file type, file-routed pages, backends of other stacks, misrouting
        self.assertEqual(c("src/zones/ZoneTable.vue", "<template><table/></template>"), "frontend")
        self.assertEqual(c("app/zones/page.tsx", "export default function Page() { return <Zones/> }"), "frontend")
        self.assertEqual(c("src/pages/zones/[id].ts", "export const load = () => ({})"), "frontend")
        self.assertEqual(c("src/app/zones/zones.page.ts", "@Component({ selector: 'app-zones' })"), "frontend")  # Ionic
        self.assertEqual(c("Program.cs", 'app.MapGet("/api/zones", () => db.Zones);'), "backend")
        self.assertEqual(c("main.go", 'r.GET("/api/zones", listZones)'), "backend")
        self.assertEqual(c("app/zones.py", "@zones.route('/api/zones', methods=['POST'])"), "backend")
        self.assertEqual(c("Dto/ZoneDto.cs", "[Required]\n[Range(0, 130)] public int SpeedLimit"), "backend")
        self.assertEqual(c("validators/zone.js", "body('speed').isInt({ min: 0, max: 130 })"), "backend")
        self.assertEqual(c("src/zone.dto.ts", "@IsEmail() email; @MaxLength(80) name;"), "backend")
        self.assertIsNone(c("service/ZoneService.java", 'throw new IllegalArgumentException("name is required");'))
        self.assertEqual(c("src/api/zones.ts", "export const save = (z) => api.post('/zones', z)"), "frontend")  # a client
        self.assertEqual(c("src/integration/ZoneController.java", '@GetMapping("/api/zones") List<Zone> all()'), "backend")
        self.assertEqual(c("src/test/ZonePage.java", "public class ZonePage { @FindBy(id = \"speed\") WebElement speed; }"),
                         "tests")  # a Selenium page object
        self.assertIsNone(c("package-lock.json", "{}"))
        self.assertIsNone(c("src/logo.svg", "<svg aria-label='x'/>"))
        self.assertEqual(c("pages/Login.ts", "import { test } from '@playwright/test'; page.locator('#u')", "e2e"), "tests")

    def test_incremental_rescan_reads_only_changed_files(self):
        repo = self.dir / "ui"
        shutil.copytree(FIX / "frontend" / "src", repo / "src")
        git(repo, "init", "-q")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "one")
        self.scan(repo)
        self.write_map("frontend", [{"page": "/form.html", "kind": "route", "label": "Speed zone",
                                     "value": "SpeedZoneForm", "file": "src/routes.js:4"},
                                    {"page": "/index.html", "kind": "locator", "label": "Incidents table",
                                     "value": "#t", "file": "src/components/IncidentsTable.jsx:6"}])
        self.ingest_all()
        (repo / "src/components/SpeedZoneForm.jsx").write_text("export const F = () => <input data-testid='speed' />;\n")
        (repo / "src/components/Detours.jsx").write_text("export const D = () => <button aria-label='Add detour' />;\n")
        (repo / "src/components/IncidentsTable.jsx").unlink()
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "two")
        s = self.scan(repo, stamp="s2")
        r = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]
        self.assertTrue(s["incremental"])
        self.assertFalse(r["full"])
        self.assertEqual(r["files"], ["src/components/Detours.jsx", "src/components/SpeedZoneForm.jsx"])
        self.assertEqual(r["deleted"], ["src/components/IncidentsTable.jsx"])
        other = json.loads((self.out / "scan-tests.json").read_text())["repos"][0]
        self.assertEqual(other["deleted"], [])  # a changed frontend file isn't "deleted" for an area that never had it
        self.write_map("frontend", [])  # the mapper found nothing new
        self.ingest_all()
        self.assertNotIn("Incidents table", codemap.code(self.con, "/index.html"))  # its file is gone
        self.assertIn("Speed zone", codemap.code(self.con))  # untouched file: kept
        full = self.scan(repo, full=True, stamp="s3")
        self.assertFalse(full["incremental"])

    # ---- ingest ----
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

    def test_code_page_is_not_a_new_build_for_runs(self):
        """A page the code map created has no build: the first real run must not treat that as a build change."""
        self.scan(FIX / "frontend")
        self.write_map("frontend", [{"page": "/form.html", "kind": "route", "label": "Speed zone", "value": "F",
                                     "file": "src/routes.js:4"}])
        self.ingest_all()
        memory.ingest(self.con, {"run_id": "p1", "build": "", "pages": [  # a plan: build unknown, ticket fact
            {"route": "/form.html", "facts": [{"kind": "rule", "text": "speed max 120", "source": "TRAF-1"}]}]})
        self.con.execute("UPDATE pages SET build=NULL WHERE route='/form.html'")  # as if the map came last
        self.con.commit()
        memory.ingest(self.con, {"run_id": "r1", "build": "1.0", "pages": [{"route": "/form.html"}]})
        self.assertEqual(self.con.execute("SELECT stale FROM facts").fetchone()[0], 0)

    # ---- card, code, search ----
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

    def test_empty_map_tips(self):
        self.assertIn("tip: /qa-sdet:qa-init", memory.card(self.con, "closures"))
        self.assertIn("no code map yet", codemap.code(self.con))
        self.scan(FIX / "frontend")
        self.write_map("frontend", [{"page": "/form.html", "kind": "route", "label": "Speed zone", "value": "F",
                                     "file": "src/routes.js:4"}])
        self.ingest_all()
        self.assertNotIn("tip:", memory.card(self.con, "closures"))
        self.assertIn("/form.html  Speed zone", codemap.code(self.con))

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
        """r16: a folder that is gone was matched only byte-for-byte: `~/old-clone/` removed 0 rows, said nothing."""
        os.environ["HOME"], home = str(self.dir), os.environ.get("HOME")
        self.addCleanup(lambda: os.environ.__setitem__("HOME", home) if home else os.environ.pop("HOME"))
        gone = str(self.dir / "old-clone")
        self.con.execute("INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) "
                         "VALUES('/old.html','route','Old','Old','a.js:1',?,'frontend','t','a.js')", (gone,))
        core.set_meta(self.con, f"codemap_commit:frontend:{gone}", "a" * 40)
        self.con.commit()
        self.assertIsNone(codemap.codemap_forget(self.con, "~/never-mapped"))
        self.assertEqual(codemap.codemap_forget(self.con, "~/old-clone/"), 1)
        self.assertNotIn("old-clone", codemap.code(self.con, "--repos"))  # its bookkeeping went too

    def test_behind_note_covers_the_backend_rows_on_the_card(self):
        """r16: the note named only the page's own repo, so a backend 3 commits on looked current beside its rules."""
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

    def test_code_lists_totals_even_with_only_a_backend(self):
        """r16: with only backend rows, `code` printed a bare header; qa-init's reply takes its counts from it."""
        api = git_repo(self.dir / "api", {"routes/zones.js": "router.post('/api/zones', h)\n"})
        self.scan(api)
        self.write_map("backend", [{"page": "*", "kind": "api", "label": "POST /api/zones", "value": "x",
                                    "file": "routes/zones.js:1"}])
        self.ingest_all()
        out = codemap.code(self.con)
        self.assertIn("(none: no frontend routes mapped yet)", out)
        self.assertIn("TOTAL: 0 routes, 0 locators, 0 rules, 1 endpoints (1 backend", out)

    # ---- the app shell's menu (BUG-1): route rows written by code, not by a mapper ----
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

    def test_menu_without_modules_drops_its_old_routes(self):  # audit r3: rows of a module-less menu stayed forever
        ui = self.shell_repo()
        self.scan(ui)
        self.assertEqual(len(self.routes()), 2)
        (ui / "public/config.json").write_text(json.dumps({"nav": [{"key": "home", "module": None}]}))
        git(ui, "commit", "-qam", "no modules")
        self.scan(ui, stamp="s2")
        self.assertEqual(self.routes(), [])

    def test_deeply_nested_shell_menu_never_breaks_the_scan(self):  # audit r2: RecursionError escaped "never raises"
        deep = {"key": "a", "title": "A", "module": {"id": "m", "url": "/m/a"}}
        for _ in range(3000):
            deep = {"key": "a", "children": [deep]}
        ui = git_repo(self.dir / "ui", {"src/routes.js": "const r = [{ path: '/m/a', element: <A/> }]\n",
                                        "public/config.json": json.dumps({"nav": [deep]})})
        s = self.scan(ui)
        self.assertEqual(s["areas"]["frontend"]["files"], 1)
        self.assertEqual(self.routes(), [])

    def test_ingest_pending_accepts_the_init_folder(self):  # ISSUE-4: qa-out/init loaded nothing, silently
        api = git_repo(self.dir / "api", {"routes/zones.js": "router.post('/api/zones', h)\n"})
        self.scan(api)
        self.write_map("backend", [{"page": "*", "kind": "api", "label": "POST /api/zones", "value": "x",
                                    "file": "routes/zones.js:1"}])
        self.assertEqual(memory.ingest_pending(self.out), 1)

    def test_renamed_menu_path_remaps_the_tests_in_full(self):  # audit pi-1: a changed multi-route file moves routes
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

    def test_a_page_renamed_inside_its_own_one_route_file_keeps_stale_rows_until_full(self):  # accepted loss
        ui = git_repo(self.dir / "ui", {"src/routes.js": "export const routes = [{ path: '/zones' }]",
                                        "e2e/zones.spec.ts": "import { test } from '@playwright/test';\n"
                                                             "test('z', async ({ page }) => { await page.goto('/zones'); await page.getByRole('row').click(); });\n"})
        self.scan(ui)
        self.write_map("frontend", [{"page": "/zones", "kind": "route", "label": "Zones", "value": "Z", "file": "src/routes.js:1"}])
        self.write_map("tests", [{"page": "/zones", "kind": "locator", "label": "Row", "value": "getByRole('row')",
                                  "file": "e2e/zones.spec.ts:2"}])
        self.ingest_all()
        (ui / "src/routes.js").write_text("export const routes = [{ path: '/areas' }]")
        git(ui, "commit", "-qam", "rename")
        s = self.scan(ui, stamp="s2")
        self.assertEqual((s["areas"]["frontend"]["files"], s["areas"]["tests"]["files"]), (1, 0))  # no full re-map
        self.write_map("frontend", [{"page": "/areas", "kind": "route", "label": "Areas", "value": "A", "file": "src/routes.js:1"}])
        self.ingest_all()
        self.assertIn("Row", codemap.code(self.con, "/zones"))  # stale until `qa-init --full`
        s = self.scan(ui, full=True, stamp="s3")
        self.assertEqual(s["areas"]["tests"]["files"], 1)

    def test_a_menu_route_never_pushes_locators_off_a_full_page(self):  # audit pi-4
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

    def test_a_renamed_menu_title_moves_the_page_name(self):  # audit pi-6: the first title stayed the page's name
        nav = lambda a, b: json.dumps({"nav": [{"key": k, "title": n, "module": {"url": "/m/" + k}}
                                               for k, n in (("a", a), ("b", b))]})
        ui = git_repo(self.dir / "ui", {"public/config.json": nav("Alpha", "Beta")})
        self.scan(ui)
        (ui / "public/config.json").write_text(nav("Beta", "Alpha"))
        git(ui, "commit", "-qam", "swap")
        self.scan(ui, stamp="s2")
        self.assertEqual(codemap.find_page(self.con, "Alpha")["route"], "/b")

    def test_menu_urls_are_redacted_like_a_mappers_rows(self):  # audit pi-3
        token = "ghp_" + "a" * 36
        ui = git_repo(self.dir / "ui", {"public/config.json": json.dumps(
            {"nav": [{"key": "home", "title": "Home", "module": {"url": f"/m/home?sig={token}"}}]})})
        self.scan(ui)
        self.assertNotIn(token, json.dumps(self.routes()))
        self.assertEqual(sorted(p.name for p in self.out.glob("*-0.json*")), [])  # the menu is applied in memory: no files

    def test_an_api_spec_with_menu_like_properties_is_not_a_menu(self):  # audit pi-7
        spec = json.dumps({"openapi": "3.0.0", "paths": {"/api/settings": {}}, "components": {"schemas": {"S": {
            "properties": {"module": {}, "key": {}, "nav": {}}}}}})
        self.assertEqual(scan_mod.classify_source("api/openapi-config.json", spec), "backend")

    def test_a_broken_menu_drops_its_routes_and_settles(self):  # audit agy-13: stale routes, a full re-map every scan
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

    # ---- review findings ----
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

    def test_incremental_handles_renames_unicode_ignored_and_new_candidates(self):
        repo = git_repo(self.dir / "ui", {"src/A.jsx": "<b data-testid='a'/>", "src/Zoné café.jsx": "<b/>",
                                          ".gitignore": "src/gen/\n", "src/gen/Gen.jsx": "<b data-testid='gen'/>"})
        first = self.scan(repo)
        files = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]["files"]
        self.assertNotIn("src/gen/Gen.jsx", files)  # ignored: generated code
        self.assertIn("src/Zoné café.jsx", files)
        self.write_map("frontend", [{"page": "/a", "kind": "locator", "label": "A", "value": "#a", "file": "src/A.jsx:1"}])
        self.ingest_all()
        git(repo, "mv", "src/A.jsx", "src/B.jsx")
        (repo / "src/Zoné café.jsx").write_text("<b data-testid='z'/>")
        git(repo, "commit", "-qam", "rename + edit")
        self.scan(repo, stamp="s2")
        r = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]
        self.assertEqual(r["files"], ["src/B.jsx", "src/Zoné café.jsx"])
        self.assertEqual(r["deleted"], ["src/A.jsx"])

    def test_scan_stays_in_the_repo_reads_any_encoding_and_lists_a_folder_once(self):  # r14
        route = "router.get('/api/zones', (req, res) => res.json([]))\n"
        outside = self.dir / "outside"
        outside.mkdir()
        (outside / "zones.js").write_text("// not this repo\n" + route)
        repo = git_repo(self.dir / "api", {"server/zones.js": route, "src/app.js": route})
        shutil.rmtree(repo / "server")
        (repo / "server").symlink_to(outside)  # a tracked folder replaced by a link out of the repo
        (repo / "Zones.cs").write_bytes("[ApiController]\n[Route(\"api/zones\")]\npublic class Z { [HttpGet] void G(){} }"
                                        .encode("utf-16"))  # Visual Studio can save UTF-16 with a BOM
        (repo / "legacy.php").write_bytes("<?php Route::get('/caf\xe9', fn() => 1);".encode("latin-1"))
        self.scan(repo, str(repo) + "/")  # the same folder twice
        repos = json.loads((self.out / "scan-backend.json").read_text())["repos"]
        self.assertEqual([r["files"] for r in repos], [["Zones.cs", "legacy.php", "src/app.js"]])
        self.write_map("backend", [])
        self.ingest_all()
        (repo / "legacy.php").write_bytes("<?php Route::get('/caf\xf6', fn() => 1);".encode("latin-1"))  # é -> ö only
        self.scan(repo, stamp="s2")
        self.assertEqual(json.loads((self.out / "scan-backend.json").read_text())["repos"][0]["files"], ["legacy.php"])

    def test_secret_stores_in_code_files_are_never_listed(self):  # r14
        for name in ("secrets.py", "app/secrets.rb", "Api/Secrets.cs", "credentials.py", "wp-config.php"):
            self.assertIsNone(scan_mod.classify_source(name, "Route::get('/x', fn() => 1);\n@app.route('/x')\n[HttpGet]"), name)
        self.assertEqual(scan_mod.classify_source("Api/Credentials.cs", "public record Credentials([Required] string User);"),
                         "backend")  # a login DTO class, not a store

    def test_bad_commit_values_never_reach_git(self):
        repo = git_repo(self.dir / "ui", {"src/A.jsx": "<b data-testid='a'/>"})
        self.con.execute("INSERT INTO meta VALUES(?, ?)", (f"codemap_commit:frontend:{repo}", "--output=/tmp/x"))
        self.con.commit()
        s = self.scan(repo)
        self.assertFalse(s["incremental"])  # an invalid commit means a full scan, never a git option

    def test_code_maps_outside_init_are_ignored(self):
        self.scan(FIX / "frontend")
        stray = self.dir / "qa-out" / "form" / "code-map-frontend.json"
        stray.parent.mkdir(parents=True)
        stray.write_text(json.dumps({"scan_id": "x", "area": "frontend", "rows": []}))
        memory.PARKED.clear()
        memory.ingest_pending(self.dir / "qa-out")
        self.assertEqual(memory.PARKED, [])
        self.assertFalse((stray.parent / "code-map-frontend.json.ingested").exists())

    # ---- review round 2 ----
    def test_route_rename_moves_component_rows(self):
        repo = git_repo(self.dir / "ui", {"src/routes.js": "export const routes = [{ path: '/zones' }, { path: '/map' }]",
                                          "src/Zones.jsx": "<input data-testid='speed'/>"})
        self.scan(repo)
        self.write_map("frontend", [
            {"page": "/zones", "kind": "route", "label": "Speed zones", "value": "Zones", "file": "src/routes.js:1"},
            {"page": "/map", "kind": "route", "label": "Map", "value": "Map", "file": "src/routes.js:1"},
            {"page": "/zones", "kind": "locator", "label": "Speed", "value": "getByTestId('speed')", "file": "src/Zones.jsx:1"}])
        self.ingest_all()
        (repo / "src/routes.js").write_text("export const routes = [{ path: '/speed-zones' }, { path: '/map' }]")
        git(repo, "commit", "-qam", "rename route")
        self.scan(repo, stamp="s2")
        r = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]
        self.assertTrue(r["full"])  # a router changed: components are re-placed in the same run
        self.write_map("frontend", [
            {"page": "/speed-zones", "kind": "route", "label": "Speed zones", "value": "Zones", "file": "src/routes.js:1"},
            {"page": "/map", "kind": "route", "label": "Map", "value": "Map", "file": "src/routes.js:1"},
            {"page": "/speed-zones", "kind": "locator", "label": "Speed", "value": "getByTestId('speed')",
             "file": "src/Zones.jsx:1"}])
        self.ingest_all()
        self.assertIsNone(codemap.find_page(self.con, "/zones"))
        self.assertEqual(codemap.find_page(self.con, "speed zones")["route"], "/speed-zones")

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

    def test_edited_files_are_read_by_content_not_git_state(self):
        repo = git_repo(self.dir / "ui", {"src/A.jsx": "<b data-testid='a'/>"})
        self.scan(repo)
        self.write_map("frontend", [])
        self.ingest_all()
        (repo / "src/A.jsx").write_text("<b data-testid='a2'/>")  # edited, not committed
        self.assertEqual(self.scan(repo, stamp="s2")["areas"]["frontend"]["files"], 1)
        self.write_map("frontend", [{"page": "/a", "kind": "locator", "label": "A", "value": "#a2", "file": "src/A.jsx:1"}])
        self.ingest_all()
        self.assertEqual(self.scan(repo, stamp="s3")["areas"]["frontend"]["files"], 0)  # same content: not again
        self.write_map("frontend", [])
        git(repo, "checkout", "--", "src/A.jsx")  # the edit is discarded
        self.assertEqual(self.scan(repo, stamp="s4")["areas"]["frontend"]["files"], 1)  # its rows came from the edit
        plain = self.dir / "plain"  # not a git repo at all: content still decides
        (plain / "src").mkdir(parents=True)
        (plain / "src/B.jsx").write_text("<b/>")
        self.scan(plain, stamp="p1")
        self.write_map("frontend", [])
        self.ingest_all()
        self.assertEqual(self.scan(plain, stamp="p2")["areas"]["frontend"]["files"], 0)

    def test_more_classification_from_review_two(self):
        c = scan_mod.classify_source
        self.assertEqual(c("app/controllers/zoneController.js", "exports.create = async (req, res) => { if (!req.body.name) }"), "backend")
        self.assertEqual(c("app/models/zone.js", "const Zone = new Schema({ name: { type: String, required: true } })"), "backend")
        self.assertIsNone(c("routes/index.js", "module.exports = require('./zones')"))
        self.assertIsNone(c("src/Button.stories.tsx", "export default { title: 'Button' }"))
        self.assertIsNone(c("src/__mocks__/api.ts", "export const get = () => fetch('/api/x')"))
        self.assertIsNone(c("src/Form.test.tsx", "render(<Form/>); screen.getByRole('button')"))  # RTL unit test
        self.assertIsNone(c("docker-compose.yml", "services:\n  api:\n    environment: [DB_PASSWORD=x]  # swagger ui"))
        self.assertIsNone(c("application.yml", "openapi: 3.0.0\nspring.datasource.password: x"))
        self.assertEqual(c("api/openapi.yaml", "openapi: 3.0.1\npaths: {}"), "backend")
        self.assertIsNone(c("api/notes.yaml", "# about openapi\nname: x"))
        self.assertEqual(c("src/routes/zones/+page.ts", "export const load = async () => ({})"), "frontend")

    def test_backend_schema_files_go_to_the_backend(self):
        repo = git_repo(self.dir / "api", {"src/routes.ts": "router.post('/api/zones', validate(zoneSchema), h)",
                                           "src/schemas/zone.ts": "import { z } from 'zod'; export const zoneSchema = "
                                                                  "z.object({ speedLimit: z.number().max(130) })"})
        s = self.scan(repo)
        self.assertEqual(s["areas"]["backend"]["files"], 2)
        self.assertEqual(s["areas"]["frontend"]["files"], 0)

    def test_large_areas_are_chunked_and_chunks_ingest_in_any_order(self):
        repo = git_repo(self.dir / "ui", {f"src/C{i}.jsx": f"<b data-testid='c{i}'/>" for i in range(5)})
        s = scan_mod.codemap_scan(self.con, [str(repo)], self.out, False, "s1", chunk_files=2, chunk_tokens=10**9)
        mappers = s["areas"]["frontend"]["mappers"]
        self.assertEqual([m["files"] for m in mappers], [2, 2, 1])
        for m in reversed(mappers):  # ingested in reverse order: nothing may wipe another chunk's rows
            scan = json.loads(Path(m["scan_file"]).read_text())
            rows = [{"page": "/c", "kind": "locator", "label": f, "value": "#" + f, "file": f + ":1"}
                    for r in scan["repos"] for f in r["files"]]
            Path(m["out"]).write_text(json.dumps({"scan_id": m["scan_id"], "area": "frontend", "rows": rows}))
            self.ingest_all()
        self.assertEqual(self.con.execute("SELECT count(*) FROM codemap").fetchone()[0], 5)
        (repo / "src/C4.jsx").unlink()
        git(repo, "commit", "-qam", "drop C4")
        s = scan_mod.codemap_scan(self.con, [str(repo)], self.out, True, "s2", chunk_files=2, chunk_tokens=10**9)  # --full
        for m in s["areas"]["frontend"]["mappers"]:
            scan = json.loads(Path(m["scan_file"]).read_text())
            rows = [{"page": "/c", "kind": "locator", "label": f, "value": "#" + f, "file": f + ":1"}
                    for r in scan["repos"] for f in r["files"]]
            Path(m["out"]).write_text(json.dumps({"scan_id": m["scan_id"], "area": "frontend", "rows": rows}))
        self.ingest_all()
        self.assertEqual(sorted(r[0] for r in self.con.execute("SELECT src FROM codemap")),
                         [f"src/C{i}.jsx" for i in range(4)])

    def test_a_new_scan_clears_old_maps(self):
        self.scan(FIX / "frontend")
        self.write_map("frontend", [])
        (self.out / "code-map-frontend.json.error").write_text("x")
        self.scan(FIX / "frontend", stamp="s2")
        self.assertFalse((self.out / "code-map-frontend.json").exists())
        self.assertFalse((self.out / "code-map-frontend.json.error").exists())

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

    # ---- review round 3 ----
    def map_chunks(self, s, rows_for):
        for m in s["areas"]["frontend"]["mappers"]:
            scan = json.loads(Path(m["scan_file"]).read_text())
            files = [f for r in scan["repos"] for f in r["files"]]
            Path(m["out"]).write_text(json.dumps({"scan_id": m["scan_id"], "area": "frontend",
                                                  "rows": [x for f in files for x in rows_for(f)]}))
        return s["areas"]["frontend"]["mappers"]

    def test_route_moving_between_chunks_keeps_component_rows(self):
        files = {f"src/C{i}.jsx": f"<b data-testid='c{i}'/>" for i in range(2)}
        files["src/routes.js"] = "export const routes = [{ path: '/zones' }, { path: '/map' }]"
        repo = git_repo(self.dir / "ui", files)
        route_rows = lambda f: [{"page": p, "kind": "route", "label": p, "value": "X", "file": f + ":1"}
                                for p in ("/zones", "/map")] if "rout" in f else []
        comp = lambda f: [{"page": "/zones", "kind": "locator", "label": f, "value": "#" + f, "file": f + ":1"}] if "/C" in f else []
        s = scan_mod.codemap_scan(self.con, [str(repo)], self.out, False, "s1", chunk_files=10**6)
        self.map_chunks(s, lambda f: route_rows(f) + comp(f))
        self.ingest_all()
        git(repo, "mv", "src/routes.js", "src/router.js")  # the routes move to a file that lands in another chunk
        git(repo, "commit", "-qm", "rename router")
        s = scan_mod.codemap_scan(self.con, [str(repo)], self.out, False, "s2", chunk_files=2, chunk_tokens=10**9)
        mappers = self.map_chunks(s, lambda f: route_rows(f) + comp(f))
        self.assertGreater(len(mappers), 1)
        for m in mappers:  # the chunk without the router first: the route seems gone until the other chunk lands
            self.ingest_all()
        self.assertEqual(sorted(r[0] for r in self.con.execute("SELECT label FROM codemap WHERE kind='locator'")),
                         ["src/C0.jsx", "src/C1.jsx"])

    def test_a_chunk_that_never_lands_is_listed_again(self):
        repo = git_repo(self.dir / "ui", {f"src/C{i}.jsx": f"<b data-testid='c{i}'/>" for i in range(4)})
        s = scan_mod.codemap_scan(self.con, [str(repo)], self.out, False, "s1", chunk_files=2, chunk_tokens=10**9)
        mappers = self.map_chunks(s, lambda f: [{"page": "/c", "kind": "locator", "label": f, "value": "#" + f, "file": f + ":1"}])
        Path(mappers[1]["out"]).unlink()  # the second mapper never wrote its map
        self.ingest_all()
        s2 = scan_mod.codemap_scan(self.con, [str(repo)], self.out, False, "s2", chunk_files=10, chunk_tokens=10**9)
        listed = json.loads(Path(s2["areas"]["frontend"]["mappers"][0]["scan_file"]).read_text())["repos"][0]["files"]
        self.assertTrue({"src/C2.jsx", "src/C3.jsx"} <= set(listed))  # never hashed: the missing chunk comes back
        self.assertFalse({"src/C0.jsx", "src/C1.jsx"} & set(listed))

    def test_map_for_the_wrong_area_is_rejected(self):
        self.scan(FIX / "frontend", FIX / "backend")
        scan = json.loads((self.out / "scan-frontend.json").read_text())
        (self.out / "code-map-frontend.json").write_text(json.dumps({"scan_id": scan["scan_id"], "area": "backend", "rows": []}))
        memory.ingest_pending(self.dir / "qa-out")
        self.assertTrue(memory.PARKED)

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

    def test_a_page_file_edit_does_not_remap_everything(self):
        repo = git_repo(self.dir / "ui", {"src/pages/zones.jsx": "export default function Zones() { return <b/> }",
                                          "src/pages/home.jsx": "export default function Home() { return <b/> }"})
        self.scan(repo)
        self.write_map("frontend", [{"page": p, "kind": "route", "label": p, "value": "X", "file": f"src/pages{p}.jsx:1"}
                                    for p in ("/zones", "/home")])
        self.ingest_all()
        (repo / "src/pages/zones.jsx").write_text("export default function Zones() { return <i/> }")
        git(repo, "commit", "-qam", "edit page")
        self.scan(repo, stamp="s2")
        r = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]
        self.assertFalse(r["full"])
        self.assertEqual(r["files"], ["src/pages/zones.jsx"])

    def test_classification_round_three(self):
        c = scan_mod.classify_source
        self.assertEqual(c("src/ApiDocs.tsx", "export const D = () => <SwaggerUI url='/openapi.json'/> // app.get('/x')"), "frontend")
        self.assertEqual(c("app/api/zones/route.ts", "export async function POST(req: Request) { }"), "backend")
        self.assertEqual(c("src/routes/api/zones/+server.ts", "export const GET = async () => json([])"), "backend")
        self.assertEqual(c("server/api/zones.post.ts", "export default defineEventHandler(async (e) => {})"), "backend")
        self.assertEqual(c("pages/api/zones.ts", "export default function handler(req: NextApiRequest, res) {}"), "backend")
        self.assertEqual(c("tests/test_zones.py", "def test_zones(page):\n    page.goto('/zones')\n    page.get_by_role('button')"), "tests")
        self.assertEqual(c("Tests/ZonePage.cs", "public class ZonePage { ILocator Save => _page.GetByRole(AriaRole.Button); }"), "tests")
        self.assertEqual(c("routes.go", 'r.Get("/api/zones", list)'), "backend")
        self.assertEqual(c("models/zone.go", 'type Zone struct { Speed int `validate:"min=0,max=130"` }'), "backend")
        for cfg, text in (("mysite/settings/prod.py", "DEBUG = False"), ("next.config.js", "module.exports = {}"),
                          ("src/environments/environment.prod.ts", "export const environment = { apiKey: 'x' }"),
                          ("app/config.py", "class Settings(BaseSettings):\n    db_password: str"),
                          ("app/core.py", "SECRET_KEY = 'abc'\nurlpatterns = []")):
            self.assertIsNone(c(cfg, text), cfg)
        self.assertEqual(c("src/ZoneList.js", "export function ZoneList() { return <Table rows={rows}/> }"), "frontend")
        self.assertEqual(c("Views/Zones/Index.cshtml", "<form asp-action='Create'>"), "frontend")
        self.assertEqual(c("resources/views/zones.blade.php", "<input name='speed'>"), "frontend")

    def test_schema_files_in_a_frontend_repo_stay_frontend(self):
        repo = git_repo(self.dir / "ui", {"server.js": "app.get('/health', (req, res) => res.send('ok'))",
                                          "src/schemas/zone.ts": "import { z } from 'zod'; export const s = z.object({})",
                                          "src/ZoneForm.tsx": "export const F = () => <form/>"})
        s = self.scan(repo)
        frontend = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]["files"]
        self.assertIn("src/ZoneForm.tsx", frontend)
        self.assertIn("src/schemas/zone.ts", frontend)  # the form's schema: its limits belong on the page

    # ---- review round 4 ----
    def test_a_dirty_router_does_not_remap_everything_every_time(self):
        files = {f"src/C{i}.jsx": f"<b data-testid='c{i}'/>" for i in range(3)}
        files["src/routes.js"] = "export const routes = [{ path: '/zones' }, { path: '/map' }]"
        repo = git_repo(self.dir / "ui", files)
        self.scan(repo)
        self.write_map("frontend", [{"page": p, "kind": "route", "label": p, "value": "X", "file": "src/routes.js:1"}
                                    for p in ("/zones", "/map")])
        self.ingest_all()
        (repo / "src/routes.js").write_text("export const routes = [{ path: '/zones' }, { path: '/map' }, { path: '/debug' }]")
        self.assertEqual(self.scan(repo, stamp="s2")["areas"]["frontend"]["files"], 4)  # the router changed: full
        self.write_map("frontend", [{"page": p, "kind": "route", "label": p, "value": "X", "file": "src/routes.js:1"}
                                    for p in ("/zones", "/map", "/debug")])
        self.ingest_all()
        self.assertEqual(self.scan(repo, stamp="s3")["areas"]["frontend"]["files"], 0)  # still dirty, but mapped as is

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

    def test_pending_maps_of_an_unscanned_folder_dont_block_orphan_cleanup(self):
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

    def test_removed_orphan_route_bookkeeping_is_deleted_once(self):
        keys = ("codemap_routes_at_scan", "codemap_lost", "codemap_scan_repos", "codemap_reread:frontend:/r",
                "codemap_pending:tests:/r")
        keep = ("codemap_hashes:frontend:/r", "codemap_commit:frontend:/r", "build")
        with self.con:
            for k in keys + keep:
                core.set_meta(self.con, k, "x")
        self.con.close()
        self.con = core.connect()
        self.assertEqual(sorted(r[0] for r in self.con.execute("SELECT key FROM meta")), sorted(keep))

    def test_classification_round_four(self):
        c = scan_mod.classify_source
        for rel, text in (("config/routes.rb", "Rails.application.routes.draw do\n resources :zones\nend"),
                          ("app/models/zone.rb", "class Zone < ApplicationRecord\n validates :name, presence: true\nend"),
                          ("routes/api.php", "Route::post('/zones', [ZoneController::class, 'store']);"),
                          ("zones/models.py", "class Zone(models.Model):\n    name = models.CharField(max_length=80)"),
                          ("app/user_settings.py", "@app.post('/settings')\ndef save(): pass")):
            self.assertEqual(c(rel, text), "backend", rel)
        for rel, text in (("tests/pages/zone_page.py", "from playwright.sync_api import Page\nclass ZonePage:\n    x = self.page.get_by_role('button')"),
                          ("src/pages/ZonePage.ts", "export class ZonePage { save = () => this.page.getByRole('button') }"),
                          ("UiTests/Pages/ZonePage.cs", "using Microsoft.Playwright;\npublic class ZonePage { ILocator S => _page.GetByRole(AriaRole.Button); }"),
                          ("my-e2e-tests/zones.spec.ts", "test('x', async ({page}) => { await page.goto('/z'); await page.getByRole('button').click() })")):
            self.assertEqual(c(rel, text), "tests", rel)
        self.assertNotEqual(c("src/api/docs.ts", "export const swaggerUrl = '/swagger.json'"), "backend")
        self.assertEqual(c("pages/settings.vue", "<template><div/></template><script>export default { runtimeConfig: 1 }</script>"), "frontend")
        self.assertIsNone(c("myapp/settings.py", "INSTALLED_APPS = []\nDATABASES = {}"))
        self.assertEqual(c("src/SettingsPage.tsx", "export class SettingsPage extends React.Component { render() { return <div data-testid='s'/> } }"), "frontend")
        self.assertEqual(codemap._api_ref("fetch('/api/zones/' + id + '/detours')"), ("", "api/zones/:id/detours"))

    # ---- review round 5 ----
    def test_a_remap_chunk_that_never_lands_is_listed_again_on_the_next_scan(self):
        files = {f"src/C{i}.jsx": f"<b data-testid='c{i}'/>" for i in range(3)}
        files["src/routes.js"] = "export const routes = [{ path: '/zones', c: A }, { path: '/map', c: B }]"
        repo = git_repo(self.dir / "ui", files)
        s = scan_mod.codemap_scan(self.con, [str(repo)], self.out, False, "s1", chunk_files=10, chunk_tokens=10**9)
        route_rows = lambda f: [{"page": p, "kind": "route", "label": p, "value": "X", "file": f + ":1"}
                                for p in ("/zones", "/map")] if f.endswith("routes.js") else []
        self.map_chunks(s, lambda f: route_rows(f) or [{"page": "/zones", "kind": "locator", "label": f, "value": "#" + f,
                                                        "file": f + ":1"}])
        self.ingest_all()
        (repo / "src/routes.js").write_text("export const routes = [{ path: '/zones', c: B }, { path: '/map', c: A }]")
        git(repo, "commit", "-qam", "swap")  # a router change: frontend is mapped in full, in two chunks
        s2 = scan_mod.codemap_scan(self.con, [str(repo)], self.out, False, "s2", chunk_files=2, chunk_tokens=10**9)
        mappers = self.map_chunks(s2, lambda f: route_rows(f) or [{"page": "/map", "kind": "locator", "label": f,
                                                                   "value": "#" + f, "file": f + ":1"}])
        self.assertEqual(len(mappers), 2)
        Path(mappers[0]["out"]).unlink()  # the components' chunk never landed; the router's did
        self.ingest_all()
        s3 = scan_mod.codemap_scan(self.con, [str(repo)], self.out, False, "s3", chunk_files=10, chunk_tokens=10**9)
        listed = json.loads(Path(s3["areas"]["frontend"]["mappers"][0]["scan_file"]).read_text())["repos"][0]["files"]
        self.assertTrue({"src/C0.jsx", "src/C1.jsx"} <= set(listed), listed)
        self.assertNotIn("src/routes.js", listed)  # the chunk that landed is settled

    def test_classification_round_five(self):
        c = scan_mod.classify_source
        form = "export const F = () => <form data-testid='f' onSubmit={() => navigate('/x')}/>"
        for rel, folder in (("src/features/inspections/InspectionForm.tsx", "ui"), ("pages/special-events/index.tsx", "ui"),
                            ("src/components/LatestIncidents.tsx", "ui"), ("src/pages/Zones.tsx", "signal-automation-ui"),
                            ("pages/zones/index.vue", "bridge-inspection-ui"), ("src/pages/AspectRatio.tsx", "speedtest-portal")):
            self.assertEqual(c(rel, form, folder), "frontend", rel)
        self.assertEqual(c("src/app/inspections/inspection-list.component.ts",
                           "@Component({}) export class InspectionListComponent { x = 'data-testid' }", "ui"), "frontend")
        self.assertEqual(c("src/pages/inspections/index.js", "export default function I() { return <div data-testid='i'/> }",
                           "ui"), "frontend")
        self.assertNotEqual(c("src/Behaviour.ts", "// how zones behave\nexport const x = page.locator('#a'); goto('/z')"), "tests")
        for rel, folder in (("ui-tests/pages/Zone.ts", "e2e"), ("UiTests/Pages/Zone.ts", "app"), ("x/e2e/zone.ts", "app"),
                            ("qa-automation/pages/zone.ts", "app"), ("src/__tests__/pages/z.ts", "app")):
            self.assertEqual(c(rel, "export class ZonePage { s = page.getByRole('button'); open() { page.goto('/z') } }",
                               folder), "tests", rel)
        # component objects, *PageObject classes, Java po/, behave steps
        self.assertEqual(c("tests/components/ZoneDialog.ts", "import { test } from '@playwright/test'\n"
                           "export class ZoneDialog { save = this.page.getByRole('button') }"), "tests")
        self.assertEqual(c("tests/models/LoginPageObject.ts", "export class LoginPageObject { u = this.page.getByLabel('User') }"), "tests")
        self.assertEqual(c("src/test/java/x/po/LoginForm.java", "import org.openqa.selenium.WebElement;\n"
                           "public class LoginForm { @FindBy(id = \"user\") WebElement user; }"), "tests")
        self.assertEqual(c("features/steps/zone_steps.py", "from behave import when\n@when('save')\n"
                           "def s(ctx): ctx.page.get_by_role('button').click()"), "tests")
        # repository calls in a backend file are not page-object calls
        self.assertEqual(c("app/routers/zones.py", "from fastapi import APIRouter\nclass ZonePage(BaseModel): items: list\n"
                           "@router.get('/zones')\ndef ls(db): return crud.zone.get_by_id(db, 1)"), "backend")
        self.assertEqual(c("Controllers/ZonesController.cs", "[ApiController]\npublic class ZonePage { }\n"
                           "[HttpGet] public Task<Zone> Get(int id) => _repo.GetByIdAsync(id);"), "backend")
        # TS generics are not JSX; backend services under app/ are not pages
        self.assertNotEqual(c("app/services/zoneService.ts", "export default class ZoneService { find(): Promise<Zone> {} }"),
                            "frontend")
        # config modules with credentials are never read
        self.assertIsNone(c("config/production.js", "module.exports = { db: 'mongodb://admin:S3cr3tPass@db:27017/x', "
                            "apiPrefix: '/api/v1' }"))
        self.assertIsNone(c("server/config/index.js", "module.exports = { jwt: { secret: 'abcdef123456' }, path: '/var/data' }"))
        self.assertIsNone(c("src/config.ts", "export default { apiUrl: '/api/', required: true }"))
        self.assertIsNone(c("src/config/mail.ts", "export const mail = { auth: { user: 'n', pass: 'MailPass123' }, required: true }"))
        self.assertIsNone(c("src/db.ts", "export const pool = new Pool({ connectionString: 'postgres://app:pw@h/db' })"))
        self.assertEqual(c("src/config/routes.ts", "export default [{ path: '/zones', component: './Zones' }]"), "frontend")
        # typed client calls
        svc = "export const create = (z: Zone) => api.post<Zone>('/zones', z); export const ls = () => api.get<Zone[]>('/zones')"
        self.assertEqual(c("src/services/zoneService.ts", svc), "frontend")
        self.assertEqual(codemap._api_ref("api.get<Zone[]>('/zones')"), ("GET", "zones"))
        self.assertEqual(codemap._api_ref("http.post<Zone>('/api/zones', z)"), ("POST", "api/zones"))

    def test_typed_backend_schemas_and_monorepo_client_schemas(self):
        api = git_repo(self.dir / "api", {"src/routes/zones.ts": "router.post('/api/zones', validate(zoneSchema), h)",
                                          "src/schemas/zone.ts": "import { z } from 'zod'; export const zoneSchema: "
                                                                 "z.ZodType<Partial<ZoneInput>> = z.object({ speed: z.number() })"})
        s = self.scan(api)
        self.assertEqual((s["areas"]["backend"]["files"], s["areas"]["frontend"]["files"]), (2, 0))
        mono = git_repo(self.dir / "mono", {"server/index.js": "app.post('/api/zones', (req, res) => res.json(req.body))",
                                            "server/validation/zone.js": "const Joi = require('joi'); module.exports = "
                                                                         "Joi.object({ speed: Joi.number().max(130) })",
                                            "client/src/ZoneForm.jsx": "export const F = () => <form/>",
                                            "client/src/schemas/zone.ts": "import { z } from 'zod'; export const s = "
                                                                          "z.object({ speed: z.number().max(130) })"})
        self.scan(mono, stamp="s2")
        fe = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]["files"]
        be = json.loads((self.out / "scan-backend.json").read_text())["repos"][0]["files"]
        self.assertIn("client/src/schemas/zone.ts", fe)
        self.assertIn("server/validation/zone.js", be)

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

    def test_new_router_file_remaps_frontend_in_full(self):  # audit 3 r5
        repo = self.dir / "ui"
        (repo / "src").mkdir(parents=True)
        (repo / "src/routes.js").write_text("export default [{ path: '/a', component: A }, { path: '/b', component: A }];\n")
        (repo / "src/A.jsx").write_text('export const A = () => <input data-testid="x"/>;\n')
        self.scan(repo)
        self.write_map("frontend", [{"page": p, "kind": "route", "label": "A", "value": "A", "file": "src/routes.js:1"}
                                    for p in ("/a", "/b")]
                       + [{"page": p, "kind": "locator", "label": "x", "value": "getByTestId('x')", "file": "src/A.jsx:1"}
                          for p in ("/a", "/b")])
        self.ingest_all()
        # a second router file sends new pages to the unchanged component: A.jsx must be read again to land on them
        (repo / "src/admin.routes.js").write_text("export default [{ path: '/c', component: A }, { path: '/d', component: A }];\n")
        self.scan(repo, stamp="s2")
        entry = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]
        self.assertTrue(entry["full"])
        self.assertIn("src/A.jsx", entry["files"])
        self.scan(repo, stamp="s3")  # nothing landed: the same full listing again, not an incremental one that forgot
        self.assertIn("src/A.jsx", json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]["files"])

    def test_fresh_review_findings(self):  # audit 3: independent review after round 50
        repo = git_repo(self.dir / "ui", {"src/Login.jsx": 'export const L = () => <input data-testid="pw"/>;\n'})
        (repo / "src").joinpath(b"bad\xffname.tsx".decode("utf-8", "surrogateescape")).write_text("export const X = () => <b/>;\n")
        self.scan(repo)  # was: UnicodeDecodeError on the file name
        rows = [{"page": "/login", "kind": "route", "label": "Login", "value": "L", "file": "src/Login.jsx:1"},
                {"page": "/login", "kind": "api", "label": "reset password", "value": "POST /api/auth/reset-password", "file": "src/Login.jsx:1"},
                {"page": "/login", "kind": "rule", "label": "password", "value": "string, min 8, max 64, required", "file": "src/Login.jsx:1"},
                {"page": "/login", "kind": "rule", "label": "new password", "value": "minLength=8", "file": "src/Login.jsx:1"},
                {"page": "/login", "kind": "rule", "label": "jwt.secret", "value": "abcDEF123456ghiJKL7890", "file": "src/Login.jsx:1"}]
        self.write_map("frontend", rows)
        self.ingest_all()
        out = codemap.code(self.con, "/login")
        for kept in ("POST /api/auth/reset-password", "string, min 8, max 64, required", "minLength=8"):
            self.assertIn(kept, out)
        self.assertNotIn("abcDEF123456", out)
        self.assertIn("reset-password", memory.search(self.con, "/api/auth/reset-password"))

    def test_submodule_and_ignored_folder_are_scanned(self):  # audit 3: second independent pass
        sub = git_repo(self.dir / "web", {"src/A.jsx": "export const A = () => <b/>;\n"})
        top = self.dir / "top"
        top.mkdir()
        git(top, "init", "-q")
        git(top, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "web")
        git(top, "commit", "-qm", "sub")
        self.assertEqual(self.scan(top)["areas"]["frontend"]["files"], 1)
        outer = git_repo(self.dir / "outer", {".gitignore": "gen/\n", "gen/app/B.jsx": "export const B = () => <i/>;\n"})
        self.assertEqual(self.scan(outer / "gen" / "app", stamp="s2")["areas"]["frontend"]["files"], 1)
        rows = [{"page": "/login", "kind": "rule", "label": "password", "value": "z.string().min(8).max(64)", "file": "B.jsx:1"},
                {"page": "/login", "kind": "rule", "label": "token", "value": "StringLength(32)", "file": "B.jsx:1"},
                {"page": "/login", "kind": "route", "label": "Login", "value": "B", "file": "B.jsx:1"}]
        self.write_map("frontend", rows)
        self.ingest_all()
        out = codemap.code(self.con, "/login")
        self.assertIn("z.string().min(8).max(64)", out)
        self.assertIn("StringLength(32)", out)

    def test_uninitialised_submodule_is_skipped(self):  # audit 3: third pass (was: RecursionError)
        sub = git_repo(self.dir / "web", {"src/A.jsx": "export const A = () => <b/>;\n"})
        top = git_repo(self.dir / "top", {"src/B.jsx": "export const B = () => <i/>;\n"})
        git(top, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "web")
        git(top, "commit", "-qm", "sub")
        clone = self.dir / "clone"
        git(self.dir, "clone", "-q", str(top), str(clone))  # without --recurse-submodules: web/ is empty
        self.assertEqual(self.scan(clone)["areas"]["frontend"]["files"], 1)

    def test_fourth_pass_findings(self):  # audit 3: fourth independent pass
        lib = git_repo(self.dir / "lib", {"src/A.jsx": "export const A = () => <b/>;\n"})
        top = git_repo(self.dir / "top", {"src/Login.jsx": "export const L = () => <i/>;\n"})
        git(top, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "vendor/lib")
        git(top, "commit", "-qm", "sub")
        self.scan(top)
        files = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]["files"]
        self.assertEqual(files, ["src/Login.jsx"])  # third-party code under vendor/ stays out
        rule = lambda label, value: {"page": "/login", "kind": "rule", "label": label, "value": value, "file": "src/Login.jsx:1"}
        rules = [("token", "Validators.required"), ("password", "passwordStrengthValidator"), ("token", "PasswordPolicy.STRONG"),
                 ("password", "string|min:8|max:64"), ("otp token", "digits:6"), ("reset token", "uuid|size:36"),
                 ("access token", "JWT,HS256,exp:15m"), ("session_id", "regex:/^[a-f0-9]{32}$/"), ("password", "confirmed")]
        self.write_map("frontend", [{"page": "/login", "kind": "route", "label": "Login", "value": "L", "file": "src/Login.jsx:1"}]
                       + [rule(k, v) for k, v in rules])
        self.ingest_all()
        out = codemap.code(self.con, "/login")
        for _, value in rules:  # every rule kept, the second `password` row included
            self.assertIn(value, out)
        self.scan(top, stamp="s9", full=True)  # sixth pass: a key under a label that ends in "key"
        self.write_map("frontend", [rule("Mapbox API key", "9f8a7b6c5d4e3f2a1b0c9f8a7b6c5d4e")])
        self.ingest_all()
        self.assertNotIn("9f8a7b6c", codemap.code(self.con, "/login"))

    def test_out_option_with_equals_and_repos_listing(self):  # audit 3 r1
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.run_cmd(self.con, "codemap-scan", [f"--out={self.out}", str(FIX)]), 0)
        self.assertEqual(json.loads(buf.getvalue())["skipped"], [])
        self.assertTrue((self.out / "scan-frontend.json").exists())
        self.assertIn(os.path.realpath(FIX), codemap.code(self.con, "--repos"))  # bookkeeping only: no map has landed yet


if __name__ == "__main__":
    unittest.main()
