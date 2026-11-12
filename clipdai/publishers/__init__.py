from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .base import PublishError, Publisher
from .manual import ManualPublisher
from .postiz import PostizPublisher
from .uploadpost import UploadPostPublisher
from .youtube import YouTubePublisher


def get_publisher(s, platform: str) -> Publisher:
    kind = s.route.get(platform, "manual")
    if kind == "manual":
        return ManualPublisher(s)
    if kind == "uploadpost":
        return UploadPostPublisher(s)
    if kind == "postiz":
        return PostizPublisher(s)
    if kind == "youtube":
        if platform != "youtube":
            raise PublishError("the 'youtube' publisher only works for the youtube platform")
        return YouTubePublisher(s)
    raise PublishError(f"unknown publisher '{kind}' for {platform}")


__all__ = ["get_publisher", "PublishError", "Publisher"]
