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
CREATE TABLE IF NOT EXISTS presets(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, style TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS split_presets(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, streamer TEXT, boxes TEXT, ratio REAL, created REAL
);
"""

# columns added after the first release; created on open so existing databases keep working
MIGRATIONS = {
    "sources": {"path": "TEXT", "width": "INTEGER", "height": "INTEGER", "duration": "REAL", "progress": "TEXT"},
    "clips": {"edit": "TEXT", "words": "TEXT", "render_status": "TEXT DEFAULT ''", "render_error": "TEXT",
              "version": "INTEGER DEFAULT 0"},
}


class DB:
    def __init__(self, path: Path):
        # one DB() per thread; WAL lets the web UI, worker and poster share the file safely
        self.c = sqlite3.connect(path, timeout=15)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in self.c.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    self.c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
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
    def source(self, sid):
        return self.c.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()

    def add_clip(self, source_id, path, c, status, edit: dict | None = None) -> int:
        cur = self.c.execute(
            "INSERT INTO clips(source_id,path,start,end,score,title,caption,hashtags,reason,status,created,edit)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, str(path), c.start, c.end, c.score, c.title, c.caption,
             json.dumps(c.hashtags), c.reason, status, time.time(), json.dumps(edit) if edit else None))
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

    def render_queue(self):
        return self.c.execute("SELECT * FROM clips WHERE render_status='queued' ORDER BY id").fetchall()

    # ---- caption presets
    def presets(self):
        return self.c.execute("SELECT * FROM presets ORDER BY name").fetchall()

    def save_preset(self, name, style: dict):
        self.c.execute("INSERT INTO presets(name,style,created) VALUES(?,?,?) "
                       "ON CONFLICT(name) DO UPDATE SET style=excluded.style", (name, json.dumps(style), time.time()))
        self.c.commit()

    def del_preset(self, name):
        self.c.execute("DELETE FROM presets WHERE name=?", (name,))
        self.c.commit()

    # ---- split layout presets (per streamer) ----
    def split_presets(self, streamer: str | None = None):
        if streamer:
            return self.c.execute(
                "SELECT * FROM split_presets WHERE streamer=? ORDER BY name", (streamer,)).fetchall()
        return self.c.execute("SELECT * FROM split_presets ORDER BY streamer, name").fetchall()

    def save_split_preset(self, name: str, streamer: str, boxes: dict, ratio: float) -> None:
        self.c.execute(
            "INSERT INTO split_presets(name,streamer,boxes,ratio,created) VALUES(?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET streamer=excluded.streamer, boxes=excluded.boxes, "
            "ratio=excluded.ratio, created=excluded.created",
            (name, streamer, json.dumps(boxes), float(ratio), time.time()))
        self.c.commit()

    def del_split_preset(self, name: str):
        self.c.execute("DELETE FROM split_presets WHERE name=?", (name,))
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

    def job(self, jid):
        return self.c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()

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
