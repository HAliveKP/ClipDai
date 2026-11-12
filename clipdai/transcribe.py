"""Local, free speech-to-text with faster-whisper (word-level timestamps)."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path

from .utils import log, run


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word]


def extract_audio(video: Path, wav: Path) -> Path:
    if not wav.exists():
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video),
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
    return wav


def _read_wav(wav: Path):
    """16 kHz mono s16le wav (from extract_audio) -> float32 numpy array in [-1, 1]."""
    import wave

    import numpy as np

    with wave.open(str(wav), "rb") as f:
        if f.getframerate() != 16000 or f.getnchannels() != 1 or f.getsampwidth() != 2:
            raise ValueError(f"{wav}: expected 16 kHz mono 16-bit PCM")
        data = f.readframes(f.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def load_cached(video: Path, work_dir: Path) -> list[Segment] | None:
    cache = work_dir / f"{video.stem}.transcript.json"
    return _load(cache) if cache.exists() else None


def _load(path: Path) -> list[Segment]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [Segment(s["start"], s["end"], s["text"], [Word(**w) for w in s["words"]]) for s in raw]


def transcribe(video: Path, work_dir: Path, model_size: str = "small", device: str = "auto",
               language: str | None = None, progress=None) -> tuple[list[Segment], Path]:
    """Returns (segments, wav_path). Cached next to the video in work_dir."""
    work_dir.mkdir(parents=True, exist_ok=True)
    cache = work_dir / f"{video.stem}.transcript.json"
    wav = work_dir / f"{video.stem}.wav"
    extract_audio(video, wav)
    if cache.exists():
        log("[transcribe] using cached transcript")
        return _load(cache), wav

    from faster_whisper import WhisperModel

    log(f"[transcribe] whisper model={model_size} device={device} (first run downloads the model)")
    model = WhisperModel(model_size, device=device, compute_type="auto")
    # Pass decoded samples instead of a path: faster-whisper's own decoder (PyAV) breaks on newer `av` releases
    # (`metadata_errors` kwarg was removed), and our wav is already 16 kHz mono PCM, which is what Whisper wants.
    seg_iter, info = model.transcribe(_read_wav(wav), word_timestamps=True, vad_filter=True, language=language)
    total = max(info.duration, 1)
    segments: list[Segment] = []
    last_pct = -1
    for s in seg_iter:
        words = [Word(float(w.start), float(w.end), w.word) for w in (s.words or [])]
        segments.append(Segment(float(s.start), float(s.end), s.text.strip(), words))
        pct = int(s.end / total * 100)
        if pct != last_pct and pct % 10 == 0:
            log(f"[transcribe] {pct}%")
            last_pct = pct
        if progress:
            progress(f"transcribing {min(pct, 99)}%")
    cache.write_text(json.dumps([asdict(s) for s in segments]), encoding="utf-8")
    return segments, wav
