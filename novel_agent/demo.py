"""Keyless end-to-end demo runner: record a real run, replay it offline.

The demo drives the *real* streaming pipeline (``NovelService.process_stream``,
the same coroutine the worker runs) against a deterministic model, so every
layer is exercised for real — multi-agent stages, checkpoints, live events —
without needing ``DEEPSEEK_API_KEY``.

Two subcommands:

* ``record``  — run one chapter against the real DeepSeek endpoint and write a
  replay file (requires a configured key and an existing novel).
* ``replay``  — play a replay file (or the built-in demo recording) against a
  throwaway database.  With ``--port`` it also starts the real HTTP server so
  the web console shows the SSE stream and typewriter live in a browser.

Examples::

    python -m novel_agent.demo replay                 # built-in, headless
    python -m novel_agent.demo replay --port 8890     # open in a browser
    python -m novel_agent.demo record --novel-id <id> --out data/replays/c1.json
    python -m novel_agent.demo replay --replay data/replays/c1.json --port 8890
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import server as srv
from .config import Config
from .deepseek import DeepSeekClient
from .envfile import load_env
from .events import EventRepository
from .llm import AsyncLLMClient
from .replay import (
    CapturingAsyncClient,
    Recording,
    ReplayClient,
    ReplayError,
    build_recording,
    default_recording,
)
from .service import NovelService
from .store import Store

ROOT = Path(__file__).resolve().parents[1]


class DemoServer:
    """Run the real HTTP/SSE server against a demo store (mirrors main())."""

    def __init__(self, store, config):
        self.prev = (srv.config, srv.store, srv.service, srv.STATIC_DIR)
        srv.config = config
        srv.store = store
        srv.service = None
        srv.STATIC_DIR = srv.ROOT / "static"
        srv._STOP_EVENT.clear()
        self.httpd = srv.NovelHTTPServer((config.host or "127.0.0.1", int(config.port)), srv.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def close(self):
        srv._STOP_EVENT.set()
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()
        self.thread.join(timeout=3)
        srv.config, srv.store, srv.service, srv.STATIC_DIR = self.prev


def _print_typewriter(text, char_delay=0.012, para_delay=0.12):
    """Terminal stand-in for the console's typewriter reveal."""
    for paragraph in [p for p in text.split("\n") if p.strip()]:
        for char in paragraph:
            print(char, end="", flush=True)
            time.sleep(char_delay)
        print()
        time.sleep(para_delay)


def _print_timeline(rows):
    for event in rows:
        payload = json.dumps(event["payload"], ensure_ascii=False)
        print(f"[#{event['id']}] {event['type']} {payload}")


def run_replay(recording, db=None, port=None, loose=False, linger=2.0, verbose=False):
    """Replay ``recording`` through the real streaming pipeline.

    Returns a summary dict with ``ok``, ``chapterStatus``, ``text`` (the
    validated final prose) and ``events`` (the published rows, oldest first).
    """
    created_temp = False
    db_path = Path(db) if db else None
    if db_path is None:
        temp = tempfile.TemporaryDirectory(prefix="novel-demo-")
        db_path = Path(temp.name) / "novel.sqlite3"
        created_temp = True

    config = Config(
        data_dir=db_path.parent,
        agent_stages=list(recording.stages),
        auto_export_txt=False,
        host="127.0.0.1",
        port=int(port or 0),
        auth_token="",
    )
    store = Store(db_path)
    client = ReplayClient(recording, loose=loose)
    events = EventRepository(store)
    service = NovelService(
        store, client, config, ROOT, async_client=client, events=events
    )

    novel = store.create_novel(
        recording.novel.get("title") or "演示小说",
        dict(recording.novel.get("storyBible") or {}),
        recording.novel.get("genre", ""),
        recording.novel.get("volume", ""),
    )
    job, _ = store.create_job(novel["id"], recording.chapter_number)

    server = None
    try:
        if port is not None:
            server = DemoServer(store, config)
            if verbose:
                print(f"\n演示控制台: {server.base}/  （浏览器打开即可观看 SSE + 打字机）")
        ok = asyncio.run(service.process_stream(store.get_job(job["id"])))
        rows = events.read_since(novel["id"], 0)
        if server is not None and ok:
            # Let a browser that is watching the SSE stream see the finish frame.
            time.sleep(linger)
    finally:
        if server is not None:
            server.close()
        store.close()

    final_text = ""
    for event in reversed(rows):
        if event["type"] == "llm.text":
            final_text = (event["payload"] or {}).get("text", "")
            break
    chapter = None
    if db_path.exists():
        store2 = Store(db_path)
        try:
            chapter = store2.chapter(novel["id"], recording.chapter_number)
        finally:
            store2.close()
    if created_temp:
        temp.cleanup()
    return {
        "ok": bool(ok),
        "db": str(db_path),
        "chapterStatus": (chapter or {}).get("status"),
        "text": final_text,
        "events": rows,
        "base": server.base if server else None,
    }

DEFAULT_STAGES = ["outline", "chapter", "polish"]


def _cmd_replay(args):
    recording = Recording.load(args.replay) if args.replay else default_recording()
    if not recording.stages:
        sys.exit("replay recording declares no stages; refusing to run")
    summary = run_replay(
        recording,
        db=args.db,
        port=args.port,
        loose=args.loose,
        verbose=not args.quiet,
    )
    if args.quiet:
        print(json.dumps({
            "ok": summary["ok"],
            "chapterStatus": summary["chapterStatus"],
            "textLength": len(summary["text"]),
            "events": len(summary["events"]),
            "base": summary["base"],
        }, ensure_ascii=False))
        return
    print(f"\n== 生成结果: {'成功' if summary['ok'] else '失败'} "
          f"(章节状态 {summary['chapterStatus']}, 事件 {len(summary['events'])} 条)")
    print("== 事件时间线")
    _print_timeline(summary["events"])
    if summary["text"]:
        print("\n== 正文逐字揭示")
        _print_typewriter(summary["text"])
    else:
        print("\n（未产生正文）")


def _cmd_record(args):
    load_env()  # DEEPSEEK_API_KEY comes from .env / the environment
    config = Config()
    db_path = Path(args.db) if args.db else config.data_dir / "novel.sqlite3"
    store = Store(db_path)
    novel = store.get_novel(args.novel_id)
    if novel is None:
        store.close()
        sys.exit(f"novel not found: {args.novel_id}")
    stages = (
        [s.strip() for s in args.stages.split(",") if s.strip()]
        if args.stages
        else list(config.agent_stages or DEFAULT_STAGES)
    )
    if "chapter" not in stages:
        stages = list(stages) + ["chapter"]

    job, _ = store.create_job(novel["id"], args.chapter)
    store.set_job_config(job["id"], {"stages": stages, "targetWords": 0, "autoExportTxt": False})
    job_row = store.get_job(job["id"])

    captured = []
    sync = DeepSeekClient(config)
    inner = AsyncLLMClient(config)
    async_client = CapturingAsyncClient(inner, captured.append)
    service = NovelService(
        store, sync, config, ROOT,
        async_client=async_client, events=EventRepository(store),
    )
    try:
        ok = asyncio.run(service.process_stream(job_row))
    finally:
        try:
            asyncio.run(inner.aclose())
        except Exception:  # noqa: BLE001 - closing is best-effort
            pass
        store.close()

    if not captured:
        sys.exit("record captured no model calls — check DEEPSEEK_API_KEY / base URL")
    recording = build_recording(novel, stages, args.chapter, captured, model=config.model)
    out = recording.save(args.out)
    print(json.dumps({
        "ok": bool(ok),
        "out": out,
        "calls": len(captured),
        "model": config.model or "<unset>",
        "chapterStatus": store_chapter_status(db_path, novel["id"], args.chapter),
    }, ensure_ascii=False, indent=2))


def store_chapter_status(db_path, novel_id, number):
    store = Store(db_path)
    try:
        chapter = store.chapter(novel_id, number)
        return (chapter or {}).get("status")
    finally:
        store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m novel_agent.demo",
        description="Keyless E2E demo: replay a recording (or the built-in one) "
                    "through the real multi-agent + SSE pipeline.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    replay = sub.add_parser("replay", help="run a recording offline (default built-in)")
    replay.add_argument("--replay", help="path to a replay JSON (default: built-in demo)")
    replay.add_argument("--db", help="reuse this SQLite file instead of a throwaway temp db")
    replay.add_argument("--port", type=int, default=None,
                        help="also start the real HTTP/SSE server on this port (0 = ephemeral)")
    replay.add_argument("--loose", action="store_true",
                        help="replay by call order, ignoring prompt equality")
    replay.add_argument("--quiet", action="store_true", help="print a JSON summary only")
    replay.set_defaults(func=_cmd_replay)

    record = sub.add_parser("record", help="run one chapter against the real model and save a replay")
    record.add_argument("--novel-id", required=True)
    record.add_argument("--chapter", type=int, default=1)
    record.add_argument("--stages", help="comma-separated (default: NOVEL_AGENT_STAGES)")
    record.add_argument("--out", required=True, help="where to write the replay JSON")
    record.add_argument("--db", help="SQLite file (default: NOVEL_DATA_DIR/novel.sqlite3)")
    record.set_defaults(func=_cmd_record)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except ReplayError as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
