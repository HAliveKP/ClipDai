"""Postiz (open source, self-hostable): https://github.com/gitroomhq/postiz-app
Connect your accounts once in the Postiz UI, then ClipForge posts through its public API.
Rate limit per their docs: 30 requests/hour (each clip uses ~3: list channels, upload, create post)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import requests

from .base import PublishError, Publisher


class PostizPublisher(Publisher):
    name = "postiz"
    _integrations: list | None = None

    def _h(self) -> dict:
        if not self.s.postiz_key:
            raise PublishError("set POSTIZ_API_KEY in .env (Postiz > Settings > Public API)")
        return {"Authorization": self.s.postiz_key}

    def _channels(self) -> list:
        if PostizPublisher._integrations is None:
            r = requests.get(f"{self.s.postiz_url}/integrations", headers=self._h(), timeout=60)
            if r.status_code >= 400:
                raise PublishError(f"Postiz /integrations {r.status_code}: {r.text[:200]}")
            PostizPublisher._integrations = r.json()
        return PostizPublisher._integrations

    def _find(self, platform: str) -> dict:
        for ch in self._channels():
            if ch.get("disabled"):
                continue
            ident = str(ch.get("identifier", ""))
            if ident == platform or (platform == "instagram" and ident.startswith("instagram")):
                return ch
        raise PublishError(f"no connected '{platform}' channel in Postiz; connect it in the Postiz UI first")

    def publish(self, platform: str, video: Path, text: dict, schedule_at: datetime | None = None) -> str:
        s = self.s
        ch = self._find(platform)
        with open(video, "rb") as fh:
            up = requests.post(f"{s.postiz_url}/upload", headers=self._h(),
                               files={"file": (video.name, fh, "video/mp4")}, timeout=900)
        if up.status_code >= 400:
            raise PublishError(f"Postiz upload {up.status_code}: {up.text[:200]}")
        media = up.json()

        if platform == "youtube":
            settings = {"__type": "youtube", "title": text["title"][:100],
                        "type": s.youtube_privacy if s.youtube_privacy in ("public", "private", "unlisted") else "private",
                        "selfDeclaredMadeForKids": "no",
                        "tags": [{"value": t, "label": t} for t in text["tags"][:10]]}
        elif platform == "tiktok":
            settings = {"__type": "tiktok", "title": text["title"][:2000], "privacy_level": s.tiktok_privacy,
                        "duet": True, "stitch": True, "comment": True, "autoAddMusic": "no",
                        "brand_content_toggle": False, "brand_organic_toggle": False,
                        "video_made_with_ai": False, "content_posting_method": "DIRECT_POST"}
        else:
            settings = {"__type": ch.get("identifier", "instagram"), "post_type": "post",
                        "is_trial_reel": False, "collaborators": []}

        now_ = schedule_at is None
        when = (schedule_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        body = {
            "type": "now" if now_ else "schedule",
            "date": when.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "shortLink": False, "tags": [],
            "posts": [{
                "integration": {"id": ch["id"]},
                "value": [{"content": text["description"],
                           "image": [{"id": media.get("id"), "path": media.get("path")}]}],
                "settings": settings,
            }],
        }
        r = requests.post(f"{s.postiz_url}/posts", headers={**self._h(), "Content-Type": "application/json"},
                          json=body, timeout=120)
        if r.status_code >= 400:
            raise PublishError(f"Postiz /posts {r.status_code}: {r.text[:300]}")
        return "queued in Postiz" + ("" if now_ else f" for {when:%Y-%m-%d %H:%M} UTC")
