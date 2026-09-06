"""Tests for setup_logging(): text vs JSON output and validation."""
import io
import json
import logging
import unittest

from novel_agent.logutil import setup_logging


class LoggingTests(unittest.TestCase):
    def tearDown(self):
        for handler in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(handler)
            handler.close()

    def test_json_formatter_emits_one_object_and_merges_fields(self):
        buf = io.StringIO()
        handler = setup_logging("INFO", "json", stream=buf)
        try:
            logging.getLogger("novel_agent.test").info(
                "job %s done", "j-123", extra={"fields": {"job_id": "j-123", "chapter": 5}}
            )
        finally:
            handler.close()
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(payload["level"], "INFO")
        self.assertEqual(payload["message"], "job j-123 done")
        self.assertEqual(payload["job_id"], "j-123")
        self.assertEqual(payload["chapter"], 5)
        self.assertIn("ts", payload)
        self.assertIn("logger", payload)

    def test_text_formatter_has_no_json_and_timestamp(self):
        buf = io.StringIO()
        handler = setup_logging("INFO", "text", stream=buf)
        try:
            logging.getLogger("novel_agent.test").info("plain line")
        finally:
            handler.close()
        line = buf.getvalue().strip()
        self.assertNotIn('"level"', line)
        self.assertIn("INFO", line)
        self.assertIn("plain line", line)

    def test_invalid_level_and_format_raise(self):
        with self.assertRaises(ValueError):
            setup_logging("LOUD", "text")
        with self.assertRaises(ValueError):
            setup_logging("INFO", "xml")

    def test_http_server_logger_is_quieted(self):
        setup_logging("INFO", "text", stream=io.StringIO())
        self.assertEqual(logging.getLogger("http.server").level, logging.WARNING)


if __name__ == "__main__":
    unittest.main()
