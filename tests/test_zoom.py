"""Unit tests for clipdai.zoom (no ffmpeg, no network)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from clipdai.zoom import detect_zoom_points, zoom_segments, INTENSITY, MIN_GAP, ZOOM_DUR, RAMP


def _make_wav(path: Path, samples: np.ndarray, sr: int = 16000):
    import wave
    a = np.clip(samples, -1, 1)
    a = (a * 32767).astype(np.int16)
    if a.size % 2:
        a = np.append(a, 0)
    stereo = np.stack([a, a], axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(stereo.tobytes())


def test_detect_zoom_points_finds_spike():
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    sr = 16000
    t = np.arange(0, 10, 1/sr)
    sig = 0.1 * np.sin(2*np.pi*200*t)  # quiet baseline
    sig[5*sr:5*sr+2000] = 0.9  # loud spike at ~5s
    wav = tmp / "a.wav"
    _make_wav(wav, sig, sr)
    pts = detect_zoom_points(wav, 10.0, "normal")
    assert len(pts) >= 1, pts
    assert any(4.8 < p["t"] < 5.3 for p in pts), pts
    assert all("z" in p for p in pts)


def test_min_gap_enforced():
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    sr = 16000
    t = np.arange(0, 10, 1/sr)
    sig = 0.1 * np.ones_like(t)
    for c in (1.0, 2.0, 3.0, 4.0, 5.0):  # spikes 1s apart
        sig[int(c*sr):int(c*sr)+500] = 0.9
    wav = tmp / "a.wav"
    _make_wav(wav, sig, sr)
    pts = detect_zoom_points(wav, 10.0, "normal")
    ts = [p["t"] for p in pts]
    for i in range(1, len(ts)):
        assert ts[i] - ts[i-1] >= MIN_GAP - 0.01, ts


def test_intensity_limits_count():
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    sr = 16000
    t = np.arange(0, 20, 1/sr)
    sig = 0.1 * np.ones_like(t)
    for c in np.arange(2, 19, 2):  # many spikes
        sig[int(c*sr):int(c*sr)+500] = 0.9
    wav = tmp / "a.wav"
    _make_wav(wav, sig, sr)
    for level, cfg in INTENSITY.items():
        pts = detect_zoom_points(wav, 20.0, level)
        assert len(pts) <= cfg["max"], (level, len(pts), cfg["max"])


def test_zoom_segments_full_clip():
    segs = zoom_segments(10.0, [])
    assert segs == [{"a": 0.0, "b": 10.0, "zoom": None}], segs


def test_zoom_segments_with_point():
    segs = zoom_segments(10.0, [{"t": 4.0, "z": 1.15, "x": None, "y": None}])
    # plain 0-4, zoom 4-5.4, plain 5.4-10
    assert segs[0] == {"a": 0.0, "b": 4.0, "zoom": None}, segs[0]
    assert segs[1]["a"] == 4.0 and segs[1]["b"] == 5.4 and segs[1]["zoom"]["z"] == 1.15, segs[1]
    assert segs[2] == {"a": 5.4, "b": 10.0, "zoom": None}, segs[2]


def test_zoom_segments_point_at_end_clamped():
    segs = zoom_segments(6.0, [{"t": 5.5, "z": 1.2, "x": None, "y": None}])
    # zoom 5.5 -> min(5.5+1.4, 6) = 6
    assert segs[1] == {"a": 5.5, "b": 6.0, "zoom": {"t": 5.5, "z": 1.2, "x": None, "y": None}}, segs


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("all zoom tests passed")
