"""Tests for the centralised PromptManager."""
import unittest
from pathlib import Path

from novel_agent.prompts import PromptManager, PromptError

ROOT = Path(__file__).parents[1]


class PromptManagerTests(unittest.TestCase):
    def setUp(self):
        self.prompts = PromptManager(ROOT)

    def test_system_and_request_render_without_variables(self):
        self.assertIn("novel", self.prompts.system().lower())
        self.assertTrue(self.prompts.render("request", {}).strip())

    def test_version_is_stamped(self):
        self.assertEqual(self.prompts.version, "novel-writer@2")

    def test_outline_required_variables(self):
        required = self.prompts.required_variables("outline")
        for key in ("chapterNumber", "storyBible", "recentChapterSummaries"):
            self.assertIn(key, required)

    def test_render_interpolates_placeholders(self):
        out = self.prompts.render(
            "outline",
            {"chapterNumber": 3, "storyBible": "{}", "recentChapterSummaries": "[]"},
        )
        self.assertIn("第 3 章", out)

    def test_missing_variable_raises(self):
        with self.assertRaises(PromptError):
            self.prompts.render("outline", {"chapterNumber": 1})

    def test_missing_file_raises(self):
        with self.assertRaises(PromptError):
            self.prompts.render("no-such-prompt", {})


if __name__ == "__main__":
    unittest.main()
