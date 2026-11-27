"""The code map on the page card (qa_sdet.codemap, cards): the FROM CODE block, `qa-mem search`, and bugs on other pages
that call the same endpoint."""

import shutil
import unittest

from support import FIX, CodeMapCase, core, git, git_repo

from qa_sdet import cards, codemap, ingest, memory


class Card(CodeMapCase):
    """The FROM CODE block of a page's card, and `qa-mem search`."""

    def test_card_block_is_compact_and_separate(self):
        self.scan(FIX / "frontend")
        rows = [{"page": "/form.html", "kind": "route", "label": "Speed zone", "value": "F", "file": "src/routes.js:4"}]
        rows += [
            {
                "page": "/form.html",
                "kind": "locator",
                "label": f"field {i:02}",
                "value": f"#f{i}",
                "file": "src/components/SpeedZoneForm.jsx:8",
            }
            for i in range(17)
        ]
        rows += [
            {
                "page": "/form.html",
                "kind": "rule",
                "label": "speed",
                "value": "min 0, max 130",
                "file": "src/components/SpeedZoneForm.jsx:8",
            }
        ]
        self.write_map("frontend", rows)
        self.ingest_all()
        c = cards.card(self.con, "speed zone")
        self.assertIn("NOTE: known from source code (or a link) only", c)
        self.assertNotIn("re-verify", c)
        self.assertIn("FROM CODE (hints from source, not requirements; unverified;", c)
        self.assertIn("speed: min 0, max 130", c)
        self.assertIn("+2 more", c)  # 17 locators, 15 shown
        self.assertNotIn("(from ", c.split("FROM CODE")[0])  # never shown as a stated requirement
        ingest.ingest(
            self.con,
            {
                "run_id": "r1",
                "build": "1.0",
                "pages": [{"route": "/form.html", "locators": [{"name": "field 00", "locator": "#f0"}]}],
            },
        )
        c = cards.card(self.con, "speed zone")
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
        self.write_map(
            "frontend",
            [
                {"page": "/form.html", "kind": "route", "label": "Speed zone", "value": "F", "file": "src/routes.js:4"},
                {
                    "page": "/form.html",
                    "kind": "rule",
                    "label": "speed",
                    "value": "max 130",
                    "file": "src/components/SpeedZoneForm.jsx:8",
                },
            ],
        )
        self.ingest_all()
        for i in range(2):
            (repo / f"n{i}.txt").write_text(str(i))
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", f"c{i}")
        self.assertIn("2 commits behind", cards.card(self.con, "/form.html"))

    def test_behind_note_covers_the_backend_rows_on_the_card(self):
        """the note named only the page's own repo, so a backend 3 commits on looked current beside its rules."""
        ui = git_repo(self.dir / "ui", {"src/routes.js": "const r = [{ path: '/zones', element: <Z/> }]\n"})
        api = git_repo(self.dir / "api", {"routes/zones.js": "router.post('/api/zones', h)\n"})
        self.scan(ui, api)
        self.write_map(
            "frontend",
            [
                {"page": "/zones", "kind": "route", "label": "Zones", "value": "Z", "file": "src/routes.js:1"},
                {
                    "page": "/zones",
                    "kind": "api",
                    "label": "save",
                    "value": "POST /api/zones",
                    "file": "src/routes.js:1",
                },
            ],
        )
        self.write_map(
            "backend",
            [
                {
                    "page": "*",
                    "kind": "rule",
                    "label": "POST /api/zones speed",
                    "value": "max 130",
                    "file": "routes/zones.js:1",
                }
            ],
        )
        self.ingest_all()
        for i in range(3):
            (api / "routes/zones.js").write_text(f"// {i}\n")
            git(api, "commit", "-qam", f"c{i}")
        (ui / "src/routes.js").write_text("// x\n")
        git(ui, "commit", "-qam", "u")
        c = cards.card(self.con, "/zones")
        self.assertIn("POST /api/zones speed: max 130", c)
        self.assertRegex(c, r"api [0-9a-f]{7}, 3 commits behind; ui [0-9a-f]{7}, 1 commits behind")
        self.assertIn("api ", codemap.code(self.con, "/zones").splitlines()[0])

    def test_behind_note_clears_and_ignores_other_folders(self):
        mono = self.dir / "mono"
        git_repo(mono, {"web/src/routes.js": "export const routes = [{ path: '/z' }]", "api/r.js": "x"})
        web = mono / "web"
        self.scan(web)
        self.write_map(
            "frontend",
            [
                {"page": "/z", "kind": "route", "label": "Z", "value": "Z", "file": "src/routes.js:1"},
                {"page": "/z", "kind": "rule", "label": "z", "value": "max 3", "file": "src/routes.js:1"},
            ],
        )
        self.ingest_all()
        (mono / "api/r.js").write_text("y")  # a commit outside the mapped folder
        git(mono, "commit", "-qam", "api only")
        self.assertNotIn("behind", cards.card(self.con, "/z"))
        (web / "README.md").write_text("docs")  # inside the folder, but no mapped file changes
        git(mono, "add", "-A")
        git(mono, "commit", "-qm", "web docs")
        self.assertIn("1 commits behind", cards.card(self.con, "/z"))
        s = self.scan(web, stamp="s2")  # nothing to map: the scan settles it
        self.assertFalse(s["areas"]["frontend"]["needs_mapper"])
        self.assertNotIn("behind", cards.card(self.con, "/z"))

    def test_empty_map_tips(self):
        self.assertIn("tip: /qa-sdet:qa-init", cards.card(self.con, "closures"))
        self.assertIn("no code map yet", codemap.code(self.con))
        self.scan(FIX / "frontend")
        self.write_map(
            "frontend",
            [{"page": "/form.html", "kind": "route", "label": "Speed zone", "value": "F", "file": "src/routes.js:4"}],
        )
        self.ingest_all()
        self.assertNotIn("tip:", cards.card(self.con, "closures"))
        self.assertIn("/form.html  Speed zone", codemap.code(self.con))

    def test_route_params_meet_run_pages(self):
        repo = git_repo(self.dir / "ui", {"src/Detail.jsx": "<h1 data-testid='title'/>"})
        self.scan(repo)
        self.write_map(
            "frontend",
            [
                {
                    "page": "/incidents/:incidentId",
                    "kind": "locator",
                    "label": "Title",
                    "value": "getByTestId('title')",
                    "file": "src/Detail.jsx:1",
                }
            ],
        )
        self.ingest_all()
        ingest.ingest(self.con, {"run_id": "r1", "build": "1.0", "pages": [{"route": "/incidents/4521"}]})
        c = cards.card(self.con, "/incidents/4521")
        self.assertIn("Title=getByTestId('title')", c)  # one page: /incidents/:id
        for r in ("/zones/{zoneId}", "/zones/[id]", "/zones/<int:pk>", "/zones/42"):
            self.assertEqual(core.norm_route(r), "/zones/:id", r)

    def test_page_known_only_from_locators_has_a_card(self):
        repo = git_repo(
            self.dir / "e2e", {"pages/Zones.ts": "import { test } from '@playwright/test'; page.getByRole('row')"}
        )
        self.scan(repo)
        self.write_map(
            "tests",
            [
                {
                    "page": "/zones",
                    "kind": "locator",
                    "label": "Rows",
                    "value": "getByRole('row')",
                    "file": "pages/Zones.ts:1",
                }
            ],
        )
        self.ingest_all()
        self.assertIn("Rows=getByRole('row')", cards.card(self.con, "/zones"))

    def test_crawl_names_dont_break_lookup_by_code_name(self):
        repo = git_repo(self.dir / "ui", {"src/routes.js": "export const routes = [{ path: '/zones' }]"})
        self.scan(repo)
        self.write_map(
            "frontend",
            [{"page": "/zones", "kind": "route", "label": "Speed zones", "value": "Z", "file": "src/routes.js:1"}],
        )
        self.ingest_all()
        self.con.execute("UPDATE pages SET name='Traffic QA' WHERE route='/zones'")  # a crawl wrote document.title
        self.con.commit()
        self.assertEqual(codemap.find_page(self.con, "speed zones")["route"], "/zones")

    def test_search_lists_page_rows_before_backend_rows(self):
        repo = git_repo(self.dir / "ui", {"src/Z.jsx": "<input name='speed'/>"})
        api = git_repo(self.dir / "api", {"r.js": "router.get('/x', h)"})
        self.scan(repo, api)
        self.write_map(
            "backend",
            [
                {
                    "repo": str(api),
                    "page": "*",
                    "kind": "rule",
                    "label": f"POST /r{i} speed",
                    "value": "max 130",
                    "file": "r.js:1",
                }
                for i in range(12)
            ],
        )
        self.write_map(
            "frontend",
            [
                {
                    "repo": str(repo),
                    "page": "/zones",
                    "kind": "rule",
                    "label": "speed",
                    "value": "max 130",
                    "file": "src/Z.jsx:1",
                }
            ],
        )
        self.ingest_all()
        self.assertTrue(memory.search(self.con, "speed").splitlines()[0].startswith("/zones [code:rule]"))

    def test_a_visit_with_an_unreadable_build_counts_as_a_visit(self):
        repo = git_repo(self.dir / "ui", {"src/routes.js": "export const routes = [{ path: '/map' }]"})
        self.scan(repo)
        self.write_map(
            "frontend", [{"page": "/map", "kind": "route", "label": "Map", "value": "M", "file": "src/routes.js:1"}]
        )
        self.ingest_all()
        ingest.ingest(self.con, {"run_id": "r1", "build": "", "pages": [{"route": "/map"}]})
        self.assertNotIn("no run has visited", cards.card(self.con, "/map"))


class SharedEndpoints(CodeMapCase):
    """Bugs on other pages that call the same endpoint: one backend bug stays one record, re-checked on each page."""

    SECTION = "BUGS ON PAGES SHARING AN ENDPOINT"

    def calls(self, page, *values):
        with self.con:
            self.con.execute("INSERT OR IGNORE INTO pages(route) VALUES(?)", (page,))
            for i, v in enumerate(values):
                self.con.execute(
                    "INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        page,
                        "api",
                        f"call {i}",
                        v,
                        f"src{page}.jsx:{i}",
                        "/repo",
                        "frontend",
                        core.now(),
                        f"src{page}.jsx",
                    ),
                )

    def bug(self, page, symptom, status="OPEN"):
        ingest.ingest(
            self.con,
            {
                "run_id": f"r-{page}-{symptom}",
                "build": "1",
                "pages": [{"route": page}],
                "bugs": [
                    {
                        "route": page,
                        "title": symptom,
                        "symptom": symptom,
                        "severity": "HIGH",
                        "classification": "CONFIRMED",
                    }
                ],
            },
        )
        h = core.bug_hash(page, symptom)
        with self.con:
            self.con.execute("UPDATE bugs SET status=? WHERE hash=?", (status, h))
        return h

    def shared(self, route):
        card = cards.card(self.con, route)
        return card.split(self.SECTION, 1)[1] if self.SECTION in card else ""

    def test_card_shows_a_bug_from_a_page_calling_the_same_endpoint(self):
        self.calls("/zones/new", "POST /api/zones")
        self.calls("/zones/:id", "POST /api/zones", "GET /api/zones/:id")
        h = self.bug("/zones/new", "server accepts speed 131")
        self.assertIn(h, self.shared("/zones/:id"))
        self.assertIn("/zones/new", self.shared("/zones/:id"))
        self.assertEqual(self.shared("/zones/new"), "")  # its own bug is under KNOWN BUGS, not here

    def test_other_methods_and_endpoints_are_not_shared(self):
        self.calls("/zones/new", "POST /api/zones")
        self.calls("/zones", "GET /api/zones")
        self.calls("/incidents", "GET /api/incidents")
        self.bug("/zones/new", "server accepts speed 131")
        self.assertEqual(self.shared("/zones"), "")
        self.assertEqual(self.shared("/incidents"), "")
        self.assertEqual(codemap.sharing_pages(self.con, "*"), [])
        self.assertEqual(codemap.sharing_pages(self.con, "/nowhere"), [])

    def test_a_plain_fetch_is_a_get_and_shares_only_with_gets(self):
        self.calls("/zones", "fetch(`${API}/zones`)")
        self.calls("/zones/admin", "DELETE /api/zones")
        self.calls("/zones/report", "GET /api/zones")
        self.bug("/zones/admin", "purge deletes every zone")
        self.assertEqual(codemap.sharing_pages(self.con, "/zones"), ["/zones/report"])
        self.assertEqual(self.shared("/zones"), "")

    def test_an_endpoint_most_pages_call_links_nothing(self):
        for i in range(7):
            self.calls(f"/p{i}", "GET /api/me")
        self.bug("/p0", "profile call fails")
        self.assertEqual(self.shared("/p1"), "")

    def endpoint_bug(self, page, symptom, endpoint, title=None):
        ingest.ingest(
            self.con,
            {
                "run_id": f"r-{page}-{symptom}",
                "build": "1",
                "pages": [{"route": page}],
                "bugs": [
                    {
                        "route": page,
                        "title": title or symptom,
                        "symptom": symptom,
                        "severity": "HIGH",
                        "classification": "CONFIRMED",
                        "endpoint": endpoint,
                    }
                ],
            },
        )
        return core.bug_hash(page, symptom)

    def test_a_bug_on_an_endpoint_most_pages_call_reaches_every_caller(self):  # APP_WIDE_CALLERS hid it
        for i in range(8):
            self.calls(f"/p{i}", "GET /api/corridors")
        self.calls("/detours", "fetch('/api/corridors')")
        h = self.endpoint_bug("/p0", "corridors list empty on 500", "GET /api/corridors")
        self.assertIn(h, self.shared("/detours"))
        self.assertIn("GET api/corridors", self.shared("/detours"))

    def test_an_endpoint_bug_does_not_leak_to_pages_that_share_another_call(self):  # a CSS bug rode on a shared GET
        self.calls("/zones/new", "POST /api/zones", "GET /api/units")
        self.calls("/closures", "GET /api/units")
        h = self.endpoint_bug("/zones/new", "speed 131 saved", "POST /api/zones")
        self.assertNotIn(h, self.shared("/closures"))

    def test_a_bug_sighted_on_this_page_is_known_here(self):
        self.calls("/zones/new", "POST /api/zones")
        h = self.endpoint_bug("/zones/new", "speed 131 saved", "POST /api/zones")
        ingest.ingest(
            self.con,
            {
                "run_id": "r-edit",
                "build": "1",
                "pages": [{"route": "/zones/:id"}],
                "bugs": [{"route": "/zones/:id", "title": "x", "hash": h}],
            },
        )
        known = cards.card(self.con, "/zones/:id").split("KNOWN BUGS", 1)[1]
        self.assertIn(h, known)
        self.assertEqual(
            self.con.execute("SELECT page FROM sightings WHERE run_id='r-edit'").fetchone()[0], "/zones/:id"
        )

    def test_fixed_bugs_show_and_invalid_ones_do_not(self):
        self.calls("/zones/new", "POST /api/zones")
        self.calls("/zones/:id", "POST /api/zones")
        fixed, invalid = (
            self.bug("/zones/new", "speed 131 saved", "FIXED"),
            self.bug("/zones/new", "label typo", "INVALID"),
        )
        self.assertIn(fixed, self.shared("/zones/:id"))
        self.assertNotIn(invalid, self.shared("/zones/:id"))

    def test_a_sighting_on_the_other_page_stays_one_bug(self):
        self.calls("/zones/new", "POST /api/zones")
        self.calls("/zones/:id", "POST /api/zones")
        h = self.bug("/zones/new", "server accepts speed 131")
        ingest.ingest(
            self.con,
            {
                "run_id": "r-edit",
                "build": "1",
                "pages": [{"route": "/zones/:id"}],
                "bugs": [
                    {
                        "route": "/zones/4521",
                        "title": "Server accepts speed 131 on edit",
                        "hash": h,
                        "severity": "HIGH",
                        "classification": "CONFIRMED",
                    }
                ],
            },
        )
        rows = self.con.execute("SELECT hash, page, seen_count FROM bugs").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(h, "/zones/new", 2)])


if __name__ == "__main__":
    unittest.main()
