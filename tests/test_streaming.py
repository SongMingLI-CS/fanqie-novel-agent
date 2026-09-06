"""Tests for the async/streaming service path and the SSE endpoint."""
import asyncio
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from novel_agent import server as srv
from novel_agent.checkpoints import CheckpointRepository
from novel_agent.config import Config
from novel_agent.deepseek import DeepSeekError
from novel_agent.events import EventRepository
from novel_agent.service import NovelService
from novel_agent.store import Store

ROOT = Path(__file__).parents[1]


def outline_json(n=1, title="大纲章"):
    return json.dumps({
        "chapterNumber": n, "title": title, "chapterGoal": "找线索",
        "beats": [{"goal": "查"}], "charactersUsed": [],
        "eventsIntroduced": [], "foreshadowingAdded": [],
        "foreshadowingResolved": [], "stateChanges": [],
        "nextChapterHook": "关门", "warnings": [],
    }, ensure_ascii=False)


def chapter_json(n=1, content="第一段。\n\n第二段。"):
    return json.dumps({
        "chapterNumber": n, "title": "开端", "chapterGoal": "找线索",
        "summary": "摘要", "beats": [{"goal": "查"}], "content": content,
        "charactersUsed": [], "eventsIntroduced": [], "foreshadowingAdded": [],
        "foreshadowingResolved": [], "stateChanges": [],
        "nextChapterHook": "关门", "warnings": [],
    }, ensure_ascii=False)


class FakeSync:
    def complete(self, system, user):
        raise AssertionError("sync client must not be used on the stream path")


class StreamClient:
    """Async stream client that replays pre-recorded responses char-wise."""

    def __init__(self, entries):
        self.entries = list(entries)
        self.calls = []

    async def stream(self, system, user):
        self.calls.append(user)
        entry = self.entries.pop(0)
        if isinstance(entry, BaseException):
            raise entry
        for ch in str(entry):
            yield {"type": "delta", "text": ch}
        yield {
            "type": "usage",
            "usage": {"model": "test", "prompt_version": "x", "input_tokens": 1,
                      "output_tokens": 2, "duration_ms": 1, "request_status": "succeeded"},
        }


class AsyncPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.novel = self.store.create_novel("流式测试", {"styleRules": {"chapterLength": 0}})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _service(self, stream_client):
        return NovelService(
            self.store, FakeSync(), Config(data_dir=Path(self.tmp.name)), ROOT,
            async_client=stream_client, events=EventRepository(self.store),
        )

    def test_compose_stream_persists_and_emits_delta_events(self):
        job, _ = self.store.create_job(self.novel["id"], 1)
        client = StreamClient([chapter_json()])
        svc = self._service(client)
        self.assertTrue(asyncio.run(svc.process_stream(self.store.get_job(job["id"]))))
        chapter = self.store.chapter(self.novel["id"], 1)
        self.assertEqual(chapter["status"], "WAITING_APPROVAL")
        repo = EventRepository(self.store)
        events = repo.read_since(self.novel["id"], 0)
        types = [e["type"] for e in events]
        self.assertIn("chapter.status", types)
        self.assertIn("chapter.ready", types)
        deltas = [e["payload"]["text"] for e in events if e["type"] == "llm.delta"]
        self.assertEqual("".join(deltas), chapter_json())

    def test_pipeline_async_publishes_stage_and_checkpoint_events(self):
        job, _ = self.store.create_job(self.novel["id"], 1)
        self.store.set_job_config(job["id"], {"stages": ["outline", "chapter"]})
        client = StreamClient([outline_json(), chapter_json()])
        svc = self._service(client)
        self.assertTrue(asyncio.run(svc.process_stream(self.store.get_job(job["id"]))))
        run = CheckpointRepository(self.store).find_run(job["id"])
        self.assertEqual(run["status"], "SUCCEEDED")
        events = EventRepository(self.store).read_since(self.novel["id"], 0)
        by_type = {}
        for e in events:
            by_type.setdefault(e["type"], []).append(e["payload"])
        stages = [p["stage"] for p in by_type.get("agent.stage", []) if p.get("state") == "done"]
        self.assertEqual(stages, ["outline", "chapter"])
        self.assertEqual(len(by_type.get("checkpoint.saved", [])), 2)
        self.assertTrue(by_type.get("chapter.ready"))

    def test_stream_network_error_schedules_job_retry(self):
        job, _ = self.store.create_job(self.novel["id"], 1)
        client = StreamClient([DeepSeekError("tcp down", "tcp_connection_error")])
        svc = self._service(client)
        self.assertFalse(asyncio.run(svc.process_stream(self.store.get_job(job["id"]))))
        row = self.store.get_job(job["id"])
        self.assertEqual(row["status"], "PENDING")  # scheduled retry, not dead-lettered
        self.assertIn("tcp", row["error"])
        failed = [u for u in self.store.usage(self.novel["id"]) if u["request_status"] == "failed"]
        self.assertTrue(failed)
class SseEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.novel = self.store.create_novel("直播小说", {})
        self._prev = (srv.config, srv.store, srv.service, srv.STATIC_DIR)
        srv.config = Config(data_dir=Path(self.tmp.name))
        srv.store = self.store
        srv.service = None
        srv.STATIC_DIR = srv.ROOT / "static"
        srv._STOP_EVENT.clear()
        self.httpd = srv.NovelHTTPServer(("127.0.0.1", 0), srv.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        srv._STOP_EVENT.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        srv.config, srv.store, srv.service, srv.STATIC_DIR = self._prev
        self.store.close()
        self.tmp.cleanup()

    def test_events_endpoint_streams_frames_and_advances_cursor(self):
        repo = EventRepository(self.store)
        url = self.base + "/api/events?novel_id=" + self.novel["id"] + "&since=0"
        resp = urllib.request.urlopen(url, timeout=5)
        try:
            repo.publish(self.novel["id"], None, "llm.delta", {"text": "甲", "chapter": 1})
            line = b""
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                line = resp.readline()
                if line.startswith(b"data:"):
                    break
            self.assertTrue(line.startswith(b"data:"), "no SSE frame arrived: %r" % line)
            event = json.loads(line[5:].strip())
            self.assertEqual(event["type"], "llm.delta")
            self.assertEqual(event["payload"]["text"], "甲")
            # Cursor semantics: subscribing from this event's id returns only newer events.
            since_url = self.base + "/api/events?novel_id=" + self.novel["id"] + "&since=%d" % event["id"]
            resp2 = urllib.request.urlopen(since_url, timeout=3)
            try:
                repo.publish(self.novel["id"], None, "agent.stage", {"stage": "outline"})
                line2 = b""
                dl = time.monotonic() + 3
                while time.monotonic() < dl:
                    line2 = resp2.readline()
                    if line2.startswith(b"data:"):
                        break
                ev2 = json.loads(line2[5:].strip())
                self.assertEqual(ev2["type"], "agent.stage")
                self.assertGreater(ev2["id"], event["id"])
            finally:
                resp2.close()
        finally:
            resp.close()


if __name__ == "__main__":
    unittest.main()
