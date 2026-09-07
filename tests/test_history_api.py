"""Tests for draft-version history/rollback and per-chapter event replay APIs."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from novel_agent import server as srv
from novel_agent.config import Config
from novel_agent.events import EventRepository
from novel_agent.service import NovelService
from novel_agent.store import Store

ROOT = Path(__file__).parents[1]


def chapter_raw(content="第一版正文。\n\n第二段。", title="开端"):
    return json.dumps({
        "chapterNumber": 1, "title": title, "chapterGoal": "目标",
        "summary": "摘要", "beats": [{"goal": "g"}], "content": content,
        "charactersUsed": [], "eventsIntroduced": [], "foreshadowingAdded": [],
        "foreshadowingResolved": [], "stateChanges": [],
        "nextChapterHook": "钩子", "warnings": [],
    }, ensure_ascii=False)


class FakeClient:
    def __init__(self, raw):
        self.raw = raw

    def complete(self, *args):
        return self.raw, {"model": "t", "prompt_version": "novel-writer@1",
                          "input_tokens": 1, "output_tokens": 1, "duration_ms": 1,
                          "request_status": "succeeded"}


def make_store(tmp):
    return Store(Path(tmp) / "db.sqlite3")


class StoreHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self.tmp.name)
        self.novel = self.store.create_novel("历史测试", {"styleRules": {"chapterLength": 0}})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _generate(self, content="第一版正文。\n\n第二段。", title="开端"):
        job, _ = self.store.create_job(self.novel["id"], 1)
        svc = NovelService(self.store, FakeClient(chapter_raw(content, title)),
                           Config(data_dir=Path(self.tmp.name)), ROOT)
        svc.process(job)
        return self.store.chapter(self.novel["id"], 1)

    def test_draft_history_tracks_generation_and_edits(self):
        ch = self._generate()
        self.store.update_draft(ch["id"], {"content": "第二版正文。"})
        self.store.update_draft(ch["id"], {"title": "改标题"})
        versions = self.store.draft_history(ch["id"])
        self.assertEqual([v["version"] for v in versions], [1, 2, 3])
        self.assertEqual(versions[0]["content"], "第一版正文。\n\n第二段。")
        self.assertEqual(versions[0]["passed"], True)   # generation was reviewed
        self.assertIsNone(versions[1]["passed"])        # manual edit -> no review yet
        payload = self.store.draft_version(ch["id"], 1)
        self.assertEqual(payload["content"], "第一版正文。\n\n第二段。")
        self.assertIsNone(self.store.draft_version(ch["id"], 99))

    def test_rollback_restores_older_payload_as_new_version(self):
        ch = self._generate(content="A 版", title="原标题")
        self.store.update_draft(ch["id"], {"content": "B 版", "title": "改后标题"})
        payload = self.store.draft_version(ch["id"], 1)
        updated = self.store.update_draft(ch["id"], {
            "title": payload["title"],
            "content": payload["content"],
            "summary": payload.get("summary", ""),
            "goal": payload.get("goal") or payload.get("chapterGoal", ""),
            "hook": payload.get("hook") or payload.get("nextChapterHook", ""),
        })
        self.assertEqual(updated["content"], "A 版")
        self.assertEqual(updated["title"], "原标题")
        self.assertEqual(updated["status"], "REVIEWING")
        self.assertEqual(updated["review"], {})
        self.assertEqual(len(self.store.draft_history(ch["id"])), 3)

class HttpHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self.tmp.name)
        self.novel = self.store.create_novel("HTTP 历史", {})
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

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path, data):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_history_and_rollback_endpoints(self):
        job, _ = self.store.create_job(self.novel["id"], 1)
        NovelService(self.store, FakeClient(chapter_raw("V1 正文")),
                     Config(data_dir=Path(self.tmp.name)), ROOT).process(job)
        ch = self.store.chapter(self.novel["id"], 1)
        self.store.update_draft(ch["id"], {"content": "V2 正文"})
        versions = self._get(f"/api/chapters/{ch['id']}/history")["versions"]
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0]["content"], "V1 正文")
        updated = self._post(f"/api/chapters/{ch['id']}/rollback", {"version": 1})
        self.assertEqual(updated["content"], "V1 正文")
        self.assertEqual(updated["status"], "REVIEWING")

    def test_rollback_published_chapter_is_rejected(self):
        job, _ = self.store.create_job(self.novel["id"], 1)
        NovelService(self.store, FakeClient(chapter_raw("V1")),
                     Config(data_dir=Path(self.tmp.name)), ROOT).process(job)
        ch = self.store.chapter(self.novel["id"], 1)
        self.store.db.execute("UPDATE chapters SET status='PUBLISHED_MANUALLY' WHERE id=?",
                              (ch["id"],))
        self.store.db.commit()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(f"/api/chapters/{ch['id']}/rollback", {"version": 1})
        self.assertEqual(ctx.exception.code, 409)

    def test_chapter_timeline_endpoint_replays_events_for_one_chapter(self):
        repo = EventRepository(self.store)
        self.store.create_job(self.novel["id"], 1)  # ensure the chapter row exists
        repo.publish(self.novel["id"], None, "llm.delta",
                     {"chapter": 1, "stage": "chapter", "text": "甲"})
        repo.publish(self.novel["id"], None, "llm.text", {"chapter": 1, "text": "甲"})
        repo.publish(self.novel["id"], None, "llm.delta",
                     {"chapter": 2, "stage": "chapter", "text": "乙"})
        data = self._get(f"/api/novels/{self.novel['id']}/chapters/1/timeline")
        types = [e["type"] for e in data["events"]]
        self.assertIn("llm.text", types)
        self.assertNotIn("乙", json.dumps(data["events"], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
