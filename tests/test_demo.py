"""Tests for the keyless replay/demo layer (novel_agent.replay + novel_agent.demo)."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from novel_agent.demo import run_replay
from novel_agent.replay import (
    CapturingAsyncClient,
    Recording,
    ReplayClient,
    ReplayError,
    build_recording,
    default_recording,
)

ROOT = Path(__file__).parents[1]


class FakeInner:
    """A minimal async inner client used to exercise capture offline."""

    def __init__(self, text):
        self.text = text
        self.closed = False

    async def aclose(self):
        self.closed = True

    async def stream(self, system, user):
        for ch in self.text:
            yield {"type": "delta", "text": ch}
        yield {
            "type": "usage",
            "usage": {"model": "m", "input_tokens": 1, "output_tokens": 1,
                      "duration_ms": 1, "request_status": "succeeded"},
        }


class RecordingTests(unittest.TestCase):
    def test_default_recording_has_three_valid_calls(self):
        rec = default_recording()
        self.assertEqual(rec.stages, ["outline", "chapter", "polish"])
        self.assertEqual(rec.chapter_number, 1)
        self.assertEqual(len(rec.calls), 3)
        for call in rec.calls:
            self.assertTrue(json.loads(call["text"]))  # valid JSON texts
            self.assertEqual(call["usage"]["request_status"], "succeeded")
        self.assertIn("storyBible", rec.novel)

    def test_save_load_roundtrip_and_format_guard(self):
        rec = build_recording(
            {"title": "T", "volume": "", "genre": "g", "story_bible": {"x": 1}},
            ["outline"], 1,
            [{"system": "s", "user": "u", "text": '{"chapterNumber":1}',
              "usage": {"request_status": "succeeded"}}],
            model="m",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = rec.save(Path(tmp) / "r.json")
            loaded = Recording.load(path)
            self.assertEqual(len(loaded.calls), 1)
            self.assertEqual(loaded.novel["title"], "T")
            self.assertTrue(loaded.strict)
            bogus = Path(tmp) / "bogus.json"
            bogus.write_text(json.dumps({"format": "nope"}), encoding="utf-8")
            with self.assertRaises(ReplayError):
                Recording.load(bogus)


class ReplayClientTests(unittest.TestCase):
    def test_stream_and_complete_replay_recorded_text(self):
        rec = default_recording()
        client = ReplayClient(rec, loose=True)
        text, usage = client.complete("s", "u")
        self.assertEqual(text, rec.calls[0]["text"])
        self.assertEqual(usage["request_status"], "succeeded")

        async def collect():
            chunks = []
            async for chunk in ReplayClient(rec, loose=True).stream("s", "u"):
                if chunk["type"] == "delta":
                    chunks.append(chunk["text"])
            return "".join(chunks)

        self.assertEqual(asyncio.run(collect()), rec.calls[0]["text"])

    def test_strict_mode_rejects_mismatched_prompt(self):
        rec = build_recording(
            {"title": "T"}, ["chapter"], 1,
            [{"system": "s", "user": "expected", "text": "{}",
              "usage": {"request_status": "succeeded"}}],
        )
        client = ReplayClient(rec)  # strict by default
        with self.assertRaises(ReplayError):
            client.complete("s", "different")

    def test_exhaustion_raises_clear_error(self):
        rec = default_recording()
        client = ReplayClient(rec, loose=True)
        client.complete("s", "u")
        client.complete("s", "u")
        client.complete("s", "u")
        with self.assertRaises(ReplayError):
            client.complete("s", "u")


class CaptureTests(unittest.TestCase):
    def test_capturing_client_records_full_text_once(self):
        text = "第一段。\n\n第二段。"
        captured = []
        inner = FakeInner(text)
        client = CapturingAsyncClient(inner, captured.append)

        async def drain():
            async for _ in client.stream("s", "u"):
                pass

        asyncio.run(drain())
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["text"], text)
        self.assertEqual(captured[0]["user"], "u")


class DemoRunTests(unittest.TestCase):
    def test_headless_replay_drives_pipeline_and_emits_events(self):
        rec = default_recording()
        summary = run_replay(rec, port=None)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["chapterStatus"], "WAITING_APPROVAL")
        types = [e["type"] for e in summary["events"]]
        for expected in ("agent.stage", "agent.run", "llm.delta", "llm.text", "chapter.ready"):
            self.assertIn(expected, types)
        # The revealed text is the validated polish content (final stage).
        polish_content = json.loads(rec.calls[-1]["text"])["content"]
        self.assertEqual(summary["text"], polish_content)
        self.assertGreater(len(polish_content), 100)

    def test_server_mode_reports_live_base_url(self):
        rec = default_recording()
        summary = run_replay(rec, port=0)
        self.assertTrue(summary["ok"])
        self.assertIsNotNone(summary["base"])
        self.assertTrue(summary["base"].startswith("http://127.0.0.1:"))


if __name__ == "__main__":
    unittest.main()
