"""Regression tests for the manual review/approve/export flow and the light
chapter-list API.

These cover defects that the original suite did not exercise:

* ``POST /api/chapters/<id>/review`` used to feed the *persisted* chapter row
  (snake_case: ``goal``/``state_changes``/``foreshadowing_added``) straight into
  :func:`novel_agent.reviewer.review`, which reads the *model* contract
  (``chapterGoal``/``stateChanges``/...). Every manual re-review therefore
  returned a bogus ``missing_chapter_goal`` blocking issue and flipped the
  chapter to ``FAILED``, dead-ending the edit -> review -> approve -> export
  pipeline.
* A manual re-review of an ``EXPORTED`` chapter used to demote it away from
  ``EXPORTED``, breaking the manual-publish gate.
* The review window included the chapter under review, so its own prose was
  reported as ``recent_chapter_overlap``.
* ``approve`` was gated only by the UI: the server accepted an unreviewed draft.
* An invalid run ``config`` created the job before being rejected.
* The chapter list always shipped every chapter's full prose body.
"""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from novel_agent import server as srv
from novel_agent.config import Config
from novel_agent.reviewer import chapter_as_output, review
from novel_agent.service import NovelService
from novel_agent.store import Store

ROOT = Path(__file__).parents[1]


def chapter_raw(content="第一版正文，足够长的一句话用于重叠检测。\n\n第二段内容。", title="开端"):
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
        return self.raw, {"model": "t", "prompt_version": "novel-writer@2",
                          "input_tokens": 1, "output_tokens": 1, "duration_ms": 1,
                          "request_status": "succeeded"}


class ChapterAsOutputTests(unittest.TestCase):
    def test_row_aliases_map_to_the_model_contract(self):
        row = {
            "number": 3, "title": "t", "goal": "g", "content": "c",
            "hook": "h", "characters": ["a"], "events": ["e"],
            "foreshadowing_added": ["f"], "foreshadowing_resolved": ["r"],
            "state_changes": [{"character": "a"}],
        }
        out = chapter_as_output(row)
        self.assertEqual(out["chapterGoal"], "g")
        self.assertEqual(out["nextChapterHook"], "h")
        self.assertEqual(out["charactersUsed"], ["a"])
        self.assertEqual(out["eventsIntroduced"], ["e"])
        self.assertEqual(out["foreshadowingAdded"], ["f"])
        self.assertEqual(out["foreshadowingResolved"], ["r"])
        self.assertEqual(out["stateChanges"], [{"character": "a"}])
        self.assertEqual(out["chapterNumber"], 3)
        for stale in ("goal", "hook", "characters", "events",
                      "foreshadowing_added", "foreshadowing_resolved", "state_changes"):
            self.assertNotIn(stale, out)

    def test_review_of_a_normalised_row_passes(self):
        row = {"number": 1, "title": "开端", "goal": "主角登场",
               "content": "第一段正文。\n\n第二段正文。", "hook": "钩子",
               "review": {}, "proposed_state": {}}
        result = review(chapter_as_output(row), {}, [], 0)
        self.assertTrue(result["passed"], result["blockingIssues"])
        self.assertNotIn("missing_chapter_goal", result["blockingIssues"])

    def test_review_of_a_raw_row_still_reports_the_defect(self):
        # Documents *why* the normaliser exists: the raw row is missing the
        # camelCase keys the reviewer reads.
        row = {"number": 1, "title": "开端", "goal": "主角登场",
               "content": "第一段正文。\n\n第二段正文。", "hook": "钩子"}
        result = review(row, {}, [], 0)
        self.assertFalse(result["passed"])
        self.assertIn("missing_chapter_goal", result["blockingIssues"])


class StoreQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.novel = self.store.create_novel(
            "查询测试", {"styleRules": {"chapterLength": 0}, "mainline": "主线"}
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _generate(self, number, content):
        job, _ = self.store.create_job(self.novel["id"], number)
        raw = json.loads(chapter_raw(content))
        raw["chapterNumber"] = number
        NovelService(self.store, FakeClient(json.dumps(raw, ensure_ascii=False)),
                     Config(data_dir=Path(self.tmp.name)), ROOT).process(job)
        return self.store.chapter(self.novel["id"], number)

    def test_recent_is_bounded_and_excludes_self_on_request(self):
        for number in range(1, 6):
            self._generate(number, "第 %d 章正文。\n\n第二段。" % number)
        tail = self.store.recent(self.novel["id"])
        self.assertEqual([c["number"] for c in tail], [3, 4, 5])
        window = self.store.recent(self.novel["id"], before=4)
        self.assertEqual([c["number"] for c in window], [1, 2, 3])
        self.assertEqual(self.store.recent(self.novel["id"], limit=0), [])

    def test_chapters_returns_every_chapter_in_order_with_decoded_json(self):
        self._generate(1, "甲。\n\n乙。")
        self._generate(2, "丙。\n\n丁。")
        chapters = self.store.chapters(self.novel["id"])
        self.assertEqual([c["number"] for c in chapters], [1, 2])
        self.assertIsInstance(chapters[0]["beats"], list)
        self.assertIsInstance(chapters[0]["review"], dict)
        self.assertTrue(chapters[0]["content"])

    def test_novels_list_includes_the_current_bible(self):
        novels = self.store.novels()
        self.assertEqual(len(novels), 1)
        self.assertEqual(novels[0]["story_bible"]["mainline"], "主线")

    def test_chapter_summaries_omit_bodies_but_report_their_length(self):
        chapter = self._generate(1, "长正文。\n\n第二段。")
        summaries = self.store.chapter_summaries(self.novel["id"])
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["content"], "")
        self.assertEqual(summaries[0]["content_length"], len(chapter["content"]))
        self.assertEqual(summaries[0]["title"], chapter["title"])

    def test_chapter_summaries_support_query_and_recent_limit(self):
        for number in range(1, 6):
            self._generate(number, "第 %d 章。\n\n第二段。" % number)
        recent = self.store.chapter_summaries(self.novel["id"], limit=2)
        self.assertEqual([c["number"] for c in recent], [4, 5])
        hit = self.store.chapter_summaries(self.novel["id"], query="3")
        self.assertEqual([c["number"] for c in hit], [3])
        self.assertEqual(
            self.store.chapter_summaries(self.novel["id"], query="没有这样的章节"), []
        )

    def test_update_draft_rejects_terminal_chapters(self):
        job, _ = self.store.create_job(self.novel["id"], 1)
        NovelService(self.store, FakeClient(chapter_raw("正文。\n\n第二段。")),
                     Config(data_dir=Path(self.tmp.name)), ROOT).process(job)
        chapter = self.store.chapter(self.novel["id"], 1)
        self.store.db.execute(
            "UPDATE chapters SET status='PUBLISHED_MANUALLY' WHERE id=?", (chapter["id"],)
        )
        self.store.db.commit()
        with self.assertRaises(ValueError) as ctx:
            self.store.update_draft(chapter["id"], {"content": "篡改。"})
        self.assertEqual(str(ctx.exception), "chapter_is_terminal")
        self.assertEqual(
            self.store.chapter_by_id(chapter["id"])["content"], chapter["content"]
        )


class ReviewFlowHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.novel = self.store.create_novel("审查流程", {"styleRules": {"chapterLength": 0}})
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

    def _request(self, method, path, data=None):
        body = None if data is None else json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=body,
            headers={"Content-Type": "application/json"} if body else {},
            method=method,
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get(self, path):
        return self._request("GET", path)

    def _post(self, path, data=None):
        return self._request("POST", path, data if data is not None else {})

    def _patch(self, path, data):
        return self._request("PATCH", path, data)

    def _status_code(self, method, path, data=None):
        try:
            self._request(method, path, data)
        except urllib.error.HTTPError as exc:
            return exc.code
        return 200

    def _generate(self, number=1, content="第一版正文。\n\n第二段内容。", title="开端"):
        job, _ = self.store.create_job(self.novel["id"], number)
        raw = json.loads(chapter_raw(content, title))
        raw["chapterNumber"] = number
        NovelService(self.store, FakeClient(json.dumps(raw, ensure_ascii=False)),
                     Config(data_dir=Path(self.tmp.name)), ROOT).process(job)
        return self.store.chapter(self.novel["id"], number)

    def test_server_side_chapter_search_and_recent_limit(self):
        titles = ["开端", "试炼", "觉醒", "试炼再现", "终局"]
        for index, title in enumerate(titles, start=1):
            self._generate(index, "第 %d 章正文。\n\n第二段。" % index, title)

        base = "/api/novels/" + self.novel["id"] + "/chapters?light=1"
        hits = self._get(base + "&q=" + urllib.parse.quote("试炼"))
        self.assertEqual([c["number"] for c in hits], [2, 4])

        by_number = self._get(base + "&q=3")
        self.assertEqual([c["number"] for c in by_number], [3])

        missing = self._get(base + "&q=" + urllib.parse.quote("不存在的情节"))
        self.assertEqual(missing, [])

        # No query + limit -> the most recent window, oldest first.
        recent = self._get(base + "&limit=2")
        self.assertEqual([c["number"] for c in recent], [4, 5])


    def test_edit_then_rereview_then_approve_then_export(self):
        chapter = self._generate()
        self.assertEqual(chapter["status"], "WAITING_APPROVAL")
        cid = chapter["id"]

        edited = self._patch("/api/chapters/" + cid, {"content": "改写后的正文。\n\n另一个段落。"})
        self.assertEqual(edited["status"], "REVIEWING")
        self.assertEqual(edited["review"], {})

        result = self._post("/api/chapters/" + cid + "/review")
        self.assertTrue(result["passed"], result["blockingIssues"])
        self.assertNotIn("missing_chapter_goal", result["blockingIssues"])
        self.assertNotIn("recent_chapter_overlap", result["blockingIssues"])
        self.assertEqual(self.store.chapter_by_id(cid)["status"], "WAITING_APPROVAL")

        approved = self._post("/api/chapters/" + cid + "/approve")
        self.assertEqual(approved["status"], "DRAFT_READY")

        exported = self._post("/api/chapters/" + cid + "/export",
                              {"novelId": self.novel["id"], "chapterNumber": 1, "format": "txt"})
        self.assertEqual(exported["status"], "EXPORTED")
        self.assertTrue(Path(exported["path"]).is_file())

    def test_rereview_reports_the_goal_from_the_persisted_row(self):
        chapter = self._generate()
        self.assertEqual(chapter["goal"], "目标")
        result = self._post("/api/chapters/" + chapter["id"] + "/review")
        self.assertTrue(result["passed"], result["blockingIssues"])

    def test_rereview_does_not_treat_own_prose_as_recent_overlap(self):
        self._generate(1, "第一章独有句子内容。\n\n第二段。")
        chapter = self._generate(2, "第二章独有句子内容。\n\n第二段。")
        result = self._post("/api/chapters/" + chapter["id"] + "/review")
        self.assertTrue(result["passed"], result["blockingIssues"])

    def test_rereview_of_an_exported_chapter_keeps_it_exported(self):
        chapter = self._generate()
        cid = chapter["id"]
        self._post("/api/chapters/" + cid + "/export",
                   {"novelId": self.novel["id"], "chapterNumber": 1, "format": "txt"})
        self._post("/api/chapters/" + cid + "/review")
        self.assertEqual(self.store.chapter_by_id(cid)["status"], "EXPORTED")

    def _publish(self, chapter):
        self._post("/api/chapters/" + chapter["id"] + "/export",
                   {"novelId": self.novel["id"], "chapterNumber": chapter["number"],
                    "format": "txt"})
        return self._post("/api/chapters/" + chapter["id"] + "/publish",
                          {"novelId": self.novel["id"],
                           "chapterNumber": chapter["number"],
                           "platform": "番茄小说", "operator": "tester"})

    def test_published_chapter_body_is_frozen(self):
        chapter = self._generate()
        published = self._publish(chapter)
        self.assertEqual(published["status"], "PUBLISHED_MANUALLY")

        code = self._status_code("PATCH", "/api/chapters/" + chapter["id"],
                                 {"content": "被篡改的正文。"})
        self.assertEqual(code, 409)

        after = self.store.chapter_by_id(chapter["id"])
        self.assertEqual(after["status"], "PUBLISHED_MANUALLY")
        self.assertEqual(after["content"], chapter["content"])

    def test_published_chapter_cannot_be_rolled_back(self):
        chapter = self._generate()
        self._publish(chapter)
        self.assertEqual(
            self._status_code("POST", "/api/chapters/" + chapter["id"] + "/rollback",
                              {"version": 1}),
            409,
        )

    def test_approve_is_rejected_without_a_passing_review(self):
        chapter = self._generate()
        cid = chapter["id"]
        self._patch("/api/chapters/" + cid, {"content": "改写正文。\n\n第二段。"})
        self.assertEqual(self._status_code("POST", "/api/chapters/" + cid + "/approve"), 409)
        self.assertEqual(self.store.chapter_by_id(cid)["status"], "REVIEWING")

    def test_invalid_run_config_does_not_create_a_job(self):
        code = self._status_code(
            "POST",
            "/api/novels/" + self.novel["id"] + "/chapters/generate",
            {"chapterNumber": 1, "config": {"targetWords": "abc"}},
        )
        self.assertEqual(code, 400)
        self.assertEqual(self.store.jobs(self.novel["id"]), [])

    def test_light_chapter_list_omits_bodies(self):
        chapter = self._generate(1, "很长的正文内容。\n\n第二段。")
        full = self._get("/api/novels/" + self.novel["id"] + "/chapters")
        light = self._get("/api/novels/" + self.novel["id"] + "/chapters?light=1")
        self.assertEqual(full[0]["content"], chapter["content"])
        self.assertEqual(light[0]["content"], "")
        self.assertEqual(light[0]["content_length"], len(chapter["content"]))
        self.assertEqual(light[0]["title"], full[0]["title"])
        self.assertEqual(light[0]["status"], full[0]["status"])

    def test_chapter_detail_endpoint_backs_the_light_list(self):
        chapter = self._generate()
        detail = self._get("/api/chapters/" + chapter["id"])
        self.assertEqual(detail["content"], chapter["content"])

    def test_export_io_failure_is_recorded_and_reported(self):
        chapter = self._generate()

        def boom(*args, **kwargs):
            raise OSError("simulated disk failure")

        original = srv.export_chapter
        srv.export_chapter = boom
        try:
            code = self._status_code(
                "POST", "/api/chapters/" + chapter["id"] + "/export",
                {"novelId": self.novel["id"], "chapterNumber": 1, "format": "txt"},
            )
        finally:
            srv.export_chapter = original

        self.assertEqual(code, 500)
        job = self.store.export_job(chapter, "txt")
        self.assertEqual(job["status"], "FAILED")
        self.assertIn("OSError", job["error"])
        # The chapter must not be marked exported when no file was written.
        self.assertNotEqual(self.store.chapter_by_id(chapter["id"])["status"], "EXPORTED")

    def test_invalid_config_does_not_corrupt_an_existing_job(self):
        self._post("/api/novels/" + self.novel["id"] + "/chapters/generate",
                   {"chapterNumber": 1})
        self.assertEqual(len(self.store.jobs(self.novel["id"])), 1)
        code = self._status_code(
            "POST", "/api/novels/" + self.novel["id"] + "/chapters/generate",
            {"chapterNumber": 2, "config": {"stages": "outline"}},
        )
        self.assertEqual(code, 400)
        self.assertEqual(len(self.store.jobs(self.novel["id"])), 1)


if __name__ == "__main__":
    unittest.main()
