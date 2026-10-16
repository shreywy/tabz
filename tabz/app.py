"""Desktop window. Python does the work; ui/ is the interface, talking through pywebview."""
import base64
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

import cv2
import numpy as np
import webview

from . import download, imports, info, jobs, layout, vision
from .library import Library

STATS_V = 1          # bump when the window's style and difficulty reading changes (gp.js partStats)
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
    # Only the methods are for the page. Everything else is underscored, because pywebview
    # walks every public attribute at load and the window object sends it into the UI
    # thread's internals: the window froze or stayed blank.
    def __init__(self):
        self._window = None
        self._settings = dict(DEFAULTS)
        try:
            with open(CONFIG, encoding="utf-8") as f:
                self._settings.update(json.load(f))
        except (OSError, ValueError):
            pass
        self._lib = Library(os.path.join(DATA_DIR, "library"))
        self._queue = jobs.Queue(self._lib, lambda: dict(self._settings), self._emit)
        self._edit = None        # the song open on the review screen
        self._outbox = queue.Queue()
        threading.Thread(target=self._send, daemon=True).start()
        threading.Thread(target=self._enrich, daemon=True).start()
        threading.Thread(target=self._read_notes, daemon=True).start()

    def _emit(self, event, data=None):
        """Push an update to the page. The job queue calls this from its own threads, so it
        only hands the update to the sender and never waits on the window."""
        self._outbox.put(f"window.tabz && tabz.on({json.dumps(event)}, {json.dumps(data)})")

    def _send(self):
        # pywebview's evaluate_js waits with no timeout, and a lost call blocks forever.
        # Each push gets a few seconds; one that hangs is left behind instead of
        # stopping every update after it.
        while True:
            js = self._outbox.get()
            if self._window:
                t = threading.Thread(target=self._run_js, args=(js,), daemon=True)
                t.start()
                t.join(5)

    def _run_js(self, js):
        try:
            self._window.evaluate_js(js)
        except Exception:
            pass                # window closing

    # ---- settings ---------------------------------------------------------------
    def get_settings(self):
        s = dict(self._settings)
        s["gemini_set"] = bool(s.pop("gemini_key", ""))     # the key itself never goes back to the page
        return s

    def set_settings(self, changes):
        self._settings.update({k: v for k, v in changes.items() if k in DEFAULTS})
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(self._settings, f, indent=2)
        return self.get_settings()

    def pick_folder(self):
        res = self._window.create_file_dialog(webview.FOLDER_DIALOG, directory=self._settings["out_dir"])
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
            if not entries:
                # YouTube answers "does not exist" to signed-out viewers of private playlists
                return {"error": "YouTube won't show this playlist to tabz. It's probably private: set it to "
                                 "Unlisted or Public in YouTube and try again, or get just the video from this link.",
                        "single_ok": "v=" in url}
            added = skipped = 0
            for e in entries:
                if e["ok"]:
                    self._queue.add(e["url"], collection=title, area_wait=0, title=e["title"])
                    added += 1
                else:
                    self._queue.skip(e["url"], e["title"], e["reason"], collection=title)
                    skipped += 1
            self._queue.changed()
            return {"playlist": title, "added": added, "skipped": skipped}
        job = self._queue.add(url)
        return {"job": job.id}

    def jobs(self):
        return self._queue.views()

    def job_frame(self, jid, t):
        """A frame for the area check, as a data URL at most 1280px wide."""
        j = self._queue.get(jid)
        if not j or not j.video:
            return None
        cap = cv2.VideoCapture(j.video)
        f = vision.read_frame(cap, float(t))
        cap.release()
        return data_url(f, 1280) if f is not None else None

    def job_hold(self, jid):
        self._queue.hold(jid)

    def job_confirm(self, jid, region):
        self._queue.confirm(jid, region)

    def job_cancel(self, jid):
        self._queue.cancel(jid)

    def jobs_clear(self):
        self._queue.clear()

    # ---- library ------------------------------------------------------------------------
    def library(self):
        """Songs, plus PDFs in the output folder that aren't from a saved song."""
        songs = self._lib.songs()
        seen = {os.path.normcase(s["path"]) for s in songs if s["path"]}
        out_dir = self._settings["out_dir"]
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
        m = self._lib.reader_data(sid)
        if m:
            self._lib.update(sid, played=__import__("time").time())
        return m

    def set_song(self, sid, changes):
        allowed = {k: v for k, v in changes.items() if k in ("bpm", "beats", "collection", "view", "sound", "fav")}
        if allowed:
            self._lib.update(sid, **allowed)

    def set_details(self, sid, changes):
        """Song details edited from the details dialog (any kind of song)."""
        m = self._lib.manifest(sid)
        if not m:
            return None
        m.setdefault("info", {}).update(_clean_info(changes))
        m["notes"] = info.summary(m["info"])
        self._lib.write(m)
        return m["info"]

    def save_score(self, sid, b64):
        """A Guitar Pro 7 file the window made from the score on screen, saved next to the PDFs."""
        m = self._lib.manifest(sid)
        if not m:
            return {"error": "That song isn't in the library any more."}
        name = download.safe_filename(info.file_name(m)) + ".gp"
        out = os.path.join(self._settings["out_dir"], download.safe_filename(m["collection"])) if m.get("collection")             else self._settings["out_dir"]
        try:
            os.makedirs(out, exist_ok=True)
            with open(os.path.join(out, name), "wb") as f:
                f.write(base64.b64decode(b64))
        except (OSError, ValueError) as e:
            return {"error": f"Couldn't save it: {e}"}
        return {"path": os.path.join(out, name), "name": name}

    # ---- importing files -------------------------------------------------------------
    def import_dialog(self, folder=False):
        """Pick files (or one folder) and import them in the background."""
        if folder:
            paths = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        else:
            exts = ";".join(f"*{e}" for e in imports.KNOWN + (".zip",))
            paths = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True,
                                                    file_types=(f"Tabs, scores and audio ({exts})", "All files (*.*)"))
        if not paths:
            return None
        return {"job": self._queue.add_import(list(paths)).id}

    def gp_pending(self):
        """Scores the window hasn't read yet: it reads tracks, tuning, tempo, playing style
        and difficulty with the same library that draws them, and sends them back through
        gp_meta. Tab read from pictures (score.atex) only gets style and difficulty."""
        out = []
        for m in self._lib.manifests():
            if m.get("score") and (m.get("need_scan") or m.get("stats_v") != STATS_V):
                out.append({"song": m["id"], "src": self._lib.url(m["id"], m["score"])})
            elif m.get("tex") and m.get("stats_v") != f"{STATS_V}/{m.get('tex_v')}":
                out.append({"song": m["id"], "src": self._lib.url(m["id"], m["tex"]), "tex": True})
        return out

    def gp_meta(self, sid, meta):
        m = self._lib.manifest(sid)
        if not m:
            return
        i = m.setdefault("info", {})
        st = meta.get("stats") or {}
        if st:
            i["style"], i["level"], i["percussive"] = st.get("style"), st.get("level"), bool(st.get("percussive"))
            if st.get("style") == "Fingerstyle" and not i.get("guitar"):
                i["guitar"] = "Acoustic"
        m["stats_v"] = STATS_V if m.get("score") else f"{STATS_V}/{m.get('tex_v')}"
        if not m.get("score"):                  # tab read from pictures: details stay as they are
            m["notes"] = info.summary(i)
            self._lib.write(m)
            return
        for k in ("song", "artist", "album"):
            v = str(meta.get(k) or "").strip()[:120]
            if v and not i.get(f"{k}_set"):
                i[k] = v
        if meta.get("tempo"):
            i["tempo"] = int(meta["tempo"])
        for k in ("tuning", "capo"):
            if meta.get(k) not in (None, ""):
                i[k] = meta[k]
        i["guitar"] = i.get("guitar") or imports.guitar_from_programs(meta.get("programs") or [])
        m["tracks"] = meta.get("tracks") or []
        m["title"] = i.get("song") or m["title"]
        m["notes"] = info.summary(i)
        m["need_scan"] = False
        self._lib.write(m)

    def _read_notes(self):
        """Give every song made from tab images a playable reading of its notes, a few at a
        time in the background: new ones as they arrive, older ones once."""
        from .library import TEX_VERSION
        while True:
            todo = [m["id"] for m in self._lib.manifests()
                    if m.get("strips") and m.get("tex_v") != TEX_VERSION]
            for k, sid in enumerate(todo):
                try:
                    self._lib.transcribe(sid)
                except Exception:
                    m = self._lib.manifest(sid)       # unreadable: don't try again until the reader improves
                    if m:
                        m["tex_v"] = TEX_VERSION
                        self._lib.write(m)
                if k % 20 == 19:
                    self._emit("library")
                time.sleep(0.05)
            if todo:
                self._emit("library")
            time.sleep(20)

    def _enrich(self):
        """Genre, year and cover art for imported songs, from the iTunes search. Apple
        allows about 20 searches a minute, so this trickles along in the background. A song
        iTunes doesn't know gets the thumbnail of its top YouTube result as its picture."""
        while True:
            todo = [m["id"] for m in self._lib.manifests() if m.get("need_lookup") and not m.get("need_scan")]
            if not todo:
                todo = [m["id"] for m in self._lib.manifests() if not m.get("need_scan") and not m.get("art_tried")
                        and not os.path.exists(os.path.join(self._lib.folder(m["id"]), "art.jpg"))]
                for sid in todo[:30]:
                    self._picture(sid)
                    time.sleep(1.5)
                if todo:
                    self._emit("library")
                    continue
            if not todo:
                time.sleep(15)
                continue
            for k, sid in enumerate(todo):
                time.sleep(3.2)
                m = self._lib.manifest(sid)
                if not m:
                    continue
                i = m.setdefault("info", {})
                q = f"{i.get('song') or m['title']} - {i['artist']}" if i.get("artist") else (i.get("song") or m["title"])
                found = info.lookup(q) or {}
                art = jobs.fetch(found.get("artwork"))
                m = self._lib.manifest(sid) or m      # read again: don't undo an edit made meanwhile
                i = m.setdefault("info", {})
                for f in ("genre", "year", "album"):
                    i[f] = i.get(f) or found.get(f)
                if found and not i.get("song_set") and not i.get("artist_set"):
                    i["song"], i["artist"] = found.get("song") or i.get("song"), found.get("artist") or i.get("artist")
                if art:
                    with open(os.path.join(self._lib.folder(sid), "art.jpg"), "wb") as f:
                        f.write(art)
                m["need_lookup"] = False
                self._lib.write(m)
                if k % 10 == 9 or k == len(todo) - 1:
                    self._emit("library")

    def _picture(self, sid):
        m = self._lib.manifest(sid)
        if not m:
            return
        i = m.get("info") or {}
        q = " ".join(x for x in (i.get("song") or m["title"], i.get("artist")) if x)
        try:
            res = download.search(q, n=3)
        except Exception:
            res = []
        art = jobs.fetch(res[0]["thumb"].replace("mqdefault", "hqdefault")) if res else None
        if art:
            # YouTube thumbnails are 4:3 with bars; keep the middle 16:9 so it fills a card
            img = cv2.imdecode(np.frombuffer(art, np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                h, w = img.shape[:2]
                ch = int(w * 9 / 16)
                img = img[max(0, (h - ch) // 2):(h + ch) // 2]
                cv2.imwrite(os.path.join(self._lib.folder(sid), "art.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 88])
        m = self._lib.manifest(sid) or m
        m["art_tried"] = True
        self._lib.write(m)

    # ---- backing tracks --------------------------------------------------------------
    def backing_pick(self, sid):
        """Choose an audio (or video) file from disk to play along with."""
        exts = ";".join(f"*{e}" for e in imports.AUDIO + imports.VIDEO)
        res = self._window.create_file_dialog(webview.OPEN_DIALOG, file_types=(f"Audio ({exts})", "All files (*.*)"))
        if not res:
            return None
        ext = os.path.splitext(res[0])[1].lower()
        self._clear_backing(sid)
        shutil.copyfile(res[0], os.path.join(self._lib.folder(sid), "backing" + ext))
        return self._set_backing(sid, {"file": "backing" + ext, "offset": 0.0, "name": os.path.basename(res[0])})

    def backing_find(self, sid):
        """YouTube results for this song's backing track, best guess first."""
        m = self._lib.manifest(sid) or {}
        i = m.get("info") or {}
        song, artist = i.get("song") or m.get("title", ""), i.get("artist") or ""
        try:
            res = download.search(f"{song} {artist} guitar backing track", n=12)
        except Exception as e:
            return {"error": jobs.friendly(e)}
        return {"results": info.rank_backing(res, song, artist, self._settings.get("gemini_key")),
                "query": f"{song} {artist}".strip()}

    def backing_get(self, sid, url):
        """Download just the sound of a video as this song's backing track."""
        folder = self._lib.folder(sid)
        try:
            path, meta = download.download(url, dest=os.path.join(folder, "_dl"), fmt=download.AUDIO, name="backing")
        except Exception as e:
            shutil.rmtree(os.path.join(folder, "_dl"), ignore_errors=True)
            return {"error": jobs.friendly(e)}
        self._clear_backing(sid)
        name = "backing" + os.path.splitext(path)[1]
        os.replace(path, os.path.join(folder, name))
        shutil.rmtree(os.path.join(folder, "_dl"), ignore_errors=True)
        return self._set_backing(sid, {"file": name, "offset": 0.0, "name": meta["title"], "url": meta["url"]})

    def backing_set(self, sid, changes):
        """Sync and level for the backing track: offset (seconds into the audio where the
        tab starts), sync points [{bar, ms}] for scores, volume."""
        m = self._lib.manifest(sid)
        if not m or not m.get("backing"):
            return None
        for k in ("offset", "sync", "volume"):
            if k in changes:
                m["backing"][k] = changes[k]
        self._lib.write(m)
        return m["backing"]

    def backing_remove(self, sid):
        self._clear_backing(sid)
        m = self._lib.manifest(sid)
        if m:
            m.pop("backing", None)
            self._lib.write(m)

    def _clear_backing(self, sid):
        b = (self._lib.manifest(sid) or {}).get("backing") or {}
        if b.get("file"):
            try:
                os.remove(os.path.join(self._lib.folder(sid), b["file"]))
            except OSError:
                pass

    def _set_backing(self, sid, b):
        m = self._lib.manifest(sid)
        m["backing"] = b
        self._lib.write(m)
        return {"backing": b, "src": self._lib.url(sid, b["file"])}

    def delete_song(self, sid):
        """Remove a song from the library. Its PDF stays where it is."""
        self._lib.delete(sid)

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
            os.makedirs(self._settings["out_dir"], exist_ok=True)
            os.startfile(self._settings["out_dir"])

    def toggle_fullscreen(self):
        self._window.toggle_fullscreen()

    # ---- review: change a saved song ---------------------------------------------------------
    def open_review(self, sid):
        m = self._lib.manifest(sid)
        if not m or not m.get("strips"):
            return {"error": "This song was saved before tabz kept its strips, so it can't be edited. Get it again to edit it."}
        self._edit = {
            "sid": sid, "manifest": m, "strips": self._lib.load_strips(sid),
            "removed": set(m.get("removed", [])),
            "art": open(os.path.join(self._lib.folder(sid), "art.jpg"), "rb").read()
            if os.path.exists(os.path.join(self._lib.folder(sid), "art.jpg")) else None,
        }
        return self._render(write=False)

    def update(self, changes):
        """Changes from the review screen. Re-saves the PDF and the reader's copy."""
        e = self._edit
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
        e, m = self._edit, self._edit["manifest"]
        meta = {k: m.get(k) for k in ("title", "url", "channel", "duration", "notes", "info")}
        meta["id"] = e["sid"]
        if write:
            rows, row_px, pdf = jobs.save_song(self._lib, self._settings, e["sid"], meta, e["strips"], m["sp"], m["n"],
                                               m["screen_w"], removed=e["removed"], art=e["art"], write_strips=False)
            e["manifest"] = self._lib.manifest(e["sid"])
        else:
            keep = [(ink, col, m["sp"]) for k, (ink, col, _, _) in enumerate(e["strips"]) if k not in e["removed"]]
            rows, row_px = layout.build_rows(keep, m["screen_w"], per_row=self._settings["per_row"],
                                             keep_color=bool(self._settings["keep_color"]), n=m["n"]) if keep else ([], 1)
            pdf = m.get("pdf")
        paper = layout.PAPER[self._settings["paper"]]
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
                       "sub": layout.artist_line(meta["info"]), "art": self._lib.url(e["sid"], "art.jpg")},
            "strips": [{"i": k, "src": self._lib.url(e["sid"], s["file"]), "t0": s["t0"], "t1": s["t1"],
                        "screens": round(ink.shape[1] / w, 1), "removed": k in e["removed"]}
                       for k, (s, (ink, _, _, _)) in enumerate(zip(m["strips"], e["strips"]))],
            "settings": self.get_settings(),
        }

    def close_review(self):
        self._edit = None

    def cleanup(self):
        shutil.rmtree(download.TEMP_DIR, ignore_errors=True)


def _clean_info(d):
    """Edits from the review form or details dialog. Numbers stay numbers, blanks become None."""
    out = {}
    for k in ("song", "artist", "tuning", "key", "album", "genre", "year"):
        if k in d:
            out[k] = str(d[k]).strip()[:120] or None
    for k in ("song", "artist"):            # typed by hand: lookups and score reads keep off
        if k in d:
            out[f"{k}_set"] = True
    if "guitar" in d:
        out["guitar"] = d["guitar"] if d["guitar"] in ("Acoustic", "Electric", "Bass", "Classical") else None
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


def _free_profile(path):
    """A tabz that was ended from Task Manager can leave WebView2 running on its profile,
    and the next window then comes up blank ("resource in use"). Close those leftovers:
    browser processes on this profile whose parent is gone. Another open tabz is left alone."""
    lock = os.path.join(path, "EBWebView", "lockfile")
    try:
        if os.path.exists(lock):
            os.remove(lock)         # nobody holds it: nothing to do
        return
    except OSError:
        pass
    ps = ("$alive = (Get-Process).Id; "
          "Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe'\" | "
          f"? {{ $_.CommandLine -and $_.CommandLine.Contains('{path.replace("'", "''")}') "
          "-and $alive -notcontains $_.ParentProcessId } | % { Stop-Process -Id $_.ProcessId -Force }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=20,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        pass


def main():
    shutil.rmtree(download.TEMP_DIR, ignore_errors=True)   # leftovers from a crash
    profile = os.path.join(DATA_DIR, "webview")
    _free_profile(profile)
    # The UI comes from the same localhost address every launch, so WebView2 would keep
    # serving its cached copy of the old app.js/play.js after an update.
    for d in ("Cache", "Code Cache"):
        shutil.rmtree(os.path.join(profile, "EBWebView", "Default", d), ignore_errors=True)
    api = Api()
    window = webview.create_window("tabz", UI, js_api=api, width=1240, height=820,
                                   min_size=(940, 640), background_color="#f3f0e8", text_select=True)
    api._window = window
    window.events.closed += api.cleanup
    # not private: keeps localStorage (theme, reader size, click sound) between launches
    webview.start(debug="--debug" in sys.argv, private_mode=False,
                  storage_path=profile)


if __name__ == "__main__":
    main()
