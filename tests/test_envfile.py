"""Tests for the pure-stdlib .env loader used at process startup."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from novel_agent.envfile import _parse_line, default_env_path, load_env

# Variables the runtime itself reads; blanking them yields deterministic defaults.
_RUNTIME_PREFIXES = ("NOVEL_", "DEEPSEEK_")


def _clean_env():
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_RUNTIME_PREFIXES)
    }


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._patch = patch.dict(os.environ, _clean_env(), clear=True)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _write(self, name, content):
        path = Path(self.tmp.name) / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_missing_file_returns_false(self):
        self.assertFalse(load_env(Path(self.tmp.name) / "nope.env"))

    def test_parse_line_skips_comments_and_blank_lines(self):
        for line in ("", "   ", "# comment", "; comment"):
            self.assertIsNone(_parse_line(line))
        self.assertIsNone(_parse_line("no_equals_here"))

    def test_loads_quotes_export_and_inline_comments(self):
        path = self._write(
            "a.env",
            "# comment\n"
            "; also comment\n"
            "\n"
            "export SIMPLE=value\n"
            "DOUBLE=\"hello world\"\n"
            "SINGLE='plain'\n"
            "TRAILING=hello # trailing note\n"
            "HASH_INSIDE=\"keep # this\"\n",
        )
        self.assertTrue(load_env(path))
        self.assertEqual(os.environ["SIMPLE"], "value")
        self.assertEqual(os.environ["DOUBLE"], "hello world")
        self.assertEqual(os.environ["SINGLE"], "plain")
        self.assertEqual(os.environ["TRAILING"], "hello")
        self.assertEqual(os.environ["HASH_INSIDE"], "keep # this")

    def test_never_overrides_existing_env_by_default(self):
        path = self._write("b.env", "ALREADY=from_file\nONLY_FILE=1\n")
        os.environ["ALREADY"] = "from_process"
        load_env(path)
        self.assertEqual(os.environ["ALREADY"], "from_process")
        self.assertEqual(os.environ["ONLY_FILE"], "1")

    def test_override_true_replaces_existing_env(self):
        path = self._write("c.env", "ALREADY=from_file\n")
        os.environ["ALREADY"] = "from_process"
        load_env(path, override=True)
        self.assertEqual(os.environ["ALREADY"], "from_file")

    def test_default_env_path_honours_novel_env_file(self):
        target = Path(self.tmp.name) / "custom.env"
        os.environ["NOVEL_ENV_FILE"] = str(target)
        self.assertEqual(default_env_path(), target)

    def test_export_prefix_and_key_spacing(self):
        self.assertEqual(_parse_line("export KEY =value"), ("KEY", "value"))
        self.assertEqual(_parse_line("  export SPACED =  padded  "), ("SPACED", "padded"))
        # A lone token without '=' is skipped, never an error.
        self.assertIsNone(_parse_line("export"))
        self.assertIsNone(_parse_line("no_equals_here"))

    def test_loading_a_directory_is_not_a_file(self):
        self.assertFalse(load_env(Path(self.tmp.name)))

    def test_novel_env_file_auto_detected_when_path_omitted(self):
        # Pointing NOVEL_ENV_FILE at a real file makes load_env() pick it up.
        path = self._write("auto.env", "AUTO_LOADED=yes\n")
        os.environ["NOVEL_ENV_FILE"] = str(path)
        self.assertTrue(load_env())
        self.assertEqual(os.environ["AUTO_LOADED"], "yes")

    def test_novel_env_file_to_nowhere_disables_loading(self):
        # The hermetic pattern used by the process tests: NOVEL_ENV_FILE points
        # at a non-existent file so a stray repository .env is never read.
        os.environ["NOVEL_ENV_FILE"] = str(Path(self.tmp.name) / "missing.env")
        self.assertFalse(load_env())
        self.assertNotIn("AUTO_LOADED", os.environ)


if __name__ == "__main__":
    unittest.main()
