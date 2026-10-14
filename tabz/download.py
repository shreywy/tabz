"""Fetch a YouTube video (video stream only) into a temp folder."""
import os
import re
import tempfile

import yt_dlp

TEMP_DIR = os.path.join(tempfile.gettempdir(), "tabz")

# H.264 first: OpenCV's bundled decoder handles it everywhere, AV1/VP9 less so.
FORMAT = ("bv*[vcodec^=avc1][height<=1080]/bv*[height<=1080][ext=mp4]/"
          "bv*[height<=1080]/b[height<=1080]")


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
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
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
