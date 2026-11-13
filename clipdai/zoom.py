"""Auto-zoom punch-in detection on hype moments (feature 7).

Audio loudness is analysed on the decoded 16 kHz mono track (low sample rate,
small hops - fast and dependency-free). Peaks significantly above the clip's
average loudness become zoom points, subject to a minimum 3 s gap and a
maximum count that depends on the requested intensity.
"""
from __future__ import annotations

import wave
import numpy as np

INTENSITY = {
    "subtle": {"factor": 1.08, "k": 2.2, "max": 2},
    "normal": {"factor": 1.15, "k": 1.6, "max": 4},
    "strong": {"factor": 1.25, "k": 1.2, "max": 6},
}
MIN_GAP = 3.0          # seconds between zoom points
HOP = 0.05             # loudness analysis hop (seconds)
ZOOM_DUR = 1.4         # 0.2s ramp up, 1.0s hold, 0.2s ramp down
RAMP = 0.2


def _loudness(wav: Path) -> tuple[np.ndarray, float]:
    with wave.open(str(wav), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if a.size % 2:
        a = a[:-1]
    a = a.reshape(-1, 2).mean(axis=1)  # mono
    hop = max(1, int(sr * HOP))
    rms = []
    for i in range(0, len(a) - hop + 1, hop):
        seg = a[i:i + hop]
        rms.append(float(np.sqrt((seg * seg).mean())))
    rms = np.array(rms)
    times = (np.arange(len(rms)) * hop + hop / 2) / sr
    return rms, times


def detect_zoom_points(wav: Path, duration: float, intensity: str = "normal") -> list[dict]:
    """[(t, x, y, z)] zoom points in source-relative seconds, x/y are None
    (target the tracked face when tracking is active, else centre)."""
    cfg = INTENSITY.get(intensity, INTENSITY["normal"])
    rms, times = _loudness(wav)
    if len(rms) == 0 or float(rms.max()) == 0:
        return []
    k = cfg["k"]
    thr = float(rms.mean()) + k * float(rms.std())
    pts: list[float] = []
    for i in range(1, len(rms) - 1):
        if rms[i] > thr and rms[i] >= rms[i - 1] and rms[i] >= rms[i + 1]:
            pts.append(float(times[i]))
    # non-maximum suppression with a minimum gap, strongest first
    order = sorted(pts, key=lambda t: -float(rms[np.searchsorted(times, t)]))
    chosen: list[float] = []
    for t in order:
        if all(abs(t - c) >= MIN_GAP for c in chosen):
            chosen.append(t)
        if len(chosen) >= cfg["max"]:
            break
    chosen.sort()
    return [{"t": round(t, 3), "x": None, "y": None, "z": cfg["factor"]} for t in chosen]


def zoom_segments(dur: float, points: list[dict]) -> list[dict]:
    """Split [0, dur] into plain segments and zoom segments.

    A zoom segment covers [t, t + ZOOM_DUR] (clamped at dur); its zoom window
    ramps 0.2s up, holds 1.0s, ramps 0.2s down around the zoom factor.
    """
    segs: list[dict] = []
    cursor = 0.0
    for p in sorted(points, key=lambda p: float(p["t"])):
        t = max(cursor, min(float(p["t"]), dur))
        if t > cursor + 0.05:
            segs.append({"a": cursor, "b": t, "zoom": None})
        zend = min(t + ZOOM_DUR, dur)
        segs.append({"a": t, "b": zend, "zoom": {"t": t, "z": float(p["z"]),
                                                 "x": p.get("x"), "y": p.get("y")}})
        cursor = zend
    if cursor < dur - 0.05:
        segs.append({"a": cursor, "b": dur, "zoom": None})
    return [s for s in segs if s["b"] - s["a"] > 0.05]
