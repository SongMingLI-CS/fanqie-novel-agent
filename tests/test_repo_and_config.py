"""Tests for the checkpoint DAO thread-safety and the run-config plumbing."""
import tempfile
import threading
import unittest
from pathlib import Path

from novel_agent.checkpoints import CheckpointRepository
from novel_agent.config import Config
from novel_agent.server import ApiError, _sanitize_run_config
from novel_agent.service import NovelService
from novel_agent.store import Store

ROOT = Path(__file__).parents[1]


class FakeClient:
    def complete(self, system, user):
        return "{}", {}


class RepoAndConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.novel = self.store.create_novel("并发测试", {"mainline": "主线"})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_concurrent_checkpoint_writes_are_serialised(self):
        job, _ = self.store.create_job(self.novel["id"], 1)
        repo = CheckpointRepository(self.store)
        run = repo.create_run(job, ["outline", "chapter"], 0, True)
        stages = [f"stage-{i}" for i in range(20)]
        errors = []

        def worker(stage):
            try:
                repo.mark_stage_started(run["id"], stage)
                repo.mark_stage_done(run["id"], stage, {"stage": stage}, "{}")
            except Exception as exc:  # noqa: BLE001 - captured for assertion
                errors.append(exc)
            finally:
                # Release this thread's per-thread connection before cleanup.
                repo.store.close()

        threads = [threading.Thread(target=worker, args=(s,)) for s in stages]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        states = repo.stage_states(run["id"])
        self.assertEqual(len(states), len(stages))
        self.assertTrue(all(v == "DONE" for v in states.values()))

    def test_latest_run_for_novel_read_model(self):
        job, _ = self.store.create_job(self.novel["id"], 1)
        repo = CheckpointRepository(self.store)
        run = repo.create_run(job, ["outline", "chapter"], 1200, False)
        repo.mark_stage_started(run["id"], "outline")
        repo.mark_stage_done(run["id"], "outline", {"title": "开端"}, "{}")
        latest = repo.latest_run_for_novel(self.novel["id"])
        self.assertEqual(latest["id"], run["id"])
        self.assertEqual(latest["stages"], ["outline", "chapter"])
        self.assertEqual(latest["target_words"], 1200)
        self.assertFalse(latest["auto_export_txt"])
        self.assertEqual(latest["stagesDetail"][0]["state"], "DONE")

    def test_sanitize_run_config_normalises_stages(self):
        self.assertIsNone(_sanitize_run_config(None))
        self.assertEqual(
            _sanitize_run_config({"stages": ["outline", "polish"]})["stages"], ["chapter"]
        )
        self.assertEqual(
            _sanitize_run_config({"stages": ["outline", "chapter", "polish"]})["stages"],
            ["outline", "chapter", "polish"],
        )
        self.assertEqual(_sanitize_run_config({"targetWords": "300"})["targetWords"], 300)

    def test_sanitize_run_config_rejects_bad_input(self):
        with self.assertRaises(ApiError):
            _sanitize_run_config("not-an-object")
        with self.assertRaises(ApiError):
            _sanitize_run_config({"stages": "outline"})
        with self.assertRaises(ApiError):
            _sanitize_run_config({"targetWords": "abc"})
        with self.assertRaises(ApiError):
            _sanitize_run_config({"autoExportTxt": "yes"})

    def test_run_config_resolution_from_job(self):
        job, _ = self.store.create_job(self.novel["id"], 1)
        self.store.set_job_config(job["id"], {"stages": ["outline", "chapter"], "targetWords": 800})
        svc = NovelService(self.store, FakeClient(), Config(data_dir=Path(self.tmp.name)), ROOT)
        resolved = svc._run_config(self.store.get_job(job["id"]))
        self.assertEqual(resolved["stages"], ["outline", "chapter"])
        self.assertEqual(resolved["targetWords"], 800)


if __name__ == "__main__":
    unittest.main()
