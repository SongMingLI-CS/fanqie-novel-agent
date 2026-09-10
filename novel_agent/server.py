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
from .checkpoints import CheckpointRepository
from .config import Config
from .deepseek import DeepSeekClient
from .envfile import load_env
from .events import EventRepository
from .exporters import EXPORT_FORMATS, export_book, export_chapter
from .logutil import setup_logging
from .reviewer import chapter_as_output, review
from .service import NovelService
from .store import Store

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
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

# Long-lived SSE connections must neither block nor be joined at process exit;
# daemon_threads + a shared stop flag let graceful shutdown interrupt them.
_STOP_EVENT = threading.Event()


class NovelHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


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
        "chapter_is_terminal",
    )


_API_CHAPTER_STRIP = ("raw_response", "proposed_state")


def _public_chapter(chapter):
    """Drop heavy internal-only fields from chapter JSON sent to the browser."""
    if not isinstance(chapter, dict):
        return chapter
    return {k: v for k, v in chapter.items() if k not in _API_CHAPTER_STRIP}


def _sanitize_run_config(data):
    """Validate an optional per-run generation ``config`` object.

    Returns a normalised dict, or ``None`` when no config was supplied. Rejects
    malformed values with a 400 so the worker never sees a half-baked plan.
    """
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_request", "config_must_be_an_object")
    result = {}
    if "stages" in data:
        if not isinstance(data["stages"], list):
            raise ApiError(400, "invalid_request", "stages_must_be_an_array")
        known = ("outline", "chapter", "polish")
        stages = [s for s in data["stages"] if s in known]
        if stages and "chapter" not in stages:
            stages = ["chapter"]
        result["stages"] = stages
    if "targetWords" in data:
        try:
            result["targetWords"] = max(0, int(data["targetWords"]))
        except (TypeError, ValueError):
            raise ApiError(400, "invalid_request", "targetWords_must_be_an_integer")
    if "autoExportTxt" in data:
        if not isinstance(data["autoExportTxt"], bool):
            raise ApiError(400, "invalid_request", "autoExportTxt_must_be_a_boolean")
        result["autoExportTxt"] = data["autoExportTxt"]
    return result or None


def _bible_chapter_length(bible):
    """Resolve the StoryBible's target chapter length (0 when unset/malformed)."""
    rules = bible.get("styleRules") if isinstance(bible, dict) else None
    if not isinstance(rules, dict):
        return 0
    value = rules.get("chapterLength", 0)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


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

    # -- server-sent events --------------------------------------------------

    def _stream_events(self, novel_id, since):
        """Hold one long-lived ``text/event-stream`` connection open.

        The worker (a separate process) writes progress into the ``events``
        table; this endpoint polls rows ``id > since`` and flushes them as SSE
        frames, so the browser keeps one connection and never misses a frame
        even across a reconnect (it just resumes from the last ``id`` it saw).
        """
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self._security_headers()
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return
        self._resp_status = 200
        repo = EventRepository(store)
        try:
            cursor = int(since or 0)
        except (TypeError, ValueError):
            cursor = 0
        last_beat = time.monotonic()
        idle_wait = 0.2
        try:
            while not _STOP_EVENT.is_set():
                rows = repo.read_since(novel_id, cursor, limit=100)
                for event in rows:
                    payload = json.dumps(event, ensure_ascii=False)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    cursor = int(event["id"])
                if rows:
                    self.wfile.flush()
                    last_beat = time.monotonic()
                    idle_wait = 0.2
                    continue
                if time.monotonic() - last_beat >= 15:
                    # Keep-alive comment so proxies do not idle the connection.
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_beat = time.monotonic()
                    continue
                # Adaptive idle backoff: fast while events flow, slow when quiet.
                _STOP_EVENT.wait(idle_wait)
                idle_wait = min(1.5, idle_wait + 0.2)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass  # client went away / server is shutting down

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
                if path == "/api/events":
                    query = parse_qs(urlparse(self.path).query)
                    novel_id = (query.get("novel_id") or [""])[0]
                    if not novel_id:
                        self._error(400, "invalid_request", "novel_id_required")
                        return
                    return self._stream_events(
                        novel_id, (query.get("since") or ["0"])[0]
                    )
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
            query = parse_qs(urlparse(self.path).query)
            # ``?light=1`` omits prose bodies so the console can poll the chapter
            # list cheaply on long novels (each body is fetched per chapter).
            # ``?q=<text>`` performs a real server-side search and ``?limit=N``
            # bounds the window, so the client never has to download and filter
            # the whole novel.
            light = (query.get("light") or ["0"])[0] in ("1", "true", "yes")
            search = (query.get("q") or [""])[0]
            raw_limit = (query.get("limit") or [None])[0]
            limit = None
            if raw_limit is not None:
                try:
                    limit = max(1, min(int(raw_limit), 5000))
                except (TypeError, ValueError):
                    raise ApiError(400, "invalid_request", "limit_must_be_an_integer")
            if light or search or limit is not None:
                return self._reply(200, [
                    _public_chapter(c)
                    for c in store.chapter_summaries(parts[2], query=search, limit=limit)
                ])
            return self._reply(200, [_public_chapter(c) for c in store.chapters(parts[2])])
        if len(parts) == 4 and parts[:2] == ["api", "novels"] and parts[3] == "jobs":
            self._novel_or_404(parts[2])
            query = parse_qs(urlparse(self.path).query)
            status = (query.get("status") or [None])[0]
            limit = None
            raw_limit = (query.get("limit") or [None])[0]
            if raw_limit is not None:
                try:
                    limit = max(1, min(int(raw_limit), 500))
                except (TypeError, ValueError):
                    raise ApiError(400, "invalid_request", "limit_must_be_an_integer")
            return self._reply(200, store.jobs(parts[2], status=status, limit=limit))
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
            return self._reply(200, _public_chapter(chapter))
        # Ops observability (auth-protected like the rest of /api/*).
        if parts[:2] == ["api", "ops"] and parts[2:] == ["metrics"]:
            return self._reply(200, store.metrics())
        if parts[:2] == ["api", "ops"] and parts[2:] == ["usage"]:
            return self._reply(200, store.usage_series(self._int_query("days", 7, 90)))
        if parts[:2] == ["api", "ops"] and parts[2:] == ["audit"]:
            query = parse_qs(urlparse(self.path).query)
            return self._reply(
                200,
                store.audit_trail(
                    self._int_query("limit", 200, 1000),
                    action=(query.get("action") or [None])[0],
                    novel_id=(query.get("novel_id") or [None])[0],
                ),
            )
        if len(parts) == 5 and parts[:2] == ["api", "novels"] and parts[4] == "runs":
            self._novel_or_404(parts[2])
            run = CheckpointRepository(store).latest_run_for_novel(parts[2])
            return self._reply(200, {"run": run})
        if len(parts) == 4 and parts[:2] == ["api", "chapters"] and parts[3] == "history":
            if store.chapter_by_id(parts[2]) is None:
                raise ApiError(404, "not_found", "Chapter not found")
            return self._reply(200, {"versions": store.draft_history(parts[2])})
        if (
            len(parts) == 6
            and parts[:2] == ["api", "novels"]
            and parts[3] == "chapters"
            and parts[5] == "timeline"
        ):
            self._novel_or_404(parts[2])
            chapter = store.chapter(parts[2], int(parts[4]))
            if chapter is None:
                raise ApiError(404, "not_found", "Chapter not found")
            events = EventRepository(store).chapter_events(
                parts[2], int(parts[4]), since=0,
                limit=self._int_query("limit", 5000, 10000),
            )
            return self._reply(200, {"chapter": int(parts[4]), "events": events})
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
            # Validate the optional run config *before* creating the job: a
            # rejected config must not leave an orphan PENDING job behind that a
            # worker would then silently start with default settings.
            run_config = _sanitize_run_config(data.get("config"))
            job, created = store.create_job(parts[2], number)
            if run_config:
                job = store.set_job_config(job["id"], run_config)
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
            run_config = _sanitize_run_config(data.get("config"))
            job, created = store.create_job(parts[2], novel["current_chapter"] + 1)
            if run_config:
                job = store.set_job_config(job["id"], run_config)
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
            return self._reply(200, _public_chapter(store.chapter(nid, number)))

        # POST /api/chapters/<id>/review
        if len(parts) == 4 and parts[:2] == ["api", "chapters"] and parts[3] == "review":
            chapter = store.chapter_by_id(parts[2])
            if chapter is None:
                raise ApiError(404, "not_found", "Chapter not found")
            novel = store.get_novel(chapter["novel_id"])
            bible = (novel or {}).get("story_bible", {})
            result = review(
                # Translate the persisted (snake_case) row into the model-output
                # contract the reviewer reads, otherwise every manual re-review
                # reported a bogus ``missing_chapter_goal`` blocking issue.
                chapter_as_output(chapter),
                bible,
                # Exclude the chapter under review (and later chapters) from the
                # "recent context" window, otherwise its own prose is reported
                # as recent_chapter_overlap.
                store.recent(chapter["novel_id"], before=chapter["number"]),
                _bible_chapter_length(bible),
            )
            store.record_review(parts[2], result)
            return self._reply(200, result)

        # POST /api/chapters/<id>/approve
        if len(parts) == 4 and parts[:2] == ["api", "chapters"] and parts[3] == "approve":
            chapter = store.chapter_by_id(parts[2])
            if chapter is None:
                raise ApiError(404, "not_found", "Chapter not found")
            # Server-side gate: approving is only meaningful for a chapter whose
            # latest review actually passed. Relying on the UI alone would let a
            # hand-crafted request approve an unreviewed/edited draft.
            current_review = chapter.get("review") or {}
            if not current_review.get("passed") or current_review.get("blockingIssues"):
                raise ApiError(409, "conflict", "chapter_cannot_be_approved")
            store.set_status(chapter["novel_id"], chapter["number"], "DRAFT_READY")
            return self._reply(200, _public_chapter(store.chapter_by_id(parts[2])))

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

        # POST /api/novels/<id>/export-book -> complete-book txt/md
        if len(parts) == 4 and parts[:2] == ["api", "novels"] and parts[3] == "export-book":
            return self._export_book(parts[2], data)

        # POST /api/chapters/<id>/rollback  -> restore an older draft version
        if len(parts) == 4 and parts[:2] == ["api", "chapters"] and parts[3] == "rollback":
            return self._rollback_chapter(parts[2], data)

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
        try:
            path = export_chapter(chapter, novel, fmt, config.data_dir / "exports")
        except ValueError as exc:
            # exporters re-checks the review gate; keep the failure visible.
            store.fail_export_job(export_job, f"ValueError: {exc}")
            raise ApiError(409, "conflict", "chapter_not_ready_for_export") from exc
        except OSError as exc:
            # Persist the reason (instead of leaving the job PENDING forever) and
            # return a stable error code rather than a bare 500.
            store.fail_export_job(export_job, f"{type(exc).__name__}: {exc}")
            logger.warning(
                "export failed novel=%s chapter=%s format=%s error=%s",
                nid, chapter["number"], fmt, type(exc).__name__,
            )
            raise ApiError(500, "export_failed", "chapter_export_failed") from exc
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

    def _export_book(self, nid, data):
        """POST /api/novels/<id>/export-book: one complete-book file (txt/md)."""
        novel = self._novel_or_404(nid)
        fmt = str(data.get("format", "txt"))
        if fmt not in ("txt", "md"):
            raise ApiError(400, "invalid_request", "unsupported_book_export_format")
        chapters = store.published_chapters(nid)
        if not chapters:
            raise ApiError(409, "conflict", "no_published_chapters")
        try:
            path = export_book(novel, chapters, fmt, config.data_dir / "exports")
        except OSError as exc:
            logger.warning(
                "book export failed novel=%s format=%s error=%s",
                nid, fmt, type(exc).__name__,
            )
            raise ApiError(500, "export_failed", "book_export_failed") from exc
        store.record_audit(nid, "book_export", {
            "format": fmt, "chapters": len(chapters), "path": str(path),
        })
        return self._reply(200, {
            "path": str(path), "format": fmt, "chapters": len(chapters),
        })

    def _rollback_chapter(self, cid, data):
        """Restore an older draft version as a NEW version (history preserved).

        Publishing a chapter is the point of no return for its body text, so a
        published chapter cannot be rolled back.
        """
        chapter = store.chapter_by_id(cid)
        if chapter is None:
            raise ApiError(404, "not_found", "Chapter not found")
        if chapter.get("status") == "PUBLISHED_MANUALLY":
            raise ApiError(409, "conflict", "chapter_already_published")
        try:
            version = int(data.get("version"))
        except (TypeError, ValueError):
            raise ApiError(400, "invalid_request", "version_must_be_an_integer")
        payload = store.draft_version(cid, version)
        if payload is None:
            raise ApiError(404, "not_found", "draft_version_not_found")
        changes = {}
        for key, alias in (
            ("title", None), ("content", None), ("summary", None),
            ("goal", "chapterGoal"), ("hook", "nextChapterHook"),
        ):
            value = payload.get(key) if alias is None else (
                payload.get(key) or payload.get(alias)
            )
            if value is not None:
                changes[key] = value
        if not changes:
            raise ApiError(400, "invalid_request", "draft_version_has_no_editable_fields")
        store.record_audit(chapter["novel_id"], "chapter_rollback", {
            "chapter": chapter["number"], "version": version,
        })
        return self._reply(200, _public_chapter(store.update_draft(cid, changes)))

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
            return self._reply(200, _public_chapter(store.update_draft(parts[2], data)))

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

    # Housekeeping: drop stale live events on startup (best-effort).
    try:
        EventRepository(store).prune(config.event_ttl_days)
    except Exception:  # noqa: BLE001
        logger.debug("server event prune skipped", exc_info=True)

    host, port = config.host, config.port
    try:
        httpd = NovelHTTPServer((host, port), Handler)
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
        _STOP_EVENT.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGBREAK"):  # Windows console CTRL+BREAK -> graceful stop
        signal.signal(signal.SIGBREAK, _shutdown)

    thread = threading.Thread(target=httpd.serve_forever, name="novel-httpd", daemon=True)
    thread.start()
    _STOP_EVENT.clear()
    while not stop.wait(0.2):
        if not thread.is_alive():
            break
    logger.info("stopping http server")
    _STOP_EVENT.set()
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)
    store.release_thread()
    logger.info("novel agent server stopped")


if __name__ == "__main__":
    main()
