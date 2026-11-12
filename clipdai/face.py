"""Optional face-aware 9:16 crop (LAYOUT=face). Needs: pip install opencv-python-headless
Finds where the main face sits horizontally across the clip and centers the vertical crop on it."""
from __future__ import annotations

from pathlib import Path


def detect_crop_x(video: Path, start: float, end: float, samples: int = 14) -> float | None:
    """Return crop position 0..1 (0=left edge, 1=right edge) or None if no face / no OpenCV."""
    try:
        import cv2
    except ImportError:
        return None
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return None
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    width = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0
    height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0
    if not width or not height:
        return None
    # the visible 9:16 window is height*9/16 wide; map face center to a crop_x fraction of the free space
    crop_w = height * 9 / 16
    free = max(width - crop_w, 1)
    centers = []
    for i in range(samples):
        t = start + (end - start) * (i + 0.5) / samples
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        scale = 480 / frame.shape[1] if frame.shape[1] > 480 else 1.0
        small = cv2.resize(frame, None, fx=scale, fy=scale) if scale != 1.0 else frame
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5, minSize=(30, 30))
        if len(faces):
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            centers.append((x + w / 2) / scale)
    cap.release()
    if len(centers) < max(2, samples // 4):
        return None
    centers.sort()
    cx = centers[len(centers) // 2]  # median = robust to a stray detection
    return float(min(1.0, max(0.0, (cx - crop_w / 2) / free)))
