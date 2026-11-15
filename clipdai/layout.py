"""Single source of truth for how a clip is framed and captioned.

The browser editor (ui.html) re-implements `frame_rect`, `caption_safe_rect` and `layout_pages` line for line, fed by
the same font metrics (fontmetrics.py). That is what makes the preview and the burned-in export match: both sides
agree on line breaks, per-caption font size, block position and safe-zone clamping before any pixels are drawn.
If you change the maths here, change the JS twin (search ui.html for "LAYOUT TWIN").
"""
from __future__ import annotations

import copy
import math
import re

from . import fontmetrics

# ------------------------------------------------------------------ canvas ------------------------------------------
ASPECTS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
    "16:9": (1920, 1080),
    "4:3": (1440, 1080),
}

# Platform UI overlays on a 1080x1920 frame (px from each edge). Approximations of the commonly published guides;
# the editor draws these, and captions on 9:16 are always kept inside their union.
SAFE_ZONES: dict[str, dict[str, int]] = {
    "tiktok": {"top": 130, "bottom": 480, "left": 60, "right": 140},
    "reels": {"top": 220, "bottom": 420, "left": 60, "right": 120},
    "shorts": {"top": 150, "bottom": 380, "left": 60, "right": 130},
}
GENERIC_MARGIN = 0.06  # other ratios: keep captions 6% away from every edge

# Split layout: two source-space crop boxes (facecam + gameplay) stacked vertically
# on a 1080x1920 canvas. Boxes are relative to the source (0..1). split_ratio is the
# canvas height fraction given to the facecam band (default 0.35 -> 35/65 split).
DEFAULT_SPLIT_BOXES = {
    "facecam": {"x": 0.10, "y": 0.10, "w": 0.80, "h": 0.30},
    "gameplay": {"x": 0.10, "y": 0.60, "w": 0.80, "h": 0.30},
}
DEFAULT_SPLIT_RATIO = 0.35


def canvas_size(aspect: str) -> tuple[int, int]:
    return ASPECTS.get(aspect, ASPECTS["9:16"])


def split_canvas_size() -> tuple[int, int]:
    """Split mode always exports 1080x1920 vertical."""
    return ASPECTS["9:16"]


def caption_safe_rect(W: int, H: int) -> tuple[float, float, float, float]:
    """(left, top, right, bottom) captions must stay inside. Horizontally symmetric so captions stay centred."""
    if (W, H) == ASPECTS["9:16"]:
        top = max(z["top"] for z in SAFE_ZONES.values())
        bottom = max(z["bottom"] for z in SAFE_ZONES.values())
        side = max(max(z["left"], z["right"]) for z in SAFE_ZONES.values())
        return side, top, W - side, H - bottom
    mx, my = W * GENERIC_MARGIN, H * GENERIC_MARGIN
    return mx, my, W - mx, H - my


# ------------------------------------------------------------------ framing ------------------------------------------
def frame_rect(sw: int, sh: int, W: int, H: int, e: dict) -> tuple[float, float, float, float]:
    """Where the scaled source lands on the canvas: (x, y, w, h). May extend past the canvas (= cropped)."""
    fit = min(W / sw, H / sh)
    cover = max(W / sw, H / sh)
    mode = e.get("framing", "fit")
    if mode == "fill":
        k = cover
        w, h = sw * k, sh * k
        return -(w - W) * e.get("pan_x", 0.5), -(h - H) * e.get("pan_y", 0.5), w, h
    if mode == "manual":
        k = fit * e.get("zoom", 1.0)
        w, h = sw * k, sh * k
        return W / 2 + e.get("off_x", 0.0) * W - w / 2, H / 2 + e.get("off_y", 0.0) * H - h / 2, w, h
    k = fit
    w, h = sw * k, sh * k
    return (W - w) / 2, (H - h) / 2, w, h


# ------------------------------------------------------------ split layout -------------
def _clamp_box(b: dict) -> dict:
    """Clamp a source-space box to [0, 1] with positive size; returns a sanitized copy."""
    out = {"x": 0.0, "y": 0.0, "w": 0.8, "h": 0.3}
    for k in ("x", "y", "w", "h"):
        try:
            v = float(b.get(k, out[k]))
        except (TypeError, ValueError):
            v = out[k]
        if math.isnan(v):
            v = out[k]
        out[k] = min(1.0, max(0.0, v))
    out["w"] = max(0.05, min(1.0 - out["x"], out["w"]))
    out["h"] = max(0.05, min(1.0 - out["y"], out["h"]))
    return out


def split_boxes(e: dict) -> tuple[dict, dict, float]:
    """(facecam_box, gameplay_box, ratio) for split framing, all validated/clamped."""
    raw = e.get("split_boxes") or {}
    fc = _clamp_box(raw.get("facecam") if isinstance(raw.get("facecam"), dict) else {})
    gp = _clamp_box(raw.get("gameplay") if isinstance(raw.get("gameplay"), dict) else {})
    ratio = _num(e.get("split_ratio"), 0.15, 0.85, DEFAULT_SPLIT_RATIO)
    return fc, gp, ratio


def split_frame_rects(sw: int, sh: int, W: int, H: int, e: dict) -> tuple[tuple[float, float, float, float], ...]:
    """Canvas rects (x, y, w, h) for the facecam and gameplay bands on the WxH canvas.

    Returns (fc_src, fc_dst, gp_src, gp_dst): source-space crop rects and the canvas
    rects they fill. Bands stack to exactly WxH (facecam on top, gameplay below).
    """
    fc, gp, ratio = split_boxes(e)
    fh = max(1, round(H * ratio))
    gh = H - fh
    fc_src = (fc["x"] * sw, fc["y"] * sh, fc["w"] * sw, fc["h"] * sh)
    gp_src = (gp["x"] * sw, gp["y"] * sh, gp["w"] * sw, gp["h"] * sh)
    return fc_src, (0, 0, W, fh), gp_src, (0, fh, W, gh)


def _split_boxes_from(d: dict | None) -> dict:
    d = d or {}
    raw = d.get("split_boxes")
    out = {"facecam": _clamp_box(dict(DEFAULT_SPLIT_BOXES["facecam"])),
           "gameplay": _clamp_box(dict(DEFAULT_SPLIT_BOXES["gameplay"]))}
    if isinstance(raw, dict):
        for name in ("facecam", "gameplay"):
            b = raw.get(name)
            if isinstance(b, dict):
                out[name] = _clamp_box(b)
    return out


def _lerp_at(t: float, times: Sequence[float], values: Sequence[float]) -> float:
    """Piecewise-linear value at ``t``; constant before the first / after the last keyframe."""
    times = list(times); values = list(values)
    n = len(times)
    if n == 0:
        return 0.5
    if n == 1 or t <= times[0]:
        return values[0]
    if t >= times[-1]:
        return values[-1]
    lo, hi = 0, n - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if t < times[mid]:
            hi = mid
        else:
            lo = mid
    t0, t1 = times[lo], times[hi]
    v0, v1 = values[lo], values[hi]
    if t1 == t0:
        return v0
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


def tracking_pan(t: float, e: dict) -> tuple[float, float]:
    """(pan_x, pan_y) at source time ``t``.

    When ``e.tracking`` is on and keyframes exist, returns the smoothed tracked
    position at ``t``; otherwise returns the static ``pan_x``/``pan_y``. Used both
    by the editor preview (ui.html mirror) and conceptually by the export, keeping
    preview and export identical.
    """
    if not e.get("tracking"):
        return _num(e.get("pan_x"), 0, 1, 0.5), _num(e.get("pan_y"), 0, 1, 0.5)
    track = e.get("track") or []
    if not track:
        return _num(e.get("pan_x"), 0, 1, 0.5), _num(e.get("pan_y"), 0, 1, 0.5)
    times = [k[0] for k in track]
    xs = [k[1] for k in track]
    ys = [k[2] for k in track]
    return _lerp_at(t, times, xs), _lerp_at(t, times, ys)


# ------------------------------------------------------------------ styles -------------------------------------------
ANIMS = ("none", "fade", "pop", "slide", "typewriter", "karaoke")
POSITIONS = ("top", "middle", "bottom")
WEIGHTS = (400, 700)  # regular / bold: the two font files we measure and hand to libass

DEFAULT_STYLE = {
    "enabled": True,
    "font": "Arial",
    "size": 78,            # px at a 1080-px short side; scales with the canvas
    "weight": 700,
    "outline": 6,
    "shadow": 2,
    "color": "#FFFFFF",
    "highlight": "#FFE600",
    "box_color": "#000000",
    "box_opacity": 0.0,
    "position": "bottom",
    "offset": 0.0,         # fraction of canvas height, added to the preset position (drag / slider)
    "anim": "karaoke",
    "uppercase": True,
    "max_words": 3,        # per line; a caption shows at most 2 lines
    "emoji": True,
    "highlight_keywords": False,
    "keywords": [],
}

BUILTIN_PRESETS = {
    "Bold karaoke": {},
    "Clean minimal": {"font": "Segoe UI", "weight": 700, "size": 64, "outline": 0, "shadow": 3, "anim": "fade",
                      "uppercase": False, "max_words": 4, "box_opacity": 0.0},
    "Boxed": {"font": "Arial", "weight": 700, "size": 62, "outline": 0, "shadow": 0, "box_opacity": 0.75,
              "box_color": "#000000", "anim": "none", "uppercase": False, "max_words": 4},
    "Impact pop": {"font": "Impact", "weight": 400, "size": 92, "outline": 7, "anim": "pop", "max_words": 2,
                   "color": "#FFFFFF", "position": "middle"},
    "Typewriter": {"font": "Verdana", "weight": 700, "size": 60, "outline": 5, "anim": "typewriter",
                   "uppercase": False, "max_words": 3, "highlight": "#7CF3FF"},
}

DEFAULT_EDIT = {
    "aspect": "9:16",
    "framing": "fit",   # fit = letterbox over blurred copy | fill = crop to cover | manual = drag + zoom |
                        # split = two source boxes (facecam/gameplay) stacked into a vertical frame
    "pan_x": 0.5, "pan_y": 0.5,
    "zoom": 1.0, "off_x": 0.0, "off_y": 0.0,
    # face tracking: when True the export window pans to follow the detected speaker.
    # "track" holds [(t, pan_x, pan_y), ...] keyframes computed server-side.
    "tracking": False, "track": None,
    # split layout: source-space boxes (0..1) for facecam and gameplay, plus the
    # fraction of the 1080x1920 canvas height given to the facecam band.
    "split_boxes": copy.deepcopy(DEFAULT_SPLIT_BOXES),
    "split_ratio": DEFAULT_SPLIT_RATIO,
    # cut-based editing: silence / filler removal. "cuts" holds the computed
    # keep segments [[a,b], ...] (clip-relative seconds) when the editor/export
    # has decided them; the toggles drive the on-the-fly planner otherwise.
    "cut_silence": False, "cut_fillers": False,
    "silence_threshold": 0.4, "filler_words": [], "cut_pad": 0.05, "cut_fade": 0.04,
    "cuts": None,
    # auto-zoom punch-ins on hype moments
    "zoom_enabled": False, "zoom_intensity": "normal",
    "zoom_points": [],
    "captions": DEFAULT_STYLE,
}

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _num(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(v):
        return default
    return min(hi, max(lo, v))


def normalize_style(d: dict | None) -> dict:
    d = d or {}
    out = copy.deepcopy(DEFAULT_STYLE)
    out["enabled"] = bool(d.get("enabled", True))
    if d.get("font") in fontmetrics.FONTS:
        out["font"] = d["font"]
    out["size"] = round(_num(d.get("size"), 24, 180, out["size"]))
    w = int(_num(d.get("weight"), 100, 1000, out["weight"]))
    out["weight"] = min(WEIGHTS, key=lambda x: abs(x - w))
    out["outline"] = round(_num(d.get("outline"), 0, 20, out["outline"]), 1)
    out["shadow"] = round(_num(d.get("shadow"), 0, 20, out["shadow"]), 1)
    for k in ("color", "highlight", "box_color"):
        if isinstance(d.get(k), str) and _HEX.match(d[k]):
            out[k] = d[k].upper()
    out["box_opacity"] = round(_num(d.get("box_opacity"), 0, 1, out["box_opacity"]), 2)
    out["position"] = d.get("position") if d.get("position") in POSITIONS else out["position"]
    out["offset"] = round(_num(d.get("offset"), -0.5, 0.5, out["offset"]), 4)
    out["anim"] = d.get("anim") if d.get("anim") in ANIMS else out["anim"]
    out["uppercase"] = bool(d.get("uppercase", out["uppercase"]))
    out["max_words"] = int(_num(d.get("max_words"), 1, 8, out["max_words"]))
    out["emoji"] = bool(d.get("emoji", out["emoji"]))
    out["highlight_keywords"] = bool(d.get("highlight_keywords", False))
    kw = d.get("keywords")
    out["keywords"] = [str(k).strip() for k in kw][:60] if isinstance(kw, list) else []
    return out


def normalize_edit(d: dict | None) -> dict:
    d = d or {}
    out = copy.deepcopy(DEFAULT_EDIT)
    out["aspect"] = d.get("aspect") if d.get("aspect") in ASPECTS else out["aspect"]
    out["framing"] = d.get("framing") if d.get("framing") in ("fit", "fill", "manual", "split") else out["framing"]
    out["pan_x"] = _num(d.get("pan_x"), 0, 1, 0.5)
    out["pan_y"] = _num(d.get("pan_y"), 0, 1, 0.5)
    out["zoom"] = _num(d.get("zoom"), 0.25, 6, 1.0)
    out["off_x"] = _num(d.get("off_x"), -1.5, 1.5, 0.0)
    out["off_y"] = _num(d.get("off_y"), -1.5, 1.5, 0.0)
    out["tracking"] = bool(d.get("tracking", False))
    track = d.get("track")
    out["track"] = track if (track is None or isinstance(track, list)) else None
    out["split_boxes"] = _split_boxes_from(d)
    out["split_ratio"] = _num(d.get("split_ratio"), 0.15, 0.85, out["split_ratio"])
    out["cut_silence"] = bool(d.get("cut_silence", False))
    out["cut_fillers"] = bool(d.get("cut_fillers", False))
    out["silence_threshold"] = _num(d.get("silence_threshold"), 0.05, 30, out["silence_threshold"])
    fw = d.get("filler_words")
    out["filler_words"] = [str(w).strip() for w in fw][:50] if isinstance(fw, list) else list(out["filler_words"])
    out["cut_pad"] = _num(d.get("cut_pad"), 0.0, 5, out["cut_pad"])
    out["cut_fade"] = _num(d.get("cut_fade"), 0.0, 2, out["cut_fade"])
    c = d.get("cuts")
    out["cuts"] = c if (c is None or isinstance(c, list)) else None
    z = d.get("zoom_points")
    out["zoom_points"] = z if isinstance(z, list) else []
    out["zoom_enabled"] = bool(d.get("zoom_enabled", False))
    out["zoom_intensity"] = d.get("zoom_intensity") if d.get("zoom_intensity") in ("subtle", "normal", "strong") else out["zoom_intensity"]
    b = d.get("brand")
    out["brand"] = b if isinstance(b, dict) else None
    out["captions"] = normalize_style(d.get("captions"))
    return out


# ------------------------------------------------------------------ caption layout -----------------------------------
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
SAFETY = 0.96          # leave 4% of the safe width as slack for kerning differences between renderers
UNKNOWN_ASCII = 0.62   # em width for an ASCII char missing from the metrics
UNKNOWN_OTHER = 1.0    # em width for emoji / CJK / anything non-ASCII


def emoji_codepoint(ch: str) -> str:
    """Convert an emoji character to its Twemoji filename codepoint (lowercase hex)."""
    return "_".join(f"{ord(c):x}" for c in ch)


def font_metrics(style: dict) -> dict:
    return fontmetrics.metrics(style["font"], style["weight"] >= 600)


def text_width(text: str, em: float, widths: dict) -> float:
    total = 0.0
    for ch in text:
        if ch in widths:
            total += widths[ch]
        else:
            total += UNKNOWN_ASCII if ord(ch) < 128 else UNKNOWN_OTHER
    return total * em


def prepare_words(words: list, style: dict) -> list[tuple[float, float, str]]:
    """[(start, end, text)] relative to the clip -> cleaned, cased, emoji-filtered."""
    out = []
    for s, e, t in words:
        t = re.sub(r"\s+", " ", str(t).replace("{", "(").replace("}", ")").replace("\\", "/")).strip()
        if not style["emoji"]:
            t = EMOJI.sub("", t).strip()
        if style["uppercase"]:
            t = t.upper()
        if t:
            out.append((float(s), float(e), t))
    return out


def _pages(words, max_words):
    pages, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        gap = (words[i + 1][0] - w[1]) if i + 1 < len(words) else 99
        if (len(cur) >= 2 * max_words or re.search(r"[.!?]$", w[2]) or gap > 0.6
                or (len(cur) >= max_words and re.search(r"[,;:]$", w[2]))):
            pages.append(cur)
            cur = []
    if cur:
        pages.append(cur)
    return pages


def layout_pages(words: list, style: dict, W: int, H: int, clip_len: float) -> list[dict]:
    """Group words into on-screen captions (max 2 lines), size them to fit, and place them inside the safe area."""
    m = font_metrics(style)
    cell, widths = m["cell"], m["widths"]
    asc = m.get("asc", cell * 0.8)
    scale = min(W, H) / 1080
    em0 = style["size"] * scale / cell          # CSS-style em size for the requested ASS size
    outline = style["outline"] * scale
    boxed = style["box_opacity"] > 0
    l, t, r, b = caption_safe_rect(W, H)
    words = [w for w in prepare_words(words, style) if w[1] > 0 and w[0] < clip_len]
    pages = _pages(words, style["max_words"])
    out = []
    for pi, pg in enumerate(pages):
        n = len(pg)
        lines = [pg] if n <= style["max_words"] else [pg[: math.ceil(n / 2)], pg[math.ceil(n / 2):]]
        pad_x0 = 0.3 * em0 if boxed else 0
        avail = (r - l) * SAFETY - 2 * outline - 2 * pad_x0
        line_w = [text_width(" ".join(w[2] for w in ln), em0, widths) for ln in lines]
        f = min(1.0, avail / max(max(line_w), 1))
        em = em0 * f
        line_h = em * cell                       # libass advances one line per ASS font size
        block_h = line_h * len(lines)
        pad_x, pad_y = (0.3 * em, 0.12 * em) if boxed else (0, 0)
        half = block_h / 2 + pad_y + outline
        if style["position"] == "top":
            cy = t + half
        elif style["position"] == "middle":
            cy = (t + b) / 2
        else:
            cy = b - half
        cy += style["offset"] * H
        cy = min(max(cy, t + half), b - half) if (b - t) > 2 * half else (t + b) / 2
        start = max(0.0, pg[0][0])
        nxt = pages[pi + 1][0][0] if pi + 1 < len(pages) else clip_len
        end = min(nxt, pg[-1][1] + 0.6, clip_len)
        if end - start < 0.12:
            end = min(clip_len, start + 0.12)
        out.append({
            "start": round(start, 3), "end": round(end, 3),
            "lines": [[{"s": w[0], "e": w[1], "t": w[2]} for w in ln] for ln in lines],
            "em": em, "ass_size": em * cell, "asc": asc, "cell": cell,
            "cx": W / 2, "cy": cy, "line_h": line_h, "block_h": block_h,
            "box": [W / 2 - max(line_w) * f / 2 - pad_x, cy - block_h / 2 - pad_y,
                    W / 2 + max(line_w) * f / 2 + pad_x, cy + block_h / 2 + pad_y] if boxed else None,
        })
    return out


# ------------------------------------------------------------------ ASS output ---------------------------------------
def _ass_color(hex_rgb: str) -> str:
    h = hex_rgb.lstrip("#")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def _ass_time(t: float) -> str:
    t = max(t, 0)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _rounded_rect(x0, y0, x1, y1, r) -> str:
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    p = lambda v: f"{v:.0f}"  # noqa: E731
    return (f"m {p(x0 + r)} {p(y0)} l {p(x1 - r)} {p(y0)} b {p(x1)} {p(y0)} {p(x1)} {p(y0)} {p(x1)} {p(y0 + r)} "
            f"l {p(x1)} {p(y1 - r)} b {p(x1)} {p(y1)} {p(x1)} {p(y1)} {p(x1 - r)} {p(y1)} "
            f"l {p(x0 + r)} {p(y1)} b {p(x0)} {p(y1)} {p(x0)} {p(y1)} {p(x0)} {p(y1 - r)} "
            f"l {p(x0)} {p(y0 + r)} b {p(x0)} {p(y0)} {p(x0)} {p(y0)} {p(x0 + r)} {p(y0)}")


def build_ass(words: list, style: dict, W: int, H: int, clip_len: float) -> str | None:
    """words: [(start, end, text)] relative to the clip. Returns ASS text or None when there is nothing to show."""
    if not style["enabled"]:
        return None
    pages = layout_pages(words, style, W, H, clip_len)
    if not pages:
        return None
    scale = min(W, H) / 1080
    bold = -1 if style["weight"] >= 600 else 0
    base, hi = _ass_color(style["color"]), _ass_color(style["highlight"])
    head = (
        f"[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\nWrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\nYCbCr Matrix: None\n\n[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,"
        "Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,"
        "MarginV,Encoding\n"
        f"Style: Cap,{style['font']},{style['size']},{base},{base},&H00000000,&H80000000,{bold},0,0,0,100,100,0,0,1,"
        f"{style['outline'] * scale:.2f},{style['shadow'] * scale:.2f},5,0,0,0,1\n\n"
        "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )
    ev = []
    anim = style["anim"]
    a = round((1 - style["box_opacity"]) * 255)
    for pg in pages:
        cx, cy = pg["cx"], pg["cy"]
        pos = f"\\an5\\pos({cx:.1f},{cy:.1f})\\fs{pg['ass_size']:.2f}"
        flat = [w for ln in pg["lines"] for w in ln]
        if pg["box"]:
            x0, y0, x1, y1 = pg["box"]
            fade = "\\fad(120,80)" if anim == "fade" else ""
            ev.append(f"Dialogue: 0,{_ass_time(pg['start'])},{_ass_time(pg['end'])},Cap,,0,0,0,,"
                      f"{{\\an7\\pos(0,0)\\bord0\\shad0\\1c{_ass_color(style['box_color'])}\\1a&H{a:02X}&{fade}\\p1}}"
                      f"{_rounded_rect(x0, y0, x1, y1, pg['em'] * 0.22)}{{\\p0}}")

        def text(active: int | None, reveal: int | None) -> str:
            parts, k, line_emoji = [], 0, 0
            kw = {re.sub(r"[^a-z0-9]+", "", str(k).lower()) for k in (style.get("keywords") or [])}
            emoji_map = style.get("emoji_map") or {}
            max_emoji = int(style.get("emoji_max_per_line") or 1)
            for li, ln in enumerate(pg["lines"]):
                ws = []
                for w in ln:
                    t = w["t"]
                    if reveal is not None and k > reveal:
                        t = "{\\alpha&HFF&}" + t + "{\\alpha&H00&}"
                    elif active is not None and k == active:
                        t = "{\\1c" + hi + "}" + t + "{\\1c" + base + "}"
                    nk = re.sub(r"[^a-z0-9]+", "", t.lower())
                    if style["highlight_keywords"] and nk and nk in kw:
                        t = "{\\1c" + hi + "}" + t + "{\\1c" + base + "}"
                    if style["emoji"] and line_emoji < max_emoji:
                        em = emoji_map.get(nk)
                        if em:
                            pg["emoji_list"].append({"line": li, "emoji": em, "cp": emoji_codepoint(em)})
                            line_emoji += 1
                    ws.append(t)
                    k += 1
                parts.append(" ".join(ws))
                line_emoji = 0
            return "\\N".join(parts)

        if anim in ("karaoke", "typewriter"):
            pg["emoji_list"] = []
            for i, w in enumerate(flat):
                st = pg["start"] if i == 0 else w["s"]
                en = flat[i + 1]["s"] if i + 1 < len(flat) else pg["end"]
                if en - st < 0.01:
                    continue
                body = text(i, i if anim == "typewriter" else None)
                ev.append(f"Dialogue: 1,{_ass_time(st)},{_ass_time(en)},Cap,,0,0,0,,{{{pos}}}{body}")
        else:
            pg["emoji_list"] = []
            tag = pos
            if anim == "fade":
                tag += "\\fad(120,80)"
            elif anim == "pop":
                tag += "\\fscx70\\fscy70\\t(0,110,\\fscx100\\fscy100)"
            elif anim == "slide":
                tag = (f"\\an5\\move({cx:.1f},{cy + 0.035 * H:.1f},{cx:.1f},{cy:.1f},0,160)"
                       f"\\fs{pg['ass_size']:.2f}\\fad(100,0)")
            ev.append(f"Dialogue: 1,{_ass_time(pg['start'])},{_ass_time(pg['end'])},Cap,,0,0,0,,{{{tag}}}{text(None, None)}")
    # Collect emoji overlay info for image-based rendering
    emoji_overlays = []
    emoji_size = style["size"] * 0.6  # emoji smaller than caption text
    for pg in pages:
        for ei in pg.get("emoji_list", []):
            line_idx = ei["line"]
            # y: center of the line within the caption block
            y = pg["cy"] - pg["block_h"] / 2 + line_idx * pg["line_h"] + pg["line_h"] / 2
            # x: right edge of the caption box (or estimate from center + text width)
            if pg["box"]:
                x = pg["box"][2] - emoji_size
            else:
                x = pg["cx"] + pg["em"] * 8
            emoji_overlays.append({
                "cp": ei["cp"],
                "start": pg["start"],
                "end": pg["end"],
                "x": max(0, x),
                "y": max(0, y - emoji_size / 2),
                "w": emoji_size,
                "h": emoji_size,
            })

    return head + "\n".join(ev) + "\n", emoji_overlays
