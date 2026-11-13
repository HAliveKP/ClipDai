"""Render a clip from its edit state (aspect ratio, framing, caption style) with ffmpeg + libass.

All geometry and caption layout comes from layout.py, which the browser editor mirrors, so the burned-in export
matches the preview. The chosen font files are copied next to the .ass file and passed with `fontsdir`, so libass
uses exactly the font whose metrics we measured instead of whatever the system font lookup returns.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from . import fontmetrics, layout, tracking
from .highlights import Clip
from .transcribe import Segment
from .utils import run


def _ffmpeg_size(path: Path) -> tuple[int, int]:
    """Fallback probe: parse the resolution ffmpeg prints for the first video stream."""
    out = run(["ffmpeg", "-hide_banner", "-i", str(path), "-f", "null", "-"]).stderr
    m = re.search(r"Video:.*?(\d{3,})x(\d{3,})", out)
    if not m:
        raise RuntimeError(f"could not probe video size of {path}")
    return int(m.group(1)), int(m.group(2))


def probe_size(path: Path) -> tuple[int, int]:
    try:
        out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:stream_tags=rotate",
                   "-of", "csv=p=0", str(path)]).stdout.strip().splitlines()[0].split(",")
        w, h = int(out[0]), int(out[1])
        if len(out) > 2 and out[2].strip() in ("90", "-90", "270", "-270"):
            w, h = h, w
        return w, h
    except Exception:  # ffprobe missing or unreadable: fall back to parsing ffmpeg -i output
        return _ffmpeg_size(path)


def words_in(segments: list[Segment], start: float, end: float) -> list[list]:
    """Transcript words overlapping [start, end] as absolute [s, e, text]."""
    return [[w.start, w.end, w.text] for seg in segments for w in seg.words if w.end > start and w.start < end]


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def video_split(sw: int, sh: int, W: int, H: int, edit: dict) -> str:
    """filter_complex producing [base] at WxH from input 0 for split (facecam + gameplay).

    Two source-space boxes are cropped and scaled to fill stacked bands; bands sum to H.
    Captions are overlaid by the caller on top of [base], so preview and export match.
    """
    fc_src, fc_dst, gp_src, gp_dst = layout.split_frame_rects(sw, sh, W, H, edit)
    (fcx, fcy, fcw, fch) = fc_src
    (_, _, ow, oh) = fc_dst
    (gx, gy, gw, gh) = gp_src
    goh = gp_dst[3]
    fcw = max(fcw, 2); fch = max(fch, 2); gw = max(gw, 2); gh = max(gh, 2)
    ow = max(_even(ow), 2); oh = max(_even(oh), 2)
    goh = max(_even(goh), 2)
    fc = (f"crop={int(round(fcw))}:{int(round(fch))}:{int(round(fcx))}:{int(round(fcy))},"
          f"scale={ow}:{oh}:flags=bicubic,setsar=1")
    gp = (f"crop={int(round(gw))}:{int(round(gh))}:{int(round(gx))}:{int(round(gy))},"
          f"scale={ow}:{goh}:flags=bicubic,setsar=1")
    return f"[0:v]{fc}[fc];[0:v]{gp}[gp];[fc][gp]vstack=inputs=2[base]"


def video_filter(sw: int, sh: int, W: int, H: int, edit: dict) -> str:
    """filter_complex producing [base] at WxH from input 0, framed per edit."""
    if edit.get("framing") == "split":
        return video_split(sw, sh, W, H, edit)
    x, y, w, h = layout.frame_rect(sw, sh, W, H, edit)
    k = w / sw
    vx0, vy0, vx1, vy1 = max(0.0, x), max(0.0, y), min(float(W), x + w), min(float(H), y + h)
    if vx1 - vx0 < 2 or vy1 - vy0 < 2:  # source dragged fully off-canvas: show only the blurred background
        vx0, vy0, vx1, vy1 = x, y, x + 2, y + 2
    # visible part of the source, in source pixels
    cx0, cy0 = max(0, int((vx0 - x) / k)), max(0, int((vy0 - y) / k))
    cw = min(sw - cx0, max(2, int(round((vx1 - vx0) / k))))
    ch = min(sh - cy0, max(2, int(round((vy1 - vy0) / k))))
    ox, oy = int(round(vx0)), int(round(vy0))
    ow, oh = _even(vx1 - vx0), _even(vy1 - vy0)
    ow, oh = min(ow, W - ox) // 2 * 2 or 2, min(oh, H - oy) // 2 * 2 or 2
    covers = ox <= 0 and oy <= 0 and ox + ow >= W and oy + oh >= H
    if covers and bool(edit.get("tracking")) and edit.get("track"):
        # dynamic face-tracked crop: constant crop size, x/y pan over time per keyframes
        track = edit["track"]
        free_x = max(sw - cw, 1)
        free_y = max(sh - ch, 1)
        times = [float(kf[0]) for kf in track]
        cx_vals = [min(max(float(kf[1]) * free_x, 0.0), float(free_x)) for kf in track]
        cy_vals = [min(max(float(kf[2]) * free_y, 0.0), float(free_y)) for kf in track]
        fg = (f"crop={cw}:{ch}:x={tracking.tracking_expr(times, cx_vals)}:"
              f"y={tracking.tracking_expr(times, cy_vals)},scale={ow}:{oh}:flags=bicubic,setsar=1")
        return f"[0:v]{fg}[base]"
    fg = f"crop={cw}:{ch}:{cx0}:{cy0},scale={ow}:{oh}:flags=bicubic,setsar=1"
    if covers:
        return f"[0:v]{fg}[base]"
    bw, bh = _even(W / 4), _even(H / 4)
    return (f"[0:v]split=2[a][b];"
            f"[a]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=8:3,"
            f"eq=brightness=-0.08,scale={W}:{H},setsar=1[bg];"
            f"[b]{fg}[fg];[bg][fg]overlay={ox}:{oy}[base]")


def render_edit(src: Path, src_size: tuple[int, int], start: float, end: float, words: list, edit: dict,
                out: Path) -> Path:
    """words: absolute [s, e, text] (source time). edit: layout.normalize_edit() output."""
    edit = layout.normalize_edit(edit)
    if edit.get("framing") == "split":
        W, H = layout.split_canvas_size()  # split always exports 1080x1920 vertical
    else:
        W, H = layout.canvas_size(edit["aspect"])
    dur = max(end - start, 0.5)
    # compute face-tracking keyframes server-side when tracking is requested but none
    # were supplied (e.g. first export after toggling tracking on in the editor)
    if edit.get("tracking") and not edit.get("track"):
        track = tracking.track_keyframes(Path(src), start, end,
                                         src_size[0], src_size[1], W, H)
        if track:
            edit = {**edit, "track": track}
    rel = [(s - start, e - start, t) for s, e, t in words if e > start and s < end]
    style = edit["captions"]
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        vf = video_filter(src_size[0], src_size[1], W, H, edit)
        last = "base"
        ass = layout.build_ass(rel, style, W, H, dur)
        if ass:
            (tdir / "caps.ass").write_text(ass, encoding="utf-8")
            fdir = tdir / "fonts"
            fdir.mkdir()
            for name in set(fontmetrics.FONTS.get(style["font"], ())):
                f = fontmetrics._find(name)
                if f:
                    shutil.copy(f, fdir / f.name)
            vf += ";[base]subtitles=caps.ass:fontsdir=fonts[vout]"
            last = "vout"
        fade_out = max(dur - 0.25, 0)
        af = f"loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=in:st=0:d=0.12,afade=t=out:st={fade_out:.2f}:d=0.25"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-i", str(Path(src).resolve()), "-t", f"{dur:.3f}",
            "-filter_complex", vf, "-map", f"[{last}]", "-map", "0:a:0?",
            "-af", af,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-movflags", "+faststart",
            str(out.resolve()),
        ]
        run(cmd, cwd=tdir)  # cwd=tmp so the subtitles filter finds caps.ass/fonts without path escaping
    return out


def default_edit(layout_name: str = "blur", crop_x: float = 0.5, font: str = "Arial", size: int = 78) -> dict:
    """Edit state equivalent to the LAYOUT / CROP_X / CAPTION_* settings."""
    framing = "fill" if layout_name in ("crop", "face") else ("split" if layout_name == "split" else "fit")
    return layout.normalize_edit({"framing": framing, "pan_x": crop_x,
                                  "captions": {**layout.DEFAULT_STYLE, "font": font, "size": size}})


def render_clip(src: Path, clip: Clip, segments: list[Segment], out: Path, layout_name: str = "blur",
                crop_x: float = 0.5, font: str = "Arial", size: int = 78, captions: bool = True,
                edit: dict | None = None) -> Path:
    """Backwards-compatible entry point used by the pipeline on first render."""
    edit = layout.normalize_edit(edit or default_edit(layout_name, crop_x, font, size))
    if not captions:
        edit["captions"]["enabled"] = False
    return render_edit(src, probe_size(src), clip.start, clip.end, words_in(segments, clip.start, clip.end), edit, out)


def make_thumbnail(video: Path, out: Path, at: float = 1.0) -> Path:
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(at), "-i", str(video),
         "-frames:v", "1", "-q:v", "3", str(out)])
    return out
