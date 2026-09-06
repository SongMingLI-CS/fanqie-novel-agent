import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import ChapterStatus

# Keep at most this many story_bibles rows per novel to stop unbounded table growth.
_KEEP_BIBLE_VERSIONS = 20

_BUSY_TIMEOUT_MS = 5000


def now():
    return datetime.now(timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


SCHEMA = """
CREATE TABLE IF NOT EXISTS novels (id TEXT PRIMARY KEY, title TEXT NOT NULL, volume TEXT DEFAULT '', genre TEXT DEFAULT '', current_chapter INTEGER DEFAULT 0, paused INTEGER DEFAULT 0, story_bible_version INTEGER DEFAULT 1, skill_version TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS story_bibles (novel_id TEXT NOT NULL, version INTEGER NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(novel_id,version), FOREIGN KEY(novel_id) REFERENCES novels(id));
CREATE TABLE IF NOT EXISTS chapters (id TEXT PRIMARY KEY, novel_id TEXT NOT NULL, number INTEGER NOT NULL, status TEXT NOT NULL, title TEXT DEFAULT '', goal TEXT DEFAULT '', beats TEXT DEFAULT '[]', content TEXT DEFAULT '', summary TEXT DEFAULT '', characters TEXT DEFAULT '[]', events TEXT DEFAULT '[]', foreshadowing_added TEXT DEFAULT '[]', foreshadowing_resolved TEXT DEFAULT '[]', state_changes TEXT DEFAULT '[]', hook TEXT DEFAULT '', raw_response TEXT DEFAULT '', review TEXT DEFAULT '{}', proposed_state TEXT DEFAULT '{}', model TEXT DEFAULT '', generated_at TEXT DEFAULT '', exported_at TEXT DEFAULT '', published_at TEXT DEFAULT '', publish_record TEXT DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(novel_id,number), FOREIGN KEY(novel_id) REFERENCES novels(id));
CREATE TABLE IF NOT EXISTS characters (novel_id TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(novel_id,key));
CREATE TABLE IF NOT EXISTS world_rules (novel_id TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(novel_id,key));
CREATE TABLE IF NOT EXISTS timeline_events (novel_id TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(novel_id,key));
CREATE TABLE IF NOT EXISTS foreshadowing (novel_id TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(novel_id,key));
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, novel_id TEXT NOT NULL, chapter_number INTEGER NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, attempts INTEGER DEFAULT 0, locked_until TEXT, next_attempt_at TEXT, error TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(novel_id,chapter_number,kind,status));
CREATE TABLE IF NOT EXISTS usage (id TEXT PRIMARY KEY, job_id TEXT, model TEXT, prompt_version TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, duration_ms INTEGER DEFAULT 0, request_status TEXT, error TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chapter_drafts (id TEXT PRIMARY KEY, chapter_id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL, raw_response TEXT DEFAULT '', proposed_state TEXT DEFAULT '{}', created_at TEXT NOT NULL, UNIQUE(chapter_id,version));
CREATE TABLE IF NOT EXISTS review_results (id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS publish_records (id TEXT PRIMARY KEY, chapter_id TEXT NOT NULL, platform TEXT NOT NULL, external_url TEXT DEFAULT '', published_at TEXT NOT NULL, operator TEXT NOT NULL, notes TEXT DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS export_jobs (id TEXT PRIMARY KEY, chapter_id TEXT NOT NULL, format TEXT NOT NULL, status TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, path TEXT DEFAULT '', error TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS publish_jobs (id TEXT PRIMARY KEY, chapter_id TEXT NOT NULL, status TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, platform TEXT DEFAULT '', external_url TEXT DEFAULT '', error TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log (id TEXT PRIMARY KEY, novel_id TEXT, action TEXT NOT NULL, detail TEXT DEFAULT '{}', created_at TEXT NOT NULL);
"""


class Store:
    """SQLite persistence with per-thread connections (WAL + busy_timeout).

    A single sqlite3.Connection is never shared across threads: every thread
    lazily opens its own connection (kept in ``threading.local``) and explicit
    ``BEGIN IMMEDIATE`` transactions serialize writers, which removes the
    read/write interleaving and TOCTOU races of the previous shared connection.
    """

    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._path = str(path)
        self._local = threading.local()
        # First connection is created/owned by the constructing thread and
        # applies the schema to the database file.
        conn = self.db
        conn.executescript(SCHEMA)
        self._ensure_column(conn, 'chapters', 'beats', "TEXT DEFAULT '[]'")
        self._ensure_column(conn, 'jobs', 'next_attempt_at', "TEXT")
        conn.commit()

    # -- connections ---------------------------------------------------------

    def _connect(self):
        # isolation_level=None disables sqlite3's implicit transaction handling;
        # every transaction is opened/committed explicitly. timeout + busy_timeout
        # make concurrent writers wait instead of failing instantly.
        conn = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @property
    def db(self):
        """Return the connection owned by the current thread (create if needed)."""
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def close(self):
        """Close the current thread's connection."""
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None

    def release_thread(self):
        """Alias used by the HTTP server to drop per-request connections."""
        self.close()

    def _ensure_column(self, conn, table, name, definition):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _prune_bible_versions(self, conn, nid):
        # story_bibles rows are append-only; keep only the most recent versions.
        conn.execute(
            "DELETE FROM story_bibles WHERE novel_id=? AND version < "
            "(SELECT MAX(version) FROM story_bibles WHERE novel_id=?) - ?",
            (nid, nid, _KEEP_BIBLE_VERSIONS - 1),
        )

    def _sync_bible(self, conn, nid, bible):
        mappings = (
            ('characters', 'characters'),
            ('world_rules', 'worldRules'),
            ('timeline_events', 'timeline'),
            ('foreshadowing', 'foreshadowing'),
        )
        for table, key in mappings:
            values = bible.get(key, []) or []
            for index, item in enumerate(values):
                item = item if isinstance(item, dict) else {'value': item}
                item_key = str(item.get('key') or item.get('name') or item.get('id') or index)
                conn.execute(
                    f"INSERT INTO {table}(novel_id,key,data) VALUES (?,?,?) "
                    "ON CONFLICT(novel_id,key) DO UPDATE SET data=excluded.data",
                    (nid, item_key, dumps(item)),
                )

    # -- transactions --------------------------------------------------------

    @contextmanager
    def tx(self):
        """Explicit ``BEGIN IMMEDIATE`` transaction (writer lock up front)."""
        conn = self.db
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        else:
            conn.commit()

    # -- novels / bible ------------------------------------------------------

    def create_novel(self, title, bible, genre="", volume=""):
        nid, ts = str(uuid.uuid4()), now()
        conn = self.db
        with self.tx():
            conn.execute(
                "INSERT INTO novels VALUES (?,?,?,?,?,?,?,?,?,?)",
                (nid, title, volume, genre, 0, 0, 1, "novel-writer@1", ts, ts),
            )
            conn.execute(
                "INSERT INTO story_bibles VALUES (?,?,?,?)", (nid, 1, dumps(bible), ts)
            )
            self._sync_bible(conn, nid, bible)
        self.record_audit(nid, "novel_created", {"title": title})
        return self.get_novel(nid)

    def novels(self):
        return [
            self.get_novel(r[0])
            for r in self.db.execute("SELECT id FROM novels ORDER BY updated_at DESC")
        ]

    def get_novel(self, nid):
        row = self.db.execute("SELECT * FROM novels WHERE id=?", (nid,)).fetchone()
        if not row:
            return None
        result = dict(row)
        bible = self.db.execute(
            "SELECT content FROM story_bibles WHERE novel_id=? AND version=?",
            (nid, row["story_bible_version"]),
        ).fetchone()
        result["story_bible"] = json.loads(bible[0]) if bible else {}
        return result

    def update_bible(self, nid, content):
        conn = self.db
        row = conn.execute(
            "SELECT MAX(version) FROM story_bibles WHERE novel_id=?", (nid,)
        ).fetchone()
        version = int(row[0] or 0) + 1
        with self.tx():
            conn.execute(
                "INSERT INTO story_bibles VALUES (?,?,?,?)",
                (nid, version, dumps(content), now()),
            )
            self._sync_bible(conn, nid, content)
            conn.execute(
                "UPDATE novels SET story_bible_version=?,updated_at=? WHERE id=?",
                (version, now(), nid),
            )
            self._prune_bible_versions(conn, nid)
        self.record_audit(nid, "bible_updated", {"version": version})
        return version

    # -- jobs ----------------------------------------------------------------

    def create_job(self, nid, number, kind="generate"):
        """Idempotent job creation, safe under concurrent double-submits.

        Runs inside a single ``BEGIN IMMEDIATE`` transaction so two racing
        ``generate`` requests serialize: the loser of the race observes the
        winner's row (or catches the UNIQUE conflict) and returns it instead of
        surfacing an IntegrityError as a 500.
        """
        novel = self.get_novel(nid)
        if not novel:
            raise ValueError("novel_not_found")
        if novel["paused"]:
            raise ValueError("novel_is_paused")
        if number < 1:
            raise ValueError("chapter_number_must_be_positive")
        key = f"{nid}:{number}:{kind}"
        conn = self.db
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing and existing["status"] not in ("FAILED", "CANCELLED"):
                conn.commit()
                return dict(existing), False
            if existing:
                conn.execute(
                    "UPDATE jobs SET status='PENDING',error='',updated_at=? WHERE id=?",
                    (now(), existing["id"]),
                )
                conn.commit()
                return dict(
                    conn.execute("SELECT * FROM jobs WHERE id=?", (existing["id"],)).fetchone()
                ), True
            jid = str(uuid.uuid4())
            ts = now()
            conn.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (jid, nid, number, kind, "PENDING", key, 0, None, ts, "", ts, ts),
            )
            conn.execute(
                "INSERT OR IGNORE INTO chapters(id,novel_id,number,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (str(uuid.uuid4()), nid, number, ChapterStatus.PENDING, ts, ts),
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()), True
        except sqlite3.IntegrityError:
            # A concurrent insert won the race; roll back and return its row.
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            winner = conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key=?", (key,)
            ).fetchone()
            if winner:
                return dict(winner), False
            raise
        except Exception:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise

    def claim_job(self, lease_seconds=900):
        conn = self.db
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE jobs SET status='PENDING',locked_until=NULL "
                "WHERE status='RUNNING' AND locked_until < ?",
                (now(),),
            )
            row = conn.execute(
                "SELECT * FROM jobs WHERE status='PENDING' AND "
                "(next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY created_at LIMIT 1",
                (now(),),
            ).fetchone()
            if not row:
                conn.commit()
                return None
            lease = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
            conn.execute(
                "UPDATE jobs SET status='RUNNING',attempts=attempts+1,locked_until=?,updated_at=? "
                "WHERE id=? AND status='PENDING'",
                (lease, now(), row["id"]),
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())
        except Exception:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise

    def get_job(self, jid):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        return dict(row) if row else None

    def jobs(self, nid):
        return [
            dict(x)
            for x in self.db.execute(
                "SELECT * FROM jobs WHERE novel_id=? ORDER BY created_at DESC", (nid,)
            )
        ]

    def cancel_job(self, jid):
        conn = self.db
        with self.tx():
            row = conn.execute(
                "SELECT id, novel_id, chapter_number FROM jobs "
                "WHERE id=? AND status IN ('PENDING','RUNNING')",
                (jid,),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE jobs SET status='CANCELLED',locked_until=NULL,updated_at=? WHERE id=?",
                (now(), jid),
            )
            conn.execute(
                "UPDATE chapters SET status='CANCELLED',updated_at=? "
                "WHERE novel_id=? AND number=? AND status IN "
                "('PENDING','PLANNING','GENERATING','REVIEWING')",
                (now(), row["novel_id"], row["chapter_number"]),
            )
            cancelled = (row["novel_id"], row["chapter_number"])
        self.record_audit(cancelled[0], "job_cancelled", {"job": jid, "chapter": cancelled[1]})
        return True

    def rewrite_chapter(self, cid):
        """Delete the newest unpublished chapter and queue its number to be
        regenerated from the Story Bible (serial overwrite rewrite).

        Only the highest-numbered chapter that has not been published yet may
        be rewritten: nothing is written after it, so a fresh generation built
        from the outline plus the preceding chapter summaries stays continuous.

        The rewrite is atomic: the chapter row keeps its id but every
        content-bearing field, its draft/review/export/publish history and all
        generation jobs (and their usage rows) for that number are removed in a
        single ``BEGIN IMMEDIATE`` transaction, then a brand-new PENDING job is
        queued for the same number.
        """
        ch = self.chapter_by_id(cid)
        if not ch:
            raise ValueError("chapter_not_found")
        nid, number = ch["novel_id"], ch["number"]
        novel = self.get_novel(nid)
        if not novel:
            raise ValueError("novel_not_found")
        if novel["paused"]:
            raise ValueError("novel_is_paused")
        if number <= novel["current_chapter"]:
            raise ValueError("chapter_already_published")
        conn = self.db
        latest = int(
            conn.execute(
                "SELECT COALESCE(MAX(number),0) FROM chapters WHERE novel_id=?", (nid,)
            ).fetchone()[0]
        )
        if number != latest:
            raise ValueError("only_latest_draft_can_be_rewritten")
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-validate under the writer lock so a racing worker cannot slip in.
            now_status = conn.execute(
                "SELECT status FROM chapters WHERE id=?", (cid,)
            ).fetchone()
            if not now_status:
                raise ValueError("chapter_not_found")
            if now_status["status"] in ("PENDING", "PLANNING", "GENERATING", "REVIEWING"):
                raise ValueError("chapter_busy")
            active = conn.execute(
                "SELECT 1 FROM jobs WHERE novel_id=? AND chapter_number=? "
                "AND status IN ('PENDING','RUNNING')",
                (nid, number),
            ).fetchone()
            if active:
                raise ValueError("chapter_busy")
            latest_now = int(
                conn.execute(
                    "SELECT COALESCE(MAX(number),0) FROM chapters WHERE novel_id=?", (nid,)
                ).fetchone()[0]
            )
            if number != latest_now:
                raise ValueError("only_latest_draft_can_be_rewritten")
            # Purge per-chapter history, then generation jobs for this number.
            conn.execute(
                "DELETE FROM review_results WHERE draft_id IN "
                "(SELECT id FROM chapter_drafts WHERE chapter_id=?)",
                (cid,),
            )
            conn.execute("DELETE FROM chapter_drafts WHERE chapter_id=?", (cid,))
            conn.execute("DELETE FROM export_jobs WHERE chapter_id=?", (cid,))
            conn.execute("DELETE FROM publish_jobs WHERE chapter_id=?", (cid,))
            conn.execute("DELETE FROM publish_records WHERE chapter_id=?", (cid,))
            conn.execute(
                "DELETE FROM usage WHERE job_id IN "
                "(SELECT id FROM jobs WHERE novel_id=? AND chapter_number=?)",
                (nid, number),
            )
            conn.execute(
                "DELETE FROM jobs WHERE novel_id=? AND chapter_number=?", (nid, number)
            )
            ts = now()
            conn.execute(
                "UPDATE chapters SET status=?,title='',goal='',beats='[]',content='',"
                "summary='',characters='[]',events='[]',foreshadowing_added='[]',"
                "foreshadowing_resolved='[]',state_changes='[]',hook='',raw_response='',"
                "review='{}',proposed_state='{}',model='',generated_at='',exported_at='',"
                "published_at='',publish_record='{}',updated_at=? WHERE id=?",
                (ChapterStatus.PENDING, ts, cid),
            )
            key = f"{nid}:{number}:generate"
            jid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (jid, nid, number, "generate", "PENDING", key, 0, None, ts, "", ts, ts),
            )
            conn.commit()
            job = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
            self.record_audit(nid, "chapter_rewritten", {"chapter": number})
            return job
        except Exception:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise

    def fail_job(self, job, error, max_attempts=3):
        current = self.get_job(job["id"]) or job
        retry = current["attempts"] < max_attempts
        status = "PENDING" if retry else "FAILED"
        chapter_status = "PENDING" if retry else "FAILED"
        retry_at = (
            (datetime.now(timezone.utc) + timedelta(seconds=min(60, 2 ** current["attempts"]))).isoformat()
            if retry
            else None
        )
        with self.tx():
            self.db.execute(
                "UPDATE jobs SET status=?,error=?,locked_until=NULL,next_attempt_at=?,updated_at=? WHERE id=?",
                (status, error, retry_at, now(), job["id"]),
            )
            self.db.execute(
                "UPDATE chapters SET status=?,updated_at=? WHERE novel_id=? AND number=?",
                (chapter_status, now(), job["novel_id"], job["chapter_number"]),
            )
        return retry

    # -- chapters ------------------------------------------------------------

    def chapter(self, nid, number):
        row = self.db.execute(
            "SELECT * FROM chapters WHERE novel_id=? AND number=?", (nid, number)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        for key in (
            "beats", "characters", "events", "foreshadowing_added",
            "foreshadowing_resolved", "state_changes", "review", "proposed_state",
            "publish_record",
        ):
            result[key] = json.loads(
                result[key] or ("{}" if key in ("review", "proposed_state", "publish_record") else "[]")
            )
        return result

    def chapters(self, nid):
        return [
            self.chapter(nid, r[0])
            for r in self.db.execute(
                "SELECT number FROM chapters WHERE novel_id=? ORDER BY number", (nid,)
            ).fetchall()
        ]

    def chapter_by_id(self, cid):
        row = self.db.execute(
            "SELECT novel_id,number FROM chapters WHERE id=?", (cid,)
        ).fetchone()
        return self.chapter(row[0], row[1]) if row else None

    def recent(self, nid, limit=3):
        return self.chapters(nid)[-limit:]

    def record_review(self, cid, result):
        with self.tx():
            self.db.execute(
                "UPDATE chapters SET review=?,status=?,updated_at=? WHERE id=?",
                (
                    dumps(result),
                    "WAITING_APPROVAL" if result.get("passed") else "FAILED",
                    now(),
                    cid,
                ),
            )

    def set_status(self, nid, number, status):
        self.db.execute(
            "UPDATE chapters SET status=?,updated_at=? WHERE novel_id=? AND number=?",
            (status, now(), nid, number),
        )
        self.db.commit()

    def set_paused(self, nid, paused):
        self.db.execute(
            "UPDATE novels SET paused=?,updated_at=? WHERE id=?", (int(paused), now(), nid)
        )
        self.db.commit()

    def record_export(self, nid, number):
        self.set_status(nid, number, ChapterStatus.EXPORTED)
        self.db.execute(
            "UPDATE chapters SET exported_at=? WHERE novel_id=? AND number=?",
            (now(), nid, number),
        )
        self.db.commit()
        self.record_audit(nid, "chapter_exported", {"chapter": number})

    def update_draft(self, cid, changes):
        ch = self.chapter_by_id(cid)
        if not ch:
            raise ValueError("chapter_not_found")
        allowed = {k: changes[k] for k in ("title", "goal", "content", "summary", "hook") if k in changes}
        if not allowed:
            raise ValueError("no_editable_fields")
        sets = ", ".join(f"{k}=?" for k in allowed)
        values = list(allowed.values())
        with self.tx():
            version = int(
                self.db.execute(
                    "SELECT COALESCE(MAX(version),0) FROM chapter_drafts WHERE chapter_id=?",
                    (ch["id"],),
                ).fetchone()[0]
            ) + 1
            self.db.execute(
                "INSERT INTO chapter_drafts VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), ch["id"], version, dumps({**ch, **allowed}), "", "{}", now()),
            )
            self.db.execute(
                f"UPDATE chapters SET {sets},status='REVIEWING',review='{{}}',updated_at=? WHERE id=?",
                (*values, now(), cid),
            )
        return self.chapter_by_id(cid)

    def manual_publish(self, nid, number, record):
        conn = self.db
        with self.tx():
            changed = conn.execute(
                "UPDATE chapters SET status=?,publish_record=?,published_at=?,updated_at=? "
                "WHERE novel_id=? AND number=? AND status='EXPORTED'",
                (ChapterStatus.PUBLISHED_MANUALLY, dumps(record), record.get("publishedAt", now()), now(), nid, number),
            ).rowcount
            if not changed:
                raise ValueError("chapter_must_be_exported_before_manual_publish")
            ch = self.chapter(nid, number)
            conn.execute(
                "INSERT INTO publish_records VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), ch["id"], record["platform"], record.get("externalUrl", ""),
                 record.get("publishedAt", now()), record["operator"], record.get("notes", ""), now()),
            )
            conn.execute(
                "INSERT INTO publish_jobs VALUES (?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), ch["id"], "SUCCEEDED", f"{ch['id']}:{record['platform']}",
                 record["platform"], record.get("externalUrl", ""), " ", now(), now()),
            )
            novel = self.get_novel(nid)
            bible = dict(novel["story_bible"])
            proposed = ch.get("proposed_state", {})
            bible["currentChapter"] = number
            bible["currentPosition"] = proposed.get("currentPosition", bible.get("currentPosition", ""))
            for key in ("events", "eventsIntroduced"):
                for event in proposed.get(key, []):
                    if event not in bible.get("events", []):
                        bible.setdefault("events", []).append(event)
            resolved = {x.get("key") if isinstance(x, dict) else x for x in proposed.get("foreshadowingResolved", [])}
            for item in bible.get("foreshadowing", []):
                if isinstance(item, dict) and item.get("key") in resolved:
                    item["status"] = "RESOLVED"
                    item["resolvedChapter"] = number
            version = int(
                conn.execute("SELECT MAX(version) FROM story_bibles WHERE novel_id=?", (nid,)).fetchone()[0]
            ) + 1
            conn.execute("INSERT INTO story_bibles VALUES (?,?,?,?)", (nid, version, dumps(bible), now()))
            self._sync_bible(conn, nid, bible)
            conn.execute("UPDATE novels SET story_bible_version=? WHERE id=?", (version, nid))
            conn.execute("UPDATE novels SET current_chapter=MAX(current_chapter,?),updated_at=? WHERE id=?", (number, now(), nid))
            self._prune_bible_versions(conn, nid)
        self.record_audit(nid, "chapter_published", {
            "chapter": number,
            "platform": str(record.get("platform", "")),
            "operator": str(record.get("operator", "")),
        })

    # -- generation persistence ----------------------------------------------

    def save_generation(self, job, output, raw, review, usage, proposed):
        c = self.chapter(job["novel_id"], job["chapter_number"])
        ts = now()
        status = ChapterStatus.WAITING_APPROVAL if review["passed"] else ChapterStatus.FAILED
        conn = self.db
        with self.tx():
            version = int(
                conn.execute(
                    "SELECT COALESCE(MAX(version),0) FROM chapter_drafts WHERE chapter_id=?",
                    (c["id"],),
                ).fetchone()[0]
            ) + 1
            draft_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO chapter_drafts VALUES (?,?,?,?,?,?,?)",
                (draft_id, c["id"], version, dumps(output), raw, dumps(proposed), ts),
            )
            conn.execute(
                "INSERT INTO review_results VALUES (?,?,?,?)",
                (str(uuid.uuid4()), draft_id, dumps(review), ts),
            )
            conn.execute(
                "UPDATE chapters SET status=?,title=?,goal=?,beats=?,content=?,summary=?,"
                "characters=?,events=?,foreshadowing_added=?,foreshadowing_resolved=?,"
                "state_changes=?,hook=?,raw_response=?,review=?,proposed_state=?,model=?,"
                "generated_at=?,updated_at=? WHERE id=?",
                (status, output.get("title", ""), output.get("chapterGoal", ""),
                 dumps(output.get("beats", [])), output.get("content", ""),
                 output.get("summary", output.get("chapterGoal", "")),
                 dumps(output.get("charactersUsed", [])), dumps(output.get("eventsIntroduced", [])),
                 dumps(output.get("foreshadowingAdded", [])), dumps(output.get("foreshadowingResolved", [])),
                 dumps(output.get("stateChanges", [])), output.get("nextChapterHook", ""),
                 raw, dumps(review), dumps(proposed), usage.get("model", ""), ts, ts, c["id"]),
            )
            conn.execute(
                "UPDATE jobs SET status=?,error=?,updated_at=? WHERE id=?",
                ("SUCCEEDED" if review["passed"] else "FAILED",
                 dumps(review.get("blockingIssues", [])), ts, job["id"]),
            )
            conn.execute(
                "INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), job["id"], usage.get("model"), usage.get("prompt_version"),
                 usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                 usage.get("duration_ms", 0), usage.get("request_status"), usage.get("error"), ts),
            )

    def record_usage(self, job, model, prompt_version, status, error=""):
        with self.tx():
            self.db.execute(
                "INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), job["id"], model, prompt_version, 0, 0, 0, status, error, now()),
            )

    # -- usage ---------------------------------------------------------------

    def usage(self, nid):
        return [
            dict(x)
            for x in self.db.execute(
                "SELECT u.* FROM usage u JOIN jobs j ON j.id=u.job_id "
                "WHERE j.novel_id=? ORDER BY u.created_at DESC",
                (nid,),
            )
        ]

    # -- export --------------------------------------------------------------

    def export_job(self, chapter, fmt):
        key = f"{chapter['id']}:{fmt}"
        row = self.db.execute(
            "SELECT * FROM export_jobs WHERE idempotency_key=?", (key,)
        ).fetchone()
        return dict(row) if row else None

    def create_export_job(self, chapter, fmt):
        key = f"{chapter['id']}:{fmt}"
        existing = self.export_job(chapter, fmt)
        if existing:
            return existing, False
        ts = now()
        row = (str(uuid.uuid4()), chapter["id"], fmt, "PENDING", key, "", "", ts, ts)
        conn = self.db
        try:
            with self.tx():
                conn.execute("INSERT INTO export_jobs VALUES (?,?,?,?,?,?,?,?,?)", row)
        except sqlite3.IntegrityError:
            # Lost a concurrent create; hand back the winner.
            winner = self.export_job(chapter, fmt)
            return (winner, False) if winner else (None, False)
        return dict(conn.execute("SELECT * FROM export_jobs WHERE id=?", (row[0],)).fetchone()), True

    def complete_export_job(self, job, path):
        with self.tx():
            self.db.execute(
                "UPDATE export_jobs SET status='SUCCEEDED',path=?,updated_at=? WHERE id=?",
                (str(path), now(), job["id"]),
            )

    # -- observability -------------------------------------------------------

    def record_audit(self, novel_id, action, detail=None):
        """Append one immutable, best-effort audit event.

        The trail exists for operator accountability (who/what/when changed a
        novel). A failed audit write must never fail the primary mutation, so
        sqlite errors here are swallowed.
        """
        try:
            with self.tx():
                self.db.execute(
                    "INSERT INTO audit_log VALUES (?,?,?,?,?)",
                    (str(uuid.uuid4()), novel_id, action, dumps(detail or {}), now()),
                )
        except sqlite3.Error:
            pass

    def audit_trail(self, limit=200):
        limit = max(1, min(int(limit), 1000))
        rows = self.db.execute(
            "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(x) for x in rows]

    def usage_series(self, days=7):
        """Per-day usage counts for the last ``days`` days (zero-filled)."""
        days = max(1, min(int(days), 90))
        today = datetime.now(timezone.utc).date()
        start = (today - timedelta(days=days - 1)).isoformat()
        rows = self.db.execute(
            "SELECT substr(created_at,1,10) AS day, COUNT(*) AS requests, "
            "SUM(CASE WHEN request_status='succeeded' THEN 1 ELSE 0 END) AS succeeded, "
            "COALESCE(SUM(input_tokens),0) AS input_tokens, "
            "COALESCE(SUM(output_tokens),0) AS output_tokens, "
            "COALESCE(SUM(duration_ms),0) AS duration_ms "
            "FROM usage WHERE substr(created_at,1,10) >= ? GROUP BY day ORDER BY day",
            (start,),
        ).fetchall()
        by_day = {r["day"]: r for r in rows}
        series = []
        for offset in range(days):
            day = (today - timedelta(days=days - 1 - offset)).isoformat()
            row = by_day.get(day)
            requests = int(row["requests"]) if row else 0
            succeeded = int(row["succeeded"] or 0) if row else 0
            series.append({
                "date": day,
                "requests": requests,
                "succeeded": succeeded,
                "failed": requests - succeeded,
                "input_tokens": int(row["input_tokens"] or 0) if row else 0,
                "output_tokens": int(row["output_tokens"] or 0) if row else 0,
                "duration_ms": int(row["duration_ms"] or 0) if row else 0,
            })
        return series

    def metrics(self):
        """Snapshot aggregate counters for ops dashboards (read-only)."""
        conn = self.db

        def scalar(sql, args=()):
            row = conn.execute(sql, args).fetchone()
            return int(row[0] or 0) if row else 0

        def grouped(sql, args=()):
            return {r[0]: int(r[1] or 0) for r in conn.execute(sql, args).fetchall()}

        by_model = {}
        for r in conn.execute(
            "SELECT model, COUNT(*) AS c, "
            "SUM(CASE WHEN request_status='succeeded' THEN 1 ELSE 0 END) AS s, "
            "COALESCE(SUM(input_tokens),0) AS it, COALESCE(SUM(output_tokens),0) AS ot "
            "FROM usage GROUP BY model"
        ).fetchall():
            by_model[r["model"] or "unknown"] = {
                "requests": int(r["c"] or 0),
                "succeeded": int(r["s"] or 0),
                "failed": int(r["c"] or 0) - int(r["s"] or 0),
                "input_tokens": int(r["it"] or 0),
                "output_tokens": int(r["ot"] or 0),
            }
        usage_total = scalar("SELECT COUNT(*) FROM usage")
        usage_succeeded = scalar("SELECT COUNT(*) FROM usage WHERE request_status='succeeded'")
        return {
            "generatedAt": now(),
            "novels": {
                "total": scalar("SELECT COUNT(*) FROM novels"),
                "paused": scalar("SELECT COUNT(*) FROM novels WHERE paused=1"),
            },
            "chapters": {
                "total": scalar("SELECT COUNT(*) FROM chapters"),
                "byStatus": grouped("SELECT status, COUNT(*) FROM chapters GROUP BY status"),
            },
            "jobs": {
                "byStatus": grouped("SELECT status, COUNT(*) FROM jobs GROUP BY status"),
            },
            "usage": {
                "requests": {"total": usage_total, "succeeded": usage_succeeded, "failed": usage_total - usage_succeeded},
                "inputTokens": scalar("SELECT COALESCE(SUM(input_tokens),0) FROM usage"),
                "outputTokens": scalar("SELECT COALESCE(SUM(output_tokens),0) FROM usage"),
                "byModel": by_model,
            },
            "exports": {
                "total": scalar("SELECT COUNT(*) FROM export_jobs"),
                "succeeded": scalar("SELECT COUNT(*) FROM export_jobs WHERE status='SUCCEEDED'"),
                "byFormat": grouped("SELECT format, COUNT(*) FROM export_jobs GROUP BY format"),
            },
            "publishes": {
                "total": scalar("SELECT COUNT(*) FROM publish_records"),
                "byPlatform": grouped("SELECT platform, COUNT(*) FROM publish_records GROUP BY platform"),
            },
            "storyBibleVersions": scalar("SELECT COUNT(*) FROM story_bibles"),
            "auditEvents": scalar("SELECT COUNT(*) FROM audit_log"),
        }
