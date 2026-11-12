# ClipForge

Paste a Twitch / Kick / YouTube (or any yt-dlp-supported) link. ClipForge **downloads it itself**, transcribes it,
picks the best moments, renders 1080x1920 clips with word-highlight captions, lets you **review** them in a local
web app (ClipShip-style), then **auto-posts** to YouTube Shorts, TikTok and Instagram Reels on a daily schedule.
It can also **watch channels** and clip every new VOD with no input from you.

## 1. Install (5 minutes)

1. Python 3.10+ and ffmpeg
   - Windows: `winget install Gyan.FFmpeg` &nbsp;|&nbsp; Mac: `brew install ffmpeg` &nbsp;|&nbsp; Linux: `sudo apt install ffmpeg`
2. In this folder:
   ```
   pip install -r requirements.txt
   copy .env.example .env        (Mac/Linux: cp .env.example .env)
   python -m clipforge serve
   ```
3. Open http://127.0.0.1:8787, paste a link, press **Make clips**.

The first run downloads the Whisper speech model (~460 MB for `small`). It runs on your computer, free forever.
Use `WHISPER_MODEL=base` on a slow laptop.

## 2. Free setup (no card)

| Need | Free option | `.env` |
|---|---|---|
| Better highlight picking | Google AI Studio key (free tier) | `GEMINI_API_KEY=` |
| ...or | Groq key (free tier) | `GROQ_API_KEY=` |
| ...or fully offline | Ollama (`ollama pull llama3.1:8b`) | `OLLAMA_MODEL=llama3.1:8b` |
| No key at all | built-in loudness + speech heuristic (works, picks are weaker) | leave blank |

## 3. Posting: what is really free

Set a publisher per platform. `manual` always works and is 100% free: the clip and a ready caption land in
`data/ready/<platform>/` and you upload with two taps.

| Publisher | Cost | Notes |
|---|---|---|
| `manual` | free | Saves video + `.txt` caption in `data/ready/`. Works for all three platforms. |
| `youtube` | free | Direct YouTube Data API. **Projects created after 2020-07-28 that Google hasn't audited upload as private only**, and one upload costs ~1,600 of 10,000 daily quota units (about 6/day). Apply for an audit to go public. Setup steps are at the top of `publishers/youtube.py`. |
| `uploadpost` | free tier | One key for all platforms. Per their docs, **TikTok is not on the Free plan**; YouTube and Instagram are. Set `UPLOADPOST_API_KEY` and `UPLOADPOST_USER`. |
| `postiz` | free if self-hosted | Open source (github.com/gitroomhq/postiz-app). Connect accounts in its UI, put its API key in `POSTIZ_API_KEY`. Self-hosting means creating your own TikTok / Meta / Google developer apps and waiting for approval. API limit: 30 requests/hour. |

Realistic free combo: **YouTube via `youtube` or `uploadpost`, Instagram via `uploadpost`, TikTok via `manual`**
(or Postiz self-hosted once your TikTok app is approved). Example:

```
PUBLISH_YOUTUBE=uploadpost
PUBLISH_INSTAGRAM=uploadpost
PUBLISH_TIKTOK=manual
UPLOADPOST_API_KEY=...
UPLOADPOST_USER=myprofile
```

Instagram needs a Business or Creator account. TikTok apps that haven't passed TikTok's audit can only post
private/draft videos.

## 4. Automation

- **Review first (default):** clips wait in the web app; *Approve & queue* gives each clip the next free daily slot
  (`POST_TIMES`, max `MAX_POSTS_PER_DAY`).
- **Fully hands-off:** `AUTO_APPROVE=true`, then `python -m clipforge watch https://www.twitch.tv/streamer` and keep
  `python -m clipforge serve` (or `daemon`, no UI) running. New VODs are downloaded, clipped, captioned and posted.
- First time you watch a channel only its newest `WATCH_BACKFILL` VODs are clipped, not its whole history.
- A failed post is retried (up to 3 attempts, with a growing delay); after that it is marked `failed` in the queue with the error message.
- `SCHEDULE_MODE=provider` hands future post times to Upload-Post / Postiz / YouTube so your PC can be off at post time.

## 5. Commands

```
python -m clipforge serve                      web app + downloader + watcher + poster
python -m clipforge clip <url> [--start 1:20:00 --end 1:45:00] [-n 5] [--auto]
python -m clipforge watch <channel-url>        auto-clip new VODs  (--remove to stop, no arg to list)
python -m clipforge daemon                     same automation without the web UI
python -m clipforge approve all | 3 4 7        queue clips from the terminal
python -m clipforge post                       publish whatever is due now
python -m clipforge status
```

## 6. Layouts

`LAYOUT=blur` (default) full video centered over a blurred copy, nothing cropped. `crop` fills the vertical frame
(set `CROP_X`). `face` crops around the speaker's face per clip (needs `pip install opencv-python-headless`;
it picks one position per clip rather than tracking motion frame by frame).

## 7. Things to know

- Only clip content you own or have permission to use. Many streamers run official clipping programs; reposting
  without permission can get the channel strikes or removed, whatever the tool.
- Live streams can't be clipped until the VOD/replay exists.
- Kick channel listing depends on yt-dlp support; if auto-watch can't list a Kick channel, paste VOD links instead.
- Keep `.env`, `client_secret.json` and `data/youtube_token.json` private.

## Code map

`downloader.py` yt-dlp · `transcribe.py` faster-whisper · `highlights.py` LLM + offline picker · `render.py` ffmpeg
9:16 + captions · `face.py` speaker crop · `pipeline.py` ingest, schedule, publish · `watch.py` channel watcher ·
`publishers/` manual, uploadpost, postiz, youtube · `server.py` + `ui.html` review app · `cli.py`
