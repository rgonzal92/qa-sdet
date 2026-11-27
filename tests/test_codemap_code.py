"""`qa-mem code`, `--repos` and `codemap-forget` (qa_sdet.codemap): a page's calls linked to backend rows."""

import os
import unittest

from support import FIX, CodeMapCase, core, git_repo

from qa_sdet import cards, codemap, ingest


class Code(CodeMapCase):
    """`qa-mem code`, `--repos` and `codemap-forget`; linking a page's calls to backend rows."""

    def test_code_lists_totals_even_with_only_a_backend(self):
        """with only backend rows, `code` printed a bare header; qa-init's reply takes its counts from it."""
        api = git_repo(self.dir / "api", {"routes/zones.js": "router.post('/api/zones', h)\n"})
        self.scan(api)
        self.write_map(
            "backend",
            [{"page": "*", "kind": "api", "label": "POST /api/zones", "value": "x", "file": "routes/zones.js:1"}],
        )
        self.ingest_all()
        out = codemap.code(self.con)
        self.assertIn("(none: no frontend routes mapped yet)", out)
        self.assertIn("TOTAL: 0 routes, 0 locators, 0 rules, 1 endpoints (1 backend", out)

    def test_repos_listing_and_forget(self):
        gone = str(self.dir / "old-clone")
        self.scan(FIX / "frontend")
        self.write_map(
            "frontend",
            [{"page": "/form.html", "kind": "route", "label": "Speed zone", "value": "F", "file": "src/routes.js:4"}],
        )
        self.ingest_all()
        self.con.execute(
            "INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) "
            "VALUES('/old.html','route','Old','Old','a.js:1',?,'frontend','t','a.js')",
            (gone,),
        )
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
        self.con.execute(
            "INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) "
            "VALUES('/old.html','route','Old','Old','a.js:1',?,'frontend','t','a.js')",
            (gone,),
        )
        core.set_meta(self.con, f"codemap_commit:frontend:{gone}", "a" * 40)
        self.con.commit()
        self.assertIsNone(codemap.codemap_forget(self.con, "~/never-mapped"))
        self.assertEqual(codemap.codemap_forget(self.con, "~/old-clone/"), 1)
        self.assertNotIn("old-clone", codemap.code(self.con, "--repos"))  # its bookkeeping went too

    def test_backend_linking_is_method_aware_and_handles_more_path_shapes(self):
        ui = git_repo(self.dir / "ui", {"src/Z.jsx": "fetch('/zones')"})
        api = git_repo(self.dir / "api", {"r.js": "router.get('/zones', h)"})
        self.scan(ui, api)
        self.write_map(
            "frontend",
            [
                {
                    "repo": str(ui),
                    "page": "/zones",
                    "kind": "api",
                    "label": "list",
                    "value": "GET https://qa.example.com/zones",
                    "file": "src/Z.jsx:1",
                }
            ],
        )
        self.write_map(
            "backend",
            [
                {
                    "repo": str(api),
                    "page": "*",
                    "kind": "api",
                    "label": "GET /zones",
                    "value": "list",
                    "file": "r.js:1",
                },
                {
                    "repo": str(api),
                    "page": "*",
                    "kind": "api",
                    "label": "DELETE /zones",
                    "value": "drop all",
                    "file": "r.js:1",
                },
                {
                    "repo": str(api),
                    "page": "*",
                    "kind": "rule",
                    "label": "POST /zones name",
                    "value": "max 80",
                    "file": "r.js:1",
                },
            ],
        )
        self.ingest_all()
        c = codemap.code(self.con, "/zones")
        self.assertIn("GET /zones = list", c)  # single segment, absolute URL on the frontend side
        self.assertNotIn("DELETE /zones", c)  # different method
        self.assertNotIn("POST /zones name", c)
        self.assertEqual(codemap.api_ref("fetch(`${BASE}/zones/${id}`)"), ("GET", "zones/:id"))  # complete, no method

    def test_backend_links_need_the_same_resource(self):
        ui = git_repo(self.dir / "ui", {"src/I.jsx": "fetch('/api/incidents/1/status', {method: 'PATCH'})"})
        api = git_repo(self.dir / "api", {"r.js": "router.patch('/:id/status', h)"})
        self.scan(ui, api)
        self.write_map(
            "frontend",
            [
                {
                    "repo": str(ui),
                    "page": "/incidents/:id",
                    "kind": "api",
                    "label": "set status",
                    "value": "PATCH /api/incidents/:id/status",
                    "file": "src/I.jsx:1",
                },
                {
                    "repo": str(ui),
                    "page": "/zones",
                    "kind": "api",
                    "label": "list",
                    "value": "fetch(`${API}/zones`)",
                    "file": "src/I.jsx:1",
                },
            ],
        )
        self.write_map(
            "backend",
            [
                {
                    "repo": str(api),
                    "page": "*",
                    "kind": "rule",
                    "label": "PATCH /:id/status status",
                    "value": "one of: ACTIVE",
                    "file": "r.js:1",
                },
                {
                    "repo": str(api),
                    "page": "*",
                    "kind": "rule",
                    "label": "PATCH /api/incidents/:id/status status",
                    "value": "one of: OPEN, CLOSED",
                    "file": "r.js:1",
                },
                {
                    "repo": str(api),
                    "page": "*",
                    "kind": "api",
                    "label": "GET /api/admin/zones",
                    "value": "admin",
                    "file": "r.js:1",
                },
                {
                    "repo": str(api),
                    "page": "*",
                    "kind": "api",
                    "label": "GET /api/v1/zones",
                    "value": "list",
                    "file": "r.js:1",
                },
            ],
        )
        self.ingest_all()
        i, z = codemap.code(self.con, "/incidents/:id"), codemap.code(self.con, "/zones")
        self.assertIn("OPEN, CLOSED", i)
        self.assertNotIn("ACTIVE", i)  # a router-relative tail could be any resource
        self.assertIn("GET /api/v1/zones", z)
        self.assertNotIn("admin", z)

    def test_api_path_shapes(self):
        p = lambda text: codemap.api_ref(text)[1]
        self.assertEqual(p("GET /api/incidents?x=1"), "api/incidents")
        self.assertEqual(p("fetch('api/incidents.json')"), "api/incidents")
        self.assertEqual(p("PUT /api/zones/{id}"), "api/zones/:id")
        self.assertEqual(p("getIncidents"), "")
        r = codemap.api_ref
        self.assertEqual(r("fetch('/api/zones/' + id + '/detours')"), ("", "api/zones/:id/detours"))
        self.assertEqual(r("api.get<Zone[]>('/zones')"), ("GET", "zones"))
        self.assertEqual(r("http.post<Zone>('/api/zones', z)"), ("POST", "api/zones"))

    def test_fetch_methods(self):
        """A method in the options is read; a complete fetch without one is a GET; a cut or variable call stays open."""
        r = codemap.api_ref
        self.assertEqual(r("fetch(`${API}/zones`, {method: 'PUT'})"), ("PUT", "zones"))
        self.assertEqual(r("axios({ method: 'delete', url: '/api/zones' })"), ("DELETE", "api/zones"))
        self.assertEqual(r("fetch('api/incidents.json', { headers: { 'x-api-key': k } })"), ("GET", "api/incidents"))
        self.assertEqual(r("fetch(`${API}/zones`)"), ("GET", "zones"))
        for open_call in (
            "fetch(`${API}/zones`, opts)",
            "fetch(`${API}/zones`, {method})",
            "fetch(`${API}/zones`",
            "fetch('/api/zones/' + id + '/detours')",
        ):
            self.assertEqual(r(open_call)[0], "", open_call)  # the method may be elsewhere: matches any

    def test_round1_codemap_stale_card_star_and_hash_routes(self):
        repo = git_repo(self.dir / "ui", {"src/A.jsx": "<button data-testid='save'/>"})
        self.scan(repo)
        self.write_map(
            "frontend",
            [
                {"page": "/a", "kind": "route", "label": "A", "value": "A", "file": "src/A.jsx:1"},
                {
                    "page": "/a",
                    "kind": "locator",
                    "label": "Save",
                    "value": "getByTestId('save')",
                    "file": "src/A.jsx:1",
                },
                {"page": "/*", "kind": "route", "label": "Catchall", "value": "C", "file": "src/A.jsx:1"},
                {"page": "/#/zones/:id", "kind": "route", "label": "Zone", "value": "Z", "file": "src/A.jsx:1"},
            ],
        )
        api = git_repo(self.dir / "api", {"r.js": "router.get('/api/x', h)"})
        self.scan(repo, api, stamp="s2")
        self.write_map(
            "frontend",
            [
                {"repo": str(repo), "page": "/a", "kind": "route", "label": "A", "value": "A", "file": "src/A.jsx:1"},
                {
                    "repo": str(repo),
                    "page": "/a",
                    "kind": "locator",
                    "label": "Save",
                    "value": "getByTestId('save')",
                    "file": "src/A.jsx:1",
                },
                {
                    "repo": str(repo),
                    "page": "/*",
                    "kind": "route",
                    "label": "Catchall",
                    "value": "C",
                    "file": "src/A.jsx:1",
                },
                {
                    "repo": str(repo),
                    "page": "/#/zones/:id",
                    "kind": "route",
                    "label": "Zone",
                    "value": "Z",
                    "file": "src/A.jsx:1",
                },
            ],
        )
        self.write_map(
            "backend",
            [
                {"repo": str(api), "page": "*", "kind": "api", "label": "GET /api/x", "value": "x", "file": "r.js:1"},
            ],
        )
        self.ingest_all()
        # 1. Stale build card still prints FROM CODE locators when LOCATORS (fresh) was skipped
        ingest.ingest(
            self.con,
            {
                "run_id": "r1",
                "build": "1.0",
                "pages": [{"route": "/a", "locators": [{"name": "Save", "locator": "getByTestId('save')"}]}],
            },
            "2026-01-01T00:00:00Z",
        )
        ingest.ingest(
            self.con, {"run_id": "r2", "build": "2.0", "pages": [{"route": "/other"}]}, "2026-01-02T00:00:00Z"
        )
        self.assertIn("getByTestId('save')", cards.card(self.con, "/a"))
        # 2. code('*') returns backend rows even when a '/*' frontend route exists
        self.assertTrue(codemap.code(self.con, "*").startswith("CODE MAP * "))
        # 3. find_page resolves SPA hash routes without a leading slash
        self.assertEqual(codemap.find_page(self.con, "#/zones/42")["route"], "/#/zones/:id")


if __name__ == "__main__":
    unittest.main()
