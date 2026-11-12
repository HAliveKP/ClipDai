"""download -> transcribe -> pick moments -> render -> (review) -> schedule -> publish"""
from __future__ import annotations

import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import face
from .db import DB
from .downloader import download
from .highlights import find_clips, write_metadata
from .publishers import PublishError, get_publisher
from .publishers.base import build_text
from .render import make_thumbnail, render_clip
from .transcribe import transcribe
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
    db.set_source(sid, status="working", error="")
    try:
        src = download(url, s.work_dir / "src", s.max_height, start, end)
        db.set_source(sid, title=src.title, uploader=src.uploader, video_id=src.id)
        segs, wav = transcribe(src.path, s.work_dir / "asr", s.whisper_model, s.whisper_device)
        if not segs:
            raise RuntimeError("no speech found in this video (nothing to base clips on)")
        ctx = f"'{src.title}' by {src.uploader}".strip()
        clips = find_clips(s, segs, wav, src.duration, ctx, n)
        made = 0
        for i, c in enumerate(clips, 1):
            write_metadata(s, c, segs, ctx, src.uploader, src.url)
            out = s.clips_dir / f"{slug(src.id, 24)}_{i:02d}_{slug(c.title, 30)}.mp4"
            layout, crop_x = s.layout, s.crop_x
            if layout == "face":
                fx = face.detect_crop_x(src.path, c.start, c.end)
                layout, crop_x = ("crop", fx) if fx is not None else ("blur", crop_x)
            log(f"[render] clip {i}/{len(clips)} {c.start:.0f}s-{c.end:.0f}s '{c.title}' ({layout})")
            render_clip(src.path, c, segs, out, layout, crop_x, s.caption_font, s.caption_size)
            make_thumbnail(out, out.with_suffix(".jpg"), min(1.0, c.duration / 2))
            cid = db.add_clip(sid, out, c, "approved" if s.auto_approve else "pending")
            if s.auto_approve:
                enqueue(s, db, cid)
            made += 1
        db.set_source(sid, status="done")
        log(f"[done] {made} clips from '{src.title}'" + ("" if s.auto_approve else " - waiting for your review"))
        return made
    except Exception as e:
        log(f"[error] {url}: {e}")
        db.set_source(sid, status="error", error=str(e)[:500])
        if not isinstance(e, (RuntimeError, ValueError)):
            log(traceback.format_exc())
        return 0


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
