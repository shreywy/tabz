"""Fetch a YouTube video (video stream only) into a temp folder."""
import os
import re
import tempfile
import time

import yt_dlp

TEMP_DIR = os.path.join(tempfile.gettempdir(), "tabz")

# H.264 first: OpenCV's bundled decoder handles it everywhere, AV1/VP9 less so.
FORMAT = ("bv*[vcodec^=avc1][height<=1080]/bv*[height<=1080][ext=mp4]/"
          "bv*[height<=1080]/b[height<=1080]/bv*/b")


def safe_filename(title):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title).strip(" .")
    return name[:150] or "untitled"


def download(url, on_progress=None, dest=TEMP_DIR):
    """Download the video and return (path, info). info has title, url, channel, duration."""
    os.makedirs(dest, exist_ok=True)

    def hook(d):
        if on_progress and d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                on_progress(d.get("downloaded_bytes", 0) / total)

    opts = {
        "format": FORMAT,
        "outtmpl": os.path.join(dest, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        "retries": 10,
        "fragment_retries": 10,
        # Smaller ranged requests: YouTube intermittently 403s long single requests.
        "http_chunk_size": 10 * 1024 * 1024,
    }
    # YouTube sometimes refuses one player client (HTTP 403 partway, or a format list with
    # no real video in it) and not another, so fall through a few before giving up.
    last = None
    clients = (None, ["web_safari"], ["tv"], ["mweb"])
    for attempt, client in enumerate(clients + clients):
        if attempt == len(clients):
            time.sleep(3)                   # second round after a short pause
        o = dict(opts)
        if client:
            o["extractor_args"] = {"youtube": {"player_client": client}}
        try:
            with yt_dlp.YoutubeDL(o) as ydl:
                info = ydl.extract_info(url, download=True)
                path = ydl.prepare_filename(info)
            break
        except yt_dlp.utils.DownloadError as e:
            last = e
            # client-specific refusals: try the next client. Anything else is final.
            if not any(k in str(e) for k in ("403", "Forbidden", "format is not available", "not a bot",
                                             "needs to be reloaded", "Read timed out", "IncompleteRead")):
                raise
            for f in os.listdir(dest):          # drop the half-written file before retrying
                if f.endswith((".part", ".ytdl")):
                    try:
                        os.remove(os.path.join(dest, f))
                    except OSError:
                        pass
    else:
        raise last
    return path, {
        "id": info.get("id"),
        "title": info.get("title") or "Untitled",
        "url": info.get("webpage_url") or url,
        "channel": info.get("channel") or info.get("uploader") or "",
        "duration": info.get("duration") or 0,
        "description": info.get("description") or "",
    }


def search(query, n=24):
    """YouTube search results (no downloads). Flat extraction keeps it to one request."""
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        res = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    out = []
    for e in res.get("entries") or []:
        vid = e.get("id")
        if not vid or e.get("ie_key") not in (None, "Youtube"):
            continue
        out.append({
            "id": vid,
            "title": e.get("title") or "",
            "channel": e.get("channel") or e.get("uploader") or "",
            "duration": e.get("duration") or 0,
            "views": e.get("view_count") or 0,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "thumb": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
        })
    return out


def is_playlist(url):
    """A playlist page, or a video link that also carries a playlist."""
    return "list=" in url and "youtu" in url


def playlist(url):
    """(title, [{url, title, ok, reason}]) for every entry, without downloading any.
    Private and deleted entries come back with ok=False so they can be reported."""
    m = re.search(r"[?&]list=([\w-]+)", url)
    if m:
        url = f"https://www.youtube.com/playlist?list={m.group(1)}"
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True,
            "ignoreerrors": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        res = ydl.extract_info(url, download=False) or {}
    out = []
    for e in res.get("entries") or []:
        if not e or not e.get("id"):
            continue
        title = e.get("title") or ""
        bad = title in ("[Private video]", "[Deleted video]") or e.get("availability") in ("private", "needs_auth")
        out.append({"url": f"https://www.youtube.com/watch?v={e['id']}", "title": title,
                    "ok": not bad, "reason": "Private or deleted" if bad else None})
    return res.get("title") or "Playlist", out


def peek_title(url):
    """The video's title without downloading it (about a second). None if it fails."""
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True,
                               "skip_download": True, "extract_flat": True}) as ydl:
            return (ydl.extract_info(url, download=False) or {}).get("title")
    except Exception:
        return None
