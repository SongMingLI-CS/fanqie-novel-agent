"""Hermetic HTTP/process tests for the server and worker entry points.

These spawn real subprocesses but never touch fixed ports or the repository
``.env``:

* ``NOVEL_PORT=0`` asks the server to bind an ephemeral port and print its
  actual address as ``NOVEL_AGENT_LISTENING host:port`` on stdout.
* ``NOVEL_ENV_FILE`` points at a non-existent file so ``load_env()`` has
  nothing to load (the repository ``.env`` holds real secrets/settings).
* ``NOVEL_DATA_DIR`` isolates each test's SQLite database in a temp dir.

This is what keeps the suite green even while a production server+worker are
running on 127.0.0.1:8787 in the same checkout.
"""
import http.client
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from novel_agent.store import Store

ROOT = Path(__file__).parents[1]


def _populated_env(data_dir):
    env = os.environ.copy()
    env["NOVEL_HOST"] = "127.0.0.1"
    env["NOVEL_PORT"] = "0"  # ephemeral -> server prints NOVEL_AGENT_LISTENING
    env["NOVEL_DATA_DIR"] = data_dir
    env["NOVEL_ENV_FILE"] = os.path.join(data_dir, "no-such.env")
    env["NOVEL_LOG_LEVEL"] = "INFO"
    env["NOVEL_LOG_FORMAT"] = "text"
    # Keep the test hermetic regardless of the invoking shell/CI environment.
    env.pop("NOVEL_AUTH_TOKEN", None)
    env.pop("DEEPSEEK_API_KEY", None)
    env.pop("NOVEL_WORKER_ONCE", None)
    return env


class ProcessOpsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _env(self):
        return _populated_env(self.tmp.name)

    def _cleanup_proc(self, proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _listen(self, proc, timeout=15):
        """Block until the server prints NOVEL_AGENT_LISTENING; return base URL."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                err = proc.stderr.read() if proc.stderr else ""
                self.fail(f"server exited early rc={proc.returncode} stderr={err!r}")
            line = proc.stdout.readline()
            if line:
                line = line.strip()
                if line.startswith("NOVEL_AGENT_LISTENING "):
                    return "http://" + line.split(" ", 1)[1]
        self.fail("server never announced its listening address")

    def _spawn_server(self, env=None):
        proc = subprocess.Popen(
            [sys.executable, "-m", "novel_agent.server"],
            cwd=ROOT,
            env=env or self._env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._cleanup_proc, proc)
        return proc, self._listen(proc)

    def _get(self, url):
        return json.loads(urllib.request.urlopen(url, timeout=5).read())

    def _api(self, base, method, path, data=None):
        body = None if data is None else json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            base + path,
            body,
            {"Content-Type": "application/json"} if body else {},
            method=method,
        )
        return json.loads(urllib.request.urlopen(req, timeout=5).read())

    # -- health ------------------------------------------------------------

    def test_healthz_and_readyz(self):
        proc, base = self._spawn_server()
        health = self._get(f"{base}/healthz")
        self.assertEqual(health["status"], "ok")
        self.assertIn("version", health["checks"])
        ready = self._get(f"{base}/readyz")
        self.assertEqual(ready["status"], "ok")
        self.assertEqual(ready["checks"]["store"], "ok")

    def test_frontend_served_from_static_dir(self):
        proc, base = self._spawn_server()
        body = urllib.request.urlopen(f"{base}/", timeout=5).read().decode("utf-8")
        self.assertIn("<!doctype html>", body)

    # -- API behaviour over HTTP (previously collided with the live 8787) --

    def test_create_and_idempotent_generate(self):
        proc, base = self._spawn_server()
        novel = self._api(base, "POST", "/api/novels", {"title": "HTTP测试", "storyBible": {"mainline": "主线"}})
        listed = self._api(base, "GET", "/api/novels")
        self.assertEqual(listed[0]["story_bible"]["mainline"], "主线")
        path = f"/api/novels/{novel['id']}/chapters/generate"
        first = self._api(base, "POST", path, {})
        second = self._api(base, "POST", path, {})
        self.assertEqual(first["id"], second["id"])

    def test_generate_409_when_previous_chapter_unconfirmed(self):
        # Serial-confirm gate: when the "next" number already holds a finished
        # but unpublished draft, generate must answer 409
        # confirm_previous_chapter_first instead of returning the stale
        # SUCCEEDED job.
        db = Path(self.tmp.name) / "novel.sqlite3"
        st = Store(db)
        nid = st.create_novel("串行测试", {"mainline": "主线"})["id"]
        st.create_job(nid, 1)
        ch = st.chapter(nid, 1)
        st.db.execute(
            "UPDATE chapters SET status='DRAFT_READY',title='已生成',content='正文' WHERE id=?",
            (ch["id"],),
        )
        st.db.execute(
            "UPDATE jobs SET status='SUCCEEDED' WHERE novel_id=? AND chapter_number=1",
            (nid,),
        )
        st.db.commit()
        st.close()

        proc, base = self._spawn_server()
        req = urllib.request.Request(
            f"{base}/api/novels/{nid}/chapters/generate",
            b"{}",
            {"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected 409 confirm_previous_chapter_first")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 409)
            body = json.loads(exc.read().decode("utf-8"))
            self.assertEqual(body["code"], "conflict")
            self.assertEqual(body["message"], "confirm_previous_chapter_first")
            self.assertEqual(body["details"]["chapterNumber"], 1)

    # -- ops endpoints ------------------------------------------------------

    def test_ops_metrics_usage_audit_endpoints(self):
        proc, base = self._spawn_server()
        self._api(base, "POST", "/api/novels", {"title": "运维HTTP", "storyBible": {"mainline": "主线"}})

        metrics = self._get(f"{base}/api/ops/metrics")
        self.assertEqual(metrics["novels"]["total"], 1)
        self.assertGreaterEqual(metrics["auditEvents"], 1)
        self.assertIn("generatedAt", metrics)

        usage = self._get(f"{base}/api/ops/usage?days=3")
        self.assertIsInstance(usage, list)
        self.assertEqual(len(usage), 3)

        audit = self._get(f"{base}/api/ops/audit?limit=10")
        self.assertTrue(any(e["action"] == "novel_created" for e in audit))
        # Newest event is listed first.
        self.assertEqual(audit[0]["action"], "novel_created")

    # -- security ------------------------------------------------------------

    def _authed_env(self, token):
        env = self._env()
        env["NOVEL_AUTH_TOKEN"] = token
        return env

    def test_health_and_static_stay_open_when_api_requires_token(self):
        proc, base = self._spawn_server(self._authed_env("t0p-s3cret"))
        # Health and the frontend are deliberately public.
        self.assertEqual(self._get(f"{base}/healthz")["status"], "ok")
        body = urllib.request.urlopen(f"{base}/", timeout=5).read().decode("utf-8")
        self.assertIn("<!doctype html>", body)

    def test_api_requires_bearer_token_when_configured(self):
        proc, base = self._spawn_server(self._authed_env("t0p-s3cret"))
        try:
            urllib.request.urlopen(f"{base}/api/novels", timeout=5)
            self.fail("expected 401 without a token")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 401)
            self.assertEqual(json.loads(exc.read())["code"], "unauthorized")

        req = urllib.request.Request(
            f"{base}/api/novels", headers={"Authorization": "Bearer wrong"}
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected 401 for a wrong token")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 401)

        good = urllib.request.Request(
            f"{base}/api/novels", headers={"Authorization": "Bearer t0p-s3cret"}
        )
        listed = json.loads(urllib.request.urlopen(good, timeout=5).read())
        self.assertIsInstance(listed, list)

    def test_oversized_body_is_rejected_with_413(self):
        proc, base = self._spawn_server()
        # Announce an over-cap Content-Length without streaming 1MB+ on the wire:
        # the server must reject from the header before it would read the body.
        hostport = base[len("http://"):]
        host, _, port = hostport.partition(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=5)
        try:
            conn.request(
                "POST",
                "/api/novels",
                body=b"",
                headers={"Content-Type": "application/json", "Content-Length": "2000000"},
            )
            resp = conn.getresponse()
            self.assertEqual(resp.status, 413)
            payload = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(payload["code"], "payload_too_large")
        finally:
            conn.close()

    def test_security_headers_sent_on_json_and_static(self):
        proc, base = self._spawn_server()
        for path in ("/healthz", "/"):
            resp = urllib.request.urlopen(base + path, timeout=5)
            headers = resp.headers
            self.assertEqual(headers.get("Cache-Control"), "no-store")
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(headers.get("X-Frame-Options"), "DENY")

    def test_path_traversal_never_leaks_files_outside_static_dir(self):
        proc, base = self._spawn_server()
        for path in (
            "/%2e%2e/README.md",
            "/%2e%2e/%2e%2e/%2e%2e/etc/passwd",
            "/..%2f..%2fnovel.sqlite3",
            "/.%2e/.%2e/README.md",
        ):
            try:
                urllib.request.urlopen(base + path, timeout=5)
                self.fail(f"expected 404 for traversal path {path!r}")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 404, f"unexpected status for {path!r}")

    # -- graceful shutdown -------------------------------------------------

    @unittest.skipUnless(
        os.name == "posix",
        "graceful SIGTERM delivery only exists on POSIX (Windows send_signal(SIGTERM) force-terminates)",
    )
    def test_server_sigterm_shuts_down_cleanly(self):
        proc, base = self._spawn_server()
        self._get(f"{base}/healthz")
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=10)
        self.assertEqual(rc, 0, "SIGTERM should trigger a graceful exit (rc=0)")

    @unittest.skipUnless(
        os.name == "posix",
        "graceful SIGTERM delivery only exists on POSIX (Windows send_signal(SIGTERM) force-terminates)",
    )
    def test_worker_sigterm_shuts_down_cleanly(self):
        # An idle worker on an empty queue must drain promptly on SIGTERM.
        proc = subprocess.Popen(
            [sys.executable, "-m", "novel_agent.worker"],
            cwd=ROOT,
            env=self._env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._cleanup_proc, proc)
        deadline = time.monotonic() + 15
        started = False
        while time.monotonic() < deadline:
            line = proc.stderr.readline()
            if "novel agent worker started" in line:
                started = True
                break
            if not line and proc.poll() is not None:
                break
        self.assertTrue(started, "worker never logged its started banner")
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=10)
        self.assertEqual(rc, 0, "SIGTERM should trigger a graceful worker exit (rc=0)")


if __name__ == "__main__":
    unittest.main()
