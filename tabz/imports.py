"""Bring tab files in from disk: Guitar Pro, PDF, pictures of tab, tab videos, and audio
to play along with. Source files are only ever read. Everything tabz keeps is a copy.

The same song often turns up several times in a collection: as .gp, .gpx and .gp5, as
the PDF exported from those, and as the same files in a zip or a second folder. Exact
copies are dropped by content hash. Guitar Pro files with the same title (and artist,
when both say) become one song, the PDF of the same name rides along as that song's
PDF, and audio named like it becomes its backing track. PDFs and pictures that don't
belong to a Guitar Pro file each stay their own song: two "Unravel" PDFs are usually
two different arrangements.
"""
import hashlib
import io
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

import cv2
import numpy as np

GP = (".gp", ".gpx", ".gp5", ".gp4", ".gp3")     # preferred first: newer formats keep more
PDF = (".pdf",)
IMAGE = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
VIDEO = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v")
AUDIO = (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus")
KNOWN = GP + PDF + IMAGE + VIDEO + AUDIO


class Source:
    """One file on disk, or one file inside a zip."""

    def __init__(self, path, member=None, size=0):
        self.path, self.member, self.size = path, member, size
        self.name = os.path.basename(member or path)
        self.stem, self.ext = os.path.splitext(self.name)
        self.ext = self.ext.lower()
        inner = os.path.dirname(member) if member else os.path.dirname(path)
        self.folder = os.path.basename(inner.rstrip("/\\")) or os.path.splitext(os.path.basename(path))[0]
        self._sha = None
        self._data = None
        self.meta = {}

    @property
    def origin(self):
        return f"{self.path} > {self.member}" if self.member else self.path

    def read(self):
        if self._data is not None:
            return self._data
        if self.member:
            with zipfile.ZipFile(self.path) as z:
                data = z.read(self.member)
        else:
            with open(self.path, "rb") as f:
                data = f.read()
        if len(data) < 20_000_000:          # tab files are small; videos are read when needed
            self._data = data
        return data

    @property
    def sha(self):
        if self._sha is None:
            self._sha = hashlib.sha1(self.read()).hexdigest()
        return self._sha


def _skip(name):
    return name.startswith(".") or name.startswith("._") or name == "__MACOSX"


def collect(paths):
    """Every importable file under paths, zips opened (one level). Returns (sources,
    skipped names). Nothing is extracted to disk."""
    out, skipped = [], []

    def add(p):
        ext = os.path.splitext(p)[1].lower()
        if ext in KNOWN:
            out.append(Source(p, size=os.path.getsize(p)))
        elif ext == ".zip":
            try:
                with zipfile.ZipFile(p) as z:
                    for i in z.infolist():
                        parts = i.filename.replace("\\", "/").split("/")
                        if i.is_dir() or any(_skip(x) for x in parts):
                            continue
                        if os.path.splitext(i.filename)[1].lower() in KNOWN:
                            out.append(Source(p, i.filename, i.file_size))
            except (zipfile.BadZipFile, OSError):
                skipped.append(os.path.basename(p))
        elif not _skip(os.path.basename(p)):
            skipped.append(os.path.basename(p))

    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs if not _skip(d))
                for f in sorted(files):
                    if not _skip(f):
                        add(os.path.join(root, f))
        elif os.path.isfile(p):
            add(p)
    return out, skipped


# ---- Guitar Pro headers --------------------------------------------------------------
# Only title, artist and album, for matching duplicates. The window reads everything
# else (tracks, tuning, tempo) with the same library that draws the score.

def _gpif_meta(xml):
    root = ET.fromstring(xml)
    sc = root.find("Score")
    get = lambda k: ((sc.findtext(k) or "").strip() if sc is not None else "")
    return {"title": get("Title"), "artist": get("Artist"), "album": get("Album")}


def _bcfz(data):
    """Guitar Pro 6's compressed container (BCFZ), unpacked to its BCFS filesystem."""
    expected = int.from_bytes(data[4:8], "little")
    bits = "".join(np.unpackbits(np.frombuffer(data[8:], np.uint8)).astype("U1"))
    pos, out = 0, bytearray()

    def read(n, reverse=False):
        nonlocal pos
        b = bits[pos:pos + n]
        if len(b) < n:
            raise EOFError
        pos += n
        return int(b[::-1] if reverse else b, 2) if n else 0

    try:
        while len(out) < expected:
            if read(1):
                size = read(4)
                offset, length = read(size, True), read(size, True)
                start = len(out) - offset
                out += out[start:start + min(offset, length)]
            else:
                for _ in range(read(2, True)):
                    out.append(read(8))
    except EOFError:
        pass
    return bytes(out[4:])          # drop the "BCFS" header


def _bcfs_file(data, want):
    sector = 4096
    off = sector
    while off + 3 < len(data):
        if int.from_bytes(data[off:off + 4], "little") == 2:
            name = data[off + 4:off + 131].split(b"\0")[0].decode("latin-1")
            size = int.from_bytes(data[off + 140:off + 144], "little")
            ptr, k, buf = off + 148, 0, bytearray()
            while True:
                s = int.from_bytes(data[ptr + 4 * k:ptr + 4 * k + 4], "little")
                k += 1
                if not s:
                    break
                off = s * sector
                buf += data[off:off + sector]
            if name == want:
                return bytes(buf[:size])
        off += sector
    return None


def _gp345_meta(data):
    """Title, artist, album from the start of a .gp3/.gp4/.gp5 file."""
    pos = 31                                    # version string, 30 chars + length byte

    def string():
        nonlocal pos
        n = int.from_bytes(data[pos:pos + 4], "little")
        ln = data[pos + 4]
        s = data[pos + 5:pos + 5 + min(ln, max(0, n - 1))]
        pos += 4 + max(n, 1)
        return s.decode("cp1252", "replace").strip()

    title, _subtitle, artist, album = string(), string(), string(), string()
    return {"title": title, "artist": artist, "album": album}


def gp_header(data, ext):
    try:
        if ext == ".gp":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                return _gpif_meta(z.read("Content/score.gpif"))
        if ext == ".gpx":
            fs = _bcfz(data) if data[:4] == b"BCFZ" else data[4:]
            xml = _bcfs_file(fs, "score.gpif")
            return _gpif_meta(xml) if xml else {}
        if ext in (".gp3", ".gp4", ".gp5"):
            return _gp345_meta(data)
    except Exception:
        pass
    return {}


# ---- matching ----------------------------------------------------------------------------
NOISE = re.compile(r"\b(guitar|tabs?|tablature|pdf|gpx?|gp\d|copy( of)?|final|backing( track)?|"
                   r"guitarless|no guitar|without guitar|instrumental|track|bt|official|full)\b")


def fold(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c)).lower()


def norm(s):
    """Match key for a title or file name: "[1] Genesis Guitar Tabs" and "1. Genesis" -> "genesis"."""
    s = NOISE.sub(" ", fold(s).replace("_", " "))
    s = re.sub(r"^[\W\s]*\[?\d{1,2}\]?\s*[.)\]\-\s]\s*", "", s)     # track numbers
    return re.sub(r"[^a-z0-9]+", "", s)


def stem_keys(stem):
    """Keys a file name can match on: the whole name, and its last part after " - "
    ("UnprocessedQuarantineRiffs - Fear_Guitar Tab" also matches "Fear")."""
    keys = {norm(stem)}
    parts = re.split(r"\s+-\s+", stem.replace("_", " "))
    if len(parts) > 1:
        keys.add(norm(parts[-1]))
    return {k for k in keys if len(k) >= 2}


def _dirs(s):
    parts = os.path.normpath(os.path.dirname(s.path)).split(os.sep)
    return parts + ([os.path.basename(s.path)] + s.member.replace("\\", "/").split("/")[:-1] if s.member else [])


def _shared(a, b):
    """How many folders deep two files' locations agree."""
    n = 0
    for x, y in zip(_dirs(a), _dirs(b)):
        if x.lower() != y.lower():
            break
        n += 1
    return n


class Song:
    """One song to make: a main file plus what rides along with it."""

    def __init__(self, kind, main):
        self.kind, self.main = kind, main
        self.pdf = self.audio = None
        self.files = [main]                      # every source this song covers
        self.title = main.meta.get("title") or tidy_title(main.stem)
        self.artist = main.meta.get("artist") or ""
        self.album = main.meta.get("album") or ""

    @property
    def shas(self):
        return [f.sha for f in self.files]


def tidy_title(stem):
    """A readable title from a file name: drop track numbers, underscores, "Guitar Tab"."""
    s = stem.replace("_", " ")
    s = re.sub(r"^[\W\s]*\[?\d{1,2}\]?\s*[.)\]\-]\s*", "", s)
    s = re.sub(r"(?i)\b(guitar\s+)?(tabs?|tablature)\b", " ", s)
    s = re.sub(r"(?i)\b(copy( of)?|unproccessed|pdf)\b", " ", s)
    return re.sub(r"\s{2,}", " ", s).strip(" -_.") or stem


def plan(sources, known=()):
    """Decide which songs to make. known: content hashes already in the library.
    Returns (songs, duplicates, unmatched_audio)."""
    seen, uniq, dupes = set(known), [], 0
    for s in sources:
        if s.sha in seen:
            dupes += 1
            continue
        seen.add(s.sha)
        uniq.append(s)

    songs, keys = [], {}
    gps = sorted((s for s in uniq if s.ext in GP), key=lambda s: GP.index(s.ext))
    for s in gps:
        s.meta = gp_header(s.read(), s.ext)
        title = s.meta.get("title") or tidy_title(s.stem)
        k, a = norm(title), norm(s.meta.get("artist"))
        match = next((g for g in songs if norm(g.title) == k and (not a or not norm(g.artist) or norm(g.artist) == a)), None)
        if match:
            match.files.append(s)
            if not match.artist and s.meta.get("artist"):
                match.artist = s.meta["artist"]
        else:
            match = Song("gp", s)
            songs.append(match)
        for key in stem_keys(s.stem) | {k}:
            keys.setdefault(key, match)

    # A Guitar Pro song takes one PDF of the same name: the one stored closest to it.
    # Any other PDF with that name is probably another arrangement, so it stays a song.
    pdfs = [s for s in uniq if s.ext in PDF]
    taken = set()
    for g in songs:
        cands = [s for s in pdfs if any(keys.get(k) is g for k in stem_keys(s.stem))]
        if cands:
            best = max(cands, key=lambda s: max(_shared(s, f) for f in g.files))
            g.files.append(best)
            g.pdf = best
            taken.add(id(best))
    for s in uniq:
        if s.ext in PDF:
            if id(s) in taken:
                continue
            g = Song("pdf", s)
            g.pdf = s
            songs.append(g)
            for key in stem_keys(s.stem):
                keys.setdefault(key, g)
        elif s.ext in IMAGE:
            songs.append(Song("image", s))
        elif s.ext in VIDEO:
            songs.append(Song("video", s))

    loose = []
    for s in uniq:
        if s.ext in AUDIO:
            g = next((keys[k] for k in stem_keys(s.stem) if k in keys), None)
            if g is not None and g.audio is None:
                g.audio = s
                g.files.append(s)
            else:
                loose.append(s)
    return songs, dupes, loose


# ---- what kind of guitar -----------------------------------------------------------------
ACOUSTIC = re.compile(r"\b(fingerstyle|finger\s*style|acoustic|nylon|classical|fingerpicking)\b", re.I)
ELECTRIC = re.compile(r"\b(electric|distortion|overdrive|shred|metal|djent)\b", re.I)


def guitar_from_text(*texts):
    t = " ".join(x for x in texts if x)
    if ACOUSTIC.search(t):
        return "Acoustic"
    if ELECTRIC.search(t):
        return "Electric"
    return None


def guitar_from_programs(programs):
    """General MIDI programs of a score's tracks: 24-25 acoustic, 26-31 electric, 32-39 bass."""
    if any(26 <= p <= 31 for p in programs):
        return "Electric"
    if any(24 <= p <= 25 for p in programs):
        return "Acoustic"
    if any(32 <= p <= 39 for p in programs):
        return "Bass"
    return None


# ---- PDF pages -----------------------------------------------------------------------------

def pdf_pages(path, width=2000, limit=60):
    """Every page of a PDF as a BGR image, through Windows' own PDF renderer."""
    import asyncio
    from winrt.windows.data.pdf import PdfDocument, PdfPageRenderOptions
    from winrt.windows.storage import StorageFile
    from winrt.windows.storage.streams import DataReader, InMemoryRandomAccessStream

    async def run():
        f = await StorageFile.get_file_from_path_async(os.path.abspath(path))
        doc = await PdfDocument.load_from_file_async(f)
        out = []
        for i in range(min(doc.page_count, limit)):
            o = PdfPageRenderOptions()
            o.destination_width = width
            s = InMemoryRandomAccessStream()
            await doc.get_page(i).render_with_options_to_stream_async(s, o)
            r = DataReader(s.get_input_stream_at(0))
            buf = bytearray(await r.load_async(s.size))
            r.read_bytes(buf)
            img = cv2.imdecode(np.frombuffer(bytes(buf), np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                out.append(img)
        return out

    return asyncio.run(run())


def decode_image(data):
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is not None and min(img.shape[:2]) < 1:
        return None
    return img


def build(lib, song):
    """Make one library song from a planned Song (not videos: those go through the queue
    like a YouTube link). Returns its id."""
    import time
    from . import info, vision
    title, artist = song.title, song.artist
    sid = lib.new_id(f"{title} - {artist}" if artist else title)
    folder = lib.folder(sid)
    os.makedirs(folder, exist_ok=True)

    def copy(src, name):
        with open(os.path.join(folder, name), "wb") as f:
            f.write(src.read())
        return name

    i = {"song": title, "artist": artist or None, "album": song.album or None, "chords": [], "key_guessed": False}
    m = {"id": sid, "kind": song.kind, "title": title, "url": "", "channel": "", "duration": 0,
         "info": i, "notes": "", "folder": song.main.folder, "files": [f.origin for f in song.files],
         "sources": song.shas, "need_lookup": True, "created": time.time(), "segments": []}
    if song.audio:
        m["backing"] = {"file": copy(song.audio, "backing" + song.audio.ext), "offset": 0.0, "name": song.audio.name}
    if song.pdf:
        m["pdf"] = os.path.join(folder, copy(song.pdf, "original.pdf"))
    if song.kind == "gp":
        m["score"] = copy(song.main, "score" + song.main.ext)
        m["need_scan"] = True           # the window reads tracks, tuning and tempo
        lib.write(m)
        return sid

    if song.kind == "image":
        m["original"] = copy(song.main, "original" + song.main.ext)
        pages = [decode_image(song.main.read())]
    else:
        pages = pdf_pages(m["pdf"])
    pages = [p for p in pages if p is not None]
    lines = [" ".join(w) for w in info._ocr(pages[0][:max(1, pages[0].shape[0] // 3)])] if pages else []
    i.update({k: v for k, v in info.facts_from_text(lines).items() if v is not None})
    i["guitar"] = guitar_from_text(title, song.main.folder, *lines)
    m["notes"] = info.summary(i)

    found = []
    for p in pages:
        crops, sp, n = vision.page_systems(p)
        found += [(c, sp, n) for c in crops]
    if not found:                       # no tab lines: kept as its PDF or picture
        lib.write(m)
        return sid
    counts = [n for _, _, n in found]
    n = max(set(counts), key=counts.count)
    found = [f for f in found if f[2] == n]
    sp = float(np.median([s for _, s, _ in found]))
    strips = [(vision.ink(c, s), c, float(k), float(k + 1)) for k, (c, s, _) in enumerate(found)]
    screen_w = int(np.median([s[0].shape[1] for s in strips]))
    meta = {k: m[k] for k in ("title", "url", "channel", "duration", "notes", "info")}
    lib.save(sid, meta, strips, sp, n, screen_w, pdf=m.get("pdf"), bars=True, extra=m)
    return sid


if __name__ == "__main__":
    assert norm("[1] Genesis Guitar Tabs") == norm("1. Genesis") == "genesis"
    assert norm("_ 3. Goose") == norm("3. Goose") == "goose"
    assert norm("Copy_of_6_The_Worst_Guitarless_Backing_Track") == norm("6. The Worst") == "theworst"
    assert "fear" in stem_keys("UnprocessedQuarantineRiffs - Fear_Guitar Tab")
    assert norm("G.O.A.T.") != norm("No intro G.O.A.T. - Copy")
    assert tidy_title("Chimera Guitar Tabs") == "Chimera"
    assert guitar_from_programs([25, 30]) == "Electric" and guitar_from_programs([24]) == "Acoustic"
    assert guitar_from_text("Kenneth Acoustic") == "Acoustic"
    print("ok")
