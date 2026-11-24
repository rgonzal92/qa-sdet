"""v0.4 qa-mem commands: keys, rundir, marker, browser-open, prune, annotate, report + index, verdict, inbox,
setup-permissions, and ingest of qa-out/<run>/.data/run-*.json. Everything runs in temp folders (HOME, config, data)."""
import builtins, contextlib, importlib.util, io, json, os, stat, struct, subprocess, sys, tempfile, time, unittest, zlib
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
qm = SourceFileLoader("qa_mem_report", str(ROOT / "bin/qa-mem")).load_module()


def cli(*argv, stdin=None):
    """(exit code, stdout, stderr) of qa-mem main() in-process."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.object(sys, "stdin", io.StringIO(stdin or "")):
        code = qm.main(list(argv))
    return code, out.getvalue(), err.getvalue()


PLAN = {
    "kind": "ticket", "title": "STUDIO-1, STUDIO-2", "created": "2026-11-23T18:00:00Z",
    "env": {"url": "https://qa.example", "browser": "chrome"},
    "tickets": [
        {"key": "STUDIO-1", "summary": "Speed limit form", "acs": [{"id": "AC-1", "text": "Saves 0-130"},
                                                                  {"id": "AC-2", "text": "Rejects 131"}],
         "plan": {"scope": "speed field", "risks": ["rounding"], "out_of_scope": ["mobile"]}},
        {"key": "STUDIO-2", "summary": "Incident list", "acs": [{"id": "AC-1", "text": "Sorted by date"}]},
    ],
    "cases": [
        {"id": "TC-1", "ticket": "STUDIO-1", "ac": "AC-1", "title": "Save 130", "route": "/form", "priority": "P1",
         "steps": ["Open form", "Type 130 | save"], "expected": "Saved"},
        {"id": "TC-2", "ticket": "STUDIO-1", "ac": "AC-2", "title": "Reject 131", "route": "/form",
         "steps": ["Type 131"], "expected": "Error shown"},
        {"id": "TC-3", "ticket": "STUDIO-2", "ac": "AC-1", "title": "Sort", "route": "/list", "steps": ["Open"],
         "expected": "Newest first"},
        {"id": "TC-4", "ticket": "STUDIO-2", "ac": "AC-1", "title": "Ties", "route": "/list", "steps": ["Open"],
         "expected": "Stable"},
    ],
}
RUN_FORM = {"run_id": "ticket-form-1", "mode": "ticket", "target": "/form", "build": "1.42", "status": "COMPLETED",
            "summary": "s", "notes": "Explored the speed field.", "env": {"url": "https://qa.example", "browser": "chrome"},
            "pages": [{"route": "/form", "coverage": [{"target": "speed", "heuristic": "boundary", "result": "fail"},
                                                      {"target": "speed", "heuristic": "happy-path", "result": "pass"}]}],
            "cases": [{"id": "TC-1", "status": "PASS", "actual": "Saved"},
                      {"id": "TC-2", "status": "FAIL", "actual": "131 saved", "bug": "BUG-1"}],
            "questions": ["Is 0 a valid speed?"], "not_tested": ["Keyboard only: out of time"],
            "bugs": [{"route": "/form", "title": "Speed accepts 131", "severity": "HIGH", "classification": "CONFIRMED",
                      "draft_path": "bugs/BUG-1.md", "evidence": "/x/evidence/BUG-1.png", "annotated": True,
                      "ticket": "STUDIO-1"}]}
RUN_LIST = {"run_id": "ticket-list-1", "mode": "ticket", "target": "/list", "build": "1.42", "status": "COMPLETED",
            "summary": "s", "cases": [{"id": "TC-3", "status": "pass"},
                                      {"id": "TC-4", "status": "QUESTION", "notes": "Order of ties unspecified"}],
            "bugs": [{"route": "/list", "title": "Header misaligned", "severity": "LOW", "classification": "CONFIRMED",
                      "draft_path": "bugs/BUG-2.md", "annotated": False}]}


def done(code, out="", err=""):
    """A finished playwright-cli run, as subprocess.run returns it."""
    return mock.Mock(returncode=code, stdout=out, stderr=err)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.saved = (qm.CONFIG, qm.DATA_DIR, dict(os.environ))
        qm.CONFIG = self.t / "config" / "config.json"
        qm.DATA_DIR = self.t / "data" / "qa-sdet"
        os.environ.update(HOME=str(self.t / "home"), QA_MEM_DB=str(self.t / "m.db"),
                          XDG_DATA_HOME=str(self.t / "xdg"), XDG_CONFIG_HOME=str(self.t / "xdgc"))
        self.proj = self.t / "proj"
        self.proj.mkdir()

    def tearDown(self):
        qm.CONFIG, qm.DATA_DIR, env = self.saved
        os.environ.clear()
        os.environ.update(env)
        self.tmp.cleanup()

    def config(self, **cfg):
        qm.CONFIG.parent.mkdir(parents=True, exist_ok=True)
        qm.CONFIG.write_text(json.dumps(cfg))

    def rundir(self, name="2026-11-23-STUDIO-1+STUDIO-2", plan=None, runs=()):
        d = self.proj / "qa-out" / name
        for sub in ("bugs", "evidence", "repro", ".data"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        if plan is not None:
            (d / ".data/plan.json").write_text(json.dumps(plan))
        for i, (slug, r) in enumerate(runs):
            f = d / f".data/run-{slug}.json"
            f.write_text(json.dumps(r))
            os.utime(f, (time.time() - 100 + i, time.time() - 100 + i))
        return d


class TicketBugOwnership(unittest.TestCase):  # audit 3: independent review after round 50
    def test_untagged_or_mistagged_serious_bug_still_fails_the_ticket(self):
        case = {"id": "TC-1", "ticket": "S-1", "route": "/incidents", "status": "PASS", "notes": "", "actual": ""}
        serious = {"severity": "CRITICAL", "classification": "CONFIRMED", "id": "BUG-1"}
        for bug in ({**serious, "route": "/incidents/:id", "target": "/incidents"},  # a record opened from the list page
                    {**serious, "route": "/incidents", "ticket": "none"}, {**serious, "route": "/incidents", "ticket": "s-1 "}):
            self.assertEqual(qm.verdict("S-1", [case], [bug])[0], "FAIL", bug)
        other = {**serious, "route": "/elsewhere", "target": "/elsewhere"}
        self.assertEqual(qm.verdict("S-1", [case], [other])[0], "PASS")
        self.assertEqual(qm.run_bugs([("a", {"target": "/incidents", "bugs": [{"route": "/incidents/4521", "title": "t"}]})])[0]["target"],
                         "/incidents")


class TicketWithoutCases(unittest.TestCase):  # audit 3: third independent pass
    def test_bug_tagged_with_a_plan_ticket_that_has_no_cases_stays_with_it(self):
        case = lambda cid, t: {"id": cid, "ticket": t, "route": "/form", "status": "PASS", "notes": "", "actual": ""}
        cases = [case("TC-1", "S-1"), case("TC-3", "S-2")]
        bug = {"id": "BUG-1", "ticket": "S-3", "route": "/form", "severity": "HIGH", "classification": "CONFIRMED"}
        keys = ["S-1", "S-2", "S-3"]
        self.assertEqual([qm.verdict(k, cases, [bug], True, keys)[0] for k in keys], ["PASS", "PASS", "FAIL"])


class SetupPermissionsEmptyPath(Base):
    def test_empty_settings_path_is_an_error(self):
        self.assertEqual(cli("setup-permissions", "--apply", "--settings=")[0], 2)
        self.assertEqual(cli("setup-permissions", "--apply", "--settings", "")[0], 2)
        self.assertFalse((self.t / "home" / ".claude").exists())


class Slug(unittest.TestCase):
    def test_dot_leading_last_segment_keeps_its_name(self):  # audit 3 r40
        self.assertEqual([qm.route_slug(r) for r in ("/.well-known", "/form.html", "/", "/a/b.c.d")],
                         [".well-known", "form", "home", "a-b.c"])


class Keys(Base):
    def test_expansion_validation_and_order(self):
        self.assertEqual(qm.expand_keys(["1234", "ops-7,1250", "STUDIO-1234"], "studio-"),
                         ["STUDIO-1234", "OPS-7", "STUDIO-1250"])
        self.assertEqual(qm.expand_keys(["1 2,3"], "STUDIO"), ["STUDIO-1", "STUDIO-2", "STUDIO-3"])
        for bad in ("abc", "STUDIO-", "1-STUDIO", "STUDIO-12a", "S-1", "ß-12", "ﬀ-1"):  # ß.upper() is SS
            with self.assertRaises(qm.BadInput) as e:
                qm.expand_keys([bad], "STUDIO")
            self.assertIn(repr(bad), str(e.exception))
        with self.assertRaisesRegex(qm.BadInput, "jiraKeyPrefix"):
            qm.expand_keys(["1234"], None)
        self.assertEqual(qm.expand_keys(["OPS-7"], None), ["OPS-7"])  # full keys need no prefix

    def test_cli(self):
        self.config(jiraKeyPrefix="STUDIO")
        code, out, _ = cli("keys", "1234", "studio-1250")
        self.assertEqual((code, json.loads(out)), (0, ["STUDIO-1234", "STUDIO-1250"]))
        code, out, err = cli("keys", "12;rm")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("12;rm", err)
        self.assertEqual(cli("keys")[0], 2)
        self.config()
        self.assertIn("set jiraKeyPrefix", cli("keys", "5")[2])

    def test_only_ascii_digits(self):  # r7: OPS-١٢٣ was accepted and would go to Jira as is
        for bad in ("OPS-\u0661\u0662", "\u0661\u0662"):
            with self.assertRaises(qm.BadInput):
                qm.expand_keys([bad], "OPS")


class RunDir(Base):
    def test_labels(self):
        self.assertEqual(qm.run_label(["A-1", "B-2"]), "A-1+B-2")
        self.assertEqual(qm.run_label(["A-1", "A-2", "A-3", "A-4", "A-5"]), "A-1+A-2+A-3+2more")
        for route, slug in (("/form.html", "form"), ("/incidents/list", "incidents-list"), ("/incidents/4521", "incidents-id"),
                            ("/#/zones", "zones"), ("/#!/zones", "zones"), ("/", "home"), ("/a?x=1", "a")):
            self.assertEqual(qm.run_label(route=route), slug, route)
        self.assertEqual(qm.run_label(keys=["A-1"], prefix="plan-"), "plan-A-1")
        self.assertEqual(qm.run_label(label="init crawl/../x"), "init-crawl-..-x")

    def test_folders_suffix_and_git_exclude(self):
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True)
        exclude = self.proj / ".git/info/exclude"
        exclude.write_text("# mine\nqa-out")  # one entry already there, no trailing newline
        code, out, _ = cli("rundir", str(self.proj), "--keys", "AB-1,ab-2")
        run = Path(out.strip())
        self.assertEqual(code, 0)
        self.assertTrue(run.is_absolute())
        self.assertRegex(run.name, r"^\d{4}-\d{2}-\d{2}-AB-1\+AB-2$")
        self.assertEqual(run.parent, self.proj.resolve() / "qa-out")
        self.assertEqual(sorted(p.name for p in run.iterdir()), [".data", "bugs", "evidence", "repro"])
        self.assertEqual(stat.S_IMODE(run.stat().st_mode), 0o700)
        second = Path(cli("rundir", str(self.proj), "--keys", "AB-1,ab-2")[1].strip())
        third = Path(cli("rundir", str(self.proj), "--keys", "AB-1,ab-2")[1].strip())
        self.assertEqual((second.name, third.name), (run.name + "-2", run.name + "-3"))
        lines = exclude.read_text().splitlines()
        self.assertEqual(lines, ["# mine", "qa-out", ".playwright-cli/", ".playwright-mcp/"])  # added once, no dupes

    def test_non_git_and_bad_input(self):
        code, out, _ = cli("rundir", str(self.proj), "--route", "/form.html", "--prefix", "plan-")
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().endswith("-plan-form"))
        self.assertEqual(cli("rundir", str(self.proj))[0], 2)  # no label source
        self.assertEqual(cli("rundir", str(self.proj), "--keys", "AB-1", "--route", "/x")[0], 2)
        self.assertEqual(cli("rundir", str(self.t / "nope"), "--label", "x")[0], 2)
        self.assertEqual(cli("rundir", str(self.proj), "--keys", "bad key")[0], 2)

    def test_long_label_and_repeated_flag(self):  # r7: a 300-char label crashed (name too long); --keys twice kept one
        code, out, err = cli("rundir", str(self.proj), "--label", "speed limit form " * 20)
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(Path(out.strip()).is_dir())
        self.assertLessEqual(len(qm.run_label(route="/" + "a/" * 200, prefix="plan-")), 80)
        code, _, err = cli("rundir", str(self.proj), "--keys", "AB-1", "--keys", "AB-2")
        self.assertEqual(code, 2)
        self.assertIn("--keys given twice", err)


class Marker(Base):
    def test_start_unlimited_and_limited_then_stop(self):
        code, out, _ = cli("marker", "start", str(self.proj))
        m = json.loads((self.proj / "qa-out/.active").read_text())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), m)
        self.assertEqual((m["budget"], m["deadline"], m["faults"], m["used"]), (0, 0, False, 0))
        self.assertAlmostEqual(m["expires"], time.time() + 8 * 3600, delta=5)
        cli("marker", "start", str(self.proj), "--budget", "40", "--minutes", "10", "--faults")
        m = json.loads((self.proj / "qa-out/.active").read_text())
        self.assertEqual((m["budget"], m["faults"]), (40, True))
        self.assertAlmostEqual(m["deadline"], time.time() + 600, delta=5)
        self.assertEqual([p.name for p in (self.proj / "qa-out").iterdir()], [".active"])  # no temp file left
        (self.proj / "qa-out/.active.lock").write_text("")
        self.assertEqual(cli("marker", "stop", str(self.proj))[0], 0)
        self.assertEqual(list((self.proj / "qa-out").iterdir()), [])
        self.assertEqual(cli("marker", "stop", str(self.proj))[0], 0)  # already gone: fine

    def test_bad_input(self):
        for argv in (("marker",), ("marker", "go", str(self.proj)), ("marker", "start", str(self.proj), "--budget", "-1"),
                     ("marker", "start", str(self.proj), "--budget", "x"), ("marker", "start", str(self.proj), "--budget", ""), ("marker", "start", str(self.proj), "--minutes", ""), ("marker", "start", str(self.proj), "--bogus")):
            self.assertEqual(cli(*argv)[0], 2, argv)
        self.assertFalse((self.proj / "qa-out/.active").exists())

    def test_marker_the_guard_accepts(self):  # r7: --minutes 5000 wrote a deadline the guard reads as malformed
        guard = SourceFileLoader("guard_r7", str(ROOT / "hooks/guard.py")).load_module()
        cli("marker", "start", str(self.proj), "--minutes", "480", "--budget", "0")
        with mock.patch.dict(os.environ, CLAUDE_PROJECT_DIR=""):
            path, marker, error = guard.read_marker(str(self.proj))
        self.assertEqual((error, marker["budget"]), (None, 0))
        code, _, err = cli("marker", "start", str(self.proj), "--minutes", "5000")
        self.assertEqual(code, 2)
        self.assertIn("at most 480", err)
        self.assertEqual(guard.spend_budget(path, 3), None)  # the earlier marker is untouched and still spendable

    def test_missing_project_is_not_created(self):  # r7: marker start /typo made /typo/qa-out
        self.assertEqual(cli("marker", "start", str(self.t / "nope" / "proj"))[0], 2)
        self.assertFalse((self.t / "nope").exists())


class BrowserOpen(Base):
    def test_command_env_and_profile(self):
        self.config(browser="msedge")
        with mock.patch.object(qm.subprocess, "run", return_value=done(3)) as run, \
                mock.patch.dict(os.environ, PLAYWRIGHT_MCP_STORAGE_STATE="/tmp/s.json", PLAYWRIGHT_MCP_ISOLATED="true"):
            code = qm.main(["browser-open", "--session", "qa-login", "--headed", "https://qa.example/"])
        self.assertEqual(code, 3)  # playwright-cli's exit code
        profile = qm.DATA_DIR / "profiles" / "main"
        argv, kw = run.call_args
        self.assertEqual(argv[0], ["playwright-cli", "-s=qa-login", "open", "--browser=msedge", "--profile", str(profile),
                                   "--headed", "https://qa.example/"])
        self.assertNotIn("PLAYWRIGHT_MCP_STORAGE_STATE", kw["env"])
        self.assertNotIn("PLAYWRIGHT_MCP_ISOLATED", kw["env"])  # made the profile launch fail live
        close = run.call_args_list[0][0][0]
        self.assertEqual(close, ["playwright-cli", "-s=qa", "close"])  # r6: the other session on profiles/main stops
        self.assertEqual(run.call_count, 2)
        for d in (profile, profile.parent):
            self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)
        with mock.patch.object(qm.subprocess, "run", return_value=done(0)) as run:
            self.assertEqual(qm.main(["browser-open"]), 0)
        self.assertEqual(run.call_args[0][0], ["playwright-cli", "-s=qa", "open", "--browser=msedge", "--profile", str(profile)])
        self.assertEqual(run.call_args_list[0][0][0], ["playwright-cli", "-s=qa-login", "close"])

    def test_profile_in_use_says_close_the_other_browser(self):  # r6: playwright-cli's hint was --isolated (no login)
        self.config(browser="chrome")
        msg = "Error: Browser is already in use for /x/profiles/main, use --isolated to run multiple instances"
        with mock.patch.object(qm.subprocess, "run", side_effect=[done(0), done(0, out=msg)]):
            code, out, err = cli("browser-open", "https://qa.example/")
        self.assertEqual(code, 1)  # an error even if playwright-cli exited 0
        self.assertTrue(err.startswith("browser-open: Browser is already in use: another qa-sdet browser"), err)
        self.assertIn("Close the other qa-sdet browser", err)
        self.assertIn(msg, out)  # the original output is still shown
        with mock.patch.object(qm.subprocess, "run", side_effect=[OSError("gone"), done(0, out="opened")]):
            self.assertEqual(cli("browser-open")[:2], (0, "opened"))  # a failing close never blocks the open

    def test_refusals(self):
        with mock.patch.object(qm.subprocess, "run") as run:
            self.config(browser="safari")
            self.assertEqual(cli("browser-open", "https://x")[0], 2)
            self.config(browser="chrome")
            self.assertEqual(cli("browser-open", "--session", "other")[0], 2)
            self.assertEqual(cli("browser-open", "--profile", "/tmp/x")[0], 2)
            self.assertEqual(cli("browser-open", "a", "b")[0], 2)
        run.assert_not_called()

    def test_config_errors_say_where_and_signal_exit(self):  # r9: "got None" named no file; -15 became exit 241
        with mock.patch.object(qm.subprocess, "run") as run:
            qm.CONFIG.unlink(missing_ok=True)
            code, _, err = cli("browser-open")
            self.assertEqual(code, 2)
            self.assertIn(f"{qm.CONFIG} is missing", err)
            qm.CONFIG.parent.mkdir(parents=True, exist_ok=True)
            qm.CONFIG.write_text("{bad")
            self.assertIn("isn't valid JSON", cli("browser-open")[2])
            self.config(browser="opera")
            self.assertIn(f'set "browser" in {qm.CONFIG}', cli("browser-open")[2])
        run.assert_not_called()
        self.config(browser="chrome")
        with mock.patch.object(qm.subprocess, "run", return_value=done(-15)):
            self.assertEqual(qm.main(["browser-open"]), 143)


class Prune(Base):
    def test_old_regular_files_only(self):
        cli_dir, mcp = self.proj / ".playwright-cli", self.proj / ".playwright-mcp/sub"
        mcp.mkdir(parents=True)
        cli_dir.mkdir()
        old = time.time() - 8 * 86400
        for f in (cli_dir / "old.yml", mcp / "old.md"):
            f.write_text("x")
            os.utime(f, (old, old))
        (cli_dir / "new.yml").write_text("x")
        outside = self.t / "outside.txt"
        outside.write_text("keep")
        os.utime(outside, (old, old))
        (cli_dir / "link").symlink_to(outside)
        os.utime(cli_dir / "link", (old, old), follow_symlinks=False)
        code, out, _ = cli("prune", str(self.proj))
        self.assertEqual((code, out.strip()), (0, "pruned 2 old snapshot files"))
        self.assertEqual(sorted(p.name for p in cli_dir.iterdir()), ["link", "new.yml"])
        self.assertTrue(outside.exists())
        self.assertEqual(cli("prune", str(self.t / "empty"))[0], 0)  # nothing there: fine


def white_png(w, h):
    """A plain white RGB PNG, built without Pillow (it is optional at runtime)."""
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    raw = b"".join(b"\0" + b"\xff" * 3 * w for _ in range(h))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


HAVE_PIL = importlib.util.find_spec("PIL") is not None


class Annotate(Base):
    def png(self, w=200, h=120):
        p = self.rundir("r") / "evidence" / "BUG-1.png"
        p.write_bytes(white_png(w, h))
        return p

    @unittest.skipUnless(HAVE_PIL, "Pillow not installed")
    def test_box_and_label(self):
        from PIL import Image
        p = self.png()
        code, out, _ = cli("annotate", str(p), "--box", "60,60,50,30", "--box", "5.4,5,10,10", "--label", "Speed accepts 999")
        self.assertEqual(code, 0)
        img = Image.open(p).convert("RGB")
        self.assertEqual(img.getpixel((57, 75)), qm.RED)  # outline just outside the first box's left edge
        self.assertEqual(img.getpixel((59, 75)), qm.RED)  # 3px wide
        self.assertEqual(img.getpixel((60, 75)), (255, 255, 255))  # the element itself stays clear
        self.assertEqual(img.getpixel((85, 75)), (255, 255, 255))
        self.assertEqual(img.getpixel((3, 10)), qm.RED)  # second box
        tag = [img.getpixel((x, y)) for x in range(60, 200) for y in range(30, 58)]
        self.assertGreater(tag.count(qm.RED), 200)  # the label tag sits above the first box
        self.assertIn(qm.WHITE, tag)  # with white text on it
        self.assertEqual(list(p.parent.iterdir()), [p])  # written in place, no temp file left

    @unittest.skipUnless(HAVE_PIL, "Pillow not installed")
    def test_label_below_when_no_room_and_clamped(self):
        from PIL import Image
        p = self.png()
        qm.annotate(p, [(150, 0, 100, 20)], "x" * 200)  # top edge, sticks out on the right
        img = Image.open(p).convert("RGB")
        self.assertEqual(img.getpixel((148, 10)), qm.RED)  # outline just outside the box
        self.assertNotEqual(img.getpixel((199, 10)), qm.RED)  # the box runs off the image: no band there, element kept
        self.assertEqual(img.getpixel((170, 21)), qm.RED)  # the band under it is drawn
        below = [img.getpixel((x, y)) for x in range(0, 200) for y in range(23, 45)]
        self.assertGreater(below.count(qm.RED), 200)
        self.assertEqual(len(qm.short(" ".join(("x" * 200).split()), 80)), 80)

    @unittest.skipUnless(HAVE_PIL, "Pillow not installed")
    def test_label_never_covers_another_box(self):
        from PIL import Image
        p = self.png(300, 160)  # the 46-char label wraps to two lines at 300px
        # live case: the 2nd box sits right above the 1st, where the label would go
        qm.annotate(p, [(10, 60, 280, 16), (40, 30, 60, 20)], "0 closures shown although the API returned 404")
        img = Image.open(p).convert("RGB")
        inside = [img.getpixel((x, y)) for x in range(44, 97) for y in range(34, 47)]
        self.assertEqual(inside.count(qm.RED), 0)  # inside of the 2nd box stays clear
        below = [img.getpixel((x, y)) for x in range(10, 290) for y in range(83, 140)]
        self.assertGreater(below.count(qm.RED), 200)  # the label went below the boxes

    @unittest.skipUnless(HAVE_PIL, "Pillow not installed")
    def test_outline_outside_text_label_off_page_content_and_hidpi(self):  # r23: outline hid the first glyph,
        from PIL import Image, ImageDraw  # label covered the Layer dropdown, 18px label unreadable at 2x
        for s in (1, 2):
            p = self.png(1280 * s, 720 * s)
            img = Image.open(p).convert("RGB")
            d = ImageDraw.Draw(img)
            d.rectangle([49 * s, 106 * s, 124 * s, 125 * s], outline=(128, 128, 128), fill=(200, 200, 200))  # dropdown
            d.rectangle([8 * s, 141 * s, 9 * s, 157 * s], fill=(0, 0, 0))  # first glyph of "0 closures shown"
            img.save(p)
            qm.annotate(p, [(8 * s, 141 * s, 110 * s, 17 * s)], 'Closures API 404 shown as a plain "0 closures shown"')
            img = Image.open(p).convert("RGB")
            self.assertEqual(img.getpixel((8 * s, 149 * s)), (0, 0, 0), s)  # the glyph is not painted over
            self.assertEqual(img.getpixel((8 * s - 1, 149 * s)), qm.RED, s)  # the outline hugs it from outside
            drop = [img.getpixel((x, y)) for x in range(49 * s, 125 * s) for y in range(106 * s, 126 * s)]
            self.assertEqual(drop.count(qm.RED), 0, s)  # dropdown left visible
            rows = [y for y in range(img.height) if qm.WHITE in [img.getpixel((x, y)) for x in range(125 * s, 700 * s)]
                    and qm.RED in [img.getpixel((x, y)) for x in range(125 * s, 700 * s)]]
            tag = max(rows) - min(rows) + 1
            self.assertGreaterEqual(tag, 20 * s, s)  # tag grows with the image
            lw = sum(img.getpixel((x, 149 * s)) == qm.RED for x in range(0, 8 * s))
            self.assertGreaterEqual(lw, 2 * s, s)

    @unittest.skipUnless(HAVE_PIL, "Pillow not installed")
    def test_long_label_wraps_on_a_narrow_image_and_box_off_image_is_refused(self):
        from PIL import Image
        p = self.png(390, 844)
        label = "Saved at 131 km/h; max is 130 and the error message never appears on mobile view"
        qm.annotate(p, [(8, 400, 250, 20)], label)
        img = Image.open(p).convert("RGB")
        reds = [x for x in range(390) for y in range(0, 844, 2) if img.getpixel((x, y)) == qm.RED]
        self.assertLessEqual(max(reds), 389)
        tag_rows = [y for y in range(844) if sum(img.getpixel((x, y)) == qm.RED for x in range(20, 200)) > 90]
        self.assertGreater(len(tag_rows), 40)  # two lines tall, not one line clipped at the right edge
        before = p.read_bytes()
        code, _, err = cli("annotate", str(p), "--box", "800,1700,100,40", "--label", "x")  # a 2x box on a 1x image
        self.assertEqual((code, p.read_bytes()), (2, before))
        self.assertIn("outside the 390x844 image", err)

    def test_no_pillow(self):
        p = self.png()
        before = p.read_bytes()
        real = builtins.__import__

        def fake(name, *a, **k):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError(name)
            return real(name, *a, **k)
        with mock.patch.object(builtins, "__import__", fake):
            code, out, _ = cli("annotate", str(p), "--box", "1,1,5,5", "--label", "x")
        self.assertEqual((code, out.strip()), (0, qm.NO_PILLOW))
        self.assertEqual(p.read_bytes(), before)

    def test_refuses_paths_outside_evidence(self):
        p = self.png()
        other = self.t / "x.png"
        other.write_bytes(p.read_bytes())
        (p.parent.parent / "bugs" / "a.png").write_bytes(p.read_bytes())
        (p.parent / "notes.txt").write_text("x")
        (p.parent / "link.png").symlink_to(other)
        for target in (other, p.parent.parent / "bugs" / "a.png", p.parent / "notes.txt", p.parent / "link.png",
                       p.parent / "missing.png"):
            self.assertEqual(cli("annotate", str(target), "--box", "1,1,5,5", "--label", "x")[0], 2, target)
        for argv in (("--box", "1,1,5"), ("--box", "1,1,0,5"), ("--label", "x")):
            self.assertEqual(cli("annotate", str(p), *argv, *(() if "--label" in argv else ("--label", "x")))[0], 2, argv)
        self.assertEqual(cli("annotate", str(p), "--box", "1,1,5,5")[0], 2)  # no label

    def test_refuses_another_projects_evidence(self):  # r8: a link to another project's evidence png was drawn on
        p = self.png()
        other = self.t / "other" / "qa-out" / "r" / "evidence"
        other.mkdir(parents=True)
        (other / "a.png").write_bytes(p.read_bytes())
        (p.parent / "a.png").symlink_to(other / "a.png")
        (self.proj / "qa-out" / "r2").symlink_to(other.parent)  # a whole run folder linked to the other project
        before = (other / "a.png").read_bytes()
        for target in (p.parent / "a.png", self.proj / "qa-out" / "r2" / "evidence" / "a.png"):
            code, _, err = cli("annotate", str(target), "--box", "1,1,5,5", "--label", "x")
            self.assertEqual(code, 2, target)
            self.assertIn("of this project", err)
        self.assertEqual((other / "a.png").read_bytes(), before)
        self.assertEqual(qm.evidence_png(other / "a.png"), (other / "a.png").resolve())  # its own project: fine
        (p.parent / "same.png").symlink_to(p)  # a link within the same project stays allowed
        self.assertEqual(qm.evidence_png(p.parent / "same.png"), p.resolve())

    @unittest.skipUnless(HAVE_PIL, "Pillow not installed")
    def test_bad_input_is_exit_2_and_image_kept(self):  # r9: inf box, non-PNG, truncated, huge -> tracebacks
        from PIL import Image
        p = self.png()
        for box in ("inf,1,5,5", "1e400,1,5,5", "nan,1,5,5"):
            self.assertEqual(cli("annotate", str(p), "--box", box, "--label", "x")[0], 2, box)
        fake, cut = p.parent / "fake.png", p.parent / "cut.png"
        fake.write_text("<html>not a png</html>")
        cut.write_bytes(p.read_bytes()[:60])
        for f in (fake, cut):
            before = f.read_bytes()
            code, _, err = cli("annotate", str(f), "--box", "1,1,5,5", "--label", "x")
            self.assertEqual((code, f.read_bytes()), (2, before), f)
            self.assertIn("can't read", err)
        with mock.patch.object(Image, "MAX_IMAGE_PIXELS", 100):  # decompression-bomb guard
            self.assertEqual(cli("annotate", str(p), "--box", "1,1,5,5", "--label", "x")[0], 2)
        self.assertEqual(sorted(x.name for x in p.parent.iterdir()), ["BUG-1.png", "cut.png", "fake.png"])

    @unittest.skipUnless(HAVE_PIL, "Pillow not installed")
    def test_planted_temp_link_is_not_written_through(self):  # r9: .BUG-1.png.tmp -> outside file was overwritten
        p = self.png()
        victim = self.t / "victim.txt"
        victim.write_text("keep")
        (p.parent / ".BUG-1.png.tmp").symlink_to(victim)
        os.chmod(p, 0o644)
        self.assertEqual(cli("annotate", str(p), "--box", "1,1,5,5", "--label", "x")[0], 0)
        self.assertEqual(victim.read_text(), "keep")
        self.assertFalse(p.is_symlink())
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o644)  # mode kept

    @unittest.skipUnless(HAVE_PIL, "Pillow not installed")
    def test_palette_and_16_bit_pngs(self):  # r9: transparent palette went black, 16-bit grey went all white
        from PIL import Image
        p = self.png()
        im = Image.new("P", (40, 40), 0)
        im.save(p, transparency=0)
        qm.annotate(p, [(1, 1, 5, 5)], "x")
        self.assertEqual(Image.open(p).convert("RGBA").getpixel((30, 30))[3], 0)  # still transparent
        Image.new("I;16", (40, 40), 32768).save(p)
        qm.annotate(p, [(1, 1, 5, 5)], "x")
        self.assertEqual(Image.open(p).convert("RGB").getpixel((30, 30)), (128, 128, 128))


class Verdict(unittest.TestCase):
    def case(self, cid, status, ac="AC-1", route="/form", **kw):
        return {"id": cid, "ticket": "T-1", "ac": ac, "route": route, "status": status, "notes": "", "actual": "", **kw}

    def test_four_outcomes(self):
        v = qm.verdict
        self.assertEqual(v("T-1", [self.case("TC-1", "PASS"), self.case("TC-2", "PASS")], []), ("PASS", "all 2 cases passed"))
        self.assertEqual(v({"key": "T-1"}, [self.case("TC-1", "PASS"), self.case("TC-3", "FAIL", "AC-2")],
                           [{"id": "BUG-1", "ticket": "T-1", "severity": "HIGH", "classification": "CONFIRMED"}]),
                         ("FAIL", "TC-3 failed (AC-2); BUG-1 HIGH"))
        self.assertEqual(v("T-1", [self.case("TC-1", "PASS")],
                           [{"id": "BUG-4", "route": "/form", "severity": "CRITICAL", "classification": "CONFIRMED"}])[0],
                         "FAIL")  # untagged bug on one of its routes
        self.assertEqual(v("T-1", [self.case("TC-1", "PASS"), self.case("TC-5", "NOT_RUN", notes="session expired")], []),
                         ("BLOCKED", "TC-5 not run: session expired"))
        self.assertEqual(v("T-1", [self.case("TC-5", "BLOCKED")], [])[0], "BLOCKED")
        same = [self.case(f"TC-{i}", "BLOCKED", notes="browser can't start") for i in range(1, 13)]
        self.assertEqual(v("T-1", same, []), ("BLOCKED", "12 cases (TC-1–TC-12) blocked: browser can't start"))
        self.assertEqual(v("T-1", [self.case("TC-1", "PASS", ticket="T-2")], []), ("BLOCKED", "no test cases"))
        self.assertEqual(v("T-1", [self.case("TC-1", "QUESTION")], []), ("PASS WITH NOTES", "TC-1 open question"))
        self.assertEqual(v("T-1", [self.case("TC-1", "PASS", bug="BUG-2")],
                           [{"id": "BUG-2", "route": "/other", "severity": "HIGH", "classification": "SUSPECTED"}]),
                         ("PASS WITH NOTES", "BUG-2 HIGH suspected"))  # referenced by a case; suspected isn't FAIL
        self.assertEqual(v("T-1", [self.case("TC-1", "PASS")],
                           [{"id": "BUG-9", "route": "/other", "ticket": "T-2", "severity": "HIGH",
                             "classification": "CONFIRMED"}])[0], "PASS")  # another ticket's bug

    def test_known_bug_named_by_hash_belongs_to_that_ticket_only(self):
        cases = [{"id": "TC-1", "ticket": "A-1", "route": "/list", "status": "PASS"},
                 {"id": "TC-2", "ticket": "B-1", "route": "/list", "status": "PASS", "bug": "known 0123456789ab"}]
        known = [{"hash": "0123456789abcdef", "route": "/list", "severity": "HIGH", "classification": "CONFIRMED", "title": "x"}]
        self.assertEqual(qm.verdict("A-1", cases, known)[0], "PASS")
        self.assertEqual(qm.verdict("B-1", cases, known)[0], "FAIL")

    def test_multi_ticket_runs(self):
        v = qm.verdict
        # shared /form: an untagged bug named by T-2's failing case is T-2's, not T-1's
        cases = [self.case("TC-1", "PASS"), self.case("TC-2", "FAIL", ticket="T-2", bug="BUG-1", actual="131 saved")]
        bug = [{"id": "BUG-1", "route": "/form", "severity": "HIGH", "classification": "CONFIRMED"}]
        self.assertEqual(v("T-1", cases, bug), ("PASS", "all 1 cases passed"))
        self.assertEqual(v("T-2", cases, bug), ("FAIL", "TC-2 failed (AC-1): 131 saved; BUG-1 HIGH"))
        # a case covering two AC covers both; an AC without a case is a note, not a PASS
        t = {"key": "T-1", "acs": [{"id": "AC-1"}, {"id": "AC-2"}, {"id": "AC-3", "text": "x"}]}
        two = [self.case("TC-1", "PASS", ac="AC-1, AC-2")]
        self.assertEqual(v(t, two, []), ("PASS WITH NOTES", "AC-3 has no test case"))
        self.assertEqual(qm._ac_lines(t, two, True)[:2], ["- AC-1 — ✅ PASS (TC-1)", "- AC-2 — ✅ PASS (TC-1)"])
        # a route skipped after another explorer was BLOCKED: say which route has no result
        self.assertEqual(v("T-1", [self.case("TC-1", "NOT_RUN", route="/list")], []),
                         ("BLOCKED", "TC-1 not run: no result reported for /list"))
        # a re-sighted known bug (hash, no draft) says so
        self.assertEqual(v("T-1", [self.case("TC-1", "PASS")], [{"hash": "abc123abc123", "route": "/form",
                           "title": "Save 500", "severity": "HIGH", "classification": "CONFIRMED"}]),
                         ("FAIL", "known bug Save 500 HIGH"))

    def test_several_failures_named_not_cut_mid_word(self):  # r26: "TC-3 failed (AC-1): … at 1…; …; +4 more"
        acts = ["Speed 131 shows 'Saved zone \"qa-sdet-zone-c\" at 131 km/h', no error", "55.5 saved", "blank saved as 0",
                "1000 saved"]
        cases = [self.case(f"TC-{i}", "FAIL", ac="AC-1" if i < 4 else "AC-10", actual=a) for i, a in enumerate(acts, 1)]
        bugs = [{"id": f"BUG-{i}", "ticket": "T-1", "severity": "HIGH", "classification": "CONFIRMED"} for i in (1, 2, 3)]
        self.assertEqual(qm.verdict("T-1", cases, bugs),
                         ("FAIL", "4 cases failed (TC-1, TC-2, TC-3, TC-4) on AC-1, AC-10; BUG-1 HIGH; BUG-2 HIGH; +1 more"))
        v, why = qm.verdict("T-1", cases[:1], [])
        self.assertEqual(why, "TC-1 failed (AC-1): Speed 131 shows 'Saved zone \"qa-sdet-zone-c\" at…")
        stuck = self.case("TC-12", "NOT_RUN", actual="Not testable: faults are off", notes="The 0 seen comes from the 404")
        self.assertEqual(qm.verdict("T-1", [stuck], [])[1], "TC-12 not run: Not testable: faults are off")  # why, not a side note
        nine = [self.case(f"TC-{i}", "FAIL") for i in range(1, 10)]
        self.assertEqual(qm.verdict("T-1", nine, [])[1], "9 cases failed (TC-1, TC-2, TC-3, TC-4, TC-5, TC-6, +3) on AC-1")

    def test_header_browser_comes_from_the_runs(self):
        plan = {"kind": "ticket", "env": {"url": "https://qa", "browser": "chrome"}}
        runs = [("run-form.json", {"env": {"url": "https://qa/form", "browser": "mcp"}})]
        self.assertEqual(qm._header("/x/qa-out/2026-11-23-T", "ticket", plan, runs)[1],
                         "ticket · 2026-11-23 · https://qa · build not run yet · browser mcp")
        self.assertTrue(qm._header("/x/qa-out/2026-11-23-T", "ticket", plan, [])[1].endswith("browser chrome"))


class MergedCases(Base):
    def test_most_decisive_result_wins(self):
        plan = {"kind": "ticket", "tickets": [{"key": "STUDIO-1"}], "cases": [
            {"id": "TC-1", "ticket": "STUDIO-1", "route": "/a"}, {"id": "TC-2", "ticket": "STUDIO-1", "route": "/b"},
            {"id": "TC-3", "ticket": "STUDIO-1", "route": "/b"}]}
        older = {"target": "/a", "cases": [{"id": "TC-1", "status": "FAIL", "actual": "131 saved"},
                                           {"id": "TC-2", "status": "NOT_RUN"}, {"id": "TC-3", "status": "BLOCKED"}]}
        newer = {"target": "/b", "cases": [{"id": "TC-2", "status": "PASS"}, {"id": "TC-3", "status": "PASS"},
                                           {"id": "TC-1", "status": "NOT_RUN", "notes": "other route"}]}
        d = qm.load_run(self.rundir(plan=plan, runs=[("a", older), ("b", newer)]))
        cases = qm.merged_cases(d["plan"], d["runs"])
        self.assertEqual([(c["id"], c["status"], c["actual"]) for c in cases],
                         [("TC-1", "FAIL", "131 saved"), ("TC-2", "PASS", ""), ("TC-3", "PASS", "")])
        self.assertEqual(qm.verdict("STUDIO-1", cases, []), ("FAIL", "TC-1 failed: 131 saved"))
        # same rank: the newest run wins
        tie = qm.merged_cases(plan, [("a", {"cases": [{"id": "TC-1", "status": "PASS", "actual": "old"}]}),
                                     ("b", {"cases": [{"id": "TC-1", "status": "pass", "actual": "new"}]})])
        self.assertEqual(tie[0]["actual"], "new")

    def test_case_without_or_with_a_repeated_id_is_named_not_dropped_silently(self):
        plan = {"kind": "ticket", "tickets": [{"key": "STUDIO-1", "acs": [{"id": "AC-1"}, {"id": "AC-2"}]}], "cases": [
            {"id": "TC-1", "ticket": "STUDIO-1", "ac": "AC-1", "title": "Save 130"},
            {"id": "TC-1", "ticket": "STUDIO-1", "ac": "AC-2", "title": "Reject 131"},  # added by hand in --review
            {"ticket": "STUDIO-1", "ac": "AC-2", "title": "Reject 0"}]}
        d = self.rundir(plan=plan)
        qm.write_report(d)
        r = (d / "REPORT.md").read_text()
        self.assertIn("## Problems reading this folder", r)
        self.assertIn("- plan.json: case 'Reject 131' repeats id TC-1; it is left out of this report: give it an id "
                      "of its own (e.g. TC-99)", r)
        self.assertIn("- plan.json: case 'Reject 0' has no id;", r)

    def test_page_column_when_a_ticket_spans_pages(self):
        cases = [{"id": "TC-1", "route": "/a", "status": "PASS", "actual": "", "notes": ""},
                 {"id": "TC-2", "route": "/b", "status": "PASS", "actual": "", "notes": ""}]
        self.assertEqual(qm._case_table(cases, "/x", True)[0],
                         "| ID | AC | Page | Case | Steps | Expected | Status | Actual / notes |")
        self.assertEqual(qm._case_table(cases[:1], "/x", False)[0], "| ID | AC | Case | Steps | Expected |")


class MergedCasesIds(unittest.TestCase):
    def test_numeric_id_zero_is_an_id(self):  # audit 3 r1
        warnings = []
        cases = qm.merged_cases({"cases": [{"id": 0, "title": "zero"}]}, [("a", {"cases": [{"id": 0, "status": "PASS"}]})],
                                warnings)
        self.assertEqual([(c["id"], c["status"]) for c in cases], [("0", "PASS")])
        self.assertEqual(warnings, [])


class Report(Base):
    def test_odd_ac_ids_do_not_lose_the_report(self):  # audit 3 r6
        plan = json.loads(json.dumps(PLAN))
        plan["tickets"][0]["acs"] = [{"id": ["AC-1"], "text": "a list for an id"}, {"id": 2, "text": "a number"}]
        plan["cases"][1]["ac"] = 2
        d = self.rundir(plan=plan, runs=[("form", {**RUN_FORM, "cases": [{"id": "TC-1", "status": "PASS"},
                                                                         {"id": "TC-2", "status": "PASS"}], "bugs": []})])
        text = qm.render_report(d)
        self.assertIn("- 2 a number — ✅ PASS (TC-2)", text)
        self.assertIn("a list for an id — not covered", text)
        self.assertNotIn("2 has no test case", text)

    def test_ticket_report_two_tickets_two_routes(self):
        d = self.rundir(plan=PLAN, runs=[("form", RUN_FORM), ("list", RUN_LIST)])
        (d / "bugs/BUG-1.md").write_text("x")
        (d / "bugs/BUG-2.md").write_text("x")
        (d / "evidence/BUG-1.png").write_bytes(b"png")
        code, out, _ = cli("report", str(d))
        self.assertEqual((code, out.strip()), (0, str(d / "REPORT.md")))
        r = (d / "REPORT.md").read_text()
        lines = r.splitlines()
        self.assertEqual(lines[0], "# QA report: STUDIO-1, STUDIO-2")
        self.assertEqual(lines[1], "ticket · 2026-11-23 · https://qa.example · build 1.42 · browser chrome")
        self.assertIn("| STUDIO-1 — Speed limit form | ❌ FAIL | TC-2 failed (AC-2): 131 saved; BUG-1 HIGH | 1 / 1 / 0 | 1 |", r)
        self.assertIn("| STUDIO-2 — Incident list | ⚠️ PASS WITH NOTES | TC-4 open question; BUG-2 LOW | 1 / 0 / 1 | 1 |", r)
        self.assertIn("## STUDIO-1 — Speed limit form\n**Verdict: ❌ FAIL** — TC-2 failed (AC-2): 131 saved; BUG-1 HIGH", r)
        self.assertIn("- AC-1 Saves 0-130 — ✅ PASS (TC-1)", r)
        self.assertIn("- AC-2 Rejects 131 — ❌ FAIL (TC-2)", r)
        self.assertIn("- AC-1 Sorted by date — ❓ QUESTION (TC-3, TC-4)", r)
        self.assertIn("- **Scope:** speed field\n- **Risks:**\n  - rounding\n- **Out of scope:**\n  - mobile", r)
        # the plan set a priority (P1 first): a Pri column; one start page per ticket: no Page column
        self.assertIn("| ID | Pri | AC | Case | Steps | Expected | Status | Actual / notes |", r)
        self.assertIn("| TC-1 | P1 | AC-1 | Save 130 | 1. Open form<br>2. Type 130 \\| save | Saved | ✅ PASS | Saved |", r)
        self.assertIn("| TC-2 |  | AC-2 | Reject 131 | 1. Type 131 | Error shown | ❌ FAIL | 131 saved [BUG-1](bugs/BUG-1.md) |", r)
        self.assertIn("| [BUG-1](bugs/BUG-1.md) | HIGH | CONFIRMED | Speed accepts 131 | /form | STUDIO-1 | "
                      "[image](evidence/BUG-1.png) |", r)
        self.assertIn("| [BUG-2](bugs/BUG-2.md) | LOW | CONFIRMED | Header misaligned | /list | STUDIO-2 | — |", r)
        self.assertIn("## Questions\n- Is 0 a valid speed?\n- TC-4: Order of ties unspecified", r)
        self.assertIn("## Not tested\n- Keyboard only: out of time", r)
        self.assertIn("### /form (run-form.json: COMPLETED, build 1.42)\nExplored the speed field.\n\n"
                      "Heuristics covered: boundary ❌, happy-path ✅", r)
        self.assertIn("### /list (run-list.json: COMPLETED, build 1.42)", r)
        order = [r.index(h) for h in ("## Summary", "## STUDIO-1", "## STUDIO-2", "## Bugs", "## Questions",
                                      "## Not tested", "## Exploration notes")]
        self.assertEqual(order, sorted(order))

    def test_nothing_run_yet(self):
        d = self.rundir(plan=PLAN)
        qm.write_report(d)
        r = (d / "REPORT.md").read_text()
        self.assertEqual(r.count("⏸️ not run"), 4 + 3)  # every case, and every covered AC
        self.assertIn("| STUDIO-1 — Speed limit form | ⏳ NOT STARTED | testing hasn't started yet | 0 / 0 / 2 | 0 |", r)
        self.assertIn("build not run yet", r)
        for absent in ("## Bugs", "## Exploration notes", "## Questions"):
            self.assertNotIn(absent, r)

    def test_empty_folder_and_broken_files(self):
        d = self.rundir("2026-11-20-x")
        self.assertIn("Nothing recorded yet", qm.render_report(d))
        (d / ".data/plan.json").write_text("{nope")
        (d / ".data/run-a.json").write_text("[1]")
        r = qm.render_report(d)
        self.assertIn("## Problems reading this folder\n- plan.json unreadable (JSONDecodeError)\n- run-a.json is not an object", r)
        self.assertEqual(cli("report", str(self.proj))[0], 2)  # not a qa-out/<run> folder

    def test_explore_kind(self):
        run = {**RUN_FORM, "mode": "explore", "cases": [], "bugs": [{**RUN_FORM["bugs"][0], "ticket": None}]}
        d = self.rundir("2026-11-23-form", plan={"kind": "explore", "title": "/form", "env": {"url": "http://localhost:8765"}},
                        runs=[("form", run)])
        r = qm.render_report(d)
        self.assertIn("## Summary\n**Status:** /form COMPLETED · 1 bug(s)", r)
        self.assertNotIn("Verdict", r)
        self.assertNotIn("| Ticket | Verdict", r)
        self.assertIn("| BUG-1 | HIGH | CONFIRMED | Speed accepts 131 | /form |  | — |", r)  # no draft file: no link
        self.assertIn("## Exploration notes", r)

    def test_plan_kind(self):
        d = self.rundir("2026-11-23-plan-STUDIO-1", plan={**PLAN, "kind": "plan"})
        r = qm.render_report(d)
        self.assertIn("| STUDIO-1 — Speed limit form | 2 | 2 |", r)
        self.assertIn("| ID | AC | Case | Steps | Expected |\n|---|---|---|---|---|", r)
        self.assertNotIn("Status", r)
        self.assertNotIn("Verdict", r)
        self.assertIn("- AC-1 Saves 0-130 (TC-1)", r)

    def test_plan_questions_render(self):
        q = {**PLAN, "kind": "plan", "questions": ["Q1 [BLOCKING] Is 0 a valid speed?", "Q2 [LOW] Units?"]}
        r = qm.render_report(self.rundir("2026-11-23-plan-STUDIO-1", plan=q))
        self.assertIn("## Questions\n- Q1 [BLOCKING] Is 0 a valid speed?\n- Q2 [LOW] Units?", r)
        d = self.rundir(plan={**q, "kind": "ticket"}, runs=[("form", RUN_FORM)])
        # r26: a run's follow-up on plan Q1 read as a second, unnumbered Q1 in one flat list: two labelled lists
        r = qm.render_report(d)
        self.assertIn("## Questions\n**From the test plan (before testing):**\n\n- Q1 [BLOCKING] Is 0 a valid speed?\n"
                      "- Q2 [LOW] Units?\n\n**Raised while testing:**\n\n- Is 0 a valid speed?\n", r)
        self.assertIn("## Exploration notes\nHeuristics: ✅ checked, no problem · ❌ problem found · ⛔ could not be checked", r)

    def test_odd_field_types_never_crash(self):
        t = [{"key": "STUDIO-1", "summary": "A", "acs": [{"id": "AC-1", "text": "x"}]}]
        c = [{"id": "TC-1", "ticket": "STUDIO-1", "ac": "AC-1", "title": "t", "route": "/f", "steps": ["a"]}]
        plans = [{"kind": "ticket", "tickets": t, "cases": c, "env": {"url": 5}},
                 {"kind": "ticket", "tickets": t, "cases": c, "env": {"url": ["x"], "browser": {"a": 1}}},
                 {"kind": "ticket", "tickets": [{"key": ["a"]}, {"key": 12}], "cases": [{"id": ["x"], "ticket": 12}]},
                 {"kind": "plan", "created": 2026, "title": ["x"], "tickets": t, "cases": {"a": 1}, "questions": "q"},
                 {"kind": ["x"], "tickets": "x", "cases": [{**c[0], "steps": "do | it\nnow", "ac": ["x"]}]}]
        runs = [{"env": {"url": 7, "browser": 3}, "target": 5, "status": ["x"], "build": {"b": 1}},
                {"bugs": [{"ticket": ["x"], "title": {"a": 1}, "route": "/f", "hash": 5}]},
                {"cases": [{"id": ["x"], "status": "PASS"}, {"id": {"a": 1}, "status": 3}, {"id": "TC-1", "bug": 9}]},
                {"pages": [{"route": "/f", "coverage": [{"heuristic": 5, "result": ["x"]}]}], "notes": ["n"]}]
        for i, plan in enumerate(plans):
            d = self.rundir(f"2026-11-23-fuzz{i}", plan=plan, runs=[(f"r{k}", r) for k, r in enumerate(runs)])
            code, _, err = cli("report", str(d))
            self.assertEqual((code, err), (0, ""), plan)
            self.assertTrue((d / "REPORT.md").read_text().startswith("# QA report: "), plan)
            self.assertNotIn("Could not render", (d / "REPORT.md").read_text(), plan)  # rendered, not the fallback
        self.assertEqual((self.proj / "qa-out/README.md").read_text().count("| 2026-11-23 |"), len(plans))

    def test_one_broken_folder_does_not_break_the_index(self):
        good = self.rundir("2026-11-23-good", plan=PLAN)
        bad = self.rundir("2026-11-22-bad", plan=PLAN)
        real = qm.run_summary
        with mock.patch.object(qm, "run_summary", lambda p: (_ for _ in ()).throw(TypeError("x")) if p.name == bad.name
                               else real(p)), mock.patch.object(qm, "render_report", side_effect=TypeError("boom")):
            self.assertEqual(cli("report", str(good))[0], 0)
        self.assertIn("Could not render this report: TypeError: boom", (good / "REPORT.md").read_text())
        idx = (self.proj / "qa-out/README.md").read_text()
        self.assertIn("| 2026-11-22 | [2026-11-22-bad](2026-11-22-bad/REPORT.md) | ? | unreadable (TypeError) | ? |", idx)
        self.assertIn("2026-11-23-good", idx)

    def test_text_cannot_break_the_markdown(self):
        plan = {"kind": "ticket", "title": "T\n## injected",
                "tickets": [{"key": "STUDIO-1", "summary": "s\n# H1 | x", "acs": [{"id": "AC-1\n- x", "text": "t"}]}],
                "cases": [{"id": "TC-1", "ticket": "STUDIO-1", "ac": "AC-1\n- x", "title": "t", "route": "/f"}]}
        run = {"target": "/f", "status": "done\n# X", "build": "1\n## b",
               "cases": [{"id": "TC-1", "status": "FAIL", "bug": "BUG-9 | <img src=x>\nnew"}],
               "bugs": [{"hash": "ab|cd\nef", "title": "t", "route": "/f"}]}
        d = self.rundir(plan=plan, runs=[("f", run)])
        (d / "bugs/BUG-9.md").write_text("x")
        r = qm.render_report(d)
        headings = [ln for ln in r.splitlines() if ln.startswith("#")]
        self.assertEqual(headings, ["# QA report: T ## injected", "## Summary", "## STUDIO-1 — s # H1 \\| x",
                                    "### Acceptance criteria", "### Test cases", "## Bugs", "## Exploration notes",
                                    "### /f (run-f.json: done # X, build 1 ## b)"])
        self.assertIn("| ❌ FAIL | BUG-9 \\| &lt;img src=x> new |", r)  # not a link: not exactly BUG-n
        self.assertNotIn("](bugs/BUG-9", r)
        self.assertIn("| known ab\\|cd ef |", r)
        self.assertIn("- AC-1 - x t — ❌ FAIL (TC-1)", r)
        self.assertIn("| STUDIO-1 — s # H1 \\| x | ❌ FAIL |", r)

    def test_wrong_typed_run_fields_are_named_not_hidden(self):  # audit 2 r20
        plan = {"kind": "ticket", "tickets": [{"key": "T-1", "acs": [{"id": "AC-1", "text": "a"}]}],
                "cases": [{"id": "TC-1", "ticket": "T-1", "ac": "AC-1", "title": "x", "route": "/f"}]}
        d = self.rundir("2026-11-23-types", plan=plan, runs=[("f", {"cases": "TC-1 PASS", "questions": "why?"})])
        qm.write_report(d)
        r = (d / "REPORT.md").read_text()
        self.assertIn("## Problems reading this folder", r)
        self.assertIn("`cases` should be a list", r)

    def test_hostile_text_stays_text(self):
        """XSS payloads, `<!--`, remote images, a leading ``` and `@@` in run text can't hide or rewire the report."""
        plan = {"kind": "ticket", "title": "T <!-- x", "tickets": [{"key": "STUDIO-1", "acs": [{"id": "AC-1", "text": "a"}]}],
                "cases": [{"id": "TC-1", "ticket": "STUDIO-1", "ac": "AC-1", "title": "<script>alert(1)</script>",
                           "route": "/f"}]}
        run = {"target": "/f", "status": "done", "notes": "```\nrest", "questions": ["q <details>"],
               "cases": [{"id": "TC-1", "status": "FAIL", "actual": "saw ![p](https://evil.example/t.png) [here](https://evil.example)"}],
               "pages": [{"route": "/f", "coverage": [{"target": "x", "heuristic": "<!-- [h](https://e)", "result": "pass"}]}],
               "bugs": [{"title": "bad @@ title", "severity": "LOW", "route": "/f", "draft_path": "/x/bugs/BUG-1.md"},
                        {"title": "two", "severity": "LOW", "route": "/f", "evidence": "/x/evidence/shot (1).png"}]}
        d = self.rundir("2026-11-23-odd name", plan=plan, runs=[("f", run)])
        (d / "bugs/BUG-1.md").write_text("x")
        (d / "evidence/BUG-1.png").write_bytes(b"x")
        (d / "evidence/shot (1).png").write_bytes(b"x")
        qm.write_report(d)
        r = (d / "REPORT.md").read_text()
        self.assertNotIn("<!--", r)
        self.assertNotIn("<script>", r)
        self.assertNotIn("<details>", r)
        self.assertIn("&lt;script>alert(1)&lt;/script>", r)
        self.assertIn("saw ![p]\\(https://evil.example/t.png) [here]\\(https://evil.example)", r)  # links stay text
        self.assertIn("Heuristics covered: other:", r)
        self.assertIn("\n\\```<br>rest\n", r)
        self.assertIn("| [BUG-1](bugs/BUG-1.md) | LOW |  | bad @@ title | /f | STUDIO-1 | [image](evidence/BUG-1.png) |", r)
        self.assertIn("| [image](evidence/shot%20%281%29.png) |", r)
        idx = (self.proj / "qa-out/README.md").read_text()
        self.assertIn("| [2026-11-23-odd name](2026-11-23-odd%20name/REPORT.md) |", idx)

    def test_index_newest_first(self):
        a = self.rundir("2026-11-20-STUDIO-1", plan=PLAN, runs=[("form", RUN_FORM)])
        b = self.rundir("2026-11-23-plan-STUDIO-9", plan={**PLAN, "kind": "plan"})
        self.rundir("old-layout-slug")  # no REPORT.md, no plan: not listed
        (self.proj / "qa-out/form").mkdir()
        (self.proj / "qa-out/form/run.json").write_text("{}")
        qm.write_report(a)
        qm.write_report(b)
        idx = (self.proj / "qa-out/README.md").read_text()
        rows = [ln for ln in idx.splitlines() if ln.startswith("| 2026")]
        self.assertEqual(rows, [
            "| 2026-11-23 | [2026-11-23-plan-STUDIO-9](2026-11-23-plan-STUDIO-9/REPORT.md) | plan | plan only (4 cases) | 0 |",
            "| 2026-11-20 | [2026-11-20-STUDIO-1](2026-11-20-STUDIO-1/REPORT.md) | ticket | "
            "STUDIO-1 ❌ FAIL, STUDIO-2 ⛔ BLOCKED | 1 |"])
        self.assertNotIn("old-layout", idx)


class Inbox(Base):
    ACLI = [{"key": "STUDIO-5", "fields": {"summary": "Old one", "status": {"name": "In Progress"},
                                           "priority": {"name": "High"}, "updated": "2026-11-20T10:00:00.000+0000"}},
            {"key": "STUDIO-7", "fields": {"summary": "Newer | pipe", "status": {"name": "To Do"},
                                           "priority": {"name": "Low"}, "updated": "2026-11-22T10:00:00.000+0000"}}]

    def test_shapes(self):
        flat = [{"key": "ops-1", "summary": "Flat", "status": "Open", "priority": "P2", "updated": "2026-11-01"}]
        for data in (self.ACLI, {"issues": self.ACLI}, {"workItems": self.ACLI}, {"values": self.ACLI}):
            self.assertEqual([x["key"] for x in qm.inbox_items(data)], ["STUDIO-5", "STUDIO-7"])
        self.assertEqual(qm.inbox_items(flat + [{"nokey": 1}, "junk"]),
                         [{"key": "OPS-1", "summary": "Flat", "status": "Open", "priority": "P2", "updated": "2026-11-01"}])
        for bad in ({"x": 1}, "text", 5):
            with self.assertRaises(qm.BadInput):
                qm.inbox_items(bad)

    def test_new_tracking_persists(self):
        self.config(jiraKeyPrefix="STUDIO")
        code, out, _ = cli("inbox", str(self.proj), stdin=json.dumps(self.ACLI))
        self.assertEqual(code, 0)
        rows = [ln for ln in out.splitlines() if ln.startswith("| ")][1:]
        self.assertIn("| New | Key | Summary | Status | Priority | Updated |", out)
        self.assertEqual(rows, ["| NEW | STUDIO-7 | Newer \\| pipe | To Do | Low | 2026-11-22 |",
                                "| NEW | STUDIO-5 | Old one | In Progress | High | 2026-11-20 |"])
        self.assertEqual(out.splitlines()[-1], "Next: /qa-sdet:qa-ticket 7 5")
        self.assertEqual((self.proj / "qa-out/INBOX.md").read_text(), out)
        self.assertTrue(out.startswith("# Jira inbox\n"))
        f = self.t / "search.json"
        f.write_text(json.dumps({"issues": self.ACLI + [{"key": "OPS-9", "fields": {"summary": "Other",
                                                                                     "updated": "2026-11-01"}}]}))
        code, out, _ = cli("inbox", str(self.proj), "--file", str(f))  # a new process run would read the same DB
        rows = [ln for ln in out.splitlines() if ln.startswith("| ")][1:]
        self.assertEqual([r.split(" | ")[0:2] for r in rows], [["| NEW", "OPS-9"], ["| ", "STUDIO-7"], ["| ", "STUDIO-5"]])
        self.assertEqual(out.splitlines()[-1], "Next: /qa-sdet:qa-ticket OPS-9")
        code, out, _ = cli("inbox", str(self.proj), stdin=json.dumps(self.ACLI))
        self.assertEqual(out.splitlines()[-1], "Next: /qa-sdet:qa-ticket 7 5")  # nothing new: top 3
        self.assertNotIn("| NEW", out)

    def test_bad_input(self):
        self.assertEqual(cli("inbox", str(self.proj), stdin="not json")[0], 2)
        code, _, err = cli("inbox", str(self.proj), stdin='[{"id": "10001", "self": "https://x/issue/10001"}]')  # audit 3 r37
        self.assertEqual((code, "none of the 1 items has a Jira key" in err), (2, True))
        self.assertEqual(cli("inbox", str(self.proj), stdin='{"x": 1}')[0], 2)
        self.assertEqual(cli("inbox")[0], 2)
        self.assertFalse((self.proj / "qa-out").exists())
        code, out, _ = cli("inbox", str(self.proj), stdin="[]")
        self.assertIn("No open tickets", out)
        self.assertEqual(out.splitlines()[-1], "Next: nothing to test")
        code, out, _ = cli("inbox", str(self.proj), stdin='[{"key": "AB-1", "summary": "bad \\ud800 char"}]')  # was: UnicodeEncodeError
        self.assertEqual((code, "AB-1" in out), (0, True))


    def test_duplicates_typo_project_deep_garbage_big_inbox(self):  # r11
        code, out, _ = cli("inbox", str(self.proj), stdin=json.dumps(self.ACLI + self.ACLI[:1]))
        self.assertIn("2 open, 2 new", out)  # STUDIO-5 listed twice is one ticket
        self.assertEqual(sum(ln.count("STUDIO-5") for ln in out.splitlines() if ln.startswith("| ")), 1)
        self.assertEqual(cli("inbox", str(self.t / "typo/p"), stdin="[]")[0], 2)
        self.assertFalse((self.t / "typo").exists())
        code, _, err = cli("inbox", str(self.proj), stdin="[" * 100000)
        self.assertEqual(code, 2)
        self.assertIn("can't read Jira JSON", err)
        big = json.dumps([{"key": f"ZZ-{i}"} for i in range(6000)])
        self.assertIn("6000 open, 6000 new", cli("inbox", str(self.proj), stdin=big)[1])
        self.assertIn("6000 open, 0 new", cli("inbox", str(self.proj), stdin=big)[1])  # was 1000 new forever

    def test_failed_write_keeps_tickets_new(self):  # r11: keys were remembered before INBOX.md was written
        with mock.patch.object(qm, "_write", side_effect=OSError("disk full")), self.assertRaises(OSError):
            cli("inbox", str(self.proj), stdin=json.dumps(self.ACLI))
        self.assertIn("2 open, 2 new", cli("inbox", str(self.proj), stdin=json.dumps(self.ACLI))[1])

    def test_planted_temp_link_is_not_written_through(self):  # r11: _write opened .INBOX.md.tmp following links
        victim = self.t / "victim"
        victim.write_text("keep")
        (self.proj / "qa-out").mkdir()
        for name in (".INBOX.md.tmp", ".README.md.tmp", ".REPORT.md.tmp"):
            (self.proj / "qa-out" / name).symlink_to(victim)
        self.assertEqual(cli("inbox", str(self.proj), stdin="[]")[0], 0)
        self.assertEqual(victim.read_text(), "keep")
        self.assertFalse((self.proj / "qa-out/INBOX.md").is_symlink())
        self.assertEqual(stat.S_IMODE((self.proj / "qa-out/INBOX.md").stat().st_mode), 0o644)
        self.assertEqual(sorted(p.name for p in (self.proj / "qa-out").iterdir() if ".tmp" in p.name and
                                not p.is_symlink()), [])


class SetupPermissions(Base):
    def test_print(self):
        code, out, _ = cli("setup-permissions")
        self.assertEqual((code, out.splitlines()), (0, qm.PERMISSION_RULES))
        self.assertFalse((self.t / "home/.claude").exists())  # printing changes nothing

    def test_merge_deep_json_same_second_backups_and_spacing(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "settings.json"
            f.write_text("[" * 200000 + "]" * 200000)
            with self.assertRaises(qm.BadInput):
                qm.merge_permissions(f)
            f.write_text(json.dumps({"permissions": {"allow": ["Bash( qa-mem *)"]}}))
            added = qm.merge_permissions(f, stamp="20261123000000")
            self.assertNotIn("Bash(qa-mem *)", added)  # same rule, other spacing
            f.write_text(json.dumps({"permissions": {"allow": []}}))
            qm.merge_permissions(f, stamp="20261123000000")
            self.assertEqual(len(list(Path(d).glob("settings.json.bak-20261123000000*"))), 2)  # first backup kept

    def test_merge_keeps_keys_no_dupes_backup_mode(self):
        s = self.t / "settings.json"
        s.write_text(json.dumps({"model": "x", "permissions": {"allow": ["Bash(ls)", "Skill(qa-sdet:*)"],
                                                                 "deny": ["Bash(rm *)"]}, "hooks": {"a": 1}}))
        os.chmod(s, 0o640)
        code, out, _ = cli("setup-permissions", "--settings", str(s), "--apply")
        self.assertEqual(code, 0)
        got = json.loads(s.read_text())
        self.assertEqual(got["model"], "x")
        self.assertEqual(got["hooks"], {"a": 1})
        self.assertEqual(got["permissions"]["deny"], ["Bash(rm *)"])
        allow = got["permissions"]["allow"]
        self.assertEqual(allow[:2], ["Bash(ls)", "Skill(qa-sdet:*)"])
        self.assertEqual(sorted(allow), sorted(set(allow)))
        self.assertEqual(set(allow), {"Bash(ls)"} | set(qm.PERMISSION_RULES))
        self.assertNotIn("Skill(qa-sdet:*)", out)  # only what was added
        self.assertIn("Bash(qa-mem *)", out)
        self.assertFalse(any(r.startswith("Agent(") or "python" in r for r in qm.PERMISSION_RULES))  # auto mode drops them
        # r10: the config read every run is allowed, but never Edit/Write (allowedHosts is the guard's trust root)
        self.assertIn("Read(~/.config/qa-sdet/config.json)", qm.PERMISSION_RULES)
        self.assertFalse(any(r.startswith(("Edit(", "Write(", "MultiEdit(")) or "qa-state" in r
                             for r in qm.PERMISSION_RULES))
        self.assertEqual(stat.S_IMODE(s.stat().st_mode), 0o640)
        backups = list(self.t.glob("settings.json.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertRegex(backups[0].name, r"\.bak-\d{14}$")
        self.assertEqual(json.loads(backups[0].read_text())["permissions"]["allow"], ["Bash(ls)", "Skill(qa-sdet:*)"])
        code, out, _ = cli("setup-permissions", "--settings", str(s), "--apply")
        self.assertIn("nothing changed", out)
        self.assertEqual(json.loads(s.read_text())["permissions"]["allow"], allow)

    def test_missing_file_and_invalid_json(self):
        s = self.t / "home/.claude/settings.json"
        self.assertEqual(cli("setup-permissions", "--apply")[0], 0)  # default path under HOME
        self.assertEqual(json.loads(s.read_text()), {"permissions": {"allow": qm.PERMISSION_RULES}})
        bad = self.t / "bad.json"
        bad.write_text('{"permissions": ')
        code, _, err = cli("setup-permissions", "--settings", str(bad), "--apply")
        self.assertEqual(code, 2)
        self.assertIn("isn't valid JSON", err)
        self.assertEqual(bad.read_text(), '{"permissions": ')
        self.assertEqual(list(self.t.glob("bad.json*")), [bad])  # no backup, no temp file
        bad.write_text('{"permissions": {"allow": "Bash(ls)"}}')
        self.assertEqual(cli("setup-permissions", "--settings", str(bad), "--apply")[0], 2)
        for text in ('{"x": NaN}', '{"x": -Infinity}', '{"x": 1e999}', '{"x": "\\ud800"}'):  # Claude Code can't read these back
            bad.write_text(text)
            code, _, err = cli("setup-permissions", "--settings", str(bad), "--apply")
            self.assertEqual(code, 2, text)
            self.assertIn("nothing changed", err)
            self.assertEqual(bad.read_text(), text)
            self.assertEqual(sorted(p.name for p in self.t.iterdir() if p.name.startswith((".bad", "bad"))), ["bad.json"])


    def test_read_only_and_concurrent_edit(self):  # r11: traceback in a read-only dir; read-only file overwritten;
        d = self.t / "cfg"                          # an edit Claude Code saved meanwhile was dropped
        d.mkdir()
        s = d / "settings.json"
        s.write_text("{}")
        os.chmod(s, 0o444)
        code, _, err = cli("setup-permissions", "--settings", str(s), "--apply")
        self.assertEqual((code, s.read_text()), (2, "{}"))
        self.assertIn("is read-only", err)
        os.chmod(s, 0o644)
        os.chmod(d, 0o555)
        try:
            code, _, err = cli("setup-permissions", "--settings", str(s), "--apply")
        finally:
            os.chmod(d, 0o755)
        self.assertEqual((code, s.read_text()), (2, "{}"))
        self.assertIn("can't write", err)
        real = qm.tempfile.mkstemp

        def racing(*a, **k):
            s.write_text('{"model": "saved meanwhile"}')
            return real(*a, **k)
        with mock.patch.object(qm.tempfile, "mkstemp", racing):
            code, _, err = cli("setup-permissions", "--settings", str(s), "--apply")
        self.assertEqual(code, 2)
        self.assertIn("changed while merging", err)
        self.assertEqual(json.loads(s.read_text()), {"model": "saved meanwhile"})
        self.assertEqual([p.name for p in d.iterdir() if p.name.endswith(".tmp")], [])
        self.assertEqual(cli("setup-permissions", "--settings", str(s), "--apply")[0], 0)
        self.assertEqual(json.loads(s.read_text())["model"], "saved meanwhile")


class IngestDataFolder(Base):
    def test_run_json_in_data_with_drafts_in_bugs(self):
        d = self.rundir(plan=PLAN, runs=[("form", RUN_FORM)])
        (d / "bugs/BUG-1.md").write_text("draft")
        self.assertEqual(qm.ingest_pending(self.proj / "qa-out"), 1)
        self.assertTrue((d / ".data/run-form.json.ingested").exists())
        self.assertFalse((d / ".data/plan.json.ingested").exists())  # the plan is not ingested
        con = qm.connect()
        try:
            self.assertEqual(con.execute("SELECT draft_path FROM bugs").fetchone()[0], str((d / "bugs/BUG-1.md").resolve()))
        finally:
            con.close()
        self.assertEqual(qm.ingest_pending(self.proj / "qa-out"), 0)

    def test_bad_report_fields_are_parked(self):
        for bad in ({"cases": [{"id": "TC-1", "status": "MAYBE"}]}, {"cases": [{"status": "PASS"}]},
                    {"cases": "TC-1 PASS"}, {"questions": "why?"}, {"not_tested": [1]}, {"notes": ["x"]},
                    {"env": "chrome"}, {"env": {"url": ["x"]}},
                    {"bugs": [{"route": "/x", "title": "t", "annotated": "yes"}]}):
            d = self.rundir("bad")
            f = d / ".data/run-x.json"
            f.write_text(json.dumps({**RUN_LIST, "bugs": [], **bad}))
            qm.PARKED.clear()
            with contextlib.redirect_stderr(io.StringIO()):
                qm.ingest_pending(self.proj / "qa-out")
            self.assertTrue(f.with_name("run-x.json.error").exists(), bad)
            f.with_name("run-x.json.error").unlink()
        good = {**RUN_LIST, "cases": [{"id": "TC-1", "status": "not run"}], "env": {"url": "u", "browser": "mcp"}}
        f.write_text(json.dumps(good))
        self.assertEqual(qm.ingest_pending(self.proj / "qa-out"), 1)


    def test_deep_json_does_not_stop_other_ingests(self):  # r11: a RecursionError in scrub aborted the whole hook
        bad = self.rundir("a") / ".data/run-deep.json"
        bad.write_text("[" * 50000 + "]" * 50000)
        good = self.rundir("b", runs=[("ok", RUN_LIST)]) / ".data/run-ok.json"
        qm.PARKED.clear()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(qm.ingest_pending(self.proj / "qa-out"), 1)
        self.assertTrue(good.with_name("run-ok.json.ingested").exists())
        self.assertTrue(bad.with_name("run-deep.json.error").exists())
        self.assertEqual(len(qm.PARKED), 1)

    def test_scrub_ignores_planted_temp_link(self):  # r11: .<draft>.scrub was opened following links
        d = self.rundir("c")
        victim = self.t / "victim"
        victim.write_text("keep")
        draft = d / "bugs/BUG-1.md"
        draft.write_text("token: abcdefghijklmnop1234\n")
        (d / "bugs/.BUG-1.md.scrub").symlink_to(victim)
        (d / "REPORT.md").write_text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz\n")
        (self.proj / "qa-out/INBOX.md").write_text("| NEW | AB-1 | api_key=sk_live_abcdefghij123 |\n")
        qm.scrub_tree(self.proj / "qa-out")
        self.assertEqual(victim.read_text(), "keep")
        self.assertFalse(draft.is_symlink())
        self.assertEqual(draft.read_text(), "token: [REDACTED]\n")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", (d / "REPORT.md").read_text())
        self.assertNotIn("sk_live", (self.proj / "qa-out/INBOX.md").read_text())
        self.assertEqual(sorted(p.name for p in (d / "bugs").iterdir()), [".BUG-1.md.scrub", "BUG-1.md"])


class Doctor(Base):
    def test_profile_pillow_and_old_cookie_lines(self):
        self.config(app="x")
        qm.CONFIG.with_name("qa-state.json").write_text("{}")
        with mock.patch.object(qm, "claude_mcp_list", lambda: ""), mock.patch("shutil.which", lambda *a, **k: None):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                qm.doctor()
            self.assertIn("run /qa-sdet:qa-login", out.getvalue())
            self.assertIn("old cookie file present: qa-setup can delete it", out.getvalue())
            self.assertIn("Pillow", out.getvalue())
            self.assertNotIn("qa-state.json readable", out.getvalue())
            qm.ensure_profile_dir()  # browser-open made the folder, but nobody signed in: still FIX
            (qm.profile_dir() / "Default").mkdir()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                qm.doctor()
            self.assertIn("run /qa-sdet:qa-login", out.getvalue())
            self.assertNotIn("signed-in profile present", out.getvalue())
            self.assertEqual(cli("login-ok")[0], 0)  # qa-login verified the sign-in
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                qm.doctor()
            self.assertIn("ok   signed-in profile present, from /qa-sdet:qa-login (expires with the app's session)",
                          out.getvalue())
            self.assertEqual(cli("login-ok", "x")[0], 2)
            with mock.patch.object(qm.subprocess, "run", return_value=done(0)):
                self.config(app="x", browser="chrome")
                qm.main(["browser-open"])  # a test run keeps the sign-in
                self.assertTrue(qm.signed_in_stamp().exists())
                qm.main(["browser-open", "--session", "qa-login", "--headed"])  # a new sign-in starts: forget it
                self.assertFalse(qm.signed_in_stamp().exists())


    def doctor_out(self):
        out = io.StringIO()
        with mock.patch.object(qm, "claude_mcp_list", lambda: ""), mock.patch("shutil.which", lambda *a, **k: None), \
                contextlib.redirect_stdout(out):
            qm.doctor()
        return out.getvalue()

    def test_doctor_agrees_with_the_guard_and_stops_at_a_json_error(self):  # audit 3: second independent pass
        good = dict(app="x", ssoHost="login.x.com", buildSource={"type": "selector", "value": "footer"})
        for cfg in (dict(qaBaseUrl="https://qa_app.example.com", allowedHosts=["qa_app.example.com"]),
                    dict(qaBaseUrl="http://127.0.0.1:8765", allowedHosts=["localhost"])):
            self.config(**good, **cfg)
            out = self.doctor_out()
            self.assertNotIn("FIX  allowedHosts", out, cfg)
            self.assertNotIn("FIX  qaBaseUrl", out, cfg)
        qm.CONFIG.write_text('{"app": "x", "qaBaseUrl": "https://qa.example.com",}')
        out = self.doctor_out()
        self.assertIn("JSONDecodeError", out)
        self.assertNotIn("config has app", out)
        self.assertNotIn("FIX  buildSource", out)

    def test_allowed_hosts_with_scheme_or_port_and_sso_host_with_port(self):  # audit 3 r11
        good = dict(app="x", qaBaseUrl="https://qa.example.com:8443/app", allowedHosts=["qa.example.com", "*.api.example.com"],
                    ssoHost="login.x.com", buildSource={"type": "selector", "value": "footer"})
        self.config(**good)
        self.assertNotIn("FIX  allowedHosts", self.doctor_out())
        for bad, want in [(dict(allowedHosts=["https://qa.example.com/"]), "FIX  allowedHosts entries are host names (https://qa.example.com/"),
                          (dict(allowedHosts=["qa.example.com:8443"]), "FIX  allowedHosts entries are host names (qa.example.com:8443"),
                          (dict(ssoHost="qa.example.com:8443"), "FIX  ssoHost differs from the qaBaseUrl host"),
                          (dict(buildSource={"type": "selector", "value": ["x"]}), "FIX  buildSource is")]:
            self.config(**{**good, **bad})
            self.assertIn(want, self.doctor_out(), bad)

    def test_odd_config_values_get_a_fix_line_not_a_crash(self):
        good = dict(app="x", qaBaseUrl="https://qa.example.com", allowedHosts=["qa.example.com"], ssoHost="login.x.com",
                    buildSource={"type": "selector", "value": "footer"})
        self.config(**good, sourcePaths=5)  # was: TypeError, no len()
        self.assertNotIn("source folders configured", self.doctor_out())
        for bad, want in [(dict(qaBaseUrl="qa.example.com"), 'FIX  qaBaseUrl is a full URL  -> e.g. "qaBaseUrl"'),
                          (dict(qaBaseUrl=5), "FIX  qaBaseUrl is a full URL"),
                          (dict(ssoHost="https://login.x.com/"), "FIX  ssoHost is a host name  -> just the host"),
                          (dict(ssoHost={"x": 1}), "FIX  ssoHost is a host name"),
                          (dict(jiraKeyPrefix=["STUDIO"]), "FIX  jiraKeyPrefix is a Jira project key"),
                          (dict(jiraKeyPrefix="STUDIO-1234"), "FIX  jiraKeyPrefix is a Jira project key")]:
            self.config(**{**good, **bad})
            out = self.doctor_out()
            self.assertIn(want, out, bad)
            self.assertNotIn("add the qaBaseUrl host to allowedHosts", out, bad)  # the wrong fix for a bad URL
        self.config(**good, jiraKeyPrefix="studio-")
        self.assertIn("ok   jiraKeyPrefix is a Jira project key", self.doctor_out())
        with self.assertRaisesRegex(qm.BadInput, "jiraKeyPrefix .* must be a Jira project key"):
            qm.expand_keys(["1234"], "STUDIO-1234")
        qm.CONFIG.write_text("[1]")
        self.assertIn("must be one JSON object", self.doctor_out())
        qm.CONFIG.write_text("{bad")
        self.assertIn("-> fix the JSON (", self.doctor_out())

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0, "needs POSIX permissions")
    def test_unreadable_folders_never_crash_and_cookie_file_is_never_read(self):
        self.config(app="x")
        qm.CONFIG.with_name("qa-state.json").write_text("SECRET-COOKIE")
        real_open = builtins.open
        def no_cookie(f, *a, **k):
            if "qa-state" in str(f):
                raise AssertionError("doctor opened the cookie file")
            return real_open(f, *a, **k)
        with mock.patch("builtins.open", no_cookie):
            self.assertIn("old cookie file present", self.doctor_out())
        for folder in (qm.CONFIG.parent, qm.DATA_DIR):  # Path.exists/is_file raised here on python < 3.12
            folder.mkdir(parents=True, exist_ok=True)
            os.chmod(folder, 0)
            try:
                out = self.doctor_out()
            finally:
                os.chmod(folder, 0o700)
            self.assertIn("python", out)
        self.assertIn("make it readable: chmod", (os.chmod(qm.CONFIG.parent, 0), self.doctor_out(),
                                                  os.chmod(qm.CONFIG.parent, 0o700))[1])


class Cli(Base):
    def test_every_command_answers_help_and_bad_options_with_usage(self):
        code, out, _ = cli()
        self.assertEqual(code, 2)
        for name in qm.COMMANDS:
            self.assertIn(f"  qa-mem {name}", out)
        self.assertEqual(cli("--help")[0], 2)
        self.assertEqual(cli("nope")[0], 2)
        for name in qm.COMMANDS:
            for args in (["--help"], ["-h"], ["--bogus"], ["x", "--help"]):
                code, out, err = cli(name, *args)
                self.assertEqual(code, 2, (name, args, out, err))
                self.assertIn(f"usage: qa-mem {name}", err + out, (name, args))
        self.assertEqual(cli("stale", "--help")[0], 2)
        con = qm.connect()
        self.addCleanup(con.close)
        self.assertIsNone(qm.meta(con, "build"))  # was: the build became "--help"
        self.assertEqual(cli("doctor", "now")[0], 2)
        self.assertEqual(cli("hash", "/form", "shows", "--", "instead")[0], 0)  # a symptom may hold "--"
        self.assertEqual(cli("code", "--repos")[0], 0)

    def test_ingest_of_a_bad_file_is_a_message(self):
        code, _, err = cli("ingest", str(self.t / "missing.json"))
        self.assertEqual(code, 2)
        self.assertIn("can't save", err)
        (self.t / "r.json").write_text("{}")
        code, _, err = cli("ingest", str(self.t / "r.json"))
        self.assertEqual((code, "run_id" in err), (2, True))

    def test_busy_db_is_a_message(self):
        with mock.patch.object(qm, "run_cmd", side_effect=qm.sqlite3.OperationalError("database is locked")):
            code, _, err = cli("stale", "1.2")
        self.assertEqual(code, 1)
        self.assertIn("stayed busy", err)


class Concurrency(Base):
    def test_overlapping_inbox_runs_keep_each_others_keys(self):
        a, b = qm.connect(), qm.connect()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        real = qm._write
        def b_runs_meanwhile(path, text):
            qm._write = real
            qm.inbox(b, self.proj, [{"key": "OPS-2"}])
            real(path, text)
        with mock.patch.object(qm, "_write", b_runs_meanwhile):
            qm.inbox(a, self.proj, [{"key": "OPS-1"}])
        self.assertEqual(sorted(json.loads(qm.meta(a, "inbox_seen"))), ["OPS-1", "OPS-2"])

    def test_scrub_keeps_an_explorers_rewrite(self):
        f = self.t / "BUG-1.md"
        f.write_text("token=abcdefabcdefabcdef1234\n")
        real = qm.redact
        def rewrite_meanwhile(text):
            time.sleep(0.01)
            f.write_text("NEW draft\n")
            return real(text)
        with mock.patch.object(qm, "redact", rewrite_meanwhile):
            qm.scrub_file(f)
        self.assertEqual(f.read_text(), "NEW draft\n")
        self.assertEqual([p.name for p in self.t.iterdir() if p.name.startswith(".BUG")], [])

    @unittest.skipIf(qm.fcntl is None, "no flock")
    def test_index_write_waits_for_another_report(self):
        import threading
        d = self.rundir(runs=[("x", {"target": "/x"})])
        fd = os.open(d.parent, os.O_RDONLY)
        qm.fcntl.flock(fd, qm.fcntl.LOCK_EX)  # another report is writing the index
        th = threading.Thread(target=qm.write_report, args=(d,))
        th.start()
        th.join(0.5)
        self.assertTrue(th.is_alive())
        self.assertFalse((d.parent / "README.md").exists())
        os.close(fd)
        th.join(5)
        self.assertIn(d.name, (d.parent / "README.md").read_text())


if __name__ == "__main__":
    unittest.main()
