"""Background queue: videos are processed one at a time on a worker thread while the
window stays free for browsing, reading and adding more."""
import itertools
import os
import threading
import time

import cv2

from . import download, info, layout, vision

AREA_WAIT = 15          # seconds the area guess waits for a look before going ahead

_ids = itertools.count(1)


class Job:
    def __init__(self, url, collection=None, area_wait=AREA_WAIT, title=None):
        self.id = next(_ids)
        self.url = url
        self.collection = collection
        self.area_wait = area_wait
        self.title = title or url
        self.status = "queued"      # queued download locate area scan read save done failed skipped cancelled
        self.progress = 0.0
        self.detail = ""
        self.error = None
        self.song = None
        self.video = None
        self.meta = None
        self.art = None
        self.area = None            # {width, height, duration, region, found, time}
        self.region = None
        self.confirmed = False
        self.held = False           # the user touched the box: wait for them, no timer
        self.deadline = 0
        self.found = 0

    def view(self):
        return {
            "id": self.id, "url": self.url, "title": self.title, "collection": self.collection,
            "status": self.status, "progress": round(self.progress, 3), "detail": self.detail,
            "error": self.error, "song": self.song, "area": self.area, "found": self.found,
            "held": self.held, "left": max(0, round(self.deadline - time.time())) if self.status == "area" else None,
            "notes": (self.meta or {}).get("notes"),
        }


class Cancelled(Exception):
    pass


class Queue:
    def __init__(self, lib, settings, emit):
        self.lib = lib
        self.settings = settings        # callable returning the current settings dict
        self.emit = emit
        self.jobs = []
        self.lock = threading.Lock()
        self.wake = threading.Event()
        threading.Thread(target=self._worker, daemon=True).start()

    # ---- control from the window ----------------------------------------------
    def add(self, url, collection=None, area_wait=AREA_WAIT, title=None):
        job = Job(url, collection, area_wait, title)
        with self.lock:
            self.jobs.append(job)
        self.changed()
        self.wake.set()
        return job

    def skip(self, url, title, reason, collection=None):
        """A playlist entry we already know can't be done (private, deleted)."""
        job = Job(url, collection, title=title)
        job.status, job.error = "skipped", reason
        with self.lock:
            self.jobs.append(job)
        return job

    def get(self, jid):
        return next((j for j in self.jobs if j.id == jid), None)

    def confirm(self, jid, region):
        j = self.get(jid)
        if j and j.status == "area":
            j.region = tuple(int(v) for v in region)
            j.confirmed = True

    def hold(self, jid):
        j = self.get(jid)
        if j and j.status == "area":
            j.held = True
            self.changed()

    def cancel(self, jid):
        j = self.get(jid)
        if not j:
            return
        if j.status == "queued":
            j.status = "cancelled"
        elif j.status not in ("done", "failed", "skipped", "cancelled"):
            j.cancel = True
        self.changed()

    def clear(self):
        with self.lock:
            self.jobs = [j for j in self.jobs if j.status not in ("done", "failed", "skipped", "cancelled")]
        self.changed()

    def views(self):
        return [j.view() for j in self.jobs]

    def changed(self, job=None):
        self.emit("jobs", self.views())

    # ---- the worker ---------------------------------------------------------------
    def _worker(self):
        while True:
            job = next((j for j in self.jobs if j.status == "queued"), None)
            if not job:
                self.wake.wait(1)
                self.wake.clear()
                continue
            try:
                self._run(job)
                job.status = "done"
                job.progress = 1
                self.emit("job_done", job.view())
            except Cancelled:
                job.status = "cancelled"
            except Exception as e:
                job.status = "skipped" if job.collection else "failed"
                job.error = friendly(e)
            finally:
                self._drop_video(job)
                self.changed()

    def _step(self, job, status, detail="", progress=0.0):
        if getattr(job, "cancel", False):
            raise Cancelled()
        job.status, job.detail, job.progress = status, detail, progress
        self.changed()

    def _progress(self, job, every=0.02):
        last = [0.0]

        def cb(p):
            if getattr(job, "cancel", False):
                raise Cancelled()
            if p - last[0] >= every or p >= 1:
                last[0] = p
                job.progress = p
                self.changed()
        return cb

    def _run(self, job):
        st = self.settings()
        self._step(job, "download", "Video only, no audio")
        job.video, job.meta = download.download(job.url, on_progress=self._progress(job))
        job.title = job.meta["title"]

        self._step(job, "locate", "Finding the tab and reading song details")
        job.meta["info"] = info.song_info(job.video, job.meta["title"], job.meta.get("channel", ""),
                                          job.meta.pop("description", ""), gemini_key=st.get("gemini_key"))
        job.meta["notes"] = info.summary(job.meta["info"])
        job.art = fetch(job.meta["info"].get("artwork"))
        region = vision.guess_region(job.video)
        cap = cv2.VideoCapture(job.video)
        W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        dur = vision.video_duration(cap)
        cap.release()
        found = region is not None
        if region is None:          # no tab lines found anywhere: start from the bottom third
            region = (0, int(H * 0.66), W, int(H * 0.34))
        job.region = tuple(int(v) for v in region)
        job.area = {"width": W, "height": H, "duration": dur, "region": job.region, "found": found, "time": dur * 0.5}

        # Give the user a chance to adjust the box. Nobody around: go with the guess.
        # A missed guess waits for a human, since scanning the wrong place is wasted time.
        wait = job.area_wait if found else 10 ** 9
        job.deadline = time.time() + wait
        self._step(job, "area", "Check the tab area")
        while not job.confirmed:
            if getattr(job, "cancel", False):
                raise Cancelled()
            if not job.held and time.time() >= job.deadline:
                break
            time.sleep(0.2)

        self._step(job, "scan", "Watching for new bars")
        strips, sp = vision.scan(job.video, job.region, on_progress=self._progress(job, 0.01),
                                 on_strip=lambda k: (setattr(job, "found", k), self.changed()))
        if not strips:
            raise RuntimeError("No tab showed up inside the box.")
        n = strips[0].n
        data = [(s.image(), s.color, s.t0, s.t1) for s in strips]

        self._step(job, "read", "Reading chord names and tempo")
        info.add_strip_info(job.meta["info"], layout.label_bands([d[0] for d in data], sp, n), sp)
        job.meta["notes"] = info.summary(job.meta["info"])

        self._step(job, "save", "Saving")
        sid = job.meta.get("id") or download.safe_filename(job.meta["title"])
        save_song(self.lib, st, sid, job.meta, data, sp, n, job.region[2], art=job.art,
                  collection=job.collection)
        job.song = sid

    def _drop_video(self, job):
        if job.video and os.path.exists(job.video):
            try:
                os.remove(job.video)
            except OSError:
                pass
        job.video = None


def save_song(lib, settings, sid, meta, strips, sp, n, screen_w, removed=(), art=None, collection=None,
              write_strips=True):
    """Write the PDF and the library entry. Returns (rows, row_px, pdf_path)."""
    keep = [(ink, col, sp) for k, (ink, col, _, _) in enumerate(strips) if k not in set(removed)]
    rows, row_px, path = [], 1, None
    if keep:
        rows, row_px = layout.build_rows(keep, screen_w, per_row=settings["per_row"],
                                         keep_color=bool(settings["keep_color"]), n=n)
        coll = collection if collection is not None else (lib.manifest(sid) or {}).get("collection")
        out_dir = os.path.join(settings["out_dir"], download.safe_filename(coll)) if coll else settings["out_dir"]
        os.makedirs(out_dir, exist_ok=True)
        name = info.file_name(meta)
        path = os.path.join(out_dir, download.safe_filename(name) + ".pdf")
        layout.write_pdf(path, rows, row_px, meta, paper=settings["paper"], art=art)
    lib.save(sid, meta, strips, sp, n, screen_w, removed=removed, pdf=path, art=art,
             collection=collection, write_strips=write_strips)
    return rows, row_px, path


def fetch(url, limit=2_000_000):
    """Small download (cover art). None on any failure; the art is a nice-to-have."""
    if not url or not url.startswith("https://"):
        return None
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=6) as r:
            return r.read(limit)
    except Exception:
        return None


def friendly(e):
    msg = str(e)
    if "Unsupported URL" in msg or "is not a valid URL" in msg:
        return "That doesn't look like a video link."
    if "Private video" in msg:
        return "Private video."
    if "Sign in" in msg or "age" in msg.lower() and "confirm" in msg.lower():
        return "YouTube wants a sign-in for this one (age-restricted or members-only)."
    if "HTTP Error 403" in msg:
        return "YouTube refused the download. Try again in a minute."
    if "Video unavailable" in msg or "removed" in msg.lower():
        return "Video unavailable."
    return msg.replace("ERROR: ", "").strip()[:200]
