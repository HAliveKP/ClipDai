"""Chat-signal: Twitch / YouTube VOD chat replay turned into a per-second activity curve.

Uses the free, MIT-licensed ``chat-downloader`` package (Twitch + YouTube supported;
Kick and direct file links are unsupported and silently degrade to "no signal").
Downloaded chat is cached on disk so the same URL is never downloaded twice.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Sequence

import numpy as np
import requests

from .utils import log

WINDOW_SECONDS = 5.0
CHAT_TIMEOUT_SEC = 18  # hard ceiling: chat fetching must never block the pipeline

# Public Twitch Client-ID (identifier only, not a secret) used by open-source Twitch tools.
_TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
_TWITCH_VIDEO_ID = re.compile(r"(?:twitch\.tv/videos/(\d+)|vod/(\d+)|/v/(\d+))")


def _run_with_timeout(fn, timeout_sec: int, *args, **kwargs):
    """Run ``fn`` in a daemon thread with a hard timeout.

    On timeout the thread is abandoned (daemon, so it won't block process exit)
    and ``TimeoutError`` is raised so callers can fall back gracefully.
    Works under test mocking because everything stays in one process.
    """
    result: list = []
    exc: list = []

    def wrapper():
        try:
            result.append(fn(*args, **kwargs))
        except BaseException as e:  # noqa: BLE001
            exc.append(e)

    t = threading.Thread(target=wrapper, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    if t.is_alive():
        raise TimeoutError(f"{fn.__name__ or 'chat fetch'} timed out after {timeout_sec}s")
    if exc:
        raise exc[0]
    return result[0]


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


def _twitch_video_id(url: str) -> str | None:
    """Extract a Twitch VOD/video id from a URL."""
    m = _TWITCH_VIDEO_ID.search(url)
    if not m:
        return None
    return m.group(1) or m.group(2) or m.group(3)


def _fetch_twitch_gql(video_id: str, duration: float) -> list[tuple[float, str]]:
    """Best-effort Twitch VOD comments via the public GQL endpoint.

    Returns [(seconds, message_text), ...] or raises on any failure.
    """
    url = "https://gql.twitch.tv/videos/comments"
    headers = {"Client-ID": _TWITCH_CLIENT_ID, "Content-Type": "application/json"}
    messages: dict[float, str] = {}
    cursor: str | None = None
    fetched_until = 0.0

    while fetched_until < duration:
        variables: dict = {"videoID": video_id, "contentOffsetSeconds": 0}
        if cursor:
            variables["cursor"] = cursor
        payload = {
            "operationName": "VideoCommentsByOffsetOrCursor",
            "variables": variables,
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        comments = data.get("data", {}).get("video", {}).get("comments", {})
        edges = comments.get("edges", [])
        if not edges:
            break
        for edge in edges:
            node = edge.get("node", {})
            t = float(node.get("contentOffsetSeconds", 0))
            message = node.get("message", {})
            if isinstance(message, dict):
                frags = message.get("fragments", [])
                text = "".join(f.get("text", "") for f in frags if isinstance(f, dict))
            else:
                text = str(message)
            text = text.strip()
            if text:
                messages[t] = text
            fetched_until = max(fetched_until, t)
        cursor = comments.get("pageInfo", {}).get("cursor")
        if not cursor:
            break
    return sorted(messages.items())


def fetch_chat_messages(url: str, cache_dir: Path | None = None,
                        duration: float | None = None) -> list[tuple[float, str]]:
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

        messages = _run_with_timeout(_download_chat_downloader, CHAT_TIMEOUT_SEC, url)

        path.write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
        log(f"[chat] downloaded {len(messages)} messages for {url}")
        return messages
    except TimeoutError:
        log(f"[chat] timed out after {CHAT_TIMEOUT_SEC}s for {url}; trying Twitch GQL fallback")
    except Exception as e:  # no chat, unsupported platform, network error ...
        log(f"[chat] chat_downloader failed for {url} ({type(e).__name__}: {e})")

    # Twitch GQL fallback (Twitch VODs only)
    vid = _twitch_video_id(url)
    if vid:
        gql_duration = float(duration) if duration else 0.0
        try:
            messages = _run_with_timeout(_fetch_twitch_gql, CHAT_TIMEOUT_SEC, vid, gql_duration)
            if messages:
                path.write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
                log(f"[chat] Twitch GQL: {len(messages)} messages for {url}")
                return messages
        except TimeoutError:
            log(f"[chat] Twitch GQL timed out after {CHAT_TIMEOUT_SEC}s for {url}")
        except Exception as e:
            log(f"[chat] Twitch GQL failed for {url} ({type(e).__name__}: {e})")

    log(f"[chat] no chat data for {url}; scoring without chat signal")
    return []


def _download_chat_downloader(url: str) -> list[tuple[float, str]]:
    """Module-level chat_downloader fetch (picklable for multiprocessing)."""
    from chat_downloader import ChatDownloader

    messages: list[tuple[float, str]] = []
    for msg in ChatDownloader().get_chat(url):
        t = _message_time(msg)
        if t is None or t < 0:
            continue
        text = _message_text(msg).strip()
        if text:
            messages.append((t, text))
    return messages


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
    messages = fetch_chat_messages(url, cache_dir, duration)
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
