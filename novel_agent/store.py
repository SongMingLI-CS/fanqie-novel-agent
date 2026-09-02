import json
import sqlite3
import uuid
from datetime import timedelta
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import ChapterStatus


def now():
    return datetime.now(timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS novels (id TEXT PRIMARY KEY, title TEXT NOT NULL, volume TEXT DEFAULT '', genre TEXT DEFAULT '', current_chapter INTEGER DEFAULT 0, paused INTEGER DEFAULT 0, story_bible_version INTEGER DEFAULT 1, skill_version TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS story_bibles (novel_id TEXT NOT NULL, version INTEGER NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(novel_id,version), FOREIGN KEY(novel_id) REFERENCES novels(id));
        CREATE TABLE IF NOT EXISTS chapters (id TEXT PRIMARY KEY, novel_id TEXT NOT NULL, number INTEGER NOT NULL, status TEXT NOT NULL, title TEXT DEFAULT '', goal TEXT DEFAULT '', beats TEXT DEFAULT '[]', content TEXT DEFAULT '', summary TEXT DEFAULT '', characters TEXT DEFAULT '[]', events TEXT DEFAULT '[]', foreshadowing_added TEXT DEFAULT '[]', foreshadowing_resolved TEXT DEFAULT '[]', state_changes TEXT DEFAULT '[]', hook TEXT DEFAULT '', raw_response TEXT DEFAULT '', review TEXT DEFAULT '{}', proposed_state TEXT DEFAULT '{}', model TEXT DEFAULT '', generated_at TEXT DEFAULT '', exported_at TEXT DEFAULT '', published_at TEXT DEFAULT '', publish_record TEXT DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(novel_id,number), FOREIGN KEY(novel_id) REFERENCES novels(id));
        CREATE TABLE IF NOT EXISTS characters (novel_id TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(novel_id,key));
        CREATE TABLE IF NOT EXISTS world_rules (novel_id TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(novel_id,key));
        CREATE TABLE IF NOT EXISTS timeline_events (novel_id TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(novel_id,key));
        CREATE TABLE IF NOT EXISTS foreshadowing (novel_id TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(novel_id,key));
        CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, novel_id TEXT NOT NULL, chapter_number INTEGER NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, attempts INTEGER DEFAULT 0, locked_until TEXT, error TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(novel_id,chapter_number,kind,status));
        CREATE TABLE IF NOT EXISTS usage (id TEXT PRIMARY KEY, job_id TEXT, model TEXT, prompt_version TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, duration_ms INTEGER DEFAULT 0, request_status TEXT, error TEXT, created_at TEXT NOT NULL);
        """)
        self._ensure_column('chapters','beats',"TEXT DEFAULT '[]'")
        self.db.commit()

    def close(self):
        self.db.close()

    def _ensure_column(self, table, name, definition):
        columns={row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
        if name not in columns: self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _sync_bible(self, nid, bible):
        mappings=(('characters','characters'),('world_rules','worldRules'),('timeline_events','timeline'),('foreshadowing','foreshadowing'))
        for table,key in mappings:
            values=bible.get(key,[]) or []
            for index,item in enumerate(values):
                item=item if isinstance(item,dict) else {'value':item}; item_key=str(item.get('key') or item.get('name') or item.get('id') or index)
                self.db.execute(f"INSERT INTO {table}(novel_id,key,data) VALUES (?,?,?) ON CONFLICT(novel_id,key) DO UPDATE SET data=excluded.data",(nid,item_key,dumps(item)))

    @contextmanager
    def tx(self):
        try:
            yield
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def create_novel(self, title, bible, genre="", volume=""):
        nid, ts = str(uuid.uuid4()), now()
        with self.tx():
            self.db.execute("INSERT INTO novels VALUES (?,?,?,?,?,?,?,?,?,?)", (nid,title,volume,genre,0,0,1,"novel-writer@1",ts,ts))
            self.db.execute("INSERT INTO story_bibles VALUES (?,?,?,?)", (nid,1,dumps(bible),ts))
            self._sync_bible(nid,bible)
        return self.get_novel(nid)

    def get_novel(self, nid):
        row = self.db.execute("SELECT * FROM novels WHERE id=?", (nid,)).fetchone()
        if not row: return None
        result = dict(row)
        bible = self.db.execute("SELECT content FROM story_bibles WHERE novel_id=? AND version=?", (nid,row["story_bible_version"])).fetchone()
        result["story_bible"] = json.loads(bible[0]) if bible else {}
        return result

    def update_bible(self, nid, content):
        row = self.db.execute("SELECT MAX(version) FROM story_bibles WHERE novel_id=?", (nid,)).fetchone(); version = int(row[0] or 0) + 1
        with self.tx():
            self.db.execute("INSERT INTO story_bibles VALUES (?,?,?,?)", (nid,version,dumps(content),now()))
            self._sync_bible(nid,content)
            self.db.execute("UPDATE novels SET story_bible_version=?,updated_at=? WHERE id=?", (version,now(),nid))
        return version

    def create_job(self, nid, number, kind="generate"):
        novel=self.get_novel(nid)
        if not novel: raise ValueError("novel_not_found")
        if novel['paused']: raise ValueError("novel_is_paused")
        if number < 1: raise ValueError("chapter_number_must_be_positive")
        key = f"{nid}:{number}:{kind}"
        existing = self.db.execute("SELECT * FROM jobs WHERE idempotency_key=?", (key,)).fetchone()
        if existing and existing["status"] not in ("FAILED", "CANCELLED"): return dict(existing), False
        if existing:
            with self.tx(): self.db.execute("UPDATE jobs SET status='PENDING',error='',updated_at=? WHERE id=?", (now(), existing['id']))
            return dict(self.db.execute("SELECT * FROM jobs WHERE id=?", (existing['id'],)).fetchone()), True
        jid = str(uuid.uuid4()); ts = now()
        with self.tx():
            self.db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)", (jid,nid,number,kind,"PENDING",key,0,None,"",ts,ts))
            self.db.execute("INSERT OR IGNORE INTO chapters(id,novel_id,number,status,created_at,updated_at) VALUES (?,?,?,?,?,?)", (str(uuid.uuid4()),nid,number,ChapterStatus.PENDING,ts,ts))
        return dict(self.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()), True

    def claim_job(self, lease_seconds=900):
        self.db.execute("UPDATE jobs SET status='PENDING',locked_until=NULL WHERE status='RUNNING' AND locked_until < ?", (now(),))
        row = self.db.execute("SELECT * FROM jobs WHERE status='PENDING' ORDER BY created_at LIMIT 1").fetchone()
        if not row: return None
        with self.tx():
            lease=(datetime.now(timezone.utc)+timedelta(seconds=lease_seconds)).isoformat()
            self.db.execute("UPDATE jobs SET status='RUNNING',attempts=attempts+1,locked_until=?,updated_at=? WHERE id=? AND status='PENDING'", (lease,now(),row["id"]))
        return dict(self.db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def chapter(self, nid, number):
        row = self.db.execute("SELECT * FROM chapters WHERE novel_id=? AND number=?", (nid,number)).fetchone()
        if not row: return None
        result = dict(row)
        for key in ("beats","characters","events","foreshadowing_added","foreshadowing_resolved","state_changes","review","proposed_state","publish_record"):
            result[key] = json.loads(result[key] or ("{}" if key in ("review","proposed_state","publish_record") else "[]"))
        return result

    def chapters(self, nid):
        return [self.chapter(nid,r[0]) for r in self.db.execute("SELECT number FROM chapters WHERE novel_id=? ORDER BY number",(nid,)).fetchall()]

    def chapter_by_id(self, cid):
        row=self.db.execute("SELECT novel_id,number FROM chapters WHERE id=?",(cid,)).fetchone()
        return self.chapter(row[0],row[1]) if row else None

    def jobs(self,nid): return [dict(x) for x in self.db.execute("SELECT * FROM jobs WHERE novel_id=? ORDER BY created_at DESC",(nid,)).fetchall()]

    def recent(self, nid, limit=3): return self.chapters(nid)[-limit:]

    def save_generation(self, job, output, raw, review, usage, proposed):
        c = self.chapter(job["novel_id"],job["chapter_number"]); ts=now(); status = ChapterStatus.WAITING_APPROVAL if review["passed"] else ChapterStatus.FAILED
        with self.tx():
            self.db.execute("UPDATE chapters SET status=?,title=?,goal=?,beats=?,content=?,summary=?,characters=?,events=?,foreshadowing_added=?,foreshadowing_resolved=?,state_changes=?,hook=?,raw_response=?,review=?,proposed_state=?,model=?,generated_at=?,updated_at=? WHERE id=?", (status,output.get("title",""),output.get("chapterGoal",""),dumps(output.get("beats",[])),output.get("content",""),output.get("chapterGoal",""),dumps(output.get("charactersUsed",[])),dumps(output.get("eventsIntroduced",[])),dumps(output.get("foreshadowingAdded",[])),dumps(output.get("foreshadowingResolved",[])),dumps(output.get("stateChanges",[])),output.get("nextChapterHook",""),raw,dumps(review),dumps(proposed),usage.get("model",""),ts,ts,c["id"]))
            self.db.execute("UPDATE jobs SET status=?,error=?,updated_at=? WHERE id=?", ("SUCCEEDED" if review["passed"] else "FAILED",dumps(review.get("blockingIssues",[])),ts,job["id"]))
            self.db.execute("INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()),job["id"],usage.get("model"),usage.get("prompt_version"),usage.get("input_tokens",0),usage.get("output_tokens",0),usage.get("duration_ms",0),usage.get("request_status"),usage.get("error"),ts))

    def set_status(self,nid,number,status): self.db.execute("UPDATE chapters SET status=?,updated_at=? WHERE novel_id=? AND number=?",(status,now(),nid,number)); self.db.commit()
    def get_job(self,jid):
        row=self.db.execute("SELECT * FROM jobs WHERE id=?",(jid,)).fetchone(); return dict(row) if row else None
    def cancel_job(self,jid): self.db.execute("UPDATE jobs SET status='CANCELLED',updated_at=? WHERE id=? AND status IN ('PENDING','RUNNING')",(now(),jid)); self.db.commit()
    def set_paused(self,nid,paused): self.db.execute("UPDATE novels SET paused=?,updated_at=? WHERE id=?",(int(paused),now(),nid)); self.db.commit()
    def record_export(self,nid,number): self.set_status(nid,number,ChapterStatus.EXPORTED); self.db.execute("UPDATE chapters SET exported_at=? WHERE novel_id=? AND number=?",(now(),nid,number)); self.db.commit()
    def update_draft(self,cid,changes):
        ch=self.chapter_by_id(cid)
        if not ch: raise ValueError('chapter_not_found')
        allowed={k:changes[k] for k in ('title','goal','content','summary','hook') if k in changes}
        if not allowed: raise ValueError('no_editable_fields')
        sets=', '.join(f'{k}=?' for k in allowed); values=list(allowed.values())
        with self.tx():
            self.db.execute(f"UPDATE chapters SET {sets},status='REVIEWING',review='{{}}',updated_at=? WHERE id=?",(*values,now(),cid))
        return self.chapter_by_id(cid)
    def manual_publish(self,nid,number,record):
        with self.tx():
            changed=self.db.execute("UPDATE chapters SET status=?,publish_record=?,published_at=?,updated_at=? WHERE novel_id=? AND number=? AND status='EXPORTED'",(ChapterStatus.PUBLISHED_MANUALLY,dumps(record),record.get("publishedAt",now()),now(),nid,number)).rowcount
            if not changed: raise ValueError("chapter_must_be_exported_before_manual_publish")
            self.db.execute("UPDATE novels SET current_chapter=MAX(current_chapter,?),updated_at=? WHERE id=?",(number,now(),nid))
