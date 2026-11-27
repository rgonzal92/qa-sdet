"""qa-mem operations (qa_sdet.ops): keys, rundir, the run marker and prune."""

import importlib.util
import json
import os
import stat
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from support import Base, cli, core, done

from qa_sdet import evidence, ops


class Keys(Base):
    """`qa-mem keys`: ticket numbers and keys become full Jira keys."""

    def test_expansion_validation_and_order(self):
        self.assertEqual(
            ops.expand_keys(["1234", "ops-7,1250", "PROJ-1234"], "proj-"), ["PROJ-1234", "OPS-7", "PROJ-1250"]
        )
        self.assertEqual(ops.expand_keys(["1 2,3"], "PROJ"), ["PROJ-1", "PROJ-2", "PROJ-3"])
        for bad in ("abc", "PROJ-", "1-PROJ", "PROJ-12a", "S-1", "ß-12", "ﬀ-1"):  # ß.upper() is SS
            with self.assertRaises(core.BadInput) as e:
                ops.expand_keys([bad], "PROJ")
            self.assertIn(repr(bad), str(e.exception))
        with self.assertRaisesRegex(core.BadInput, "jiraKeyPrefix"):
            ops.expand_keys(["1234"], None)
        self.assertEqual(ops.expand_keys(["OPS-7"], None), ["OPS-7"])  # full keys need no prefix

    def test_cli(self):
        self.config(jiraKeyPrefix="PROJ")
        code, out, _ = cli("keys", "1234", "proj-1250")
        self.assertEqual((code, json.loads(out)), (0, ["PROJ-1234", "PROJ-1250"]))
        code, out, err = cli("keys", "12;rm")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("12;rm", err)
        self.assertEqual(cli("keys")[0], 2)
        self.config()
        self.assertIn("set jiraKeyPrefix", cli("keys", "5")[2])

    def test_only_ascii_digits(self):  # OPS-١٢٣ was accepted and would go to Jira as is
        for bad in ("OPS-\u0661\u0662", "\u0661\u0662"):
            with self.assertRaises(core.BadInput):
                ops.expand_keys([bad], "OPS")


class RunDir(Base):
    """`qa-mem rundir`: the qa-out/<KEY> folder of one ticket, or the qa-out/<date>-<label> folder of one run."""

    def test_dot_leading_last_segment_keeps_its_name(self):
        self.assertEqual(
            [ops.route_slug(r) for r in ("/.well-known", "/form.html", "/", "/a/b.c.d")],
            [".well-known", "form", "home", "a-b.c"],
        )

    def test_labels(self):
        self.assertEqual(ops.run_label(["A-1", "B-2"]), "A-1+B-2")
        self.assertEqual(ops.run_label(["A-1", "A-2", "A-3", "A-4", "A-5"]), "A-1+A-2+A-3+2more")
        for route, slug in (
            ("/form.html", "form"),
            ("/incidents/list", "incidents-list"),
            ("/incidents/4521", "incidents-id"),
            ("/#/zones", "zones"),
            ("/#!/zones", "zones"),
            ("/", "home"),
            ("/a?x=1", "a"),
        ):
            self.assertEqual(ops.run_label(route=route), slug, route)
        self.assertEqual(ops.run_label(keys=["A-1"], prefix="plan-"), "plan-A-1")
        self.assertEqual(ops.run_label(label="init crawl/../x"), "init-crawl-..-x")

    def test_folders_suffix_and_git_exclude(self):
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True)
        exclude = self.proj / ".git/info/exclude"
        exclude.write_text("# mine\nqa-out")  # one entry already there, no trailing newline
        code, out, _ = cli("rundir", str(self.proj), "--route", "/form.html")
        run = Path(out.strip())
        self.assertEqual(code, 0)
        self.assertTrue(run.is_absolute())
        self.assertRegex(run.name, r"^\d{4}-\d{2}-\d{2}-form$")
        self.assertEqual(run.parent, self.proj.resolve() / "qa-out")
        self.assertEqual(sorted(p.name for p in run.iterdir()), [".data", "bugs", "evidence", "repro", "scratch"])
        self.assertEqual(stat.S_IMODE(run.stat().st_mode), 0o700)
        second = Path(cli("rundir", str(self.proj), "--route", "/form.html")[1].strip())
        third = Path(cli("rundir", str(self.proj), "--route", "/form.html")[1].strip())
        self.assertEqual((second.name, third.name), (run.name + "-2", run.name + "-3"))
        lines = exclude.read_text().splitlines()
        self.assertEqual(lines, ["# mine", "qa-out", ".playwright-cli/"])  # added once, no dupes

    def test_a_ticket_folder_is_its_key_and_reused(self):  # one folder per ticket, updated in place on every re-run
        code, out, err = cli("rundir", str(self.proj), "--keys", "ab-1")
        run = Path(out.strip())
        self.assertEqual((code, err, run), (0, "", self.proj.resolve() / "qa-out" / "AB-1"))
        self.assertEqual(sorted(p.name for p in run.iterdir()), [".data", "bugs", "evidence", "repro", "scratch"])
        self.assertEqual(stat.S_IMODE(run.stat().st_mode), 0o700)
        (run / ".data/plan.json").write_text("{}")
        (run / "scratch").rmdir()  # the teardown may have emptied it; a missing sub-folder comes back
        code, out, _ = cli("rundir", str(self.proj), "--keys", "AB-1")
        self.assertEqual((code, Path(out.strip())), (0, run))
        self.assertEqual((run / ".data/plan.json").read_text(), "{}")
        self.assertTrue((run / "scratch").is_dir())
        self.assertEqual(sorted(p.name for p in run.parent.iterdir()), ["AB-1"])

    def test_a_ticket_folder_takes_one_key_and_no_prefix(self):
        code, out, err = cli("rundir", str(self.proj), "--keys", "AB-1,AB-2")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("one `qa-mem rundir --keys` call per ticket", err)
        code, out, err = cli("rundir", str(self.proj), "--keys", "AB-1", "--prefix", "plan-")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--prefix", err)
        self.assertFalse((self.proj / "qa-out").exists())

    def test_a_ticket_folder_name_that_is_no_folder_is_refused(self):  # a planted link must not take the run's files
        (self.proj / "qa-out").mkdir()
        (self.proj / "qa-out/AB-1").write_text("x")
        (self.t / "elsewhere").mkdir()
        (self.proj / "qa-out/AB-2").symlink_to(self.t / "elsewhere")
        for key in ("AB-1", "AB-2"):
            code, out, err = cli("rundir", str(self.proj), "--keys", key)
            self.assertEqual((code, out), (2, ""), key)
            self.assertIn("not a folder", err)
        self.assertEqual((self.proj / "qa-out/AB-1").read_text(), "x")
        self.assertEqual(list((self.t / "elsewhere").iterdir()), [])

    def test_non_git_and_bad_input(self):
        code, out, _ = cli("rundir", str(self.proj), "--route", "/form.html", "--prefix", "plan-")
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().endswith("-plan-form"))
        self.assertEqual(cli("rundir", str(self.proj))[0], 2)  # no label source
        self.assertEqual(cli("rundir", str(self.proj), "--keys", "AB-1", "--route", "/x")[0], 2)
        self.assertEqual(cli("rundir", str(self.t / "nope"), "--label", "x")[0], 2)
        self.assertEqual(cli("rundir", str(self.proj), "--keys", "bad key")[0], 2)

    def test_long_label_and_repeated_flag(self):  # a 300-char label crashed (name too long); --keys twice kept one
        code, out, err = cli("rundir", str(self.proj), "--label", "speed limit form " * 20)
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(Path(out.strip()).is_dir())
        self.assertLessEqual(len(ops.run_label(route="/" + "a/" * 200, prefix="plan-")), 80)
        code, _, err = cli("rundir", str(self.proj), "--keys", "AB-1", "--keys", "AB-2")
        self.assertEqual(code, 2)
        self.assertIn("--keys given twice", err)


class Marker(Base):
    """`qa-mem marker`: the run marker the guard reads."""

    def test_start_then_stop(self):
        code, out, _ = cli("marker", "start", str(self.proj))
        m = json.loads((self.proj / "qa-out/.active").read_text())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), m)
        self.assertEqual(sorted(m), ["api_writes", "expires", "faults"])  # no budget, deadline or turn count
        self.assertIs(m["faults"], False)
        self.assertAlmostEqual(m["expires"], time.time() + 8 * 3600, delta=5)
        cli("marker", "start", str(self.proj), "--faults")
        self.assertIs(json.loads((self.proj / "qa-out/.active").read_text())["faults"], True)
        self.assertEqual([p.name for p in (self.proj / "qa-out").iterdir()], [".active"])  # no temp file left
        (self.proj / "qa-out/.active.progress").write_text("x")
        self.assertEqual(cli("marker", "stop", str(self.proj))[0], 0)
        self.assertEqual(list((self.proj / "qa-out").iterdir()), [])
        self.assertEqual(cli("marker", "stop", str(self.proj))[0], 0)  # already gone: fine

    def start_run(self, slug="form"):
        d = self.proj / "qa-out/2026-11-25-PROJ-1"
        (d / ".data").mkdir(parents=True, exist_ok=True)
        return d, ops.marker_start(self.proj, run_dir=d, slug=slug)["expires"]

    def lines(self, e, *recs):
        with (self.proj / "qa-out/.active.progress").open("a") as f:
            f.writelines(json.dumps({"e": e, **r}) + "\n" for r in recs)

    def summaries(self):
        log = self.proj / "qa-out/plugin-log.jsonl"
        return [r for r in map(json.loads, log.read_text().splitlines()) if r["type"] == "run_summary"]

    def test_stop_keeps_the_runs_trace_beside_its_run_file(self):
        """The hooks' lines of this run move to <run>/.data/trace-<slug>.jsonl: a resumed route adds to them."""
        d, e = self.start_run()
        self.lines(e - 1, {"t": "pw", "ts": 1})  # an older run's line
        self.lines(e, {"t": "q", "ts": 2, "d": "a", "k": "k1"}, {"t": "pw", "ts": 3, "k": "k1"})
        with (self.proj / "qa-out/.active.progress").open("a") as f:
            f.write("{oops\n")
        ops.marker_stop(self.proj)
        trace = d / ".data/trace-form.jsonl"
        self.assertEqual([r["ts"] for r in map(json.loads, trace.read_text().splitlines())], [2, 3])
        self.assertEqual(stat.S_IMODE(trace.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in (self.proj / "qa-out").iterdir()), [d.name, "plugin-log.jsonl"])
        _, e2 = self.start_run()  # the route resumed
        self.lines(e2 + 1, {"t": "q", "ts": 4})
        self.lines(e2, {"t": "q", "ts": 5})
        ops.marker_stop(self.proj)
        self.assertEqual([r["ts"] for r in map(json.loads, trace.read_text().splitlines())], [2, 3, 5])

    def test_the_kept_trace_holds_verbs_not_what_was_typed(self):
        """A line's `s` is the verb and its first argument while the run needs it (the stuck nudge); the argument can be
        a word of page text or a typed value, so the trace that stays keeps the verb only."""
        d, e = self.start_run()
        self.lines(
            e, {"t": "pw", "ts": 1, "s": "find 'Saved", "k": "k1"}, {"t": "pw", "ts": 2, "s": "close", "k": "k2"}
        )
        self.lines(e, {"t": "n", "n": "stuck", "s": "fill e5"})
        ops.marker_stop(self.proj)
        kept = (d / ".data/trace-form.jsonl").read_text()
        self.assertEqual([r.get("s") for r in map(json.loads, kept.splitlines())], ["find", "close", "fill"])
        self.assertNotIn("Saved", kept)

    def test_a_crawl_or_a_planted_link_leaves_no_trace(self):
        ops.marker_start(self.proj)  # crawl and discover: no run file to keep it beside
        self.lines(1, {"t": "o"})
        ops.marker_stop(self.proj)
        self.assertEqual(list((self.proj / "qa-out").iterdir()), [])
        victim = self.t / "victim.txt"
        victim.write_text('{"e": 1}\n')
        d, e = self.start_run()
        (self.proj / "qa-out/.active.progress").symlink_to(victim)
        ops.marker_stop(self.proj)
        self.assertFalse((d / ".data/trace-form.jsonl").exists())
        self.assertFalse((self.proj / "qa-out/.active.progress").is_symlink())
        _, e = self.start_run()
        self.lines(e, {"t": "o", "ts": 1})
        (d / ".data/trace-form.jsonl").symlink_to(victim)  # never written through
        ops.marker_stop(self.proj)
        self.assertEqual(victim.read_text(), '{"e": 1}\n')
        self.assertFalse((self.proj / "qa-out/.active.progress").exists())

    def test_the_run_summary_has_the_traces_totals(self):
        """Where a run's time went: the guard's approvals, Claude Code's own check, the model, and after the cases."""
        _, e = self.start_run()
        self.lines(
            e,
            {"t": "q", "ts": 100.0, "d": "a", "k": "k1", "id": "i1"},
            {"t": "q", "ts": 100.25, "d": "c", "w": "jq", "k": "k5", "id": "i5"},  # in parallel with i1
            {"t": "pw", "ts": 100.5, "k": "k1", "id": "i1", "ok": 1, "p": 1},
            {"t": "o", "ts": 101.0, "k": "k5", "id": "i5", "p": 0},
            {"t": "q", "ts": 102.0, "d": "c", "w": "jq", "k": "k2"},  # no ids: paired by key
            {"t": "n", "n": "slow:jq"},
            {"t": "o", "ts": 107.5, "k": "k2", "p": 0},
            {"t": "q", "ts": 108.0, "d": "d", "w": "playwright-cli goto to non-allowlisted host", "k": "k3"},
            {"t": "w", "ts": 110.0, "p": 1, "f": "run"},  # the cases are done
            {"t": "q", "ts": 111.0, "d": "c", "w": "a loop", "k": "k4", "id": "i4"},
            {"t": "pw", "ts": 116.0, "k": "k4", "id": "i4", "ok": 1, "p": 0},
        )
        ops.marker_stop(self.proj)
        (s,) = self.summaries()
        self.assertEqual(s["turns"], 5)  # the results, never the request lines or nudges
        self.assertEqual(
            {k: s[k] for k in ("calls", "approved", "unapproved", "denied", "approved_s", "unapproved_s")},
            {"calls": 5, "approved": 1, "unapproved": 3, "denied": 1, "approved_s": 0.5, "unapproved_s": 11.25},
        )
        self.assertEqual(s["model_s"], 1.0 + 0.5 + 1.0)  # each request after the last result; parallel ones add none
        self.assertEqual(s["denials"], {"playwright-cli goto to non-allowlisted host": 1})
        self.assertEqual(s["blockers"], {"jq": 2, "a loop": 1})
        self.assertEqual(s["after_cases_s"], 6.0)

    def test_a_run_without_a_trace_or_a_checkpoint_has_zero_totals(self):
        self.start_run()
        ops.marker_stop(self.proj)
        (s,) = self.summaries()
        self.assertEqual(
            {k: s[k] for k in ("calls", "approved_s", "unapproved_s", "model_s", "after_cases_s")},
            {"calls": 0, "approved_s": 0, "unapproved_s": 0, "model_s": 0, "after_cases_s": 0},
        )
        self.assertEqual((s["denials"], s["blockers"]), ({}, {}))

    def test_api_writes_is_a_flag_of_the_run_and_off_by_default(self):
        cli("marker", "start", str(self.proj))
        self.assertIs(json.loads((self.proj / "qa-out/.active").read_text())["api_writes"], False)
        cli("marker", "start", str(self.proj), "--api-writes")
        self.assertIs(json.loads((self.proj / "qa-out/.active").read_text())["api_writes"], True)

    def test_bad_input(self):
        for argv in (
            ("marker",),
            ("marker", "go", str(self.proj)),
            ("marker", "start", str(self.proj), "--bogus"),
            ("marker", "start", str(self.proj), "--budget", "5"),  # the run limits are gone
            ("marker", "start", str(self.proj), "--minutes", "5"),
            ("marker", "start", str(self.proj), "--max-turns", "5"),
        ):
            self.assertEqual(cli(*argv)[0], 2, argv)
        self.assertFalse((self.proj / "qa-out/.active").exists())

    def test_stop_on_a_missing_project_is_an_error(self):  # `marker stop /typo` said "marker removed"
        code, out, err = cli("marker", "stop", str(self.t / "nope"))
        self.assertEqual(code, 2)
        self.assertNotIn("marker removed", out)

    def test_missing_project_is_not_created(self):  # marker start /typo made /typo/qa-out
        self.assertEqual(cli("marker", "start", str(self.t / "nope" / "proj"))[0], 2)
        self.assertFalse((self.t / "nope").exists())


class Prune(Base):
    """`qa-mem prune`: old snapshot files and a run's temp files."""

    def test_old_regular_files_only(self):
        cli_dir = self.proj / ".playwright-cli"
        cli_dir.mkdir()
        old = time.time() - 8 * 86400
        (cli_dir / "old.yml").write_text("x")
        os.utime(cli_dir / "old.yml", (old, old))
        (cli_dir / "new.yml").write_text("x")
        outside = self.t / "outside.txt"
        outside.write_text("keep")
        os.utime(outside, (old, old))
        (cli_dir / "link").symlink_to(outside)
        os.utime(cli_dir / "link", (old, old), follow_symlinks=False)
        code, out, _ = cli("prune", str(self.proj))
        self.assertEqual((code, out.strip()), (0, "pruned 1 old snapshot files"))
        self.assertEqual(sorted(p.name for p in cli_dir.iterdir()), ["link", "new.yml"])
        self.assertTrue(outside.exists())
        self.assertEqual(cli("prune", str(self.t / "empty"))[0], 0)  # nothing there: fine
        # with the run folder: this run's temp files go, what the report needs stays
        d = self.proj / "qa-out" / "2026-11-24-r"
        for rel in (
            ".data/plan.json",
            ".data/run-form.json",
            ".data/snapshot.md",
            ".data/map-1.png",
            "evidence/TC-1.png",
            "bugs/BUG-1.md",
            "repro/BUG-1.spec.ts",
            "REPORT.md",
        ):
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text("x")
        start = time.time() - 600
        os.utime(d / ".data/plan.json", (start, start))  # the run began here
        before = time.time() - 3600
        os.utime(cli_dir / "new.yml", (before, before))  # a snapshot from an earlier run
        (cli_dir / "during.yml").write_text("x")
        code, out, _ = cli("prune", str(self.proj), str(d))
        self.assertEqual((code, out.strip()), (0, "pruned 3 snapshot and temp files of this run"))
        self.assertEqual(sorted(p.name for p in cli_dir.iterdir()), ["link", "new.yml"])
        self.assertEqual(
            sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()),
            [
                ".data/plan.json",
                ".data/run-form.json",
                "REPORT.md",
                "bugs/BUG-1.md",
                "evidence/TC-1.png",
                "repro/BUG-1.spec.ts",
            ],
        )
        self.assertEqual(cli("prune", str(self.proj), str(self.t))[0], 2)  # not a run folder
        # a transient temp file disappearing between os.walk and os.lstat must not crash prune
        real_lstat = os.lstat
        vanished = d / ".data/tmp.scrub"
        vanished.write_text("x")

        def disappearing(p, *a, **k):
            if str(p) == str(vanished):
                raise FileNotFoundError(str(p))
            return real_lstat(p, *a, **k)

        with mock.patch.object(os, "lstat", side_effect=disappearing):
            self.assertEqual(ops.prune(self.proj, run=d), 0)

    def test_round2_ops_browser_open_prune_and_annotate(self):
        # M3: page title containing 'already in use' on exit 0 must not trigger profile-in-use error
        self.config(browser="chrome")
        ok_out = "### Page\n- Page Title: Port already in use\n"
        with mock.patch.object(subprocess, "run", side_effect=[done(0), done(0, out=ok_out)]):
            code, out, err = cli("browser-open", "https://qa.example/")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Port already in use", out)
        # L3: empty crashed run folder still prunes snapshots created since folder creation
        cli_dir = self.proj / ".playwright-cli"
        cli_dir.mkdir(exist_ok=True)
        old_snap = cli_dir / "before.yml"
        old_snap.write_text("old")
        t0 = time.time() - 3600
        os.utime(old_snap, (t0, t0))
        empty_run = self.proj / "qa-out/2026-11-24-crashed"
        empty_run.mkdir(parents=True)
        os.utime(empty_run, (time.time() - 60, time.time() - 60))
        (cli_dir / "after.yml").write_text("new")
        self.assertEqual(ops.prune(self.proj, run=empty_run), 1)
        self.assertTrue(old_snap.exists())
        self.assertFalse((cli_dir / "after.yml").exists())

    def test_a_runs_scratch_files_go_at_teardown(self):
        """<run>/scratch/ takes the explorer's throwaway snapshots and screenshots: page data nobody scrubbed, so the
        teardown's prune empties it and keeps the evidence."""
        run = self.rundir("2026-11-25-form")
        (run / "scratch").mkdir()
        (run / "scratch/page.yml").write_text("- button Save")
        (run / "scratch/look.png").write_bytes(b"png")
        (run / "evidence/BUG-1.png").write_bytes(b"png")
        self.assertEqual(ops.prune(self.proj, run=run), 2)
        self.assertEqual(list((run / "scratch").iterdir()), [])
        self.assertTrue((run / "evidence/BUG-1.png").exists())

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow not installed")  # optional at run time
    def test_a_pillow_value_error_is_bad_input_not_a_traceback(self):  # L6: e.g. a huge zTXt chunk
        from PIL import Image

        png = self.t / "run/evidence/BUG-1.png"
        png.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (20, 20)).save(png)
        with (
            mock.patch.object(Image, "open", side_effect=ValueError("Decompressed data too large")),
            self.assertRaises(core.BadInput),
        ):
            evidence.annotate(png, [(0, 0, 10, 10)], "x")


if __name__ == "__main__":
    unittest.main()
