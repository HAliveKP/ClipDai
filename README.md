# ClipForge

Paste a Twitch / Kick / YouTube (or any yt-dlp-supported) link. ClipForge **downloads it itself**, transcribes it,
picks the best moments, and renders short vertical clips with word-highlight captions. You **review and edit** them in
a local web app, choosing aspect ratio, framing, caption style and caption text. ClipForge then **auto-posts** them to
YouTube Shorts, TikTok and Instagram Reels on a daily schedule. It can also **watch channels** and clip every new VOD
with no input from you.

Everything runs on your own computer. With no API keys at all it still works.

## 1. Install (5 minutes)

1. Install Python 3.10+ and ffmpeg.
   - Windows: `winget install Gyan.FFmpeg`, then **open a new terminal** so `ffmpeg` is on your PATH
   - Mac: `brew install ffmpeg` &nbsp;|&nbsp; Linux: `sudo apt install ffmpeg`
2. In this folder:
   ```
   python -m venv .venv
   .venv\Scripts\activate            (Mac/Linux: source .venv/bin/activate)
   pip install -r requirements.txt
   copy .env.example .env            (Mac/Linux: cp .env.example .env)
   python -m clipdai serve
   ```
3. Open http://127.0.0.1:8787, paste a link, press **Make clips**.

The first transcription downloads the Whisper speech model (~460 MB for `small`). It runs locally and is free.
On a slow laptop, use `WHISPER_MODEL=base`.

## 2. Free setup (no card)

| Need | Free option | `.env` |
|---|---|---|
| Better highlight picking | Google AI Studio key (free tier) | `GEMINI_API_KEY=` |
| ...or | Groq key (free tier) | `GROQ_API_KEY=` (default model `openai/gpt-oss-120b`) |
| ...or fully offline | Ollama (`ollama pull llama3.1:8b`) | `OLLAMA_MODEL=llama3.1:8b` |
| No key at all | built-in loudness + speech heuristic (works, picks are weaker) | leave blank |

If an AI request fails (rate limit, network, retired model), that part of the video falls back to the offline picker,
so a run never ends with zero clips. Providers retire models from time to time. If the log shows `model_not_found`,
set `GROQ_MODEL` / `GEMINI_MODEL` to a model your key can currently use.

## 3. The workflow

The web app follows four steps.

1. **Paste a link.** Optionally set a start and end time to clip only part of a long stream. The *Processing* list
   shows live progress (downloading %, transcribing %, rendering 2/5). Failed jobs have a **Retry** button.
2. **Review.** Each suggested clip has a player, an AI score, and editable title, caption and hashtags. Use
   **Approve**, **Reject** (recoverable from the *Rejected* tab) or **Save**.
3. **Edit.** Click **Edit** on any clip to open the editor (details below).
4. **Export & post.** **Save & export** re-renders the clip in the background, and the card shows progress.
   Approved clips get the next free daily slot in the posting queue. From the queue you can **Retry** failed posts
   or **Cancel** queued ones. Every card has a **Download MP4** link.

### The editor

- **Aspect ratio:** 9:16 (default), 1:1, 4:5, 16:9, 4:3. The preview and the export size change together
  (1080×1920, 1080×1080, 1080×1350, 1920×1080, 1440×1080).
- **Framing:** *Fit* (whole video over a blurred copy), *Fill* (crop to cover; drag to choose what stays in view),
  *Manual* (drag to move; scroll or use the slider to zoom), *Split* (two source boxes — facecam and gameplay —
  stacked into a vertical 1080×1920 frame; save presets per streamer). Arrow keys nudge the video too.
- **Safe zones:** toggle guides for the TikTok, Reels and Shorts UI overlays. Captions always stay inside them.
- **Captions:** word-timed from the transcript, at most 2 lines, auto-shrunk so nothing is ever cut off.
  - Position top / middle / bottom, a vertical offset slider, or drag the captions on the preview
  - Font, size, regular / bold, outline, shadow
  - Text, active-word and background-box colours, and box opacity
  - Animations: word-by-word highlight (karaoke), none, fade, pop, slide up, typewriter
  - UPPERCASE, max words per line, emoji on/off
- **Keyword highlights & emoji:** rule-based or LLM-driven keyword highlighting in the brand colour, plus auto-emoji
  (max one per line, rendered as text glyphs; drop Twemoji/Noto PNGs into `data/emoji/<codepoint>.png` for image
  rendering). Emoji images are free: Twemoji (CC-BY 4.0) and Noto Emoji (SIL OFL).
- **Cut silences & filler words:** drop gaps longer than a threshold and/or filler words ("um", "uh", …); preview the
  cut list before export; captions are re-timed to the new timeline.
- **Hook title:** editable top-centre title for the first N seconds with fade/pop-in animation, plus an
  "Auto-generate hook" button (LLM, max 8 words).
- **Auto-zoom punch-ins:** loudness-based hype-moment detection punches in ~1.05×–1.25× for ~1.4s (min 3s gap,
  max count set by intensity), targeting the tracked face when face tracking is on, otherwise centre.
- **Brand kit:** logo watermark with position/opacity/size, primary & secondary colours, font, and per-clip overrides.
  Applied to every new clip automatically.
- **Text & timing:** fix any word, change its start or end time, delete it, or reset to the transcript.
- **Trim:** change the clip's start and end, or set them from the playhead.
- **Presets:** five built-in caption styles, plus save your own. *Apply this style to all clips* can re-export
  every clip at once.

**What you see is what you export:** the preview and the ffmpeg export use the same caption layout maths and the same
font files, so line breaks, sizes and positions match.

Links like `http://127.0.0.1:8787/#edit=3` open clip 3 straight in the editor.

## 4. Posting: what is really free

Set a publisher per platform. `manual` always works and is 100% free: the clip and a ready caption land in
`data/ready/<platform>/` and you upload them yourself.

| Publisher | Cost | Notes |
|---|---|---|
| `manual` | free | Saves video + `.txt` caption in `data/ready/`. Works for all three platforms. |
| `youtube` | free | Direct YouTube Data API. **Projects created after 2020-07-28 that Google hasn't audited upload as private only**, and one upload costs ~1,600 of 10,000 daily quota units (about 6/day). Apply for an audit to go public. Setup steps are at the top of `publishers/youtube.py`. |
| `uploadpost` | free tier | One key for all platforms. Per their docs, **TikTok is not on the Free plan**; YouTube and Instagram are. Set `UPLOADPOST_API_KEY` and `UPLOADPOST_USER`. |
| `postiz` | free if self-hosted | Open source (github.com/gitroomhq/postiz-app). Connect accounts in its UI, put its API key in `POSTIZ_API_KEY`. Self-hosting means creating your own TikTok / Meta / Google developer apps and waiting for approval. API limit: 30 requests/hour. |

A realistic free combination is **YouTube via `youtube` or `uploadpost`, Instagram via `uploadpost`, and TikTok via
`manual`** (or self-hosted Postiz once your TikTok app is approved). Example:

```
PUBLISH_YOUTUBE=uploadpost
PUBLISH_INSTAGRAM=uploadpost
PUBLISH_TIKTOK=manual
UPLOADPOST_API_KEY=...
UPLOADPOST_USER=myprofile
```

Instagram needs a Business or Creator account. TikTok apps that haven't passed TikTok's audit can only post
private/draft videos.

If you re-export a clip that is already queued, the scheduled post automatically uses the new version.

## 5. Automation

- **Review first (default):** clips wait in the web app. *Approve* gives each clip the next free daily slot
  (`POST_TIMES`, max `MAX_POSTS_PER_DAY`).
- **Fully hands-off:** set `AUTO_APPROVE=true`, run `python -m clipdai watch https://www.twitch.tv/streamer`, and keep
  `python -m clipdai serve` (or `daemon`, with no UI) running. New VODs are downloaded, clipped, captioned and posted.
- The first time you watch a channel, only its newest `WATCH_BACKFILL` VODs are clipped, not its whole history.
- A failed post is retried (up to 3 attempts, with a growing delay). After that it is marked `failed` in the queue
  with the error message, and you can retry it from the UI.
- `SCHEDULE_MODE=provider` hands future post times to Upload-Post / Postiz / YouTube, so your PC can be off at post time.
- If the server is restarted mid-job, interrupted downloads and exports start again automatically.

## 6. Commands

```
python -m clipdai serve                      web app + downloader + watcher + poster + exporter
python -m clipdai clip <url> [--start 1:20:00 --end 1:45:00] [-n 5] [--auto]
python -m clipdai watch <channel-url>        auto-clip new VODs  (--remove to stop, no arg to list)
python -m clipdai daemon                     same automation without the web UI
python -m clipdai approve all | 3 4 7        queue clips from the terminal
python -m clipdai post                       publish whatever is due now
python -m clipdai status
python tests/smoke_test.py                   offline end-to-end check (needs ffmpeg, no keys/network)
```

## 7. Default look for new clips

These `.env` settings control how clips are rendered the first time. Everything can be changed per clip in the editor.

- `LAYOUT=blur` (default) shows the full video over a blurred copy, with nothing cropped (*Fit* in the editor).
- `LAYOUT=crop` fills the vertical frame (*Fill*). Set the horizontal position with `CROP_X`.
- `LAYOUT=face` crops around the speaker's face per clip. It needs `pip install opencv-python-headless`, and it
  picks one position per clip rather than following a moving speaker.
- `CAPTION_FONT` / `CAPTION_SIZE` set the default caption font and size. Editor fonts are Arial, Arial Black,
  Impact, Verdana, Segoe UI, Trebuchet MS, Tahoma, Georgia and Comic Sans MS.

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `ffmpeg` not found / smoke test fails at once | Install ffmpeg (above) and open a **new** terminal |
| `model_not_found` from Groq/Gemini in the log | The model was retired: set `GROQ_MODEL` / `GEMINI_MODEL` in `.env` |
| `Request too large ... tokens per minute` | Free-tier limit. ClipForge already sends Groq smaller pieces; a paid tier or Gemini avoids it |
| Port 8787 already in use | Another copy is running: stop it, or set `WEB_PORT` |
| Editor says the source video is missing | The original download in `data/work/src/` was deleted. Paste the link again |
| Captions export with plain (monochrome) emoji | Known limit of ffmpeg's subtitle renderer (libass) |
| Debug web requests | Start the server with the environment variable `CLIPDAI_HTTP_LOG=1` |

## 9. Things to know

- Only clip content you own or have permission to use. Many streamers run official clipping programs. Reposting
  without permission can get a channel strikes or removed, whatever tool you use.
- Live streams can't be clipped until the VOD/replay exists.
- Kick channel listing depends on yt-dlp support. If auto-watch can't list a Kick channel, paste VOD links instead.
- Keep `.env`, `client_secret.json` and `data/` private (they are in `.gitignore`).
- The web app has no login. Keep it on `127.0.0.1`.
- Source videos stay in `data/work/src/` so clips can be re-edited later. Delete them to free space once you're done
  with a video.

## Code map

`downloader.py` yt-dlp · `transcribe.py` faster-whisper · `highlights.py` LLM + offline picker ·
`layout.py` aspect ratios, framing, safe zones and caption layout (mirrored in `ui.html`) · `fontmetrics.py` font
measurements · `render.py` ffmpeg + libass export · `face.py` speaker crop · `tracking.py` face tracking · `cuts.py`
silence/filler removal · `zoom.py` auto-zoom punch-ins · `emphasis.py` keyword highlights & emoji · `brand.py` logo
watermark & hook title · `pipeline.py` ingest, export, schedule, publish · `watch.py` channel watcher ·
`publishers/` manual, uploadpost, postiz, youtube · `server.py` + `ui.html` web app and editor · `cli.py`. See
[PROJECT.md](PROJECT.md) for architecture and contributor notes.
