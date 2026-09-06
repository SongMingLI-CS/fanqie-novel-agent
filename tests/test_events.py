"""Tests for the cross-process SQLite event bus."""
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from novel_agent.events import EventRepository
from novel_agent.store import Store


class EventRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.repo = EventRepository(self.store)
        self.nid = self.store.create_novel("事件", {})["id"]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_publish_returns_hydrated_event_with_monotonic_id(self):
        a = self.repo.publish(self.nid, None, "llm.delta", {"text": "一"})
        b = self.repo.publish(self.nid, "run1", "agent.stage", {"stage": "outline"})
        self.assertEqual(a["type"], "llm.delta")
        self.assertEqual(a["payload"], {"text": "一"})
        self.assertLess(a["id"], b["id"])
        self.assertEqual(b["run_id"], "run1")

    def test_read_since_is_cursor_based_and_ordered(self):
        ids = [self.repo.publish(self.nid, None, "t", {"i": i})["id"] for i in range(5)]
        first = self.repo.read_since(self.nid, 0)
        self.assertEqual([e["id"] for e in first], ids)
        after2 = self.repo.read_since(self.nid, ids[2])
        self.assertEqual([e["id"] for e in after2], ids[3:])
        # Other novels are never mixed in.
        other = self.store.create_novel("另一本", {})["id"]
        self.repo.publish(other, None, "t", {})
        self.assertEqual(len(self.repo.read_since(self.nid, 0)), 5)

    def test_prune_removes_only_expired_rows(self):
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        self.store.db.execute(
            "INSERT INTO events(novel_id, run_id, type, payload, created_at) "
            "VALUES (?,?,?,?,?)",
            (self.nid, None, "stale", "{}", old),
        )
        self.store.db.commit()
        self.repo.publish(self.nid, None, "fresh", {})
        removed = self.repo.prune(keep_days=7)
        self.assertEqual(removed, 1)
        remaining = self.repo.read_since(self.nid, 0)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["type"], "fresh")

    def test_concurrent_publish_keeps_distinct_monotonic_ids(self):
        errors = []
        ids = []

        def worker(i):
            try:
                ev = self.repo.publish(self.nid, None, "t", {"i": i})
                ids.append(ev["id"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                self.store.close()  # release this thread's connection

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(15)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(ids)), 15)


if __name__ == "__main__":
    unittest.main()
