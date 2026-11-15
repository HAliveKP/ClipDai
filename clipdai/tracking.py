"""Dynamic face-tracking crop for vertical (9:16) export.

Detects the speaker's face on downscaled frames read sequentially (no seeking) at
~3-5 fps, smooths the position (exponential moving average + dead zone), and emits
FFmpeg crop keyframes so the export window pans to follow the face. Results are cached
on disk so re-exports do not re-analyse. OpenCV (Haar cascade) only; when OpenCV is
unavailable or detection fails the module returns ``None`` and the caller keeps the
fixed crop, so an export never fails because of tracking.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

SAMPLE_INTERVAL = 0.25          # seconds between detection samples (~4 fps)
DOWNSCALE_WIDTH = 320           # px wide frame used for detection (speed)
EMA_ALPHA = 0.35               # smoothing: higher tracks faster but jitters more
DEAD_ZONE = 0.02               # fraction of frame width under which position is held
MIN_DETECTIONS = 2             # minimum usable samples before accepting a track


@dataclass
class TrackPoint:
    t: float        # seconds into the source
    x: float        # face centre, 0..1 across source width
    y: float        # face centre, 0..1 across source height


def _cache_dir() -> Path:
    return Path("data") / "work" / "tracking"


def _cache_key(video: Path, start: float, end: float) -> str:
    h = hashlib.sha256()
    h.update(str(video.resolve()).encode())
    h.update(f"{start:.3f}".encode())
    h.update(f"{end:.3f}".encode())
    return h.hexdigest()[:32]


def _load_cache(video: Path, start: float, end: float) -> list[TrackPoint] | None:
    try:
        p = _cache_dir() / f"{_cache_key(video, start, end)}.json"
        if not p.exists():
            return None
        raw = json.loads(p.read_text(encoding="utf-8"))
        return [TrackPoint(t=r["t"], x=r["x"], y=r["y"]) for r in raw]
    except Exception:
        return None


def _save_cache(video: Path, start: float, end: float, pts: list[TrackPoint]) -> None:
    try:
        d = _cache_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{_cache_key(video, start, end)}.json"
        p.write_text(json.dumps([asdict(pt) for pt in pts], ensure_ascii=False),
                     encoding="utf-8")
    except Exception:
        pass  # cache is optional; never fail the export


def _largest_face(faces) -> object | None:
    if not len(faces):
        return None
    return max(faces, key=lambda f: f[2] * f[3])


def detect_faces(video: Path, start: float, end: float,
                 interval: float = SAMPLE_INTERVAL) -> list[TrackPoint] | None:
    """Return face-centre samples [(t, x01, y01), ...] or None on any failure.

    Reads frames sequentially (no seeking) for speed; detection runs on frames
    downscaled to ``DOWNSCALE_WIDTH`` px width.
    """
    # Check cache first
    cached = _load_cache(video, start, end)
    if cached is not None and len(cached) >= MIN_DETECTIONS:
        return cached

    try:
        import cv2
    except ImportError:  # opencv not installed: no tracking possible
        return None
    try:
        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            return None
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades +
                                        "haarcascade_frontalface_default.xml")
        if cascade.empty():
            cap.release()
            return None
        width = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0
        height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0
        if width <= 0 or height <= 0:
            cap.release()
            return None
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        frame_interval = max(1, int(round(fps * interval)))  # frames between samples
        pts: list[TrackPoint] = []
        frame_idx = 0
        while True:
            t = frame_idx / fps
            if t > end:
                break
            if t < start or frame_idx % frame_interval != 0:
                # grab without decoding for speed on non-sampled frames
                if not cap.grab():
                    break
                frame_idx += 1
                continue
            ok, frame = cap.read()
            if not ok:
                break
            scale = DOWNSCALE_WIDTH / frame.shape[1] if frame.shape[1] > DOWNSCALE_WIDTH else 1.0
            small = cv2.resize(frame, None, fx=scale, fy=scale) if scale != 1.0 else frame
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            min_size = (max(20, int(30 * scale)), max(20, int(30 * scale)))
            faces = cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5,
                                             minSize=min_size)
            face = _largest_face(faces)
            if face is not None:
                fx, fy, fw, fh = face
                cx = (fx + fw / 2) / scale / width
                cy = (fy + fh / 2) / scale / height
                pts.append(TrackPoint(round(t, 3),
                                      min(1.0, max(0.0, cx)),
                                      min(1.0, max(0.0, cy))))
            frame_idx += 1
        cap.release()
        if len(pts) < MIN_DETECTIONS:
            return None
        _save_cache(video, start, end, pts)
        return pts
    except Exception:  # any OpenCV / file error: degrade to fixed crop
        return None


def smooth_track(points: Sequence[TrackPoint], alpha: float = EMA_ALPHA,
                 dead_zone: float = DEAD_ZONE) -> list[TrackPoint]:
    """EMA smoothing with dead zone; leading gap defaults to centre."""
    pts = sorted(points, key=lambda p: p.t)
    if not pts:
        return []
    out: list[TrackPoint] = []
    sx = 0.5
    sy = 0.5
    have = False
    for p in pts:
        px, py = p.x, p.y
        if not have:
            sx, sy = px, py
            have = True
        else:
            if abs(px - sx) >= dead_zone:
                sx = alpha * px + (1 - alpha) * sx
            if abs(py - sy) >= dead_zone:
                sy = alpha * py + (1 - alpha) * sy
        out.append(TrackPoint(p.t, sx, sy))
    return out


def track_keyframes(video: Path, start: float, end: float,
                    sw: int, sh: int, W: int, H: int
                    ) -> list[tuple[float, float, float]] | None:
    """Detect + smooth the face; return [(t, pan_x, pan_y), ...] in 0..1, or None."""
    raw = detect_faces(video, start, end)
    if raw is None:
        return None
    smooth = smooth_track(raw)
    if not smooth:
        return None
    return [(p.t, p.x, p.y) for p in smooth]


def tracking_expr(times: Sequence[float], values: Sequence[float], var: str = "t") -> str:
    """Piecewise-linear expression over ``var`` with no commas/colons, safe to embed
    verbatim inside an FFmpeg filter option list (e.g. ``crop=...:x=<expr>``).

    The expression is constant before the first keyframe and after the last, and
    linearly interpolates between adjacent keyframes. Keyframe values are assumed
    pre-clamped to a valid range, so no clamping (which needs commas) is required.
    """
    times = [float(t) for t in times]
    values = [float(v) for v in values]
    n = len(times)
    if n == 0:
        return "0.5"
    if n == 1:
        return f"{values[0]:.6f}"
    parts: list[str] = [f"({var}<{times[0]:.6f})*{values[0]:.6f}"]
    for i in range(n - 1):
        seg = (f"{values[i]:.6f}+({values[i + 1]:.6f}-{values[i]:.6f})"
               f"*({var}-{times[i]:.6f})/({times[i + 1]:.6f}-{times[i]:.6f})")
        parts.append(f"({var}>={times[i]:.6f})*({var}<{times[i + 1]:.6f})*({seg})")
    parts.append(f"({var}>={times[-1]:.6f})*{values[-1]:.6f}")
    return "+".join(parts)
