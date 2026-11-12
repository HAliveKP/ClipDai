"""Chat-signal: Twitch / YouTube VOD chat replay turned into a per-second activity curve.

Uses the free, MIT-licensed ``chat-downloader`` package (Twitch + YouTube supported;
Kick and direct file links are unsupported and silently degrade to "no signal").
Downloaded chat is cached on disk so the same URL is never downloaded twice.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Sequence

import numpy as np

from .utils import log

WINDOW_SECONDS = 5.0


def _cache_path(cache_dir: Path, url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    return cache_dir / f"{digest}.json"


def _hms_to_seconds(text: str | None) -> float | None:
    """'H:MM:SS' or 'MM:SS' -> seconds."""
    if not text:
        return None
    secs = 0.0
    for p in str(text).split(":"):
        secs = secs * 60 + float(p)
    return secs


def _message_time(msg: dict) -> float | None:
    """Video-relative seconds for one chat message."""
    t = msg.get("time_in_seconds")
    if t is None:
        t = _hms_to_seconds(msg.get("time_text"))
    return float(t) if t is not None else None


def _message_text(msg: dict) -> str:
    text = msg.get("message")
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        return "".join(r.get("text", "") if isinstance(r, dict) else str(r) for r in text)
    return str(text) if text is not None else ""


def fetch_chat_messages(url: str, cache_dir: Path | None = None) -> list[tuple[float, str]]:
    """Download chat replay for ``url`` and return [(seconds_into_vod, message_text), ...].

    Seconds are video-relative (as reported by chat-downloader for Twitch/YouTube VODs).
    Returns an empty list - and logs why - when the platform has no usable chat
    (Kick, disabled chat, expired replay, ...) or the download fails, so callers
    can keep scoring with the remaining signals.
    """
    if cache_dir is None:
        cache_dir = Path("data") / "work" / "chat"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, url)

    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            messages = [(float(t), str(m)) for t, m in raw]
            log(f"[chat] using cached chat for {url} ({len(messages)} messages)")
            return messages
        except Exception as e:
            log(f"[chat] unreadable cache for {url}: {e}; re-downloading")

    try:
        from chat_downloader import ChatDownloader

        messages: list[tuple[float, str]] = []
        for msg in ChatDownloader().get_chat(url):
            t = _message_time(msg)
            if t is None or t < 0:
                continue
            text = _message_text(msg).strip()
            if text:
                messages.append((t, text))

        path.write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
        log(f"[chat] downloaded {len(messages)} messages for {url}")
        return messages
    except Exception as e:  # no chat, unsupported platform, network error ...
        log(f"[chat] no chat data for {url} ({type(e).__name__}: {e}); scoring without chat signal")
        return []


def messages_per_second(messages: Sequence[tuple[float, str]], duration: float) -> np.ndarray:
    """Count chat messages in each 1-second bin of ``[0, duration)``."""
    n = max(int(duration), 1)
    series = np.zeros(n, dtype=np.float32)
    for t, _ in messages:
        i = int(t)
        if 0 <= i < n:
            series[i] += 1.0
    return series


def smooth(series: np.ndarray, window_seconds: float = WINDOW_SECONDS) -> np.ndarray:
    """Rolling-mean smoothing over an approximately ``window_seconds``-second window."""
    if series.size == 0:
        return series
    k = max(1, int(round(window_seconds)))
    if k == 1:
        return series.astype(np.float32, copy=True)
    kernel = np.ones(k, dtype=np.float32) / k
    return np.convolve(series, kernel, mode="same").astype(np.float32)


def normalize(series: np.ndarray) -> np.ndarray:
    """Min-max normalize to [0, 1]; a flat series maps to all zeros."""
    if series.size == 0:
        return series
    lo = float(series.min())
    hi = float(series.max())
    span = hi - lo
    if span < 1e-9:
        return np.zeros_like(series, dtype=np.float32)
    return ((series - lo) / span).astype(np.float32)


def chat_curve(url: str, duration: float, cache_dir: Path | None = None) -> np.ndarray | None:
    """Normalized, smoothed chat activity over ``[0, duration]`` seconds, or ``None`` when unavailable."""
    messages = fetch_chat_messages(url, cache_dir)
    if not messages:
        return None
    series = messages_per_second(messages, duration)
    series = smooth(series)
    return normalize(series)


def chat_score(start: float, end: float, series: np.ndarray | None = None) -> float:
    """Average normalized chat activity in ``[start, end]`` seconds (0.0 without a series)."""
    if series is None or series.size == 0:
        return 0.0
    a = max(int(start), 0)
    b = min(int(end), series.size)
    if b <= a:
        return 0.0
    return float(series[a:b].mean())
