"""Saved songs on disk, and a tiny local web server so the window can load their images
by URL instead of shipping megabytes of base64 through the bridge.

One folder per song:
    manifest.json   everything about the song (see save())
    strips/         the cleaned strips (ink PNG) and the raw colour version (JPG/PNG),
                    kept so layout, details and strip choices can be changed any time
    bars/           the strips cut into bars at one string spacing, for the reader
    thumb.png       first screen of tab, for the library card
    art.jpg         cover art, when the song was found online
"""
import functools
import glob
import http.server
import json
import os
import shutil
import threading
import time

import cv2
import numpy as np

from . import layout


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
        path = os.path.join(self.folder(sid), name)
        if not os.path.exists(path):
            return None
        return f"{self.base_url}/{sid}/{name}?v={int(os.path.getmtime(path))}"

    def songs(self):
        """Card data for every song, newest first. Images go by URL."""
        out = []
        for mf in glob.glob(os.path.join(self.root, "*", "manifest.json")):
            m = _read_json(mf)
            if not m:
                continue
            sid = m["id"]
            pdf = m.get("pdf") if m.get("pdf") and os.path.exists(m["pdf"]) else None
            out.append({
                "song": sid, "name": m["title"], "url": m.get("url"), "path": pdf,
                "info": m.get("info") or {}, "notes": m.get("notes", ""),
                "channel": m.get("channel", ""), "duration": m.get("duration", 0),
                "collection": m.get("collection"), "created": m.get("created", 0),
                "played": m.get("played", 0), "when": m.get("updated") or os.path.getmtime(mf),
                "thumb": self.url(sid, "thumb.png"), "art": self.url(sid, "art.jpg"),
                "editable": bool(m.get("strips")),
            })
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
        kept = [s for k, s in enumerate(m.get("strips", [])) if k not in set(m.get("removed", []))]
        if kept:        # how long the tab was on screen, for the reader's Follow pacing
            m["span"] = max(10.0, kept[-1]["t1"] - kept[0]["t0"])
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

    # ---- writing ---------------------------------------------------------
    def save(self, sid, meta, strips, sp, n, screen_w, removed=(), pdf=None, art=None, collection=None,
             write_strips=True):
        """Store a song. strips: [(ink, colour, t0, t1)]. Kept fields (tempo set in the
        reader, play count, created time) survive a re-save."""
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
        shutil.rmtree(os.path.join(folder, "bars"), ignore_errors=True)
        os.makedirs(os.path.join(folder, "bars"))
        items, unit, sp_ref = [], screen_w, sp
        if keep:
            segs, unit, sp_ref = layout.segments(keep, screen_w, n)
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
            "id": sid, "title": meta["title"], "url": meta["url"],
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

    def end_headers(self):
        self.send_header("Cache-Control", "max-age=31536000")    # URLs carry ?v=mtime
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


def _serve(root):
    """Serve the library folder read-only on a free localhost port. Nothing else is
    reachable through it, and it only listens on 127.0.0.1."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=root))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}"
