"""The page card (qa_sdet.cards): `qa-mem card` and the states it lists."""

import json
import unittest

from support import MemBase, core, run

from qa_sdet import cards, cli, gaps, ingest, memory


class Card(MemBase):
    """`qa-mem card`: what an explorer reads about a page."""

    def test_card_shows_facts_locators_coverage_untried(self):
        ingest.ingest(
            self.con,
            run(
                facts=[{"kind": "rule", "text": "Speed 0-130"}],
                locators=[{"name": "Save", "locator": "getByRole('button')"}],
                coverage=[{"target": "speed", "heuristic": "boundary", "result": "fail"}],
            ),
        )
        c = cards.card(self.con, "form")
        self.assertIn("Speed 0-130", c)
        self.assertIn("Save=getByRole('button')", c)
        self.assertIn("boundary(speed:fail)", c)
        untried = c.split("UNTRIED: ")[1].splitlines()[0]
        self.assertNotIn("boundary", untried)
        self.assertIn("happy-path", untried)

    def rules(self, page, *labels):
        with self.con:
            for i, label in enumerate(labels):
                self.con.execute(
                    "INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                    (page, "rule", label, "max 10", f"src/F.jsx:{i}", "/repo", "frontend", core.now(), "src/F.jsx"),
                )

    @staticmethod
    def untested(card):
        line = next((x for x in card.splitlines() if x.startswith("UNTESTED TARGETS")), "")
        return line.split("): ", 1)[1] if line else ""

    def test_card_lists_code_rule_fields_no_run_covered(self):
        """One boundary-tested field must not hide the page's other validated fields."""
        self.rules("/form", "speed", "name", "start date")
        ingest.ingest(
            self.con, run(coverage=[{"target": "Speed limit field", "heuristic": "boundary", "result": "pass"}])
        )
        c = cards.card(self.con, "/form")
        self.assertEqual(self.untested(c), "name; start date")
        self.assertIn("covered on build 1.0", c)
        self.assertNotIn("boundary", c.split("UNTRIED: ")[1].splitlines()[0])  # the heuristic view is unchanged

    def test_untested_targets_match_one_way_and_ignore_blocked(self):
        self.rules("/form", "speed", "speed (2)", "start date", "name")
        ingest.ingest(
            self.con,
            run(
                coverage=[
                    {"target": "date", "heuristic": "boundary", "result": "pass"},
                    {"target": "name", "heuristic": "empty-null", "result": "blocked"},
                ]
            ),
        )
        self.assertEqual(
            self.untested(cards.card(self.con, "/form")), "name; speed; start date"
        )  # 'speed (2)': one field
        ingest.ingest(self.con, run(coverage=[{"target": "speed", "heuristic": "boundary", "result": "fail"}]))
        self.assertEqual(self.untested(cards.card(self.con, "/form")), "name; start date")

    def row(self, page, kind, label, value, src="src/F.jsx"):
        with self.con:
            self.con.execute(
                "INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    page,
                    kind,
                    label,
                    value,
                    f"{src}:1",
                    "/repo",
                    "frontend" if page != "*" else "backend",
                    core.now(),
                    src,
                ),
            )

    def test_only_an_adversarial_check_clears_a_rule(self):  # a happy-path save of a valid speed proved nothing
        self.rules("/form", "speed")
        ingest.ingest(self.con, run(coverage=[{"target": "speed", "heuristic": "happy-path", "result": "pass"}]))
        self.assertEqual(self.untested(cards.card(self.con, "/form")), "speed")
        ingest.ingest(self.con, run(coverage=[{"target": "speed", "heuristic": "invalid-format", "result": "pass"}]))
        self.assertEqual(self.untested(cards.card(self.con, "/form")), "")

    def test_linked_server_rules_are_targets_and_a_client_server_mismatch_is_hinted(self):
        self.row("/form", "api", "save", "POST /api/zones")
        self.row("/form", "rule", "speed", "max 130")
        self.row("*", "api", "POST /api/zones", "create zone", "api/zones.js")
        self.row("*", "rule", "POST /api/zones speed", "max 150", "api/zones.js")
        ingest.ingest(self.con, run())
        c = cards.card(self.con, "/form")
        self.assertIn("[server] POST /api/zones speed", self.untested(c))
        hint = next(x for x in c.splitlines() if x.startswith("CLIENT/SERVER"))
        self.assertIn("speed", hint)
        self.assertIn("max 130", hint)
        self.assertIn("max 150", hint)

    def test_camel_case_server_fields_and_negative_bounds_are_compared(self):
        self.row("/zones/new", "api", "save", "POST /api/zones")
        self.row("/zones/new", "rule", "speed", "min 0, max 130 (react-hook-form register)")
        self.row("/zones/new", "rule", "offset", "min -5, max 5")
        self.row("*", "api", "POST /api/zones", "create zone", "api/zones.js")
        self.row("*", "rule", "POST /api/zones speedLimit", "integer, min 0, max 150, required (Joi)", "api/zones.js")
        self.row("*", "rule", "POST /api/zones offset", "min 5, max 5", "api/zones.js")  # -5 vs 5: the minus counts
        ingest.ingest(self.con, {"run_id": "z", "build": "1", "pages": [{"route": "/zones/new"}]})
        differ = cards._client_server_differ(self.con, "/zones/new")
        self.assertEqual(len(differ), 2, differ)
        self.assertTrue(any(d.startswith("speed:") and "max 150" in d for d in differ), differ)
        self.assertTrue(any(d.startswith("offset:") and "min -5" in d for d in differ), differ)

    def test_equal_camel_case_bounds_and_range_text_are_not_a_mismatch(self):
        self.row("/zones/new", "api", "save", "POST /api/zones")
        self.row("/zones/new", "rule", "speed", "0-130 km/h")
        self.row("/zones/new", "rule", "start date", "min 2024-01-01")
        self.row("*", "api", "POST /api/zones", "create zone", "api/zones.js")
        self.row("*", "rule", "POST /api/zones speedLimit", "min 0, max 130 (Joi)", "api/zones.js")
        self.row("*", "rule", "POST /api/zones startDate", "min 2024-01-01", "api/zones.js")
        ingest.ingest(self.con, {"run_id": "z", "build": "1", "pages": [{"route": "/zones/new"}]})
        self.assertEqual(cards._client_server_differ(self.con, "/zones/new"), [])

    def zones(self, client, server, endpoint=True):
        """A form page calling POST /api/zones: its rules, the backend's rules (and the endpoint row unless endpoint=False)."""
        self.row("/zones/new", "api", "save", "POST /api/zones")
        for label, value in client.items():
            self.row("/zones/new", "rule", label, value)
        if endpoint:
            self.row("*", "api", "POST /api/zones", "create zone", "api/zones.js")
        for label, value in server.items():
            self.row("*", "rule", f"POST /api/zones {label}", value, "api/zones.js")
        ingest.ingest(self.con, {"run_id": "z", "build": "1", "pages": [{"route": "/zones/new"}]})
        return cards._client_server_differ(self.con, "/zones/new")

    def test_the_fixture_rules_differ_in_what_the_client_leaves_out(self):  # tests/fixture-src: the real values
        differ = self.zones(
            {"speed": "min 0, max 130", "name": "required, maxLength 80"},
            {"speedLimit": "integer, min 0, max 130, required (Joi)", "name": "trim, min 1, max 80, required (Joi)"},
        )
        self.assertEqual(len(differ), 2, differ)
        self.assertTrue(
            any(d.startswith("speed:") and "server only: integer, required" in d and "max" not in d for d in differ),
            differ,
        )
        self.assertTrue(
            any(
                d.startswith("name:") and "server only: trim" in d and "required" not in d and "min 1" not in d
                for d in differ
            ),
            differ,
        )  # equal max 80 and required, and min 1 is the server's way of saying required

    def test_a_limit_only_the_client_has_or_only_the_server_has(self):
        differ = self.zones(
            {"speed": "max 130", "age": "required"}, {"speedLimit": "integer", "age": "min 18, required"}
        )
        self.assertEqual(len(differ), 2, differ)
        self.assertTrue(
            any(d.startswith("speed:") and "client only: max 130" in d and "server only: integer" in d for d in differ),
            differ,
        )
        self.assertTrue(any(d.startswith("age:") and "server only: min 18" in d for d in differ), differ)

    def test_enum_options_and_formats_are_compared(self):
        differ = self.zones(
            {"status": "one of: draft, active", "email": "required, email format"},
            {"status": "one of: draft, active, archived (Joi)", "email": "string"},
        )
        self.assertEqual(len(differ), 2, differ)
        self.assertTrue(
            any(d.startswith("status:") and "draft, active" in d and "archived" in d for d in differ), differ
        )
        self.assertTrue(any(d.startswith("email:") and "client only: pattern, required" in d for d in differ), differ)

    def test_a_client_rule_with_no_server_rule_is_flagged_only_when_the_endpoint_is_mapped(self):
        mapped = self.zones(
            {"name": "required", "speed": "max 130"}, {"speedLimit": "max 130"}
        )  # POST /api/zones is mapped, no rule for name
        self.assertEqual(len(mapped), 1, mapped)
        self.assertTrue(mapped[0].startswith("name: client only (required)") and "POST /api/zones" in mapped[0], mapped)

    def test_a_frontend_only_map_has_no_one_sided_noise(self):
        self.assertEqual(self.zones({"name": "required", "speed": "max 130"}, {}, endpoint=False), [])

    def test_a_server_rule_the_page_has_no_rule_for_is_flagged_when_the_page_states_rules(self):
        differ = self.zones({"speed": "max 130"}, {"speedLimit": "max 130", "approved": "boolean, default false"})
        self.assertEqual(len(differ), 1, differ)
        self.assertTrue(differ[0].startswith("POST /api/zones approved") and "server only" in differ[0], differ)
        self.con.execute("DELETE FROM codemap WHERE page='/zones/new' AND kind='rule'")
        self.assertEqual(
            cards._client_server_differ(self.con, "/zones/new"), []
        )  # a page with no rules: nothing to compare to

    def test_the_card_shows_at_most_four_hints_in_one_line(self):
        client = {f"f{i}": "required" for i in range(6)}
        self.zones(client, {f"f{i}": "min 3" for i in range(6)})
        lines = [x for x in cards.card(self.con, "/zones/new").splitlines() if x.startswith("CLIENT/SERVER")]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].count("f0") + lines[0].count("f1") + lines[0].count("f2") + lines[0].count("f3"), 4)
        self.assertNotIn("f4:", lines[0])

    def test_a_server_check_does_not_clear_the_page_field_of_the_same_name(self):
        self.row("/form", "api", "save", "POST /api/zones")
        self.row("/form", "rule", "speed", "max 130")
        self.row("*", "api", "POST /api/zones", "create zone", "api/zones.js")
        self.row("*", "rule", "POST /api/zones speed", "max 150", "api/zones.js")
        ingest.ingest(
            self.con,
            run(coverage=[{"target": "[server] POST /api/zones speed", "heuristic": "boundary", "result": "pass"}]),
        )
        self.assertEqual(
            self.untested(cards.card(self.con, "/form")), "speed"
        )  # the form's own field is still untested
        ingest.ingest(
            self.con, run(coverage=[{"target": "POST /api/zones speed", "heuristic": "boundary", "result": "pass"}])
        )
        self.assertEqual(self.untested(cards.card(self.con, "/form")), "speed")  # also without the [server] prefix
        ingest.ingest(self.con, run(coverage=[{"target": "speed", "heuristic": "boundary", "result": "pass"}]))
        self.assertEqual(
            self.untested(cards.card(self.con, "/form")), ""
        )  # the form's check clears it; the server rule was checked

    def test_untested_targets_reset_on_a_new_build(self):
        self.rules("/form", "speed")
        ingest.ingest(
            self.con, run("1.0", coverage=[{"target": "speed", "heuristic": "boundary"}]), "2026-01-01T00:00:00Z"
        )
        self.assertEqual(self.untested(cards.card(self.con, "/form")), "")
        ingest.ingest(self.con, run("2.0"), "2026-01-02T00:00:00Z")
        self.assertEqual(self.untested(cards.card(self.con, "/form")), "speed")

    def test_untested_targets_line_is_capped_and_absent_without_rules(self):
        ingest.ingest(self.con, run())
        self.assertNotIn("UNTESTED TARGETS", cards.card(self.con, "/form"))
        labels = [f"field {i:02} " + "x" * 41 for i in range(12)]  # 50 characters each
        self.rules("/form", *labels)
        shown, more = self.untested(cards.card(self.con, "/form")).split("; +")
        self.assertEqual(shown.split("; "), labels[:7])  # whole labels, at most ~400 characters
        self.assertEqual(more, "5 more (qa-mem code '/form')")

    def test_untested_targets_on_a_code_only_page_name_no_build(self):
        self.rules("/zones/new", "speed")
        self.con.execute("INSERT INTO pages(route) VALUES('/zones/new')")
        line = next(x for x in cards.card(self.con, "/zones/new").splitlines() if x.startswith("UNTESTED TARGETS"))
        self.assertIn("covered on this build", line)
        self.assertNotIn("None", line)

    def test_unknown_page_card(self):
        self.assertIn("NO CARD", cards.card(self.con, "/nope"))

    def test_card_prefers_exact_then_shortest_route(self):
        for rt in ("/form/advanced", "/form"):
            ingest.ingest(self.con, {"run_id": rt, "build": "1", "pages": [{"route": rt}]})
        self.assertIn("PAGE /form ", cards.card(self.con, "form"))

    def test_route_query_is_exact(self):
        ingest.ingest(self.con, {"run_id": "a", "build": "1", "pages": [{"route": "/incidents/list"}]})
        self.assertIn("NO CARD", cards.card(self.con, "/incidents"))
        self.assertIn("matched from 'incidents'", cards.card(self.con, "incidents"))

    def test_page_name_lookup_prefers_segment_and_exact_name(self):
        for route, name in [
            ("/roadmap", "Roadmap"),
            ("/map.html", "Live map"),
            ("/zones", "Speed zones"),
            ("/z", "Zones"),
        ]:
            ingest.ingest(self.con, {"run_id": route, "build": "1", "pages": [{"route": route, "name": name}]})
        self.assertIn("PAGE /map.html", cards.card(self.con, "map"))
        self.assertIn("PAGE /zones ", cards.card(self.con, "zones"))  # the route itself first
        self.assertIn("PAGE /map.html", cards.card(self.con, "live map"))  # then the exact page name

    def test_detail_routes_share_one_card(self):
        self.assertEqual(core.norm_route("http://h/incidents/4521/?x=1"), "/incidents/:id")
        self.assertEqual(core.norm_route("/#/zones/12/"), "/#/zones/:id")
        self.assertEqual(core.norm_route("/v2/a//b"), "/v2/a/b")
        self.assertEqual(core.norm_route("https://qa.example.com/#!/incidents/4521"), "/#!/incidents/:id")
        self.assertNotEqual(core.norm_route("/#!/incidents"), core.norm_route("/#!/zones"))
        self.assertEqual(core.norm_route("/app#section"), "/app")
        for r in ['"I #5ge', "a b ?x", "/x/ 12 /y#/z ?q"]:  # idempotent, so a stored route finds itself
            self.assertEqual(core.norm_route(core.norm_route(r)), core.norm_route(r))
        ingest.ingest(
            self.con,
            {
                "run_id": "d",
                "build": "1",
                "pages": [{"route": "/incidents/4521", "facts": [{"kind": "rule", "text": "detail shows road"}]}],
            },
        )
        self.assertIn("detail shows road", cards.card(self.con, "/incidents/77"))
        self.assertEqual(
            core.bug_hash(core.norm_route("/incidents/1"), "x"), core.bug_hash(core.norm_route("/incidents/2"), "x")
        )

    def test_route_spellings_find_the_same_card(self):
        ingest.ingest(self.con, run())
        for q in ("/form/", "/form?x=1", "http://localhost:8765/form"):
            self.assertIn("PAGE /form ", cards.card(self.con, q), q)
        self.assertEqual(core.norm_route("/#/map"), "/#/map")
        with self.assertRaises(ValueError):
            ingest.ingest(self.con, run(bugs=[{"title": "no route"}]))

    def test_unparseable_url_is_no_route_not_a_traceback(self):
        self.assertEqual(core.norm_route("http://[x"), "")
        self.assertIn("NO CARD", cards.card(self.con, "http://[x"))
        with self.assertRaises(ValueError):
            ingest.ingest(self.con, {"run_id": "r", "pages": [{"route": "http://[x"}]})

    def test_card_says_when_verified_locators_did_not_fit(self):
        locs = [
            {"name": f"btn{i}", "locator": f"getByRole('button', {{ name: 'Button number {i} {'x' * 60}' }})"}
            for i in range(25)
        ]
        ingest.ingest(self.con, run("1.0", locators=locs))
        c = cards.card(self.con, "/form")
        self.assertRegex(c, r"\(\+\d+ more verified")

    def fixed_bug(self, draft="/proj/qa-out/run/bugs/BUG-1.md"):
        ingest.ingest(
            self.con,
            run(bugs=[{"route": "/form", "title": "t", "symptom": "speed 999 saved", "draft_path": draft}]),
            "2026-01-01T00:00:00Z",
        )
        h = core.bug_hash("/form", "speed 999 saved")
        cli.run_cmd(self.con, "bug-status", [h, "FIXED"])
        return h

    @staticmethod
    def fixed_line(card, h):
        return next(x for x in card.split("PREVIOUSLY FIXED", 1)[1].splitlines() if x.startswith(f"- {h} "))

    def test_fixed_bug_drafts_are_listed_by_the_bugs_command(self):
        """The explorer re-checks a fixed bug by its draft's steps: `qa-mem bugs FIXED` names the draft."""
        self.fixed_bug()
        self.assertIn("/proj/qa-out/run/bugs/BUG-1.md", memory.bugs(self.con, "FIXED"))

    def test_fixed_bug_rechecked_on_this_build_is_marked(self):
        h = self.fixed_bug(draft=None)
        ingest.ingest(
            self.con, run(coverage=[{"target": h, "heuristic": "regression", "result": "pass"}]), "2026-01-02T00:00:00Z"
        )
        c = cards.card(self.con, "/form")
        self.assertTrue(self.fixed_line(c, h).endswith(" | re-checked on build 1.0"), c)
        self.assertIn(f"other:regression({h}:pass)", c)
        self.assertEqual(
            c.split("UNTRIED: ")[1].splitlines()[0], ", ".join(core.HEURISTICS)
        )  # regression is no heuristic
        ingest.ingest(self.con, run("2.0"), "2026-01-03T00:00:00Z")  # a new build: check it again
        self.assertNotIn("re-checked", self.fixed_line(cards.card(self.con, "/form"), h))

    def test_a_newly_fixed_older_bug_is_on_the_card_gaps_counts(self):
        """bug-status and bug-sync don't touch last_seen: an older bug fixed today must still be listed to re-check."""
        ingest.ingest(
            self.con,
            run("2.0", bugs=[{"route": "/form", "title": "old", "symptom": "old bug"}]),
            "2026-01-01T00:00:00Z",
        )
        h_old = core.bug_hash("/form", "old bug")
        for i in range(6):
            s = f"newer {i}"
            ingest.ingest(
                self.con, run("2.0", bugs=[{"route": "/form", "title": s, "symptom": s}]), f"2026-01-0{i + 2}T00:00:00Z"
            )
            h = core.bug_hash("/form", s)
            cli.run_cmd(self.con, "bug-status", [h, "FIXED"])
            ingest.ingest(
                self.con,
                run("2.0", coverage=[{"target": h, "heuristic": "regression", "result": "pass"}]),
                f"2026-01-0{i + 2}T01:00:00Z",
            )
        cli.run_cmd(self.con, "bug-status", [h_old, "FIXED"])
        self.assertIn("1 fixed bug to re-check", gaps.gaps(self.con))
        c = cards.card(self.con, "/form")
        self.assertFalse(self.fixed_line(c, h_old).endswith("re-checked on build 2.0"), c)
        self.assertIn("(+2 more FIXED: qa-mem bugs FIXED)", c)

    def test_a_recheck_on_an_unreadable_build_counts_for_a_week(self):
        """Like coverage: a re-check from a run that couldn't read the build holds for 7 days, then is due again."""
        ingest.ingest(self.con, run("unknown", bugs=[{"route": "/form", "title": "t", "symptom": "s"}]), core.now())
        h = core.bug_hash("/form", "s")
        cli.run_cmd(self.con, "bug-status", [h, "FIXED"])
        ingest.ingest(
            self.con, run("unknown", coverage=[{"target": h, "heuristic": "regression", "result": "pass"}]), core.now()
        )
        self.assertEqual(gaps._unrechecked_fixed(self.con, "/form", core.UNKNOWN), 0)
        self.assertTrue(self.fixed_line(cards.card(self.con, "/form"), h).endswith("| re-checked"))
        self.con.execute("UPDATE coverage SET ts='2020-01-01T00:00:00Z'")
        self.assertEqual(gaps._unrechecked_fixed(self.con, "/form", core.UNKNOWN), 1)

    def test_card_size_is_bounded(self):
        ingest.ingest(
            self.con,
            run(
                facts=[{"kind": "rule", "text": f"fact {i} " + "y" * 280} for i in range(30)],
                locators=[{"name": f"loc{i} " + "n" * 250, "locator": "x" * 290} for i in range(30)],
                coverage=[{"heuristic": h, "target": f"t{i} " + "z" * 280} for h in core.HEURISTICS for i in range(6)],
                bugs=[
                    {
                        "route": "/form",
                        "title": "b" * 300,
                        "symptom": f"s{i} " + "q" * 280,
                        "draft_path": "/d/" + "p" * 2000,
                    }
                    for i in range(12)
                ],
            ),
        )
        with self.con:
            self.con.execute("UPDATE bugs SET status='FIXED' WHERE rowid IN (SELECT rowid FROM bugs LIMIT 6)")
        self.rules("/form", *(f"rule {i:02} " + "r" * 110 for i in range(12)))
        for page in ("/form", "/other"):  # another page on the same endpoint, with bugs of its own
            self.con.execute(
                "INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                (page, "api", "save", "POST /api/x", "a.js:1", "/repo", "frontend", core.now(), "a.js"),
            )
        ingest.ingest(
            self.con,
            {
                "run_id": "other",
                "build": "1.0",
                "pages": [{"route": "/other"}],
                "bugs": [{"route": "/other", "title": "o" * 300, "symptom": f"o{i} " + "q" * 280} for i in range(8)],
            },
        )
        c = cards.card(self.con, "/form")
        self.assertIn("BUGS ON PAGES SHARING AN ENDPOINT", c)
        self.assertLess(len(c), 15000)  # ~3.5k tokens at worst


class States(MemBase):
    """In-page states (tabs, dialogs, query views) and page-to-page transitions, recorded by explorers as they go."""

    def visit(self, build, ts, states=(), transitions=()):
        ingest.ingest(
            self.con,
            {
                "run_id": f"r-{ts}",
                "target": "/incidents",
                "build": build,
                "pages": [{"route": "/incidents", "states": list(states), "transitions": list(transitions)}],
            },
            ts,
        )

    def test_a_hash_routed_detail_page_keeps_its_sample(self):  # the sample was cut to its path: "/"
        self.visit(
            "1.0",
            "2026-01-01T00:00:00Z",
            transitions=[
                {"to": "/#/zones/12", "trigger": "row click", "sample_path": "/#/zones/12?tab=log", "kind": "link"}
            ],
        )
        row = self.con.execute("SELECT to_route, sample_path FROM transitions").fetchone()
        self.assertEqual(tuple(row), ("/#/zones/:id", "/#/zones/12"))
        self.assertIn("e.g. /#/zones/12", cards.card(self.con, "/#/zones/:id"))

    def test_unvisited_states_leads_and_the_entry_of_a_detail_page(self):
        tab = lambda name, status: {
            "key": f"tab: {name}",
            "kind": "tab",
            "trigger": f"getByRole('tab', {{ name: '{name}' }})",
            "status": status,
        }
        self.visit(
            "1.0",
            "2026-01-01T00:00:00Z",
            [
                tab("Active", "visited"),
                tab("Scheduled", "unvisited"),
                {"key": "dialog: Filter", "kind": "dialog", "trigger": "getByRole('button', { name: 'Filter' })"},
            ],
            [
                {
                    "to": "/incidents/4521",
                    "trigger": "row click",
                    "sample_path": "/incidents/4521?token=abc123secretvalue",
                    "kind": "detail-row",
                }
            ],
        )
        card = cards.card(self.con, "/incidents")
        line = next(x for x in card.splitlines() if x.startswith("UNVISITED STATES"))
        self.assertIn(
            "tab: Scheduled (getByRole('tab', { name: 'Scheduled' }))", line
        )  # with the trigger that opens it
        self.assertIn("dialog: Filter (getByRole('button', { name: 'Filter' }))", line)
        self.assertNotIn("tab: Active", line)
        self.assertIn("/incidents/:id (row click)", next(x for x in card.splitlines() if x.startswith("LEADS TO")))
        detail = cards.card(self.con, "/incidents/:id")
        self.assertIn("ENTRY: from /incidents (row click; e.g. /incidents/4521)", detail)
        self.assertNotIn("abc123secretvalue", detail)
        data = json.loads(gaps.gaps(self.con, as_json=True))
        listing = next(p for p in data["pages"] if p["route"] == "/incidents")
        self.assertEqual(listing["unvisited_states"], 2)
        self.assertIn("2 unvisited states", listing["risk_reasons"])
        self.assertIn("2 unvisited states", gaps.gaps(self.con))
        page = next(p for p in data["pages"] if p["route"] == "/incidents/:id")
        self.assertEqual((page["entry_route"], page["entry_trigger"]), ("/incidents", "row click"))
        self.assertNotIn("/incidents/:id", data["needs_id"])  # it can be opened from its list page now

    def test_a_new_build_makes_visited_states_due_again(self):
        self.visit("1.0", "2026-01-01T00:00:00Z", [{"key": "tab: Active", "kind": "tab", "status": "visited"}])
        self.assertNotIn("UNVISITED STATES", cards.card(self.con, "/incidents"))
        self.visit("2.0", "2026-01-02T00:00:00Z")
        self.assertIn("tab: Active", cards.card(self.con, "/incidents"))

    def test_a_state_visited_on_an_unreadable_build_stays_visited_for_a_week(self):
        # the user recorded build 2.0 (qa-mem stale) and runs can't read the live build: each run's visit is stored
        # on `unknown`, which never equals 2.0, so every visited state was listed again on every run
        self.visit(core.UNKNOWN, core.now(), [{"key": "tab: Active", "kind": "tab", "status": "visited"}])
        core.set_meta(self.con, "build", "2.0")
        self.assertNotIn("UNVISITED STATES", cards.card(self.con, "/incidents"))
        self.assertEqual(json.loads(gaps.gaps(self.con, as_json=True))["pages"][0]["unvisited_states"], 0)
        self.con.execute("UPDATE page_states SET ts='2026-01-01T00:00:00Z'")  # older than a week: due again
        self.assertIn("tab: Active", cards.card(self.con, "/incidents"))

    def test_a_state_visited_on_another_known_build_is_due(self):
        self.visit("1.0", core.now(), [{"key": "tab: Active", "kind": "tab", "status": "visited"}])
        core.set_meta(self.con, "build", "2.0")  # after the run: a run that reads its build records it
        self.assertIn("tab: Active", cards.card(self.con, "/incidents"))

    def test_a_state_that_deletes_or_signs_out_is_never_listed(self):  # an explorer opened "Delete" on a record
        self.visit(
            "1.0",
            "2026-01-01T00:00:00Z",
            [
                {"key": "menuitem: Delete zone", "kind": "menuitem"},
                {
                    "key": "menuitem: Actions 3",
                    "kind": "menuitem",
                    "trigger": "getByRole('menuitem', { name: 'Remove' })",
                },
                {"key": "menuitem: Sign out", "kind": "menuitem"},
                {"key": "tab: Scheduled", "kind": "tab"},
            ],
        )
        line = next(x for x in cards.card(self.con, "/incidents").splitlines() if x.startswith("UNVISITED STATES"))
        self.assertIn("tab: Scheduled", line)
        for gone in ("Delete", "Actions 3", "Sign out"):
            self.assertNotIn(gone, line)
        self.assertEqual(json.loads(gaps.gaps(self.con, as_json=True))["pages"][0]["unvisited_states"], 1)


if __name__ == "__main__":
    unittest.main()
