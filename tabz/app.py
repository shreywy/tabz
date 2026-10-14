"""Desktop window. Python does the work; ui/ is the interface, talking through pywebview."""
import base64
import json
import os
import shutil
import sys
import webbrowser

import cv2
import numpy as np
import webview

from . import download, info, jobs, layout, vision
from .library import Library

HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UI = os.path.join(HERE, "tabz", "ui", "index.html")


def _home():
    """Folder next to tabz.exe (or the repo when run from source). Falls back to
    %APPDATA% when that folder can't be written to, e.g. under Program Files."""
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else \
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        os.makedirs(os.path.join(base, ".tabz"), exist_ok=True)
        probe = os.path.join(base, ".tabz", ".write-test")
        open(probe, "w").close()
        os.remove(probe)
        return base
    except OSError:
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "tabz")


BASE = _home()
DATA_DIR = os.path.join(BASE, ".tabz")
CONFIG = os.path.join(DATA_DIR, "settings.json")
DEFAULTS = {
    "out_dir": os.path.join(BASE, "Completed"),
    "per_row": "auto",
    "paper": "a4",
    "keep_color": False,
    "gemini_key": "",
}


def data_url(img, max_w=None, fmt=".jpg", quality=86):
    if max_w and img.shape[1] > max_w:
        img = cv2.resize(img, (max_w, int(img.shape[0] * max_w / img.shape[1])), interpolation=cv2.INTER_AREA)
    params = [cv2.IMWRITE_JPEG_QUALITY, quality] if fmt == ".jpg" else []
    ok, buf = cv2.imencode(fmt, img, params)
    mime = "jpeg" if fmt == ".jpg" else "png"
    return f"data:image/{mime};base64," + base64.b64encode(buf.tobytes()).decode()


class Api:
    def __init__(self):
        self.window = None
        self.settings = dict(DEFAULTS)
        try:
            with open(CONFIG, encoding="utf-8") as f:
                self.settings.update(json.load(f))
        except (OSError, ValueError):
            pass
        self.lib = Library(os.path.join(DATA_DIR, "library"))
        self.queue = jobs.Queue(self.lib, lambda: dict(self.settings), self._emit)
        self.edit = None        # the song open on the review screen

    def _emit(self, event, data=None):
        if self.window:
            try:
                self.window.evaluate_js(f"window.tabz && tabz.on({json.dumps(event)}, {json.dumps(data)})")
            except Exception:
                pass            # window closing

    # ---- settings ---------------------------------------------------------------
    def get_settings(self):
        s = dict(self.settings)
        s["gemini_set"] = bool(s.pop("gemini_key", ""))     # the key itself never goes back to the page
        return s

    def set_settings(self, changes):
        self.settings.update({k: v for k, v in changes.items() if k in DEFAULTS})
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(self.settings, f, indent=2)
        return self.get_settings()

    def pick_folder(self):
        res = self.window.create_file_dialog(webview.FOLDER_DIALOG, directory=self.settings["out_dir"])
        if res:
            self.set_settings({"out_dir": res[0]})
        return self.get_settings()

    # ---- finding videos ------------------------------------------------------------
    def search(self, query):
        try:
            return {"results": download.search(query)}
        except Exception as e:
            return {"error": jobs.friendly(e)}

    def add(self, url, whole_playlist=False):
        """Queue a link. With whole_playlist, every video in its playlist goes in, filed
        under the playlist's name; entries that can't be done are listed as skipped."""
        url = url.strip()
        if whole_playlist and download.is_playlist(url):
            try:
                title, entries = download.playlist(url)
            except Exception as e:
                return {"error": jobs.friendly(e)}
            added = skipped = 0
            for e in entries:
                if e["ok"]:
                    self.queue.add(e["url"], collection=title, area_wait=0, title=e["title"])
                    added += 1
                else:
                    self.queue.skip(e["url"], e["title"], e["reason"], collection=title)
                    skipped += 1
            self.queue.changed()
            return {"playlist": title, "added": added, "skipped": skipped}
        job = self.queue.add(url)
        return {"job": job.id}

    def jobs(self):
        return self.queue.views()

    def job_frame(self, jid, t):
        """A frame for the area check, as a data URL at most 1280px wide."""
        j = self.queue.get(jid)
        if not j or not j.video:
            return None
        cap = cv2.VideoCapture(j.video)
        f = vision.read_frame(cap, float(t))
        cap.release()
        return data_url(f, 1280) if f is not None else None

    def job_hold(self, jid):
        self.queue.hold(jid)

    def job_confirm(self, jid, region):
        self.queue.confirm(jid, region)

    def job_cancel(self, jid):
        self.queue.cancel(jid)

    def jobs_clear(self):
        self.queue.clear()

    # ---- library ------------------------------------------------------------------------
    def library(self):
        """Songs, plus PDFs in the output folder that aren't from a saved song."""
        songs = self.lib.songs()
        seen = {os.path.normcase(s["path"]) for s in songs if s["path"]}
        out_dir = self.settings["out_dir"]
        extra = []
        if os.path.isdir(out_dir):
            for root, _, files in os.walk(out_dir):
                for name in files:
                    p = os.path.join(root, name)
                    if name.lower().endswith(".pdf") and os.path.normcase(p) not in seen:
                        rel = os.path.relpath(root, out_dir)
                        extra.append({"song": None, "name": name[:-4], "path": p, "info": {},
                                      "collection": None if rel == "." else rel, "when": os.path.getmtime(p),
                                      "created": os.path.getmtime(p)})
        return songs + extra

    def load_song(self, sid):
        m = self.lib.reader_data(sid)
        if m:
            self.lib.update(sid, played=__import__("time").time())
        return m

    def set_song(self, sid, changes):
        allowed = {k: v for k, v in changes.items() if k in ("bpm", "beats", "collection")}
        if allowed:
            self.lib.update(sid, **allowed)

    def delete_song(self, sid):
        """Remove a song from the library. Its PDF stays where it is."""
        self.lib.delete(sid)

    def open_url(self, url):
        if url and url.startswith("https://"):
            webbrowser.open(url)

    def open_path(self, path):
        if path and os.path.exists(path):
            os.startfile(path)

    def show_in_folder(self, path=None):
        if path and os.path.exists(path):
            os.system(f'explorer /select,"{os.path.normpath(path)}"')
        else:
            os.makedirs(self.settings["out_dir"], exist_ok=True)
            os.startfile(self.settings["out_dir"])

    def toggle_fullscreen(self):
        self.window.toggle_fullscreen()

    # ---- review: change a saved song ---------------------------------------------------------
    def open_review(self, sid):
        m = self.lib.manifest(sid)
        if not m or not m.get("strips"):
            return {"error": "This song was saved before tabz kept its strips, so it can't be edited. Get it again to edit it."}
        self.edit = {
            "sid": sid, "manifest": m, "strips": self.lib.load_strips(sid),
            "removed": set(m.get("removed", [])),
            "art": open(os.path.join(self.lib.folder(sid), "art.jpg"), "rb").read()
            if os.path.exists(os.path.join(self.lib.folder(sid), "art.jpg")) else None,
        }
        return self._render(write=False)

    def update(self, changes):
        """Changes from the review screen. Re-saves the PDF and the reader's copy."""
        e = self.edit
        if not e:
            return None
        if "removed" in changes:
            e["removed"] = set(changes.pop("removed"))
        if "info" in changes:
            e["manifest"]["info"].update(_clean_info(changes.pop("info")))
            e["manifest"]["notes"] = info.summary(e["manifest"]["info"])
        self.set_settings(changes)
        return self._render(write=True)

    def _render(self, write):
        e, m = self.edit, self.edit["manifest"]
        meta = {k: m.get(k) for k in ("title", "url", "channel", "duration", "notes", "info")}
        meta["id"] = e["sid"]
        if write:
            rows, row_px, pdf = jobs.save_song(self.lib, self.settings, e["sid"], meta, e["strips"], m["sp"], m["n"],
                                               m["screen_w"], removed=e["removed"], art=e["art"], write_strips=False)
            e["manifest"] = self.lib.manifest(e["sid"])
        else:
            keep = [(ink, col, m["sp"]) for k, (ink, col, _, _) in enumerate(e["strips"]) if k not in e["removed"]]
            rows, row_px = layout.build_rows(keep, m["screen_w"], per_row=self.settings["per_row"],
                                             keep_color=bool(self.settings["keep_color"]), n=m["n"]) if keep else ([], 1)
            pdf = m.get("pdf")
        paper = layout.PAPER[self.settings["paper"]]
        pt = (paper[0] - 2 * layout.MARGIN) / row_px
        top = paper[1] - layout.MARGIN
        header_h = top - layout.draw_header(None, meta, layout.MARGIN, top, paper[0] - 2 * layout.MARGIN, e["art"])
        w = m["screen_w"]
        return {
            "song": e["sid"], "meta": meta, "info": meta["info"], "notes": meta["notes"],
            "pdf": pdf if pdf and os.path.exists(pdf) else None, "name": os.path.basename(pdf) if pdf else None,
            "rows": [{"src": data_url(r, 1400, ".png" if r.ndim == 2 else ".jpg"),
                      "w": r.shape[1] * pt, "h": r.shape[0] * pt} for r in rows],
            "paper": {"w": paper[0], "h": paper[1], "margin": layout.MARGIN, "gap": layout.ROW_GAP},
            "header": {"h": header_h, "cells": layout.header_cells(meta["info"]),
                       "sub": layout.artist_line(meta["info"]), "art": self.lib.url(e["sid"], "art.jpg")},
            "strips": [{"i": k, "src": self.lib.url(e["sid"], s["file"]), "t0": s["t0"], "t1": s["t1"],
                        "screens": round(ink.shape[1] / w, 1), "removed": k in e["removed"]}
                       for k, (s, (ink, _, _, _)) in enumerate(zip(m["strips"], e["strips"]))],
            "settings": self.get_settings(),
        }

    def close_review(self):
        self.edit = None

    def cleanup(self):
        shutil.rmtree(download.TEMP_DIR, ignore_errors=True)


def _clean_info(d):
    """Edits from the review form. Numbers stay numbers, blanks become None."""
    out = {}
    for k in ("song", "artist", "tuning", "key", "album"):
        if k in d:
            out[k] = str(d[k]).strip()[:120] or None
    for k, lo, hi in (("capo", 0, 12), ("tempo", 20, 400)):
        if k in d:
            try:
                v = int(str(d[k]).strip())
                out[k] = v if lo <= v <= hi else None
            except ValueError:
                out[k] = None
    if "chords" in d:
        out["chords"] = [c for c in str(d["chords"]).replace(",", " ").split() if c][:40]
    if "key" in out:
        out["key_guessed"] = False
    return out


def main():
    shutil.rmtree(download.TEMP_DIR, ignore_errors=True)   # leftovers from a crash
    api = Api()
    window = webview.create_window("tabz", UI, js_api=api, width=1240, height=820,
                                   min_size=(940, 640), background_color="#f3f0e8", text_select=True)
    api.window = window
    window.events.closed += api.cleanup
    # not private: keeps localStorage (theme, reader size, click sound) between launches
    webview.start(debug="--debug" in sys.argv, private_mode=False,
                  storage_path=os.path.join(DATA_DIR, "webview"))


if __name__ == "__main__":
    main()
