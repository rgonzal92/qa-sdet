"""The app shell's menu config (qa_sdet.scan, codemap): route rows written by code, not by a mapper."""

import json
import unittest

from support import CodeMapCase, git, git_repo

from qa_sdet import codemap


class ShellMenu(CodeMapCase):
    """The app shell's menu config gives route rows written by code, not by a mapper."""

    SHELL_NAV = {
        "nav": [
            {"key": "home", "title": "Home", "module": {"id": "m0", "url": "/m/home"}},
            {
                "key": "reports",
                "title": "Reports",
                "children": [{"key": "daily", "title": "Daily", "module": {"id": "m1", "url": "/m/daily"}}],
            },
        ]
    }

    def shell_repo(self):
        return git_repo(
            self.dir / "ui",
            {
                "src/routes.js": "const r = [{ path: '/m/daily', element: <D/> }]\n",
                "public/config.json": json.dumps(self.SHELL_NAV, indent=1),
            },
        )

    def routes(self):
        return [tuple(r) for r in self.con.execute("SELECT page, value FROM codemap WHERE kind='route' ORDER BY page")]

    def test_shell_menu_routes_survive_a_full_frontend_map(self):
        ui = self.shell_repo()
        s = self.scan(ui)
        frontend = json.loads((self.out / "scan-frontend.json").read_text())["repos"][0]
        self.assertEqual(frontend["files"], ["src/routes.js"])  # the menu is not a mapper's file
        self.write_map(
            "frontend",
            [{"page": "/m/daily", "kind": "route", "label": "Daily", "value": "D", "file": "src/routes.js:1"}],
        )
        self.ingest_all()
        self.assertEqual(
            self.routes(),
            [("/home", "module m0 at /m/home"), ("/m/daily", "D"), ("/reports/daily", "module m1 at /m/daily")],
        )
        self.assertFalse(s["incremental"])

    def test_unchanged_shell_menu_keeps_the_next_scan_incremental(self):
        ui = self.shell_repo()
        self.scan(ui)
        self.write_map(
            "frontend",
            [{"page": "/m/daily", "kind": "route", "label": "Daily", "value": "D", "file": "src/routes.js:1"}],
        )
        self.ingest_all()
        s = self.scan(ui, stamp="s2")
        self.assertTrue(s["incremental"])
        self.assertEqual(s["areas"]["frontend"]["files"], 0)  # a stored "" hash for the menu forced a full re-map
        self.assertEqual(len(self.routes()), 3)

    def test_a_second_menu_list_maps_its_modules_too(self):  # a profile menu beside the main one, same item shape
        menu = dict(
            self.SHELL_NAV,
            accountMenu=[
                {"key": "preferences", "title": "Preferences", "module": {"id": "m2", "url": "/m/prefs"}},
                {"key": "help", "title": "Help", "url": "https://help.example.com"},  # an external link: no module
                {
                    "key": "me",
                    "children": [{"key": "display", "title": "Display", "module": {"id": "m3", "url": "/m/d"}}],
                },
                {"key": "home", "title": "Home again", "module": {"id": "m9", "url": "/m/other"}},  # nav has /home
            ],
            version=3,  # other top-level values are no menus
            features=["charts", {"key": "beta"}],
        )
        ui = git_repo(
            self.dir / "ui", {"src/routes.js": "const r = []\n", "public/config.json": json.dumps(menu, indent=1)}
        )
        self.scan(ui)
        self.assertEqual(
            self.routes(),
            [
                ("/home", "module m0 at /m/home"),
                ("/me/display", "module m3 at /m/d"),
                ("/preferences", "module m2 at /m/prefs"),
                ("/reports/daily", "module m1 at /m/daily"),
            ],
        )

    def test_menu_without_modules_drops_its_old_routes(self):  # rows of a module-less menu stayed forever
        ui = self.shell_repo()
        self.scan(ui)
        self.assertEqual(len(self.routes()), 2)
        (ui / "public/config.json").write_text(json.dumps({"nav": [{"key": "home", "module": None}]}))
        git(ui, "commit", "-qam", "no modules")
        self.scan(ui, stamp="s2")
        self.assertEqual(self.routes(), [])

    def test_deeply_nested_shell_menu_never_breaks_the_scan(self):  # RecursionError escaped "never raises"
        leaf = json.dumps({"key": "a", "title": "A", "module": {"id": "m", "url": "/m/a"}})
        deep = '{"key": "a", "children": [' * 3000 + leaf + "]}" * 3000  # as text: json.dumps recurses too on 3.9
        ui = git_repo(
            self.dir / "ui",
            {
                "src/routes.js": "const r = [{ path: '/m/a', element: <A/> }]\n",
                "public/config.json": '{"nav": [' + deep + "]}",
            },
        )
        s = self.scan(ui)
        self.assertEqual(s["areas"]["frontend"]["files"], 1)
        self.assertEqual(self.routes(), [])

    def test_renamed_menu_path_remaps_the_tests_in_full(self):  # a changed multi-route file moves routes
        ui = git_repo(
            self.dir / "ui",
            {
                "public/config.json": json.dumps(self.SHELL_NAV),
                "e2e/home.spec.ts": "import { test } from '@playwright/test';\n"
                "test('home', async ({ page }) => { await page.getByRole('button').click(); });\n",
            },
        )
        self.scan(ui)
        self.write_map(
            "tests",
            [
                {
                    "page": "/home",
                    "kind": "locator",
                    "label": "Open",
                    "value": "getByRole('button')",
                    "file": "e2e/home.spec.ts:2",
                }
            ],
        )
        self.ingest_all()
        self.assertEqual(
            self.con.execute("SELECT count(*) FROM codemap WHERE page='/home' AND area='tests'").fetchone()[0], 1
        )
        nav = json.loads(json.dumps(self.SHELL_NAV))
        nav["nav"][0]["key"] = "start"
        (ui / "public/config.json").write_text(json.dumps(nav))
        git(ui, "commit", "-qam", "rename")
        self.scan(ui, stamp="s2")  # the menu holds several routes and changed: the tests are mapped in full
        t = json.loads((self.out / "scan-tests.json").read_text())["repos"][0]
        self.assertEqual((t["full"], t["files"]), (True, ["e2e/home.spec.ts"]))
        self.write_map(
            "tests",
            [
                {
                    "page": "/start",
                    "kind": "locator",
                    "label": "Open",
                    "value": "getByRole('button')",
                    "file": "e2e/home.spec.ts:2",
                }
            ],
        )
        self.ingest_all()
        self.assertEqual(self.con.execute("SELECT count(*) FROM codemap WHERE page='/home'").fetchone()[0], 0)
        self.assertEqual(
            self.con.execute("SELECT count(*) FROM codemap WHERE page='/start' AND area='tests'").fetchone()[0], 1
        )

    def test_a_menu_route_never_pushes_locators_off_a_full_page(self):
        ui = git_repo(self.dir / "ui", {"src/Main.jsx": "const Main = () => <form/>;\n"})
        self.scan(ui)
        self.write_map(
            "frontend",
            [
                {"page": "/home", "kind": "locator", "label": f"Field {i}", "value": "x", "file": "src/Main.jsx:1"}
                for i in range(codemap.CODEMAP_PAGE_CAP)
            ],
        )
        self.ingest_all()
        (ui / "public").mkdir()
        (ui / "public/config.json").write_text(
            json.dumps({"nav": [{"key": "home", "title": "Home", "module": {"url": "/m"}}]})
        )
        git(ui, "add", "-A")
        git(ui, "commit", "-qm", "menu")
        self.scan(ui, stamp="s2")
        self.assertEqual(
            dict(self.con.execute("SELECT kind, count(*) FROM codemap WHERE page='/home' GROUP BY kind").fetchall()),
            {"locator": codemap.CODEMAP_PAGE_CAP, "route": 1},
        )

    def test_a_renamed_menu_title_moves_the_page_name(self):  # the first title stayed the page's name
        nav = lambda a, b: json.dumps(
            {"nav": [{"key": k, "title": n, "module": {"url": "/m/" + k}} for k, n in (("a", a), ("b", b))]}
        )
        ui = git_repo(self.dir / "ui", {"public/config.json": nav("Alpha", "Beta")})
        self.scan(ui)
        (ui / "public/config.json").write_text(nav("Beta", "Alpha"))
        git(ui, "commit", "-qam", "swap")
        self.scan(ui, stamp="s2")
        self.assertEqual(codemap.find_page(self.con, "Alpha")["route"], "/b")

    def test_menu_urls_are_redacted_like_a_mappers_rows(self):
        token = "ghp_" + "a" * 36
        ui = git_repo(
            self.dir / "ui",
            {
                "public/config.json": json.dumps(
                    {"nav": [{"key": "home", "title": "Home", "module": {"url": f"/m/home?sig={token}"}}]}
                )
            },
        )
        self.scan(ui)
        self.assertNotIn(token, json.dumps(self.routes()))
        self.assertEqual(
            sorted(p.name for p in self.out.glob("*-0.json*")), []
        )  # the menu is applied in memory: no files

    def test_a_broken_menu_drops_its_routes_and_settles(self):  # stale routes, a full re-map every scan
        ui = self.shell_repo()
        self.scan(ui)
        self.write_map(
            "frontend",
            [{"page": "/m/daily", "kind": "route", "label": "Daily", "value": "D", "file": "src/routes.js:1"}],
        )
        self.ingest_all()
        (ui / "public/config.json").write_text('{"nav": [{"key": "a", "module": {"url": "/x"} BROKEN')
        git(ui, "commit", "-qam", "broken")
        self.scan(ui, stamp="s2")  # the menu changed: one full pass, as for any changed multi-route menu
        self.assertEqual(self.routes(), [("/m/daily", "D")])  # the menu's two routes are gone, the router's stays
        self.write_map(
            "frontend",
            [{"page": "/m/daily", "kind": "route", "label": "Daily", "value": "D", "file": "src/routes.js:1"}],
        )
        self.ingest_all()
        s = self.scan(ui, stamp="s3")  # and not on every scan after it
        self.assertTrue(s["incremental"])
        self.assertEqual(s["areas"]["frontend"]["files"], 0)


if __name__ == "__main__":
    unittest.main()
