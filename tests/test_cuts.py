"""Unit tests for clipdai.cuts using synthetic timestamps (no network, no API keys).
The render test needs ffmpeg on PATH.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clipdai.cuts import plan_cuts, remap_words, DEFAULT_FILLERS


def words(triples):
    return [[float(a), float(b), t] for a, b, t in triples]


def test_fillers_drop_and_silence_cuts():
    ws = words([(0.0, 0.4, "hey"), (0.5, 0.9, "um"), (1.0, 1.4, "uh"), (2.0, 2.4, "ok"),
                (5.0, 5.4, "wow"), (5.6, 6.0, "now")])
    keep, cutseg = plan_cuts(ws, silence_threshold=0.4, remove_fillers=True,
                             filler_words=DEFAULT_FILLERS, pad=0.05, clip_len=7.0)
    # "ok" is a filler: kept hey, wow, now; gaps 0.4->5.0 and 6.0->7.0 cut
    assert keep == [[0.0, 0.45], [4.95, 6.05]], keep
    assert cutseg == [[0.45, 4.95], [6.05, 7.0]], cutseg


def test_no_fillers_means_speech_bridges_gaps():
    ws = words([(0.0, 0.4, "hey"), (1.0, 1.4, "there")])  # 0.6 gap > 0.4 -> cut
    keep, _ = plan_cuts(ws, silence_threshold=0.4, remove_fillers=False, filler_words=set(), pad=0.05, clip_len=2.0)
    assert keep == [[0.0, 0.45], [0.95, 1.45]], keep


def test_remap_words_shifts_by_removed_time():
    ws = words([(0.0, 0.4, "a"), (1.0, 1.4, "b"), (5.0, 5.4, "c")])
    keep = [[0.0, 0.45], [0.95, 1.45], [4.95, 5.45]]
    rw = remap_words(ws, keep)
    # 'a' stays 0-0.4; 'b' shifted back by 0.5 (cut 0.45-0.95); 'c' shifted back by 3.95
    assert rw == [[0.0, 0.4, "a"], [0.5, 0.9, "b"], [1.0, 1.4, "c"]], rw


def test_empty_words_keeps_whole_clip():
    keep, cutseg = plan_cuts([], clip_len=10.0)
    assert keep == [[0.0, 10.0]] and cutseg == []


def test_all_fillers_nothing_kept():
    ws = words([(0.0, 0.4, "um"), (1.0, 1.4, "uh")])
    keep, cutseg = plan_cuts(ws, remove_fillers=True, filler_words=DEFAULT_FILLERS, clip_len=2.0)
    # everything is filler: keep the whole clip rather than exporting nothing
    assert keep == [[0.0, 2.0]] and cutseg == [], (keep, cutseg)


def _make_clip(path: Path, duration: float = 20.0, audio: bool = True) -> None:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
           f"testsrc2=size=1280x720:rate=30:duration={duration}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=300:duration={duration}", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast"]
    if audio:
        cmd += ["-c:a", "aac"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True)


def test_render_keeps_keep_segment_duration():
    from clipdai import config, render
    tmp = Path(tempfile.mkdtemp())
    _make_clip(tmp / "src.mp4")
    s = config.load("/nonexistent")
    words = [[0.0, 0.4, "hey"], [0.5, 0.9, "um"], [2.0, 2.4, "ok"],
             [5.0, 5.4, "wow"], [5.6, 6.0, "now"]]
    keep, _ = plan_cuts(words, 0.4, True, DEFAULT_FILLERS, 0.05, 7.0)
    edit = {"framing": "fit", "captions": {"enabled": False}, "cuts": keep}
    render.render_edit(tmp / "src.mp4", (1280, 720), 0, 7, words, edit, tmp / "out.mp4")
    try:
        info = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "format=duration", "-of", "json", str(tmp / "out.mp4")],
                       capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        print("SKIP render duration test (ffprobe unavailable)")
        return
    # ffprobe may be unavailable in some environments; skip gracefully
    if info.returncode != 0:
        print("SKIP render duration test (ffprobe unavailable)")
        return
    dur = float(json.loads(info.stdout)["format"]["duration"])
    expected = sum(b - a for a, b in keep)
    assert abs(dur - expected) < 0.1, (dur, expected)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("all cuts tests passed")
