from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class PublishError(Exception):
    pass


def build_text(s, clip, source, platform: str) -> dict:
    """Caption/title/description tuned per platform. `clip` and `source` are sqlite rows."""
    tags = json.loads(clip["hashtags"] or "[]")
    title = (clip["title"] or "").strip()
    caption = (clip["caption"] or title).strip()
    credit = ""
    if s.credit and source is not None and source["url"]:
        who = source["uploader"] or "the original creator"
        credit = f"\n\nClipped from {who}: {source['url']}"
    if platform == "youtube":
        yt_tags = list(dict.fromkeys(["#Shorts"] + tags))
        ytitle = title[:92] or "Clip"
        if "#shorts" not in ytitle.lower():
            ytitle = f"{ytitle} #Shorts"
        return {"title": ytitle[:100],
                "description": f"{caption}\n\n{' '.join(yt_tags[:15])}{credit}",
                "tags": [t.lstrip('#') for t in yt_tags[:15]]}
    if platform == "tiktok":
        text = f"{caption} {' '.join(tags[:6])}".strip()
        return {"title": text[:2000], "description": text[:2000], "tags": [t.lstrip('#') for t in tags]}
    # instagram
    text = f"{caption}\n\n{' '.join(tags[:15])}{credit}".strip()
    return {"title": text[:2200], "description": text[:2200], "tags": [t.lstrip('#') for t in tags]}


class Publisher:
    name = "base"

    def __init__(self, s):
        self.s = s

    def publish(self, platform: str, video: Path, text: dict, schedule_at: datetime | None = None) -> str:
        """Upload one clip to one platform. Return a url/id string. Raise PublishError on failure."""
        raise NotImplementedError
