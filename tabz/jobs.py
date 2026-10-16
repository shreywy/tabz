"""Background queue. Videos move through download -> locate -> area check -> scan while the
window stays free for browsing, reading and adding more. File imports run here too.

Downloads are network-bound, so several run at once. Locating and scanning are CPU-bound,
so only as many run as the machine has room for. A video waiting for its area check
holds no slot, so the rest keep moving.
"""
import itertools
import os
import shutil
import threading
import time

import cv2

from . import download, imports, info, layout, vision

AREA_WAIT = 15          # seconds the area guess waits for a look before going ahead
DOWNLOADS = 3           # downloads at once
CPU_SLOTS = 2 if (os.cpu_count() or 4) >= 8 else 1   # locate/scan at once
STALL = 600             # seconds a working stage may go without any progress before it's dropped

_ids = itertools.count(1)

WAITING = ("queued", "downloaded", "area", "ready")
WORKING = ("download", "locate", "scan", "read", "save", "import")
FINISHED = ("done", "failed", "skipped", "cancelled")


class Job:
    def __init__(self, url, collection=None, area_wait=AREA_WAIT, title=None):
        self.id = next(_ids)
        self.url = url
        self.collection = collection
        self.area_wait = area_wait
        self.title = title or url
        self.status = "queued"
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
        self.cancel = False
        self.touched = 0            # last sign of life from the working stage
        self.kind = "video"         # or "import": a batch of files from disk
        self.paths = []

    def view(self):
        return {
            "id": self.id, "kind": self.kind, "url": self.url, "title": self.title, "collection": self.collection,
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
        self._last_emit = 0.0
        self._pending = False
        threading.Thread(target=self._schedule, daemon=True).start()

    # ---- control from the window ----------------------------------------------
    def add(self, url, collection=None, area_wait=AREA_WAIT, title=None):
        job = Job(url, collection, area_wait, title)
        with self.lock:
            self.jobs.append(job)
        if not title:       # show the real title right away, not the link
            threading.Thread(target=self._fill_title, args=(job,), daemon=True).start()
        self.changed()
        self.wake.set()
        return job

    def add_import(self, paths):
        """Bring in files and folders from disk. Runs at once, beside the video queue."""
        names = [os.path.basename(p.rstrip("/\\")) or p for p in paths]
        job = Job("", title=names[0] if len(names) == 1 else f"{names[0]} and {len(names) - 1} more")
        job.kind, job.paths = "import", list(paths)
        job.status, job.detail, job.touched = "import", "Reading files", time.time()
        with self.lock:
            self.jobs.append(job)
        threading.Thread(target=self._guard, args=(self._import, job), daemon=True).start()
        self.changed()
        return job

    def _import(self, job):
        sources, skipped = imports.collect(job.paths)
        self._step(job, "import", f"Checking {len(sources)} files for copies")
        songs, dupes, loose = imports.plan(sources, self.lib.known())
        made, failed, videos = 0, [], 0
        for k, song in enumerate(songs):
            self._step(job, "import", f"{k + 1} of {len(songs)}: {song.title}", k / max(1, len(songs)))
            if song.kind == "video":
                if not song.main.member:          # a video inside a zip is left alone
                    self.add(song.main.path, area_wait=0, title=song.title)
                    videos += 1
                continue
            try:
                imports.build(self.lib, song)
                made += 1
            except Exception:
                failed.append(song.main.name)
            if made % 15 == 1:
                self.emit("library")
        bits = [f"{made} {'song' if made == 1 else 'songs'} added"]
        if videos:
            bits.append(f"{videos} {'video' if videos == 1 else 'videos'} queued")
        if dupes:
            bits.append(f"{dupes} copies skipped")
        if skipped or failed:
            bits.append(f"{len(skipped) + len(failed)} files tabz can't read")
        job.status, job.progress, job.detail = "done", 1.0, " · ".join(bits)
        job.error = ("Couldn't read: " + ", ".join((failed + skipped)[:12])) if failed or skipped else None
        self.emit("library")
        self.emit("import_done", {"title": job.title, "detail": job.detail, "made": made})

    def _fill_title(self, job):
        t = download.peek_title(job.url)
        if t and job.title == job.url:
            job.title = t
            self.changed()

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
            self.wake.set()

    def hold(self, jid):
        j = self.get(jid)
        if j and j.status == "area":
            j.held = True
            self.changed()

    def cancel(self, jid):
        j = self.get(jid)
        if j and j.status not in FINISHED:
            j.cancel = True
            self.wake.set()

    def clear(self):
        with self.lock:
            self.jobs = [j for j in self.jobs if j.status not in FINISHED]
        self.changed()

    def views(self):
        return [j.view() for j in self.jobs]

    def changed(self, job=None):
        """Tell the window. Several downloads report progress at once, so updates are
        batched to about seven a second."""
        now = time.time()
        if now - self._last_emit >= 0.15:
            self._last_emit = now
            self._pending = False
            self.emit("jobs", self.views())
        else:
            self._pending = True

    # ---- scheduling ---------------------------------------------------------------
    def _schedule(self):
        while True:
            try:
                self._tick()
            except Exception:
                pass            # one bad pass must not stop the queue for good
            self.wake.wait(0.25)
            self.wake.clear()

    def _tick(self):
        starts = []
        with self.lock:
            now = time.time()
            for j in self.jobs:
                if j.status in WORKING and now - j.touched > STALL:
                    # stuck (a download or decode that never returns): free its slot
                    # so the rest of a playlist keeps going
                    j.cancel = True
                    j.status = "skipped" if j.collection else "failed"
                    j.error = "Stopped responding, so tabz moved on. Try this one again on its own."
                elif j.cancel and j.status in WAITING:
                    j.status = "cancelled"
                    self._drop_video(j)
                elif j.status == "area" and (j.confirmed or (not j.held and now >= j.deadline)):
                    j.status, j.detail = "ready", "Waiting for a free slot"
            downloads = sum(j.status == "download" for j in self.jobs)
            cpu = sum(j.status in ("locate", "scan", "read", "save") for j in self.jobs)
            # finish songs before starting new ones: scans first, then locating
            for j in self.jobs:
                if cpu < CPU_SLOTS and j.status == "ready":
                    j.status, j.progress, j.detail = "scan", 0.0, "Watching for new bars"
                    cpu += 1
                    starts.append((self._scan, j))
            for j in self.jobs:
                if cpu < CPU_SLOTS and j.status == "downloaded":
                    j.status, j.progress, j.detail = "locate", 0.0, "Finding the tab and reading song details"
                    cpu += 1
                    starts.append((self._locate, j))
            for j in self.jobs:
                if downloads < DOWNLOADS and j.status == "queued":
                    j.status, j.progress, j.detail = "download", 0.0, "Video only, no audio"
                    downloads += 1
                    starts.append((self._download, j))
        for fn, j in starts:
            j.touched = time.time()
            threading.Thread(target=self._guard, args=(fn, j), daemon=True).start()
        if starts or self._pending:
            self._last_emit = 0
            self.changed()

    def _guard(self, fn, job):
        try:
            fn(job)
        except Cancelled:
            if job.status not in FINISHED:      # dropped as stuck keeps its message
                job.status = "cancelled"
            self._drop_video(job)
        except Exception as e:
            job.status = "skipped" if job.collection else "failed"
            job.error = friendly(e)
            self._drop_video(job)
        finally:
            self._last_emit = 0
            self.changed()
            self.wake.set()

    def _check(self, job):
        if job.cancel:
            raise Cancelled()

    def _step(self, job, status, detail="", progress=0.0):
        self._check(job)
        job.status, job.detail, job.progress = status, detail, progress
        job.touched = time.time()
        self.changed()

    def _progress(self, job, every=0.02):
        last = [0.0]

        def cb(p):
            self._check(job)
            job.touched = time.time()
            if p - last[0] >= every or p >= 1:
                last[0] = p
                job.progress = p
                self.changed()
        return cb

    # ---- stages ---------------------------------------------------------------------
    def _download(self, job):
        if os.path.isfile(job.url):             # a video file from disk: work on a copy
            os.makedirs(download.TEMP_DIR, exist_ok=True)
            job.video = os.path.join(download.TEMP_DIR, f"local-{job.id}{os.path.splitext(job.url)[1]}")
            shutil.copyfile(job.url, job.video)
            cap = cv2.VideoCapture(job.video)
            dur = vision.video_duration(cap)
            cap.release()
            job.meta = {"id": self.lib.new_id(job.title), "title": job.title, "url": "", "channel": "",
                        "duration": dur, "description": ""}
            job.status, job.detail = "downloaded", "Waiting for a free slot"
            return
        job.video, job.meta = download.download(job.url, on_progress=self._progress(job))
        job.title = job.meta["title"]
        self._check(job)
        job.status, job.detail = "downloaded", "Waiting for a free slot"

    def _locate(self, job):
        st = self.settings()
        job.meta["info"] = info.song_info(job.video, job.meta["title"], job.meta.get("channel", ""),
                                          job.meta.pop("description", ""), gemini_key=st.get("gemini_key"))
        job.meta["notes"] = info.summary(job.meta["info"])
        job.art = fetch(job.meta["info"].get("artwork"))
        region = vision.guess_region(job.video)
        self._check(job)
        cap = cv2.VideoCapture(job.video)
        W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        dur = vision.video_duration(cap)
        cap.release()
        found = region is not None
        if region is None:          # no tab lines found anywhere: start from the bottom third
            region = (0, int(H * 0.66), W, int(H * 0.34))
        job.region = tuple(int(v) for v in region)
        job.area = {"width": W, "height": H, "duration": dur, "region": job.region, "found": found, "time": dur * 0.5}
        # Give the user a chance to adjust the box; nobody around, go with the guess.
        # A missed guess waits for a human, since scanning the wrong place is wasted time.
        if found and job.area_wait <= 0:
            job.status, job.detail = "ready", "Waiting for a free slot"
        else:
            job.deadline = time.time() + (job.area_wait if found else 10 ** 9)
            job.status, job.detail = "area", "Check the tab area"

    def _scan(self, job):
        st = self.settings()
        strips, sp = vision.scan(job.video, job.region, on_progress=self._progress(job, 0.01),
                                 on_strip=lambda k: (setattr(job, "found", k), self.changed()))
        if not strips:
            raise RuntimeError("No tab showed up inside the box.")
        n = strips[0].n
        data = [(s.image(), s.color, s.t0, s.t1) for s in strips]
        self._drop_video(job)       # done with the video; free the disk early

        self._step(job, "read", "Reading chord names and tempo")
        info.add_strip_info(job.meta["info"], layout.label_bands([d[0] for d in data], sp, n), sp)
        job.meta["notes"] = info.summary(job.meta["info"])

        self._step(job, "save", "Saving")
        sid = job.meta.get("id") or download.safe_filename(job.meta["title"])
        save_song(self.lib, st, sid, job.meta, data, sp, n, job.region[2], art=job.art,
                  collection=job.collection)
        job.song = sid
        job.status, job.progress, job.detail = "done", 1.0, ""
        self.emit("job_done", job.view())

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
