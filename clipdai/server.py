"""Local web app (ClipShip-style): paste a link -> it downloads & clips -> you review -> approve -> auto-posts."""
from __future__ import annotations

import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import pipeline, watch
from .db import DB
from .utils import log, parse_time

UI = (Path(__file__).parent / "ui.html")


def _rows(rs):
    return [dict(r) for r in rs]


class Handler(BaseHTTPRequestHandler):
    s = None  # settings, set by serve()
    server_version = "ClipForge"

    def log_message(self, *a):  # quiet
        pass

    # ----- helpers
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return {}

    def _file(self, path: Path):
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else 0
            end = int(b) if b else size - 1
            end = min(end, size - 1)
            code = 206
        length = max(end - start + 1, 0)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ----- routes
    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/":
            b = UI.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif p == "/api/state":
            db = DB(self.s.db_path)
            self._json({
                "sources": _rows(db.c.execute("SELECT * FROM sources ORDER BY id DESC LIMIT 30")),
                "clips": _rows(db.c.execute("SELECT * FROM clips ORDER BY id DESC LIMIT 200")),
                "jobs": _rows(db.all_jobs()),
                "watches": _rows(db.watches()),
                "config": {"platforms": self.s.platforms, "route": self.s.route, "auto_approve": self.s.auto_approve,
                           "post_times": self.s.post_times, "llm": self.s.llm_provider,
                           "schedule_mode": self.s.schedule_mode},
            })
        elif p.startswith("/media/"):
            self._file(self.s.clips_dir / Path(unquote(p[7:])).name)
        else:
            self.send_error(404)

    def do_POST(self):
        p = urlparse(self.path).path
        d = self._body()
        db = DB(self.s.db_path)
        if p == "/api/add":
            url = (d.get("url") or "").strip()
            if not url:
                return self._json({"error": "paste a link or file path"}, 400)
            st, en = parse_time(d.get("start")), parse_time(d.get("end"))
            row = db.source_by_url(url)
            if row and row["status"] in ("done", "working", "new"):
                return self._json({"error": f"already {row['status']}"}, 409)
            sid = db.add_source(url)
            db.set_source(sid, status="new", error="")
            if st is not None or en is not None:
                Worker.ranges[url] = (st, en)
            return self._json({"ok": True})
        if p == "/api/watch":
            if d.get("remove"):
                db.del_watch(d["url"])
            else:
                db.add_watch(d["url"].strip())
                Worker.watch_now.set()
            return self._json({"ok": True})
        if p.startswith("/api/clip/"):
            cid = int(p.rsplit("/", 1)[1])
            clip = db.clip(cid)
            if not clip:
                return self._json({"error": "no such clip"}, 404)
            fields = {}
            if "title" in d:
                fields["title"] = str(d["title"])[:100]
            if "caption" in d:
                fields["caption"] = str(d["caption"])[:2000]
            if "hashtags" in d:
                tags = [("#" + t.lstrip("#")) for t in str(d["hashtags"]).replace(",", " ").split() if t.strip("#")]
                fields["hashtags"] = json.dumps(tags[:20])
            if fields:
                db.set_clip(cid, **fields)
            act = d.get("action")
            if act == "approve" and clip["status"] != "approved":
                due = pipeline.enqueue(self.s, db, cid)
                return self._json({"ok": True, "due": due.isoformat()})
            if act == "reject":
                db.set_clip(cid, status="rejected")
                for j in db.jobs_for_clip(cid):
                    if j["status"] == "queued":
                        db.set_job(j["id"], status="skipped")
            if act == "post_now":
                for j in db.jobs_for_clip(cid):
                    if j["status"] in ("queued", "failed"):
                        db.set_job(j["id"], status="queued", due=time.time() - 1, attempts=0)
                Worker.post_now.set()
            return self._json({"ok": True})
        self.send_error(404)


class Worker:
    ranges: dict = {}
    watch_now = threading.Event()
    post_now = threading.Event()


def _ingest_loop(s):
    db = DB(s.db_path)
    while True:
        try:
            rows = db.pending_sources()
            for r in rows:
                st, en = Worker.ranges.pop(r["url"], (None, None))
                pipeline.process_source(s, db, r["url"], st, en)
            time.sleep(3)
        except Exception as e:  # never let the loop die
            log(f"[worker] {e}")
            time.sleep(10)


def _post_loop(s):
    db = DB(s.db_path)
    while True:
        try:
            pipeline.run_due_jobs(s, db)
        except Exception as e:
            log(f"[poster] {e}")
        Worker.post_now.wait(30)
        Worker.post_now.clear()


def _watch_loop(s):
    db = DB(s.db_path)
    while True:
        try:
            watch.check_watches(s, db)
        except Exception as e:
            log(f"[watch] {e}")
        Worker.watch_now.wait(s.check_minutes * 60)
        Worker.watch_now.clear()


def start_background(s, ingest=True, post=True, watching=True):
    for fn, on in ((_ingest_loop, ingest), (_post_loop, post), (_watch_loop, watching)):
        if on:
            threading.Thread(target=fn, args=(s,), daemon=True).start()


def serve(s):
    Handler.s = s
    start_background(s)
    srv = ThreadingHTTPServer((s.web_host, s.web_port), Handler)
    log(f"ClipForge is running -> open http://{s.web_host}:{s.web_port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("bye")
