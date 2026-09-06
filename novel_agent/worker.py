import asyncio
import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .config import Config
from .deepseek import DeepSeekClient
from .envfile import load_env
from .events import EventRepository
from .llm import AsyncLLMClient
from .logutil import setup_logging
from .service import NovelService
from .store import Store

logger = logging.getLogger(__name__)


def run_once(store, service):
    """Claim and process at most one job (crash-safe).

    Returns True when a job was claimed (regardless of pass/fail) so callers can
    distinguish "work happened" from "queue empty".

    Unexpected exceptions raised by ``service.process`` are caught here and the
    job is marked failed *immediately* (bounded by ``max_job_attempts`` with
    exponential backoff) instead of being left ``RUNNING`` until its lease
    expires. Without this, a single code-level crash would stall the chapter
    for up to ``NOVEL_JOB_TIMEOUT`` and then retry forever without ever
    advancing the attempt counter into a terminal ``FAILED`` state.
    """
    job = store.claim_job(service.config.job_timeout)
    if job is None:
        return False
    try:
        service.process(job)
    except Exception as exc:  # noqa: BLE001 - a bad job must not kill the daemon
        logger.exception(
            "job processing crashed job_id=%s chapter=%s novel=%s",
            job.get("id"), job.get("chapter_number"), job.get("novel_id"),
            extra={"fields": {"job_id": job.get("id"), "chapter": job.get("chapter_number")}},
        )
        try:
            store.fail_job(
                job,
                f"worker_crash:{type(exc).__name__}: {exc}",
                service.config.max_job_attempts,
            )
        except Exception:  # noqa: BLE001 - never mask the original crash
            logger.exception("failed to mark crashed job job_id=%s", job.get("id"))
    return True


def _process_one(config, db_path):
    store = Store(db_path)
    try:
        service = NovelService(store, DeepSeekClient(config), config)
        return run_once(store, service)
    finally:
        store.close()


def run_once_stream(store, service):
    """Claim and process at most one job over the async/streaming pipeline.

    Same crash semantics as :func:`run_once`, but the model call runs through
    :meth:`NovelService.process_stream`, which publishes live token/stage
    events into the ``events`` table for the web console to stream.
    """
    job = store.claim_job(service.config.job_timeout)
    if job is None:
        return False
    try:
        asyncio.run(service.process_stream(job))
    except Exception as exc:  # noqa: BLE001 - a bad job must not kill the daemon
        logger.exception(
            "job streaming crashed job_id=%s chapter=%s novel=%s",
            job.get("id"), job.get("chapter_number"), job.get("novel_id"),
            extra={"fields": {"job_id": job.get("id"), "chapter": job.get("chapter_number")}},
        )
        try:
            store.fail_job(
                job,
                f"worker_crash:{type(exc).__name__}: {exc}",
                service.config.max_job_attempts,
            )
        except Exception:  # noqa: BLE001 - never mask the original crash
            logger.exception("failed to mark crashed job job_id=%s", job.get("id"))
    return True


def _process_one_stream(config, db_path):
    store = Store(db_path)
    client = None
    try:
        client = AsyncLLMClient(config)
        service = NovelService(
            store, DeepSeekClient(config), config,
            async_client=client, events=EventRepository(store),
        )
        return run_once_stream(store, service)
    finally:
        if client is not None:
            try:
                asyncio.run(client.aclose())
            except Exception:  # noqa: BLE001 - closing the pool is best-effort
                pass
        store.close()


def main():
    load_env()  # .env is loaded before Config() so values are picked up.
    config = Config()  # raises ConfigError on malformed settings
    setup_logging(config.log_level, config.log_format)

    db_path = config.data_dir / "novel.sqlite3"
    max_workers = max(1, config.worker_concurrency)

    # Housekeeping: drop stale live events on startup (best-effort).
    try:
        _store = Store(db_path)
        try:
            EventRepository(_store).prune(config.event_ttl_days)
        finally:
            _store.close()
    except Exception:  # noqa: BLE001
        logger.debug("worker event prune skipped", exc_info=True)

    stop = threading.Event()

    def _handle_signal(signum, frame):
        logger.info("received signal %s, draining worker", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Adaptive polling: spin quickly while work is available, but back off when
    # the queue is empty so a long-running daemon does not blind-poll / spin.
    idle_sleep = 1.0
    max_idle_sleep = 15.0
    logger.info(
        "novel agent worker started concurrency=%s once=%s data=%s",
        max_workers, config.worker_once, db_path,
    )
    try:
        while not stop.is_set():
            active = 0
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [pool.submit(_process_one_stream, config, db_path) for _ in range(max_workers)]
                for future in futures:
                    try:
                        if future.result():
                            active += 1
                    except Exception:  # noqa: BLE001 - a single bad task must not kill the daemon
                        logger.exception("worker task failed unexpectedly")

            if config.worker_once:
                logger.info("worker once=true, exiting after one pass (active=%s)", active)
                break
            if active:
                idle_sleep = max(0.5, idle_sleep * 0.5)
            else:
                idle_sleep = min(max_idle_sleep, idle_sleep * 2)
            # Event.wait (instead of time.sleep) makes SIGTERM interrupt promptly.
            stop.wait(idle_sleep)
    finally:
        logger.info("novel agent worker stopped")


if __name__ == "__main__":
    main()
