"""Tests for whole-book export (txt/md) via the API and store gate."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from novel_agent import server as srv
from novel_agent.config import Config
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
                          "input_tokens": 1, "output_tokens": 1,
                          "duration_ms": 1, "request_status": "succeeded"}


class StoreBookExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.novel = self.store.create_novel(
            "全本导出测试", {"styleRules": {"chapterLength": 0}})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _generate(self):
        job, _ = self.store.create_job(self.novel["id"], 1)
        NovelService(self.store, FakeClient(chapter_raw("第一章正文。")),
                     Config(data_dir=Path(self.tmp.name)), ROOT).process(job)
        ch = self.store.chapter(self.novel["id"], 1)
        self.store.db.execute(
            "UPDATE chapters SET status='EXPORTED' WHERE id=?", (ch["id"],))
        self.store.db.commit()

    def test_published_chapters_only_include_final_states(self):
        self.assertEqual(self.store.published_chapters(self.novel["id"]), [])
        self._generate()
        rows = self.store.published_chapters(self.novel["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "EXPORTED")
        self.assertEqual(rows[0]["content"], "第一章正文。")


class ServerBookExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.novel = self.store.create_novel(
            "全本导出测试", {"styleRules": {"chapterLength": 0}})
        self._prev = (srv.config, srv.store, srv.service, srv.STATIC_DIR)
        srv.config = Config(data_dir=Path(self.tmp.name))
        srv.store = self.store
        srv.service = None
        srv.STATIC_DIR = srv.ROOT / "static"
        srv._STOP_EVENT.clear()
        self.httpd = srv.NovelHTTPServer(("127.0.0.1", 0), srv.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
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

    def _post(self, path, data):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _publish_chapter(self, content="第一章正文。", number=1, title="开端"):
        job, _ = self.store.create_job(self.novel["id"], number)
        NovelService(self.store, FakeClient(chapter_raw(content, title)),
                     Config(data_dir=Path(self.tmp.name)), ROOT).process(job)
        ch = self.store.chapter(self.novel["id"], number)
        self.store.db.execute(
            "UPDATE chapters SET status='PUBLISHED_MANUALLY' WHERE id=?",
            (ch["id"],))
        self.store.db.commit()
        return ch

    def test_book_export_endpoint_writes_ordered_file(self):
        self._publish_chapter(content="第 1 章正文。\n\n第二段。")
        result = self._post(f"/api/novels/{self.novel['id']}/export-book",
                            {"format": "md"})
        self.assertEqual(result["format"], "md")
        self.assertEqual(result["chapters"], 1)
        path = Path(result["path"])
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        self.assertIn("# 全本导出测试", text)
        self.assertIn("## 1. 开端", text)
        self.assertIn("第 1 章正文。", text)

    def test_book_export_txt_and_conflict_when_nothing_published(self):
        self._publish_chapter(content="正文甲。")
        result = self._post(f"/api/novels/{self.novel['id']}/export-book",
                            {"format": "txt"})
        text = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn("第1章 开端", text)
        self.assertIn("正文甲。", text)
        other = self.store.create_novel("空书", {})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(f"/api/novels/{other['id']}/export-book",
                       {"format": "txt"})
        self.assertEqual(ctx.exception.code, 409)


if __name__ == "__main__":
    unittest.main()
