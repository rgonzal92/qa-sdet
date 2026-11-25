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
            self.con.execute(
                "INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                (page, kind, label, value, f"{src}:1", repo, area, core.now(), src),
            )

    def calls(self, page, *values, src=None):
        for i, v in enumerate(values):
            self.row(page, "api", f"call {i}", v, src or f"src{page}.jsx")

    def mount(self, shell, module, mid="m1"):
        self.row(shell, "route", "Daily", f"module {mid} at {module}", "public/config.json")

    def bug(self, page, symptom, endpoint=None, status="OPEN"):
        memory.ingest(
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
                        **({"endpoint": endpoint} if endpoint else {}),
                    }
                ],
            },
        )
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
        self.assertEqual(
            {(x["page"], x["kind"]) for x in rows},
            {
                ("/reports/daily", "route"),
                ("/m/daily", "locator"),
                ("/m/daily", "rule"),
                ("/m/daily", "api"),
                ("*", "rule"),
            },
        )
        self.assertIn("/m/daily", codemap.code(self.con, "/reports/daily"))

    def test_the_card_of_the_shell_route_is_not_empty(self):
        card = memory.card(self.con, "/reports/daily")
        self.assertIn("MODULE: m1 at /m/daily", card)
        self.assertIn("getByLabel('Date from')", card)
        self.assertIn("speed: min 0, max 130", card)
        self.assertIn("UNTESTED TARGETS", card)
        self.assertIn("CLIENT/SERVER RULES DIFFER", card)
        self.assertNotIn("MODULE:", memory.card(self.con, "/m/daily"))

    def test_a_bug_on_an_endpoint_the_module_calls_shows_on_the_shell_card(self):
        self.calls("/zones/new", "POST /api/zones")
        memory.ingest(
            self.con,
            {
                "run_id": "r1",
                "build": "1",
                "pages": [{"route": "/zones/new"}],
                "bugs": [
                    {
                        "route": "/zones/new",
                        "title": "speed 131 saved",
                        "symptom": "speed 131 saved",
                        "severity": "HIGH",
                        "classification": "CONFIRMED",
                        "endpoint": "POST /api/zones",
                    }
                ],
            },
        )
        self.assertIn(core.bug_hash("/zones/new", "speed 131 saved"), memory.card(self.con, "/reports/daily"))

    def test_the_module_page_is_hidden_from_gaps_and_the_shell_route_stands_for_both(self):
        out = memory.gaps(self.con)
        self.assertNotIn("/m/daily", out)
        self.assertIn("/reports/daily", out)
        self.assertIn("untested field", next(x for x in out.splitlines() if "/reports/daily" in x))
        pages = json.loads(memory.gaps(self.con, as_json=True))["pages"]
        self.assertEqual([p["route"] for p in pages], ["/reports/daily"])
        self.assertEqual(
            pages[0]["tier"], "new, with forms"
        )  # the module's rule and write call count for the shell route

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
        for none in (
            "",
            "api",
            ":id/x",
            "api/graphql",
            "api/v1/me",
            "api/config/theme",
        ):  # no root, or a utility endpoint
            self.assertEqual(root(none), "", none)

    def test_a_writer_sees_the_readers_of_its_resource_and_the_other_writers(self):
        n = codemap.resource_neighbors(self.con, "/zones/new")
        self.assertEqual(self.routes(n["siblings"]), ["/zones/:id"])
        self.assertEqual(
            self.routes(n["consumers"]), ["/map", "/zones"]
        )  # a page listed once: as a sibling if it writes
        self.assertEqual(n["producers"], [])
        self.assertEqual(n["consumers"][0]["why"], "GET api/zones")
        self.assertEqual(n["siblings"][0]["why"], "PUT api/zones/:id")

    def test_a_reader_sees_the_pages_that_write_what_it_shows(self):
        n = codemap.resource_neighbors(self.con, "/zones")
        self.assertEqual(self.routes(n["producers"]), ["/zones/:id", "/zones/new"])
        self.assertEqual((n["consumers"], n["siblings"]), ([], []))
        self.assertEqual(
            codemap.resource_neighbors(self.con, "/closures"), {"producers": [], "consumers": [], "siblings": []}
        )

    def test_a_base_prefix_or_a_method_less_call_still_links(self):
        self.calls("/zones/old", "fetch(`${BASE}/zones`)")
        self.assertIn("/zones/old", self.routes(codemap.resource_neighbors(self.con, "/zones/new")["consumers"]))

    def test_utility_endpoints_and_widely_called_resources_link_nothing(self):
        for i in range(13):
            self.calls(f"/p{i}", "POST /api/audit", "GET /api/me")
        self.assertEqual(
            codemap.resource_neighbors(self.con, "/p0"), {"producers": [], "consumers": [], "siblings": []}
        )

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


class CrossPageRules(GraphCase):
    """Pages that write one resource may state its field's rules differently: a hint, never an oracle."""

    def setUp(self):
        super().setUp()
        self.calls("/zones/new", "POST /api/zones")
        self.calls("/zones/:id", "PUT /api/zones/:id")
        self.calls("/zones", "GET /api/zones")
        self.row("/zones/new", "rule", "Speed limit", "min 0, max 130", "src/New.jsx")
        self.row("/zones/:id", "rule", "speedLimit", "min 0, max 150", "src/Edit.jsx")
        self.row("/zones", "rule", "speed limit", "max 999", "src/List.jsx")  # a reader: not compared

    def test_the_same_field_with_another_limit_on_the_edit_page_is_a_hint(self):
        hints = memory._cross_page_rules_differ(self.con, "/zones/new")
        self.assertEqual(hints, ["Speed limit: here max 130 vs /zones/:id max 150"])
        self.assertEqual(
            memory._cross_page_rules_differ(self.con, "/zones/:id"), ["speedLimit: here max 150 vs /zones/new max 130"]
        )

    def test_a_flag_one_page_lacks_is_named(self):
        self.row("/zones/new", "rule", "name", "required, max 40", "src/New.jsx")
        self.row("/zones/:id", "rule", "Name", "max 40", "src/Edit.jsx")
        self.assertIn("name: here only: required", memory._cross_page_rules_differ(self.con, "/zones/new"))

    def test_agreeing_rules_fields_on_one_page_only_and_other_resources_say_nothing(self):
        self.row("/zones/new", "rule", "color", "required", "src/New.jsx")  # no counterpart on the edit page
        self.row("/zones/:id", "rule", "Speed limit", "min 0, max 130", "src/Edit.jsx")  # the same rule, a second label
        self.assertEqual(
            memory._cross_page_rules_differ(self.con, "/zones/new"), ["Speed limit: here max 130 vs /zones/:id max 150"]
        )
        self.calls("/closures/new", "POST /api/closures")
        self.row("/closures/new", "rule", "speed limit", "max 5", "src/Closure.jsx")
        self.assertEqual(len(memory._cross_page_rules_differ(self.con, "/zones/new")), 1)
        self.assertEqual(memory._cross_page_rules_differ(self.con, "/closures/new"), [])

    def test_the_backend_rules_are_not_compared_here(self):
        self.row("*", "rule", "POST /api/zones speed", "max 1", "routes/zones.js", area="backend")
        self.assertEqual(len(memory._cross_page_rules_differ(self.con, "/zones/new")), 1)

    def test_the_card_names_them_as_a_hint(self):
        card = memory.card(self.con, "/zones/new")
        line = next(x for x in card.splitlines() if x.startswith("CROSS-PAGE RULES DIFFER"))
        self.assertIn("not an oracle", line)
        self.assertIn("max 130 vs /zones/:id max 150", line)
        self.assertNotIn("CROSS-PAGE", memory.card(self.con, "/zones"))

    def test_the_card_shows_three_hints_at_most(self):
        for i in range(5):
            self.row("/zones/new", "rule", f"field{i}", "max 1", "src/New.jsx")
            self.row("/zones/:id", "rule", f"field{i}", "max 2", "src/Edit.jsx")
        line = next(x for x in memory.card(self.con, "/zones/new").splitlines() if x.startswith("CROSS-PAGE"))
        self.assertEqual(line.count(" vs /zones/:id"), 3)


class Related(GraphCase):
    """Cards, `prep` and `qa-mem impact` name the pages a change here may reach."""

    def setUp(self):
        super().setUp()
        self.calls("/zones/new", "POST /api/areas")
        self.calls("/zones", "GET /api/areas")
        self.calls("/zones/:id", "GET /api/areas/:id", "PUT /api/areas/:id")
        self.calls("/map", "GET /api/areas")
        for page in ("/incidents", "/schedules", "/zones"):
            self.row(page, "locator", "Date range", "getByLabel('Range')", "src/components/DateRangePicker.tsx")

    def line(self, route):
        return next((x for x in memory.card(self.con, route).splitlines() if x.startswith("RELATED PAGES")), "")

    def test_the_card_lists_the_related_pages_with_why(self):
        line = self.line("/zones/new")
        for want in (
            "/zones/:id (also writes PUT api/areas/:id)",
            "/map (reads GET api/areas)",
            "/zones (reads GET api/areas)",
        ):
            self.assertIn(want, line)
        self.assertIn("a change here may show there", line)
        self.assertEqual(
            self.line("/schedules"),
            "RELATED PAGES (a change here may show there; from the code map): "
            "/incidents (shares src/components/DateRangePicker.tsx); /zones (shares "
            "src/components/DateRangePicker.tsx)",
        )
        self.assertEqual(self.line("/closures"), "")

    def test_the_card_caps_the_list_at_four(self):
        for i in range(6):
            self.calls(f"/more{i}", "GET /api/areas")
        line = self.line("/zones/new")
        self.assertEqual(line.count(" (reads "), 3)  # the sibling writer first, then three readers
        self.assertIn("; +5 more (qa-mem impact '/zones/new')", line)
        self.assertIn("qa-mem impact '/zones/new'", line)

    def test_an_open_bug_on_a_page_sharing_a_component_shows_on_the_card_and_in_bug_match(self):
        h = self.bug("/incidents", "range picker drops the end date")
        card = memory.card(self.con, "/schedules")
        self.assertIn("BUGS ON PAGES SHARING AN ENDPOINT OR COMPONENT", card)
        self.assertIn(h, card.split("BUGS ON PAGES SHARING", 1)[1])
        self.assertIn(h, memory.bug_match(self.con, "/schedules", "the range picker drops the end date"))

    def test_a_layout_shared_by_many_pages_carries_no_bug_across(self):
        for i in range(20):
            self.row(f"/p{i}", "locator", "Menu", "getByRole('navigation')", "src/AppLayout.tsx")
        self.bug("/p0", "menu overlaps the title")
        self.assertNotIn("BUGS ON PAGES SHARING", memory.card(self.con, "/p1"))

    def test_prep_context_names_the_impacted_routes(self):
        ctx = memory.ticket_context(self.con, "Speed limit can be a decimal on /zones/new")
        self.assertEqual([x["route"] for x in ctx["candidate_routes"]][:1], ["/zones/new"])
        impacted = {x["route"]: x["why"] for x in ctx["impacted_routes"]}
        self.assertEqual(set(impacted), {"/zones", "/zones/:id", "/map"})
        self.assertEqual(impacted["/zones"], "reads GET api/areas (via /zones/new)")
        self.assertNotIn("/zones/new", impacted)

    def test_impacted_routes_are_capped_at_six(self):
        for i in range(7):
            self.calls(f"/more{i}", "GET /api/areas")
        ctx = memory.ticket_context(self.con, "Speed limit on /zones/new")
        self.assertEqual(len(ctx["impacted_routes"]), 6)
        self.assertEqual(memory.ticket_context(self.con, "nothing known")["impacted_routes"], [])

    def test_impact_of_a_route_json_and_text(self):
        code, out, _ = cli("impact", "/zones/new", "--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["route"], "/zones/new")
        self.assertEqual([x["route"] for x in data["resource_siblings"]], ["/zones/:id"])
        self.assertEqual([x["route"] for x in data["resource_consumers"]], ["/map", "/zones"])
        self.assertEqual(data["shared_components"], [])
        code, out, _ = cli("impact", "/schedules")
        self.assertIn("SHARED COMPONENTS", out)
        self.assertIn("/incidents (src/components/DateRangePicker.tsx)", out)

    def test_impact_of_an_endpoint_lists_its_callers_and_the_resource_around_it(self):
        code, out, _ = cli("impact", "PUT", "/api/areas/42", "--json")
        data = json.loads(out)
        self.assertEqual(data["endpoint"], "PUT api/areas/:id")
        self.assertEqual(data["callers"], ["/zones/:id"])
        self.assertEqual([x["route"] for x in data["resource_siblings"]], ["/zones/new"])
        self.assertEqual([x["route"] for x in data["resource_consumers"]], ["/map", "/zones"])

    def test_impact_names_a_page_it_does_not_know_and_wants_a_target(self):
        self.assertIn("no page matches", cli("impact", "/nowhere")[1])
        self.assertEqual(cli("impact")[0], 2)
        self.assertEqual(cli("impact", "/zones", "--bogus")[0], 2)

    def test_impact_of_a_module_page_is_its_shell_routes(self):
        self.mount("/reports/zones", "/m/zones")
        self.calls("/m/zones", "PUT /api/areas/:id")
        out = json.loads(cli("impact", "/reports/zones", "--json")[1])
        self.assertEqual(out["module"], "m1 at /m/zones")
        self.assertIn("/zones/new", [x["route"] for x in out["resource_siblings"]])


class EntryChain(GraphCase):
    """The way from a page with no record id to a detail page, over the stored transitions (BFS, 4 hops at most)."""

    def go(self, a, b, trigger=""):
        with self.con:
            for r in (a, b):
                self.con.execute("INSERT OR IGNORE INTO pages(route) VALUES(?)", (r,))
            self.con.execute(
                "INSERT INTO transitions VALUES(?,?,?,?,?,?,?)", (a, b, trigger, None, None, "1", core.now())
            )

    def test_a_two_hop_detail_route_has_its_list_page_as_entry(self):
        self.go("/incidents", "/incidents/:id", "row click")
        self.go("/incidents/:id", "/incidents/:id/history", "History tab")
        self.assertEqual(
            memory.entry_chain(self.con, "/incidents/:id/history"),
            [
                {"from": "/incidents", "to": "/incidents/:id", "trigger": "row click"},
                {"from": "/incidents/:id", "to": "/incidents/:id/history", "trigger": "History tab"},
            ],
        )
        self.assertEqual(
            memory.entry_chain(self.con, "/incidents/:id"),
            [{"from": "/incidents", "to": "/incidents/:id", "trigger": "row click"}],
        )

    def test_a_route_with_no_id_needs_no_chain_and_an_unreached_one_has_none(self):
        self.go("/incidents", "/incidents/:id")
        self.assertEqual(memory.entry_chain(self.con, "/incidents"), [])
        self.assertEqual(memory.entry_chain(self.con, "/zones/:id"), [])

    def test_cycles_stop(self):
        self.go("/a/:id", "/b/:id")
        self.go("/b/:id", "/a/:id")
        self.assertEqual(memory.entry_chain(self.con, "/a/:id"), [])
        self.go("/home", "/a/:id")
        self.assertEqual([h["from"] for h in memory.entry_chain(self.con, "/b/:id")], ["/home", "/a/:id"])

    def test_the_chain_is_four_hops_at_most_and_the_shortest_way_wins(self):
        for a, b in (
            ("/l", "/x1/:id"),
            ("/x1/:id", "/x2/:id"),
            ("/x2/:id", "/x3/:id"),
            ("/x3/:id", "/x4/:id"),
            ("/x4/:id", "/x5/:id"),
        ):
            self.go(a, b)
        self.assertEqual(len(memory.entry_chain(self.con, "/x4/:id")), 4)
        self.assertEqual(memory.entry_chain(self.con, "/x5/:id"), [])  # five hops
        self.go("/shortcut", "/x4/:id")
        self.assertEqual(
            memory.entry_chain(self.con, "/x4/:id"), [{"from": "/shortcut", "to": "/x4/:id", "trigger": ""}]
        )

    def test_an_entry_on_another_host_is_never_used(self):
        self.go("https://evil.example/list", "/x/:id")
        self.assertEqual(memory.entry_chain(self.con, "/x/:id"), [])

    def test_gaps_gives_a_deep_route_its_static_entry_page(self):
        self.go("/incidents", "/incidents/:id", "row click")
        self.go("/incidents/:id", "/incidents/:id/history", "History tab")
        self.row("/incidents/:id/history", "rule", "from", "required", "src/History.jsx")
        data = json.loads(memory.gaps(self.con, as_json=True))
        deep = next(p for p in data["pages"] if p["route"] == "/incidents/:id/history")
        self.assertEqual((deep["entry_route"], deep["entry_trigger"]), ("/incidents", "row click"))
        self.assertEqual([h["to"] for h in deep["entry_chain"]], ["/incidents/:id", "/incidents/:id/history"])
        self.assertNotIn("/incidents/:id/history", data["needs_id"])
        line = next(x for x in memory.gaps(self.con).splitlines() if "/incidents/:id/history" in x)
        self.assertIn("(open from /incidents: row click, then History tab)", line)

    def test_gaps_names_a_detail_route_nothing_leads_to_as_needing_an_id(self):
        self.go("/a/:id", "/a/:id/x")  # reached only from a detail page nobody reached
        self.row("/a/:id/x", "rule", "from", "required", "src/X.jsx")
        data = json.loads(memory.gaps(self.con, as_json=True))
        self.assertIn("/a/:id/x", data["needs_id"])


if __name__ == "__main__":
    unittest.main()
