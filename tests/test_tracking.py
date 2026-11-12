"""Unit tests for clipdai.tracking using synthetic data (no OpenCV, no ffmpeg)."""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clipdai.tracking import (
    TrackPoint,
    detect_faces,
    smooth_track,
    tracking_expr,
    track_keyframes,
)
from clipdai.layout import tracking_pan


def tp(t, x, y):
    return TrackPoint(t, x, y)


def test_tracking_expr_is_safe_for_ffmpeg_option_list():
    """The generated expression must contain no ':' or ',' (option/filter separators)
    so it embeds in crop=x=<expr>:y=<expr>. '=' is fine (option name/value split)."""
    times = [0.0, 5.0, 10.0]
    values = [0.2, 0.7, 0.4]
    expr = tracking_expr(times, values)
    for ch in (":", ","):
        assert ch not in expr, f"expression contains forbidden char {ch!r}: {expr}"
    assert "t" in expr


def test_tracking_expr_matches_piecewise_linear():
    times = [0.0, 5.0, 10.0]
    values = [0.2, 0.7, 0.4]
    expr = tracking_expr(times, values)

    def expected(t):
        if t <= 0.0:
            return 0.2
        if t >= 10.0:
            return 0.4
        if t < 5.0:
            return 0.2 + (0.7 - 0.2) * (t - 0.0) / 5.0
        return 0.7 + (0.4 - 0.7) * (t - 5.0) / 5.0

    for t in (-1.0, 0.0, 2.5, 5.0, 7.5, 10.0, 12.0):
        got = eval(expr, {"__builtins__": {}}, {"t": t})
        assert abs(got - expected(t)) < 1e-9, f"t={t}: {got} != {expected(t)}"


def test_tracking_expr_single_keyframe_is_constant():
    expr = tracking_expr([3.0], [0.8])
    for t in (0.0, 3.0, 100.0):
        assert abs(eval(expr, {"__builtins__": {}}, {"t": t}) - 0.8) < 1e-9


def test_smooth_track_ema_and_dead_zone():
    pts = [tp(0.0, 0.5, 0.5), tp(0.5, 0.9, 0.5), tp(1.0, 0.71, 0.5)]
    out = smooth_track(pts, alpha=0.5, dead_zone=0.02)
    assert out[0].x == 0.5 and out[0].y == 0.5  # leading sample initializes
    # big move smoothed toward new value: 0.5*0.9 + 0.5*0.5 = 0.7
    assert abs(out[1].x - 0.7) < 1e-9
    # dead zone: |0.71-0.7|=0.01 < 0.02 -> held at previous smoothed x
    assert abs(out[2].x - out[1].x) < 1e-9
    # a further big move still smooths
    out2 = smooth_track([tp(0.0, 0.5, 0.5), tp(0.5, 0.9, 0.5)], alpha=0.5, dead_zone=0.02)
    assert 0.5 < out2[1].x < 0.9


def test_smooth_track_leading_gap_defaults_to_centre():
    # first detection not at t=0: earlier times use centre (0.5) via tracking_pan
    track = [(2.0, 0.8, 0.3), (4.0, 0.2, 0.7)]
    assert tracking_pan(0.0, {"tracking": True, "track": track}) == (0.8, 0.3)
    assert tracking_pan(2.0, {"tracking": True, "track": track}) == (0.8, 0.3)
    assert tracking_pan(3.0, {"tracking": True, "track": track}) == (0.5, 0.5)
    assert tracking_pan(4.0, {"tracking": True, "track": track}) == (0.2, 0.7)
    assert tracking_pan(10.0, {"tracking": True, "track": track}) == (0.2, 0.7)


def test_tracking_pan_off_returns_static():
    assert tracking_pan(5.0, {"tracking": False, "pan_x": 0.25, "pan_y": 0.75}) == (0.25, 0.75)
    assert tracking_pan(5.0, {"tracking": True, "track": []}) == (0.5, 0.5)
    assert tracking_pan(5.0, {}) == (0.5, 0.5)


def test_detect_faces_none_without_opencv():
    assert detect_faces(Path("any.mp4"), 0, 10) is None


def test_track_keyframes_none_without_opencv():
    assert track_keyframes(Path("any.mp4"), 0, 10, 1920, 1080, 1080, 1920) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("all tracking tests passed")
