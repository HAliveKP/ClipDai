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

from . import fontmetrics, layout, tracking, cuts, brand as brand_module, emphasis, zoom
from .highlights import Clip
from .transcribe import Segment
from .utils import run, log


def _video_codec() -> str:
    """Detect an available H.264 encoder; fall back through common options."""
    for codec in ("libx264", "h264_mf", "h264_nvenc", "hevc_mf", "mpeg4"):
        try:
            out = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-encoders"]).stdout
            if codec in out:
                return codec
        except Exception:
            continue
    return "libx264"  # let ffmpeg fail with a clear error if nothing available


_VIDEO_CODEC = _video_codec()


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
            f"[a]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},gblur=3,"
            f"scale={W}:{H},setsar=1[bg];"
            f"[b]{fg}[fg];[bg][fg]overlay={ox}:{oy}[base]")


def _has_audio(path: Path) -> bool:
    try:
        out = run(["ffmpeg", "-hide_banner", "-i", str(path), "-f", "null", "-"]).stderr
    except RuntimeError:
        return False
    return "Audio:" in out


def _ass_part(tdir: Path, rel: list, style: dict, W: int, H: int, dur: float, base_vf: str, last: str):
    ass_result = layout.build_ass(rel, style, W, H, dur)
    if not ass_result:
        return base_vf, last, []
    ass, emoji_overlays = ass_result
    caps_ass = tdir / "caps.ass"
    caps_ass.write_text(ass, encoding="utf-8")
    fdir = tdir / "fonts"; fdir.mkdir()
    for name in set(fontmetrics.FONTS.get(style["font"], ())):
        f = fontmetrics._find(name)
        if f:
            shutil.copy(f, fdir / f.name)
    # Copy emoji PNGs to temp dir for overlay
    emoji_dir = Path("data") / "emoji"
    for ov in emoji_overlays:
        src = emoji_dir / f"{ov['cp']}.png"
        if src.exists():
            shutil.copy(src, tdir / f"emoji_{ov['cp']}.png")
    return base_vf + ";[base]subtitles=caps.ass:fontsdir=fonts[vout]", "vout", emoji_overlays


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
    t0 = f"{int(dur // 3600)}:{int(dur % 3600 // 60):02d}:{dur % 60:05.2f}"
    margin_v = int(round(H * 0.09))  # below the top platform overlay
    return (f"[Script Info]\nTitle: hook\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\nWrapStyle: 0\n"
            "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
            "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,"
            "Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,"
            "MarginV,Encoding\n"
            f"Style: hook,Arial,{size},{_ass_color(color)},{_ass_color(color)},&H00000000,&H80000000,-1,0,0,0,"
            f"100,100,0,0,1,{max(2, size // 14)},2,8,{int(W * 0.06)},{int(W * 0.06)},{margin_v},1\n\n"
            "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
            f"Dialogue: 0,0:00:00.00,{t0},hook,,0,0,0,,{eff}{t}\n")


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
    """ffmpeg filter chain appending a logo watermark overlay after ``base_label`` (bare name, no brackets)."""
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
             f"[{base_label}][wmi]overlay={x}:{y}:format=auto[final]")
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
    # words are absolute source seconds; keep segments are clip-relative. Convert to clip-relative first,
    # then remap onto the concatenated (cut) timeline - remap_words needs both in the same time base.
    rel = [(s - start, e - start, t) for s, e, t in words if e > start and s < end]
    if keep:
        rel = [(s, e, t) for s, e, t in cuts.remap_words(rel, keep)]
    style = edit["captions"]
    kw, emap = emphasis.plan(rel, style)
    style = {**style, "keywords": sorted(kw), "emoji_map": emap} if kw else style

    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        sw, sh = src_size

        # ---- segment plan (source seconds) ----
        # cuts define the keep segments; zoom points punch in on top of them.
        zoom_pts: list[dict] = []
        if edit.get("zoom_enabled"):
            for p in edit.get("zoom_points") or []:
                try:
                    t = float(p["t"])
                except (TypeError, ValueError):
                    continue
                zoom_pts.append({"t": max(start, min(end, t)),
                                 "z": float(p.get("z", 1.15)),
                                 "x": p.get("x"), "y": p.get("y")})
            zoom_pts.sort(key=lambda p: p["t"])
        base = [[start + a, start + b] for a, b in keep] or [[start, end]]
        segments: list[dict] = []
        zsub = max(2, int(round(zoom.ZOOM_DUR / 0.2)))  # static sub-segments per zoom
        for seg_a, seg_b in base:
            pts_in = [p for p in zoom_pts if seg_a <= p["t"] < seg_b]
            pts_in.sort(key=lambda p: p["t"])
            cursor = seg_a
            for p in pts_in:
                if p["t"] > cursor + 0.05:
                    segments.append({"a": cursor, "b": p["t"], "zoom": None})
                zend = min(p["t"] + zoom.ZOOM_DUR, seg_b)
                zlen = zend - p["t"]
                step = zlen / zsub
                for i in range(zsub):
                    sa = p["t"] + i * step
                    sb = min(sa + step, zend)
                    # smooth ramp: hold factor in the middle, ease toward 1 at the ends
                    mid = p["t"] + zlen / 2
                    ramp = min(1.0, max(0.0, (sa - p["t"]) / zoom.RAMP))
                    hold = 1.0 if (sa >= p["t"] + zoom.RAMP and sa <= p["t"] + zoom.RAMP + 1.0) else 0.0
                    ease = min(1.0, max(0.0, (zend - sa) / zoom.RAMP))
                    prog = max(ramp, hold, ease)
                    zf = 1.0 + (float(p["z"]) - 1.0) * prog
                    segments.append({"a": sa, "b": sb, "zoom": {"t": p["t"], "z": zf, "x": p.get("x"), "y": p.get("y")}})
                cursor = zend
            if cursor < seg_b - 0.05:
                segments.append({"a": cursor, "b": seg_b, "zoom": None})
        dur = sum(s["b"] - s["a"] for s in segments)
        ha = _has_audio(src)
        fade = float(edit.get("cut_fade", cuts.DEFAULT_FADE))

        def seg_vf(A: float, B: float, zseg: dict | None, out_label: str) -> str:
            """Per-segment filter: trim + setpts + framing (crop+scale). Returns filter fragment."""
            if zseg is None:
                # Normal segment: fit scaling to canvas (may letterbox, but safe)
                kc = min(W / sw, H / sh)
                cw0 = sw * kc
                ch0 = sh * kc
                cx = (W - cw0) / 2
                cy = (H - ch0) / 2
                cw = cw0
                ch = ch0
            else:
                z = float(zseg["z"])
                cx, cy = sw / 2.0, sh / 2.0
                if edit.get("tracking") and edit.get("track"):
                    track = edit["track"]
                    ts = [float(k[0]) for k in track]
                    tt = max(ts[0], min(ts[-1], float(zseg["t"])))
                    for i in range(len(ts) - 1):
                        if ts[i] <= tt <= ts[i + 1]:
                            f = (tt - ts[i]) / (ts[i + 1] - ts[i])
                            px = float(track[i][1]) + f * (float(track[i + 1][1]) - float(track[i][1]))
                            py = float(track[i][2]) + f * (float(track[i + 1][2]) - float(track[i][2]))
                            break
                    else:
                        px, py = float(track[0][1]), float(track[0][2])
                    kc = max(W / sw, H / sh)
                    cw0, ch0 = sw * kc, sh * kc
                    cx = px * (sw - cw0) + cw0 / 2
                    cy = py * (sh - ch0) + ch0 / 2
                cw = max(2.0, sw / z)
                ch = max(2.0, sh / z)
                cx = min(max(cx, cw / 2), sw - cw / 2)
                cy = min(max(cy, ch / 2), sh - ch / 2)
            return (f"[0:v]trim=start={A:.3f}:end={B:.3f},setpts=PTS-{A:.3f}/TB+{offset[0]:.3f}/TB,"
                    f"crop={cw:.2f}:{ch:.2f}:{cx:.2f}:{cy:.2f},scale={W}:{H}:flags=bicubic,setsar=1[{out_label}]")

        segs: list[Path] = []
        if len(segments) == 1 and segments[0]["zoom"] is None and abs(segments[0]["a"] - start) < 0.001 and abs(segments[0]["b"] - end) < 0.001:
            # plain single-pass export (no cuts, no effective zoom)
            vf = video_filter(sw, sh, W, H, edit)
            vf, last, emoji_ov = _ass_part(tdir, rel, style, W, H, dur, vf, "base")
            brand = edit.get("brand") or {}
            hook = brand_module.hook_style(brand)
            if hook.enabled and hook.title:
                (tdir / "hook.ass").write_text(_hook_ass(hook, W, H,
                                                        brand.get("primary", "#ff5a36")), encoding="utf-8")
                vf += f";[{last}]subtitles=hook.ass[hout]"
                last = "hout"
            wm_chain, last = _wm_filter(brand, W, H, data_dir, last, tdir)
            if wm_chain:
                vf += ";" + wm_chain
            # emoji image overlays
            emoji_dir = Path("data") / "emoji"
            for i, ov in enumerate(emoji_ov):
                src = emoji_dir / f"{ov['cp']}.png"
                dst = tdir / f"emoji_{ov['cp']}_{i}.png"
                if src.exists():
                    shutil.copy(src, dst)
                    elabel = f"[emoji{i}]"
                    # Escape colon in Windows drive letter for ffmpeg filtergraph
                    movie_path = dst.as_posix().replace(":", "\\:")
                    vf += f";movie='{movie_path}'{elabel}[{last}][{elabel[1:-1]}]overlay={ov['x']:.0f}:{ov['y']:.0f}:enable='between(t,{ov['start']:.3f},{ov['end']:.3f})'[e{i}]"
                    last = f"[e{i}]"
            fade_out = max(dur - 0.25, 0)
            af = f"loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=in:st=0:d=0.12,afade=t=out:st={fade_out:.2f}:d=0.25"
            audio_map = ("-map", "0:a:0?") if ha else ()
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{start:.3f}", "-i", str(Path(src).resolve()), "-t", f"{dur:.3f}",
                "-filter_complex", vf, "-map", f"[{last}]", *audio_map, "-af", af,
                "-c:v", _VIDEO_CODEC, "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
                "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-movflags", "+faststart",
                str(out.resolve()),
            ]
            run(cmd, cwd=tdir)
            return out
        # multi-segment export: single-pass ffmpeg with unified filtergraph
        fade = float(edit.get("cut_fade", cuts.DEFAULT_FADE))
        offset = [0.0]
        vf_parts: list[str] = []
        af_parts: list[str] = []
        vlabels: list[str] = []
        alabels: list[str] = []

        for i, seg in enumerate(segments):
            A, B = seg["a"], seg["b"]
            seg_dur = B - A
            vlabels.append(f"[v{i}]")
            vf_parts.append(seg_vf(A, B, seg["zoom"], f"v{i}"))
            if ha:
                alabels.append(f"[a{i}]")
                fade_a = min(fade, seg_dur / 2)
                af = (f"afade=t=in:st=0:d={fade_a:.2f},afade=t=out:st={max(seg_dur - fade_a, 0.0):.2f}:d={fade_a:.2f}"
                      if seg_dur > 0.05 else "")
                af_parts.append(
                    f"[0:a]atrim=start={A:.3f}:end={B:.3f},asetpts=PTS-{A:.3f}/TB+{offset[0]:.3f}/TB{',' + af if af else ''}[a{i}]")
            offset[0] += seg_dur

        vf_concat = f"{''.join(vlabels)}concat=n={len(segments)}:v=1[a_concat]"
        af_concat = f"{''.join(alabels)}concat=n={len(segments)}:v=0:a=1[a_concat_a]" if ha else ""
        vf_all = ";".join(vf_parts) + ";" + vf_concat
        if ha:
            vf_all += ";" + ";".join(af_parts) + ";" + af_concat

        all_ = tdir / "all.mp4"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(Path(src).resolve()),
            "-filter_complex", vf_all,
            "-map", "[a_concat]",
        ]
        if ha:
            cmd += ["-map", "[a_concat_a]"]
        cmd += [
            "-c:v", _VIDEO_CODEC, "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-movflags", "+faststart",
            str(all_),
        ]
        run(cmd)
        # second pass: burn captions + hook title (+ watermark) on the assembled timeline
        brand = edit.get("brand") or {}
        hook = brand_module.hook_style(brand)
        chain: list[str] = []
        label = "[0:v]"
        dur = sum(s["b"] - s["a"] for s in segments) or dur  # length of the assembled (cut) timeline
        ass_result = layout.build_ass(rel, style, W, H, dur)
        emoji_overlays: list[dict] = []
        if ass_result:
            ass, emoji_overlays = ass_result
            caps_ass = tdir / "caps.ass"
            caps_ass.write_text(ass, encoding="utf-8")
            fdir = tdir / "fonts"; fdir.mkdir()
            for name in set(fontmetrics.FONTS.get(style["font"], ())):
                f = fontmetrics._find(name)
                if f:
                    shutil.copy(f, fdir / f.name)
            chain.append(f"{label}subtitles=caps.ass:fontsdir=fonts[v]")
            label = "[v]"
        if hook.enabled and hook.title:
            hook_ass = tdir / "hook.ass"
            hook_ass.write_text(_hook_ass(hook, W, H,
                                            brand.get("primary", "#ff5a36")), encoding="utf-8")
            chain.append(f"{label}subtitles=hook.ass[h]")
            label = "[h]"
        wm_chain, last = _wm_filter(brand, W, H, data_dir, label[1:-1], tdir)
        if wm_chain:
            chain.append(wm_chain)
            label = f"[{last}]"
        # Emoji image overlays (on top of everything)
        emoji_dir = Path("data") / "emoji"
        for i, ov in enumerate(emoji_overlays):
            src = emoji_dir / f"{ov['cp']}.png"
            dst = tdir / f"emoji_{ov['cp']}_{i}.png"
            if src.exists():
                shutil.copy(src, dst)
                emoji_label = f"[emoji{i}]"
                # Escape colon in Windows drive letter for ffmpeg filtergraph
                movie_path = dst.as_posix().replace(":", "\\:")
                chain.append(f"movie='{movie_path}'{emoji_label}")
                chain.append(f"{label}[{emoji_label[1:-1]}]overlay={ov['x']:.0f}:{ov['y']:.0f}:enable='between(t,{ov['start']:.3f},{ov['end']:.3f})'[e{i}]")
                label = f"[e{i}]"
        vf2 = ";".join(chain)
        fade_out = max(dur - 0.25, 0)
        af2 = f"loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=in:st=0:d=0.12,afade=t=out:st={fade_out:.2f}:d=0.25"
        cmd2 = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(all_)]
        if vf2:
            cmd2 += ["-filter_complex", vf2]
            cmd2 += ["-map", label]
        else:
            cmd2 += ["-map", "0:v"]
        if ha:
            cmd2 += ["-map", "0:a:0?", "-af", af2]
        # encoder settings + output apply with or without audio
        cmd2 += [
            "-c:v", _VIDEO_CODEC, "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-movflags", "+faststart",
            str(out.resolve()),
        ]
        run(cmd2, cwd=tdir)
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
