# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Naming gotcha: `clipdai` vs `clipforge`

The project was renamed: the Python package directory is `clipdai/` (run as `python -m clipdai ...`), but README.md,
PROJECT.md, user-facing strings ("ClipForge") and the DB filename (`data/clipforge.db`) still say `clipforge`. Don't
rename `clipforge.db` casually; existing installs would lose their state.

## Commands

Requires Python 3.10+ and `ffmpeg`/`ffprobe` on PATH. A `.venv/` exists in the repo root.

```
pip install -r requirements.txt
cp .env.example .env                 # all keys optional; blank = local Whisper + heuristic picker + manual publisher

python -m clipdai serve              # web UI at http://127.0.0.1:8787 + ingest/poster/watcher threads
python -m clipdai clip <url|file> [--start 1:20:00 --end 1:45:00] [-n 5] [--auto]
python -m clipdai watch <channel-url> [--remove]    # no arg = list watches
python -m clipdai daemon             # same automation loop, no web UI
python -m clipdai approve all | 3 4 7
python -m clipdai post               # publish due jobs now
python -m clipdai status
python -m clipdai --env path/to/.env <cmd>

python tests/smoke_test.py           # the only test: offline end-to-end (synthetic video, no network/keys/Whisper model)
```

There is no linter config or test framework; the smoke test is a plain script with asserts. It runs in a temp dir
with `DATA_DIR`, `PUBLISHER=manual`, and calls `config.load("/nonexistent")` so the local `.env` is not used.

## Architecture

Single process, stdlib-heavy, all state in one SQLite file (WAL mode). See PROJECT.md for the full flowchart, data
model, and web API table.

- **Entry points**: `cli.py` (argparse subcommands) and `server.py` (stdlib `ThreadingHTTPServer` serving `ui.html`
  plus a JSON API). `serve` starts three background threads, each with its **own** `DB` connection: ingest
  (`pipeline.process_pending`), poster (`pipeline.run_due_jobs`, every ~30 s), and watcher (`watch.check_watches`).
  `daemon` runs the same three in one loop without HTTP.
- **Pipeline** (`pipeline.process_source`): `downloader` (yt-dlp, or local file passthrough) → `transcribe`
  (faster-whisper word timestamps, JSON cache next to the audio) → `highlights.find_clips` → `highlights.write_metadata`
  → `render.render_clip` (one ffmpeg filter graph: 1080x1920, `blur`/`crop`/`face` layout, ASS karaoke captions,
  EBU R128 loudness) → `clips` row (`pending`, or straight to `enqueue` if `AUTO_APPROVE`).
- **Highlight picking**: `llm.ask_json` (Gemini / Groq / OpenAI-compatible / Ollama, chosen in `config.py`) on ~25 min
  transcript chunks; any LLM failure or empty result falls back to the offline loudness/speech/hype-word heuristic,
  so a run never yields zero clips for API reasons. Both paths then snap to sentence boundaries and drop overlaps.
- **Scheduling**: `pipeline.enqueue` → `next_slot()` picks the next free `POST_TIMES` slot (local time, capped by
  `MAX_POSTS_PER_DAY`) and creates one `jobs` row per platform in `PLATFORMS`, all sharing that slot. Failed jobs
  retry up to 3 times with growing delay, then become `failed`. `SCHEDULE_MODE=provider` passes the future time to the
  publisher instead of waiting locally.
- **Publishers**: routed **per platform** via `Settings.route` (`PUBLISH_<PLATFORM>`, else `PUBLISHER`, else auto:
  uploadpost → postiz → manual). Interface in `publishers/base.py`:
  `publish(platform, video, text, schedule_at=None) -> str`, raise `PublishError` on failure. Register new ones in
  `publishers/__init__.py::get_publisher` and add settings to `config.py` + `.env.example`. The `youtube` publisher
  only serves the youtube platform.
- **Editor / export (WYSIWYG)**: each clip stores an `edit` JSON (aspect, framing fit/fill/manual, caption style) and
  optional `words` overrides. `layout.py` is the single source of truth for framing geometry, safe zones and caption
  layout (paging, max 2 lines, shrink-to-fit, clamping), and `ui.html` contains a line-for-line JS twin (search
  "LAYOUT TWIN"). **Change both together** or the preview stops matching the export. Both use font metrics read from
  the actual font files (`fontmetrics.py`); `render.py` copies those font files next to the `.ass` and passes
  `fontsdir` so libass uses the measured face. Exports run in the server's `_render_loop` thread
  (`clips.render_status` = queued/rendering/error) via `pipeline.export_clip`, writing `<name>_vN.mp4` and deleting
  the previous file; queued posts pick up the new path automatically.
- **DB migrations**: new columns go in `db.MIGRATIONS` (added with `ALTER TABLE` on open), never by editing `SCHEMA`
  alone.
- **Config**: `config.load()` reads `.env` with `override=False` (real env vars win) and builds a `Settings` object;
  constructing it creates `data/clips`, `data/work`, `data/ready`.

## Conventions

- Keep dependencies minimal; optional features import lazily (OpenCV in `face.py`, Google libs in
  `publishers/youtube.py`).
- `/media/<file>` serves basenames only with HTTP Range; keep the path-traversal check intact.
- Live-service integrations (yt-dlp sites, Whisper download, LLM APIs, Upload-Post/Postiz/YouTube, OpenCV) were written
  from docs and are not covered by the smoke test; only the offline path is verified.
- Never commit `.env`, `client_secret.json`, or `data/`.
