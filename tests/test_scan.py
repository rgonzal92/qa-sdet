"""Code-map scan (qa_sdet.scan): which source files go to which mapper, what a re-scan reads again."""

import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from support import FIX, CodeMapCase, git, git_repo

from qa_sdet import cli, codemap, core
from qa_sdet import scan as scan_mod


class Classify(unittest.TestCase):
    """scan.classify_source: frontend, backend, tests or nothing, from a file's path and text."""

    def test_pages_components_and_clients_are_frontend(self):
        c = scan_mod.classify_source
        form = "export const F = () => <form data-testid='f' onSubmit={() => navigate('/x')}/>"
        svc = "export const create = (z: Zone) => api.post<Zone>('/zones', z); export const ls = () => api.get<Zone[]>('/zones')"
        self.assertEqual(c("src/app/app.routes.ts", "export const routes: Routes = [{ path: 'zones' }]"), "frontend")
        self.assertEqual(
            c("src/auth/PasswordReset.tsx", "export const P = () => <form/>"), "frontend"
        )  # a page, not a secret
        self.assertEqual(c("src/zones/ZoneTable.vue", "<template><table/></template>"), "frontend")
        self.assertEqual(c("app/zones/page.tsx", "export default function Page() { return <Zones/> }"), "frontend")
        self.assertEqual(c("src/pages/zones/[id].ts", "export const load = () => ({})"), "frontend")
        self.assertEqual(c("src/app/zones/zones.page.ts", "@Component({ selector: 'app-zones' })"), "frontend")  # Ionic
        self.assertEqual(
            c("src/api/zones.ts", "export const save = (z) => api.post('/zones', z)"), "frontend"
        )  # a client
        self.assertEqual(c("src/routes/zones/+page.ts", "export const load = async () => ({})"), "frontend")
        self.assertEqual(
            c("src/ApiDocs.tsx", "export const D = () => <SwaggerUI url='/openapi.json'/> // app.get('/x')"), "frontend"
        )
        self.assertEqual(c("src/ZoneList.js", "export function ZoneList() { return <Table rows={rows}/> }"), "frontend")
        self.assertEqual(c("Views/Zones/Index.cshtml", "<form asp-action='Create'>"), "frontend")
        self.assertEqual(c("resources/views/zones.blade.php", "<input name='speed'>"), "frontend")
        self.assertEqual(
            c("pages/settings.vue", "<template><div/></template><script>export default { runtimeConfig: 1 }</script>"),
            "frontend",
        )
        self.assertEqual(
            c(
                "src/SettingsPage.tsx",
                "export class SettingsPage extends React.Component { render() { return <div data-testid='s'/> } }",
            ),
            "frontend",
        )
        for rel, folder in (
            ("src/features/inspections/InspectionForm.tsx", "ui"),
            ("pages/special-events/index.tsx", "ui"),
            ("src/components/LatestIncidents.tsx", "ui"),
            ("src/pages/Zones.tsx", "signal-automation-ui"),
            ("pages/zones/index.vue", "bridge-inspection-ui"),
            ("src/pages/AspectRatio.tsx", "speedtest-portal"),
        ):
            self.assertEqual(c(rel, form, folder), "frontend", rel)
        self.assertEqual(
            c(
                "src/app/inspections/inspection-list.component.ts",
                "@Component({}) export class InspectionListComponent { x = 'data-testid' }",
                "ui",
            ),
            "frontend",
        )
        self.assertEqual(
            c("src/pages/inspections/index.js", "export default function I() { return <div data-testid='i'/> }", "ui"),
            "frontend",
        )
        self.assertEqual(
            c("src/config/routes.ts", "export default [{ path: '/zones', component: './Zones' }]"), "frontend"
        )
        self.assertEqual(c("src/services/zoneService.ts", svc), "frontend")

    def test_routes_controllers_models_and_validators_are_backend(self):
        c = scan_mod.classify_source
        self.assertEqual(c("src/Main.java", '@GetMapping("/api/zones") public List<Zone> all()'), "backend")
        self.assertEqual(c("api/openapi.yaml", "openapi: 3.0.0\npaths:"), "backend")
        self.assertEqual(c("Program.cs", 'app.MapGet("/api/zones", () => db.Zones);'), "backend")
        self.assertEqual(c("main.go", 'r.GET("/api/zones", listZones)'), "backend")
        self.assertEqual(c("app/zones.py", "@zones.route('/api/zones', methods=['POST'])"), "backend")
        self.assertEqual(c("Dto/ZoneDto.cs", "[Required]\n[Range(0, 130)] public int SpeedLimit"), "backend")
        self.assertEqual(c("validators/zone.js", "body('speed').isInt({ min: 0, max: 130 })"), "backend")
        self.assertEqual(c("src/zone.dto.ts", "@IsEmail() email; @MaxLength(80) name;"), "backend")
        self.assertEqual(
            c("src/integration/ZoneController.java", '@GetMapping("/api/zones") List<Zone> all()'), "backend"
        )
        self.assertEqual(
            c("app/controllers/zoneController.js", "exports.create = async (req, res) => { if (!req.body.name) }"),
            "backend",
        )
        self.assertEqual(
            c("app/models/zone.js", "const Zone = new Schema({ name: { type: String, required: true } })"), "backend"
        )
        self.assertEqual(c("api/openapi.yaml", "openapi: 3.0.1\npaths: {}"), "backend")
        self.assertEqual(c("app/api/zones/route.ts", "export async function POST(req: Request) { }"), "backend")
        self.assertEqual(c("src/routes/api/zones/+server.ts", "export const GET = async () => json([])"), "backend")
        self.assertEqual(c("server/api/zones.post.ts", "export default defineEventHandler(async (e) => {})"), "backend")
        self.assertEqual(
            c("pages/api/zones.ts", "export default function handler(req: NextApiRequest, res) {}"), "backend"
        )
        self.assertEqual(c("routes.go", 'r.Get("/api/zones", list)'), "backend")
        self.assertEqual(c("models/zone.go", 'type Zone struct { Speed int `validate:"min=0,max=130"` }'), "backend")
        for rel, text in (
            ("config/routes.rb", "Rails.application.routes.draw do\n resources :zones\nend"),
            ("app/models/zone.rb", "class Zone < ApplicationRecord\n validates :name, presence: true\nend"),
            ("routes/api.php", "Route::post('/zones', [ZoneController::class, 'store']);"),
            ("zones/models.py", "class Zone(models.Model):\n    name = models.CharField(max_length=80)"),
            ("app/user_settings.py", "@app.post('/settings')\ndef save(): pass"),
        ):
            self.assertEqual(c(rel, text), "backend", rel)
        self.assertEqual(
            c(
                "app/routers/zones.py",
                "from fastapi import APIRouter\nclass ZonePage(BaseModel): items: list\n"
                "@router.get('/zones')\ndef ls(db): return crud.zone.get_by_id(db, 1)",
            ),
            "backend",
        )
        self.assertEqual(
            c(
                "Controllers/ZonesController.cs",
                "[ApiController]\npublic class ZonePage { }\n"
                "[HttpGet] public Task<Zone> Get(int id) => _repo.GetByIdAsync(id);",
            ),
            "backend",
        )

    def test_specs_and_page_objects_are_tests(self):
        c = scan_mod.classify_source
        self.assertEqual(c("e2e/zones.spec.ts", "await page.getByRole('button').click()"), "tests")
        self.assertEqual(
            c("src/test/ZonePage.java", 'public class ZonePage { @FindBy(id = "speed") WebElement speed; }'), "tests"
        )  # a Selenium page object
        self.assertEqual(
            c("pages/Login.ts", "import { test } from '@playwright/test'; page.locator('#u')", "e2e"), "tests"
        )
        self.assertEqual(
            c("tests/test_zones.py", "def test_zones(page):\n    page.goto('/zones')\n    page.get_by_role('button')"),
            "tests",
        )
        self.assertEqual(
            c("Tests/ZonePage.cs", "public class ZonePage { ILocator Save => _page.GetByRole(AriaRole.Button); }"),
            "tests",
        )
        for rel, text in (
            (
                "tests/pages/zone_page.py",
                "from playwright.sync_api import Page\nclass ZonePage:\n    x = self.page.get_by_role('button')",
            ),
            ("src/pages/ZonePage.ts", "export class ZonePage { save = () => this.page.getByRole('button') }"),
            (
                "UiTests/Pages/ZonePage.cs",
                "using Microsoft.Playwright;\npublic class ZonePage { ILocator S => _page.GetByRole(AriaRole.Button); }",
            ),
            (
                "my-e2e-tests/zones.spec.ts",
                "test('x', async ({page}) => { await page.goto('/z'); await page.getByRole('button').click() })",
            ),
        ):
            self.assertEqual(c(rel, text), "tests", rel)
        for rel, folder in (
            ("ui-tests/pages/Zone.ts", "e2e"),
            ("UiTests/Pages/Zone.ts", "app"),
            ("x/e2e/zone.ts", "app"),
            ("qa-automation/pages/zone.ts", "app"),
            ("src/__tests__/pages/z.ts", "app"),
        ):
            self.assertEqual(
                c(rel, "export class ZonePage { s = page.getByRole('button'); open() { page.goto('/z') } }", folder),
                "tests",
                rel,
            )
        self.assertEqual(
            c(
                "tests/components/ZoneDialog.ts",
                "import { test } from '@playwright/test'\n"
                "export class ZoneDialog { save = this.page.getByRole('button') }",
            ),
            "tests",
        )
        self.assertEqual(
            c("tests/models/LoginPageObject.ts", "export class LoginPageObject { u = this.page.getByLabel('User') }"),
            "tests",
        )
        self.assertEqual(
            c(
                "src/test/java/x/po/LoginForm.java",
                "import org.openqa.selenium.WebElement;\n"
                'public class LoginForm { @FindBy(id = "user") WebElement user; }',
            ),
            "tests",
        )
        self.assertEqual(
            c(
                "features/steps/zone_steps.py",
                "from behave import when\n@when('save')\ndef s(ctx): ctx.page.get_by_role('button').click()",
            ),
            "tests",
        )

    def test_secrets_configs_unit_tests_stories_and_lockfiles_are_never_listed(self):
        c = scan_mod.classify_source
        self.assertIsNone(c("src/util.test.ts", "expect(add(1, 2)).toBe(3)"))  # a unit test: nothing about pages
        for secret in (
            "config/secrets.json",
            ".env.local",
            "certs/server.pem",
            "appsettings.Production.json",
            "credentials.yaml",
        ):
            self.assertIsNone(c(secret, "data-testid password=hunter2"), secret)
        self.assertIsNone(c("service/ZoneService.java", 'throw new IllegalArgumentException("name is required");'))
        self.assertIsNone(c("package-lock.json", "{}"))
        self.assertIsNone(c("src/logo.svg", "<svg aria-label='x'/>"))
        self.assertIsNone(c("routes/index.js", "module.exports = require('./zones')"))
        self.assertIsNone(c("src/Button.stories.tsx", "export default { title: 'Button' }"))
        self.assertIsNone(c("src/__mocks__/api.ts", "export const get = () => fetch('/api/x')"))
        self.assertIsNone(c("src/Form.test.tsx", "render(<Form/>); screen.getByRole('button')"))  # RTL unit test
        self.assertIsNone(c("docker-compose.yml", "services:\n  api:\n    environment: [DB_PASSWORD=x]  # swagger ui"))
        self.assertIsNone(c("application.yml", "openapi: 3.0.0\nspring.datasource.password: x"))
        self.assertIsNone(c("api/notes.yaml", "# about openapi\nname: x"))
        for cfg, text in (
            ("mysite/settings/prod.py", "DEBUG = False"),
            ("next.config.js", "module.exports = {}"),
            ("src/environments/environment.prod.ts", "export const environment = { apiKey: 'x' }"),
            ("app/config.py", "class Settings(BaseSettings):\n    db_password: str"),
            ("app/core.py", "SECRET_KEY = 'abc'\nurlpatterns = []"),
        ):
            self.assertIsNone(c(cfg, text), cfg)
        self.assertIsNone(c("myapp/settings.py", "INSTALLED_APPS = []\nDATABASES = {}"))
        self.assertIsNone(
            c(
                "config/production.js",
                "module.exports = { db: 'mongodb://admin:S3cr3tPass@db:27017/x', apiPrefix: '/api/v1' }",
            )
        )
        self.assertIsNone(
            c("server/config/index.js", "module.exports = { jwt: { secret: 'abcdef123456' }, path: '/var/data' }")
        )
        self.assertIsNone(c("src/config.ts", "export default { apiUrl: '/api/', required: true }"))
        self.assertIsNone(
            c("src/config/mail.ts", "export const mail = { auth: { user: 'n', pass: 'MailPass123' }, required: true }")
        )
        self.assertIsNone(
            c("src/db.ts", "export const pool = new Pool({ connectionString: 'postgres://app:pw@h/db' })")
        )

    def test_look_alikes_are_not_misread(self):
        c = scan_mod.classify_source
        self.assertNotEqual(c("src/api/docs.ts", "export const swaggerUrl = '/swagger.json'"), "backend")
        self.assertNotEqual(
            c("src/Behaviour.ts", "// how zones behave\nexport const x = page.locator('#a'); goto('/z')"), "tests"
        )
        self.assertNotEqual(
            c("app/services/zoneService.ts", "export default class ZoneService { find(): Promise<Zone> {} }"),
            "frontend",
        )

    def test_secret_stores_in_code_files_are_never_listed(self):
        for name in ("secrets.py", "app/secrets.rb", "Api/Secrets.cs", "credentials.py", "wp-config.php"):
            self.assertIsNone(
                scan_mod.classify_source(name, "Route::get('/x', fn() => 1);\n@app.route('/x')\n[HttpGet]"), name
            )
        self.assertEqual(
            scan_mod.classify_source("Api/Credentials.cs", "public record Credentials([Required] string User);"),
            "backend",
        )  # a login DTO class, not a store

    def test_an_api_spec_with_menu_like_properties_is_not_a_menu(self):
        spec = json.dumps(
            {
                "openapi": "3.0.0",
                "paths": {"/api/settings": {}},
                "components": {"schemas": {"S": {"properties": {"module": {}, "key": {}, "nav": {}}}}},
            }
        )
        self.assertEqual(scan_mod.classify_source("api/openapi-config.json", spec), "backend")


class ClassifySpeed(unittest.TestCase):
    def test_api_call_pattern_stays_linear(self):  # was ~20 s for 160 KB of `api.get<`
        import time

        for text in ("api.get<" * 20000, "api" * 50000 + ".get"):
            t = time.monotonic()
            scan_mod.classify_source("src/a.ts", text)
            self.assertLess(time.monotonic() - t, 2, text[:20])
        self.assertEqual(
            scan_mod.classify_source("src/a.ts", "const zones = await apiClient.post<Zone[]>('/zones', body)"),
            scan_mod.classify_source("src/a.ts", "const zones = await fetch('/zones')"),
        )


class Scan(CodeMapCase):
    def test_root_and_home_are_refused_not_walked(self):
        with mock.patch.dict(os.environ, HOME=str(self.dir)):  # a real folder, whatever HOME the run has
            out = self.scan("/", "~")
        self.assertEqual(out["total_files"], 0)
        self.assertEqual(len(out["skipped"]), 2)
        self.assertTrue(all("too broad" in x for x in out["skipped"]), out["skipped"])

    def test_scan_classifies_areas_and_skips_secrets_and_generated(self):
        s = self.scan(FIX / "frontend", FIX / "backend", FIX / "e2e")
        files = {
            a: [f for r in json.loads((self.out / f"scan-{a}.json").read_text())["repos"] for f in r["files"]]
            for a in codemap.CODEMAP_AREAS
        }
        self.assertEqual(
            sorted(files["frontend"]),
            [
                "src/api/client.js",
                "src/components/IncidentMap.jsx",
                "src/components/IncidentsTable.jsx",
                "src/components/SpeedZoneForm.jsx",
                "src/routes.js",
            ],
        )
        self.assertEqual(sorted(files["backend"]), ["routes/admin.js", "routes/zones.js"])
        self.assertEqual(files["tests"], ["pages/SpeedZonePage.ts"])
        listed = json.dumps(files)
        for never in (".env", "node_modules", "dist", "bundle.min.js", "app.js", "secrets.json", "README.md"):
            self.assertNotIn(never, listed)
        self.assertEqual(s["total_files"], 8)
        self.assertFalse(s["incremental"])
        self.assertGreater(s["tokens"], 0)
        self.assertTrue(s["areas"]["tests"]["needs_mapper"])

    def test_incremental_rescan_reads_only_changed_files(self):
        repo = self.dir / "ui"
        shutil.copytree(FIX / "frontend" / "src", repo / "src")
        git(repo, "init", "-q")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "one")
        self.scan(repo)
        self.write_map(
            "frontend",
            [
                {
                    "page": "/form.html",
                    "kind": "route",
                    "label": "Speed zone",
                    "value": "SpeedZoneForm",
                    "file": "src/routes.js:4",
                },
                {
                    "page": "/index.html",
                    "kind": "locator",
                    "label": "Incidents table",
                    "value": "#t",
                    "file": "src/components/IncidentsTable.jsx:6",
                },
            ],
        )
        self.ingest_all()
        (repo / "src/components/SpeedZoneForm.jsx").write_text(
            "export const F = () => <input data-testid='speed' />;\n"
        )
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

    def test_incremental_handles_renames_unicode_ignored_and_new_candidates(self):
        repo = git_repo(
            self.dir / "ui",
            {
                "src/A.jsx": "<b data-testid='a'/>",
                "src/Zoné café.jsx": "<b/>",
                ".gitignore": "src/gen/\n",
                "src/gen/Gen.jsx": "<b data-testid='gen'/>",
            },
        )
        self.scan(repo)
        files = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]["files"]
        self.assertNotIn("src/gen/Gen.jsx", files)  # ignored: generated code
        self.assertIn("src/Zoné café.jsx", files)
        self.write_map(
            "frontend", [{"page": "/a", "kind": "locator", "label": "A", "value": "#a", "file": "src/A.jsx:1"}]
        )
        self.ingest_all()
        git(repo, "mv", "src/A.jsx", "src/B.jsx")
        (repo / "src/Zoné café.jsx").write_text("<b data-testid='z'/>")
        git(repo, "commit", "-qam", "rename + edit")
        self.scan(repo, stamp="s2")
        r = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]
        self.assertEqual(r["files"], ["src/B.jsx", "src/Zoné café.jsx"])
        self.assertEqual(r["deleted"], ["src/A.jsx"])

    def test_scan_stays_in_the_repo_reads_any_encoding_and_lists_a_folder_once(self):
        route = "router.get('/api/zones', (req, res) => res.json([]))\n"
        outside = self.dir / "outside"
        outside.mkdir()
        (outside / "zones.js").write_text("// not this repo\n" + route)
        repo = git_repo(self.dir / "api", {"server/zones.js": route, "src/app.js": route})
        shutil.rmtree(repo / "server")
        (repo / "server").symlink_to(outside)  # a tracked folder replaced by a link out of the repo
        (repo / "Zones.cs").write_bytes(
            '[ApiController]\n[Route("api/zones")]\npublic class Z { [HttpGet] void G(){} }'.encode("utf-16")
        )  # Visual Studio can save UTF-16 with a BOM
        (repo / "legacy.php").write_bytes("<?php Route::get('/caf\xe9', fn() => 1);".encode("latin-1"))
        self.scan(repo, str(repo) + "/")  # the same folder twice
        repos = json.loads((self.out / "scan-backend.json").read_text())["repos"]
        self.assertEqual([r["files"] for r in repos], [["Zones.cs", "legacy.php", "src/app.js"]])
        self.write_map("backend", [])
        self.ingest_all()
        (repo / "legacy.php").write_bytes("<?php Route::get('/caf\xf6', fn() => 1);".encode("latin-1"))  # é -> ö only
        self.scan(repo, stamp="s2")
        self.assertEqual(json.loads((self.out / "scan-backend.json").read_text())["repos"][0]["files"], ["legacy.php"])

    def test_edited_files_are_read_by_content_not_git_state(self):
        repo = git_repo(self.dir / "ui", {"src/A.jsx": "<b data-testid='a'/>"})
        self.scan(repo)
        self.write_map("frontend", [])
        self.ingest_all()
        (repo / "src/A.jsx").write_text("<b data-testid='a2'/>")  # edited, not committed
        self.assertEqual(self.scan(repo, stamp="s2")["areas"]["frontend"]["files"], 1)
        self.write_map(
            "frontend", [{"page": "/a", "kind": "locator", "label": "A", "value": "#a2", "file": "src/A.jsx:1"}]
        )
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

    def test_bad_commit_values_never_reach_git(self):
        repo = git_repo(self.dir / "ui", {"src/A.jsx": "<b data-testid='a'/>"})
        self.con.execute("INSERT INTO meta VALUES(?, ?)", (f"codemap_commit:frontend:{repo}", "--output=/tmp/x"))
        self.con.commit()
        s = self.scan(repo)
        self.assertFalse(s["incremental"])  # an invalid commit means a full scan, never a git option

    def test_a_new_scan_clears_old_maps(self):
        self.scan(FIX / "frontend")
        self.write_map("frontend", [])
        (self.out / "code-map-frontend.json.error").write_text("x")
        self.scan(FIX / "frontend", stamp="s2")
        self.assertFalse((self.out / "code-map-frontend.json").exists())
        self.assertFalse((self.out / "code-map-frontend.json.error").exists())

    def test_a_new_scan_keeps_files_it_did_not_write(self):  # --out <folder> held the user's own scan-*.json
        mine = ["scan-results.json", "code-map-notes.json.bak", "scan-frontend-notes.json"]
        self.out.mkdir(parents=True, exist_ok=True)
        for name in mine + ["scan-tests-2.json", "code-map-backend-3.json.ingested"]:
            (self.out / name).write_text("{}")
        self.scan(FIX / "frontend", stamp="s3")
        self.assertTrue(all((self.out / n).exists() for n in mine))
        self.assertFalse((self.out / "scan-tests-2.json").exists())
        self.assertFalse((self.out / "code-map-backend-3.json.ingested").exists())

    def test_backend_schema_files_go_to_the_backend(self):
        repo = git_repo(
            self.dir / "api",
            {
                "src/routes.ts": "router.post('/api/zones', validate(zoneSchema), h)",
                "src/schemas/zone.ts": "import { z } from 'zod'; export const zoneSchema = "
                "z.object({ speedLimit: z.number().max(130) })",
            },
        )
        s = self.scan(repo)
        self.assertEqual(s["areas"]["backend"]["files"], 2)
        self.assertEqual(s["areas"]["frontend"]["files"], 0)

    def test_schema_files_in_a_frontend_repo_stay_frontend(self):
        repo = git_repo(
            self.dir / "ui",
            {
                "server.js": "app.get('/health', (req, res) => res.send('ok'))",
                "src/schemas/zone.ts": "import { z } from 'zod'; export const s = z.object({})",
                "src/ZoneForm.tsx": "export const F = () => <form/>",
            },
        )
        self.scan(repo)
        frontend = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]["files"]
        self.assertIn("src/ZoneForm.tsx", frontend)
        self.assertIn("src/schemas/zone.ts", frontend)  # the form's schema: its limits belong on the page

    def test_typed_backend_schemas_and_monorepo_client_schemas(self):
        api = git_repo(
            self.dir / "api",
            {
                "src/routes/zones.ts": "router.post('/api/zones', validate(zoneSchema), h)",
                "src/schemas/zone.ts": "import { z } from 'zod'; export const zoneSchema: "
                "z.ZodType<Partial<ZoneInput>> = z.object({ speed: z.number() })",
            },
        )
        s = self.scan(api)
        self.assertEqual((s["areas"]["backend"]["files"], s["areas"]["frontend"]["files"]), (2, 0))
        mono = git_repo(
            self.dir / "mono",
            {
                "server/index.js": "app.post('/api/zones', (req, res) => res.json(req.body))",
                "server/validation/zone.js": "const Joi = require('joi'); module.exports = "
                "Joi.object({ speed: Joi.number().max(130) })",
                "client/src/ZoneForm.jsx": "export const F = () => <form/>",
                "client/src/schemas/zone.ts": "import { z } from 'zod'; export const s = "
                "z.object({ speed: z.number().max(130) })",
            },
        )
        self.scan(mono, stamp="s2")
        fe = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]["files"]
        be = json.loads((self.out / "scan-backend.json").read_text())["repos"][0]["files"]
        self.assertIn("client/src/schemas/zone.ts", fe)
        self.assertIn("server/validation/zone.js", be)

    def test_submodule_and_ignored_folder_are_scanned(self):
        sub = git_repo(self.dir / "web", {"src/A.jsx": "export const A = () => <b/>;\n"})
        top = self.dir / "top"
        top.mkdir()
        git(top, "init", "-q")
        git(top, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "web")
        git(top, "commit", "-qm", "sub")
        self.assertEqual(self.scan(top)["areas"]["frontend"]["files"], 1)
        outer = git_repo(
            self.dir / "outer", {".gitignore": "gen/\n", "gen/app/B.jsx": "export const B = () => <i/>;\n"}
        )
        self.assertEqual(self.scan(outer / "gen" / "app", stamp="s2")["areas"]["frontend"]["files"], 1)

    def test_uninitialised_submodule_is_skipped(self):
        sub = git_repo(self.dir / "web", {"src/A.jsx": "export const A = () => <b/>;\n"})
        top = git_repo(self.dir / "top", {"src/B.jsx": "export const B = () => <i/>;\n"})
        git(top, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "web")
        git(top, "commit", "-qm", "sub")
        clone = self.dir / "clone"
        git(self.dir, "clone", "-q", str(top), str(clone))  # without --recurse-submodules: web/ is empty
        self.assertEqual(self.scan(clone)["areas"]["frontend"]["files"], 1)

    def test_a_file_name_that_is_not_utf8_is_scanned(self):
        repo = git_repo(self.dir / "ui", {"src/Login.jsx": 'export const L = () => <input data-testid="pw"/>;\n'})
        (repo / "src").joinpath(b"bad\xffname.tsx".decode("utf-8", "surrogateescape")).write_text(
            "export const X = () => <b/>;\n"
        )
        self.scan(repo)  # used to raise UnicodeDecodeError
        files = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]["files"]
        self.assertEqual(files, ["src/Login.jsx"])
        # non-git folder too: non-UTF-8 names must stay out of scan files and shell menus (SQLite refuses surrogates)
        plain = self.dir / "plain-nonutf8"
        (plain / "src").mkdir(parents=True)
        (plain / "src/Good.jsx").write_text("export const G = () => <b/>;\n")
        (plain / "src").joinpath(b"bad\xff.tsx".decode("utf-8", "surrogateescape")).write_text(
            "export const X = () => <b/>;\n"
        )
        plain.joinpath(b"bad\xffconfig.json".decode("utf-8", "surrogateescape")).write_text(
            json.dumps({"nav": [{"key": "a", "title": "A", "module": {"id": "m", "url": "/u"}}]})
        )
        self.scan(plain, stamp="s2")
        files2 = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]["files"]
        self.assertEqual(files2, ["src/Good.jsx"])

    def test_third_party_code_under_vendor_stays_out(self):
        lib = git_repo(self.dir / "lib", {"src/A.jsx": "export const A = () => <b/>;\n"})
        top = git_repo(self.dir / "top", {"src/Login.jsx": "export const L = () => <i/>;\n"})
        git(top, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "vendor/lib")
        git(top, "commit", "-qm", "sub")
        self.scan(top)
        files = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]["files"]
        self.assertEqual(files, ["src/Login.jsx"])

    def test_out_option_with_equals_and_repos_listing(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.run_cmd(self.con, "codemap-scan", [f"--out={self.out}", str(FIX)]), 0)
        self.assertEqual(json.loads(buf.getvalue())["skipped"], [])
        self.assertTrue((self.out / "scan-frontend.json").exists())
        self.assertIn(
            os.path.realpath(FIX), codemap.code(self.con, "--repos")
        )  # bookkeeping only: no map has landed yet


class Remap(CodeMapCase):
    """A changed router re-maps the frontend in full; chunks that never land are listed again."""

    def test_a_page_file_edit_does_not_remap_everything(self):
        repo = git_repo(
            self.dir / "ui",
            {
                "src/pages/zones.jsx": "export default function Zones() { return <b/> }",
                "src/pages/home.jsx": "export default function Home() { return <b/> }",
            },
        )
        self.scan(repo)
        self.write_map(
            "frontend",
            [
                {"page": p, "kind": "route", "label": p, "value": "X", "file": f"src/pages{p}.jsx:1"}
                for p in ("/zones", "/home")
            ],
        )
        self.ingest_all()
        (repo / "src/pages/zones.jsx").write_text("export default function Zones() { return <i/> }")
        git(repo, "commit", "-qam", "edit page")
        self.scan(repo, stamp="s2")
        r = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]
        self.assertFalse(r["full"])
        self.assertEqual(r["files"], ["src/pages/zones.jsx"])

    def test_a_dirty_router_does_not_remap_everything_every_time(self):
        files = {f"src/C{i}.jsx": f"<b data-testid='c{i}'/>" for i in range(3)}
        files["src/routes.js"] = "export const routes = [{ path: '/zones' }, { path: '/map' }]"
        repo = git_repo(self.dir / "ui", files)
        self.scan(repo)
        self.write_map(
            "frontend",
            [
                {"page": p, "kind": "route", "label": p, "value": "X", "file": "src/routes.js:1"}
                for p in ("/zones", "/map")
            ],
        )
        self.ingest_all()
        (repo / "src/routes.js").write_text(
            "export const routes = [{ path: '/zones' }, { path: '/map' }, { path: '/debug' }]"
        )
        self.assertEqual(self.scan(repo, stamp="s2")["areas"]["frontend"]["files"], 4)  # the router changed: full
        self.write_map(
            "frontend",
            [
                {"page": p, "kind": "route", "label": p, "value": "X", "file": "src/routes.js:1"}
                for p in ("/zones", "/map", "/debug")
            ],
        )
        self.ingest_all()
        self.assertEqual(self.scan(repo, stamp="s3")["areas"]["frontend"]["files"], 0)  # still dirty, but mapped as is

    def test_new_router_file_remaps_frontend_in_full(self):
        repo = self.dir / "ui"
        (repo / "src").mkdir(parents=True)
        (repo / "src/routes.js").write_text(
            "export default [{ path: '/a', component: A }, { path: '/b', component: A }];\n"
        )
        (repo / "src/A.jsx").write_text('export const A = () => <input data-testid="x"/>;\n')
        self.scan(repo)
        self.write_map(
            "frontend",
            [{"page": p, "kind": "route", "label": "A", "value": "A", "file": "src/routes.js:1"} for p in ("/a", "/b")]
            + [
                {"page": p, "kind": "locator", "label": "x", "value": "getByTestId('x')", "file": "src/A.jsx:1"}
                for p in ("/a", "/b")
            ],
        )
        self.ingest_all()
        # a second router file sends new pages to the unchanged component: A.jsx must be read again to land on them
        (repo / "src/admin.routes.js").write_text(
            "export default [{ path: '/c', component: A }, { path: '/d', component: A }];\n"
        )
        self.scan(repo, stamp="s2")
        entry = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]
        self.assertTrue(entry["full"])
        self.assertIn("src/A.jsx", entry["files"])
        self.scan(repo, stamp="s3")  # nothing landed: the same full listing again, not an incremental one that forgot
        self.assertIn("src/A.jsx", json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]["files"])

    def test_route_rename_moves_component_rows(self):
        repo = git_repo(
            self.dir / "ui",
            {
                "src/routes.js": "export const routes = [{ path: '/zones' }, { path: '/map' }]",
                "src/Zones.jsx": "<input data-testid='speed'/>",
            },
        )
        self.scan(repo)
        self.write_map(
            "frontend",
            [
                {
                    "page": "/zones",
                    "kind": "route",
                    "label": "Speed zones",
                    "value": "Zones",
                    "file": "src/routes.js:1",
                },
                {"page": "/map", "kind": "route", "label": "Map", "value": "Map", "file": "src/routes.js:1"},
                {
                    "page": "/zones",
                    "kind": "locator",
                    "label": "Speed",
                    "value": "getByTestId('speed')",
                    "file": "src/Zones.jsx:1",
                },
            ],
        )
        self.ingest_all()
        (repo / "src/routes.js").write_text("export const routes = [{ path: '/speed-zones' }, { path: '/map' }]")
        git(repo, "commit", "-qam", "rename route")
        self.scan(repo, stamp="s2")
        r = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]
        self.assertTrue(r["full"])  # a router changed: components are re-placed in the same run
        self.write_map(
            "frontend",
            [
                {
                    "page": "/speed-zones",
                    "kind": "route",
                    "label": "Speed zones",
                    "value": "Zones",
                    "file": "src/routes.js:1",
                },
                {"page": "/map", "kind": "route", "label": "Map", "value": "Map", "file": "src/routes.js:1"},
                {
                    "page": "/speed-zones",
                    "kind": "locator",
                    "label": "Speed",
                    "value": "getByTestId('speed')",
                    "file": "src/Zones.jsx:1",
                },
            ],
        )
        self.ingest_all()
        self.assertIsNone(codemap.find_page(self.con, "/zones"))
        self.assertEqual(codemap.find_page(self.con, "speed zones")["route"], "/speed-zones")

    def test_a_page_renamed_inside_its_own_one_route_file_keeps_stale_rows_until_full(self):  # accepted loss
        ui = git_repo(
            self.dir / "ui",
            {
                "src/routes.js": "export const routes = [{ path: '/zones' }]",
                "e2e/zones.spec.ts": "import { test } from '@playwright/test';\n"
                "test('z', async ({ page }) => { await page.goto('/zones'); await page.getByRole('row').click(); });\n",
            },
        )
        self.scan(ui)
        self.write_map(
            "frontend", [{"page": "/zones", "kind": "route", "label": "Zones", "value": "Z", "file": "src/routes.js:1"}]
        )
        self.write_map(
            "tests",
            [
                {
                    "page": "/zones",
                    "kind": "locator",
                    "label": "Row",
                    "value": "getByRole('row')",
                    "file": "e2e/zones.spec.ts:2",
                }
            ],
        )
        self.ingest_all()
        (ui / "src/routes.js").write_text("export const routes = [{ path: '/areas' }]")
        git(ui, "commit", "-qam", "rename")
        s = self.scan(ui, stamp="s2")
        self.assertEqual((s["areas"]["frontend"]["files"], s["areas"]["tests"]["files"]), (1, 0))  # no full re-map
        self.write_map(
            "frontend", [{"page": "/areas", "kind": "route", "label": "Areas", "value": "A", "file": "src/routes.js:1"}]
        )
        self.ingest_all()
        self.assertIn("Row", codemap.code(self.con, "/zones"))  # stale until `qa-init --full`
        s = self.scan(ui, full=True, stamp="s3")
        self.assertEqual(s["areas"]["tests"]["files"], 1)

    def test_large_areas_are_chunked_and_chunks_ingest_in_any_order(self):
        repo = git_repo(self.dir / "ui", {f"src/C{i}.jsx": f"<b data-testid='c{i}'/>" for i in range(5)})
        s = scan_mod.codemap_scan(self.con, [str(repo)], self.out, False, "s1", chunk_files=2, chunk_tokens=10**9)
        mappers = s["areas"]["frontend"]["mappers"]
        self.assertEqual([m["files"] for m in mappers], [2, 2, 1])
        for m in reversed(mappers):  # ingested in reverse order: nothing may wipe another chunk's rows
            scan = json.loads(Path(m["scan_file"]).read_text())
            rows = [
                {"page": "/c", "kind": "locator", "label": f, "value": "#" + f, "file": f + ":1"}
                for r in scan["repos"]
                for f in r["files"]
            ]
            Path(m["out"]).write_text(json.dumps({"scan_id": m["scan_id"], "area": "frontend", "rows": rows}))
            self.ingest_all()
        self.assertEqual(self.con.execute("SELECT count(*) FROM codemap").fetchone()[0], 5)
        (repo / "src/C4.jsx").unlink()
        git(repo, "commit", "-qam", "drop C4")
        s = scan_mod.codemap_scan(
            self.con, [str(repo)], self.out, True, "s2", chunk_files=2, chunk_tokens=10**9
        )  # --full
        for m in s["areas"]["frontend"]["mappers"]:
            scan = json.loads(Path(m["scan_file"]).read_text())
            rows = [
                {"page": "/c", "kind": "locator", "label": f, "value": "#" + f, "file": f + ":1"}
                for r in scan["repos"]
                for f in r["files"]
            ]
            Path(m["out"]).write_text(json.dumps({"scan_id": m["scan_id"], "area": "frontend", "rows": rows}))
        self.ingest_all()
        self.assertEqual(
            sorted(r[0] for r in self.con.execute("SELECT src FROM codemap")), [f"src/C{i}.jsx" for i in range(4)]
        )

    def test_route_moving_between_chunks_keeps_component_rows(self):
        files = {f"src/C{i}.jsx": f"<b data-testid='c{i}'/>" for i in range(2)}
        files["src/routes.js"] = "export const routes = [{ path: '/zones' }, { path: '/map' }]"
        repo = git_repo(self.dir / "ui", files)
        route_rows = lambda f: (
            [{"page": p, "kind": "route", "label": p, "value": "X", "file": f + ":1"} for p in ("/zones", "/map")]
            if "rout" in f
            else []
        )
        comp = lambda f: (
            [{"page": "/zones", "kind": "locator", "label": f, "value": "#" + f, "file": f + ":1"}] if "/C" in f else []
        )
        s = scan_mod.codemap_scan(self.con, [str(repo)], self.out, False, "s1", chunk_files=10**6)
        self.map_chunks(s, lambda f: route_rows(f) + comp(f))
        self.ingest_all()
        git(repo, "mv", "src/routes.js", "src/router.js")  # the routes move to a file that lands in another chunk
        git(repo, "commit", "-qm", "rename router")
        s = scan_mod.codemap_scan(self.con, [str(repo)], self.out, False, "s2", chunk_files=2, chunk_tokens=10**9)
        mappers = self.map_chunks(s, lambda f: route_rows(f) + comp(f))
        self.assertGreater(len(mappers), 1)
        for _ in mappers:  # the chunk without the router first: the route seems gone until the other chunk lands
            self.ingest_all()
        self.assertEqual(
            sorted(r[0] for r in self.con.execute("SELECT label FROM codemap WHERE kind='locator'")),
            ["src/C0.jsx", "src/C1.jsx"],
        )

    def test_a_chunk_that_never_lands_is_listed_again(self):
        repo = git_repo(self.dir / "ui", {f"src/C{i}.jsx": f"<b data-testid='c{i}'/>" for i in range(4)})
        s = scan_mod.codemap_scan(self.con, [str(repo)], self.out, False, "s1", chunk_files=2, chunk_tokens=10**9)
        mappers = self.map_chunks(
            s, lambda f: [{"page": "/c", "kind": "locator", "label": f, "value": "#" + f, "file": f + ":1"}]
        )
        Path(mappers[1]["out"]).unlink()  # the second mapper never wrote its map
        self.ingest_all()
        s2 = scan_mod.codemap_scan(self.con, [str(repo)], self.out, False, "s2", chunk_files=10, chunk_tokens=10**9)
        listed = json.loads(Path(s2["areas"]["frontend"]["mappers"][0]["scan_file"]).read_text())["repos"][0]["files"]
        self.assertTrue({"src/C2.jsx", "src/C3.jsx"} <= set(listed))  # never hashed: the missing chunk comes back
        self.assertFalse({"src/C0.jsx", "src/C1.jsx"} & set(listed))

    def test_a_remap_chunk_that_never_lands_is_listed_again_on_the_next_scan(self):
        files = {f"src/C{i}.jsx": f"<b data-testid='c{i}'/>" for i in range(3)}
        files["src/routes.js"] = "export const routes = [{ path: '/zones', c: A }, { path: '/map', c: B }]"
        repo = git_repo(self.dir / "ui", files)
        s = scan_mod.codemap_scan(self.con, [str(repo)], self.out, False, "s1", chunk_files=10, chunk_tokens=10**9)
        route_rows = lambda f: (
            [{"page": p, "kind": "route", "label": p, "value": "X", "file": f + ":1"} for p in ("/zones", "/map")]
            if f.endswith("routes.js")
            else []
        )
        self.map_chunks(
            s,
            lambda f: (
                route_rows(f) or [{"page": "/zones", "kind": "locator", "label": f, "value": "#" + f, "file": f + ":1"}]
            ),
        )
        self.ingest_all()
        (repo / "src/routes.js").write_text("export const routes = [{ path: '/zones', c: B }, { path: '/map', c: A }]")
        git(repo, "commit", "-qam", "swap")  # a router change: frontend is mapped in full, in two chunks
        s2 = scan_mod.codemap_scan(self.con, [str(repo)], self.out, False, "s2", chunk_files=2, chunk_tokens=10**9)
        mappers = self.map_chunks(
            s2,
            lambda f: (
                route_rows(f) or [{"page": "/map", "kind": "locator", "label": f, "value": "#" + f, "file": f + ":1"}]
            ),
        )
        self.assertEqual(len(mappers), 2)
        Path(mappers[0]["out"]).unlink()  # the components' chunk never landed; the router's did
        self.ingest_all()
        s3 = scan_mod.codemap_scan(self.con, [str(repo)], self.out, False, "s3", chunk_files=10, chunk_tokens=10**9)
        listed = json.loads(Path(s3["areas"]["frontend"]["mappers"][0]["scan_file"]).read_text())["repos"][0]["files"]
        self.assertTrue({"src/C0.jsx", "src/C1.jsx"} <= set(listed), listed)
        self.assertNotIn("src/routes.js", listed)  # the chunk that landed is settled

    def test_round1_scan_fixes(self):
        # 1. _apply_shell_menu does not prematurely advance codemap_commit before frontend mappers run
        ui = git_repo(
            self.dir / "shell-ui",
            {
                "public/config.json": json.dumps(
                    {"nav": [{"key": "z", "title": "Z", "module": {"id": "z", "url": "/z"}}]}
                ),
                "src/A.jsx": "export const A = () => <b/>;\n",
            },
        )
        self.scan(ui)
        self.assertIsNone(core.meta(self.con, f"codemap_commit:frontend:{ui.resolve()}"))
        # 2. 0-route file edited into a multi-route router triggers full frontend remap
        ui2 = git_repo(
            self.dir / "zero-route",
            {
                "src/routes.js": "export const x = fetch('/api/x');\n",
                "src/A.jsx": "export const A = () => <b/>;\n",
            },
        )
        self.scan(ui2, stamp="z1")
        self.write_map("frontend", [])
        self.ingest_all()
        (ui2 / "src/routes.js").write_text("export const r = [{ path: '/a' }, { path: '/b' }];\n")
        self.scan(ui2, stamp="z2")
        r = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]
        self.assertTrue(r["full"])
        self.assertIn("src/A.jsx", r["files"])
        # 3. _chunk never emits an empty 0-file chunk before a large file
        entries = [
            {
                "path": "/r1",
                "commit": "",
                "full": False,
                "files": [],
                "deleted": [],
                "hashes": {},
                "sizes": {},
                "bytes": 0,
            },
            {
                "path": "/r2",
                "commit": "",
                "full": False,
                "files": ["big.js"],
                "deleted": [],
                "hashes": {"big.js": "h"},
                "sizes": {"big.js": 300000},
                "bytes": 300000,
            },
        ]
        chunks = scan_mod._chunk(entries, 40, 60000)
        self.assertTrue(all(sum(len(e["files"]) for e in ch) > 0 for ch in chunks), chunks)
        # 4. non-UTF-8 submodule/nested repo dir is skipped without crashing
        sub = git_repo(self.dir / "sub", {"src/S.jsx": "export const S = () => <b/>;\n"})
        top = git_repo(self.dir / "nonutf-top", {"src/A.jsx": "export const A = () => <b/>;\n"})
        bad_dir = top / b"sub\xffdir".decode("utf-8", "surrogateescape")
        shutil.copytree(sub, bad_dir)
        self.scan(top, stamp="nu1")
        listed = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]["files"]
        self.assertEqual(listed, ["src/A.jsx"])
        # M1: URL with port + @ in query string does not falsely trigger SECRET_CONTENT_RE
        self.assertEqual(
            scan_mod.classify_source("src/Mail.tsx", "export const u = 'http://localhost:8765?email=qa@example.com';"),
            "frontend",
        )
        # M6: planted symlink at scan-frontend.json is not written through
        victim = self.dir / "scan-victim"
        victim.write_text("keep")
        (self.out / "scan-frontend.json").unlink(missing_ok=True)
        (self.out / "scan-frontend.json").symlink_to(victim)
        self.scan(top, stamp="sym1")
        self.assertEqual(victim.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
