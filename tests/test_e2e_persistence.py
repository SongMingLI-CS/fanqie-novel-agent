"""End-to-end persistence test against a *real* server subprocess.

The rest of the suite talks to an in-process ``HTTPHandler``; this one proves the
acceptance criteria that only a live process can demonstrate:

* the console really serves from disk and the API really enforces auth;
* every write lands in SQLite and is still there after the process is restarted
  (a plain in-memory or per-process cache would silently pass the in-process
  tests yet fail this one);
* job creation stays idempotent and the ops counters/audit trail are computed
  from real rows, not from synthesized numbers.

Hermetic like ``test_http_ops``: ``NOVEL_PORT=0`` (ephemeral), an isolated
``NOVEL_DATA_DIR`` and a ``NOVEL_ENV_FILE`` pointing at a non-existent file, so a
production server/worker running in the same checkout is never touched.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parents[1]
TOKEN = "e2e-token"


def _env(data_dir):
    env = os.environ.copy()
    env["NOVEL_HOST"] = "127.0.0.1"
    env["NOVEL_PORT"] = "0"
    env["NOVEL_DATA_DIR"] = data_dir
    env["NOVEL_ENV_FILE"] = os.path.join(data_dir, "no-such.env")
    env["NOVEL_AUTH_TOKEN"] = TOKEN
    env["NOVEL_LOG_LEVEL"] = "WARNING"
    env.pop("DEEPSEEK_API_KEY", None)
    return env


def _start(data_dir):
    proc = subprocess.Popen(
        [sys.executable, "-m", "novel_agent.server"],
        cwd=str(ROOT), env=_env(data_dir),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise AssertionError("server exited before announcing its port")
            continue
        match = re.search(r"NOVEL_AGENT_LISTENING (\S+):(\d+)", line)
        if match:
            return proc, "http://%s:%s" % (match.group(1), match.group(2))
    proc.kill()
    raise AssertionError("server did not announce its port in time")


def _call(method, url, body=None, token=TOKEN):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data else {}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            payload = resp.read().decode("utf-8")
            if "json" in resp.headers.get("Content-Type", ""):
                return resp.status, json.loads(payload)
            return resp.status, payload
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(payload)
        except ValueError:
            return exc.code, payload


class E2EPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = None

    def tearDown(self):
        self._stop()

    def _stop(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=20)
            if self.proc.stdout is not None:
                try:
                    self.proc.stdout.close()
                except OSError:
                    pass
        self.proc = None

    def _restart(self):
        self._stop()
        self.proc, base = _start(self.tmp.name)
        return base

    def test_full_flow_persists_across_a_process_restart(self):
        self.proc, base = _start(self.tmp.name)

        status, body = _call("GET", base + "/healthz", token=None)
        self.assertEqual(status, 200)
        status, body = _call("GET", base + "/readyz", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(body["checks"]["store"], "ok")

        # Auth is enforced by the server, not by the UI.
        status, _ = _call("GET", base + "/api/novels", token=None)
        self.assertEqual(status, 401)
        status, _ = _call("GET", base + "/api/novels", token="wrong")
        self.assertEqual(status, 401)
        status, body = _call("GET", base + "/api/novels")
        self.assertEqual((status, body), (200, []))

        # The console is served from disk.
        status, html = _call("GET", base + "/", token=None)
        self.assertEqual(status, 200)
        self.assertIn("<title>", html)

        status, novel = _call("POST", base + "/api/novels", {
            "title": "持久化测试", "genre": "玄幻", "volume": "第一卷",
            "storyBible": {"mainline": "主线", "styleRules": {"chapterLength": 0}},
        })
        self.assertEqual(status, 201)
        nid = novel["id"]

        status, updated = _call("PATCH", base + "/api/novels/%s/story-bible" % nid,
                                {"storyBible": {"mainline": "改后主线"}})
        self.assertEqual(status, 200)
        self.assertEqual(updated["version"], 2)

        status, job = _call("POST", base + "/api/novels/%s/chapters/generate" % nid,
                            {"chapterNumber": 1})
        self.assertEqual(status, 202)
        status, again = _call("POST", base + "/api/novels/%s/chapters/generate" % nid,
                              {"chapterNumber": 1})
        self.assertEqual(status, 200)          # idempotent, not a second job
        self.assertEqual(again["id"], job["id"])

        # A rejected run config must not leave an orphan job behind.
        status, _ = _call("POST", base + "/api/novels/%s/chapters/generate" % nid,
                          {"chapterNumber": 2, "config": {"targetWords": "abc"}})
        self.assertEqual(status, 400)
        _, jobs = _call("GET", base + "/api/novels/%s/jobs" % nid)
        self.assertEqual(len(jobs), 1)

        status, light = _call("GET", base + "/api/novels/%s/chapters?light=1" % nid)
        self.assertEqual(status, 200)
        self.assertEqual(light[0]["content"], "")

        status, cancelled = _call("POST", base + "/api/jobs/%s/cancel" % job["id"])
        self.assertEqual(status, 200)
        self.assertEqual(cancelled["status"], "CANCELLED")

        _, metrics = _call("GET", base + "/api/ops/metrics")
        self.assertEqual(metrics["novels"]["total"], 1)
        self.assertGreaterEqual(metrics["chapters"]["total"], 1)
        _, series = _call("GET", base + "/api/ops/usage?days=7")
        self.assertEqual(len(series), 7)
        _, audit = _call("GET", base + "/api/ops/audit?limit=50")
        self.assertTrue(
            {"novel_created", "bible_updated"} <= {row["action"] for row in audit}
        )

        # ---- restart the real process against the same data directory ---------
        base = self._restart()

        status, listed = _call("GET", base + "/api/novels")
        self.assertEqual(status, 200)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], nid)
        self.assertEqual(listed[0]["story_bible_version"], 2)
        self.assertEqual(listed[0]["story_bible"]["mainline"], "改后主线")

        _, jobs = _call("GET", base + "/api/novels/%s/jobs" % nid)
        self.assertEqual(jobs[0]["status"], "CANCELLED")
        _, audit = _call("GET", base + "/api/ops/audit?limit=50")
        self.assertGreaterEqual(len(audit), 2)


if __name__ == "__main__":
    unittest.main()

