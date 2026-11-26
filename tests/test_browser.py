"""The agent-free commands against the fixture app in a real browser: what the other tests' fake playwright-cli can't
show, because it never runs the page's JavaScript. Opt-in (`make test-browser`): it needs playwright-cli and a system
Chrome or Edge, and it must not run beside a real QA run (a machine has one `qa` browser session)."""

import json
import os
import shlex
import shutil
import threading
import unittest

from support import ROOT, Base, cli, load_hook

from qa_sdet import core, crawl, doctor, ops, report

serve = load_hook("fixture_serve", ROOT / "tests/fixture/serve.py")
# a system browser: Playwright's own builds live under the real HOME, which the sandbox replaces
BROWSER = next((b for b, (ok, _) in doctor.installed_browsers(("chrome", "msedge")).items() if ok), None)
FRAME = crawl.FRAME


@unittest.skipUnless(
    os.environ.get("QA_SDET_BROWSER") == "1" and shutil.which("playwright-cli") and BROWSER,
    "real-browser tests: run `make test-browser` (needs playwright-cli and Chrome or Edge)",
)
class BrowserBase(Base):
    """The sandbox plus the fixture app on a free port and a config that points at it."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = serve.server()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        super().setUp()
        self.config(
            app="fixture",
            browser=BROWSER,
            qaBaseUrl=self.base,
            allowedHosts=["127.0.0.1"],
            ssoHost="login.example.com",
            buildSource={"type": "selector", "value": "#build"},
        )
        self.run_dir = self.rundir("2026-11-25-browser")
        self.cwd = os.getcwd()
        os.chdir(self.proj)  # playwright-cli writes .playwright-cli/ into the folder it runs in

    def tearDown(self):
        ops._close_qa(ops._pw_env())  # before the sandbox goes: the browser's profile lives in it
        os.chdir(self.cwd)
        super().tearDown()

    def open(self, path):
        cfg, hosts = ops._qa_config("test")
        started = ops._start_page(cfg, hosts, self.base + path)
        self.assertEqual(started["status"], "ok", started)
        return started

    def pw(self, *args):
        """The output of one playwright-cli command on the `qa` session; a failed command fails the test."""
        ok, out = crawl._pw(list(args), 60)
        self.assertTrue(ok, out)
        return out

    def js(self, script, target=None):
        return ops._unwrap_raw(self.pw("--raw", "eval", script, *([target] if target else [])))


class PlaywrightCli(BrowserBase):
    """What qa-sdet relies on playwright-cli to do. One of these failing after an upgrade names the code to revisit."""

    def test_page_start_reads_the_build_in_the_sandboxed_profile(self):
        self.assertEqual(self.open("/index.html")["build"], "1.0.0")
        self.assertIn(str(self.t), self.pw("list"))  # the profile is the sandbox's, never the real one

    def test_a_shell_version_and_a_module_version_make_one_build(self):
        sources = [{"type": "selector", "value": "#build"}, {"type": "url", "value": "/modules/version.txt"}]
        self.config(**{**json.loads(core.CONFIG.read_text()), "buildSource": sources})
        self.assertEqual(self.open("/console/schedules")["build"], "1.0.0+schedules-1.0")

    def test_a_response_body_is_printed_alone(self):  # the explorer pipes it into `qa-mem body-check`
        self.open("/gql.html")
        calls = [x for x in self.pw("requests", "--all").splitlines() if "/graphql" in x]
        self.assertEqual(len(calls), 4, calls)
        bodies = [json.loads(self.pw("response-body", x.split(".")[0].strip())) for x in calls]
        self.assertIn({"errors": [{"message": "Not allowed to read zones"}], "data": None}, bodies)
        sent = json.loads(self.pw("--raw", "request-body", calls[0].split(".")[0].strip()))
        self.assertIn("operationName", sent)
        ok, out = crawl._pw(["--raw", "response-body", "9999"], 60)  # a request the browser doesn't hold
        self.assertEqual((ok, "not found" in out), (False, True), out)

    def test_a_binary_response_body_is_saved_to_a_file(self):
        self.open("/gql.html")
        n = next(x for x in self.pw("requests", "--all").splitlines() if "pixel.png" in x).split(".")[0].strip()
        out = self.pw("--raw", "response-body", n).strip()
        self.assertRegex(out, r"\.playwright-cli/response-.*\.png$")
        self.assertTrue((self.proj / out).resolve().is_file())

    def test_only_a_visible_frame_matches_the_visible_selector(self):
        self.open("/console/schedules")  # four iframes: two hidden modules, the shown one, a 0x0 session frame
        self.assertEqual(self.js("document.querySelectorAll('iframe').length"), "4")
        self.assertEqual(self.js("el => el.ownerDocument.title", FRAME.format(0)), "Schedules")
        ok, out = crawl._pw(["--raw", "eval", "el => 1", FRAME.format(1)], 60)
        self.assertEqual((ok, "does not match any elements" in out), (False, True), out)

    def test_an_eval_on_a_frame_reads_the_frames_document(self):  # the tips file's `el => (<script>)` wrap
        self.open("/console/schedules")
        self.assertEqual(self.js("el => document.title + ' ' + location.pathname", FRAME.format(0)),
                         "Schedules /modules/schedules.html")  # fmt: skip

    def test_a_hash_route_is_a_navigation(self):
        self.open("/spa.html#/list")
        self.assertEqual(self.js("document.getElementById('h').textContent"), "Devices")
        self.pw("goto", self.base + "/spa.html#/items/42")
        self.assertEqual(self.js("document.getElementById('h').textContent"), "Device 42")

    def test_role_locators_match_as_the_crawl_assumes(self):
        self.open("/spa.html")
        tag = "el => el.tagName"
        self.assertEqual(self.js(tag, "getByRole('searchbox', { name: 'Find a device' })"), "INPUT")
        self.assertEqual(self.js(tag, '[data-qa-item="close-btn"]'), "BUTTON")
        for missing in ("getByRole('textbox')", "getByRole('button', { name: 'Ghost' })", "getByTestId('close-btn')"):
            ok, out = crawl._pw(["--raw", "eval", tag, missing], 60)
            self.assertEqual((ok, "does not match any elements" in out), (False, True), missing)


class Crawl(BrowserBase):
    """`qa-mem crawl` and `qa-mem discover` on the fixture's shell: three module iframes stay mounted, one is shown."""

    SHELL = (
        ("/console/schedules", "route", "Schedules", "module schedules at /modules/schedules.html"),
        ("/console/preferences", "route", "Preferences", "module preferences at /modules/preferences.html"),
        ("/console/schedules", "locator", "Shell preview", "getByRole('button', { name: 'Preview' })"),
        ("/modules/schedules.html", "locator", "Preview", "getByRole('button', { name: 'Preview' })"),
        ("/modules/schedules.html", "locator", "Save schedule", "getByTestId('schedule-save')"),
        ("/modules/schedules.html", "locator", "Archived tab", "getByRole('tab', { name: 'Archived' })"),
        ("/modules/schedules.html", "locator", "Sign save", "getByTestId('sign-save')"),  # the hidden Signs module's
        ("/modules/schedules.html", "locator", "Signs link", "getByRole('link', { name: 'Signs' })"),  # the shell's
        ("/modules/preferences.html", "locator", "Time zone", "getByLabel('Time zone')"),
        ("/modules/preferences.html", "locator", "UTC", "getByRole('option', { name: 'UTC' })"),
    )

    def codemap(self, *rows):
        con = core.connect()
        with con:
            con.executemany(
                "INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                [(*r, "src/x.tsx", "/repo", "frontend", "t", "s") for r in rows],
            )
        con.close()

    def crawl(self, *routes):
        """{route: the names of its confirmed locators} of a crawl of `routes`."""
        code, _, err = cli("crawl", str(self.run_dir), *routes)
        self.assertEqual(code, 0, err)
        run = json.loads((self.run_dir / ".data/run-init-crawl.json").read_text())
        self.assertEqual(run["status"], "COMPLETED", run["notes"])
        self.assertNotIn("failed", run["notes"])
        return {p["route"]: sorted(x["name"] for x in p["locators"]) for p in run["pages"]}

    def test_a_shell_route_confirms_what_its_shown_module_holds(self):
        self.codemap(*self.SHELL)
        found = self.crawl("/console/schedules", "/console/preferences")
        # not the hidden module's test id, not the shell's own link; each Preview button in its own document
        self.assertEqual(found["/console/schedules"], ["Archived tab", "Preview", "Save schedule", "Shell preview"])
        self.assertEqual(found["/console/preferences"], ["Time zone", "UTC"])  # an option of a closed select counts

    def test_a_role_locator_skips_what_assistive_technology_cannot_see(self):
        self.codemap(
            ("/spa.html", "route", "Devices", ""),
            ("/spa.html", "locator", "Ghost", "getByRole('button', { name: 'Ghost' })"),  # inside aria-hidden
            ("/spa.html", "locator", "Close", "getByRole('button', { name: 'Close' })"),
            ("/spa.html", "locator", "Close by attribute", '[data-qa-item="close-btn"]'),
        )
        self.assertEqual(self.crawl("/spa.html"), {"/spa.html": ["Close", "Close by attribute"]})

    def test_a_search_input_is_a_searchbox_not_a_textbox(self):
        self.codemap(
            ("/spa.html", "route", "Devices", ""),
            ("/spa.html", "locator", "Search", "getByRole('searchbox', { name: 'Find a device' })"),
            ("/spa.html", "locator", "Search as text", "getByRole('textbox', { name: 'Find a device' })"),
        )
        self.assertEqual(self.crawl("/spa.html"), {"/spa.html": ["Search"]})

    def test_discover_maps_hash_routes_as_pages(self):
        code, _, err = cli("discover", str(self.run_dir), self.base + "/spa.html", "--depth", "1")
        self.assertEqual(code, 0, err)
        run = json.loads((self.run_dir / ".data/run-discover.json").read_text())
        # one sample of the two device pages; the `#top` anchor is no page
        self.assertEqual([p["route"] for p in run["pages"]], ["/spa.html", "/spa.html#/list", "/spa.html#/items/42"])

    def test_discover_maps_the_shown_modules_states_onto_the_shell_page(self):
        code, _, err = cli("discover", str(self.run_dir), self.base + "/console/schedules", "--depth", "1")
        self.assertEqual(code, 0, err)
        run = json.loads((self.run_dir / ".data/run-discover.json").read_text())
        pages = {p["route"]: p for p in run["pages"]}
        self.assertEqual(sorted(pages), ["/console/preferences", "/console/schedules", "/console/signs"])
        self.assertEqual([s["key"] for s in pages["/console/schedules"]["states"]], ["tab: Active", "tab: Archived"])
        self.assertEqual(pages["/console/signs"]["states"], [])
        links = {t["to"] for p in run["pages"] for t in p["transitions"]}
        self.assertEqual(links, {"/console/signs", "/console/schedules", "/console/preferences"})  # no module URL


class Signals(BrowserBase):
    """`qa-mem body-check --all` on the fixture's GraphQL page: four calls to one endpoint, all answered 200."""

    def test_body_check_all_names_each_failed_graphql_operation(self):
        self.open("/gql.html")
        code, out, err = cli("body-check", str(self.run_dir), "--slug", "x", "--all")  # it saves the request log too
        self.assertEqual(code, 0, err)
        self.assertIn("4 bodies read, 3 with a finding", out)
        self.assertEqual((self.run_dir / ".data/requests-x.log").read_text().count("/graphql"), 4)
        t = report.triage_signals(self.run_dir, "x", {})
        self.assertEqual(
            [k for k, _, _ in t["app"]],
            [
                "POST /graphql#Zones → 200 [error body: errors: Not allowed to read zones]",
                "POST /graphql#SaveNote → 200 [error body: data.saveNote.errors: Text is too long]",
                "POST /graphql#BulkUpdate → 200 [partial failure: data.bulkUpdate.failed 1]",
            ],
        )
        self.assertEqual(t["suspect_200"], [])

    def test_an_answer_the_reload_dropped_is_gone_not_clean(self):
        self.open("/gql.html")
        self.pw("reload")  # the first load's four answers are no longer in the browser
        code, out, err = cli("body-check", str(self.run_dir), "--slug", "x", "--all")
        self.assertEqual(code, 0, err)
        self.assertIn("4 bodies read, 3 with a finding, 4 with no body left", out)
        t = report.triage_signals(self.run_dir, "x", {})
        self.assertEqual((len(t["app"]), t["suspect_200"]), (3, []))


class Fixtures(BrowserBase):
    """The planted behaviour of each fixture page, driven as the tips file tells the explorer to: the answer key in a
    form that fails when a page (or a playwright-cli verb the tips rely on) stops doing what the live runs expect."""

    def text(self, css):
        return self.js(f"(document.querySelector({json.dumps(css)}) || {{}}).textContent || ''")

    def click(self, name, role="button"):
        self.pw("click", f"getByRole('{role}', {{ name: '{name}' }})")

    def test_grid_draws_only_the_rows_in_view_and_a_filter_keeps_a_stale_page(self):
        self.open("/grid.html")
        self.assertEqual(self.text("#range"), "Showing 1-20 of 65")
        self.assertEqual(self.js("document.querySelectorAll('[role=listitem]').length"), "9")  # 20 on the page
        for _ in range(3):
            self.click("Next")
        self.assertEqual(self.text("#range"), "Showing 61-65 of 65")
        self.assertEqual(self.js("document.getElementById('next').disabled"), "true")
        self.pw("fill", "getByRole('searchbox', { name: 'Find a detector' })", "Detector 0")
        self.assertEqual(self.text("#range"), "No detectors")  # planted: 9 match, the page index stayed at 4

    def test_dashboard_filter_leaks_into_the_summary_and_a_failed_panel_says_so(self):
        self.open("/dashboard.html#/signals")
        self.pw("fill", "getByRole('searchbox', { name: 'Filter signals' })", "Signal 10")
        self.click("Summary", role="link")
        self.assertIn("Active: 8 (800%)", self.text("[aria-label=Signals]"))  # planted: the total is the filtered list
        self.assertIn("Alarms unavailable: The alarm service answered 404", self.text("#alarms [role=alert]"))
        self.click("Retry")  # a failed section says so and offers a way on: correct, and worth checking

    def test_time_field_loses_the_hour_when_typed_key_by_key_but_not_when_filled(self):
        self.open("/timefield.html")
        self.click("Edit schedule")
        self.pw("fill", "getByLabel('Hours')", "09")
        self.pw("fill", "getByLabel('Minutes')", "30")
        self.assertEqual(
            self.js("document.getElementById('hh').value + ':' + document.getElementById('mm').value"), "09:30"
        )
        self.pw("fill", "getByLabel('Hours')", "")
        self.pw("fill", "getByLabel('Minutes')", "")
        self.pw("click", "getByLabel('Hours')")  # the tips file's "Type key by key"
        self.pw("type", "09")
        self.pw("press", "Tab")
        self.pw("type", "30")
        self.assertEqual(
            self.js("document.getElementById('hh').value + ':' + document.getElementById('mm').value"), ":30"
        )
        self.assertEqual(self.text("#err"), "Enter a time as HH:MM")  # planted: the first minute digit empties the hour

    def test_escape_closes_the_outer_dialog_with_the_inner_one(self):
        self.open("/timefield.html")
        self.click("Edit schedule")
        self.click("Preview")
        self.pw("press", "Escape")
        self.assertEqual(
            self.js("document.getElementById('dialog').hidden"), "true"
        )  # planted: only the preview should close

    def test_a_report_tab_fills_in_and_a_failed_one_stays_blank(self):
        self.open("/export.html")
        self.click("Open daily report")
        self.assertIn("- 1:", self.pw("tab-list"))  # the tips file's "Second tab or popup"
        self.pw("tab-select", "1")
        for _ in range(20):  # the tab opens blank and gets its content when the report is ready
            if self.js("document.title") == "Daily report":
                break
        self.assertEqual(
            self.js("document.title + ' | ' + document.body.innerText.replace(/\\s+/g, ' ')"),
            "Daily report | Daily report 12 rows",
        )
        self.pw("tab-close", "1")
        self.pw("tab-select", "0")
        self.click("Open weekly report")
        self.pw("tab-select", "1")
        self.assertEqual(self.js("location.href + '|' + document.body.innerText"), "about:blank|")  # planted
        self.pw("tab-select", "0")
        self.assertEqual(self.text("#msg"), "")  # and the first tab says nothing

    def test_notes_plants_a_lost_edit_a_lost_draft_and_a_write_behind_an_error(self):
        self.open("/notes.html")
        name, save = "getByLabel('Name')", "Save"

        def add(text, priority="normal"):
            self.click("Add note")
            self.pw("fill", name, text)
            self.pw("select", "getByLabel('Priority')", priority)
            self.click(save)

        add("qa-sdet-n1", "high")
        self.assertEqual(self.text("#toast"), "Saved note qa-sdet-n1")
        self.click("Edit")
        self.pw("select", "getByLabel('Priority')", "normal")
        self.click(save)
        self.assertEqual(self.text("#rows td:nth-child(2)"), "normal")  # the page shows the edit and says Saved
        self.pw("reload")
        self.assertEqual(self.text("#rows td:nth-child(2)"), "high")  # planted: it was never stored
        add("reserved")
        self.assertEqual(self.text("#toast"), "Something went wrong")  # planted: no reason, the dialog is gone
        self.assertEqual(
            self.js("document.getElementById('edit').hidden + ' ' + document.getElementById('name').value"), "true "
        )
        long = "qa-sdet-" + "w" * 60
        add(long)
        self.assertEqual(self.text("#err"), "Not saved: Bad gateway")
        self.pw("reload")
        self.assertIn(long, self.text("#rows"))  # planted: stored behind the error
        self.pw("click", f"getByRole('row', {{ name: '{long}' }}).getByRole('button', {{ name: 'Delete' }})")
        self.assertEqual(self.js("document.getElementById('del').disabled"), "true")  # correct: a typed confirmation
        self.pw("fill", "getByLabel('Confirmation')", "delete")
        self.pw("click", "getByRole('dialog', { name: 'Delete note' }).getByRole('button', { name: 'Delete' })")
        self.assertNotIn(long, self.text("#rows"))


class Probe(BrowserBase):
    """The tips file's broken-output probe, run as written: in the page, and inside a frame with the `el => (…)` wrap."""

    def probe(self, target=None):
        tips = (ROOT / "references/playwright-cli-tips.md").read_text()
        block = tips.split("## Look for broken output")[1].split("```")[1]
        script = block.split('--raw eval "', 1)[1].rsplit('"', 1)[0]
        return self.js(f"el => ({script})" if target else script, target)

    def test_raw_machine_values_inside_a_frame_are_leads(self):
        self.open("/leaks.html")
        self.assertEqual(self.probe(), "none")  # the shell document holds none of it
        hits = self.probe(crawl.FRAME.format(0))
        for want in (
            "raw timestamp: 2026-11-15T14:30:00Z",
            "internal name: IN_PROGRESS",
            "over 100%: 800%",
            "no accessible name: <button>",
        ):
            self.assertIn(want, hits)

    def test_a_percentage_over_100_is_a_lead(self):
        self.open("/dashboard.html#/signals")
        self.pw("fill", "getByRole('searchbox', { name: 'Filter signals' })", "Signal 10")
        self.pw("click", "getByRole('link', { name: 'Summary' })")
        self.assertIn("over 100%: Active: 8 (800%)", self.probe())


class Recipes(BrowserBase):
    """The tips file's recipes, run word for word on the fixture pages they are for."""

    def recipe(self, title, n=0, **fill):
        """The output of the last command of the n-th recipe under `## <title>`, its placeholders filled in."""
        section = (ROOT / "references/playwright-cli-tips.md").read_text().split(f"## {title}")[1].split("\n## ")[0]
        cmd = [x for block in section.split("```")[1::2] for x in block.strip().splitlines()][n]
        for mark, value in fill.items():
            cmd = cmd.replace(f"<{mark}>", value)
        words, out = shlex.shlex(cmd, posix=True, punctuation_chars=";"), ""
        words.whitespace_split = True
        argv = []
        for w in [*words, ";"]:
            if w != ";":
                argv.append(w)
                continue
            self.assertEqual(argv[:2], ["playwright-cli", "-s=qa"], cmd)
            out, argv = self.pw(*argv[2:]), []
        return out

    def test_typing_key_by_key_shows_what_fill_hides(self):
        self.open("/timefield.html")
        self.pw("click", "getByRole('button', { name: 'Edit schedule' })")
        shown = self.recipe(
            "Type key by key", **{"locator of the first part": "getByLabel('Hours')", "css of the parts": "#time input"}
        )
        self.assertEqual(ops._unwrap_raw(shown), ":30")  # the hour typed first is gone

    def test_a_count_is_read_once_it_stops_changing(self):
        self.open("/grid.html")
        self.assertEqual(
            ops._unwrap_raw(self.recipe("Settled, not just loaded", **{"css of the count": "#range"})),
            "Showing 1-20 of 65",
        )

    def test_the_wait_recipe_returns_what_appeared(self):
        self.open("/form.html")
        self.pw("fill", "getByLabel('Zone name')", "qa-sdet-zone")
        fill = {"locator": "getByRole('button', { name: 'Save' })", "css of what appears": "#msg"}
        shown = ops._unwrap_raw(self.recipe("Settled, not just loaded", 1, **fill))
        self.assertEqual(shown, 'Saved zone "qa-sdet-zone" at 0 km/h')

    def test_a_popup_tab_is_read_and_closed(self):
        self.open("/export.html")
        self.pw("click", "getByRole('button', { name: 'Open daily report' })")
        self.assertIn("Daily report | about:blank | Daily report", ops._unwrap_raw(self.recipe("Second tab or popup")))
        self.assertIn("- 0: (current) [Reports]", self.recipe("Second tab or popup", 1))
        self.assertNotIn("- 1:", self.pw("tab-list"))


if __name__ == "__main__":
    unittest.main()
