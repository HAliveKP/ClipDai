"""Direct YouTube Data API upload (free). One-time OAuth login opens your browser.

Setup: Google Cloud Console > new project > enable "YouTube Data API v3" > OAuth consent screen
(add yourself as a test user) > Credentials > OAuth client ID (Desktop app) > download as client_secret.json.

LIMIT: API projects created after 2020-07-28 that haven't passed Google's audit can only upload PRIVATE
videos, and each upload costs ~1600 of the 10,000 daily quota units (~6 uploads/day).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .base import PublishError, Publisher

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


class YouTubePublisher(Publisher):
    name = "youtube"

    def _service(self):
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as e:
            raise PublishError("pip install google-api-python-client google-auth-oauthlib") from e
        s = self.s
        creds = None
        if s.youtube_token.exists():
            creds = Credentials.from_authorized_user_file(str(s.youtube_token), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not s.youtube_secret.exists():
                    raise PublishError(f"{s.youtube_secret} not found (see docstring in publishers/youtube.py)")
                flow = InstalledAppFlow.from_client_secrets_file(str(s.youtube_secret), SCOPES)
                creds = flow.run_local_server(port=0)
            s.youtube_token.write_text(creds.to_json())
        return build("youtube", "v3", credentials=creds, cache_discovery=False)

    def publish(self, platform: str, video: Path, text: dict, schedule_at: datetime | None = None) -> str:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        yt = self._service()
        privacy = self.s.youtube_privacy
        status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}
        if schedule_at is not None and schedule_at > datetime.now(timezone.utc):
            status["privacyStatus"] = "private"  # YouTube requires private + publishAt
            status["publishAt"] = schedule_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        body = {"snippet": {"title": text["title"][:100], "description": text["description"][:4900],
                            "tags": text["tags"][:15], "categoryId": self.s.youtube_category},
                "status": status}
        media = MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
        try:
            req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
            resp = None
            while resp is None:
                _, resp = req.next_chunk()
        except HttpError as e:
            raise PublishError(f"YouTube API error: {e}") from e
        note = "" if privacy == "public" else f" ({privacy} - see YOUTUBE_PRIVACY / audit note)"
        return f"https://youtube.com/shorts/{resp['id']}{note}"
