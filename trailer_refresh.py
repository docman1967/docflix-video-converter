#!/usr/bin/env python3
"""
Bulk trailer refresh — replace the library's low-resolution trailers.

WHY THIS EXISTS
---------------
2026-08-17: 4,673 trailer files in the library, and a sample of 120 found 91%
of them at 360p (median 10.8 MB). Cause was not YouTube and not the downloader:
yt-dlp had been pinned at 2026.01.29 and could only see format 18 (640x360
pre-merged), so every format string correctly picked "the best available".
Updating yt-dlp restored the full ladder to 1080p. This script goes and gets
them again.

    "now how do we go about replacing all of those without incurring the
     wrath of Youtube?"  — Tony

DESIGN, and every point of it is about not getting throttled
------------------------------------------------------------
  * SERIAL. One download at a time. Concurrency is the fastest way to get
    noticed, and there is no deadline here.
  * JITTERED SLEEPS between items, never a fixed interval — a request exactly
    every 30s is a robot signature.
  * ADAPTIVE BACKOFF. Consecutive failures double the delay; a sustained run of
    them pauses for an hour. This matters more than the base rate: it lets the
    job start reasonably brisk and slow itself down only if YouTube objects,
    rather than grinding on and earning a real block.
    (Arthur throttled himself on 2026-08-17 with ~15 requests in ten minutes,
     then drew wrong conclusions from the results. Hence the caution.)
  * RESUMABLE. ~4,250 items over days WILL be interrupted. State lives in a
    manifest; re-running picks up where it stopped.
  * REPLACE ONLY ON VERIFIED IMPROVEMENT. Download to temp, probe it, and swap
    only if the new file is genuinely taller. A trailer that is 360p at source
    stays as it is and is marked done-not-retryable, so the job never comes back
    to it. (Banshee's trailer maxes out at 720p; Condor's at 1080p. It varies
    per upload, mostly by age.)
  * ANYTHING NEEDING A HUMAN GOES ON A LIST rather than being retried forever:
    no trailer on TMDB, or the source itself is low-res. Tony's ask —
    "drop Failures and low bitrate only files into a document so I can do them
    manually."

USAGE
-----
    trailer_refresh.py --scan            # build/refresh the worklist, no downloads
    trailer_refresh.py --run             # work the list (Ctrl-C safe, resumable)
    trailer_refresh.py --run --limit 20  # try a small batch first
    trailer_refresh.py --report          # regenerate the manual worklist
    trailer_refresh.py --status          # progress so far

State:  ~/.local/share/docflix/trailer_refresh.json
Report: ~/.local/share/docflix/trailer_manual_worklist.md
"""
import argparse
import json
import os
import random
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from modules.trailer_downloader import (            # noqa: E402
    download_trailer, find_ytdlp, tmdb_search, tmdb_trailer_candidates,
    tvdb_login, tvdb_search, tvdb_trailer_url,
)
from modules.constants import (                     # noqa: E402
    BETA_DEFAULT_TMDB_KEY, BETA_DEFAULT_TVDB_KEY,
)

DB       = os.path.expanduser("~/scripts/video_database/media_master.db")
STATE    = os.path.expanduser("~/.local/share/docflix/trailer_refresh.json")
REPORT   = os.path.expanduser("~/.local/share/docflix/trailer_manual_worklist.md")
MEDIA_EXT = {".mkv", ".mp4", ".webm", ".m4v", ".mov", ".avi"}

MIN_HEIGHT   = 720     # anything at or above this is left alone
SLEEP_MIN    = 20      # base jitter window between items, seconds
SLEEP_MAX    = 60
BACKOFF_CAP  = 900     # a single sleep never exceeds this
PAUSE_AFTER  = 6       # consecutive failures before a long pause
PAUSE_SECS   = 3600
# ⚠️ Consecutive BOT-CHECKS before giving up. One is weather — YouTube
# challenges individual requests at random even on a signed-in session. Three
# in a row means the machine or account is genuinely flagged and every further
# request is just digging. Measured both regimes on 2026-08-21 and 08-23.
BOTCHECK_LIMIT = 3

_stop = {"now": False}


def _sigint(*_a):
    if _stop["now"]:
        sys.exit(130)
    _stop["now"] = True
    print("\n  stopping after this item (Ctrl-C again to abort now)...", flush=True)


signal.signal(signal.SIGINT, _sigint)


# ── state ────────────────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {"items": {}, "updated": None}


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    st["updated"] = datetime.now().isoformat(timespec="seconds")
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


def probe_height(path):
    """Video height, or 0 if unreadable."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30)
        return int((r.stdout or "0").strip() or 0)
    except Exception:
        return 0


# ── scan ─────────────────────────────────────────────────────────────────────
def find_trailers(folder):
    """Trailer files under a title's folder: <folder>/Trailers/* and *-trailer.*"""
    out = []
    tdir = os.path.join(folder, "Trailers")
    if os.path.isdir(tdir):
        for f in os.listdir(tdir):
            if os.path.splitext(f)[1].lower() in MEDIA_EXT:
                out.append(os.path.join(tdir, f))
    try:
        for f in os.listdir(folder):
            if "-trailer" in f.lower() and os.path.splitext(f)[1].lower() in MEDIA_EXT:
                out.append(os.path.join(folder, f))
    except OSError:
        pass
    return out


def scan(st, verbose=True):
    """Walk the library via the DB and record every trailer's current height."""
    if not os.path.exists(DB):
        print(f"  media DB not found: {DB}")
        return st
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    titles = []
    for t, fp in c.execute("select title, folder_path from shows where folder_path is not null"):
        titles.append(("tv", t, None, fp))
    try:
        for t, y, fp in c.execute("select title, year, folder_path from movies "
                                  "where folder_path is not null"):
            titles.append(("movie", t, y, fp))
    except sqlite3.OperationalError:
        for t, fp in c.execute("select title, folder_path from movies "
                               "where folder_path is not null"):
            titles.append(("movie", t, None, fp))

    seen = 0
    for kind, title, year, folder in titles:
        if not os.path.isdir(folder):
            continue
        for path in find_trailers(folder):
            seen += 1
            prev = st["items"].get(path, {})
            # "keep" = Tony looked and decided the current trailer stays. That is a
            # judgement, not a failure — it must survive a rescan or the job will
            # keep re-proposing something he already said no to.
            if prev.get("status") in ("done", "source_limited", "no_trailer", "keep"):
                continue
            h = probe_height(path)
            st["items"][path] = {
                "kind": kind, "title": title, "year": year, "folder": folder,
                "height": h,
                "status": "ok" if h >= MIN_HEIGHT else "pending",
            }
        if verbose and seen and seen % 500 == 0:
            print(f"    ...{seen} trailers scanned", flush=True)
    if verbose:
        print(f"  scanned {seen} trailer files")
    return st


# ── work ─────────────────────────────────────────────────────────────────────
def worklist(st):
    return [(p, v) for p, v in st["items"].items() if v.get("status") == "pending"]


def run(st, limit=None, dry=False, vcodec="h265", cookies=None):
    ytdlp = find_ytdlp()
    if not ytdlp:
        print("  yt-dlp not found."); return st
    key = BETA_DEFAULT_TMDB_KEY
    # ⚠️ TVDB as a FALLBACK, not a replacement: TMDB has far better trailer
    # coverage overall, but it has nothing at all for a meaningful slice of the
    # library. Spot-checked 10 of the "no trailer on TMDB" titles on 2026-08-18
    # and TVDB had 3 of them (Amazing Stories, Banshee, Batman and Robin 1949).
    # At ~20% of items hitting that bucket, this is worth a few hundred manual
    # lookups Tony doesn't have to do.
    tvdb_tok = None
    try:
        tvdb_tok = tvdb_login(BETA_DEFAULT_TVDB_KEY)
    except Exception as e:
        print(f"  (TVDB fallback unavailable: {str(e)[:50]})", flush=True)
    todo = worklist(st)
    if limit:
        todo = todo[:limit]
    print(f"  {len(todo)} to attempt (of {len(worklist(st))} pending)\n", flush=True)

    fails = 0
    botchecks = 0          # CONSECUTIVE bot-checks; reset by any other outcome
    for n, (path, info) in enumerate(todo, 1):
        if _stop["now"]:
            break
        title, kind = info["title"], info["kind"]
        cur_h = info.get("height", 0)
        label = f"[{n}/{len(todo)}] {title[:44]}"

        # ── find a trailer URL ────────────────────────────────────────────
        urls = []
        try:
            hits = tmdb_search(key, title, "tv" if kind == "tv" else "movie")
            if info.get("year"):
                exact = [h for h in hits if str(h.get("year")) == str(info["year"])]
                hits = exact or hits
            if hits:
                urls = tmdb_trailer_candidates(key, "tv" if kind == "tv" else "movie",
                                               hits[0]["id"])
        except Exception as e:
            print(f"  {label}  lookup error: {str(e)[:50]}", flush=True)

        if not urls and tvdb_tok:
            try:
                k = "tv" if kind == "tv" else "movie"
                h = tvdb_search(tvdb_tok, title, k)
                u = tvdb_trailer_url(tvdb_tok, k, h[0]["id"]) if h else None
                if u:
                    urls = [u]
                    info["source"] = "tvdb"
            except Exception:
                pass

        if not urls:
            info["status"] = "no_trailer"
            info["note"] = "no trailer on TMDB or TVDB"
            print(f"  {label}  -> no trailer on TMDB or TVDB (manual list)", flush=True)
            save_state(st)
            continue

        if dry:
            print(f"  {label}  would fetch {urls[0]}  ({len(urls)} candidate(s))", flush=True)
            continue

        # ── download to temp, verify, then swap ───────────────────────────
        ext = os.path.splitext(path)[1] or ".mkv"
        # AVI cannot carry HEVC; leave those alone rather than fail the item.
        vc = "copy" if ext.lower() == ".avi" else vcodec
        tmp = path + ".new" + ext
        # ⚠️ Fall through the candidates. download_trailer already retries the
        # intermittent 403 four times against ONE url; if that url is simply bad
        # we move to the next TMDB entry rather than abandoning a title that has
        # three other 1080p trailers sitting there (Better Call Saul, 2026-08-17).
        ok, msg = False, "no candidates"
        for ci, url in enumerate(urls, 1):
            ok, msg = download_trailer(ytdlp, url, tmp, container=ext.lstrip("."),
                                       strip=True, log=lambda s: None, vcodec=vc,
                                       cookies_from=cookies)
            if ok:
                if ci > 1:
                    print(f"  {label}  (candidate {ci} of {len(urls)})", flush=True)
                break
            if _stop["now"]:
                break
        if not ok:
            fails += 1
            info["status"] = "pending"
            info["last_error"] = msg[:120]
            print(f"  {label}  FAIL {msg[:52]}", flush=True)
            # ⚠️ BOT-CHECKS COME IN TWO REGIMES and only one is fatal.
            #
            #   OCCASIONAL — YouTube challenges individual requests at random,
            #     even on a signed-in session. Measured 2026-08-23: one blocked
            #     video, then a clean fetch seconds later on the same cookies,
            #     then another block. Stopping on the first of these threw away
            #     14 good items for nothing.
            #
            #   SUSTAINED — the machine or account is actually flagged and
            #     EVERYTHING fails. 2026-08-21: from item 2371 onward, every
            #     single request. Carrying on there turned a soft flag into a
            #     real block over ~100 items.
            #
            # So: count CONSECUTIVE challenges. One is weather; three in a row
            # is a wall. Backoff is the wrong tool for the second case — there
            # is nothing to back off from.
            _e = (msg or "").lower()
            if ("not a bot" in _e or "sign in to confirm" in _e
                    or "please sign in" in _e):
                botchecks += 1
                if botchecks >= BOTCHECK_LIMIT:
                    print(f"\n  ⛔ {botchecks} BOT-CHECKS IN A ROW — stopping.\n"
                          "     This is the sustained kind, not the occasional\n"
                          "     kind; every further request would fail too.\n"
                          "     Fix: refresh the cookies\n"
                          "       ~/.cache/docflix/refresh-bulk-cookies.sh\n"
                          "     or wait it out. Nothing is lost; the manifest\n"
                          "     resumes where it stopped.", flush=True)
                    save_state(st)
                    return st
                print(f"     (bot-check {botchecks}/{BOTCHECK_LIMIT} — "
                      f"carrying on)", flush=True)
            else:
                botchecks = 0
        else:
            new_h = probe_height(tmp)
            if new_h > cur_h:
                os.replace(tmp, path)
                info.update(status="done", height=new_h, was=cur_h,
                            done_at=datetime.now().isoformat(timespec="seconds"))
                print(f"  {label}  {cur_h}p -> {new_h}p", flush=True)
                fails = 0
                botchecks = 0
            else:
                # ⚠️ Not a failure. The upload itself is this small -- mark it so
                # the job never returns to it, and put it on the manual list.
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                info.update(status="source_limited", best_available=new_h)
                print(f"  {label}  source is only {new_h}p (manual list)", flush=True)
                fails = 0
        save_state(st)

        # ── politeness ────────────────────────────────────────────────────
        if n < len(todo) and not _stop["now"]:
            if fails >= PAUSE_AFTER:
                print(f"  -- {fails} failures in a row; pausing {PAUSE_SECS//60} min", flush=True)
                _sleep(PAUSE_SECS)
                fails = 0
            else:
                base = random.uniform(SLEEP_MIN, SLEEP_MAX)
                delay = min(base * (2 ** fails), BACKOFF_CAP)
                _sleep(delay)
    write_report(st)
    return st


def _sleep(secs):
    end = time.time() + secs
    while time.time() < end:
        if _stop["now"]:
            return
        time.sleep(min(1.0, end - time.time()))


# ── report ───────────────────────────────────────────────────────────────────
def _yt_search(title, kind, year=None):
    import urllib.parse
    q = f"{title} {year or ''} {'TV series' if kind == 'tv' else ''} official trailer"
    return "https://www.youtube.com/results?search_query=" + urllib.parse.quote(q.strip())


def classify_error(err):
    """Bucket a recorded failure by WHAT CAN STILL BE DONE ABOUT IT.

    ⚠️ Sorting by error text alone is misleading. On 2026-08-23 the manifest
    held 404 'failures', and 380 of them were not failures any more:

        275  HTTP 403      the stale yt-dlp era, fixed by the nightly
         92  bot-check     Friday's IP flag, cleared by cookies
        ~15  "Please sign in"   same thing, message truncated

    Only ~23 were genuinely dead. A report that treats every error as a
    judgement case hands over 404 things to do by hand instead of 23.

    Returns (bucket, retryable).
    """
    e = (err or "").lower()
    if "not a bot" in e or "sign in to confirm" in e or "please sign in" in e \
            or "exporting-youtube-cookies" in e:
        return "blocked without cookies", True
    if "403" in e or "forbidden" in e:
        return "HTTP 403 (stale yt-dlp era)", True
    if "private" in e:
        return "private video", False
    if "removed" in e or "unavailable" in e or "not available" in e:
        return "removed or unavailable", False
    if "region" in e or "country" in e or "geo" in e:
        return "region blocked", False
    if "drm" in e:
        return "DRM protected", False
    if "age" in e:
        return "age-gated", False
    return "other", False


def write_report(st):
    """A list built for a person to grind through, worst first.

    ⚠️ This is now a MANUAL worklist, not a status report. Tony's call
    2026-08-23: the IP tolerates roughly 25 automated items a day, so the bulk
    job cannot finish the remaining ~1,300 in any reasonable time. He has done
    this before — "I downloaded thousands of trailers manually" — and would
    rather just work it. See [[feedback_hands-on-not-a-manager]]: the job is to
    make each manual action cheap, NOT to keep trying to remove it.

    So: every outstanding item, in ONE list, ordered by how bad the current
    trailer is. A 240p trailer is a bigger win than a 480p one, and if he only
    gets through fifty in a sitting they should be the fifty that matter most.

    Each entry carries what he needs to act without going and looking it up:
    current height, a ready-made YouTube search, the destination folder, and —
    where there is one — the reason the automated pass could not do it.

    ⚠️ Rescan clears finished work: `--scan` re-probes anything still `pending`
    and flips it to `ok` at >=720p. It does NOT re-probe `source_limited`.
    """
    from collections import Counter
    items = st["items"]
    no_tr = [(p, v) for p, v in items.items() if v.get("status") == "no_trailer"]
    low   = [(p, v) for p, v in items.items() if v.get("status") == "source_limited"]
    pend  = [(p, v) for p, v in items.items() if v.get("status") == "pending"]

    fresh, retry, dead = [], [], []
    for p, v in pend:
        if not v.get("last_error"):
            fresh.append((p, v))
        else:
            bucket, ok = classify_error(v["last_error"])
            (retry if ok else dead).append((p, v, bucket))
    errs = [(p, v) for p, v, _b in dead]

    def entry(f, v, note):
        yr = f" ({v['year']})" if v.get("year") else ""
        f.write(f"- [ ] **{v['title']}**{yr} — {note}  \n")
        f.write(f"      [search YouTube]({_yt_search(v['title'], v.get('kind'), v.get('year'))})  \n")
        f.write(f"      `{v['folder']}`\n")

    # Everything outstanding, in one pile, worst first.
    todo = []
    for pth, v in fresh:
        todo.append((v, None))
    for pth, v, bucket in retry:
        todo.append((v, None))          # retryable = no lasting reason
    for pth, v, bucket in dead:
        todo.append((v, bucket))
    for pth, v in no_tr:
        todo.append((v, "no trailer on TMDB or TVDB"))
    for pth, v in low:
        todo.append((v, f"best source found was {v.get('best_available', '?')}p"))

    def _h(v):
        try:    return int(v.get("height") or 0)
        except (TypeError, ValueError): return 0
    # ⚠️ Worst FIRST. If he only gets through fifty in a sitting, they should be
    # the fifty with the most to gain.
    todo.sort(key=lambda t: (_h(t[0]), t[0]["title"].lower()))

    bands = [(0, 300, "Under 300p — the worst of it"),
             (300, 400, "300-399p"),
             (400, 600, "400-599p"),
             (600, 10000, "600p and up — marginal gains")]

    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w") as f:
        f.write("# Trailers to do\n\n")
        f.write(f"_Generated {datetime.now():%Y-%m-%d %H:%M}_ — "
                f"**{len(todo)} outstanding**\n\n")
        f.write(f"Done so far: "
                f"{sum(1 for v in items.values() if v.get('status') == 'done')} replaced, "
                f"{sum(1 for v in items.values() if v.get('status') == 'ok')} already fine, "
                f"{sum(1 for v in items.values() if v.get('status') == 'keep')} kept by choice.\n\n")
        f.write("Worst first. Paste a YouTube URL into the Trailer Grabber\n"
                "(Media Suite → Trailer Grabber → Trailer URL); it downloads,\n"
                "encodes to HEVC and drops it in the right folder.\n\n")
        f.write("When you have done a batch, run `trailer_refresh.py --scan` and\n"
                "anything now 720p or better disappears off this list.\n\n")

        for lo, hi, label in bands:
            chunk = [(v, why) for v, why in todo if lo <= _h(v) < hi]
            if not chunk:
                continue
            f.write(f"\n## {label} ({len(chunk)})\n\n")
            for v, why in chunk:
                note = f"have {_h(v)}p"
                if why:
                    note += f" — {why}"
                entry(f, v, note)
    print(f"  report -> {REPORT}", flush=True)


def status(st):
    from collections import Counter
    c = Counter(v.get("status", "?") for v in st["items"].values())
    total = sum(c.values())
    print(f"  {total} trailers known")
    for k in ("pending", "done", "ok", "source_limited", "no_trailer", "keep"):
        if c.get(k):
            print(f"     {k:16} {c[k]:5}")
    done = [v for v in st["items"].values() if v.get("status") == "done"]
    if done:
        gain = sum(v.get("height", 0) - v.get("was", 0) for v in done) / len(done)
        print(f"  average gain on replaced: +{gain:.0f}p")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan", action="store_true", help="build/refresh the worklist")
    ap.add_argument("--run", action="store_true", help="work the list")
    ap.add_argument("--report", action="store_true", help="regenerate the manual worklist")
    ap.add_argument("--status", action="store_true", help="progress so far")
    ap.add_argument("--limit", type=int, help="only attempt N items this run")
    ap.add_argument("--cookies", metavar="FILE",
                    help="cookie jar for yt-dlp. ⚠️ Use a THROWAWAY account: "
                         "bulk downloading while signed in as yourself is "
                         "attributable in a way anonymous throttling is not.")
    ap.add_argument("--dry-run", action="store_true", help="resolve URLs, download nothing")
    # ⚠️ Default is h265, NOT copy: Tony wants trailers to match the library
    # (HEVC CQ32 10-bit). YouTube never serves HEVC, so this is always a local
    # NVENC transcode -- a few seconds per 2-minute clip. --copy skips it.
    ap.add_argument("--copy", action="store_true",
                    help="keep the downloaded codec instead of re-encoding to HEVC")
    a = ap.parse_args()
    st = load_state()
    if a.scan or (a.run and not st["items"]):
        print("  scanning library...")
        st = scan(st)
        save_state(st)
    if a.run:
        if a.cookies:
            _cf = os.path.expanduser(a.cookies)
            if not os.path.isfile(_cf):
                print(f"  cookie file not found: {_cf}")
                return
            print(f"  using cookies: {_cf}", flush=True)
        else:
            _cf = None
            print("  no --cookies given; unauthenticated requests get "
                  "bot-checked after the first few", flush=True)
        st = run(st, limit=a.limit, dry=a.dry_run,
                 vcodec="copy" if a.copy else "h265", cookies=_cf)
        save_state(st)
    if a.report:
        write_report(st)
    if a.status or not (a.scan or a.run or a.report):
        status(st)


if __name__ == "__main__":
    main()
