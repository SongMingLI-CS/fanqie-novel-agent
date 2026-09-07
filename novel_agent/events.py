"""Cross-process event bus backed by SQLite.

The HTTP server and the worker are separate processes, so live progress
(chapter saved, token deltas, agent stage changes) cannot travel over an
in-memory channel. This repository appends structured events to the ``events``
table whose auto-increment ``id`` is a strict cursor: an SSE subscriber asks for
``id > since`` and is always able to resume after a disconnect without missing a
frame. Writing uses the Store's per-thread connection and explicit
``BEGIN IMMEDIATE`` transactions, exactly like every other DAO in this project.

Event types used by the worker and consumed by the web console:
``agent.stage`` / ``agent.run`` / ``checkpoint.saved`` / ``llm.delta`` /
``llm.done`` / ``chapter.ready`` / ``job.status``.
"""
import json

from .store import dumps, now


class EventRepository:
    def __init__(self, store):
        self.store = store

    @property
    def db(self):
        return self.store.db

    def tx(self):
        return self.store.tx()

    def publish(self, novel_id, run_id, event_type, payload=None):
        """Append one event and return its row as a dict."""
        row = (novel_id, run_id, event_type, dumps(payload or {}), now())
        with self.tx():
            cursor = self.db.execute(
                "INSERT INTO events(novel_id, run_id, type, payload, created_at) "
                "VALUES (?,?,?,?,?)",
                row,
            )
            event_id = cursor.lastrowid
        return self.get(event_id)

    def get(self, event_id):
        row = self.db.execute(
            "SELECT * FROM events WHERE id=?", (event_id,)
        ).fetchone()
        return self._hydrate(row) if row else None

    @staticmethod
    def _hydrate(row):
        event = dict(row)
        try:
            event["payload"] = json.loads(event["payload"] or "{}")
        except (TypeError, ValueError):
            event["payload"] = {}
        return event

    def read_since(self, novel_id, since=0, limit=200):
        """Return events for ``novel_id`` with ``id > since``, oldest first."""
        rows = self.db.execute(
            "SELECT * FROM events WHERE novel_id=? AND id>? "
            "ORDER BY id ASC LIMIT ?",
            (novel_id, int(since or 0), max(1, min(int(limit), 1000))),
        ).fetchall()
        return [self._hydrate(r) for r in rows]

    def latest_id(self, novel_id):
        row = self.db.execute(
            "SELECT MAX(id) AS m FROM events WHERE novel_id=?", (novel_id,)
        ).fetchone()
        return int(row["m"] or 0)

    def chapter_events(self, novel_id, chapter_number, since=0, limit=300):
        """Events whose payload belongs to one chapter (used to replay a run)."""
        rows = self.db.execute(
            "SELECT * FROM events WHERE novel_id=? "
            "AND CAST(json_extract(payload,'$.chapter') AS INTEGER)=? AND id>? "
            "ORDER BY id ASC LIMIT ?",
            (
                novel_id,
                int(chapter_number),
                int(since or 0),
                max(1, min(int(limit), 1000)),
            ),
        ).fetchall()
        return [self._hydrate(r) for r in rows]

    def prune(self, keep_days=7):
        """Delete events older than ``keep_days`` (best-effort housekeeping)."""
        try:
            from datetime import datetime, timedelta, timezone

            older = (
                datetime.now(timezone.utc) - timedelta(days=max(1, int(keep_days)))
            ).isoformat()
            with self.tx():
                return self.db.execute(
                    "DELETE FROM events WHERE created_at < ?", (older,)
                ).rowcount
        except Exception:  # noqa: BLE001 - pruning is best-effort
            return 0
