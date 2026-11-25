"""The cross-app graph: shell menu bindings, resource roots and shared components, built from the code map's rows and
the stored transitions; and what cards, gaps, `prep` and `qa-mem impact` show from it. Hints, never oracles."""
import json, unittest

from support import MemBase, cli, core, memory
from qa_sdet import codemap


class GraphCase(MemBase):
    def row(self, page, kind, label, value, src="src/x.jsx", repo="/repo", area="frontend"):
        with self.con:
            if page != "*":
                self.con.execute("INSERT OR IGNORE INTO pages(route) VALUES(?)", (page,))
            self.con.execute("INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                             (page, kind, label, value, f"{src}:1", repo, area, core.now(), src))

    def calls(self, page, *values, src=None):
        for i, v in enumerate(values):
            self.row(page, "api", f"call {i}", v, src or f"src{page}.jsx")

    def mount(self, shell, module, mid="m1"):
        self.row(shell, "route", "Daily", f"module {mid} at {module}", "public/config.json")

    def bug(self, page, symptom, endpoint=None, status="OPEN"):
        memory.ingest(self.con, {"run_id": f"r-{page}-{symptom}", "build": "1", "pages": [{"route": page}],
                                 "bugs": [{"route": page, "title": symptom, "symptom": symptom, "severity": "HIGH",
                                           "classification": "CONFIRMED", **({"endpoint": endpoint} if endpoint else {})}]})
        h = core.bug_hash(page, symptom)
        with self.con:
            self.con.execute("UPDATE bugs SET status=? WHERE hash=?", (status, h))
        return h


class ShellModules(GraphCase):
    """The shell menu's `module <id> at <url>` route row binds a shell route to its module page: one page."""

    def setUp(self):
        super().setUp()
        self.mount("/reports/daily", "/m/daily")
        self.row("/m/daily", "locator", "Date from", "getByLabel('Date from')", "src/Daily.jsx")
        self.row("/m/daily", "rule", "speed", "min 0, max 130", "src/Daily.jsx")
        self.calls("/m/daily", "POST /api/zones", src="src/Daily.jsx")
        self.row("*", "rule", "POST /api/zones speed", "integer, min 0, max 150", "routes/zones.js", area="backend")

    def test_the_binding_reads_both_ways(self):
        self.assertEqual(codemap.module_target(self.con, "/reports/daily"), "/m/daily")
        self.assertEqual(codemap.shell_alias(self.con, "/m/daily"), ["/reports/daily"])
        self.assertIsNone(codemap.module_target(self.con, "/m/daily"))
        self.assertEqual(codemap.shell_alias(self.con, "/reports/daily"), [])

    def test_a_url_with_a_host_or_query_binds_to_its_path(self):
        self.mount("/home", "https://qa.example/m/home?x=1", "m0")
        self.assertEqual(codemap.module_target(self.con, "/home"), "/m/home")

    def test_cyclic_mounts_stop_after_one_hop(self):
        self.mount("/a", "/b")
        self.mount("/b", "/a")
        self.assertEqual(codemap.module_target(self.con, "/a"), "/b")
        self.assertEqual(len(codemap._codemap_rows(self.con, "/a")), 2)  # /a's route row and /b's: nothing chained
        self.assertIn("PAGE /a", memory.card(self.con, "/a"))

    def test_the_shell_route_has_its_modules_rows_and_the_backend_rules(self):
        rows = codemap._codemap_rows(self.con, "/reports/daily")
        self.assertEqual({(x["page"], x["kind"]) for x in rows},
                         {("/reports/daily", "route"), ("/m/daily", "locator"), ("/m/daily", "rule"), ("/m/daily", "api"),
                          ("*", "rule")})
        self.assertIn("/m/daily", codemap.code(self.con, "/reports/daily"))

    def test_the_card_of_the_shell_route_is_not_empty(self):
        card = memory.card(self.con, "/reports/daily")
        self.assertIn("MODULE: m1 at /m/daily", card)
        self.assertIn("getByLabel('Date from')", card)
        self.assertIn("speed: min 0, max 130", card)
        self.assertIn("UNTESTED TARGETS", card)
        self.assertIn("CLIENT/SERVER RULES DIFFER", card)
        self.assertNotIn("MODULE:", memory.card(self.con, "/m/daily"))

    def test_the_module_page_is_hidden_from_gaps_and_the_shell_route_stands_for_both(self):
        out = memory.gaps(self.con)
        self.assertNotIn("/m/daily", out)
        self.assertIn("/reports/daily", out)
        self.assertIn("untested field", next(x for x in out.splitlines() if "/reports/daily" in x))
        pages = json.loads(memory.gaps(self.con, as_json=True))["pages"]
        self.assertEqual([p["route"] for p in pages], ["/reports/daily"])
        self.assertEqual(pages[0]["tier"], "new, with forms")  # the module's rule and write call count for the shell route

    def test_a_module_nobody_mounts_still_shows_in_gaps(self):
        self.row("/other", "rule", "name", "required", "src/Other.jsx")
        self.assertIn("/other", memory.gaps(self.con))

    def test_sharing_pages_treat_the_pair_as_one_page(self):
        self.calls("/zones/new", "POST /api/zones")
        self.assertEqual(codemap.sharing_pages(self.con, "/reports/daily"), ["/zones/new"])  # not its own module page
        self.assertEqual(sorted(codemap.sharing_pages(self.con, "/zones/new")), ["/m/daily", "/reports/daily"])

    def test_a_ticket_naming_the_modules_words_points_at_the_shell_route(self):
        ctx = memory.ticket_context(self.con, "Daily report date from filter speed")
        self.assertIn("/reports/daily", [x["route"] for x in ctx["candidate_routes"]])
        self.assertNotIn("/m/daily", [x["route"] for x in ctx["candidate_routes"]])


class Resources(GraphCase):
    """Pages that write and read one resource (`api/zones` and `api/zones/:id` share a root) are linked."""

    def setUp(self):
        super().setUp()
        self.calls("/zones/new", "POST /api/zones")
        self.calls("/zones", "GET /api/zones")
        self.calls("/zones/:id", "GET /api/zones/:id", "PUT /api/zones/:id")
        self.calls("/map", "GET /api/zones")
        self.calls("/closures", "GET /api/closures")

    @staticmethod
    def routes(items):
        return [x["route"] for x in items]

    def test_the_root_of_an_endpoint_is_its_path_before_the_first_id(self):
        root = codemap.resource_root
        self.assertEqual(root("api/zones/:id/history"), "api/zones")
        self.assertEqual(root("api/v1/zones/:id"), "api/v1/zones")
        self.assertEqual(root("zones"), "zones")
        self.assertEqual(root("api/admin/zones"), "api/admin/zones")  # another resource than api/zones
        for none in ("", "api", ":id/x", "api/graphql", "api/v1/me", "api/config/theme"):  # no root, or a utility endpoint
            self.assertEqual(root(none), "", none)

    def test_a_writer_sees_the_readers_of_its_resource_and_the_other_writers(self):
        n = codemap.resource_neighbors(self.con, "/zones/new")
        self.assertEqual(self.routes(n["siblings"]), ["/zones/:id"])
        self.assertEqual(self.routes(n["consumers"]), ["/map", "/zones"])  # a page listed once: as a sibling if it writes
        self.assertEqual(n["producers"], [])
        self.assertEqual(n["consumers"][0]["why"], "GET api/zones")
        self.assertEqual(n["siblings"][0]["why"], "PUT api/zones/:id")

    def test_a_reader_sees_the_pages_that_write_what_it_shows(self):
        n = codemap.resource_neighbors(self.con, "/zones")
        self.assertEqual(self.routes(n["producers"]), ["/zones/:id", "/zones/new"])
        self.assertEqual((n["consumers"], n["siblings"]), ([], []))
        self.assertEqual(codemap.resource_neighbors(self.con, "/closures"),
                         {"producers": [], "consumers": [], "siblings": []})

    def test_a_base_prefix_or_a_method_less_call_still_links(self):
        self.calls("/zones/old", "fetch(`${BASE}/zones`)")
        self.assertIn("/zones/old", self.routes(codemap.resource_neighbors(self.con, "/zones/new")["consumers"]))

    def test_utility_endpoints_and_widely_called_resources_link_nothing(self):
        for i in range(13):
            self.calls(f"/p{i}", "POST /api/audit", "GET /api/me")
        self.assertEqual(codemap.resource_neighbors(self.con, "/p0"), {"producers": [], "consumers": [], "siblings": []})

    def test_a_module_page_shows_as_its_shell_route(self):
        self.mount("/reports/zones", "/m/zones")
        self.calls("/m/zones", "GET /api/zones")
        n = codemap.resource_neighbors(self.con, "/zones/new")
        self.assertIn("/reports/zones", self.routes(n["consumers"]))
        self.assertNotIn("/m/zones", self.routes(n["consumers"]))
        self.assertEqual(codemap.resource_neighbors(self.con, "/reports/zones")["producers"][0]["route"], "/zones/:id")

    def test_an_endpoint_alone_gives_the_same_links(self):
        n = codemap.resource_links(self.con, {("PUT", "api/zones/:id")})
        self.assertEqual(self.routes(n["siblings"]), ["/zones/:id", "/zones/new"])
        self.assertEqual(self.routes(n["consumers"]), ["/map", "/zones"])


class Components(GraphCase):
    """Pages whose rows come from one component file are neighbours: a bug in the component shows on each."""

    def setUp(self):
        super().setUp()
        for page in ("/incidents", "/schedules"):
            self.row(page, "locator", "Date range", "getByLabel('Range')", "src/components/DateRangePicker.tsx")
        self.row("/incidents", "rule", "from", "required", "src/components/DateRangePicker.tsx")
        self.row("/incidents", "locator", "Title", "getByLabel('Title')", "src/Incidents.tsx")

    def test_pages_sharing_a_component_file_are_neighbours(self):
        n = codemap.component_neighbors(self.con, "/schedules")
        self.assertEqual(n, [{"route": "/incidents", "why": "src/components/DateRangePicker.tsx"}])
        self.assertEqual(codemap.component_neighbors(self.con, "/incidents")[0]["route"], "/schedules")

    def test_a_layout_on_many_pages_links_nothing(self):
        for i in range(16):
            self.row(f"/p{i}", "locator", "Menu", "getByRole('navigation')", "src/AppLayout.tsx")
        self.assertEqual(codemap.component_neighbors(self.con, "/p0"), [])

    def test_endpoints_tests_and_the_backend_do_not_count(self):
        self.row("/a", "api", "x", "GET /api/x", "src/api/client.js")
        self.row("/b", "api", "x", "GET /api/y", "src/api/client.js")
        self.row("/a", "locator", "x", "y", "e2e/page.ts", area="tests")
        self.row("/b", "locator", "x", "y", "e2e/page.ts", area="tests")
        self.assertEqual(codemap.component_neighbors(self.con, "/a"), [])

    def test_a_module_and_its_shell_route_are_one_page(self):
        self.mount("/traffic/incidents", "/m/incidents")
        self.row("/m/incidents", "locator", "Range", "x", "src/components/DateRangePicker.tsx")
        n = codemap.component_neighbors(self.con, "/traffic/incidents")
        self.assertEqual([x["route"] for x in n], ["/incidents", "/schedules"])
        self.assertIn("/traffic/incidents", [x["route"] for x in codemap.component_neighbors(self.con, "/schedules")])


if __name__ == "__main__":
    unittest.main()
