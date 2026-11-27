"""`qa-mem ingest-pending` and `init-status` (qa_sdet.ingest): the hooks' intake of changed run files and code maps."""

import contextlib
import io
import json
import os
import sqlite3
import time
import unittest
from pathlib import Path
from unittest import mock

from support import PLAN, RUN_FORM, RUN_LIST, MemBase, core, run, runfile
from support import cli as qa_mem

from qa_sdet import cli, ingest, memory


class IngestPending(MemBase):
    """`qa-mem ingest-pending`: the run files under qa-out/<run>/.data."""

    def test_ingest_pending_is_idempotent_and_records_errors(self):
        out = Path(self.tmp.name) / "qa-out"
        (out / "a").mkdir(parents=True)
        (out / "b").mkdir()
        runfile(out / "a").write_text(json.dumps(run(facts=[{"kind": "note", "text": "hi"}])))
        runfile(out / "b").write_text("{not json")
        self.assertEqual(ingest.ingest_pending(out), 1)
        self.assertTrue((out / "a/.data/run-x.json.ingested").exists())
        self.assertTrue((out / "b/.data/run-x.json.error").exists())
        self.assertEqual(ingest.ingest_pending(out), 0)

    def test_odd_text_and_paths_are_ingested_not_parked(self):  # a sliced emoji, a BOM, `~user/` in draft_path
        out = Path(self.tmp.name) / "qa-out"
        for name, data in [
            ("s", json.dumps(run(facts=[{"kind": "note", "text": "emoji cut \ud83d here"}]))),
            (
                "b",
                "\ufeff"
                + json.dumps(run(facts=[{"kind": "note", "text": "Authorization: Bearer abcdefghijklmnop1234"}])),
            ),
            ("t", json.dumps(run(bugs=[{"route": "/form", "title": "t", "draft_path": "~nosuchuser9/BUG-1.md"}]))),
        ]:
            (out / name).mkdir(parents=True)
            runfile(out / name).write_text(data, encoding="utf-8")
        self.assertEqual(ingest.ingest_pending(out), 3, list(out.rglob("*.error")))
        self.assertNotIn(
            "abcdefghijklmnop1234", runfile(out / "b").read_text(encoding="utf-8")
        )  # the file is scrubbed too
        self.assertIn("emoji cut", memory.search(self.con, "emoji"))

    def test_transient_error_retries(self):
        out = Path(self.tmp.name) / "qa-out" / "a"
        out.mkdir(parents=True)
        runfile(out).write_text(json.dumps(run()))
        real = ingest.ingest
        ingest.ingest = lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))
        try:
            self.assertEqual(ingest.ingest_pending(out.parent), 0)
        finally:
            ingest.ingest = real
        self.assertFalse((out / ".data/run-x.json.error").exists())
        self.assertEqual(ingest.ingest_pending(out.parent), 1)

    def test_wrongly_typed_fields_are_parked_not_retried(self):
        out = Path(self.tmp.name) / "qa-out"
        for i, bad in enumerate(
            [
                {"pages": [{"route": ["/a"]}]},
                {"mode": {"x": 1}},
                {"bugs": [{"route": "/a", "title": "t", "severity": ["HIGH"]}]},
                {"status": 10**30, "pages": [{"route": "/a", "facts": [{"text": {"a": 1}}]}]},
            ]
        ):
            d = out / str(i)
            d.mkdir(parents=True)
            runfile(d).write_text(json.dumps({"run_id": f"r{i}", "build": "1", **bad}))
        ingest.ingest_pending(out)
        errors = sorted(p.parent.parent.name for p in out.rglob("run-x.json.error"))
        self.assertEqual(errors[:3], ["0", "1", "2"])

    def test_ingest_pending_cli_dedupes_dirs_and_content_hash(self):
        out = Path(self.tmp.name) / "qa-out" / "a"
        out.mkdir(parents=True)
        runfile(out).write_text(json.dumps(run(bugs=[{"route": "/form", "title": "t"}])))
        cli.main(["ingest-pending", str(out.parent), str(out.parent) + "/", ""])
        self.assertEqual(self.con.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
        future = time.time() + 3600  # skewed mtime must not cause re-ingest loops
        os.utime(runfile(out), (future, future))
        self.assertEqual(ingest.ingest_pending(out.parent), 0)
        self.assertLessEqual(self.con.execute("SELECT max(ts) FROM runs").fetchone()[0], core.now())

    def test_draft_paths_stored_absolute(self):
        proj = Path(self.tmp.name) / "proj"
        out = proj / "qa-out" / "form"
        (out / "bugs").mkdir(parents=True)
        (out / "bugs/BUG-1.md").write_text("x")
        runfile(out).write_text(
            json.dumps(
                run(
                    bugs=[
                        {"route": "/form", "title": "a", "draft_path": "qa-out/form/bugs/BUG-1.md"},
                        {"route": "/form", "title": "b", "draft_path": "bugs/BUG-1.md"},
                    ]
                )
            )
        )
        ingest.ingest_pending(proj / "qa-out")
        paths = {r[0] for r in self.con.execute("SELECT draft_path FROM bugs")}
        self.assertEqual(paths, {str((out / "bugs/BUG-1.md").resolve())})

    def test_a_run_file_with_an_old_mtime_is_still_redacted(self):
        out = Path(self.tmp.name) / "qa-out" / "a"
        out.mkdir(parents=True)
        f = runfile(out)
        f.write_text(
            json.dumps(
                run(facts=[{"kind": "note", "text": "login sets eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijk"}])
            )
        )
        os.utime(f, (1, 1))
        (out.parent / ".scrubbed").write_text(str(time.time()))  # scrub skips the old file
        self.con.close()
        ingest.ingest_pending(out.parent)
        self.con = core.connect()
        self.assertNotIn("eyJhbGci", " ".join(r[0] for r in self.con.execute("SELECT text FROM facts")))

    def test_run_json_in_data_with_drafts_in_bugs(self):
        d = self.rundir(plan=PLAN, runs=[("form", RUN_FORM)])
        (d / "bugs/BUG-1.md").write_text("draft")
        self.assertEqual(ingest.ingest_pending(self.proj / "qa-out"), 2)
        self.assertTrue((d / ".data/run-form.json.ingested").exists())
        self.assertTrue(
            (d / ".data/plan.json.ingested").exists()
        )  # only its questions are kept (facts and bugs come from runs)
        con = core.connect()
        try:
            self.assertEqual(
                con.execute("SELECT draft_path FROM bugs").fetchone()[0], str((d / "bugs/BUG-1.md").resolve())
            )
        finally:
            con.close()
        self.assertEqual(ingest.ingest_pending(self.proj / "qa-out"), 0)

    def test_bad_report_fields_are_parked(self):
        for bad in (
            {"cases": [{"id": "TC-1", "status": "MAYBE"}]},
            {"cases": [{"status": "PASS"}]},
            {"cases": "TC-1 PASS"},
            {"questions": "why?"},
            {"not_tested": [1]},
            {"notes": ["x"]},
            {"questions": [{"status": "open"}]},
            {"not_tested": [{"category": "safety", "text": 3}]},
            {"env": "chrome"},
            {"env": {"url": ["x"]}},
            {"env": {"context": {"role": "editor"}}},
            {"bugs": [{"route": "/x", "title": "t", "annotated": "yes"}]},
        ):
            d = self.rundir("bad")
            f = d / ".data/run-x.json"
            f.write_text(json.dumps({**RUN_LIST, "bugs": [], **bad}))
            ingest.PARKED.clear()
            with contextlib.redirect_stderr(io.StringIO()):
                ingest.ingest_pending(self.proj / "qa-out")
            self.assertTrue(f.with_name("run-x.json.error").exists(), bad)
            f.with_name("run-x.json.error").unlink()
        good = {
            **RUN_LIST,
            "cases": [{"id": "TC-1", "status": "not run"}],
            "env": {"url": "u", "browser": "firefox"},
            # question and not-tested objects are valid too
            "questions": ["a?", {"text": "b?", "status": "resolved", "answer": "yes"}],
            "not_tested": ["c", {"category": "safety", "text": "d"}],
        }
        f.write_text(json.dumps(good))
        self.assertEqual(ingest.ingest_pending(d), 1)  # a run folder finds its qa-out

    def test_deep_json_does_not_stop_other_ingests(self):  # a RecursionError in scrub used to abort the whole hook
        bad = self.rundir("a") / ".data/run-deep.json"
        bad.write_text("[" * 50000 + "]" * 50000)
        good = self.rundir("b", runs=[("ok", RUN_LIST)]) / ".data/run-ok.json"
        ingest.PARKED.clear()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ingest.ingest_pending(self.proj / "qa-out"), 1)
        self.assertTrue(good.with_name("run-ok.json.ingested").exists())
        self.assertTrue(bad.with_name("run-deep.json.error").exists())
        self.assertEqual(len(ingest.PARKED), 1)

    def test_concurrent_ingest_pending_rereads_under_lock(self):
        import fcntl
        import hashlib
        import threading

        out = Path(self.tmp.name) / "qa-out"
        d = out / "2026-11-24-race"
        d.mkdir(parents=True)
        rf = runfile(d)
        rf.write_text(json.dumps(run(facts=[{"kind": "note", "text": "OLD_STALE_FACT"}])))
        db = core.db_path()
        db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        entered_lock = threading.Event()
        real_flock = fcntl.flock
        # Hold the lock so worker B snapshots _pending before lock, while worker A ingests an updated file first.
        lock_fd = open(db.with_suffix(".lock"), "a")  # noqa: SIM115 - held across the threads, closed below
        real_flock(lock_fd, fcntl.LOCK_EX)

        def waiting_flock(fh, op):
            if fh is not lock_fd:
                entered_lock.set()
            return real_flock(fh, op)

        results = []
        with mock.patch.object(fcntl, "flock", side_effect=waiting_flock):
            t = threading.Thread(target=lambda: results.append(ingest.ingest_pending(out)))
            t.start()
            self.assertTrue(entered_lock.wait(5))
            # While B is blocked on flock, rewrite the run file and ingest it as A (holding lock_fd already).
            rf.write_text(json.dumps(run(facts=[{"kind": "note", "text": "NEW_FIXED_FACT"}])))
            con_a = core.connect()
            try:
                ingest._ingest_files(con_a, ingest._pending(out))
            finally:
                con_a.close()
            lock_fd.close()
            t.join(5)
        self.assertEqual(results, [0])  # B re-checked under the lock and did not re-ingest stale pre-lock bytes
        self.con.close()
        self.con = core.connect()
        self.assertEqual([r[0] for r in self.con.execute("SELECT text FROM facts")], ["NEW_FIXED_FACT"])
        self.assertEqual(
            (d / ".data/run-x.json.ingested").read_text().splitlines()[0],
            hashlib.sha1(rf.read_bytes(), usedforsecurity=False).hexdigest(),
        )


class InitStatus(MemBase):
    """`qa-mem init-status <init dir>`: which mapper chunks of the newest scan are settled, and which to start again."""

    def test_settled_parked_missing_and_stale_maps(self):
        import hashlib

        d = Path(self.tmp.name) / "qa-out/init"
        d.mkdir(parents=True)

        def scan(name, sid, files):
            (d / f"scan-{name}.json").write_text(
                json.dumps({"scan_id": sid, "area": name.split("-")[0], "repos": [{"repo": "/r", "files": files}]})
            )

        def cmap(name, sid, sentinel=None, note=""):
            f = d / f"code-map-{name}.json"
            f.write_text(json.dumps({"scan_id": sid, "area": name.split("-")[0], "rows": []}))
            if sentinel:
                (d / f"{f.name}.{sentinel}").write_text(
                    f"{hashlib.sha1(f.read_bytes(), usedforsecurity=False).hexdigest()}\n2026-11-25T10:00:00Z {note}\n"
                )

        new = "20261125-100000"
        scan("frontend-1", f"frontend-1-{new}", ["a.jsx"])
        scan("frontend-2", f"frontend-2-{new}", ["b.jsx"])
        scan("frontend-3", f"frontend-3-{new}", ["c.jsx"])
        scan("backend", f"backend-{new}", ["d.js"])
        scan("tests", f"tests-{new}", [])  # nothing to read: settled, no mapper
        scan("old", "old-20261120-090000", ["x.js"])  # a scan file left from an earlier scan
        cmap("frontend-1", f"frontend-1-{new}", "ingested")
        (d / "code-map-frontend-1.json.ingested").write_text(
            (d / "code-map-frontend-1.json.ingested").read_text().split("\n")[0] + "\nT\ndropped rows: {'kind': 2}\n"
        )
        cmap("frontend-2", f"frontend-2-{new}", "error", "ValueError: row 3 has no label")
        cmap("backend", "backend-20261120-090000", "ingested")  # the mapper didn't write this scan's map
        code, out, _ = qa_mem("init-status", str(d))
        st = json.loads(out)
        self.assertEqual((code, st["total"], st["ingested"]), (0, 4, 1))
        self.assertEqual(st["dropped_rows"], {str(d / "code-map-frontend-1.json"): "{'kind': 2}"})
        retry = {Path(r["out"]).name: r for r in st["retry"]}
        self.assertEqual(set(retry), {"code-map-frontend-2.json", "code-map-frontend-3.json", "code-map-backend.json"})
        self.assertIn("row 3 has no label", retry["code-map-frontend-2.json"]["reason"])
        self.assertIn("no code map", retry["code-map-frontend-3.json"]["reason"])
        self.assertIn("older scan", retry["code-map-backend.json"]["reason"])
        self.assertEqual(
            (retry["code-map-backend.json"]["area"], retry["code-map-backend.json"]["scan"]),
            ("backend", str(d / "scan-backend.json")),
        )


if __name__ == "__main__":
    unittest.main()
