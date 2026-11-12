"""Render a 1080x1920 vertical clip with burned-in word-highlight captions (ASS) using ffmpeg."""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from .highlights import Clip
from .transcribe import Segment
from .utils import run

W, H = 1080, 1920


def _ass_time(t: float) -> str:
    t = max(t, 0)
    return f"{int(t // 3600)}:{int(t // 60) % 60:02d}:{t % 60:05.2f}"


def _clean(w: str) -> str:
    return re.sub(r"\s+", " ", w.replace("{", "(").replace("}", ")")).strip()


def build_ass(segments: list[Segment], start: float, end: float, font: str, size: int, path: Path) -> bool:
    """Karaoke-style captions: 3-4 words at a time, the active word is highlighted yellow."""
    words = []
    for seg in segments:
        for w in seg.words:
            if w.end > start and w.start < end and _clean(w.text):
                words.append((max(w.start, start) - start, min(w.end, end) - start, _clean(w.text)))
    if not words:
        return False

    # group into short phrases (<=4 words, break on punctuation or pauses)
    groups, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        gap = (words[i + 1][0] - w[1]) if i + 1 < len(words) else 9
        if len(cur) >= 4 or re.search(r"[.!?,;:]$", w[2]) or gap > 0.6:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,"
        "Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,"
        "MarginV,Encoding\n"
        f"Style: Default,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,2,60,60,520,1\n\n"
        "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    ) % (W, H)

    lines = []
    for g in groups:
        for idx, (ws, we, wt) in enumerate(g):
            # show the whole group while word idx is active; highlight that word
            st = ws
            en = g[idx + 1][0] if idx + 1 < len(g) else max(we, ws + 0.15)
            text = " ".join(
                (r"{\c&H00FFFF&}" + t.upper() + r"{\c&HFFFFFF&}") if j == idx else t.upper()
                for j, (_, _, t) in enumerate(g)
            )
            lines.append(f"Dialogue: 0,{_ass_time(st)},{_ass_time(en)},Default,,0,0,0,,{text}")
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return True


def render_clip(src: Path, clip: Clip, segments: list[Segment], out: Path, layout: str = "blur",
                crop_x: float = 0.5, font: str = "Arial", size: int = 78, captions: bool = True) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        has_caps = captions and build_ass(segments, clip.start, clip.end, font, size, tdir / "caps.ass")

        if layout == "crop":
            # fill the 9:16 frame: scale to height, crop width around crop_x
            base = (f"[0:v]scale=-2:{H},crop={W}:{H}:'max(0,min(iw-{W},(iw-{W})*{crop_x:.3f}))':0,setsar=1[bg]")
        else:
            # blurred, zoomed copy as background + the full (uncropped) video centered on top
            base = (
                f"[0:v]split=2[a][b];"
                f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:5,eq=brightness=-0.08[bgs];"
                f"[b]scale={W}:-2:force_original_aspect_ratio=decrease[fg];"
                f"[bgs][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[bg]"
            )
        vf = base
        last = "bg"
        if has_caps:
            vf += f";[{last}]subtitles=caps.ass[vout]"
            last = "vout"
        fade_out = max(clip.duration - 0.25, 0)
        af = f"loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=in:st=0:d=0.12,afade=t=out:st={fade_out:.2f}:d=0.25"

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{clip.start:.3f}", "-i", str(Path(src).resolve()), "-t", f"{clip.duration:.3f}",
            "-filter_complex", vf, "-map", f"[{last}]", "-map", "0:a:0?",
            "-af", af,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-movflags", "+faststart",
            str(out.resolve()),
        ]
        run(cmd, cwd=tdir)  # cwd=tmp so the subtitles filter finds caps.ass without path escaping
    return out


def make_thumbnail(video: Path, out: Path, at: float = 1.0) -> Path:
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(at), "-i", str(video),
         "-frames:v", "1", "-q:v", "3", str(out)])
    return out
