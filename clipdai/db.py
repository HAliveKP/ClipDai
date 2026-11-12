"""SQLite state: sources (videos), clips, and per-platform publish jobs."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY, url TEXT UNIQUE, title TEXT, uploader TEXT, video_id TEXT,
  status TEXT DEFAULT 'new', error TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS clips(
  id INTEGER PRIMARY KEY, source_id INTEGER, path TEXT, start REAL, end REAL, score REAL,
  title TEXT, caption TEXT, hashtags TEXT, reason TEXT,
  status TEXT DEFAULT 'pending',   -- pending (needs approval) | approved | rejected
  created REAL
);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, clip_id INTEGER, platform TEXT, status TEXT DEFAULT 'queued',
  due REAL, result TEXT, attempts INTEGER DEFAULT 0, updated REAL,
  UNIQUE(clip_id, platform)
);
CREATE TABLE IF NOT EXISTS watches(
  id INTEGER PRIMARY KEY, url TEXT UNIQUE, last_checked REAL
);
"""


class DB:
    def __init__(self, path: Path):
        # one DB() per thread; WAL lets the web UI, worker and poster share the file safely
        self.c = sqlite3.connect(path, timeout=15)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.executescript(SCHEMA)
        self.c.commit()

    # ---- sources
    def add_source(self, url, title="", uploader="", video_id="") -> int:
        cur = self.c.execute(
            "INSERT OR IGNORE INTO sources(url,title,uploader,video_id,created) VALUES(?,?,?,?,?)",
            (url, title, uploader, video_id, time.time()))
        self.c.commit()
        if cur.lastrowid and cur.rowcount:
            return cur.lastrowid
        return self.c.execute("SELECT id FROM sources WHERE url=?", (url,)).fetchone()["id"]

    def set_source(self, sid, **kw):
        sets = ",".join(f"{k}=?" for k in kw)
        self.c.execute(f"UPDATE sources SET {sets} WHERE id=?", (*kw.values(), sid))
        self.c.commit()

    def source_by_url(self, url):
        return self.c.execute("SELECT * FROM sources WHERE url=?", (url,)).fetchone()

    def pending_sources(self):
        return self.c.execute("SELECT * FROM sources WHERE status='new' ORDER BY id").fetchall()

    # ---- clips
    def add_clip(self, source_id, path, c, status) -> int:
        cur = self.c.execute(
            "INSERT INTO clips(source_id,path,start,end,score,title,caption,hashtags,reason,status,created)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, str(path), c.start, c.end, c.score, c.title, c.caption,
             json.dumps(c.hashtags), c.reason, status, time.time()))
        self.c.commit()
        return cur.lastrowid

    def clip(self, cid):
        return self.c.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()

    def clips(self, status=None):
        if status:
            return self.c.execute("SELECT * FROM clips WHERE status=? ORDER BY id", (status,)).fetchall()
        return self.c.execute("SELECT * FROM clips ORDER BY id").fetchall()

    def set_clip(self, cid, **kw):
        sets = ",".join(f"{k}=?" for k in kw)
        self.c.execute(f"UPDATE clips SET {sets} WHERE id=?", (*kw.values(), cid))
        self.c.commit()

    # ---- jobs
    def add_job(self, clip_id, platform, due) -> None:
        self.c.execute("INSERT OR IGNORE INTO jobs(clip_id,platform,due,updated) VALUES(?,?,?,?)",
                       (clip_id, platform, due, time.time()))
        self.c.commit()

    def due_jobs(self, now):
        return self.c.execute(
            "SELECT * FROM jobs WHERE status='queued' AND due<=? ORDER BY due", (now,)).fetchall()

    def set_job(self, jid, **kw):
        kw["updated"] = time.time()
        sets = ",".join(f"{k}=?" for k in kw)
        self.c.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*kw.values(), jid))
        self.c.commit()

    def jobs_for_clip(self, cid):
        return self.c.execute("SELECT * FROM jobs WHERE clip_id=?", (cid,)).fetchall()

    def posted_since(self, ts) -> int:
        """Distinct clips that had at least one successful post since ts."""
        r = self.c.execute(
            "SELECT COUNT(DISTINCT clip_id) n FROM jobs WHERE status='done' AND updated>=?", (ts,)).fetchone()
        return int(r["n"])

    def last_due(self):
        r = self.c.execute("SELECT MAX(due) m FROM jobs").fetchone()
        return r["m"] or 0

    def all_jobs(self):
        return self.c.execute("SELECT * FROM jobs ORDER BY due").fetchall()

    # ---- watches
    def add_watch(self, url):
        self.c.execute("INSERT OR IGNORE INTO watches(url,last_checked) VALUES(?,0)", (url,))
        self.c.commit()

    def watches(self):
        return self.c.execute("SELECT * FROM watches").fetchall()

    def del_watch(self, url):
        self.c.execute("DELETE FROM watches WHERE url=?", (url,))
        self.c.commit()

    def touch_watch(self, url):
        self.c.execute("UPDATE watches SET last_checked=? WHERE url=?", (time.time(), url))
        self.c.commit()
