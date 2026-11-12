"""Download a video from Twitch / Kick / YouTube / anything yt-dlp supports (or use a local file)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .utils import log, slug


@dataclass
class SourceVideo:
    path: Path
    title: str
    uploader: str
    url: str
    id: str
    duration: float


def _probe_duration(path: Path) -> float:
    from .utils import run

    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(path)]).stdout.strip()
    return float(out or 0)


def download(url: str, out_dir: Path, max_height: int = 1080,
             start: float | None = None, end: float | None = None, progress=None) -> SourceVideo:
    out_dir.mkdir(parents=True, exist_ok=True)

    local = Path(url).expanduser()
    if local.exists():
        return SourceVideo(local.resolve(), local.stem, "local file", str(local), slug(local.stem, 30),
                           _probe_duration(local))

    import yt_dlp
    from yt_dlp.utils import download_range_func

    # a time range gets its own filename so it never collides with a full download of the same video
    suffix = f"_{int(start or 0)}_{int(end) if end else 'end'}" if (start is not None or end is not None) else ""
    opts = {
        "format": f"bv*[height<={max_height}]+ba/b[height<={max_height}]/best",
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / f"%(id)s{suffix}.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 5,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        "windowsfilenames": True,
    }
    if progress:
        def _hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    progress(f"downloading {min(99, int(d.get('downloaded_bytes', 0) * 100 / total))}%")
            elif d.get("status") == "finished":
                progress("downloading: merging")
        opts["progress_hooks"] = [_hook]
    if start is not None or end is not None:
        opts["download_ranges"] = download_range_func(None, [(start or 0, end or 10**7)])
        opts["force_keyframes_at_cuts"] = True

    log(f"[download] {url}")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info.get("is_live"):
            raise RuntimeError("This is a LIVE stream; wait until the VOD/replay is available.")
        files = info.get("requested_downloads") or []
        path = Path(files[0]["filepath"]) if files else Path(ydl.prepare_filename(info)).with_suffix(".mp4")
    return SourceVideo(
        path=path,
        title=info.get("title") or path.stem,
        uploader=info.get("uploader") or info.get("channel") or "",
        url=info.get("webpage_url") or url,
        id=str(info.get("id") or slug(path.stem, 30)),
        duration=float(info.get("duration") or _probe_duration(path)),
    )
