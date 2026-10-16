"""Saved songs on disk, and a tiny local web server so the window can load their images
by URL instead of shipping megabytes of base64 through the bridge.

One folder per song:
    manifest.json   everything about the song (see save())
    strips/         the cleaned strips (ink PNG) and the raw colour version (JPG/PNG),
                    kept so layout, details and strip choices can be changed any time
    bars/           the strips cut into bars at one string spacing, for the reader
    thumb.png       first screen of tab, for the library card
    art.jpg         cover art, when the song was found online
    score.gp*       a Guitar Pro file, drawn and played by the window
    original.*      an imported PDF or picture, as it came
    backing.*       audio to play along with

kind is "video" (from YouTube or a video file), "pdf", "image" or "gp".
"""
import functools
import glob
import http.server
import json
import os
import shutil
import threading
import time
import urllib.parse

import re

import cv2
import numpy as np

from . import layout, transcribe
from .imports import guitar_from_text

TEX_VERSION = 1      # bump when transcribe.py reads tab better, to read every song again


class Library:
    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.base_url = _serve(root)

    # ---- reading --------------------------------------------------------
    def folder(self, sid):
        return os.path.join(self.root, os.path.basename(sid))

    def manifest(self, sid):
        return _read_json(os.path.join(self.folder(sid), "manifest.json"))

    def url(self, sid, name):
        if not name:
            return None
        path = os.path.join(self.folder(sid), name)
        if not os.path.exists(path):
            return None
        return f"{self.base_url}/{urllib.parse.quote(sid)}/{name}?v={int(os.path.getmtime(path))}"

    def manifests(self):
        for mf in glob.glob(os.path.join(self.root, "*", "manifest.json")):
            m = _read_json(mf)
            if m:
                yield m

    def known(self):
        """Content hashes of every file already imported, so importing again skips them."""
        return {h for m in self.manifests() for h in m.get("sources", [])}

    def new_id(self, name):
        base = re.sub(r'[<>:"/\\|?*#%\x00-\x1f]', "", name).strip(" .")[:120] or "song"
        sid, k = base, 2
        while os.path.exists(self.folder(sid)):
            sid, k = f"{base} ({k})", k + 1
        return sid

    def write(self, m):
        os.makedirs(self.folder(m["id"]), exist_ok=True)
        m["updated"] = time.time()
        with open(os.path.join(self.folder(m["id"]), "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(m, f)
        return m

    def songs(self):
        """Card data for every song, newest first. Images go by URL."""
        out, genres = [], {}
        for m in self.manifests():
            sid = m["id"]
            pdf = m.get("pdf") if m.get("pdf") and os.path.exists(m["pdf"]) else None
            i = m.get("info") or {}
            kind = m.get("kind", "video")
            if i.get("genre") and i.get("artist"):
                genres.setdefault(i["artist"].lower(), i["genre"])
            out.append({
                "song": sid, "name": m["title"], "url": m.get("url"), "path": pdf, "kind": kind,
                "info": i, "notes": m.get("notes", ""),
                "channel": m.get("channel", ""), "duration": m.get("duration", 0),
                "collection": m.get("collection"), "created": m.get("created", 0),
                "played": m.get("played", 0), "when": m.get("updated", 0),
                "thumb": self.url(sid, "thumb.png"), "art": self.url(sid, "art.jpg"),
                "editable": bool(m.get("strips")) and kind == "video",
                "folder": m.get("folder"), "backing": bool((m.get("backing") or {}).get("file")),
                "fav": bool(m.get("fav")), "playable": bool(m.get("score") or m.get("tex")),
                "guitar": i.get("guitar") or guitar_from_text(m["title"], m.get("channel", "")),
            })
        for it in out:          # a song the lookup missed takes its artist's genre
            it["genre"] = it["info"].get("genre") or genres.get((it["info"].get("artist") or "").lower())
        out.sort(key=lambda i: i["created"] or i["when"], reverse=True)
        return out

    def reader_data(self, sid):
        """What the reader needs: the manifest with bar image URLs."""
        m = self.manifest(sid)
        if not m:
            return None
        for sg in m.get("segments", []):
            sg["src"] = self.url(sid, sg["file"])
        m["art"] = self.url(sid, "art.jpg")
        pdf = m.get("pdf")
        m["pdf_src"] = f"{self.base_url}/{urllib.parse.quote(sid)}/tab.pdf?v={int(os.path.getmtime(pdf))}"             if pdf and os.path.exists(pdf) else None
        m["kind"] = m.get("kind", "video")
        m["score_src"] = self.url(sid, m.get("score"))
        m["original_src"] = self.url(sid, m.get("original"))
        m["backing_src"] = self.url(sid, (m.get("backing") or {}).get("file"))
        m["tex_src"] = self.url(sid, m.get("tex"))
        kept = [s for k, s in enumerate(m.get("strips", [])) if k not in set(m.get("removed", []))]
        if kept and m["kind"] == "video":
            # When each bar was on screen, so the reader's cursor can follow the video's
            # timing: a strip's time is shared across its bars by width.
            for g, st in enumerate(kept):
                segs = [sg for sg in m.get("segments", []) if sg.get("group") == g]
                total, x = sum(sg["w"] for sg in segs) or 1, 0
                for sg in segs:
                    sg["t0"] = st["t0"] + (st["t1"] - st["t0"]) * x / total
                    x += sg["w"]
                    sg["t1"] = st["t0"] + (st["t1"] - st["t0"]) * x / total
        m.pop("strips", None)
        return m

    def load_strips(self, sid):
        """[(ink 0..1, colour BGR, t0, t1)] as saved by save()."""
        m, f = self.manifest(sid), self.folder(sid)
        out = []
        for s in m.get("strips", []):
            ink = cv2.imread(os.path.join(f, s["file"]), cv2.IMREAD_GRAYSCALE)
            col = cv2.imread(os.path.join(f, s["color"])) if s.get("color") else None
            if ink is None:
                continue
            out.append(((255 - ink.astype(np.float32)) / 255, col, s["t0"], s["t1"]))
        return out

    def transcribe(self, sid):
        """Read the song's tab images into notes (score.atex), so it can be played as a score.
        The images stay as they are; this is an extra, rougher view of them."""
        m = self.manifest(sid)
        if not m:
            return None
        removed = set(m.get("removed", []))
        inks = [s[0] for k, s in enumerate(self.load_strips(sid)) if k not in removed]
        tex, stats = transcribe.transcribe(inks, m.get("sp") or 20, m.get("n") or 6, m.get("info")) if inks else (None, {})
        path = os.path.join(self.folder(sid), "score.atex")
        if tex:
            with open(path, "w", encoding="utf-8") as f:
                f.write(tex)
            m["tex"] = "score.atex"
        else:
            m.pop("tex", None)
        m = self.manifest(sid) | {"tex": m.get("tex"), "tex_v": TEX_VERSION,
                                  "tex_stats": {k: stats.get(k) for k in ("notes", "bars")}}
        if not m["tex"]:
            m.pop("tex")
        self.write(m)
        return m.get("tex")

    # ---- writing ---------------------------------------------------------
    def save(self, sid, meta, strips, sp, n, screen_w, removed=(), pdf=None, art=None, collection=None,
             write_strips=True, bars=None, extra=None):
        """Store a song. strips: [(ink, colour, t0, t1)]. Kept fields (tempo set in the
        reader, play count, created time, backing track) survive a re-save. bars: cut
        every strip into bars for the reader (lines of a PDF), not only scrolled ones."""
        folder = self.folder(sid)
        old = self.manifest(sid) or {}
        os.makedirs(folder, exist_ok=True)

        strip_rows = old.get("strips", [])
        if write_strips:
            shutil.rmtree(os.path.join(folder, "strips"), ignore_errors=True)
            os.makedirs(os.path.join(folder, "strips"))
            strip_rows = []
            for k, (ink, col, t0, t1) in enumerate(strips):
                name = f"strips/{k:03d}.png"
                cv2.imwrite(os.path.join(folder, name), (255 - ink * 255).astype(np.uint8))
                cname = None
                if col is not None:
                    # JPEG tops out at 65535px; very long scrolled strips fall back to PNG
                    cname = f"strips/{k:03d}c." + ("jpg" if col.shape[1] < 65000 else "png")
                    cv2.imwrite(os.path.join(folder, cname), col, [cv2.IMWRITE_JPEG_QUALITY, 88])
                strip_rows.append({"file": name, "color": cname, "t0": t0, "t1": t1})

        keep = [(ink, sp) for k, (ink, _, _, _) in enumerate(strips) if k not in set(removed)]
        bars = old.get("bars", False) if bars is None else bars
        shutil.rmtree(os.path.join(folder, "bars"), ignore_errors=True)
        os.makedirs(os.path.join(folder, "bars"))
        items, unit, sp_ref = [], screen_w, sp
        if keep:
            segs, unit, sp_ref = layout.segments(keep, screen_w, n, bars=bars)
            cv2.imwrite(os.path.join(folder, "thumb.png"), _thumb(segs, unit))
            for k, sg in enumerate(segs):
                name = f"bars/{k:03d}.png"
                im = sg.pop("img")
                cv2.imwrite(os.path.join(folder, name), im)
                items.append({"file": name, "w": im.shape[1], "h": im.shape[0], **sg})
        if art:
            with open(os.path.join(folder, "art.jpg"), "wb") as f:
                f.write(art)

        manifest = {
            **old, **(extra or {}),
            "id": sid, "title": meta["title"], "url": meta["url"], "bars": bars,
            "channel": meta.get("channel", ""), "duration": meta.get("duration", 0),
            "notes": meta.get("notes", ""), "info": meta.get("info") or {},
            "pdf": pdf or old.get("pdf"), "collection": collection if collection is not None else old.get("collection"),
            "created": old.get("created") or time.time(), "updated": time.time(),
            "bpm": old.get("bpm"), "beats": old.get("beats"), "played": old.get("played", 0),
            "sp": sp, "n": n, "screen_w": screen_w, "unit": unit, "sp_ref": sp_ref,
            "removed": sorted(removed), "strips": strip_rows, "segments": items,
        }
        with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        return manifest

    def update(self, sid, **fields):
        path = os.path.join(self.folder(sid), "manifest.json")
        m = _read_json(path)
        if m:
            m.update(fields)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(m, f)
        return m

    def delete(self, sid):
        shutil.rmtree(self.folder(sid), ignore_errors=True)


def _thumb(segs, unit, width=640):
    """First screen-width of a song, as a small picture for the library card."""
    row, used = [], 0
    for sg in segs:
        if used >= unit:
            break
        row.append(sg)
        used += sg["img"].shape[1]
    above = max(int(sg["staff_y"]) for sg in row)
    below = max(sg["img"].shape[0] - int(sg["staff_y"]) for sg in row)
    out = np.full((above + below, sum(sg["img"].shape[1] for sg in row)), 255, np.uint8)
    x = 0
    for sg in row:
        im, top = sg["img"], above - int(sg["staff_y"])
        out[top:top + im.shape[0], x:x + im.shape[1]] = im
        x += im.shape[1]
    out = out[:, :int(unit)]
    return cv2.resize(out, (width, max(1, int(out.shape[0] * width / out.shape[1]))), interpolation=cv2.INTER_AREA)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def translate_path(self, path):
        """<song>/tab.pdf is that song's PDF, wherever the output folder put it."""
        sid, _, name = urllib.parse.unquote(path.split("?")[0]).strip("/").partition("/")
        if name == "tab.pdf":
            m = _read_json(os.path.join(self.directory, os.path.basename(sid), "manifest.json")) or {}
            return m.get("pdf") or ""
        return super().translate_path(path)

    def end_headers(self):
        self.send_header("Cache-Control", "max-age=31536000")    # URLs carry ?v=mtime
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


def _serve(root):
    """Serve the library folder read-only on a free localhost port. Nothing else is
    reachable through it besides each song's own PDF, and it only listens on 127.0.0.1."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=root))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}"
