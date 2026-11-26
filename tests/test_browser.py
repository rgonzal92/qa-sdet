"""The agent-free commands against the fixture app in a real browser: what the other tests' fake playwright-cli can't
show, because it never runs the page's JavaScript. Opt-in (`make test-browser`): it needs playwright-cli and a system
Chrome or Edge, and it must not run beside a real QA run (a machine has one `qa` browser session)."""

import json
import os
import shutil
import threading
import unittest

from support import ROOT, Base, load_hook

from qa_sdet import crawl, doctor, ops

serve = load_hook("fixture_serve", ROOT / "tests/fixture/serve.py")
# a system browser: Playwright's own builds live under the real HOME, which the sandbox replaces
BROWSER = next((b for b, (ok, _) in doctor.installed_browsers(("chrome", "msedge")).items() if ok), None)
FRAME = "iframe:visible >> nth={} >> internal:control=enter-frame >> :root"


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


if __name__ == "__main__":
    unittest.main()
