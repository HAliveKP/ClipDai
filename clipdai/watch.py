"""Self-download: watch Twitch / YouTube / Kick channel pages and grab new VODs automatically."""
from __future__ import annotations

from .db import DB
from .utils import log


def list_latest(url: str, limit: int = 5) -> list[dict]:
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "playlistend": limit,
            "skip_download": True, "ignoreerrors": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        return []
    entries = info.get("entries") or [info]
    out = []
    for e in entries:
        if not e:
            continue
        u = e.get("url") or e.get("webpage_url")
        if not u or not u.startswith("http"):
            continue
        if e.get("live_status") in ("is_live", "is_upcoming"):
            continue
        out.append({"url": u, "title": e.get("title") or "", "duration": e.get("duration")})
    return out


def normalize_watch_url(url: str) -> str:
    """Turn a bare channel link into its VOD list page."""
    u = url.rstrip("/")
    if "twitch.tv/" in u and not any(x in u for x in ("/videos", "/video/", "/clip")):
        return u + "/videos?filter=archives&sort=time"
    if "youtube.com/" in u and any(x in u for x in ("/@", "/channel/", "/c/", "/user/")) \
            and not any(x in u for x in ("/videos", "/streams", "/shorts", "/watch")):
        return u + "/streams"
    return u


def check_watches(s, db: DB, min_seconds: int = 600) -> int:
    """Queue unseen VODs. First time a channel is seen, only the newest WATCH_BACKFILL are processed."""
    queued = 0
    for w in db.watches():
        first = not w["last_checked"]
        try:
            items = list_latest(normalize_watch_url(w["url"]), limit=max(s.watch_backfill, 5))
        except Exception as e:
            log(f"[watch] {w['url']}: {e}")
            continue
        fresh = [i for i in items if not db.source_by_url(i["url"])]
        if first:
            keep, skip = fresh[: s.watch_backfill], fresh[s.watch_backfill:]
            for i in skip:
                sid = db.add_source(i["url"], i["title"])
                db.set_source(sid, status="skipped")
        else:
            keep = fresh
        for i in keep:
            if i["duration"] and i["duration"] < min_seconds:
                sid = db.add_source(i["url"], i["title"])
                db.set_source(sid, status="skipped")
                continue
            db.add_source(i["url"], i["title"])
            log(f"[watch] new video queued: {i['title'] or i['url']}")
            queued += 1
        db.touch_watch(w["url"])
    return queued
