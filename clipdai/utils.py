from __future__ import annotations

import re
import subprocess
from pathlib import Path


def parse_time(s: str | float | int | None) -> float | None:
    """'90', '1:30', '01:02:03' -> seconds."""
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return float(s)
    parts = [float(p) for p in str(s).split(":")]
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return secs


def fmt_time(t: float) -> str:
    t = int(max(t, 0))
    return f"{t // 3600:d}:{(t // 60) % 60:02d}:{t % 60:02d}"


def slug(text: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (s[:maxlen].strip("-")) or "clip"


def run(cmd: list[str], cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip()[-1500:]
        raise RuntimeError(f"command failed ({cmd[0]}): {tail}")
    return p


def log(msg: str) -> None:
    print(msg, flush=True)
