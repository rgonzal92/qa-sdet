"""The instruction files and the code must agree; drift here makes agents fail quietly."""
import re
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
qm = SourceFileLoader("qa_mem_docs", str(ROOT / "bin/qa-mem")).load_module()
DOCS = sorted([*ROOT.glob("skills/*/SKILL.md"), *ROOT.glob("agents/*.md"), *ROOT.glob("references/*.md")])


class Docs(unittest.TestCase):
    def test_heuristic_ids_match_code(self):
        table = (ROOT / "references/heuristics.md").read_text()
        ids = re.findall(r"^\| ([a-z][a-z0-9-]+) \|", table, re.M)
        self.assertEqual(ids, qm.HEURISTICS)

    def test_common_steps_referenced_exist(self):
        defined = set(re.findall(r"^## (C\d+)\.", (ROOT / "references/common.md").read_text(), re.M))
        for f in DOCS:
            for step in re.findall(r"\bC(\d+)\b", f.read_text()):
                self.assertIn(f"C{step}", defined, f"{f.name} refers to C{step}")

    def test_referenced_plugin_files_exist(self):
        for f in DOCS:
            for rel in re.findall(r"\$\{CLAUDE_SKILL_DIR\}/\.\./\.\./([\w./-]+)", f.read_text()):
                self.assertTrue((ROOT / rel).exists(), f"{f.name}: {rel}")
            for name in re.findall(r"`((?:[\w-]+)\.md)`", f.read_text()):
                if name not in ("plan.md", "cases.md"):  # outputs, not plugin files
                    self.assertTrue((ROOT / "references" / name).exists() or (ROOT / name).exists(), f"{f.name}: {name}")

    def test_no_hardcoded_browser(self):
        """The browser is the user's choice (config `browser`, asked in C1), never assumed."""
        for f in DOCS + [ROOT / "README.md"]:
            text = f.read_text()
            self.assertNotRegex(text, r"--browser\s*[= ]\s*(chrome|chromium|msedge|firefox|webkit)\b", f.name)
            self.assertNotRegex(text, r"(?i)\b(firefox|chrome|chromium|webkit|(ms)?edge) headless\b", f.name)  # bug template text
            self.assertNotRegex(text, r'"browser":\s*"(chrome|msedge|firefox|webkit)"', f.name)  # a preset config value

    def test_statuses_in_docs_are_real(self):
        text = (ROOT / "skills/qa-memory/SKILL.md").read_text()
        for status in re.findall(r"\b(OPEN|FILED|FIXED|INVALID|REGRESSED)\b", text):
            self.assertIn(status, qm.STATUSES)


if __name__ == "__main__":
    unittest.main()
