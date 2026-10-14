"""Desktop window. Python does the work; ui/ is the interface, talking through pywebview."""
import base64
import glob
import json
import os
import shutil
import sys
import threading
import time

import cv2
import numpy as np
import webview

from . import download, info, layout, vision

HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UI = os.path.join(HERE, "tabz", "ui", "index.html")
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "tabz")
CONFIG = os.path.join(CONFIG_DIR, "settings.json")
LIBRARY = os.path.join(CONFIG_DIR, "library")      # one folder per song, for the Play view
DEFAULTS = {
    "out_dir": os.path.join(os.path.expanduser("~"), "Documents", "tabz", "Completed"),
    "per_row": 2,
    "paper": "a4",
    "keep_color": False,
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
            with open(CONFIG) as f:
                self.settings.update(json.load(f))
        except (OSError, ValueError):
            pass
        self._reset()

    def _reset(self):
        self.video = None
        self.meta = None
        self.region = None
        self.strips = []       # [(ink, color, sp, t0, t1)]
        self.removed = set()
        self.pdf_path = None
        self.song_id = None
        self.busy = False

    # -- plumbing ---------------------------------------------------------
    def _emit(self, event, data=None):
        if self.window:
            self.window.evaluate_js(f"window.tabz && tabz.on({json.dumps(event)}, {json.dumps(data)})")

    def _thread(self, fn, *args):
        def run():
            try:
                fn(*args)
            except Exception as e:  # surface every failure in the UI instead of a silent hang
                self.busy = False
                self._emit("error", _friendly(e))
        threading.Thread(target=run, daemon=True).start()

    def _drop_video(self):
        if self.video and os.path.exists(self.video):
            try:
                os.remove(self.video)
            except OSError:
                pass
        self.video = None

    # -- settings / library -----------------------------------------------
    def get_settings(self):
        return self.settings

    def set_settings(self, changes):
        self.settings.update({k: v for k, v in changes.items() if k in DEFAULTS})
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG, "w") as f:
            json.dump(self.settings, f, indent=2)
        return self.settings

    def pick_folder(self):
        res = self.window.create_file_dialog(webview.FOLDER_DIALOG, directory=self.settings["out_dir"])
        if res:
            self.set_settings({"out_dir": res[0]})
        return self.settings

    def library(self):
        """Songs saved for Play, plus any other PDFs in the output folder."""
        items, seen = [], set()
        for mf in glob.glob(os.path.join(LIBRARY, "*", "manifest.json")):
            m = _read_json(mf)
            if not m:
                continue
            pdf = m.get("pdf") if m.get("pdf") and os.path.exists(m["pdf"]) else None
            seen.add(os.path.normcase(pdf or ""))
            items.append({"name": m["title"], "song": m["id"], "path": pdf, "notes": m.get("notes", ""),
                          "when": os.path.getmtime(mf)})
        for f in glob.glob(os.path.join(self.settings["out_dir"], "*.pdf")):
            if os.path.normcase(f) not in seen:
                items.append({"name": os.path.splitext(os.path.basename(f))[0], "song": None,
                              "path": f, "notes": "", "when": os.path.getmtime(f)})
        items.sort(key=lambda i: i["when"], reverse=True)
        return items[:60]

    def open_path(self, path):
        if os.path.exists(path):
            os.startfile(path)

    def show_in_folder(self, path=None):
        path = path or self.pdf_path
        if path and os.path.exists(path):
            os.system(f'explorer /select,"{os.path.normpath(path)}"')
        else:
            os.makedirs(self.settings["out_dir"], exist_ok=True)
            os.startfile(self.settings["out_dir"])

    # -- step 1: download -------------------------------------------------
    def start(self, url):
        if self.busy:
            return False
        self._drop_video()
        self._reset()
        self.busy = True
        self._thread(self._download, url.strip())
        return True

    def _download(self, url):
        self._emit("stage", {"stage": "download"})
        last = [0.0]

        def prog(p):
            if p - last[0] > 0.01 or p >= 1:
                last[0] = p
                self._emit("progress", p)
        self.video, self.meta = download.download(url, on_progress=prog)
        self._emit("stage", {"stage": "locate"})
        self.meta["notes"] = " \u00b7 ".join(info.song_info(self.video, self.meta.pop("description", "")))
        self._emit("meta", self.meta)
        region = vision.guess_region(self.video)
        cap = cv2.VideoCapture(self.video)
        W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        dur = vision.video_duration(cap)
        cap.release()
        if region is None:   # no tab lines found anywhere: start from the bottom third
            region = (0, int(H * 0.66), W, int(H * 0.34))
        self.region = tuple(int(v) for v in region)
        self.busy = False
        self._emit("area", {"width": W, "height": H, "duration": dur, "region": self.region,
                            "found": region is not None, "time": dur * 0.5})

    def frame(self, t):
        """A frame for the crop screen, as a data URL at most 1280px wide."""
        cap = cv2.VideoCapture(self.video)
        f = vision.read_frame(cap, float(t))
        cap.release()
        return data_url(f, 1280) if f is not None else None

    # -- step 2: scan -----------------------------------------------------
    def scan(self, region):
        if self.busy:
            return False
        self.busy = True
        self.region = tuple(int(v) for v in region)
        self._thread(self._scan)
        return True

    def _scan(self):
        self._emit("stage", {"stage": "scan"})
        last = [0.0]

        def prog(p):
            if p - last[0] > 0.005 or p >= 1:
                last[0] = p
                self._emit("progress", p)

        strips, sp = vision.scan(self.video, self.region, on_progress=prog,
                                 on_strip=lambda n: self._emit("found", n))
        if not strips:
            self.busy = False
            self._emit("error", "No tab showed up inside that box. Try making it bigger, "
                                "or move it to where the string lines are.")
            return
        self.strips = [(s.image(), s.color, sp, s.t0, s.t1) for s in strips]
        self.removed = set()
        self._render_and_save()
        self.busy = False

    # -- step 3: review ---------------------------------------------------
    def update(self, changes):
        """Layout changes from the review screen. Re-saves the same PDF."""
        if "removed" in changes:
            self.removed = set(changes.pop("removed"))
        if "notes" in changes:
            self.meta["notes"] = changes.pop("notes").strip()
        self.set_settings(changes)
        self._render_and_save()

    def _render_and_save(self):
        keep = [s for i, s in enumerate(self.strips) if i not in self.removed]
        if not keep:
            self._emit("review", {"rows": [], "strips": self._strip_cards(), "pdf": None})
            return
        st = self.settings
        rows, row_px = layout.build_rows([(a, b, c) for a, b, c, _, _ in keep], self.region[2],
                                         per_row=int(st["per_row"]), keep_color=bool(st["keep_color"]))
        os.makedirs(st["out_dir"], exist_ok=True)
        path = os.path.join(st["out_dir"], download.safe_filename(self.meta["title"]) + ".pdf")
        layout.write_pdf(path, rows, row_px, self.meta, paper=st["paper"])
        self.pdf_path = path
        self.song_id = self._save_song([(a, c) for a, b, c, _, _ in keep], path)
        paper = layout.PAPER[st["paper"]]
        pt = (paper[0] - 2 * layout.MARGIN) / row_px
        self._emit("review", {
            "rows": [{"src": data_url(r, 1400, ".png" if r.ndim == 2 else ".jpg"),
                      "w": r.shape[1] * pt, "h": r.shape[0] * pt} for r in rows],
            "paper": {"w": paper[0], "h": paper[1], "margin": layout.MARGIN, "gap": layout.ROW_GAP},
            "strips": self._strip_cards(),
            "pdf": path,
            "name": os.path.basename(path),
            "notes": self.meta.get("notes", ""),
            "song": self.song_id,
        })

    def _save_song(self, strips, pdf):
        """Bars as PNGs + manifest.json, so the Play view can reflow them later."""
        sid = self.meta.get("id") or download.safe_filename(self.meta["title"])
        folder = os.path.join(LIBRARY, sid)
        old = _read_json(os.path.join(folder, "manifest.json")) or {}
        shutil.rmtree(folder, ignore_errors=True)
        os.makedirs(folder)
        segs, unit, sp = layout.segments(strips, self.region[2])
        items = []
        for n, sg in enumerate(segs):
            name = f"{n:03d}.png"
            cv2.imwrite(os.path.join(folder, name), sg.pop("img"))
            items.append({"file": name, **sg})
        manifest = {
            "id": sid, "title": self.meta["title"], "url": self.meta["url"],
            "channel": self.meta.get("channel", ""), "duration": self.meta.get("duration", 0),
            "notes": self.meta.get("notes", ""), "pdf": pdf, "unit": unit, "sp": sp,
            "created": old.get("created", time.time()), "bpm": old.get("bpm"),
            "segments": items,
        }
        with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        return sid

    # -- play view ---------------------------------------------------------
    def load_song(self, sid):
        folder = os.path.join(LIBRARY, os.path.basename(sid))
        m = _read_json(os.path.join(folder, "manifest.json"))
        if not m:
            return None
        for sg in m["segments"]:
            with open(os.path.join(folder, sg["file"]), "rb") as f:
                sg["src"] = "data:image/png;base64," + base64.b64encode(f.read()).decode()
        return m

    def set_song(self, sid, changes):
        path = os.path.join(LIBRARY, os.path.basename(sid), "manifest.json")
        m = _read_json(path)
        if m:
            m.update({k: v for k, v in changes.items() if k in ("bpm", "beats")})
            with open(path, "w", encoding="utf-8") as f:
                json.dump(m, f)

    def delete_song(self, sid):
        """Remove a song from the library. The PDF in the output folder is left alone."""
        shutil.rmtree(os.path.join(LIBRARY, os.path.basename(sid)), ignore_errors=True)

    def toggle_fullscreen(self):
        self.window.toggle_fullscreen()

    def _strip_cards(self):
        cards = []
        for i, (img, _, _, t0, t1) in enumerate(self.strips):
            w = self.region[2]
            thumb = (255 - img[:, :w] * 227).astype(np.uint8)
            cards.append({"i": i, "src": data_url(thumb, 480, ".png"), "t0": t0, "t1": t1,
                          "screens": round(img.shape[1] / w, 1), "removed": i in self.removed})
        return cards

    def finish(self):
        """Leave the review screen: the temp video goes, the PDF stays."""
        self._drop_video()
        self._reset()
        return True

    def cleanup(self):
        self._drop_video()


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _friendly(e):
    msg = str(e)
    if "Unsupported URL" in msg or "is not a valid URL" in msg:
        return "That doesn't look like a video link."
    if "Private video" in msg or "Sign in" in msg:
        return "YouTube won't let tabz download that one (private, age-restricted or members-only)."
    if "HTTP Error 403" in msg:
        return "YouTube refused the download. Wait a minute and try again."
    if "Video unavailable" in msg:
        return "That video is unavailable."
    return msg.replace("ERROR: ", "")[:300]


def main():
    shutil.rmtree(download.TEMP_DIR, ignore_errors=True)   # leftovers from a crash
    api = Api()
    window = webview.create_window("tabz", UI, js_api=api, width=1180, height=800,
                                   min_size=(900, 640), background_color="#f3f0e8")
    api.window = window
    window.events.closed += api.cleanup
    webview.start(debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
