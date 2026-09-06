"""DAO / repository for the multi-agent run lifecycle and checkpoints.

Persistence reuses the :class:`~novel_agent.store.Store` per-thread connection
and its explicit ``BEGIN IMMEDIATE`` transactions, so a single repository
instance is only ever touched from one thread at a time (the worker already
builds one Store per task). This keeps checkpoint writes crash-safe and
serialised exactly like the rest of the application's writes.

Tables (append-only, created via the Store schema):

* ``agent_runs``    — one row per multi-agent generation of a chapter.
* ``agent_stages``  — per-stage state machine (PENDING/RUNNING/DONE/FAILED).
* ``checkpoints``   — cumulative stage memory written after each stage finishes.
"""
import json
import uuid

from .models import RunStatus, StageState
from .store import dumps, now


class CheckpointRepository:
    def __init__(self, store):
        self.store = store

    @property
    def db(self):
        return self.store.db

    def tx(self):
        return self.store.tx()

    # -- runs ----------------------------------------------------------------

    def find_run(self, job_id):
        row = self.db.execute(
            "SELECT * FROM agent_runs WHERE job_id=?", (job_id,)
        ).fetchone()
        return self._hydrate(row) if row else None

    def get_run(self, run_id):
        row = self.db.execute(
            "SELECT * FROM agent_runs WHERE id=?", (run_id,)
        ).fetchone()
        return self._hydrate(row) if row else None

    @staticmethod
    def _hydrate(row):
        run = dict(row)
        run["stages"] = json.loads(run["stages"])
        run["auto_export_txt"] = bool(run["auto_export_txt"])
        return run

    def create_run(self, job, stages, target_words, auto_export):
        run_id = str(uuid.uuid4())
        ts = now()
        with self.tx():
            self.db.execute(
                "INSERT INTO agent_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, job["id"], job["novel_id"], job["chapter_number"],
                    json.dumps(list(stages), ensure_ascii=False),
                    int(target_words or 0), int(bool(auto_export)),
                    RunStatus.PENDING, "", "", ts, ts,
                ),
            )
        return self.get_run(run_id)
    # -- stages --------------------------------------------------------------

    def stage_states(self, run_id):
        rows = self.db.execute(
            "SELECT stage, state FROM agent_stages WHERE run_id=?", (run_id,)
        ).fetchall()
        return {r["stage"]: r["state"] for r in rows}

    def latest_memory(self, run_id):
        row = self.db.execute(
            "SELECT memory FROM checkpoints WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return row["memory"] if row else "{}"

    def stage_raw(self, run_id, stage):
        row = self.db.execute(
            "SELECT raw FROM agent_stages WHERE run_id=? AND stage=?", (run_id, stage)
        ).fetchone()
        return row["raw"] if row else ""

    def resume_from(self, run, stages):
        """Return the first stage not yet DONE, or None when all are done."""
        states = self.stage_states(run["id"])
        for stage in stages:
            if states.get(stage) != StageState.DONE:
                return stage
        return None

    def mark_stage_started(self, run_id, stage):
        ts = now()
        with self.tx():
            self.db.execute(
                "INSERT INTO agent_stages VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id,stage) DO UPDATE SET "
                "state=excluded.state,error='',started_at=excluded.started_at",
                (run_id, stage, StageState.RUNNING, "{}", "", "", ts, ""),
            )
            self.db.execute(
                "UPDATE agent_runs SET status=?,current_stage=?,updated_at=? WHERE id=?",
                (RunStatus.RUNNING, stage, ts, run_id),
            )

    def mark_stage_done(self, run_id, stage, payload, memory, raw=""):
        ts = now()
        with self.tx():
            self.db.execute(
                "UPDATE agent_stages SET state=?,payload=?,raw=?,finished_at=? "
                "WHERE run_id=? AND stage=?",
                (StageState.DONE, dumps(payload), raw, ts, run_id, stage),
            )
            self.db.execute(
                "INSERT INTO checkpoints VALUES (?,?,?,?,?)",
                (run_id, stage, "{}", memory, ts),
            )

    def mark_stage_failed(self, run_id, stage, error):
        ts = now()
        with self.tx():
            self.db.execute(
                "UPDATE agent_stages SET state=?,error=? WHERE run_id=? AND stage=?",
                (StageState.FAILED, error, ts, run_id, stage),
            )

    # -- run terminal states -------------------------------------------------

    def mark_run_succeeded(self, run_id):
        with self.tx():
            self.db.execute(
                "UPDATE agent_runs SET status=?,current_stage='',updated_at=? WHERE id=?",
                (RunStatus.SUCCEEDED, now(), run_id),
            )

    def mark_run_failed(self, run_id, error):
        with self.tx():
            self.db.execute(
                "UPDATE agent_runs SET status=?,error=?,updated_at=? WHERE id=?",
                (RunStatus.FAILED, error, now(), run_id),
            )

    # -- read model for the UI ----------------------------------------------

    def latest_run_for_novel(self, nid):
        row = self.db.execute(
            "SELECT * FROM agent_runs WHERE novel_id=? ORDER BY created_at DESC LIMIT 1",
            (nid,),
        ).fetchone()
        if not row:
            return None
        run = dict(row)
        run["stages"] = json.loads(run["stages"])
        run["auto_export_txt"] = bool(run["auto_export_txt"])
        run["stagesDetail"] = []
        for s in self.db.execute(
            "SELECT stage,state,payload,error,started_at,finished_at "
            "FROM agent_stages WHERE run_id=? ORDER BY started_at",
            (run["id"],),
        ).fetchall():
            item = dict(s)
            try:
                item["payload"] = json.loads(item["payload"] or "{}")
            except (TypeError, ValueError):
                item["payload"] = {}
            run["stagesDetail"].append(item)
        return run
