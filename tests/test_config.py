"""Tests for Config: env parsing, validation, clamping and kwarg overrides."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from novel_agent.config import Config, ConfigError

_RUNTIME_PREFIXES = ("NOVEL_", "DEEPSEEK_")


def _clean_env():
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_RUNTIME_PREFIXES)
    }


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._patch = patch.dict(os.environ, _clean_env(), clear=True)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_defaults_are_sane(self):
        cfg = Config()
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertEqual(cfg.port, 8787)
        self.assertEqual(cfg.log_level, "INFO")
        self.assertEqual(cfg.log_format, "text")
        self.assertEqual(cfg.max_job_attempts, 3)
        self.assertEqual(cfg.max_retries, 2)
        self.assertEqual(cfg.data_dir, Path("./data"))
        self.assertFalse(cfg.worker_once)

    def test_host_and_port_parse_from_env(self):
        os.environ["NOVEL_HOST"] = "0.0.0.0"
        os.environ["NOVEL_PORT"] = "9000"
        cfg = Config()
        self.assertEqual(cfg.host, "0.0.0.0")
        self.assertEqual(cfg.port, 9000)

    def test_port_zero_is_ephemeral(self):
        os.environ["NOVEL_PORT"] = "0"
        self.assertEqual(Config().port, 0)

    def test_port_is_clamped_to_valid_range(self):
        os.environ["NOVEL_PORT"] = "70000"
        self.assertEqual(Config().port, 65535)
        os.environ["NOVEL_PORT"] = "-5"
        self.assertEqual(Config().port, 0)

    def test_kwarg_overrides_beat_environment(self):
        os.environ["NOVEL_PORT"] = "9000"
        os.environ["NOVEL_DATA_DIR"] = "/tmp/from_env"
        cfg = Config(port=7000, data_dir=Path("/tmp/override"))
        self.assertEqual(cfg.port, 7000)
        self.assertEqual(cfg.data_dir, Path("/tmp/override"))

    def test_malformed_int_raises_config_error(self):
        os.environ["NOVEL_PORT"] = "not-a-port"
        with self.assertRaises(ConfigError):
            Config()

    def test_malformed_bool_raises_config_error(self):
        os.environ["NOVEL_REQUIRE_REVIEW"] = "banana"
        with self.assertRaisesRegex(ConfigError, "NOVEL_REQUIRE_REVIEW"):
            Config()

    def test_bool_accepts_common_spellings(self):
        for raw, expected in (("1", True), ("yes", True), ("on", True),
                              ("0", False), ("no", False), ("off", False)):
            os.environ["NOVEL_PUBLISH_ENABLED"] = raw
            self.assertEqual(Config().publish_enabled, expected, raw)

    def test_invalid_log_format_raises(self):
        os.environ["NOVEL_LOG_FORMAT"] = "xml"
        with self.assertRaisesRegex(ConfigError, "NOVEL_LOG_FORMAT"):
            Config()

    def test_invalid_log_level_raises(self):
        os.environ["NOVEL_LOG_LEVEL"] = "CHATTY"
        with self.assertRaisesRegex(ConfigError, "NOVEL_LOG_LEVEL"):
            Config()

    def test_invalid_thinking_raises(self):
        os.environ["DEEPSEEK_THINKING"] = "sometimes"
        with self.assertRaisesRegex(ConfigError, "DEEPSEEK_THINKING"):
            Config()

    def test_repr_hides_no_auth_state(self):
        os.environ["NOVEL_AUTH_TOKEN"] = "secret-token"
        text = repr(Config())
        self.assertIn("auth=on", text)
        self.assertNotIn("secret-token", text)


if __name__ == "__main__":
    unittest.main()
