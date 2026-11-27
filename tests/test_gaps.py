"""`qa-mem gaps`, `blindspots`, `hotspots` and `history` (qa_sdet.gaps), and the observed endpoints they read."""

import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from support import MemBase, core, git, git_repo, run
from support import cli as qa_mem

from qa_sdet import cards, cli, gaps, ingest, memory


class Gaps(MemBase):
    """`qa-mem gaps`: every page, the most urgent to explore first."""

    def row(self, page, kind, label, value, src, repo="/repo", area="frontend"):
        with self.con:
            if page != "*":
                self.con.execute("INSERT OR IGNORE INTO pages(route) VALUES(?)", (page,))
            self.con.execute(
                "INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                (page, kind, label, value, f"{src}:1", repo, area, core.now(), src),
            )

    def visit(self, route, build, ts):
        ingest.ingest(self.con, {"run_id": f"r-{route}-{ts}", "build": build, "pages": [{"route": route}]}, ts)

    @staticmethod
    def lines(out):
        return [x for x in out.splitlines() if x.startswith("- ")]

    def commit(self, repo, rel, when):
        os.environ["GIT_AUTHOR_DATE"] = os.environ["GIT_COMMITTER_DATE"] = when  # restored by the sandbox
        (repo / rel).write_text(when)
        git(repo, "commit", "-qam", when)

    def test_no_pages_is_a_hint(self):
        self.assertIn("/qa-sdet:qa-init", gaps.gaps(self.con))

    def test_order_never_explored_then_old_build_then_the_rest(self):
        self.row("/new", "rule", "speed", "max 130", "src/New.jsx")
        self.visit("/old", "1.0", "2026-01-01T00:00:00Z")
        self.visit("/cur", "2.0", "2026-01-02T00:00:00Z")
        self.visit("/unread", "unknown", "2026-01-03T00:00:00Z")
        out = gaps.gaps(self.con)
        self.assertIn("current build 2.0", out.splitlines()[0])
        lines = self.lines(out)
        self.assertTrue(lines[0].startswith('- /new "": never explored; 1 untested field'), lines)
        self.assertTrue(lines[1].startswith('- /old "": verified on build 1.0: re-verify; 15 untried'), lines)
        self.assertEqual({x.split()[1] for x in lines[2:]}, {"/cur", "/unread"})
        self.assertIn("build never read", next(x for x in lines if "/unread" in x))
        self.assertTrue(out.rstrip().endswith("Next: /qa-sdet:qa-explore '/new'"), out)

    def test_source_changed_since_the_last_visit(self):
        os.environ["GIT_AUTHOR_DATE"] = os.environ["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        repo = git_repo(self.t / "app", {"src/Form.jsx": "a", "src/Other.jsx": "b", "api/zones.js": "c"})
        rp = os.path.realpath(repo)
        self.row("/form", "api", "save", "POST /api/zones", "src/Form.jsx", rp)
        self.row("*", "api", "POST /api/zones", "create zone", "api/zones.js", rp, "backend")
        self.visit("/form", "1.0", "2026-01-05T00:00:00Z")  # after the first commit: nothing changed since
        self.commit(repo, "src/Other.jsx", "2026-01-06T00:00:00Z")  # another page's file
        self.assertNotIn("source changed", gaps.gaps(self.con))
        self.commit(repo, "api/zones.js", "2026-01-07T00:00:00Z")  # the backend of an endpoint the page calls
        self.assertIn("source changed (api/zones.js)", gaps.gaps(self.con))
        self.commit(repo, "src/Form.jsx", "2026-01-08T00:00:00Z")
        self.assertIn("source changed (api/zones.js, +1)", gaps.gaps(self.con))

    def test_a_change_inside_a_submodule_counts(self):
        """The scan maps a submodule's files under the parent repo; the parent's log names only the submodule folder."""
        os.environ["GIT_AUTHOR_DATE"] = os.environ["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        kit = git_repo(self.t / "kit", {"Button.jsx": "a"})
        app = git_repo(self.t / "app", {"src/Form.jsx": "a"})
        git(app, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(kit), "lib/kit")
        git(app, "commit", "-qm", "add kit")
        self.row("/form", "locator", "Save", "getByLabel('Save')", "lib/kit/Button.jsx", os.path.realpath(app))
        self.row("/other", "locator", "Name", "getByLabel('Name')", "lib/kitchen.js", os.path.realpath(app))
        self.visit("/form", "1.0", "2026-01-05T00:00:00Z")
        self.visit("/other", "1.0", "2026-01-05T00:00:00Z")
        self.commit(kit, "Button.jsx", "2026-01-06T00:00:00Z")
        git(app / "lib/kit", "fetch", "-q")
        git(app / "lib/kit", "checkout", "-q", "FETCH_HEAD")
        os.environ["GIT_AUTHOR_DATE"] = os.environ["GIT_COMMITTER_DATE"] = "2026-01-07T00:00:00Z"
        git(app, "commit", "-qam", "bump kit")  # the parent's log lists only 'lib/kit'
        out = gaps.gaps(self.con)
        self.assertIn(
            "source changed (lib/kit/Button.jsx)", next(x for x in out.splitlines() if x.startswith("- /form "))
        )
        self.assertNotIn(
            "source changed", next(x for x in out.splitlines() if x.startswith("- /other "))
        )  # 'lib/kitchen.js'

    def test_source_changes_on_a_page_whose_build_was_never_read(self):
        """No last_verified (build unreadable): the last run on the page is the visit to compare commits against."""
        os.environ["GIT_AUTHOR_DATE"] = os.environ["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        repo = git_repo(self.t / "app", {"src/Form.jsx": "a"})
        self.row("/form", "rule", "speed", "max 130", "src/Form.jsx", os.path.realpath(repo))
        ingest.ingest(
            self.con,
            {"run_id": "r1", "target": "http://qa/form", "build": "unknown", "pages": [{"route": "/form"}]},
            "2026-01-05T00:00:00Z",
        )
        self.assertNotIn("source changed", gaps.gaps(self.con))
        self.commit(repo, "src/Form.jsx", "2026-01-06T00:00:00Z")
        self.assertIn("source changed (src/Form.jsx)", gaps.gaps(self.con))

    def test_routes_with_an_id_are_not_batch_picks(self):
        """'/incidents/:id' can't be opened as is: gaps marks it, Next: and --json skip it."""
        self.row("/incidents/:id", "rule", "title", "required", "src/Detail.jsx")
        self.row("/incidents", "rule", "q", "max 50", "src/List.jsx")
        out = gaps.gaps(self.con)
        self.assertIn("(needs a record id)", next(x for x in self.lines(out) if "/incidents/:id" in x))
        self.assertTrue(out.rstrip().endswith("Next: /qa-sdet:qa-explore '/incidents'"), out)
        data = json.loads(gaps.gaps(self.con, top=1, as_json=True))
        self.assertEqual(([p["route"] for p in data["pages"]], data["needs_id"]), (["/incidents"], ["/incidents/:id"]))

    def test_the_card_names_what_changed_since_the_last_visit(self):
        os.environ["GIT_AUTHOR_DATE"] = os.environ["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        repo = git_repo(self.t / "app", {"src/Form.jsx": "a"})
        self.row("/form", "rule", "speed", "max 130", "src/Form.jsx", os.path.realpath(repo))
        self.visit("/form", "1.0", "2026-01-05T00:00:00Z")
        self.assertNotIn("CHANGED SINCE", cards.card(self.con, "/form"))
        self.commit(repo, "src/Form.jsx", "2026-01-06T00:00:00Z")
        line = next(x for x in cards.card(self.con, "/form").splitlines() if x.startswith("CHANGED SINCE"))
        self.assertIn("src/Form.jsx", line)
        self.assertIn("speed", line)  # the rule defined in the changed file

    def test_tiers_changed_new_with_forms_old_build_current_and_history_breaks_ties(self):
        self.row("/static", "locator", "Help", "getByText('Help')", "src/Help.jsx")  # never explored, nothing to submit
        self.row("/new-form", "rule", "speed", "max 130", "src/New.jsx")  # never explored, a validated form
        self.visit("/old", "1.0", "2026-01-01T00:00:00Z")
        for route in ("/quiet", "/fragile", "/regressed"):
            self.visit(route, "2.0", "2026-01-02T00:00:00Z")
        ingest.ingest(
            self.con,
            {
                "run_id": "b",
                "build": "2.0",
                "pages": [{"route": "/fragile"}],
                "bugs": [
                    {"route": "/fragile", "title": f"t{i}", "symptom": f"s{i}", "severity": "HIGH"} for i in range(2)
                ],
            },
            "2026-01-02T00:00:00Z",
        )
        ingest.ingest(
            self.con,
            {
                "run_id": "c",
                "build": "2.0",
                "pages": [{"route": "/regressed"}],
                "bugs": [{"route": "/regressed", "title": "r", "symptom": "r"}],
            },
            "2026-01-02T00:00:00Z",
        )
        self.con.execute("UPDATE bugs SET status='REGRESSED' WHERE page='/regressed'")
        self.con.execute("UPDATE bugs SET status='FILED' WHERE page='/fragile'")
        routes = [p["route"] for p in json.loads(gaps.gaps(self.con, as_json=True))["pages"]]
        self.assertEqual(routes[:3], ["/regressed", "/new-form", "/old"])
        self.assertLess(routes.index("/fragile"), routes.index("/quiet"))  # 2 HIGH bugs: a hotspot first in its tier
        self.assertEqual(routes[-1], "/static")
        info = json.loads(gaps.gaps(self.con, as_json=True))["pages"][0]
        self.assertEqual((info["tier"], info["hotspot"] >= 0), ("changed or regressed", True))

    def test_a_repo_git_cannot_read_is_named_not_fatal(self):
        self.row("/form", "rule", "speed", "max 130", "src/Form.jsx", "/nowhere/repo")
        self.visit("/form", "1.0", "2026-01-05T00:00:00Z")
        out = gaps.gaps(self.con)
        self.assertIn("/form", out)
        self.assertIn("source changes not checked for /nowhere/repo", out)

    def test_cli(self):
        self.visit("/form", "1.0", "2026-01-05T00:00:00Z")
        code, out, _ = qa_mem("gaps")
        self.assertEqual(code, 0)
        self.assertIn("/form", out)

    def test_gaps_changed_top_json_and_unrechecked_fixed(self):
        os.environ["GIT_AUTHOR_DATE"] = os.environ["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        repo = git_repo(self.t / "app", {"src/A.jsx": "a", "src/B.jsx": "b", "src/C.jsx": "c"})
        rp = os.path.realpath(repo)
        for route, src in (("/a", "src/A.jsx"), ("/b", "src/B.jsx"), ("/c", "src/C.jsx")):
            self.row(route, "rule", "x", "req", src, rp)
            self.visit(route, "1.0", "2026-01-05T00:00:00Z")
        # Before any source changes: --changed reports none cleanly
        code, out, _ = qa_mem("gaps", "--changed")
        self.assertEqual(code, 0)
        self.assertIn("no pages with source changes", out)
        code, jout, _ = qa_mem("gaps", "--changed", "--json")
        self.assertEqual((code, json.loads(jout)["pages"]), (0, []))
        # Modify A and C; mark a FIXED bug on B (un-rechecked)
        self.commit(repo, "src/A.jsx", "2026-01-06T00:00:00Z")
        self.commit(repo, "src/C.jsx", "2026-01-07T00:00:00Z")
        ingest.ingest(
            self.con,
            {
                "run_id": "fb",
                "build": "1.0",
                "pages": [{"route": "/b"}],
                "bugs": [{"route": "/b", "title": "old", "symptom": "old"}],
            },
            "2026-01-05T00:00:00Z",
        )
        h = core.bug_hash("/b", "old")
        self.con.execute("UPDATE bugs SET status='FIXED' WHERE hash=?", (h,))
        self.con.commit()
        code, jout, _ = qa_mem("gaps", "--changed", "--json")
        data = json.loads(jout)
        self.assertEqual(code, 0)
        self.assertEqual([p["route"] for p in data["pages"]], ["/a", "/c"])
        self.assertTrue(all(p["changed"] for p in data["pages"]))
        code, jtop, _ = qa_mem("gaps", "--top", "2", "--json")
        self.assertEqual((code, len(json.loads(jtop)["pages"])), (0, 2))
        code, jall, _ = qa_mem("gaps", "--json")
        by_route = {p["route"]: p for p in json.loads(jall)["pages"]}
        self.assertEqual(by_route["/b"]["unrechecked_fixed"], 1)
        self.assertIn("1 fixed bug to re-check", qa_mem("gaps")[1])
        for bad in ("0", "-2", "abc"):
            self.assertEqual(qa_mem("gaps", "--top", bad)[0], 2, bad)


class History(MemBase):
    """Bug build lifecycle, run counters, `qa-mem history` (build over build, a page's timeline) and `qa-mem hotspots`."""

    def go(self, build, ts, cases=(), bugs=(), route="/form", status="COMPLETED"):
        ingest.ingest(
            self.con,
            {
                "run_id": f"r-{route}-{ts}",
                "mode": "ticket",
                "target": route,
                "build": build,
                "status": status,
                "summary": "s",
                "pages": [{"route": route}],
                "cases": list(cases),
                "bugs": list(bugs),
            },
            ts,
        )

    def test_a_bug_knows_its_builds_and_regressions_and_runs_their_counts(self):
        bug = {"route": "/form", "title": "t", "symptom": "s", "severity": "HIGH"}
        self.go(
            "1.0",
            "2026-01-01T00:00:00Z",
            [{"id": "TC-1", "status": "PASS"}, {"id": "TC-2", "status": "FAIL"}, {"id": "TC-3", "status": "BLOCKED"}],
            [bug],
        )
        h = core.bug_hash("/form", "s")
        with mock.patch.object(core, "now", return_value="2026-01-02T00:00:00Z"):
            cli.run_cmd(self.con, "bug-status", [h, "FIXED"])
        self.go("1.1", "2026-01-03T00:00:00Z", bugs=[{**bug, "hash": h}])
        row = self.con.execute(
            "SELECT first_build, last_build, fixed_build, regressed_count, status FROM bugs"
        ).fetchone()
        self.assertEqual(tuple(row), ("1.0", "1.1", "1.0", 1, "REGRESSED"))
        counts = self.con.execute(
            "SELECT cases_pass, cases_fail, cases_blocked, bugs_found FROM runs WHERE build='1.0'"
        ).fetchone()
        self.assertEqual(tuple(counts), (1, 1, 1, 1))

    def test_history_by_build_and_by_page_and_hotspots(self):
        self.go(
            "1.0",
            "2026-01-01T00:00:00Z",
            [{"id": "TC-1", "status": "PASS"}],
            [{"route": "/form", "title": "a", "symptom": "a", "severity": "HIGH"}],
        )
        self.go(
            "1.1",
            "2026-01-05T00:00:00Z",
            [{"id": "TC-1", "status": "FAIL"}],
            [{"route": "/form", "title": "b", "symptom": "b", "severity": "LOW"}],
        )
        self.go("1.1", "2026-01-05T01:00:00Z", route="/list")
        out = gaps.history(self.con)
        lines = [x for x in out.splitlines() if x.startswith("- build")]
        self.assertTrue(
            lines[0].startswith("- build 1.1: 2 runs on 2 pages; cases 0 pass / 1 fail / 0 blocked; 1 new bug"), out
        )
        self.assertTrue(lines[1].startswith("- build 1.0: 1 run on 1 page"), out)
        page = gaps.history(self.con, "/form")
        self.assertLess(page.index("build 1.1"), page.index("build 1.0"))  # newest first
        self.assertEqual(json.loads(qa_mem("history", "--json")[1])["builds"][0]["build"], "1.1")
        hot = gaps.hotspots(self.con)
        self.assertTrue(hot.splitlines()[1].startswith("- /form: 4"), hot)  # HIGH 3 + LOW 1
        self.assertEqual(json.loads(qa_mem("hotspots", "--json")[1])[0]["page"], "/form")


class ObservedEndpoints(MemBase):
    """The calls a run's browser really made (requests-<slug>.log), kept as shapes, against the endpoints the code maps."""

    def setUp(self):
        super().setUp()
        self.config(allowedHosts=["localhost"])
        self.n = 0

    def calls(self, page, *values):
        with self.con:
            self.con.execute("INSERT OR IGNORE INTO pages(route) VALUES(?)", (page,))
            for v in values:
                self.con.execute(
                    "INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                    (page, "api", v, v, f"a{page}.js:1", "/repo", "frontend", core.now(), f"a{page}.js"),
                )

    def explore(self, lines, target="/map", build="1.0", ts=None, **extra):
        """A run of `target` whose browser made these requests ("GET /api/x => [200]" lines), ingested as the hook does."""
        self.n += 1
        d = Path(self.tmp.name) / "proj/qa-out" / f"r{self.n}" / ".data"
        d.mkdir(parents=True)
        (d / "plan.json").write_text(json.dumps({"kind": "explore", "title": target, "tickets": [], "cases": []}))
        (d / "requests-x.log").write_text(
            "".join(
                f"{i}. [{line.split()[0]}] http://localhost:8765{line.split()[1]} => {line.split('=> ')[1]} OK\n"
                for i, line in enumerate(lines, 1)
            )
        )
        f = d / "run-x.json"
        f.write_text(
            json.dumps(
                {**run(build=build), "run_id": f"r{self.n}", "target": target, "pages": [{"route": target}], **extra}
            )
        )
        t = ts or time.time() - 1000 + self.n
        os.utime(f, (t, t))
        ingest.ingest_pending(d.parent.parent)
        return d.parent

    def seen(self, route="/map"):
        return [
            tuple(x)
            for x in self.con.execute(
                "SELECT method, path, status_class, build FROM observed_endpoints WHERE route=? "
                "ORDER BY method, path, status_class",
                (route,),
            )
        ]

    def test_a_run_keeps_the_shape_and_status_class_of_each_call_never_the_query(self):
        self.explore(
            [
                "GET /api/incidents.json?token=abc123secretvalue&page=2 => [200]",
                "GET /api/zones/4521 => [404]",
                "POST /api/zones => [500]",
                "GET /api/units => [FAILED]",
                "GET /api/zones/4522 => [404]",
                "GET /api/other => [302]",
            ]
        )
        self.assertEqual(
            self.seen(),
            [
                ("GET", "/api/incidents.json", "2xx", "1.0"),
                ("GET", "/api/other", "3xx", "1.0"),
                ("GET", "/api/units", "failed", "1.0"),
                ("GET", "/api/zones/:id", "4xx", "1.0"),
                ("POST", "/api/zones", "5xx", "1.0"),
            ],
        )
        dump = json.dumps([tuple(x) for x in self.con.execute("SELECT * FROM observed_endpoints")])
        self.assertNotIn("abc123secretvalue", dump)
        self.assertNotIn("page=2", dump)

    def test_only_the_apps_own_hosts_and_no_static_files_are_kept(self):
        d = self.explore(["GET /api/x => [200]"])
        (d / ".data/requests-x.log").write_text(
            "1. [GET] http://localhost:8765/api/mine => [200] OK\n2. [GET] https://cdn.example.net/api/theirs => [200] OK\n"
            "3. [GET] http://localhost:8765/static/app.js => [200] OK\n4. [GET] http://localhost:8765/logo.png => [200] OK\n"
        )
        os.utime(d / ".data/run-x.json", (time.time(), time.time() + 1))
        (d / ".data/run-x.json.ingested").unlink()
        ingest.ingest_pending(d.parent)
        self.assertEqual(
            {x[1] for x in self.seen()}, {"/api/x", "/api/mine"}
        )  # /api/x: the first ingest of this folder

    def test_a_run_without_a_request_log_or_a_crawl_adds_nothing_and_never_fails(self):
        d = self.explore(["GET /api/x => [200]"], target="/a")
        (d / ".data/requests-x.log").unlink()
        self.assertEqual(self.seen("/a"), [("GET", "/api/x", "2xx", "1.0")])  # the first run's rows stay
        self.explore(
            ["GET /api/crawl => [200]"], target="/c", mode="map"
        )  # the read-only crawl: one log for many pages
        self.assertEqual(self.seen("/c"), [])

    def test_a_rewritten_run_does_not_duplicate_and_each_build_has_its_own_rows(self):
        self.explore(["GET /api/x => [200]"])
        self.explore(["GET /api/x => [200]"])
        self.explore(["GET /api/x => [200]"], build="2.0")
        self.assertEqual(self.seen(), [("GET", "/api/x", "2xx", "1.0"), ("GET", "/api/x", "2xx", "2.0")])

    def test_a_page_keeps_its_100_freshest_rows(self):
        self.explore([f"GET /api/r{i} => [200]" for i in range(130)])
        self.assertEqual(len(self.seen()), 100)

    def test_gaps_and_the_card_name_mapped_endpoints_the_run_never_made(self):
        self.calls(
            "/map",
            "GET /api/incidents.json",
            "GET /api/closures.json",
            "DELETE /api/zones/{id}",
            "POST /api/zones",
            "GET /api/a",
            "GET /api/b",
            "GET /api/c",
        )
        self.explore(["GET /api/incidents.json => [200]", "POST /api/zones => [200]"])
        card = cards.card(self.con, "/map")
        line = next(x for x in card.splitlines() if x.startswith("UNEXERCISED ENDPOINTS"))
        self.assertIn("not yet observed on build 1.0", line)
        self.assertIn("DELETE /api/zones/:id", line)  # writes first
        self.assertNotIn("incidents", line)  # seen, though the code writes it .json-less
        self.assertEqual(line.split("oracle): ")[1].count(";"), 3)  # at most 4 named
        self.assertIn("(+1 more)", line)
        listing = next(p for p in json.loads(gaps.gaps(self.con, as_json=True))["pages"] if p["route"] == "/map")
        self.assertEqual(len(listing["unexercised_apis"]), 5)
        self.assertEqual(listing["unexercised_apis"][0], "DELETE /api/zones/:id")
        self.assertIn("5 unexercised endpoints", gaps.gaps(self.con))
        self.assertIn("5 unexercised endpoints", listing["risk_reasons"])

    def test_nothing_is_flagged_for_a_page_with_no_observations_on_this_build_or_after_the_call_is_made(self):
        self.calls("/map", "GET /api/closures.json")
        self.assertNotIn(
            "UNEXERCISED", cards.card(self.con, "/map")
        )  # never explored with a request log: unknown, not a gap
        self.explore(["GET /api/other => [200]"])
        self.assertIn("UNEXERCISED ENDPOINTS", cards.card(self.con, "/map"))
        self.explore(["GET /api/closures.json => [200]"])
        self.assertNotIn("UNEXERCISED", cards.card(self.con, "/map"))
        self.explore(["GET /api/other => [200]"], build="2.0")  # a new build: the call is due again
        self.assertIn("GET /api/closures", cards.card(self.con, "/map"))

    def test_a_method_the_code_does_not_show_matches_any_observed_method(self):
        self.calls("/map", "api.get('zones')", "fetch(`${BASE}/units/${id}`)")
        self.explore(["GET /api/zones => [200]"])
        line = next(x for x in cards.card(self.con, "/map").splitlines() if x.startswith("UNEXERCISED"))
        self.assertNotIn("zones", line)
        self.assertIn("units", line)

    def test_unexercised_apis_order_pages_inside_their_tier_never_across_tiers(self):
        for r, n in (("/a", 2), ("/b", 4)):
            self.calls(r, *[f"GET /api/{r[1]}{i}" for i in range(n)])
        self.explore(["GET /api/other => [200]"], target="/a")
        self.explore(["GET /api/other => [200]"], target="/b")
        order = lambda: [p["route"] for p in json.loads(gaps.gaps(self.con, as_json=True))["pages"]]
        self.assertEqual(order(), ["/b", "/a"])  # the same tier: more unexercised calls first
        with self.con:
            self.con.execute(
                "INSERT INTO bugs(hash,page,title,status,severity,first_seen,last_seen,symptom) "
                "VALUES('h','/a','t','REGRESSED','HIGH','x','x','s')"
            )
        self.assertEqual(order(), ["/a", "/b"])  # a regressed bug is a higher tier, whatever /b leaves untried

    def test_an_old_database_gains_the_table_and_forgetting_a_page_clears_it(self):
        self.con.execute("DROP TABLE observed_endpoints")
        self.con.commit()
        con = core.connect()  # as a 0.4 DB of before this table: created on connect
        self.assertEqual(con.execute("SELECT count(*) FROM observed_endpoints").fetchone()[0], 0)
        con.close()
        self.explore(["GET /api/x => [200]"])
        memory.forget_page(self.con, "/map", force=True)
        self.assertEqual(self.seen(), [])


class BlindSpots(ObservedEndpoints):
    """What the runs keep leaving unchecked: the not_tested ledger and `qa-mem blindspots`."""

    def defer(self, *items, target="/map", status="COMPLETED", **extra):
        return self.explore(["GET /api/x => [200]"], target=target, not_tested=list(items), status=status, **extra)

    def rows(self, page="/map"):
        return [
            tuple(x)
            for x in self.con.execute(
                "SELECT category, text, seen_runs, resolved FROM blind_spots WHERE page=? ORDER BY category, text",
                (page,),
            )
        ]

    def test_a_deferral_repeated_by_later_runs_is_counted_once_per_run(self):
        item = {"category": "needs_setup", "text": "error-handling needs --faults"}
        d = self.defer(item, "no second account")
        self.assertEqual(
            self.rows(), [("needs_setup", "error-handling needs --faults", 1, 0), ("other", "no second account", 1, 0)]
        )
        (d / ".data/run-x.json.ingested").unlink()
        ingest.ingest_pending(d.parent)  # the same run again: not a second sighting
        self.assertEqual(self.rows()[0][2], 1)
        self.defer({**item, "text": "Error-handling  needs --faults"})  # a later run, the same words
        self.assertEqual(self.rows()[0], ("needs_setup", "error-handling needs --faults", 2, 0))

    def test_a_deferral_a_completed_later_run_no_longer_makes_is_resolved_and_comes_back_unresolved(self):
        self.defer({"category": "not_reached", "text": "the export dialog"})
        self.defer({"category": "needs_setup", "text": "admin role"})
        self.assertEqual(
            self.rows(), [("needs_setup", "admin role", 1, 0), ("not_reached", "the export dialog", 1, 1)]
        )  # gone
        self.defer({"category": "not_reached", "text": "the export dialog"})
        self.assertEqual(self.rows(), [("needs_setup", "admin role", 1, 1), ("not_reached", "the export dialog", 2, 0)])
        self.defer(status="INCOMPLETE")  # an unfinished run proves nothing
        self.assertEqual([r[3] for r in self.rows()], [1, 0])

    def test_a_crawl_and_a_blocked_run_leave_the_ledger_alone_and_it_is_capped_per_page(self):
        self.defer("x", mode="map")
        self.defer("y", status="BLOCKED")
        self.assertEqual(self.rows(), [])
        self.defer(*[f"item {i}" for i in range(60)])
        self.assertEqual(len(self.rows()), 50)

    def test_blindspots_lists_the_four_kinds_for_a_page_and_as_json(self):
        self.calls("/map", "GET /api/closures.json", "POST /api/zones")
        with self.con:
            self.con.execute(
                "INSERT INTO page_states(page,state_key,kind,trigger,status,build,ts) VALUES('/map','tab: Layers','tab','t','unvisited','1.0','x')"
            )
        faults = {"category": "needs_setup", "text": "error-handling needs --faults"}
        self.explore(
            [
                "POST /api/zones => [200]",
                "POST /api/zones => [200]",
                "PUT /api/units => [200]",
                "PUT /api/units => [422]",
            ],
            not_tested=[faults, {"category": "safety", "text": "bulk delete"}],
        )
        self.defer(faults)
        out = gaps.blindspots(self.con, "/map")
        self.assertIn("/map", out)
        self.assertIn("GET /api/closures", out)  # mapped, never observed
        self.assertIn(
            "POST /api/zones", out.split("write")[1]
        )  # only ever 2xx: its server-side validation was never provoked
        self.assertNotIn("PUT /api/units", out.split("write")[1])  # saw a 422
        self.assertIn("tab: Layers", out)
        self.assertIn("error-handling needs --faults (2 runs)", out)
        self.assertNotIn("bulk delete", out)  # told once only: not recurring
        page = json.loads(gaps.blindspots(self.con, "/map", as_json=True))["pages"][0]
        self.assertEqual(page["route"], "/map")
        self.assertEqual(page["unexercised_apis"], ["GET /api/closures"])
        self.assertEqual(page["untested_write_errors"], ["POST /api/zones"])
        self.assertEqual(page["unvisited_states"], ["tab: Layers"])
        self.assertEqual(
            [(x["category"], x["text"], x["runs"]) for x in page["not_tested"]],
            [("needs_setup", "error-handling needs --faults", 2)],
        )

    def test_blindspots_across_the_app_name_the_blockers_that_hit_several_pages(self):
        faults = {"category": "needs_setup", "text": "error-handling needs --faults"}
        for page in ("/a", "/b", "/c"):
            self.defer(faults, target=page)
        self.defer(faults, {"category": "needs_setup", "text": "another"}, target="/a")
        data = json.loads(gaps.blindspots(self.con, as_json=True))
        self.assertEqual(
            data["systemic"], [{"category": "needs_setup", "text": "error-handling needs --faults", "pages": 3}]
        )
        self.assertIn("error-handling needs --faults (3 pages)", gaps.blindspots(self.con))
        self.assertEqual(gaps.blindspots(self.con, "/nope"), "no blind spots recorded for /nope")

    def test_nothing_recorded_says_so_and_an_old_database_gains_the_table(self):
        self.assertEqual(gaps.blindspots(self.con), "no blind spots recorded yet")
        self.con.execute("DROP TABLE blind_spots")
        self.con.commit()
        con = core.connect()
        self.assertEqual(con.execute("SELECT count(*) FROM blind_spots").fetchone()[0], 0)
        con.close()

    def test_forgetting_a_page_clears_its_ledger(self):
        self.defer("x")
        memory.forget_page(self.con, "/map", force=True)
        self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
