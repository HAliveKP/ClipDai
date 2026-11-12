"""Always-free fallback: drops the video + caption into data/ready/<platform>/ for you to upload."""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from .base import Publisher


class ManualPublisher(Publisher):
    name = "manual"

    def publish(self, platform: str, video: Path, text: dict, schedule_at: datetime | None = None) -> str:
        d = self.s.ready_dir / platform
        d.mkdir(parents=True, exist_ok=True)
        dest = d / video.name
        shutil.copy2(video, dest)
        body = f"TITLE:\n{text['title']}\n\nCAPTION / DESCRIPTION:\n{text['description']}\n"
        if schedule_at:
            body += f"\nSUGGESTED POST TIME: {schedule_at:%Y-%m-%d %H:%M}\n"
        dest.with_suffix(".txt").write_text(body, encoding="utf-8")
        return f"saved to {dest}"
