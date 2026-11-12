"""Read just enough of a TrueType font to lay captions out identically in the browser preview and in libass.

Both renderers get the same numbers from here:
  * cell  - (usWinAscent + usWinDescent) / unitsPerEm. libass sizes fonts so that ASS `Fontsize` equals this cell
            height, so CSS font-size (em) = ASS size / cell, and an ASS line advances by exactly `Fontsize` pixels.
  * widths - advance width (in em) of printable ASCII, used for line breaking and shrink-to-fit on both sides.
No third-party dependency: a minimal parser for the head / hhea / hmtx / OS/2 / cmap(format 4) tables.
"""
from __future__ import annotations

import os
import struct
import sys
from functools import lru_cache
from pathlib import Path

# family -> (regular file, bold file).  These ship with Windows; elsewhere we fall back to generic metrics.
FONTS: dict[str, tuple[str, str]] = {
    "Arial": ("arial.ttf", "arialbd.ttf"),
    "Arial Black": ("ariblk.ttf", "ariblk.ttf"),
    "Impact": ("impact.ttf", "impact.ttf"),
    "Verdana": ("verdana.ttf", "verdanab.ttf"),
    "Segoe UI": ("segoeui.ttf", "segoeuib.ttf"),
    "Trebuchet MS": ("trebuc.ttf", "trebucbd.ttf"),
    "Tahoma": ("tahoma.ttf", "tahomabd.ttf"),
    "Georgia": ("georgia.ttf", "georgiab.ttf"),
    "Comic Sans MS": ("comic.ttf", "comicbd.ttf"),
}
FALLBACK_CELL = 1.18
FALLBACK_WIDTH = 0.62  # em, deliberately wide so an unknown font never overflows


def _font_dirs() -> list[Path]:
    dirs = []
    if sys.platform == "win32":
        dirs.append(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts")
        dirs.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts")
    dirs += [Path("/Library/Fonts"), Path.home() / "Library/Fonts", Path("/usr/share/fonts/truetype/msttcorefonts"),
             Path.home() / ".fonts"]
    return dirs


def _find(name: str) -> Path | None:
    for d in _font_dirs():
        for cand in (d / name, d / name.lower(), d / name.capitalize()):
            if cand.is_file():
                return cand
    return None


def _parse(path: Path) -> tuple[float, float, dict[int, float]]:
    b = path.read_bytes()
    num = struct.unpack(">H", b[4:6])[0]
    tables = {}
    for i in range(num):
        tag, _, off, ln = struct.unpack(">4sIII", b[12 + 16 * i: 28 + 16 * i])
        tables[tag.decode("latin-1")] = (off, ln)
    upem = struct.unpack(">H", b[tables["head"][0] + 18: tables["head"][0] + 20])[0]
    os2 = tables["OS/2"][0]
    win_asc, win_desc = struct.unpack(">HH", b[os2 + 74: os2 + 78])
    if win_asc + win_desc == 0:
        hh = tables["hhea"][0]
        a, d = struct.unpack(">hh", b[hh + 4: hh + 8])
        win_asc, win_desc = a, -d
    cell = (win_asc + win_desc) / upem
    asc = win_asc / upem
    n_hmetrics = struct.unpack(">H", b[tables["hhea"][0] + 34: tables["hhea"][0] + 36])[0]
    hmtx = tables["hmtx"][0]
    adv = [struct.unpack(">H", b[hmtx + 4 * i: hmtx + 4 * i + 2])[0] for i in range(n_hmetrics)]

    # cmap: find a (3,1) or (0,x) subtable in format 4
    cm = tables["cmap"][0]
    n_sub = struct.unpack(">H", b[cm + 2: cm + 4])[0]
    sub = None
    for i in range(n_sub):
        pid, eid, off = struct.unpack(">HHI", b[cm + 4 + 8 * i: cm + 12 + 8 * i])
        if struct.unpack(">H", b[cm + off: cm + off + 2])[0] == 4 and (pid, eid) in ((3, 1), (0, 3), (0, 4), (0, 1)):
            sub = cm + off
            break
    widths: dict[int, float] = {}
    if sub is not None:
        segx2 = struct.unpack(">H", b[sub + 6: sub + 8])[0]
        seg = segx2 // 2
        ends = struct.unpack(f">{seg}H", b[sub + 14: sub + 14 + segx2])
        starts_off = sub + 16 + segx2
        starts = struct.unpack(f">{seg}H", b[starts_off: starts_off + segx2])
        deltas = struct.unpack(f">{seg}h", b[starts_off + segx2: starts_off + 2 * segx2])
        ro_off = starts_off + 2 * segx2
        ros = struct.unpack(f">{seg}H", b[ro_off: ro_off + segx2])
        for ch in range(32, 127):
            for k in range(seg):
                if starts[k] <= ch <= ends[k]:
                    if ros[k] == 0:
                        gid = (ch + deltas[k]) & 0xFFFF
                    else:
                        p = ro_off + 2 * k + ros[k] + 2 * (ch - starts[k])
                        gid = struct.unpack(">H", b[p: p + 2])[0]
                        if gid:
                            gid = (gid + deltas[k]) & 0xFFFF
                    if gid:
                        widths[ch] = adv[min(gid, len(adv) - 1)] / upem
                    break
    return cell, asc, widths


@lru_cache(maxsize=None)
def metrics(family: str, bold: bool) -> dict:
    """{'cell': float, 'widths': {char: em}, 'found': bool} - JSON-friendly."""
    files = FONTS.get(family)
    path = _find(files[1 if bold else 0]) if files else None
    if path:
        try:
            cell, asc, w = _parse(path)
            return {"cell": round(cell, 4), "asc": round(asc, 4),
                    "widths": {chr(k): round(v, 4) for k, v in w.items()}, "found": True}
        except Exception:  # unreadable font file: fall through to generic metrics
            pass
    return {"cell": FALLBACK_CELL, "asc": round(FALLBACK_CELL * 0.8, 4), "widths": {}, "found": False}


def catalog() -> dict:
    """All selectable families with regular + bold metrics, for the editor and the renderer."""
    return {f: {"regular": metrics(f, False), "bold": metrics(f, True)} for f in FONTS}
