"""`qa-mem crawl` and `qa-mem discover`: the read-only map passes, run by the coordinator in one call (no agent, no turn
limit). A fake playwright-cli stands in for the browser: pages by URL, each with the locators it holds."""

import json
import os
import subprocess
import time
import unittest
from unittest import mock

from support import Base, cli, done

from qa_sdet import core, crawl, memory, ops

CFG = dict(
    app="x",
    browser="chrome",
    qaBaseUrl="https://qa.example.com/app",
    allowedHosts=["qa.example.com"],
    ssoHost="login.microsoftonline.com",
    buildSource={"type": "selector", "value": "footer .version"},
)
BASE = CFG["qaBaseUrl"]


def page(found=(), frames=(), status=200, host="qa.example.com", password=False, invalid=(), links=(), states=()):
    """A fake page: the locator values its top document holds, one set per iframe, and what its harvester returns."""
    return dict(
        found=set(found),
        frames=[set(f) for f in frames],
        status=status,
        host=host,
        password=password,
        invalid=set(invalid),
        links=list(links),
        states=list(states),
        frame_count=None,
    )


class Browser:
    """A fake `playwright-cli`: routes each call by verb and target, never by call order."""

    def __init__(self, pages, start=None, fail=(), on_goto=None):
        self.pages, self.fail, self.on_goto = pages, set(fail), on_goto
        self.url, self.calls = start, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        args = [a for a in cmd[1:] if not a.startswith("-s=")]
        if args[:1] == ["open"]:
            self.url = args[-1] if "://" in args[-1] else self.url
            return done(0)
        if args[:1] == ["goto"]:
            if self.on_goto:
                self.on_goto(args[1])
            if args[1] in self.fail:
                return done(1, err="Error: net::ERR_CONNECTION_REFUSED")
            self.url = args[1]
            return done(0, out=f"### Page\n- Page URL: {args[1]}")
        if args[:1] in (["requests"], ["console"]):
            return done(0, out=f"{args[0]} log\n")
        if args[:1] == ["close"]:
            return done(0)
        if args[:2] == ["--raw", "eval"]:
            return self.eval(args[2], args[3] if len(args) > 3 else None)
        raise AssertionError(f"unexpected playwright-cli call {cmd}")

    def eval(self, js, target):
        pg = self.pages.get(self.url) or page(status=404)
        state = {"host": pg["host"], "hasPassword": pg["password"], "status": pg["status"]}
        if "const spec = " in js:
            spec = json.JSONDecoder().raw_decode(js, js.index("const spec = ") + len("const spec = "))[0]
            if target:
                i = int(target.split("nth=")[1].split(" ")[0])
                if i >= len(pg["frames"]):
                    return done(1, err=f"Error: locator.evaluate: Locator '{target}' does not match any elements")
                held = pg["frames"][i]
            else:
                held = pg["found"]
            have = {json.dumps(crawl.parse_locator(v)) for v in held}
            bad = {json.dumps(crawl.parse_locator(v)) for v in pg["invalid"]}
            out = {
                "found": [k for k, s in enumerate(spec) if json.dumps(s) in have],
                "invalid": [k for k, s in enumerate(spec) if json.dumps(s) in bad],
            }
            return done(
                0,
                out=json.dumps(
                    out
                    | (
                        {}
                        if target
                        else state | {"frames": len(pg["frames"]) if pg["frame_count"] is None else pg["frame_count"]}
                    )
                ),
            )
        if "a[href]" in js:
            return done(0, out=json.dumps(state | {"links": pg["links"], "states": pg["states"]}))
        if "hasPassword" in js:  # page-start's sign-in check
            return done(0, out=json.dumps(state))
        if "footer .version" in js:
            return done(0, out="1.43.0\n")
        raise AssertionError(f"unexpected eval {js[:80]}")

    def gotos(self):
        return [c[-1] for c in self.calls if "goto" in c]

    def evals(self):
        return [c for c in self.calls if "eval" in c and "const spec = " in c[4]]


class Translate(unittest.TestCase):
    """parse_locator: the four documented forms become DOM-check specs; anything else is unchecked (None)."""

    def test_the_four_forms_translate(self):
        p = crawl.parse_locator
        self.assertEqual(p("getByTestId('save')"), ["testid", "save"])
        self.assertEqual(p('getByTestId("save")'), ["testid", "save"])
        self.assertEqual(p("getByRole('button', { name: 'Save' })"), ["role", "button", "Save", False])
        self.assertEqual(p("getByRole('button', { name: 'Save', exact: true })"), ["role", "button", "Save", True])
        self.assertEqual(p("getByRole('heading')"), ["role", "heading", None, False])
        self.assertEqual(p("getByLabel('Speed limit (km/h)')"), ["label", "Speed limit (km/h)", False])
        self.assertEqual(p("getByLabel('Speed', { exact: true })"), ["label", "Speed", True])
        self.assertEqual(p("page.getByTestId('save')"), ["testid", "save"])  # a page object's line, copied as written
        self.assertEqual(p("[name=speed]"), ["css", "[name=speed]"])
        self.assertEqual(p("#msg"), ["css", "#msg"])

    def test_quotes_reach_the_page_as_json_data(self):
        self.assertEqual(crawl.parse_locator(r"""getByLabel('Driver\'s "id"')"""), ["label", 'Driver\'s "id"', False])
        spec = crawl.parse_locator("getByTestId('x\\'); fetch(\\'//evil')")
        self.assertEqual(spec, ["testid", "x'); fetch('//evil"])
        js = crawl.check_js([spec])
        self.assertIn('const spec = [["testid", "x\'); fetch(\'//evil"]];', js)  # a JSON string literal, never code
        self.assertEqual(json.JSONDecoder().raw_decode(js, js.index("const spec = ") + 13)[0], [spec])

    def test_other_forms_are_unchecked_never_missing(self):
        for loc in (
            "getByText('Save')",
            "page.locator('#x')",
            "getByRole('button', { name: /save/i })",
            "getByRole('button', { name: 'Save' }).first()",
            "getByRole('checkbox', { checked: true })",
            "getByTestId(id)",
            "getByRole('b\"]x', { name: 'y' })",
            "",
            "getByLabel(`Name ${n}`)",
        ):
            self.assertIsNone(crawl.parse_locator(loc), loc)


class Crawl(Base):
    """`qa-mem crawl <run> [<route>...]`: every mapped locator of every static route, in one call."""

    def setUp(self):
        super().setUp()
        self.config(**CFG)
        self.run_dir = self.rundir("2026-11-25-init-crawl")
        con = core.connect()
        rows = [
            ("/zones", "route", "Zones", ""),
            ("/zones/:id", "route", "Zone", ""),
            ("/about", "route", "About", ""),
            ("/alarms", "route", "Alarms", ""),
            ("/zones", "locator", "Save", "getByTestId('save')"),
            ("/zones", "locator", "Speed", "getByLabel('Speed limit')"),
            ("/zones", "locator", "Name", "getByRole('textbox', { name: 'Name' })"),
            ("/zones", "locator", "Msg", "#msg"),
            ("/zones", "locator", "Hi", "getByText('Hi')"),
            ("/zones/:id", "locator", "Edit", "getByTestId('edit')"),
            ("/alarms", "locator", "Ack", "getByTestId('ack')"),
        ]
        with con:
            for pg, kind, label, value in rows:
                con.execute(
                    "INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                    (pg, kind, label, value, "src/x.tsx", "/repo", "frontend", "t", "s"),
                )
            con.execute(
                "INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                ("/zones", "locator", "Save", "getByTestId('save')", "e2e/zones.ts", "/repo", "tests", "t", "s"),
            )
        con.close()
        self.pages = {
            BASE: page(),
            f"{BASE}/zones": page(
                found=["getByTestId('save')", "getByLabel('Speed limit')", "#msg"],
                frames=[["getByRole('textbox', { name: 'Name' })"]],
            ),
            f"{BASE}/alarms": page(found=["getByTestId('ack')"]),
        }

    def crawl(self, *routes, browser=None):
        browser = browser or Browser(self.pages, start=BASE)
        with mock.patch.object(subprocess, "run", side_effect=browser):
            code, out, err = cli("crawl", str(self.run_dir), *routes)
        f = self.run_dir / ".data/run-init-crawl.json"
        return (
            code,
            (json.loads(out) if out.strip() else None),
            (json.loads(f.read_text()) if f.exists() else None),
            browser,
            err,
        )

    def test_crawl_checks_every_mapped_locator_of_every_static_route(self):
        code, out, run, b, err = self.crawl()
        self.assertEqual(code, 0, err)
        self.assertEqual(
            (run["mode"], run["status"], run["build"], run["env"]),
            ("map", "COMPLETED", "1.43.0", {"url": BASE, "browser": "chrome"}),
        )
        self.assertTrue(run["run_id"].startswith("map-init-crawl-"))
        self.assertEqual(b.gotos(), [f"{BASE}/alarms", f"{BASE}/zones"])  # no :id route, no route without locators
        zones = next(p for p in run["pages"] if p["route"] == "/zones")
        self.assertEqual(
            sorted(map(tuple, (x.values() for x in zones["locators"]))),  # verbatim from the code map
            sorted(
                [
                    ("Save", "getByTestId('save')"),
                    ("Speed", "getByLabel('Speed limit')"),
                    ("Msg", "#msg"),
                    ("Name", "getByRole('textbox', { name: 'Name' })"),
                ]
            ),
        )
        self.assertNotIn("name", zones)
        self.assertEqual(
            (
                out["status"],
                out["routes"],
                out["loaded"],
                out["locators_checked"],
                out["locators_confirmed"],
                out["unchecked"],
            ),
            ("COMPLETED", 2, 2, 5, 5, 1),
        )
        self.assertIn("1 unchecked", run["notes"])
        self.assertIn("/about", run["notes"])  # skipped without locators: said, not silent
        self.assertIn("crawl 2/2 /zones: 4 of 4 confirmed, 1 unchecked", err)  # progress, one line per route
        memory.check_run(json.loads(json.dumps(run)))
        self.assertEqual(cli("ingest", str(self.run_dir / ".data/run-init-crawl.json"))[0], 0)
        con = core.connect()
        self.assertEqual(con.execute("SELECT count(*) FROM locators WHERE page='/zones' AND stale=0").fetchone()[0], 4)
        con.close()
        self.assertEqual(b.calls[-1], ["playwright-cli", "-s=qa", "close"])
        self.assertEqual((self.run_dir / ".data/requests-init-crawl.log").read_text(), "requests log\n")
        self.assertEqual((self.run_dir / ".data/console-init-crawl.log").read_text(), "console log\n")

    def test_explicit_routes_limit_the_crawl_and_an_id_route_is_refused(self):
        code, out, run, b, _ = self.crawl("/zones")
        self.assertEqual((code, b.gotos(), [p["route"] for p in run["pages"]]), (0, [f"{BASE}/zones"], ["/zones"]))
        code, out, run, b, err = self.crawl("/zones/:id")
        self.assertEqual((code, b.calls), (2, []))
        self.assertIn("static", err)

    def test_frames_are_checked_by_index_and_a_missing_frame_is_no_error(self):
        self.pages[f"{BASE}/zones"]["frames"] = [[], ["getByRole('textbox', { name: 'Name' })"]]
        code, _, run, b, _ = self.crawl("/zones")
        targets = [c[5] for c in b.evals() if len(c) > 5]
        self.assertEqual(
            targets,
            [
                "iframe >> nth=0 >> internal:control=enter-frame >> :root",
                "iframe >> nth=1 >> internal:control=enter-frame >> :root",
            ],
        )
        self.assertIn("Name", [x["name"] for x in run["pages"][0]["locators"]])
        self.pages[f"{BASE}/zones"].update(frames=[], frame_count=2)  # counted, then gone before its eval
        code, _, run, b, _ = self.crawl("/zones")
        self.assertEqual(
            (code, run["status"], len(b.evals())), (0, "COMPLETED", 2)
        )  # nth=0 said no frame: nth=1 not tried
        self.assertNotIn("failed", run["notes"])

    def test_no_frame_eval_when_the_top_document_confirms_all(self):
        self.pages[f"{BASE}/alarms"]["frames"] = [["x"]]
        _, _, _, b, _ = self.crawl("/alarms")
        self.assertEqual([len(c) for c in b.evals()], [5])  # the top document only

    def test_invalid_css_from_the_page_is_unchecked(self):
        self.pages[f"{BASE}/zones"]["invalid"] = {"#msg"}
        self.pages[f"{BASE}/zones"]["found"].discard("#msg")
        _, out, run, _, _ = self.crawl("/zones")
        self.assertEqual((out["unchecked"], out["locators_checked"]), (2, 3))  # getByText, and the CSS the page refused
        self.assertNotIn("Msg", [x["name"] for x in run["pages"][0]["locators"]])

    def test_a_checkpoint_is_written_after_every_page(self):
        seen = []
        f = self.run_dir / ".data/run-init-crawl.json"

        def on_goto(url):
            if url.endswith("/zones"):
                seen.append(json.loads(f.read_text()))

        self.crawl(browser=Browser(self.pages, start=BASE, on_goto=on_goto))
        self.assertEqual(seen[0]["status"], "INCOMPLETE")
        self.assertEqual([p["route"] for p in seen[0]["pages"]], ["/alarms"])

    def test_budget_stop_at_the_reserve_saves_results_as_incomplete(self):
        con = core.connect()
        with con:
            for n in range(5):
                con.execute(
                    "INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                    (f"/p{n}", "locator", "Ack", "getByTestId('ack')", "f", "/repo", "frontend", "t", "s"),
                )
                con.execute(
                    "INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                    (f"/p{n}", "route", f"P{n}", "", "f", "/repo", "frontend", "t", "s"),
                )
                self.pages[f"{BASE}/p{n}"] = page(found=["getByTestId('ack')"])
        con.close()
        ops.marker_start(self.proj, budget=10)  # reserve 3: page-start 1, then 2 per route: 3 routes
        code, out, run, b, _ = self.crawl()
        self.assertEqual(
            (code, run["status"], run["stop_reason"], len(run["pages"])), (0, "INCOMPLETE", "budget_limit", 3)
        )
        self.assertEqual(json.loads((self.proj / "qa-out/.active").read_text())["used"], 7)
        self.assertEqual(b.calls[-1], ["playwright-cli", "-s=qa", "close"])  # the finish ran, uncharged
        self.assertTrue((self.run_dir / ".data/requests-init-crawl.log").exists())

    def test_each_browser_command_is_charged_to_the_marker(self):
        ops.marker_start(self.proj, budget=100)
        _, _, _, b, _ = self.crawl()
        charged = 1 + len(b.gotos()) + len(b.evals())  # page-start, every goto and every eval
        self.assertEqual(json.loads((self.proj / "qa-out/.active").read_text())["used"], charged)

    def test_no_marker_budget_is_unlimited(self):
        ops.marker_start(self.proj)
        code, _, run, _, _ = self.crawl()
        self.assertEqual((code, run["status"]), (0, "COMPLETED"))
        self.assertEqual(json.loads((self.proj / "qa-out/.active").read_text())["used"], 0)

    def test_another_projects_marker_is_never_charged(self):
        other = self.t / "other"
        other.mkdir()
        ops.marker_start(other, budget=5)
        with mock.patch.dict(os.environ, CLAUDE_PROJECT_DIR=str(other)):
            code, _, run, _, _ = self.crawl()
        self.assertEqual((code, run["status"]), (0, "COMPLETED"))
        self.assertEqual(json.loads((other / "qa-out/.active").read_text())["used"], 0)

    def test_deadline_passed_stops_before_any_goto(self):
        ops.marker_start(self.proj, minutes=1, t=time.time() - 120)
        code, out, run, b, _ = self.crawl()
        self.assertEqual((code, run["status"], run["stop_reason"], run["pages"]), (0, "INCOMPLETE", "time_limit", []))
        self.assertEqual(b.gotos(), [])

    def test_sign_in_page_blocks_and_keeps_the_pages_so_far(self):
        self.pages[f"{BASE}/zones"] = page(host="login.microsoftonline.com")
        code, out, run, b, _ = self.crawl()
        self.assertEqual(
            (code, run["status"], run["block_scope"], run["stop_reason"]), (1, "BLOCKED", "session", "blocked")
        )
        self.assertIn("/qa-sdet:qa-login", run["summary"])
        self.assertEqual([p["route"] for p in run["pages"]], ["/alarms"])
        self.assertEqual(b.calls[-1], ["playwright-cli", "-s=qa", "close"])

    def test_page_start_blocker_writes_a_blocked_run_file(self):
        self.pages[BASE] = page(password=True)
        code, out, run, b, _ = self.crawl()
        self.assertEqual((code, out["status"], run["status"], run["block_scope"]), (1, "BLOCKED", "BLOCKED", "session"))
        self.assertEqual(b.gotos(), [])
        memory.check_run(run)

    def test_http_error_route_is_noted_and_left_out(self):
        self.pages[f"{BASE}/alarms"] = page(status=500)
        _, out, run, _, _ = self.crawl()
        self.assertEqual([p["route"] for p in run["pages"]], ["/zones"])
        self.assertIn("/alarms: HTTP 500", run["notes"])
        self.assertEqual((run["status"], out["loaded"]), ("COMPLETED", 1))

    def test_off_host_landing_is_skipped(self):
        self.pages[f"{BASE}/alarms"] = page(found=["getByTestId('ack')"], host="evil.example.com")
        _, _, run, _, _ = self.crawl()
        self.assertEqual([p["route"] for p in run["pages"]], ["/zones"])
        self.assertIn("/alarms: landed on evil.example.com", run["notes"])

    def test_three_failures_in_a_row_stop_incomplete(self):
        con = core.connect()
        with con:
            for n in range(4):
                for kind, value in (("route", ""), ("locator", "getByTestId('ack')")):
                    con.execute(
                        "INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                        (f"/f{n}", kind, "Ack", value, "f", "/repo", "frontend", "t", "s"),
                    )
        con.close()
        fail = {f"{BASE}/f{n}" for n in range(4)}
        code, out, run, b, _ = self.crawl(browser=Browser(self.pages, start=BASE, fail=fail))
        self.assertEqual((code, run["status"]), (0, "INCOMPLETE"))
        self.assertEqual(b.gotos(), [f"{BASE}/alarms", f"{BASE}/f0", f"{BASE}/f1", f"{BASE}/f2"])
        self.assertIn("3 routes in a row failed", run["summary"])


class Discover(Base):
    """`qa-mem discover <run> [<url>] [--depth D] [--max-pages N]`: breadth-first same-host links, read-only."""

    def setUp(self):
        super().setUp()
        self.config(**CFG)
        self.run_dir = self.rundir("2026-11-25-discover")

    def discover(self, pages, *args, start=BASE):
        b = Browser(pages)
        with mock.patch.object(subprocess, "run", side_effect=b):
            code, out, err = cli("discover", str(self.run_dir), *args)
        f = self.run_dir / ".data/run-discover.json"
        return code, (json.loads(f.read_text()) if f.exists() else None), b, err

    def test_discover_is_breadth_first_to_depth(self):
        pages = {
            BASE: page(
                links=[["/app/zones", "Zones"], ["/app/alarms", "Alarms"]],
                states=[["tab", "Active"], ["tab", "Active"], ["button", ""]],
            ),
            f"{BASE}/zones": page(links=[["/app/zones/new", "New zone"]]),
            f"{BASE}/alarms": page(links=[["/app/alarms/history", "History"]]),
            f"{BASE}/zones/new": page(links=[["/app/deep", "Deep"]]),
        }
        code, run, b, err = self.discover(pages, "--depth", "1")
        self.assertEqual(code, 0, err)
        self.assertEqual(b.gotos(), [f"{BASE}/zones", f"{BASE}/alarms"])  # the start page is already open
        self.assertEqual([p["route"] for p in run["pages"]], ["/app", "/app/zones", "/app/alarms"])
        start = run["pages"][0]
        self.assertEqual(
            start["states"], [{"key": "tab: Active", "kind": "tab", "trigger": "Active", "status": "unvisited"}]
        )
        self.assertEqual(
            start["transitions"][0],
            {"to": "/app/zones", "trigger": "Zones", "kind": "link", "sample_path": "/app/zones"},
        )
        self.assertEqual(run["pages"][1]["transitions"][0]["to"], "/app/zones/new")  # depth 2: recorded, not visited
        self.assertEqual((run["mode"], run["status"]), ("map", "COMPLETED"))
        memory.check_run(run)

    def test_query_string_dropped_and_one_sample_per_detail_pattern(self):
        pages = {
            BASE: page(
                links=[
                    ["/app/incidents/12", "A"],
                    ["/app/incidents/34", "B"],
                    ["/app/list?page=2", "2"],
                    ["/app/list", "List"],
                ]
            )
        }
        _, run, b, _ = self.discover(pages)
        self.assertEqual(b.gotos(), [f"{BASE}/incidents/12", f"{BASE}/list"])
        self.assertEqual(len(run["pages"][0]["transitions"]), 4)  # every link is a transition; one sample is visited

    def test_logout_delete_and_auth_links_never_followed(self):
        pages = {
            BASE: page(
                links=[
                    ["/app/logout", "Log out"],
                    ["/app/items/7/delete", "x"],
                    ["/app/x", "Delete all"],
                    ["/app/oauth/callback", "SSO"],
                    ["/app/ok", "OK"],
                ]
            )
        }
        _, run, b, _ = self.discover(pages)
        self.assertEqual(b.gotos(), [f"{BASE}/ok"])
        self.assertEqual([t["to"] for t in run["pages"][0]["transitions"]], ["/app/ok"])

    def test_off_host_links_skipped(self):
        pages = {
            BASE: page(links=[["//evil.example.com/x", "x"], ["https://evil.example.com/y", "y"], ["app/rel", "r"]])
        }
        _, run, b, _ = self.discover(pages)
        self.assertFalse([c for c in b.calls if any("evil" in a for a in c)])
        self.assertEqual(run["pages"][0]["transitions"], [])

    def test_stops_at_max_pages(self):
        pages = {
            BASE: page(links=[[f"/app/p{n}", str(n)] for n in range(10)]),
            **{f"{BASE}/p{n}": page() for n in range(10)},
        }
        _, run, b, _ = self.discover(pages, "--max-pages", "3")
        self.assertEqual(len(run["pages"]), 3)
        self.assertEqual(len(b.gotos()), 2)

    def test_url_outside_allowed_hosts_refused_before_the_browser(self):
        code, run, b, _ = self.discover({}, "https://evil.example.com/")
        self.assertEqual((code, run, b.calls), (2, None, []))

    def test_a_blocked_start_page_blocks_the_discover(self):
        code, run, b, _ = self.discover({BASE: page(status=403)})
        self.assertEqual((code, run["status"], run["block_scope"]), (1, "BLOCKED", "page"))


class LocatorCap(Base):
    def test_two_hundred_confirmed_locators_on_one_page_survive_ingest(self):
        con = core.connect()
        locs = [{"name": f"L{n}", "locator": f"getByTestId('l{n}')"} for n in range(200)]
        memory.ingest(
            con,
            {
                "run_id": "map-1",
                "mode": "map",
                "target": BASE,
                "build": "1.0",
                "status": "COMPLETED",
                "summary": "s",
                "pages": [{"route": "/zones", "locators": locs}],
            },
        )
        self.assertEqual(con.execute("SELECT count(*) FROM locators WHERE page='/zones'").fetchone()[0], 200)
        con.close()


if __name__ == "__main__":
    unittest.main()
