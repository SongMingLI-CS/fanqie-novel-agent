"""Tests for the multi-agent pipeline, checkpointing and crash recovery."""
import json
import tempfile
import unittest
from pathlib import Path

from novel_agent.checkpoints import CheckpointRepository
from novel_agent.config import Config
from novel_agent.service import NovelService
from novel_agent.store import Store
from novel_agent.worker import run_once

ROOT = Path(__file__).parents[1]


def usage():
    return {
        "model": "test-r1", "prompt_version": "novel-writer@2",
        "input_tokens": 1, "output_tokens": 2, "duration_ms": 1,
        "request_status": "succeeded",
    }


def outline_json(n=1, title="开端"):
    return json.dumps({
        "chapterNumber": n, "title": title, "chapterGoal": "找到线索",
        "beats": [{"goal": "调查"}], "charactersUsed": [],
        "eventsIntroduced": [], "foreshadowingAdded": [],
        "foreshadowingResolved": [], "stateChanges": [],
        "nextChapterHook": "门开了", "warnings": [],
    }, ensure_ascii=False)


def chapter_json(n=1, content="第一段。\n\n他说：“继续。”"):
    return json.dumps({
        "chapterNumber": n, "title": "开端", "chapterGoal": "找到线索",
        "summary": "发现线索", "beats": [{"goal": "调查"}], "content": content,
        "charactersUsed": [], "eventsIntroduced": [], "foreshadowingAdded": [],
        "foreshadowingResolved": [], "stateChanges": [],
        "nextChapterHook": "门开了", "warnings": [],
    }, ensure_ascii=False)


class SeqClient:
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = []

    def complete(self, system, user):
        self.calls.append(user)
        return self.texts.pop(0), usage()


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.novel = self.store.create_novel(
            "测试小说", {"characters": [{"name": "林默"}], "styleRules": {"chapterLength": 0}}
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _service(self, client, **cfg):
        return NovelService(self.store, client, Config(data_dir=Path(self.tmp.name), **cfg), ROOT)

    def test_outline_then_chapter_persists_run(self):
        job, _ = self.store.create_job(self.novel["id"], 1)
        self.store.set_job_config(job["id"], {"stages": ["outline", "chapter"]})
        job = self.store.get_job(job["id"])  # fresh row carries the config
        client = SeqClient([outline_json(), chapter_json()])
        self.assertTrue(self._service(client).process(job))
        self.assertEqual(len(client.calls), 2)
        self.assertIn("开端", client.calls[1])  # chapter prompt received the outline
        self.assertTrue(self.store.chapter(self.novel["id"], 1)["content"])
        repo = CheckpointRepository(self.store)
        run = repo.find_run(job["id"])
        self.assertEqual(run["status"], "SUCCEEDED")
        self.assertIsNone(repo.resume_from(run, ["outline", "chapter"]))

    def test_polish_is_final_output_when_enabled(self):
        job, _ = self.store.create_job(self.novel["id"], 1)
        self.store.set_job_config(job["id"], {"stages": ["outline", "chapter", "polish"]})
        job = self.store.get_job(job["id"])
        polished = json.loads(chapter_json())
        polished["content"] = "润色后的正文。\n\n更好了。"
        client = SeqClient([outline_json(), chapter_json(), json.dumps(polished, ensure_ascii=False)])
        self.assertTrue(self._service(client).process(job))
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(
            self.store.chapter(self.novel["id"], 1)["content"], "润色后的正文。\n\n更好了。"
        )

    def test_compose_path_used_without_stages(self):
        job, _ = self.store.create_job(self.novel["id"], 1)
        client = SeqClient([chapter_json()])
        self.assertTrue(self._service(client).process(job))
        self.assertEqual(len(client.calls), 1)
        sent = json.loads(client.calls[0])
        self.assertIn("storyBible", sent)

    def test_recovery_resumes_at_chapter_stage(self):
        job, _ = self.store.create_job(self.novel["id"], 1)
        self.store.set_job_config(job["id"], {"stages": ["outline", "chapter"]})

        class CrashClient(SeqClient):
            def complete(self, system, user):
                self.calls.append(user)
                if len(self.calls) == 1:
                    return outline_json(), usage()
                raise RuntimeError("boom at chapter")

        self.assertTrue(run_once(self.store, self._service(CrashClient([]), max_job_attempts=3)))
        repo = CheckpointRepository(self.store)
        run = repo.find_run(job["id"])
        self.assertEqual(repo.stage_states(run["id"]).get("outline"), "DONE")
        self.assertEqual(self.store.get_job(job["id"])["status"], "PENDING")

        # Make the retry immediately claimable, then resume with a working client.
        self.store.db.execute(
            "UPDATE jobs SET next_attempt_at='2000-01-01T00:00:00+00:00' WHERE id=?", (job["id"],)
        )
        self.store.db.commit()
        resume_client = SeqClient([chapter_json()])
        self.assertTrue(run_once(self.store, self._service(resume_client)))
        # Only the chapter stage re-ran; the completed outline was not re-called.
        self.assertEqual(len(resume_client.calls), 1)
        self.assertEqual(self.store.chapter(self.novel["id"], 1)["status"], "WAITING_APPROVAL")
        self.assertEqual(repo.find_run(job["id"])["status"], "SUCCEEDED")


if __name__ == "__main__":
    unittest.main()
