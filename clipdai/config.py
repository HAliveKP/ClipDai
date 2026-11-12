from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PLATFORMS_ALL = ("youtube", "tiktok", "instagram")


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip()


def _bool(name: str, default: bool = False) -> bool:
    return _env(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self) -> None:
        self.data_dir = Path(_env("DATA_DIR", "data")).expanduser().resolve()
        self.clips_dir = self.data_dir / "clips"
        self.work_dir = self.data_dir / "work"
        self.ready_dir = self.data_dir / "ready"
        self.db_path = self.data_dir / "clipforge.db"
        for d in (self.clips_dir, self.work_dir, self.ready_dir):
            d.mkdir(parents=True, exist_ok=True)

        # --- LLM ---
        self.gemini_key = _env("GEMINI_API_KEY")
        self.gemini_model = _env("GEMINI_MODEL", "gemini-2.5-flash")
        self.groq_key = _env("GROQ_API_KEY")
        self.groq_model = _env("GROQ_MODEL", "openai/gpt-oss-120b")
        self.ollama_url = _env("OLLAMA_URL", "http://localhost:11434")
        self.ollama_model = _env("OLLAMA_MODEL")
        self.openai_key = _env("OPENAI_API_KEY")
        self.openai_base = _env("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
        self.openai_model = _env("OPENAI_MODEL")
        prov = _env("LLM_PROVIDER").lower()
        if not prov:
            if self.gemini_key:
                prov = "gemini"
            elif self.groq_key:
                prov = "groq"
            elif self.openai_key and self.openai_model:
                prov = "openai"
            elif self.ollama_model:
                prov = "ollama"
            else:
                prov = "none"
        self.llm_provider = prov

        # --- clipping ---
        self.whisper_model = _env("WHISPER_MODEL", "small")
        self.whisper_device = _env("WHISPER_DEVICE", "auto")
        self.clips_per_video = int(_env("CLIPS_PER_VIDEO", "5"))
        self.clip_min = float(_env("CLIP_MIN_SECONDS", "20"))
        self.clip_max = float(_env("CLIP_MAX_SECONDS", "60"))
        self.chat_signal_weight = float(_env("CHAT_SIGNAL_WEIGHT", "0.3"))
        self.max_height = int(_env("MAX_SOURCE_HEIGHT", "1080"))
        self.layout = _env("LAYOUT", "blur").lower()
        self.crop_x = float(_env("CROP_X", "0.5"))
        self.caption_font = _env("CAPTION_FONT", "Arial")
        self.caption_size = int(_env("CAPTION_SIZE", "78"))
        self.credit = _bool("CREDIT", True)

        # --- automation ---
        self.auto_approve = _bool("AUTO_APPROVE", False)
        self.post_times = [t.strip() for t in _env("POST_TIMES", "10:00,14:00,19:00").split(",") if t.strip()]
        self.max_posts_per_day = int(_env("MAX_POSTS_PER_DAY", "3"))
        self.schedule_mode = _env("SCHEDULE_MODE", "local").lower()
        self.platforms = [p.strip().lower() for p in _env("PLATFORMS", "youtube,tiktok,instagram").split(",") if p.strip()]
        self.check_minutes = int(_env("CHECK_INTERVAL_MINUTES", "30"))
        self.watch_backfill = int(_env("WATCH_BACKFILL", "1"))
        self.web_host = _env("WEB_HOST", "127.0.0.1")
        self.web_port = int(_env("WEB_PORT", "8787"))

        # --- publishing ---
        self.uploadpost_key = _env("UPLOADPOST_API_KEY")
        self.uploadpost_user = _env("UPLOADPOST_USER")
        self.postiz_key = _env("POSTIZ_API_KEY")
        self.postiz_url = _env("POSTIZ_URL", "https://api.postiz.com/public/v1").rstrip("/")
        self.youtube_secret = Path(_env("YOUTUBE_CLIENT_SECRET", "client_secret.json"))
        self.youtube_token = self.data_dir / "youtube_token.json"
        self.youtube_privacy = _env("YOUTUBE_PRIVACY", "public").lower()
        self.youtube_category = _env("YOUTUBE_CATEGORY", "24")
        self.tiktok_privacy = _env("TIKTOK_PRIVACY", "PUBLIC_TO_EVERYONE")

        default_pub = _env("PUBLISHER").lower()
        if not default_pub:
            if self.uploadpost_key:
                default_pub = "uploadpost"
            elif self.postiz_key:
                default_pub = "postiz"
            else:
                default_pub = "manual"
        self.route = {p: _env(f"PUBLISH_{p.upper()}", default_pub).lower() for p in PLATFORMS_ALL}


def load(env_file: str | None = None) -> Settings:
    load_dotenv(env_file or ".env", override=False)
    return Settings()
