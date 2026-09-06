import json
import logging
import os
import signal
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .auth import authorize
from .config import Config
from .deepseek import DeepSeekClient
from .envfile import load_env
from .exporters import export_chapter
from .logutil import setup_logging
from .reviewer import review
from .service import NovelService
from .store import Store

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
EXPORT_FORMATS = ("txt", "md", "json")
MAX_BODY = 1_000_000  # 1 MB request-body cap for JSON endpoints.

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}

# Filled in by ``main()`` so importing this module has no side effects (no DB
# file is opened, no Config is baked at import time).
config = None
store = None
service = None
STATIC_DIR = ROOT / "static"

_STARTED = time.monotonic()


class ApiError(Exception):
    """Carries an HTTP status and the stable machine-readable error code."""

    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def _conflict_message(message):
    # Stable signals that indicate a state conflict rather than a malformed request.
    return message in (
        "novel_is_paused",
        "chapter_must_be_exported_before_manual_publish",
        "chapter_cannot_be_approved",
        "chapter_not_ready_for_export",
        "chapter_already_published",
        "only_latest_draft_can_be_rewritten",
        "chapter_busy",
    )


def resolve_static_dir(config_value):
    """Locate the frontend directory.

    Resolution order:
      1. ``NOVEL_STATIC_DIR`` from config (when the package is pip-installed and
         no source tree is available operators point this at a checkout/volume).
      2. ``<repo>/static`` when running from a source checkout.
      3. ``novel_agent/static`` (bundled installs).
    Falls back to the repo path (which simply 404s if absent).
    """
    if config_value:
        return Path(config_value)
    for candidate in (ROOT / "static", Path(__file__).resolve().parent / "static"):
        if candidate.is_dir():
            return candidate
    return ROOT / "static"


class Handler(BaseHTTPRequestHandler):
    server_version = f"NovelAgent/{__version__}"

    # -- plumbing ------------------------------------------------------------

    def _security_headers(self):
        """Cheap hardening headers applied to every response (UI and JSON)."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def _reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self._resp_status = status
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, status, code, message, details=None):
        self._reply(status, {"code": code, "message": message, "details": details or {}})

    def _handle_error(self, exc):
        if isinstance(exc, ApiError):
            self._error(exc.status, exc.code, exc.message, exc.details)
            return
        if isinstance(exc, (ValueError, KeyError, TypeError)):
            message = str(exc) or exc.__class__.__name__
            if _conflict_message(message):
                self._error(409, "conflict", message)
            else:
                self._error(400, "invalid_request", message)
            return
        if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower():
            self._error(503, "db_locked", "Database is busy, please retry")
            return
        logger.exception("request failed: %s %s", self.command, self.path)
        self._error(500, "internal_error", "Request failed")

    def _read_body(self):
        length = self.headers.get("Content-Length")
        if not length:
            return {}
        try:
            size = int(length)
        except (TypeError, ValueError):
            raise ApiError(400, "invalid_request", "invalid_content_length")
        if size < 0:
            raise ApiError(400, "invalid_request", "invalid_content_length")
        if size > MAX_BODY:
            raise ApiError(413, "payload_too_large", "Request body too large")
        raw = self.rfile.read(size)
        if not raw or not raw.strip():
            return {}
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ApiError(400, "invalid_request", "body_must_be_utf8")
        try:
            value = json.loads(decoded)
        except ValueError:
            raise ApiError(400, "invalid_request", "invalid_json_body")
        return value if isinstance(value, dict) else {}

    def _authorized(self):
        if not authorize(self, config):
            self._error(401, "unauthorized", "Authentication required")
            return False
        return True

    def _novel_or_404(self, nid):
        novel = store.get_novel(nid)
        if novel is None:
            raise ApiError(404, "not_found", "Novel not found")
        return novel

    def _int_query(self, name, default, max_value):
        """Read a bounded positive integer from the query string."""
        try:
            value = int(parse_qs(urlparse(self.path).query).get(name, [default])[0])
        except (TypeError, ValueError, IndexError):
            value = default
        return max(1, min(value, max_value))

    def _log_request(self, start):
        """One structured line per HTTP request (status + timing)."""
        status = getattr(self, "_resp_status", 500)
        path = urlparse(self.path).path
        duration_ms = round((time.monotonic() - start) * 1000)
        logger.info(
            "http method=%s path=%s status=%s duration_ms=%s",
            self.command, path, status, duration_ms,
            extra={"fields": {"method": self.command, "path": path, "status": status, "duration_ms": duration_ms}},
        )

    # -- health --------------------------------------------------------------

    def _health(self, path):
        checks = {"version": __version__, "uptime_s": round(time.monotonic() - _STARTED)}
        if path == "/readyz":
            try:
                store.db.execute("SELECT 1").fetchone()
            except Exception:  # noqa: BLE001 - readiness must not 500
                logger.exception("readiness db check failed")
                self._error(503, "unavailable", "store_unavailable")
                return
            checks["store"] = "ok"
        return self._reply(200, {"status": "ok", "checks": checks})

    # -- dispatch ------------------------------------------------------------

    def do_GET(self):
        start = time.monotonic()
        try:
            path = urlparse(self.path).path
            if path in ("/healthz", "/readyz"):
                return self._health(path)
            if path.startswith("/api/"):
                if not self._authorized():
                    return
                return self._route_get(path.strip("/").split("/"))
            return self._serve_static(path)
        except Exception as exc:  # noqa: BLE001 - unified error envelope
            self._handle_error(exc)
        finally:
            self._log_request(start)
            store.release_thread()

    def do_POST(self):
        start = time.monotonic()
        try:
            path = urlparse(self.path).path
            if path.startswith("/api/"):
                if not self._authorized():
                    return
                data = self._read_body()
                return self._route_post(path.strip("/").split("/"), data)
            return self._serve_static(path)
        except Exception as exc:  # noqa: BLE001
            self._handle_error(exc)
        finally:
            self._log_request(start)
            store.release_thread()

    def do_PATCH(self):
        start = time.monotonic()
        try:
            path = urlparse(self.path).path
            if path.startswith("/api/"):
                if not self._authorized():
                    return
                data = self._read_body()
                return self._route_patch(path.strip("/").split("/"), data)
            return self._serve_static(path)
        except Exception as exc:  # noqa: BLE001
            self._handle_error(exc)
        finally:
            self._log_request(start)
            store.release_thread()

    # -- static files --------------------------------------------------------

    def _serve_static(self, path):
        rel = unquote(path.lstrip("/"))
        if "\x00" in rel:
            self._error(404, "not_found", "File not found")
            return
        if not rel or rel.endswith("/"):
            rel = "index.html"
        base = STATIC_DIR.resolve()
        target = (base / rel).resolve()
        if target != base and base not in target.parents:
            self._error(404, "not_found", "File not found")
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._error(404, "not_found", "File not found")
            return
        body = target.read_bytes()
        ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self._resp_status = 200
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- GET routes ----------------------------------------------------------

    def _route_get(self, parts):
        if parts == ["api", "novels"]:
            return self._reply(200, store.novels())
        if len(parts) == 3 and parts[:2] == ["api", "novels"]:
            return self._reply(200, self._novel_or_404(parts[2]))
        if len(parts) == 4 and parts[:2] == ["api", "novels"] and parts[3] == "chapters":
            self._novel_or_404(parts[2])
            return self._reply(200, store.chapters(parts[2]))
        if len(parts) == 4 and parts[:2] == ["api", "novels"] and parts[3] == "jobs":
            self._novel_or_404(parts[2])
            return self._reply(200, store.jobs(parts[2]))
        if len(parts) == 4 and parts[:2] == ["api", "novels"] and parts[3] == "usage":
            self._novel_or_404(parts[2])
            return self._reply(200, store.usage(parts[2]))
        if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
            job = store.get_job(parts[2])
            if job is None:
                raise ApiError(404, "not_found", "Job not found")
            return self._reply(200, job)
        if len(parts) == 3 and parts[:2] == ["api", "chapters"]:
            chapter = store.chapter_by_id(parts[2])
            if chapter is None:
                raise ApiError(404, "not_found", "Chapter not found")
            return self._reply(200, chapter)
        # Ops observability (auth-protected like the rest of /api/*).
        if parts[:2] == ["api", "ops"] and parts[2:] == ["metrics"]:
            return self._reply(200, store.metrics())
        if parts[:2] == ["api", "ops"] and parts[2:] == ["usage"]:
            return self._reply(200, store.usage_series(self._int_query("days", 7, 90)))
        if parts[:2] == ["api", "ops"] and parts[2:] == ["audit"]:
            return self._reply(200, store.audit_trail(self._int_query("limit", 200, 1000)))
        raise ApiError(404, "not_found", "Route not found")

    # -- POST routes ---------------------------------------------------------

    def _assert_chapter_generatable(self, novel, number):
        """Serial-confirm gate for the generate/continue routes.

        A number may only be queued when nothing is already blocking it. If a
        finished but still-unpublished draft (待人工批准 / 已批准待导出 / 已导出)
        already occupies this number, re-generating would only idempotently return
        its old SUCCEEDED job and look like it is "stuck in the queue" forever.
        The user must first push that chapter through 批准 -> 导出 -> 已人工发布 so
        ``current_chapter`` advances, after which the real next number becomes
        generatable.
        """
        if int(novel.get("current_chapter") or 0) >= number:
            return  # number already published; not the serial next step
        ch = store.chapter(novel["id"], number)
        if ch and ch.get("content") and ch.get("status") in (
            "WAITING_APPROVAL",
            "DRAFT_READY",
            "EXPORTED",
        ):
            raise ApiError(
                409,
                "conflict",
                "confirm_previous_chapter_first",
                {"chapterNumber": number, "currentChapter": novel["current_chapter"]},
            )

    def _route_post(self, parts, data):
        # POST /api/novels  ->  create novel
        if parts == ["api", "novels"]:
            title = str(data.get("title", "")).strip()
            if not title:
                raise ApiError(400, "invalid_request", "title_required")
            bible = data.get("storyBible")
            if bible is not None and not isinstance(bible, dict):
                raise ApiError(400, "invalid_request", "storyBible_must_be_an_object")
            novel = store.create_novel(
                title,
                bible or {},
                str(data.get("genre", "")),
                str(data.get("volume", "")),
            )
            return self._reply(201, novel)

        # POST /api/novels/<id>/chapters/generate
        if len(parts) == 5 and parts[:2] == ["api", "novels"] and parts[3] == "chapters" and parts[4] == "generate":
            novel = self._novel_or_404(parts[2])
            if novel["paused"]:
                raise ApiError(409, "conflict", "novel_is_paused")
            chapter_number = data.get("chapterNumber")
            try:
                number = int(chapter_number) if chapter_number not in (None, "") else novel["current_chapter"] + 1
            except (TypeError, ValueError):
                raise ApiError(400, "invalid_request", "chapter_number_must_be_a_positive_integer")
            if number < 1:
                raise ApiError(400, "invalid_request", "chapter_number_must_be_positive")
            self._assert_chapter_generatable(novel, number)
            job, created = store.create_job(parts[2], number)
            return self._reply(202 if created else 200, job)

        # POST /api/jobs/<id>/cancel
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel":
            if store.get_job(parts[2]) is None:
                raise ApiError(404, "not_found", "Job not found")
            store.cancel_job(parts[2])
            return self._reply(200, store.get_job(parts[2]))

        # POST /api/novels/<id>/continue
        if len(parts) == 4 and parts[:2] == ["api", "novels"] and parts[3] == "continue":
            novel = self._novel_or_404(parts[2])
            store.set_paused(parts[2], False)
            self._assert_chapter_generatable(novel, novel["current_chapter"] + 1)
            job, created = store.create_job(parts[2], novel["current_chapter"] + 1)
            try:
                count = max(1, int(data.get("count", 1)))
            except (TypeError, ValueError):
                count = 1
            return self._reply(
                202,
                {
                    "job": job,
                    "created": created,
                    "requestedCount": count,
                    "note": "每章人工确认后再次调用 continue 才会创建下一章",
                },
            )

        # POST /api/chapters/<id>/export
        if len(parts) == 4 and parts[:2] == ["api", "chapters"] and parts[3] == "export":
            return self._export_chapter(data)

        # POST /api/chapters/<id>/publish | /manual-publish
        if len(parts) == 4 and parts[:2] == ["api", "chapters"] and parts[3] in ("publish", "manual-publish"):
            platform = str(data.get("platform", "")).strip()
            operator = str(data.get("operator", "")).strip()
            if not platform or not operator:
                raise ApiError(400, "invalid_request", "platform_and_operator_required")
            nid = str(data.get("novelId", ""))
            try:
                number = int(data.get("chapterNumber"))
            except (TypeError, ValueError):
                raise ApiError(400, "invalid_request", "chapterNumber_must_be_an_integer")
            if store.chapter(nid, number) is None:
                raise ApiError(404, "not_found", "Chapter not found")
            store.manual_publish(nid, number, data)
            return self._reply(200, store.chapter(nid, number))

        # POST /api/chapters/<id>/review
        if len(parts) == 4 and parts[:2] == ["api", "chapters"] and parts[3] == "review":
            chapter = store.chapter_by_id(parts[2])
            if chapter is None:
                raise ApiError(404, "not_found", "Chapter not found")
            novel = store.get_novel(chapter["novel_id"])
            result = review(
                chapter,
                (novel or {}).get("story_bible", {}),
                store.recent(chapter["novel_id"]),
            )
            store.record_review(parts[2], result)
            return self._reply(200, result)

        # POST /api/chapters/<id>/approve
        if len(parts) == 4 and parts[:2] == ["api", "chapters"] and parts[3] == "approve":
            chapter = store.chapter_by_id(parts[2])
            if chapter is None:
                raise ApiError(404, "not_found", "Chapter not found")
            if chapter.get("review", {}).get("blockingIssues"):
                raise ApiError(409, "conflict", "chapter_cannot_be_approved")
            store.set_status(chapter["novel_id"], chapter["number"], "DRAFT_READY")
            return self._reply(200, store.chapter_by_id(parts[2]))

        # POST /api/chapters/<id>/rewrite
        # Serial overwrite rewrite: delete the newest unpublished chapter and
        # re-queue its number to be regenerated from the Story Bible.
        if len(parts) == 4 and parts[:2] == ["api", "chapters"] and parts[3] == "rewrite":
            if store.chapter_by_id(parts[2]) is None:
                raise ApiError(404, "not_found", "Chapter not found")
            job = store.rewrite_chapter(parts[2])
            return self._reply(
                202,
                {"job": job, "chapterNumber": job["chapter_number"], "deleted": True},
            )

        # POST /api/novels/<id>/pause
        if len(parts) == 4 and parts[:2] == ["api", "novels"] and parts[3] == "pause":
            self._novel_or_404(parts[2])
            store.set_paused(parts[2], True)
            return self._reply(200, store.get_novel(parts[2]))

        raise ApiError(404, "not_found", "Route not found")

    def _export_chapter(self, data):
        nid = str(data.get("novelId", ""))
        try:
            number = int(data.get("chapterNumber"))
        except (TypeError, ValueError):
            raise ApiError(400, "invalid_request", "chapterNumber_must_be_an_integer")
        novel = store.get_novel(nid)
        chapter = store.chapter(nid, number) if novel else None
        if not novel or chapter is None:
            raise ApiError(404, "not_found", "Chapter not found")
        fmt = str(data.get("format", "txt"))
        if fmt not in EXPORT_FORMATS:
            raise ApiError(400, "invalid_request", "unsupported_export_format")

        # Readiness check FIRST so we never leave an orphan export_job row behind.
        ready = bool(
            chapter.get("review", {}).get("passed")
            and chapter.get("status") in ("DRAFT_READY", "WAITING_APPROVAL", "EXPORTED")
        )
        if not ready:
            raise ApiError(409, "conflict", "chapter_not_ready_for_export")

        existing = store.export_job(chapter, fmt)
        if existing and existing["status"] == "SUCCEEDED":
            return self._reply(
                200,
                {"path": existing["path"], "status": "EXPORTED", "idempotent": True},
            )
        export_job, _created = store.create_export_job(chapter, fmt)
        path = export_chapter(chapter, novel, fmt, config.data_dir / "exports")
        store.complete_export_job(export_job, path)
        store.record_export(nid, chapter["number"])
        return self._reply(
            200,
            {
                "path": str(path),
                "status": "EXPORTED",
                "idempotent": chapter["status"] == "EXPORTED",
            },
        )

    # -- PATCH routes --------------------------------------------------------

    def _route_patch(self, parts, data):
        # PATCH /api/novels/<id>/story-bible
        if len(parts) == 4 and parts[:2] == ["api", "novels"] and parts[3] == "story-bible":
            self._novel_or_404(parts[2])
            bible = data.get("storyBible", data)
            if not isinstance(bible, dict):
                raise ApiError(400, "invalid_request", "storyBible_must_be_an_object")
            version = store.update_bible(parts[2], bible)
            return self._reply(200, {"version": version, "novel": store.get_novel(parts[2])})

        # PATCH /api/chapters/<id>
        if len(parts) == 3 and parts[:2] == ["api", "chapters"]:
            if store.chapter_by_id(parts[2]) is None:
                raise ApiError(404, "not_found", "Chapter not found")
            return self._reply(200, store.update_draft(parts[2], data))

        raise ApiError(404, "not_found", "Route not found")

    def log_message(self, *args):  # request logging is handled by _log_request
        pass


def main():
    global config, store, service, STATIC_DIR

    load_env()  # .env is loaded before Config() so values are picked up.
    config = Config()  # raises ConfigError on malformed settings
    setup_logging(config.log_level, config.log_format)
    STATIC_DIR = resolve_static_dir(config.static_dir)

    store = Store(config.data_dir / "novel.sqlite3")
    service = NovelService(store, DeepSeekClient(config), config, ROOT)

    host, port = config.host, config.port
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        logger.error("failed to bind %s:%s error=%s", host, port, exc)
        raise SystemExit(1) from exc
    actual_host, actual_port = httpd.server_address[:2]
    logger.info(
        "novel agent server listening on http://%s:%s version=%s static=%s",
        actual_host, actual_port, __version__, STATIC_DIR,
    )
    if config.port == 0:
        # Ephemeral port: print the bound address so launch scripts/tests can
        # discover it without guessing.
        print(f"NOVEL_AGENT_LISTENING {actual_host}:{actual_port}", flush=True)

    stop = threading.Event()

    def _shutdown(signum, frame):
        logger.info("received signal %s, draining http server", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    thread = threading.Thread(target=httpd.serve_forever, name="novel-httpd", daemon=True)
    thread.start()
    while not stop.wait(0.2):
        if not thread.is_alive():
            break
    logger.info("stopping http server")
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)
    store.release_thread()
    logger.info("novel agent server stopped")


if __name__ == "__main__":
    main()
