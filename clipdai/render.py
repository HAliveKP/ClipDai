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

from . import fontmetrics, layout, tracking, cuts, brand as brand_module
from .highlights import Clip
from .transcribe import Segment
from .utils import run, log


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


def video_split(sw: int, sh: int, W: int, H: int, edit: dict, input_label: str = "[0:v]") -> str:
    """filter_complex producing [base] at WxH from ``input_label`` for split layout."""
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
    return f"{input_label}{fc}[fc];{input_label}{gp}[gp];[fc][gp]vstack=inputs=2[base]"


def video_filter(sw: int, sh: int, W: int, H: int, edit: dict, input_label: str = "[0:v]") -> str:
    """filter_complex producing [base] at WxH from ``input_label``, framed per edit."""
    if edit.get("framing") == "split":
        return video_split(sw, sh, W, H, edit, input_label)
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
        return f"{input_label}{fg}[base]"
    fg = f"crop={cw}:{ch}:{cx0}:{cy0},scale={ow}:{oh}:flags=bicubic,setsar=1"
    if covers:
        return f"{input_label}{fg}[base]"
    bw, bh = _even(W / 4), _even(H / 4)
    return (f"{input_label}split=2[a][b];"
            f"[a]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=8:3,"
            f"eq=brightness=-0.08,scale={W}:{H},setsar=1[bg];"
            f"[b]{fg}[fg];[bg][fg]overlay={ox}:{oy}[base]")


def _has_audio(path: Path) -> bool:
    try:
        out = run(["ffmpeg", "-hide_banner", "-i", str(path), "-f", "null", "-"]).stderr
    except RuntimeError:
        return False
    return "Audio:" in out


def _ass_part(tdir: Path, rel: list, style: dict, W: int, H: int, dur: float, base_vf: str, last: str):
    ass = layout.build_ass(rel, style, W, H, dur)
    if not ass:
        return base_vf, last
    (tdir / "caps.ass").write_text(ass, encoding="utf-8")
    fdir = tdir / "fonts"; fdir.mkdir()
    for name in set(fontmetrics.FONTS.get(style["font"], ())):
        f = fontmetrics._find(name)
        if f:
            shutil.copy(f, fdir / f.name)
    return base_vf + ";[base]subtitles=caps.ass:fontsdir=fonts[vout]", "vout"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _ass_color(hex_color: str) -> str:
    rgb = str(hex_color).lstrip("#")
    return f"&H00{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}&"


def _hook_ass(hook, W: int, H: int, color: str) -> str:
    """ASS with a single top-centered hook-title event lasting ``hook.duration`` seconds."""
    size = max(20, int(round(H / 14)))
    dur = max(min(float(hook.duration), 60.0), 0.5)
    if hook.anim == "pop":
        eff = "{\\an8\\t(0,300,\\fscx140\\fscy140)\\t(300,700,\\fscx100\\fscy100)}"
    elif hook.anim == "fade":
        eff = "{\\an8\\fad(400,400)}"
    else:
        eff = "{\\an8}"
    t = _ass_escape(str(hook.title))
    fmt = "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    t0 = f"{int(dur//3600)}:{int(dur%3600//60):02d}:{dur%60:05.2f}"
    return ("[Script Info]\nTitle: hook\n[V4+Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, TertiaryColour, BackColour,"
            " Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, AlphaLevel, Encoding\n"
            f"Style: hook,Arial,{size},{_ass_color(color)},&H000000&,&H00000000,&H00000000,-1,0,1,2,2,8,0,0,0,0,0\n"
            f"[Events]\n{fmt}"
            f"Dialogue: 0,0:00:00.00,{t},hook,,0,0,0,,{eff}{t}")


def _esc_path(p: str) -> str:
    # single-quote the path for ffmpeg's movie= parser; escape ' and \ inside quotes
    return "'" + p.replace("'", "'\\''").replace("\\", "\\\\") + "'"


def _png_size(p: Path) -> tuple[int, int]:
    try:
        raw = p.read_bytes()
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
            import struct
            return struct.unpack(">II", raw[16:24])
    except OSError:
        pass
    return (0, 0)


def _wm_filter(brand: dict, W: int, H: int, data_dir: Path | None, base_label: str,
               tdir: Path | None) -> tuple[str, str]:
    """ffmpeg filter chain appending a logo watermark overlay after ``base_label``."""
    logo = str(brand.get("logo") or "").strip()
    if not logo:
        return "", base_label
    src_lp = Path(logo)
    if not src_lp.is_absolute() and data_dir is not None:
        src_lp = data_dir / src_lp
    if not src_lp.exists():
        log(f"[brand] logo not found: {src_lp}; skipping watermark")
        return "", base_label
    if tdir is not None:
        lp = tdir / "wm_logo.png"
        try:
            shutil.copy(src_lp, lp)
        except OSError:
            lp = src_lp
    else:
        lp = src_lp
    scale_w = max(2, int(round(W * float(brand.get("wm_scale") or 0.10))))
    lw, lh = _png_size(lp)
    scale_h = max(2, round(scale_w * lh / lw)) if lw and lh else scale_w
    opacity = float(brand.get("wm_opacity") or 0.55)
    opacity = min(1.0, max(0.0, opacity))
    margin = int(round(min(W, H) * 0.04))
    pos = str(brand.get("wm_position") or "br")
    if pos == "tl":
        x, y = margin, margin
    elif pos == "tr":
        x, y = W - scale_w - margin, margin
    elif pos == "bl":
        x, y = margin, H - scale_h - margin
    elif pos == "center":
        x, y = (W - scale_w) // 2, (H - scale_h) // 2
    else:  # br
        x, y = W - scale_w - margin, H - scale_h - margin
    x = max(0, min(x, W - scale_w)); y = max(0, min(y, H - scale_h))
    chain = (f"movie='wm_logo.png'[wm];"
             f"[wm]scale={scale_w}:{scale_h}:flags=bicubic[wml];"
             f"[wml]colorchannelmixer=aa={opacity}[wmi];"
             f"{base_label}[wmi]overlay={x}:{y}:format=auto[final]")
    return chain, "final"


def render_edit(src: Path, src_size: tuple[int, int], start: float, end: float, words: list, edit: dict,
                out: Path, data_dir: Path | None = None) -> Path:
    """words: absolute [s, e, text] (source time). edit: layout.normalize_edit() output.

    When edit["cuts"] (keep segments, clip-relative seconds) is present the source
    is trimmed to those segments, remapped into a continuous timeline, and the
    segments are concatenated with short audio fades at the joins. Caption words
    are remapped to the new timeline before layout, so captions stay aligned.
    """
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
    keep = [[float(a), float(b)] for a, b in edit.get("cuts") or []]
    if keep:
        words = cuts.remap_words(words, keep)
    rel = [(s - start, e - start, t) for s, e, t in words if e > start and s < end]
    style = edit["captions"]
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        sw, sh = src_size
        if keep:
            if edit.get("tracking"):
                log("[render] cuts + tracking: falling back to static crop (tracking incompatible with cut timeline)")
                edit = {**edit, "tracking": False, "track": None}
            fade = float(edit.get("cut_fade", cuts.DEFAULT_FADE))
            ha = _has_audio(src)
            segs: list[Path] = []
            for i, (a, b) in enumerate(keep):
                A, B = start + a, start + b
                seg_dur = b - a
                seg_v = tdir / f"seg{i}.mp4"
                vf = video_filter(sw, sh, W, H, edit)  # [0:v] -> [base], no captions yet
                fade_a = min(fade, seg_dur / 2)
                af = (f"afade=t=in:st=0:d={fade_a:.2f},afade=t=out:st={max(seg_dur - fade_a, 0.0):.2f}:d={fade_a:.2f}"
                      if seg_dur > 0.05 and ha else "")
                cmd = [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{A:.3f}", "-i", str(Path(src).resolve()), "-t", f"{seg_dur:.3f}",
                    "-filter_complex", vf, "-map", "[base]", *(("-map", "0:a:0?") if ha else ()),
                ]
                if ha:
                    cmd += ["-af", af]
                cmd += [
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
                    "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-movflags", "+faststart",
                    str(seg_v),
                ]
                run(cmd)
                segs.append(seg_v)
            # concat demuxer: all segments share codec params, stream copy, continuous timeline from 0
            pl = tdir / "segs.txt"
            pl.write_text("".join(f"file '{p.resolve()}'\n" for p in segs), encoding="utf-8")
            all_ = tdir / "all.mp4"
            run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                 "-i", str(pl), "-c", "copy", str(all_)])
            # second pass: burn captions + hook title on the assembled timeline
            brand = edit.get("brand") or {}
            hook = brand_module.hook_style(brand)
            chain: list[str] = []
            label = "[0:v]"
            extra_inputs: list[str] = []
            ass = layout.build_ass(rel, style, W, H, dur)
            if ass:
                (tdir / "caps.ass").write_text(ass, encoding="utf-8")
                fdir = tdir / "fonts"; fdir.mkdir()
                for name in set(fontmetrics.FONTS.get(style["font"], ())):
                    f = fontmetrics._find(name)
                    if f:
                        shutil.copy(f, fdir / f.name)
                chain.append(f"{label}subtitles=caps.ass:fontsdir=fonts[v]")
                label = "[v]"
            if hook.enabled and hook.title:
                (tdir / "hook.ass").write_text(_hook_ass(hook, W, H,
                                                        brand.get("primary", "#ff5a36")), encoding="utf-8")
                chain.append(f"{label}subtitles=hook.ass[h]")
                label = "[h]"
            wm_logo = str(brand.get("logo") or "").strip()
            if wm_logo:
                src_lp = Path(wm_logo)
                if not src_lp.is_absolute() and data_dir is not None:
                    src_lp = data_dir / src_lp
                if src_lp.exists():
                    wm_file = tdir / "wm_logo.png"
                    try:
                        shutil.copy(src_lp, wm_file)
                    except OSError:
                        wm_file = None
                    if wm_file:
                        lw, lh = _png_size(wm_file)
                        scale_w = max(2, int(round(W * float(brand.get("wm_scale") or 0.10))))
                        scale_h = max(2, round(scale_w * lh / lw)) if lw and lh else scale_w
                        opacity = min(1.0, max(0.0, float(brand.get("wm_opacity") or 0.55)))
                        margin = int(round(min(W, H) * 0.04))
                        pos = str(brand.get("wm_position") or "br")
                        if pos == "tl":
                            x, y = margin, margin
                        elif pos == "tr":
                            x, y = W - scale_w - margin, margin
                        elif pos == "bl":
                            x, y = margin, H - scale_h - margin
                        elif pos == "center":
                            x, y = (W - scale_w) // 2, (H - scale_h) // 2
                        else:
                            x, y = W - scale_w - margin, H - scale_h - margin
                        x = max(0, min(x, W - scale_w)); y = max(0, min(y, H - scale_h))
                        chain.append(f"movie='wm_logo.png'[wm];"
                                     f"[wm]scale={scale_w}:{scale_h}:flags=bicubic[wml];"
                                     f"[wml]colorchannelmixer=aa={opacity}[wmi];"
                                     f"{label}[wmi]overlay={x}:{y}:format=auto[o]")
                        label = "[o]"
                else:
                    log(f"[brand] logo not found: {src_lp}; skipping watermark")
            vf2 = ";".join(chain)
            fade_out = max(dur - 0.25, 0)
            af2 = f"loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=in:st=0:d=0.12,afade=t=out:st={fade_out:.2f}:d=0.25"
            cmd2 = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(all_)]
            for inp in extra_inputs:
                cmd2 += ["-i", inp]
            if vf2:
                cmd2 += ["-filter_complex", vf2]
                cmd2 += ["-map", label]
            else:
                cmd2 += ["-map", "0:v"]
            if ha:
                cmd2 += ["-map", "0:a:0?", "-af", af2]
            cmd2 += [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
                "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-movflags", "+faststart",
                str(out.resolve()),
            ]
            run(cmd2, cwd=tdir)
            return out
        else:
            vf = video_filter(sw, sh, W, H, edit)
            vf, last = _ass_part(tdir, rel, style, W, H, dur, vf, "base")
            brand = edit.get("brand") or {}
            hook = brand_module.hook_style(brand)
            if hook.enabled and hook.title:
                (tdir / "hook.ass").write_text(_hook_ass(hook, W, H,
                                                        brand.get("primary", "#ff5a36")), encoding="utf-8")
                vf += f";[{last}]subtitles=hook.ass[hout]"
                last = "hout"
            wm_chain, last = _wm_filter(brand, W, H, data_dir, f"[{last}]", tdir)
            if wm_chain:
                vf += ";" + wm_chain
            fade_out = max(dur - 0.25, 0)
            af = f"loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=in:st=0:d=0.12,afade=t=out:st={fade_out:.2f}:d=0.25"
            audio_map = ("-map", "0:a:0?") if _has_audio(src) else ()
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{start:.3f}", "-i", str(Path(src).resolve()), "-t", f"{dur:.3f}",
                "-filter_complex", vf, "-map", f"[{last}]", *audio_map, "-af", af,
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
