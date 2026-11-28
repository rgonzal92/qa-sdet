"""Ingest of run files (qa_sdet.ingest): build staleness, and what a run's facts, coverage and locators become."""

import os
import unittest
from unittest import mock

from support import MemBase, core, run

from qa_sdet import cards, cli, ingest, memory


class Builds(MemBase):
    """A new build marks old knowledge stale; a plan or an unreadable build never does."""

    def test_new_build_marks_old_knowledge_stale(self):
        ingest.ingest(
            self.con,
            run("1.0", facts=[{"kind": "rule", "text": "old rule"}], locators=[{"name": "Save", "locator": "x"}]),
        )
        ingest.ingest(self.con, run("2.0", facts=[{"kind": "rule", "text": "new rule"}]))
        c = cards.card(self.con, "/form")
        self.assertIn("old rule *", c)
        self.assertNotIn("new rule *", c)
        self.assertNotIn("LOCATORS", c)  # stale locator hidden
        # coverage from build 1.0 no longer counts
        self.assertIn("boundary", c.split("UNTRIED: ")[1])

    def test_stale_command_resets_coverage_view(self):
        ingest.ingest(self.con, run("1.0", coverage=[{"target": "x", "heuristic": "boundary"}]))
        cli.main(["stale", "2.0"])
        c = cards.card(self.con, "/form")
        self.assertIn("re-verify", c)
        self.assertIn("boundary", c.split("UNTRIED: ")[1])

    def test_a_corrected_build_makes_knowledge_current_again(self):
        ingest.ingest(self.con, run("1.43.0", facts=[{"kind": "rule", "text": "speed max 130"}]))
        cli.main(["stale", "1.44.0"])
        self.assertEqual(self.con.execute("SELECT max(stale) FROM facts").fetchone()[0], 1)
        cli.main(["stale", "1.43.0"])  # corrected: what was verified on 1.43.0 is current again
        self.assertEqual(self.con.execute("SELECT max(stale) FROM facts").fetchone()[0], 0)

    def test_unknown_build_plan_does_not_stale_real_knowledge(self):
        ingest.ingest(
            self.con,
            run("1.0", facts=[{"kind": "rule", "text": "real"}], coverage=[{"target": "x", "heuristic": "boundary"}]),
        )
        ingest.ingest(self.con, run("unknown", facts=[{"kind": "rule", "text": "from ticket"}]))
        c = cards.card(self.con, "/form")
        self.assertIn("build=1.0", c)
        self.assertNotIn("real *", c)
        self.assertIn("boundary(", c)

    def test_unreadable_build_spellings(self):
        ingest.ingest(self.con, run("1.42.0", facts=[{"kind": "rule", "text": "real"}]))
        for b in ["undefined", "unknown (footer .version not found)", "Unknown", "UNKNOWN", "null", "N/A", ""]:
            ingest.ingest(self.con, run(b, coverage=[{"target": "t", "heuristic": "duplicate"}]))
        c = cards.card(self.con, "/form")
        self.assertIn("build=1.42.0", c)
        self.assertNotIn("real *", c)
        self.assertEqual({r[0] for r in self.con.execute("SELECT DISTINCT build FROM coverage")}, {"unknown"})
        self.assertIn("duplicate(", c)  # unknown-build coverage visible (within the week)

    def test_build_whitespace_is_not_a_new_deploy(self):
        ingest.ingest(self.con, run("Build:\n   1.42", facts=[{"kind": "rule", "text": "real"}]))
        ingest.ingest(self.con, run("Build: 1.42", facts=[{"kind": "rule", "text": "other"}]))
        self.assertNotIn("real *", cards.card(self.con, "/form"))
        ingest.ingest(self.con, run("x" * 5000, bugs=[{"route": "/form", "title": "t", "severity": "H" * 500}]))
        self.assertLessEqual(len(core.meta(self.con, "build")), core.MAX_TEXT)
        self.assertLessEqual(len(self.con.execute("SELECT severity FROM bugs").fetchone()[0]), 20)

    def test_a_late_run_of_an_older_build_does_not_roll_back_the_page_or_the_build(self):
        ingest.ingest(self.con, run("1.1", facts=[{"kind": "rule", "text": "fresh"}]), "2026-11-22T10:00:00Z")
        ingest.ingest(self.con, run("1.0", facts=[{"kind": "rule", "text": "old"}]), "2026-11-21T10:00:00Z")
        c = cards.card(self.con, "/form")
        self.assertIn("build=1.1", c)
        self.assertEqual(core.meta(self.con, "build"), "1.1")
        self.assertIn("fresh\n", c + "\n")
        self.assertNotIn("fresh *", c)

    def test_late_run_of_the_current_build_still_adds_what_it_saw(self):
        ingest.ingest(
            self.con,
            run("2.0", locators=[{"name": "Add", "locator": "getByRole('button', { name: 'Add' })"}]),
            "2026-11-22T10:00:00Z",
        )
        ingest.ingest(
            self.con,
            run(
                "2.0",
                facts=[{"kind": "rule", "text": "code max 6"}],
                locators=[{"name": "Code", "locator": "getByLabel('Code')"}],
            ),
            "2026-11-22T09:00:00Z",
        )
        names = {r[0] for r in self.con.execute("SELECT name FROM locators")}
        self.assertEqual(names, {"Add", "Code"})
        self.assertEqual(self.con.execute("SELECT stale FROM facts WHERE text='code max 6'").fetchone()[0], 0)
        ingest.ingest(self.con, run("1.0", locators=[{"name": "Old", "locator": "#old"}]), "2026-11-21T09:00:00Z")
        self.assertNotIn("Old", {r[0] for r in self.con.execute("SELECT name FROM locators")})  # older build: skipped

    def test_a_plan_does_not_refresh_a_stale_fact(self):
        ingest.ingest(self.con, run("1.0", facts=[{"kind": "rule", "text": "r"}]), "2026-11-20T10:00:00Z")
        ingest.ingest(self.con, run("1.1"), "2026-11-21T10:00:00Z")  # r now stale
        ingest.ingest(self.con, run("unknown", facts=[{"kind": "rule", "text": "r"}]), "2026-11-22T10:00:00Z")
        self.assertEqual(self.con.execute("SELECT stale, build FROM facts").fetchone()[:], (1, "1.0"))

    def test_coverage_of_an_unreadable_build_expires_after_a_week(self):
        ingest.ingest(self.con, run("unknown", coverage=[{"target": "t", "heuristic": "duplicate"}]))
        self.assertIn("duplicate(", cards.card(self.con, "/form"))
        ingest.ingest(
            self.con, run("unknown", coverage=[{"target": "t", "heuristic": "boundary"}]), "2020-01-01T00:00:00Z"
        )
        self.assertNotIn("boundary(", cards.card(self.con, "/form"))

    def test_locators_of_an_unreadable_build_expire_after_a_week(self):  # as its coverage: a later deploy may drop them
        ingest.ingest(self.con, run("unknown", locators=[{"name": "Old", "locator": "#old"}]), "2020-01-01T00:00:00Z")
        ingest.ingest(self.con, run("unknown", locators=[{"name": "New", "locator": "#new"}]))
        self.assertIn("LOCATORS (fresh): New=#new\n", cards.card(self.con, "/form") + "\n")
        ingest.ingest(
            self.con,
            {
                "run_id": "k",
                "build": "1.0",
                "pages": [{"route": "/map", "locators": [{"name": "Layer", "locator": "#layer"}]}],
            },
            "2020-01-01T00:00:00Z",
        )
        self.assertIn("LOCATORS (fresh): Layer=#layer", cards.card(self.con, "/map"))  # a read build: no age limit

    def test_the_cards_locators_are_listed_once_and_frame_safe_ones_first(self):
        """An app shell loads its pages in an iframe: a locator saved without the frame in front fails as given, and the
        same locator saved under three names took three of the card's 20 places."""
        frame = "locator('iframe[name=\"module\"]').contentFrame()."
        locs = lambda *pairs: [{"name": n, "locator": v} for n, v in pairs]
        ingest.ingest(
            self.con, run("1.0", locators=locs(("Save", f"{frame}getByTestId('save')"))), "2026-11-20T10:00:00Z"
        )
        newer = locs(
            ("Save button", "getByTestId('save')"),
            ("Save btn", "getByTestId('save')"),
            ("Save (toolbar)", "getByTestId('save')"),
            ("Name field", "getByLabel('Name')"),
        )
        ingest.ingest(self.con, run("1.0", locators=newer), "2026-11-21T10:00:00Z")
        line = next(x for x in cards.card(self.con, "/form").splitlines() if x.startswith("LOCATORS (fresh)"))
        self.assertEqual(line.count("getByTestId('save')"), 2)  # once with the frame, once without: not four times
        self.assertLess(line.index(frame), line.index("Name field"))  # the one that works as given comes first
        card = cards.card(self.con, "/form")
        self.assertIn(f"FRAME: this page loads in a frame: put `{frame}` in front of a locator that lacks it", card)

    def test_a_frame_spelled_two_ways_is_one_locator_and_no_code_hint(self):
        """A crawl names the frame by its place among the shown ones, `generate-locator` by its id: the card listed the
        element once for each, and once more under FROM CODE."""
        shown, named = "locator('iframe:visible').nth(0).contentFrame().", "locator('#module').contentFrame()."
        locs = lambda *pairs: [{"name": n, "locator": v} for n, v in pairs]
        crawled = locs(("Save", f"{shown}getByTestId('save')"), ("Msg", f"{shown}locator('#msg')"))
        ingest.ingest(self.con, run("1.0", locators=crawled), "2026-11-20T10:00:00Z")
        ingest.ingest(
            self.con, run("1.0", locators=locs(("Save button", f"{named}getByTestId('save')"))), "2026-11-21T10:00:00Z"
        )
        with self.con:
            for label, value in (("Save", "getByTestId('save')"), ("Msg", "#msg"), ("Name", "getByLabel('Name')")):
                self.con.execute(
                    "INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("/form", "locator", label, value, "src/F.jsx:1", "/repo", "frontend", core.now(), "src/F.jsx"),
                )
        card = cards.card(self.con, "/form")
        self.assertEqual(card.count("getByTestId('save')"), 1)
        self.assertIn(f"Save button={named}getByTestId('save')", card)  # the newest spelling
        self.assertEqual(card.count("#msg"), 1)
        self.assertIn("- locators to try: Name=getByLabel('Name')\n", card + "\n")  # the one no run has confirmed

    def test_a_page_without_a_framed_locator_borrows_the_frame_of_the_other_pages(self):
        """Found live: an app shell draws every page in one iframe, and the card of a page whose locators were all
        saved without it had no FRAME line, so the explorer acted by refs (21 `find` calls in one run)."""
        frame, widget = "locator('#module').contentFrame().", "locator('#chat').contentFrame()."
        page = lambda route, *pairs: {"route": route, "locators": [{"name": n, "locator": v} for n, v in pairs]}
        save = lambda rid, build, *pages: ingest.ingest(self.con, {"run_id": rid, "build": build, "pages": list(pages)})
        hint = (
            "FRAME: no locator of this page names a frame yet; {} in one: try `{}` in front of a locator that lacks "
            "it (a FROM CODE one too)"
        )
        save("a", "1.0", page("/map", ("Layer", "#layer")))
        self.assertNotIn("FRAME:", cards.card(self.con, "/map"))  # no page names a frame: nothing to say
        self.assertNotIn("FRAME:", cards.card(self.con, "/never-seen"))
        save(
            "b",
            "1.0",
            page("/zones", ("Save", f"{frame}getByTestId('save')")),
            page("/alarms", ("Ack", f"{frame}getByTestId('ack')")),
            page("/help", ("Send", f"{widget}getByTestId('send')")),
        )
        most = hint.format("2 other pages load", frame)  # the frame of most pages, not the help widget's
        self.assertIn(most, cards.card(self.con, "/map").splitlines())
        self.assertIn(most, cards.card(self.con, "/never-seen").splitlines())  # a first visit starts with it too
        own = cards.card(self.con, "/alarms")
        self.assertIn(f"FRAME: this page loads in a frame: put `{frame}` in front", own)  # its own: no borrowing
        self.assertNotIn("other page", own)
        save("c", "2.0", page("/zones"), page("/alarms"))  # a new build: their locators are stale until seen again
        self.assertIn(hint.format("1 other page loads", widget), cards.card(self.con, "/map").splitlines())

    def test_a_page_whose_framed_locators_sit_in_a_second_frame_gets_the_apps_frame_first(self):
        """Found live: a page of an app shell opened a panel in a second iframe, a run saved the panel's locators with
        that frame, and the card said to put the panel's frame in front of the page's other locators, which sit in
        the frame every page loads in: none of them matched."""
        frame, panel = "locator('#module').contentFrame().", "locator('#panel').contentFrame()."
        page = lambda route, *pairs: {"route": route, "locators": [{"name": n, "locator": v} for n, v in pairs]}
        save = lambda rid, *pages: ingest.ingest(self.con, {"run_id": rid, "build": "1.0", "pages": list(pages)})
        line = lambda route: next(x for x in cards.card(self.con, route).splitlines() if x.startswith("FRAME:"))
        own = "FRAME: this page loads in a frame: put `{}` in front of a locator that lacks it (a FROM CODE one too)"
        billing = "locator('#billing').contentFrame()."
        save(
            "a",
            page("/billing", ("Pay", f"{billing}getByTestId('pay')")),
            page("/users", ("Add", "locator('#users').contentFrame().getByTestId('add')")),
        )
        self.assertEqual(line("/billing"), own.format(billing))  # no frame is the app's: the page's own stands
        save(
            "b",
            page("/zones", ("Save", f"{frame}getByTestId('save')")),
            page("/alarms", ("Ack", f"{frame}getByTestId('ack')")),
            page("/signals", ("Detail", "getByTestId('detail')"), ("Variant", f"{panel}getByTestId('variant')")),
        )
        self.assertEqual(
            line("/signals"),
            f"FRAME: 2 other pages load in a frame: try `{frame}` in front of a locator that lacks it (a FROM CODE one "
            f"too); only if that matches nothing, `{panel}`, which locators of this page name",
        )
        save("c", page("/signals", ("Tab", f"{frame}getByText('Configuration')")))
        self.assertEqual(line("/signals"), own.format(frame))  # a locator of its own names the app's frame: no guess

    def test_a_locator_saved_with_its_frame_replaces_the_one_saved_without(self):
        """Found live: a crawl saved 124 locators behind their frame beside the same ones an earlier run had saved
        without it under other names. Both stayed fresh, and the one without matches nothing as written."""
        frame = "locator('iframe:visible').nth(0).contentFrame()."
        named = lambda *pairs: [{"name": n, "locator": v} for n, v in pairs]
        ingest.ingest(
            self.con,
            run(
                locators=named(
                    ("Ack all (not History)", "getByTestId('ack-all')"),
                    ("Ack all, page object", "this.page.getByTestId('ack-all')"),
                    ("Message", "#msg"),
                    ("Other", "getByTestId('other')"),
                    ("A class, not the tag", "div.msg"),
                )
            ),
        )
        ingest.ingest(
            self.con,
            run(
                locators=named(
                    ("Ack all", f"{frame}getByTestId('ack-all')"),
                    ("Msg", f"{frame}locator('#msg')"),
                    ("Tag", f"{frame}locator('msg')"),
                )
            ),
        )
        names = {n for (n,) in self.con.execute("SELECT name FROM locators WHERE page='/form'")}
        self.assertEqual(names, {"Ack all", "Msg", "Tag", "Other", "A class, not the tag"})

    def test_the_card_says_how_to_reach_the_locators_it_has_no_room_for(self):
        many = [{"name": f"Row {n}", "locator": f"getByTestId('row-{n}')"} for n in range(25)]
        ingest.ingest(self.con, run("1.0", locators=many))
        line = next(x for x in cards.card(self.con, "/form").splitlines() if x.startswith("LOCATORS (fresh)"))
        self.assertIn("(+5 more verified: `find '<name>'` gives a ref)", line)
        self.assertNotIn("snapshot", line)

    def test_plan_does_not_mark_verified(self):
        ingest.ingest(self.con, {"run_id": "p", "build": "unknown", "pages": [{"route": "/new"}]})
        self.assertIn("verified=never", cards.card(self.con, "/new"))

    def test_unknown_build_page_keeps_its_card(self):
        ingest.ingest(self.con, {"run_id": "a", "build": "1.42.0", "pages": [{"route": "/incidents"}]})
        ingest.ingest(
            self.con,
            {
                "run_id": "b",
                "build": "unknown",
                "pages": [
                    {
                        "route": "/map",
                        "facts": [{"kind": "rule", "text": "layers switch"}],
                        "locators": [{"name": "Layer", "locator": "getByLabel('Layer')"}],
                    }
                ],
            },
        )
        c = cards.card(self.con, "/map")
        self.assertIn("LOCATORS (fresh): Layer=getByLabel('Layer')", c)
        self.assertIn("- [rule] layers switch\n", c + "\n")  # not marked stale
        # found live: "this page's build was never readable" on a page a plan had listed and no run had opened
        self.assertIn(
            "NOTE: no run verified this page on a build it could read; its knowledge may predate build 1.42.0.", c
        )


class Facts(MemBase):
    """What a run's facts, coverage and locators become."""

    def test_coverage_results_normalised(self):
        cov = [
            {"target": "t", "heuristic": h, "result": r}
            for h, r in [
                ("boundary", "BLOCKED"),
                ("duplicate", "blocked (needs --faults)"),
                ("empty-null", "NOT_RUN"),
                ("keyboard-a11y", "skipped"),
                ("happy-path", "PASS"),
                ("invalid-format", "Failed"),
            ]
        ]
        ingest.ingest(self.con, run(coverage=cov))
        untried = cards.card(self.con, "/form").split("UNTRIED: ")[1].splitlines()[0]
        for h in ("boundary", "duplicate", "empty-null", "keyboard-a11y"):
            self.assertIn(h, untried)
        for h in ("happy-path", "invalid-format"):
            self.assertNotIn(h, untried)
        self.assertEqual({r[0] for r in self.con.execute("SELECT result FROM coverage")}, {"pass", "fail", "blocked"})

    def test_heuristic_ids_normalised(self):
        ingest.ingest(
            self.con,
            run(coverage=[{"target": "x", "heuristic": "Boundary"}, {"target": "y", "heuristic": "Traffic time zone"}]),
        )
        c = cards.card(self.con, "/form")
        self.assertIn("other:traffic-time-zone", c)
        self.assertNotIn("boundary", c.split("UNTRIED: ")[1].splitlines()[0])

    def test_blocked_stays_untried_and_paraphrased_facts_collapse(self):
        ingest.ingest(
            self.con,
            run(
                coverage=[{"target": "api", "heuristic": "error-handling", "result": "blocked"}],
                facts=[{"kind": "rule", "text": "ID, Road and Type buttons sort ascending"}],
            ),
            "2026-11-22T10:00:00Z",
        )
        ingest.ingest(
            self.con,
            run(facts=[{"kind": "rule", "text": "ID, Road and Type header buttons sort ascending"}]),
            "2026-11-22T11:00:00Z",
        )
        c = cards.card(self.con, "/form")
        self.assertIn("error-handling", c.split("UNTRIED")[1])
        self.assertIn("header buttons", c)
        self.assertEqual(c.count("sort ascending"), 1)

    def test_distinct_rules_not_collapsed(self):
        facts = [
            "Name field is required",
            "Email field is required",
            "Start date must be before end date",
            "Start time must be before end time",
            "Speed limit must be between 0 and 130 km/h",
            "Speed limit must be between 0 and 120 km/h",
        ]
        ingest.ingest(self.con, run(facts=[{"kind": "rule", "text": t} for t in facts]))
        c = cards.card(self.con, "/form")
        for t in facts:
            self.assertIn(t, c)

    def both_shown(self, twins):
        said = [t for rule, swap in twins for t in (rule, rule.replace(*swap))]
        ingest.ingest(self.con, run(facts=[{"kind": "rule", "text": t} for t in said]))
        c = cards.card(self.con, "/form")
        for t in said:
            self.assertIn(f"- [rule] {t}\n", c)

    def test_long_rules_that_differ_in_one_word_are_two_rules(self):  # the card showed one of each pair
        self.both_shown(
            [
                ("The Name field is required before the user can submit the form", ("Name", "Email")),
                ("Admins can delete a zone that has no active closures", ("no active", "active")),
                ("Viewers can't save the form changes on this page", ("can't", "can")),
                ("The list is sorted by date when the page first loads", ("is sorted", "is not sorted")),
                ("Drafts are deleted after thirty days without any change", ("are deleted", "are never deleted")),
                ("Managers can approve a closure request from their own region", ("Managers", "Only managers")),
                ("Admin users can open the audit log of every other user", ("Admin users", "Non-admin users")),
            ]
        )

    def test_a_condition_added_to_a_long_rule_makes_another_rule(self):
        self.both_shown(
            [
                ("A manager can close an incident that has open tasks", ("open tasks", "open tasks without approval")),
                ("Every user can see the closures of every region on the map", ("the map", "the map except drafts")),
                (
                    "An editor can change the end date of a closure that has started",
                    ("started", "started unless locked"),
                ),
            ]
        )

    def test_an_observation_that_says_the_opposite_does_not_hide_the_tickets_rule(self):
        rule = "The Save button is disabled for users with the viewer role"
        facts = lambda: [x for x in cards.card(self.con, "/form").splitlines() if x.startswith("- [rule]")]
        tell = lambda text, hour, **f: ingest.ingest(
            self.con, run(facts=[{"kind": "rule", "text": text, **f}]), f"2026-11-22T{hour}:00:00Z"
        )
        tell(rule, 10, source="PROJ-1")
        opposite = rule.replace("disabled", "enabled")
        tell(opposite, 11)
        self.assertEqual(facts(), [f"- [rule] {opposite}", f"- [rule] {rule} (from PROJ-1)"])
        restated = rule.replace("is disabled", "is always disabled")
        tell(restated, 12)
        self.assertEqual(facts(), [f"- [rule] {restated}", f"- [rule] {opposite}"])  # a restatement still stands in

    def test_ticket_fact_becomes_observed(self):
        ingest.ingest(
            self.con, run("unknown", facts=[{"kind": "rule", "text": "End date after start", "source": "PROJ-1"}])
        )
        self.assertIn("(from PROJ-1)", cards.card(self.con, "/form"))
        ingest.ingest(
            self.con, run("1.0", facts=[{"kind": "rule", "text": "End date after start", "source": "observed"}])
        )
        self.assertNotIn("(from PROJ-1)", cards.card(self.con, "/form"))
        ingest.ingest(
            self.con, run("unknown", facts=[{"kind": "rule", "text": "x", "source": "PROJ-2"}])
        )  # unverified stays 'from'
        self.assertIn("(from PROJ-2)", cards.card(self.con, "/form"))

    def test_size_caps(self):
        core.MAX_FACTS = 5
        try:
            for n in range(8):
                ingest.ingest(
                    self.con,
                    run(facts=[{"kind": "note", "text": f"fact number {n} " + "w" * n}]),
                    f"2026-11-22T10:0{n}:00Z",
                )
            texts = [r[0] for r in self.con.execute("SELECT text FROM facts")]
            self.assertEqual(len(texts), 5)
            self.assertTrue(any("fact number 7" in t for t in texts))
            self.assertEqual(self.con.execute("SELECT count(*) FROM facts_fts").fetchone()[0], 5)
        finally:
            core.MAX_FACTS = 100

    def test_bad_shapes_rejected(self):
        for bad in (
            [],
            {"run_id": "x", "build": "1", "pages": {"route": "/a"}},
            {"run_id": "x", "build": "1", "bugs": ["oops"]},
            {"build": "1"},
            {"run_id": "   ", "build": "1"},
            {"run_id": "x", "pages": {}},
            {"run_id": "x", "bugs": ""},
            {"run_id": "x", "cases": False},
            {"run_id": "x", "env": []},
            {"run_id": "x", "questions": {}},
            {"run_id": "x", "not_tested": ""},
            {"run_id": "x", "bugs": [{"route": "http://[x", "title": "bad"}]},
            {"run_id": "x", "bugs": [{"route": "/a", "title": "   "}]},
            {"run_id": "x", "cases": [{"id": "TC-1", "status": "PASS", "annotated": "false"}]},
        ):
            with self.assertRaises(ValueError, msg=str(bad)):
                ingest.ingest(self.con, bad)

    def test_round1_memory_and_core_fixes(self):
        # 1. cap/norm/short preserve numeric 0; norm preserves non-ASCII
        self.assertEqual((core.cap(0), core.norm(0), core.short(0, 10)), ("0", "0", "0"))
        self.assertNotEqual(core.bug_hash("/a", "日本語エラー"), core.bug_hash("/a", "한국어오류"))
        self.assertEqual(core.norm("Erreur: échec"), "erreur échec")
        # 2. whitespace symptom falls back to title (distinct bugs don't collide)
        ingest.ingest(
            self.con,
            {
                "run_id": "b1",
                "build": "1.0",
                "bugs": [
                    {"route": "/a", "title": "Bug A", "symptom": "   "},
                    {"route": "/a", "title": "Bug B", "symptom": "   "},
                ],
            },
        )
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 2)
        # 3. unknown-build plan does not overwrite/un-stale verified locators or store blank locators/heuristics
        ingest.ingest(
            self.con,
            {
                "run_id": "r1",
                "build": "1.0",
                "pages": [{"route": "/loc", "locators": [{"name": "Save", "locator": "#old"}]}],
            },
            "2026-01-01T00:00:00Z",
        )
        ingest.ingest(self.con, {"run_id": "r2", "build": "2.0", "pages": [{"route": "/loc"}]}, "2026-01-02T00:00:00Z")
        ingest.ingest(
            self.con,
            {
                "run_id": "p1",
                "build": "unknown",
                "pages": [
                    {
                        "route": "/loc",
                        "locators": [{"name": "Save", "locator": "#guess"}, {"name": "  ", "locator": "  "}],
                        "coverage": [{"target": "t", "heuristic": "   ", "result": "pass"}],
                    }
                ],
            },
            "2026-01-03T00:00:00Z",
        )
        rows = [dict(r) for r in self.con.execute("SELECT name, locator, build, stale FROM locators WHERE page='/loc'")]
        self.assertEqual(rows, [{"name": "Save", "locator": "#old", "build": "1.0", "stale": 1}])
        self.assertEqual(self.con.execute("SELECT count(*) FROM coverage WHERE page='/loc'").fetchone()[0], 0)
        # 4. fact dedup keeps numbers with attached units distinct
        ingest.ingest(
            self.con,
            {
                "run_id": "u1",
                "build": "2.0",
                "pages": [
                    {
                        "route": "/units",
                        "facts": [
                            {"kind": "rule", "text": "Speed limit must be between 0 and 130km/h"},
                            {"kind": "rule", "text": "Speed limit must be between 0 and 120km/h"},
                        ],
                    }
                ],
            },
        )
        cu = cards.card(self.con, "/units")
        self.assertIn("130km/h", cu)
        self.assertIn("120km/h", cu)
        # 5. search multi-word negation excludes only the phrase, not individual words
        ingest.ingest(
            self.con,
            {
                "run_id": "s1",
                "build": "2.0",
                "pages": [
                    {
                        "route": "/s",
                        "facts": [
                            {"kind": "note", "text": "speed chart loads slowly"},
                            {"kind": "rule", "text": "speed-limit max 130"},
                            {"kind": "api", "text": "GET /api/incidents returns 500"},
                            {"kind": "api", "text": "GET /api/zones returns 500"},
                        ],
                    }
                ],
            },
        )
        self.assertIn("speed chart", memory.search(self.con, "speed -speed-limit"))
        self.assertNotIn("speed-limit", memory.search(self.con, "speed -speed-limit"))
        self.assertIn("/api/incidents", memory.search(self.con, "500 -/api/zones"))
        self.assertNotIn("/api/zones", memory.search(self.con, "500 -/api/zones"))
        # 6. MAX_RUNS pruning leaves no orphan sightings
        orig_max = core.MAX_RUNS
        core.MAX_RUNS = 2
        try:
            ingest.ingest(self.con, {"run_id": "m2", "build": "2.0", "bugs": []}, "2026-12-02T00:00:00Z")
            ingest.ingest(self.con, {"run_id": "m3", "build": "2.0", "bugs": []}, "2026-12-03T00:00:00Z")
            ingest.ingest(
                self.con,
                {"run_id": "m1", "build": "2.0", "bugs": [{"route": "/m", "title": "old"}]},
                "2026-01-01T00:00:00Z",
                src="f1",
            )
            orphans = self.con.execute(
                "SELECT count(*) FROM sightings WHERE run_id NOT IN (SELECT id FROM runs)"
            ).fetchone()[0]
            self.assertEqual(orphans, 0)
        finally:
            core.MAX_RUNS = orig_max
        # 7. QA_MEM_DB expands ~ and empty QA_SDET_DATA / QA_SDET_CONFIG do not resolve to '.'
        with mock.patch.dict(os.environ, QA_MEM_DB="~/custom.db"):
            self.assertFalse(str(core.db_path()).startswith("~"))


if __name__ == "__main__":
    unittest.main()
