"""Tests for jobs/audit list filters and pagination (store + HTTP)."""
import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from novel_agent import server as srv
from novel_agent.config import Config
from novel_agent.store import Store

ROOT = Path(__file__).parents[1]


class StoreQueryFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.novel = self.store.create_novel("查询过滤测试", {})
        self.novel2 = self.store.create_novel("另一本", {})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _seed_jobs(self):
        for n in (1, 2, 3):
            self.store.create_job(self.novel["id"], n)
        job, _ = self.store.create_job(self.novel["id"], 4)
        self.store.cancel_job(job["id"])

    def test_jobs_filter_status_and_limit(self):
        self._seed_jobs()
        pending = self.store.jobs(self.novel["id"], status="PENDING")
        self.assertEqual(len(pending), 3)
        self.assertTrue(all(j["status"] == "PENDING" for j in pending))
        limited = self.store.jobs(self.novel["id"], limit=2)
        self.assertEqual(len(limited), 2)
        cancelled = self.store.jobs(self.novel["id"], status="CANCELLED")
        self.assertEqual(len(cancelled), 1)
        # other novel unaffected
        self.assertEqual(self.store.jobs(self.novel2["id"]), [])

    def test_audit_trail_filter_action_and_novel(self):
        self.store.record_audit(self.novel["id"], "novel_create", {"v": 1})
        self.store.record_audit(self.novel["id"], "chapter_rewrite", {"v": 2})
        self.store.record_audit(self.novel2["id"], "novel_create", {"v": 3})
        by_action = self.store.audit_trail(limit=50, action="novel_create")
        self.assertEqual(len(by_action), 2)
        self.assertTrue(all(a["action"] == "novel_create" for a in by_action))
        by_novel = self.store.audit_trail(limit=50, novel_id=self.novel["id"])
        got = {a["action"] for a in by_novel}
        self.assertEqual(got, {"novel_created", "novel_create", "chapter_rewrite"})
        both = self.store.audit_trail(limit=50, action="chapter_rewrite",
                                      novel_id=self.novel["id"])
        self.assertEqual(len(both), 1)
        self.assertEqual(json.loads(both[0]["detail"]), {"v": 2})


class ServerQueryFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.novel = self.store.create_novel("查询过滤测试", {})
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

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_jobs_and_audit_query_params(self):
        for n in (1, 2):
            self.store.create_job(self.novel["id"], n)
        job, _ = self.store.create_job(self.novel["id"], 3)
        self.store.cancel_job(job["id"])
        self.store.record_audit(self.novel["id"], "novel_create", {"a": 1})
        self.store.record_audit(self.novel["id"], "pause", {"a": 2})

        pending = self._get(f"/api/novels/{self.novel['id']}/jobs?status=PENDING")
        self.assertEqual(len(pending), 2)
        only_one = self._get(f"/api/novels/{self.novel['id']}/jobs?limit=1")
        self.assertEqual(len(only_one), 1)
        audits = self._get(f"/api/ops/audit?action=pause&limit=20")
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["action"], "pause")
        by_novel = self._get(f"/api/ops/audit?novel_id={self.novel['id']}&limit=20")
        got = {a["action"] for a in by_novel}
        self.assertEqual(got, {"novel_created", "novel_create", "pause", "job_cancelled"})


if __name__ == "__main__":
    unittest.main()
