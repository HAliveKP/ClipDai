from __future__ import annotations

import argparse
import sys
import time

from . import config, pipeline, server, watch
from .db import DB
from .utils import log, parse_time


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="clipdai", description="Turn streams & long videos into posted short clips.")
    ap.add_argument("--env", help="path to .env (default ./.env)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="start the web app + auto-download + auto-post (recommended)")

    p = sub.add_parser("clip", help="download a URL/file and make clips right now")
    p.add_argument("url")
    p.add_argument("--start", help="only use from this time (e.g. 1:30:00)")
    p.add_argument("--end", help="only use up to this time")
    p.add_argument("-n", type=int, help="number of clips")
    p.add_argument("--auto", action="store_true", help="skip review: queue straight for posting")

    p = sub.add_parser("watch", help="auto-clip new VODs from a channel (Twitch/YouTube/Kick channel link)")
    p.add_argument("url", nargs="?")
    p.add_argument("--remove", action="store_true")

    sub.add_parser("daemon", help="headless: watch channels, clip, post (no web UI)")
    sub.add_parser("post", help="publish everything that is due right now")
    sub.add_parser("status", help="show queue")

    p = sub.add_parser("approve", help="approve clips for posting: ids or 'all'")
    p.add_argument("ids", nargs="+")

    a = ap.parse_args(argv)
    s = config.load(a.env)
    db = DB(s.db_path)

    if a.cmd == "serve":
        server.serve(s)
    elif a.cmd == "clip":
        if a.auto:
            s.auto_approve = True
        n = pipeline.process_source(s, db, a.url, parse_time(a.start), parse_time(a.end), a.n)
        log(f"{n} clips in {s.clips_dir}")
        if not s.auto_approve and n:
            log("Review them with `python -m clipdai serve`, or `python -m clipdai approve all`")
    elif a.cmd == "watch":
        if not a.url:
            for w in db.watches():
                print(w["url"])
            return
        if a.remove:
            db.del_watch(a.url)
        else:
            db.add_watch(a.url)
            log(f"watching {a.url}. Run `python -m clipdai daemon` (or `serve`) to keep it going.")
    elif a.cmd == "daemon":
        log("daemon started (Ctrl+C to stop)")
        last_watch = 0.0
        try:
            while True:
                if time.time() - last_watch > s.check_minutes * 60:
                    watch.check_watches(s, db)
                    last_watch = time.time()
                pipeline.process_pending(s, db)
                pipeline.run_due_jobs(s, db)
                time.sleep(20)
        except KeyboardInterrupt:
            pass
    elif a.cmd == "post":
        pipeline.run_due_jobs(s, db)
    elif a.cmd == "approve":
        rows = db.clips("pending") if a.ids == ["all"] else [db.clip(int(i)) for i in a.ids]
        for r in rows:
            if r and r["status"] == "pending":
                due = pipeline.enqueue(s, db, r["id"])
                log(f"clip {r['id']} queued for {due:%Y-%m-%d %H:%M}")
    elif a.cmd == "status":
        for r in db.clips():
            print(f"clip {r['id']:>3} [{r['status']:<8}] {r['title'][:50]}")
        for j in db.all_jobs():
            print(f"  job {j['id']:>3} clip {j['clip_id']} {j['platform']:<9} {j['status']:<7} {j['result'] or ''}")


if __name__ == "__main__":
    main(sys.argv[1:])
