"""download -> transcribe -> pick moments -> render -> (review) -> schedule -> publish"""
from __future__ import annotations

import json
import re
import threading
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import face, layout
from .db import DB
from .downloader import download
from .highlights import find_clips, write_metadata
from .publishers import PublishError, get_publisher
from .publishers.base import build_text
from .render import default_edit, make_thumbnail, probe_size, render_clip, render_edit, words_in
from .transcribe import load_cached, transcribe
from .utils import log, slug


# ------------------------------- scheduling -------------------------------
def _slots_for_day(s, day: datetime) -> list[datetime]:
    out = []
    for t in sorted(s.post_times)[: max(s.max_posts_per_day, 1)]:
        try:
            h, m = (int(x) for x in t.split(":"))
        except ValueError:
            continue
        out.append(day.replace(hour=h, minute=m, second=0, microsecond=0))
    return out


def next_slot(s, db: DB) -> datetime:
    """Next free daily posting slot (local time) after both 'now' and the last queued slot."""
    now = datetime.now().astimezone()
    after = max(now, datetime.fromtimestamp(db.last_due()).astimezone()) if db.last_due() else now
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(400):
        for slot in _slots_for_day(s, day):
            if slot > after + timedelta(seconds=30):
                return slot
        day += timedelta(days=1)
    return now + timedelta(hours=6)


def enqueue(s, db: DB, clip_id: int) -> datetime:
    due = next_slot(s, db)
    for p in s.platforms:
        db.add_job(clip_id, p, due.timestamp())
    db.set_clip(clip_id, status="approved")
    return due


# --------------------------------- ingest ---------------------------------
def process_source(s, db: DB, url: str, start: float | None = None, end: float | None = None,
                   n: int | None = None) -> int:
    """Self-download the URL and turn it into reviewed-or-queued clips. Returns number of clips made."""
    sid = db.add_source(url)
    db.set_source(sid, status="working", error="", progress="starting")
    last = {"msg": ""}
    owner = threading.get_ident()
    conns = threading.local()

    def progress(msg: str) -> None:
        # yt-dlp calls this from its own download threads; sqlite connections are per-thread, so use one per thread
        if msg == last["msg"]:
            return
        last["msg"] = msg
        try:
            if threading.get_ident() == owner:
                conn = db
            else:
                conn = getattr(conns, "db", None) or DB(s.db_path)
                conns.db = conn
            conn.set_source(sid, progress=msg)
        except Exception:  # a progress update must never break the download
            pass

    try:
        src = download(url, s.work_dir / "src", s.max_height, start, end, progress=progress)
        sw, sh = probe_size(src.path)
        db.set_source(sid, title=src.title, uploader=src.uploader, video_id=src.id, path=str(src.path),
                      width=sw, height=sh, duration=src.duration)
        progress("transcribing")
        segs, wav = transcribe(src.path, s.work_dir / "asr", s.whisper_model, s.whisper_device, progress=progress)
        if not segs:
            raise RuntimeError("no speech found in this video (nothing to base clips on)")
        ctx = f"'{src.title}' by {src.uploader}".strip()
        progress("finding highlights")
        clips = find_clips(s, segs, wav, src.duration, ctx, n)
        made = 0
        for i, c in enumerate(clips, 1):
            write_metadata(s, c, segs, ctx, src.uploader, src.url)
            out = s.clips_dir / f"{slug(src.id, 24)}_{i:02d}_{slug(c.title, 30)}.mp4"
            lay, crop_x = s.layout, s.crop_x
            if lay == "face":
                fx = face.detect_crop_x(src.path, c.start, c.end)
                lay, crop_x = ("crop", fx) if fx is not None else ("blur", crop_x)
            progress(f"rendering {i}/{len(clips)}")
            log(f"[render] clip {i}/{len(clips)} {c.start:.0f}s-{c.end:.0f}s '{c.title}' ({lay})")
            edit = default_edit(lay, crop_x, s.caption_font, s.caption_size)
            render_clip(src.path, c, segs, out, edit=edit)
            make_thumbnail(out, out.with_suffix(".jpg"), min(1.0, c.duration / 2))
            cid = db.add_clip(sid, out, c, "approved" if s.auto_approve else "pending", edit)
            if s.auto_approve:
                enqueue(s, db, cid)
            made += 1
        db.set_source(sid, status="done", progress="")
        log(f"[done] {made} clips from '{src.title}'" + ("" if s.auto_approve else " - waiting for your review"))
        return made
    except Exception as e:
        log(f"[error] {url}: {e}")
        db.set_source(sid, status="error", error=str(e)[:500], progress="")
        if not isinstance(e, (RuntimeError, ValueError)):
            log(traceback.format_exc())
        return 0


# ------------------------------- editor support -------------------------------
def source_media(s, db: DB, src_row) -> tuple[Path | None, int, int, float]:
    """(path, width, height, duration) of a clip's source video. Fills in rows made before these columns existed."""
    path = Path(src_row["path"]) if src_row["path"] else None
    if not path or not path.exists():
        path = None
        vid = src_row["video_id"] or ""
        local = Path(src_row["url"]).expanduser()
        if local.is_file():
            path = local.resolve()
        elif vid:
            hits = sorted((s.work_dir / "src").glob(f"{vid}*.mp4"), key=lambda p: len(p.name))
            path = hits[0] if hits else None
        if path is None:
            return None, 0, 0, 0.0
        db.set_source(src_row["id"], path=str(path))
    w, h, d = src_row["width"] or 0, src_row["height"] or 0, src_row["duration"] or 0.0
    if not (w and h and d):
        from .downloader import _probe_duration
        w, h = probe_size(path)
        d = _probe_duration(path)
        db.set_source(src_row["id"], width=w, height=h, duration=d)
    return path, w, h, d


def transcript_words(s, src_path: Path, a: float, b: float) -> list[list]:
    segs = load_cached(src_path, s.work_dir / "asr") or []
    return [[round(w[0], 3), round(w[1], 3), w[2].strip()] for w in words_in(segs, a, b)]


def clip_edit(s, clip) -> dict:
    if clip["edit"]:
        try:
            return layout.normalize_edit(json.loads(clip["edit"]))
        except ValueError:
            pass
    return default_edit(s.layout, s.crop_x, s.caption_font, s.caption_size)


def clip_words(s, clip, src_path: Path) -> list[list]:
    if clip["words"]:
        try:
            return json.loads(clip["words"])
        except ValueError:
            pass
    return transcript_words(s, src_path, clip["start"], clip["end"])


def export_clip(s, db: DB, clip_id: int) -> Path:
    """Re-render a clip from its saved edit state. The new file gets a fresh name (cache-busting) and replaces the
    old one, so anything queued for posting automatically uploads the edited version."""
    clip = db.clip(clip_id)
    src_row = db.source(clip["source_id"])
    path, w, h, _ = source_media(s, db, src_row)
    if path is None:
        raise RuntimeError("the source video is no longer on disk; re-add the link to download it again")
    edit = clip_edit(s, clip)
    words = clip_words(s, clip, path)
    old = Path(clip["path"])
    ver = (clip["version"] or 0) + 1
    base = re.sub(r"_v\d+$", "", old.stem)
    out = old.with_name(f"{base}_v{ver}.mp4")
    render_edit(path, (w, h), clip["start"], clip["end"], words, edit, out)
    make_thumbnail(out, out.with_suffix(".jpg"), min(1.0, (clip["end"] - clip["start"]) / 2))
    db.set_clip(clip_id, path=str(out), version=ver, render_status="", render_error="")
    if old != out:
        for f in (old, old.with_suffix(".jpg")):
            try:
                f.unlink()
            except OSError:
                pass
    return out


def process_pending(s, db: DB) -> int:
    total = 0
    for row in db.pending_sources():
        total += process_source(s, db, row["url"])
    return total


# -------------------------------- publishing --------------------------------
def run_due_jobs(s, db: DB, now: datetime | None = None) -> int:
    """Publish everything that's due. In SCHEDULE_MODE=provider, hand future slots to the provider instead."""
    now = now or datetime.now().astimezone()
    horizon = now.timestamp() + (400 * 86400 if s.schedule_mode == "provider" else 0)
    sent = 0
    for job in db.due_jobs(horizon):
        clip = db.clip(job["clip_id"])
        if clip is None or clip["status"] == "rejected":
            db.set_job(job["id"], status="skipped")
            continue
        src = db.c.execute("SELECT * FROM sources WHERE id=?", (clip["source_id"],)).fetchone()
        due = datetime.fromtimestamp(job["due"]).astimezone()
        schedule_at = due if (s.schedule_mode == "provider" and due > now + timedelta(minutes=2)) else None
        try:
            pub = get_publisher(s, job["platform"])
            text = build_text(s, clip, src, job["platform"])
            res = pub.publish(job["platform"], Path(clip["path"]), text, schedule_at)
            db.set_job(job["id"], status="done", result=res)
            log(f"[publish] clip {clip['id']} -> {job['platform']} via {pub.name}: {res}")
            sent += 1
        except (PublishError, Exception) as e:  # noqa: BLE001
            attempts = job["attempts"] + 1
            final = attempts >= 3
            db.set_job(job["id"], status="failed" if final else "queued", attempts=attempts, result=str(e)[:400],
                       **({} if final else {"due": (now + timedelta(minutes=10 * attempts)).timestamp()}))
            log(f"[publish] clip {clip['id']} -> {job['platform']} FAILED (attempt {attempts}): {e}")
    return sent
