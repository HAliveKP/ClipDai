"""Filler-word and silence detection for cut-based editing.

Given word timestamps (relative to a clip start) it builds the list of segments
to keep: filler words are dropped entirely and gaps of silence longer than the
threshold cut the clip apart. Kept segments keep their internal timing and are
padded by ~pad seconds; the export then trims and concatenates them with short
fades at the joins, so nothing is re-timed inside a kept piece of speech.
"""
from __future__ import annotations

DEFAULT_FILLERS = {
    "um", "uh", "er", "hm", "hmm", "ah", "uhh", "umm", "like", "you know",
    "i mean", "actually", "basically", "literally", "so", "well", "right",
    "okay", "ok", "huh",
}
DEFAULT_SILENCE_THRESHOLD = 0.4   # seconds of non-speech that force a cut
DEFAULT_PAD = 0.05                # seconds of padding on each kept segment
DEFAULT_FADE = 0.04               # seconds of fade at each join (prevents pops)


def _norm(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum() or ch == " ")


def _is_filler(text: str, fillers: set[str]) -> bool:
    return _norm(text) in fillers


def plan_cuts(words: list, silence_threshold: float = DEFAULT_SILENCE_THRESHOLD,
              remove_fillers: bool = True, filler_words=None, pad: float = DEFAULT_PAD,
              clip_len: float | None = None) -> tuple[list[list[float]], list[list[float]]]:
    """(keep_segments, cut_segments) in clip-relative seconds.

    words: [(start, end, text), ...] relative to the clip start. Filler words
    never bridge a silence gap: a gap is measured between consecutive kept
    words. Segments are padded and merged; empty result keeps the whole clip.
    """
    fillers = set(filler_words) if filler_words is not None else set(DEFAULT_FILLERS)
    kept = [w for w in words if not _is_filler(w[2], fillers)]
    clips: list[list[float]] = []
    cur: list[float] | None = None
    prev_end = 0.0
    for s, e, _t in kept:
        s = max(0.0, float(s)); e = max(s, float(e))
        if cur is None:
            cur = [s, e]
        elif s - prev_end > silence_threshold:
            clips.append(cur); cur = [s, e]
        else:
            cur[1] = e
        prev_end = e
    if cur is not None:
        clips.append(cur)
    out: list[list[float]] = []
    total = clip_len if clip_len and clip_len > 0 else (clips[-1][1] if clips else 0.0)
    for a, b in clips:
        a = max(0.0, a - pad); b = min(total, b + pad)
        if b - a > 0.05:
            if out and a <= out[-1][1]:
                out[-1][1] = b
            else:
                out.append([a, b])
    if not out:
        out = [[0.0, total]] if total > 0 else []
    cuts: list[list[float]] = []
    prev = 0.0
    for a, b in out:
        if a > prev + 0.01:
            cuts.append([prev, a])
        prev = b
    if total > 0 and prev < total - 0.01:
        cuts.append([prev, total])
    return [[round(a, 3), round(b, 3)] for a, b in out], [[round(a, 3), round(b, 3)] for a, b in cuts]


def remap_words(words: list, keep_segments: list) -> list:
    """Shift word timestamps into the concatenated timeline of keep_segments."""
    out = []
    offset = 0.0
    for a, b in keep_segments:
        for s, e, t in words:
            ws = max(float(s), float(a)); we = min(float(e), float(b))
            if we > ws:
                out.append([round(offset + ws - a, 3), round(offset + we - a, 3), str(t)])
        offset += b - a
    return out
