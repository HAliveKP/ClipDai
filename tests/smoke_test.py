"""Offline smoke test (no network, no API keys, no Whisper model): python tests/smoke_test.py
Makes a synthetic video, runs highlight picking, renders 9:16 clips, schedules and 'publishes' via the manual publisher."""
import os, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
tmp = Path(tempfile.mkdtemp())
os.chdir(tmp)
os.environ.update({"DATA_DIR": str(tmp / "data"), "POST_TIMES": "10:00,14:00", "MAX_POSTS_PER_DAY": "2",
                   "PUBLISHER": "manual"})

from datetime import timedelta
from clipdai import config, highlights, pipeline, render
from clipdai.db import DB
from clipdai.transcribe import Segment, Word, extract_audio
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=70",
                "-f", "lavfi", "-i", "sine=frequency=300:duration=70", "-shortest", "-c:v", "libx264",
                "-preset", "ultrafast", "-c:a", "aac", "src.mp4"], check=True)
s = config.load("/nonexistent")
wav = extract_audio(Path("src.mp4"), s.work_dir / "src.wav")

segs, t = [], 5.0
for i in range(18):
    ws = [Word(t + k * 0.4, t + k * 0.4 + 0.35, "wow!" if k == 3 else f"word{k}") for k in range(8)]
    segs.append(Segment(ws[0].start, ws[-1].end, " ".join(w.text for w in ws), ws))
    t += 5

clips = highlights.find_clips(s, segs, wav, 70.0, "test", 2)
assert clips, "no clips picked"
db = DB(s.db_path)
sid = db.add_source("test://video", "Test", "Streamer", "t")
for i, c in enumerate(clips):
    highlights.write_metadata(s, c, segs, "ctx", "Streamer", "test://video")
    out = s.clips_dir / f"c{i}.mp4"
    render.render_clip(Path("src.mp4"), c, segs, out, "blur")
    if i == 0:  # also exercise the crop layout once
        render.render_clip(Path("src.mp4"), c, segs, s.clips_dir / "c0_crop.mp4", "crop")
    cid = db.add_clip(sid, out, c, "pending")
    pipeline.enqueue(s, db, cid)
w = render.probe_size(s.clips_dir / "c0.mp4")
w = f"{w[0]},{w[1]}"
assert w == "1080,1920", w
dues = sorted({j["due"] for j in db.all_jobs()})
assert len(dues) == len(clips), "each clip should get its own slot"
far_future = pipeline.datetime.now().astimezone() + timedelta(days=30)
sent = pipeline.run_due_jobs(s, db, now=far_future)
assert sent == len(clips) * len(s.platforms), sent
assert (s.ready_dir / "tiktok" / "c0.txt").exists()
print("OK:", len(clips), "clips rendered at 1080x1920, queued in separate slots, 'published' to", s.ready_dir)
