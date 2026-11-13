"""Brand kit: logo watermark + hook title overlay.

Stored as data_dir/brand.json. Clips may carry per-clip overrides in
edit["brand"]; merge_brand() applies the kit unless the clip overrides it.
Everything here is free: any image can be a logo and any TTF font works.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import time
from pathlib import Path


def brand_file(data_dir: Path) -> Path:
    return data_dir / "brand.json"


DEFAULT_BRAND = {
    "enabled": True,
    "logo": "",                # path (relative to data_dir) of the logo/watermark image
    "wm_position": "br",       # tl | tr | bl | br | center
    "wm_opacity": 0.55,
    "wm_scale": 0.10,          # logo width as fraction of canvas width
    "primary": "#ff5a36",
    "secondary": "#6db0ff",
    "font": "Arial",
    "hook": {"enabled": True, "title": "", "duration": 3.0, "anim": "fade"},
}


def load_brand(data_dir: Path) -> dict:
    p = brand_file(data_dir)
    if not p.exists():
        return dict(DEFAULT_BRAND)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            out = dict(DEFAULT_BRAND)
            out.update(d)
            if isinstance(d.get("hook"), dict):
                h = dict(DEFAULT_BRAND["hook"]); h.update(d["hook"]); out["hook"] = h
            return out
    except (json.JSONDecodeError, OSError) as e:
        from .utils import log
        log(f"[brand] could not read brand.json: {e}; using defaults")
    return dict(DEFAULT_BRAND)


def save_brand(data_dir: Path, d: dict) -> dict:
    out = dict(DEFAULT_BRAND); out.update(d)
    if isinstance(d.get("hook"), dict):
        h = dict(DEFAULT_BRAND["hook"]); h.update(d["hook"]); out["hook"] = h
    out["updated"] = time.time()
    brand_file(data_dir).write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def merge_brand(brand_kit: dict, override: dict | None) -> dict:
    """Merge the global kit with a per-clip override (override wins)."""
    if not brand_kit or not brand_kit.get("enabled"):
        return {"enabled": False}
    merged = {k: v for k, v in brand_kit.items() if k != "hook"}
    ov = override or {}
    if ov.get("enabled") is False:
        merged["enabled"] = False
    for k in ("logo", "wm_position", "wm_opacity", "wm_scale", "primary", "secondary", "font"):
        if k in ov and ov[k] is not None:
            merged[k] = ov[k]
    h = dict(brand_kit.get("hook", DEFAULT_BRAND["hook"]))
    hov = ov.get("hook") or {}
    for k in ("enabled", "title", "duration", "anim"):
        if k in hov and hov[k] is not None:
            h[k] = hov[k]
    merged["hook"] = h
    return merged


@dataclass
class HookStyle:
    enabled: bool = True
    title: str = ""
    duration: float = 3.0
    anim: str = "fade"   # fade | pop | none


def hook_style(brand: dict) -> HookStyle:
    h = brand.get("hook") or {}
    return HookStyle(bool(h.get("enabled", True)), str(h.get("title", "")),
                     float(h.get("duration", 3.0)), str(h.get("anim", "fade")))
