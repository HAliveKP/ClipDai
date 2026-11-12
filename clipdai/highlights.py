"""Pick the best clip moments. LLM when a key exists, otherwise an offline heuristic."""
from __future__ import annotations

import re
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import llm
from .transcribe import Segment
from .utils import fmt_time, log


@dataclass
class Clip:
    start: float
    end: float
    score: float = 0.0
    title: str = ""
    hook: str = ""
    reason: str = ""
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start


HYPE = re.compile(
    r"\b(no way|oh my god|omg|what the|holy|insane|crazy|unbelievable|clutch|let'?s go|lets go|"
    r"wait what|are you kidding|i can'?t believe|oh no|bro|dude|secret|the truth|nobody|never|always|"
    r"worst|best|biggest|shocking|actually|here'?s the thing|the problem is|mistake|why)\b", re.I)


def _energy_curve(wav_path: Path, hop: float = 1.0) -> np.ndarray:
    """RMS loudness per `hop` seconds (stdlib wave + numpy, 16k mono pcm16)."""
    out: list[float] = []
    with wave.open(str(wav_path), "rb") as w:
        sr = w.getframerate()
        n = int(sr * hop)
        if n == 0:
            return np.zeros(1)
        while True:  # read in 1-minute blocks so a 6-hour VOD doesn't need gigabytes of RAM
            raw = w.readframes(n * 60)
            if not raw:
                break
            a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            a = a[: len(a) // n * n]
            if len(a):
                out.extend(np.sqrt((a.reshape(-1, n) ** 2).mean(axis=1)).tolist())
    return np.array(out) if out else np.zeros(1)


def _snap(segments: list[Segment], start: float, end: float, lo: float, hi: float, total: float) -> tuple[float, float]:
    """Snap start/end to sentence-ish boundaries and clamp the duration."""
    for s in segments:
        if s.start <= start < s.end:
            start = s.start
            break
    for s in segments:
        if s.start < end <= s.end + 0.01:
            end = s.end
            break
    start = max(0.0, start - 0.15)
    end = min(total, end + 0.35)
    if end - start > hi:
        end = start + hi
    if end - start < lo:
        end = min(total, start + lo)
    return start, end


def _overlaps(c: Clip, chosen: list[Clip]) -> bool:
    return any(c.start < o.end and o.start < c.end for o in chosen)


def _text_in(segments: list[Segment], a: float, b: float) -> str:
    return " ".join(s.text for s in segments if s.end > a and s.start < b).strip()


# --------------------------- heuristic (no API key) ---------------------------
def heuristic(segments: list[Segment], wav: Path, total: float, n: int, lo: float, hi: float) -> list[Clip]:
    energy = _energy_curve(wav)
    if energy.max() > 0:
        e = (energy - np.median(energy)) / (energy.std() + 1e-6)
    else:
        e = energy
    win = int((lo + hi) / 2)
    cands: list[Clip] = []
    for t in range(0, max(int(total) - win, 1), 5):
        seg_e = e[t:t + win]
        if len(seg_e) == 0:
            continue
        txt = _text_in(segments, t, t + win)
        words = len(txt.split())
        wpm = words / win
        hype = len(HYPE.findall(txt))
        qs = txt.count("?") + txt.count("!")
        score = float(seg_e.mean()) + 0.35 * float(seg_e.max()) + 1.1 * min(hype, 5) + 0.3 * min(qs, 5) + 0.8 * min(wpm, 3)
        if words < 15:
            score -= 3
        s, en = _snap(segments, t, t + win, lo, hi, total)
        cands.append(Clip(s, en, score, reason="loud + dense speech + hype words"))
    cands.sort(key=lambda c: -c.score)
    chosen: list[Clip] = []
    for c in cands:
        if not _overlaps(c, chosen):
            chosen.append(c)
        if len(chosen) >= n:
            break
    for c in chosen:
        txt = _text_in(segments, c.start, c.end)
        first = re.split(r"(?<=[.!?])\s+", txt)[0]
        c.hook = first[:90]
        c.title = (first[:70] or "Best moment").rstrip(" .,")
    return sorted(chosen, key=lambda c: c.start)


# --------------------------------- LLM path -----------------------------------
SYSTEM = (
    "You are a viral short-form video editor for TikTok, Instagram Reels and YouTube Shorts. "
    "From a timestamped transcript of a long video or livestream you pick the moments most likely to "
    "perform as standalone clips: strong hook in the first 3 seconds, a complete thought or story beat, "
    "emotion, humor, surprise, a bold opinion, a clutch play, or a useful insight. Avoid intros, "
    "sponsor reads, dead air and anything that needs earlier context. Reply with JSON only."
)


def _lines(segments: list[Segment], a: float, b: float) -> str:
    out = []
    for s in segments:
        if s.end > a and s.start < b:
            out.append(f"[{s.start:.1f}] {s.text}")
    return "\n".join(out)


def llm_pick(s, segments: list[Segment], total: float, n: int, lo: float, hi: float,
             context: str, chunk_seconds: int = 1500, overlap: int = 60) -> list[Clip]:
    chunks, a = [], 0.0
    while a < total:
        chunks.append((a, min(total, a + chunk_seconds)))
        if a + chunk_seconds >= total:
            break
        a += chunk_seconds - overlap
    per_chunk = max(2, int(np.ceil(n * 1.8 / len(chunks))))
    cands: list[Clip] = []
    for i, (ca, cb) in enumerate(chunks, 1):
        log(f"[highlights] asking {s.llm_provider} about chunk {i}/{len(chunks)} ({fmt_time(ca)}-{fmt_time(cb)})")
        user = (
            f"Video context: {context}\n"
            f"Find up to {per_chunk} clips in this transcript excerpt. Each clip must be {lo:.0f}-{hi:.0f} seconds long, "
            f"start at the beginning of a sentence and end at the end of one. Use the [seconds] markers.\n"
            'Return: {"clips":[{"start":<sec>,"end":<sec>,"score":<1-100 virality>,"title":"<=60 chars hooky title",'
            '"hook":"first line viewers see","reason":"why it works","caption":"1-2 sentence social caption, no hashtags",'
            '"hashtags":["#a","#b","#c","#d","#e"]}]}\n\nTRANSCRIPT:\n' + _lines(segments, ca, cb)
        )
        try:
            data = llm.ask_json(s, SYSTEM, user)
        except Exception as e:  # keep going with other chunks
            log(f"[highlights] LLM chunk failed: {e}")
            continue
        for c in data.get("clips", []):
            try:
                st, en = float(c["start"]), float(c["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (ca - 5 <= st < en <= cb + 5):
                continue
            st, en = _snap(segments, st, en, lo, hi, total)
            tags = [("#" + str(h).lstrip("#").replace(" ", "")) for h in c.get("hashtags", [])][:8]
            cands.append(Clip(st, en, float(c.get("score", 50)), str(c.get("title", ""))[:100],
                              str(c.get("hook", "")), str(c.get("reason", "")),
                              str(c.get("caption", "")), tags))
    cands.sort(key=lambda c: -c.score)
    chosen: list[Clip] = []
    for c in cands:
        if not _overlaps(c, chosen):
            chosen.append(c)
        if len(chosen) >= n:
            break
    return sorted(chosen, key=lambda c: c.start)


def find_clips(s, segments: list[Segment], wav: Path, total: float, context: str,
               n: int | None = None) -> list[Clip]:
    n = n or s.clips_per_video
    clips: list[Clip] = []
    if llm.available(s):
        clips = llm_pick(s, segments, total, n, s.clip_min, s.clip_max, context)
    if not clips:
        if llm.available(s):
            log("[highlights] LLM returned nothing, falling back to offline heuristic")
        else:
            log("[highlights] no LLM key set: using offline heuristic (add a free Gemini/Groq key for better picks)")
        clips = heuristic(segments, wav, total, n, s.clip_min, s.clip_max)
    return clips


# ------------------------- captions for heuristic clips -------------------------
def write_metadata(s, clip: Clip, segments: list[Segment], context: str, streamer: str, source_url: str) -> Clip:
    """Ensure every clip has caption + hashtags (LLM if available, simple fallback otherwise)."""
    text = _text_in(segments, clip.start, clip.end)
    if (not clip.caption or not clip.hashtags) and llm.available(s):
        try:
            data = llm.ask_json(
                s, "You write viral social captions. JSON only.",
                f"Video: {context}\nClip transcript:\n{text[:2500]}\n"
                'Return {"title":"<=60 chars hook","caption":"1-2 punchy sentences, no hashtags",'
                '"hashtags":["#a","#b","#c","#d","#e"]}')
            clip.title = clip.title or str(data.get("title", ""))[:100]
            clip.caption = clip.caption or str(data.get("caption", ""))
            if not clip.hashtags:
                clip.hashtags = [("#" + str(h).lstrip("#").replace(" ", "")) for h in data.get("hashtags", [])][:8]
        except Exception as e:
            log(f"[metadata] LLM failed ({e}); using fallback caption")
    if not clip.caption:
        clip.caption = (clip.hook or text[:140]).strip()
    if not clip.title:
        clip.title = clip.caption[:70]
    base = ["#shorts", "#fyp", "#viral"]
    for tag in base:
        if tag not in clip.hashtags:
            clip.hashtags.append(tag)
    clip.hashtags = clip.hashtags[:10]
    return clip
