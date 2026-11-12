"""Local web app: paste a link -> it downloads & clips -> review -> edit (framing + captions) -> export -> auto-post."""
from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import fontmetrics, layout, pipeline, watch
from .db import DB
from .utils import log, parse_time

UI = (Path(__file__).parent / "ui.html")
CHUNK = 4 << 20  # bytes per open-ended Range response


def _rows(rs):
    return [dict(r) for r in rs]


class ApiError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def _time(v, name):
    try:
        return parse_time(v)
    except (TypeError, ValueError):
        raise ApiError(f"{name} time '{v}' is not valid; use seconds or h:mm:ss") from None


class Handler(BaseHTTPRequestHandler):
    s = None  # settings, set by serve()
    server_version = "ClipForge"

    def log_message(self, fmt, *a):  # quiet unless CLIPDAI_HTTP_LOG=1
        if os.environ.get("CLIPDAI_HTTP_LOG"):
            log(f"[http] {fmt % a} range={self.headers.get('Range')}")

    # ----- helpers
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            d = json.loads(self.rfile.read(n))
        except ValueError:
            raise ApiError("request body is not valid JSON") from None
        if not isinstance(d, dict):
            raise ApiError("request body must be a JSON object")
        return d

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
            try:
                if a:
                    # open-ended ranges ("bytes=N-") get a bounded chunk: streaming the rest of a multi-GB file
                    # would pin one of the browser's ~6 connections per host and starve every other request
                    start, end = int(a), (int(b) if b else int(a) + CHUNK - 1)
                elif b:  # suffix range: last N bytes
                    start, end = max(0, size - int(b)), size - 1
            except ValueError:
                start, end = 0, size - 1
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
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
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    # ----- routes
    def do_GET(self):
        p = urlparse(self.path).path
        try:
            if p == "/":
                b = UI.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b)
            elif p == "/api/state":
                db = DB(self.s.db_path)
                self._json({
                    "sources": _rows(db.c.execute("SELECT * FROM sources ORDER BY id DESC LIMIT 30")),
                    "clips": _rows(db.c.execute("SELECT * FROM clips ORDER BY id DESC LIMIT 200")),
                    "jobs": _rows(db.all_jobs()),
                    "watches": _rows(db.watches()),
                    "config": {"platforms": self.s.platforms, "route": self.s.route,
                               "auto_approve": self.s.auto_approve, "post_times": self.s.post_times,
                               "llm": self.s.llm_provider, "schedule_mode": self.s.schedule_mode},
                })
            elif p == "/api/meta":
                db = DB(self.s.db_path)
                self._json(self._meta(db))
            elif p.startswith("/api/editor/"):
                db = DB(self.s.db_path)
                self._json(self._editor(db, self._id(p)))
            elif p.startswith("/media/"):
                self._file(self.s.clips_dir / Path(unquote(p[7:])).name)
            elif p.startswith("/source/"):
                db = DB(self.s.db_path)
                row = db.source(self._id(p))
                if not row:
                    return self.send_error(404)
                path, *_ = pipeline.source_media(self.s, db, row)
                if path is None:
                    return self.send_error(404)
                self._file(path)
            else:
                self.send_error(404)
        except ApiError as e:
            self._json({"error": str(e)}, e.code)
        except Exception as e:  # noqa: BLE001
            log(f"[web] GET {p}: {e}\n{traceback.format_exc()}")
            self._json({"error": f"server error: {e}"}, 500)

    @staticmethod
    def _id(p: str) -> int:
        try:
            return int(p.rstrip("/").rsplit("/", 1)[1])
        except (ValueError, IndexError):
            raise ApiError("bad id in URL", 404) from None

    def _meta(self, db):
        return {
            "aspects": layout.ASPECTS,
            "safe_zones": layout.SAFE_ZONES,
            "generic_margin": layout.GENERIC_MARGIN,
            "fonts": fontmetrics.catalog(),
            "default_style": layout.DEFAULT_STYLE,
            "builtin_presets": {k: layout.normalize_style({**layout.DEFAULT_STYLE, **v})
                                for k, v in layout.BUILTIN_PRESETS.items()},
            "presets": {r["name"]: json.loads(r["style"]) for r in db.presets()},
            "consts": {"safety": layout.SAFETY, "unknown_ascii": layout.UNKNOWN_ASCII,
                       "unknown_other": layout.UNKNOWN_OTHER},
        }

    def _editor(self, db, cid):
        clip = db.clip(cid)
        if not clip:
            raise ApiError("no such clip", 404)
        src = db.source(clip["source_id"])
        path, w, h, dur = pipeline.source_media(self.s, db, src) if src else (None, 0, 0, 0)
        if path is None:
            raise ApiError("The original video for this clip is no longer on disk, so it can't be re-edited. "
                           "Paste the link again to re-download it.", 409)
        a, b = max(0.0, clip["start"] - 60), min(dur or clip["end"] + 60, clip["end"] + 60)
        return {
            "clip": dict(clip),
            "edit": pipeline.clip_edit(self.s, clip),
            "words": json.loads(clip["words"]) if clip["words"] else None,
            "transcript": pipeline.transcript_words(self.s, path, a, b),
            "source": {"id": src["id"], "title": src["title"], "width": w, "height": h, "duration": dur,
                       "media": f"/source/{src['id']}"},
        }

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            d = self._body()
            db = DB(self.s.db_path)
            res = self._post(p, d, db)
            if res is None:
                return self.send_error(404)
            self._json(res)
        except ApiError as e:
            self._json({"error": str(e)}, e.code)
        except Exception as e:  # noqa: BLE001
            log(f"[web] POST {p}: {e}\n{traceback.format_exc()}")
            self._json({"error": f"server error: {e}"}, 500)

    def _post(self, p, d, db):
        if p == "/api/add":
            url = str(d.get("url") or "").strip()
            if not url:
                raise ApiError("Paste a video link or a file path first.")
            st, en = _time(d.get("start"), "Start"), _time(d.get("end"), "End")
            if st is not None and en is not None and en <= st:
                raise ApiError("End time must be after the start time.")
            row = db.source_by_url(url)
            if row and row["status"] in ("working", "new"):
                raise ApiError("This link is already being processed.", 409)
            if row and row["status"] == "done" and not d.get("again"):
                return {"ok": False, "duplicate": True,
                        "error": "This link was already clipped. Process it again to get a fresh set of clips?"}
            sid = db.add_source(url)
            db.set_source(sid, status="new", error="", progress="queued")
            if st is not None or en is not None:
                Worker.ranges[url] = (st, en)
            return {"ok": True}
        if p.startswith("/api/source/"):
            row = db.source(self._id(p))
            if not row:
                raise ApiError("no such source", 404)
            if d.get("action") == "retry":
                if row["status"] in ("working", "new"):
                    raise ApiError("It's already being processed.", 409)
                db.set_source(row["id"], status="new", error="", progress="queued")
                return {"ok": True}
            raise ApiError("unknown action")
        if p == "/api/watch":
            url = str(d.get("url") or "").strip()
            if not url:
                raise ApiError("Paste a channel link first.")
            if d.get("remove"):
                db.del_watch(url)
                return {"ok": True}
            if not url.startswith(("http://", "https://")):
                raise ApiError("A channel link must start with http:// or https://")
            db.add_watch(url)
            Worker.watch_now.set()
            return {"ok": True}
        if p.startswith("/api/job/"):
            job = db.job(self._id(p))
            if not job:
                raise ApiError("no such job", 404)
            act = d.get("action")
            if act == "retry":
                if job["status"] not in ("failed", "skipped"):
                    raise ApiError(f"Only failed or cancelled posts can be retried (this one is {job['status']}).")
                db.set_job(job["id"], status="queued", due=time.time() - 1, attempts=0, result="")
                Worker.post_now.set()
                return {"ok": True}
            if act == "cancel":
                if job["status"] != "queued":
                    raise ApiError(f"Only queued posts can be cancelled (this one is {job['status']}).")
                db.set_job(job["id"], status="skipped", result="cancelled")
                return {"ok": True}
            raise ApiError("unknown action")
        if p == "/api/presets":
            name = str(d.get("name") or "").strip()[:40]
            if not name:
                raise ApiError("Give the preset a name.")
            if name in layout.BUILTIN_PRESETS:
                raise ApiError("That name is used by a built-in preset; pick another.")
            if d.get("delete"):
                db.del_preset(name)
            else:
                db.save_preset(name, layout.normalize_style(d.get("style")))
            return {"ok": True, "presets": {r["name"]: json.loads(r["style"]) for r in db.presets()}}
        if p == "/api/presets/apply":
            style = layout.normalize_style(d.get("style"))
            ids = [r["id"] for r in db.c.execute("SELECT id FROM clips WHERE status!='rejected'")]
            for cid in ids:
                clip = db.clip(cid)
                e = pipeline.clip_edit(self.s, clip)
                e["captions"] = {**style, "position": style["position"], "offset": style["offset"]}
                fields = {"edit": json.dumps(e)}
                if d.get("export"):
                    fields.update(render_status="queued", render_error="")
                db.set_clip(cid, **fields)
            if d.get("export"):
                Worker.render_now.set()
            return {"ok": True, "count": len(ids)}
        if p.startswith("/api/clip/"):
            return self._clip_action(db, self._id(p), d)
        return None

    def _clip_action(self, db, cid, d):
        clip = db.clip(cid)
        if not clip:
            raise ApiError("no such clip", 404)
        fields = {}
        if "title" in d:
            fields["title"] = str(d["title"])[:100]
        if "caption" in d:
            fields["caption"] = str(d["caption"])[:2000]
        if "hashtags" in d:
            tags = [("#" + t.lstrip("#")) for t in str(d["hashtags"]).replace(",", " ").split() if t.strip("#")]
            fields["hashtags"] = json.dumps(tags[:20])
        if "edit" in d:
            fields["edit"] = json.dumps(layout.normalize_edit(d["edit"]))
        if "words" in d:
            if d["words"] is None:
                fields["words"] = None
            else:
                try:
                    ws = [[round(float(s), 3), round(float(e), 3), str(t)[:60]] for s, e, t in d["words"]]
                except (TypeError, ValueError):
                    raise ApiError("caption words must be [start, end, text] rows") from None
                fields["words"] = json.dumps(sorted([w for w in ws if w[1] > w[0] and w[2].strip()]))
        if "start" in d or "end" in d:
            st = _time(d.get("start", clip["start"]), "Start")
            en = _time(d.get("end", clip["end"]), "End")
            if st is None or en is None or en - st < 1:
                raise ApiError("A clip must be at least 1 second long.")
            if en - st > 600:
                raise ApiError("A clip can be at most 10 minutes long.")
            fields["start"], fields["end"] = max(0.0, st), en
        if fields:
            db.set_clip(cid, **fields)
        act = d.get("action")
        if act == "approve":
            if clip["status"] == "approved":
                return {"ok": True, "message": "Already approved."}
            due = pipeline.enqueue(self.s, db, cid)
            return {"ok": True, "due": due.timestamp()}
        if act == "reject":
            db.set_clip(cid, status="rejected")
            for j in db.jobs_for_clip(cid):
                if j["status"] == "queued":
                    db.set_job(j["id"], status="skipped", result="clip rejected")
            return {"ok": True}
        if act == "restore":
            db.set_clip(cid, status="pending")
            return {"ok": True}
        if act == "post_now":
            if clip["status"] != "approved":
                raise ApiError("Approve the clip before posting it.")
            n = 0
            for j in db.jobs_for_clip(cid):
                if j["status"] in ("queued", "failed"):
                    db.set_job(j["id"], status="queued", due=time.time() - 1, attempts=0)
                    n += 1
            if not n:
                raise ApiError("Nothing left to post for this clip: every platform is already done or cancelled.")
            Worker.post_now.set()
            return {"ok": True, "count": n}
        if act == "export":
            if clip["render_status"] in ("queued", "rendering"):
                return {"ok": True, "message": "Already exporting."}
            db.set_clip(cid, render_status="queued", render_error="")
            Worker.render_now.set()
            return {"ok": True}
        return {"ok": True}


class Worker:
    ranges: dict = {}
    watch_now = threading.Event()
    post_now = threading.Event()
    render_now = threading.Event()


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


def _render_loop(s):
    db = DB(s.db_path)
    # a crash/restart mid-render leaves rows in 'rendering': put them back in the queue
    db.c.execute("UPDATE clips SET render_status='queued' WHERE render_status='rendering'")
    db.c.commit()
    while True:
        try:
            for r in db.render_queue():
                db.set_clip(r["id"], render_status="rendering")
                try:
                    out = pipeline.export_clip(s, db, r["id"])
                    log(f"[export] clip {r['id']} -> {out.name}")
                except Exception as e:  # noqa: BLE001
                    log(f"[export] clip {r['id']} failed: {e}")
                    db.set_clip(r["id"], render_status="error", render_error=str(e)[:500])
        except Exception as e:  # noqa: BLE001
            log(f"[export] {e}")
        Worker.render_now.wait(5)
        Worker.render_now.clear()


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


def start_background(s, ingest=True, post=True, watching=True, rendering=True):
    for fn, on in ((_ingest_loop, ingest), (_post_loop, post), (_watch_loop, watching), (_render_loop, rendering)):
        if on:
            threading.Thread(target=fn, args=(s,), daemon=True).start()


def serve(s):
    Handler.s = s
    # a server restart interrupts any in-flight ingest: let those sources run again
    db = DB(s.db_path)
    db.c.execute("UPDATE sources SET status='new', progress='queued' WHERE status='working'")
    db.c.commit()
    start_background(s)
    srv = ThreadingHTTPServer((s.web_host, s.web_port), Handler)
    log(f"ClipForge is running -> open http://{s.web_host}:{s.web_port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("bye")
