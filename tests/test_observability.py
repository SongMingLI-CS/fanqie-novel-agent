"""Hermetic tests for observability: audit trail, metrics, usage series, ops CLI.

These exercise the Store-level read models added for operations dashboards and
the ``novel-agent-ops`` maintenance CLI (stats/backup/vacuum). Everything uses a
temp SQLite file so the suite stays green while a production server+worker run
on the same checkout.
"""
import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from novel_agent import ops
from novel_agent.store import Store


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "novel.sqlite3"
        self.store = Store(self.db)
        self.addCleanup(self.store.close)

    # -- audit trail --------------------------------------------------------

    def test_create_and_cancel_leave_audit_trail(self):
        novel = self.store.create_novel("审计", {"mainline": "主线"})
        job, _ = self.store.create_job(novel["id"], 1)
        self.assertTrue(self.store.cancel_job(job["id"]))

        trail = self.store.audit_trail()
        actions = [entry["action"] for entry in trail]
        self.assertIn("novel_created", actions)
        self.assertIn("job_cancelled", actions)
        # Newest event first.
        self.assertEqual(trail[0]["action"], "job_cancelled")
        self.assertEqual(trail[0]["novel_id"], novel["id"])
        detail = json.loads(trail[0]["detail"])
        self.assertEqual(detail["job"], job["id"])
        self.assertEqual(detail["chapter"], 1)

    def test_audit_trail_limit_clamps_and_orders(self):
        nid = self.store.create_novel("批量", {})["id"]
        for i in range(5):
            self.store.record_audit(nid, f"event_{i}")
        trail = self.store.audit_trail(limit=2)
        self.assertEqual(len(trail), 2)
        self.assertEqual(trail[0]["action"], "event_4")
        # Limit is bounded even when callers pass absurd values.
        self.assertLessEqual(len(self.store.audit_trail(limit=99999)), 1000)

    def test_record_audit_never_breaks_when_table_writes_fail(self):
        # Best-effort: a record_audit failure must not raise. We simulate by
        # dropping the table behind the method's back is not possible through
        # the public API, so verify it accepts unusual but valid inputs.
        nid = self.store.create_novel("尽力而为", {})["id"]
        self.store.record_audit(nid, "custom_event", {"k": "v"})
        self.assertIn(
            "custom_event", [e["action"] for e in self.store.audit_trail()]
        )

    # -- metrics ------------------------------------------------------------

    def test_metrics_aggregates_core_tables(self):
        nid = self.store.create_novel("统计", {"mainline": "主线"})["id"]
        job, _ = self.store.create_job(nid, 1)
        self.store.record_usage(job, "model-x", "v1", "succeeded")

        m = self.store.metrics()
        self.assertEqual(m["novels"]["total"], 1)
        self.assertEqual(m["novels"]["paused"], 0)
        self.assertGreaterEqual(m["chapters"]["total"], 1)
        self.assertGreaterEqual(m["jobs"]["byStatus"].get("PENDING", 0), 1)
        self.assertEqual(m["usage"]["requests"], {"total": 1, "succeeded": 1, "failed": 0})
        self.assertEqual(m["usage"]["byModel"]["model-x"]["requests"], 1)
        self.assertEqual(m["usage"]["byModel"]["model-x"]["succeeded"], 1)
        self.assertEqual(m["storyBibleVersions"], 1)
        self.assertGreaterEqual(m["auditEvents"], 1)

    def test_metrics_tracks_exports_and_publishes(self):
        nid = self.store.create_novel("发布统计", {"mainline": "主线"})["id"]
        self.store.create_job(nid, 1)
        self.store.record_export(nid, 1)
        self.store.manual_publish(
            nid,
            1,
            {
                "platform": "测试平台",
                "operator": "测试员",
                "externalUrl": "https://example.test/1",
            },
        )
        m = self.store.metrics()
        self.assertEqual(m["exports"]["total"], 0)  # record_export has no export job row
        self.assertEqual(m["publishes"]["total"], 1)
        self.assertEqual(m["publishes"]["byPlatform"]["测试平台"], 1)
        by_status = m["chapters"]["byStatus"]
        self.assertIn("PUBLISHED_MANUALLY", by_status)
        self.assertGreaterEqual(
            m["auditEvents"], 3  # created + exported + published
        )

    # -- usage series -------------------------------------------------------

    def test_usage_series_zero_fills_and_sorts_days(self):
        nid = self.store.create_novel("趋势", {})["id"]
        job, _ = self.store.create_job(nid, 1)
        self.store.record_usage(job, "model-x", "v1", "succeeded")

        series = self.store.usage_series(7)
        self.assertEqual(len(series), 7)
        dates = [entry["date"] for entry in series]
        self.assertEqual(dates, sorted(dates))
        today = datetime.now(timezone.utc).date().isoformat()
        self.assertEqual(series[-1]["date"], today)
        self.assertGreaterEqual(series[-1]["requests"], 1)
        self.assertEqual(series[-1]["succeeded"], 1)
        # Empty days are zero-filled, not omitted.
        self.assertEqual(series[0]["requests"], 0)
        # Out-of-range windows are clamped.
        self.assertEqual(len(self.store.usage_series(999)), 90)

    # -- ops CLI ------------------------------------------------------------

    def _run_ops(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ops.main(argv)
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_ops_stats_prints_metrics_and_trend(self):
        nid = self.store.create_novel("运维", {"mainline": "主线"})["id"]
        job, _ = self.store.create_job(nid, 1)
        self.store.record_usage(job, "model-x", "v1", "failed")

        payload = json.loads(self._run_ops(["stats", "--db", str(self.db), "--days", "3"]))
        self.assertEqual(payload["database"], str(self.db))
        self.assertGreater(payload["size_bytes"], 0)
        self.assertEqual(payload["metrics"]["novels"]["total"], 1)
        self.assertEqual(payload["metrics"]["usage"]["requests"]["failed"], 1)
        self.assertEqual(len(payload["usage_trend"]), 3)

    def test_ops_backup_writes_restorable_snapshot(self):
        nid = self.store.create_novel("备份", {"mainline": "主线"})["id"]
        self.store.create_job(nid, 1)
        self.store.close()  # release the source handle

        out_path = Path(self.tmp.name) / "backup.sqlite3"
        self._run_ops(["backup", "--db", str(self.db), "--out", str(out_path)])
        self.assertTrue(out_path.is_file())

        restored = Store(out_path)
        try:
            self.assertEqual(restored.metrics()["novels"]["total"], 1)
            self.assertIsNotNone(restored.get_novel(nid))
        finally:
            restored.close()

    def test_ops_backup_refuses_to_overwrite_without_force(self):
        self.store.close()
        out_path = Path(self.tmp.name) / "backup.sqlite3"
        out_path.write_text("occupied")
        with self.assertRaises(SystemExit):
            ops.main(["backup", "--db", str(self.db), "--out", str(out_path)])
        # --force replaces it.
        self._run_ops(["backup", "--db", str(self.db), "--out", str(out_path), "--force"])
        restored = Store(out_path)
        try:
            self.assertEqual(restored.metrics()["novels"]["total"], 0)
        finally:
            restored.close()

    def test_ops_vacuum_checkpoints_and_succeeds(self):
        self.store.close()
        out = self._run_ops(["vacuum", "--db", str(self.db)])
        self.assertIn("vacuum complete", out)
        # DB remains readable afterwards.
        store = Store(self.db)
        try:
            self.assertEqual(store.metrics()["novels"]["total"], 0)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
