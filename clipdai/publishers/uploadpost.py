"""Upload-Post (https://upload-post.com): one API key posts to TikTok / Instagram / YouTube.
NOTE: per their docs, TikTok is NOT available on the Free plan; YouTube + Instagram are."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import requests

from .base import PublishError, Publisher

URL = "https://api.upload-post.com/api/upload"


class UploadPostPublisher(Publisher):
    name = "uploadpost"

    def publish(self, platform: str, video: Path, text: dict, schedule_at: datetime | None = None) -> str:
        s = self.s
        if not s.uploadpost_key or not s.uploadpost_user:
            raise PublishError("set UPLOADPOST_API_KEY and UPLOADPOST_USER in .env")
        data: list[tuple[str, str]] = [
            ("user", s.uploadpost_user),
            ("platform[]", platform),
            ("title", text["title"]),
            ("description", text["description"]),
        ]
        if platform == "youtube":
            data += [("youtube_title", text["title"]), ("youtube_description", text["description"]),
                     ("privacyStatus", "public" if s.youtube_privacy == "public" else s.youtube_privacy),
                     ("categoryId", s.youtube_category), ("selfDeclaredMadeForKids", "false")]
            data += [("tags[]", t) for t in text["tags"]]
        elif platform == "tiktok":
            data += [("tiktok_title", text["title"]), ("privacy_level", s.tiktok_privacy),
                     ("post_mode", "DIRECT_POST")]
        elif platform == "instagram":
            data += [("instagram_title", text["title"])]
        if schedule_at is not None:
            when = schedule_at.astimezone(timezone.utc)
            data += [("scheduled_date", when.strftime("%Y-%m-%dT%H:%M:%SZ")), ("timezone", "UTC")]
        headers = {"Authorization": f"Apikey {s.uploadpost_key}", "Idempotency-Key": f"{video.stem}-{platform}"}
        with open(video, "rb") as fh:
            r = requests.post(URL, headers=headers, data=data, files={"video": (video.name, fh, "video/mp4")},
                              timeout=900)
        try:
            j = r.json()
        except ValueError:
            raise PublishError(f"Upload-Post {r.status_code}: {r.text[:300]}")
        if r.status_code >= 400 or not j.get("success", False):
            raise PublishError(f"Upload-Post {r.status_code}: {j.get('message') or j.get('error') or j}")
        res = (j.get("results") or {}).get(platform)
        if res is not None and not res.get("success", False):
            raise PublishError(f"Upload-Post {platform}: {res.get('error') or res}")
        if j.get("job_id"):
            return f"scheduled job {j['job_id']} for {j.get('scheduled_date')}"
        if j.get("request_id"):
            return f"accepted (request {j['request_id']})"
        return (res or {}).get("url") or "ok"
