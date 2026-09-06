"""Operator CLI: offline maintenance and backup for the Novel Agent SQLite DB.

Zero runtime dependencies (pure stdlib + this package). Usage::

    python -m novel_agent.ops stats  --db data/novel.sqlite3 --days 14
    python -m novel_agent.ops backup --db data/novel.sqlite3 --out backups/novel-2026-09-04.sqlite3
    python -m novel_agent.ops vacuum --db data/novel.sqlite3

``backup`` uses the SQLite online backup API, so it is safe to run while a
server or worker is actively writing (the store runs in WAL mode). ``stats``
mirrors the aggregate counters exposed by ``GET /api/ops/metrics`` plus daily
usage and the on-disk footprint, so dashboards and cron jobs agree with the app.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

from novel_agent.store import Store


def _db_path(value):
    path = Path(value).expanduser()
    if not path.is_file():
        raise SystemExit(f"database not found: {path}")
    return path


def _file_size(path):
    try:
        return path.stat().st_size
    except OSError:
        return 0


def cmd_stats(args):
    path = _db_path(args.db)
    store = Store(path)  # read-only metrics; Store only ensures schema exists
    try:
        metrics = store.metrics()
        series = store.usage_series(args.days)
    finally:
        store.close()
    payload = {
        "database": str(path),
        "size_bytes": _file_size(path),
        "wal_bytes": _file_size(Path(str(path) + "-wal")),
        "metrics": metrics,
        "usage_trend": series,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_backup(args):
    src = _db_path(args.db)
    out = Path(args.out).expanduser()
    if out.exists() and not args.force:
        raise SystemExit(
            f"refusing to overwrite existing backup: {out} (pass --force to replace)"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    try:
        source = sqlite3.connect(str(src))
        try:
            dest = sqlite3.connect(str(tmp))
            try:
                # Online backup API: a consistent snapshot even while writers run.
                source.backup(dest)
            finally:
                dest.close()
        finally:
            source.close()
        tmp.replace(out)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    print(f"backup written: {out} ({_file_size(out)} bytes)")


def cmd_vacuum(args):
    path = _db_path(args.db)
    conn = sqlite3.connect(str(path))
    try:
        # Flush the WAL into the main file first, then reclaim freed pages.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    finally:
        conn.close()
    print(f"vacuum complete: {path} ({_file_size(path)} bytes)")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="novel-agent-ops",
        description="Maintenance and backup for the Novel Agent SQLite database.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_stats = sub.add_parser("stats", help="print DB metrics, usage trend and footprint")
    p_stats.add_argument("--db", required=True, help="path to the SQLite database file")
    p_stats.add_argument("--days", type=int, default=7, help="usage trend window in days")
    p_stats.set_defaults(func=cmd_stats)

    p_backup = sub.add_parser("backup", help="write an online consistent snapshot")
    p_backup.add_argument("--db", required=True, help="path to the SQLite database file")
    p_backup.add_argument("--out", required=True, help="destination backup file path")
    p_backup.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    p_backup.set_defaults(func=cmd_backup)

    p_vacuum = sub.add_parser("vacuum", help="checkpoint the WAL and reclaim space")
    p_vacuum.add_argument("--db", required=True, help="path to the SQLite database file")
    p_vacuum.set_defaults(func=cmd_vacuum)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
