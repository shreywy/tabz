"""Importing files: duplicates merge, PDFs become bars, audio becomes a backing track,
and the source files are never changed. Run: python tests/test_import.py"""
import hashlib
import os
import shutil
import sys
import tempfile
import wave
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from reportlab.pdfgen import canvas  # noqa: E402

from tabz import imports  # noqa: E402
from tabz.library import Library  # noqa: E402

tmp = tempfile.mkdtemp(prefix="tabz-import-")
src = os.path.join(tmp, "tabs")
os.makedirs(os.path.join(src, "a"))
os.makedirs(os.path.join(src, "b"))


def gp7(path, title, artist, pad=""):
    """A minimal Guitar Pro 7 file: a zip holding the score XML."""
    xml = f"<GPIF><Score><Title><![CDATA[{title}]]></Title><Artist><![CDATA[{artist}]]></Artist></Score>{pad}</GPIF>"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Content/score.gpif", xml)


def tab_pdf(path, lines=4):
    """A page of 6-line tab staves with barlines and a few fret numbers."""
    c = canvas.Canvas(path, pagesize=(595, 842))
    for k in range(lines):
        top = 760 - k * 120
        for s in range(6):
            c.line(50, top - s * 9, 545, top - s * 9)
        for x in (50, 174, 298, 422, 545):
            c.line(x, top, x, top - 45)
        for j, x in enumerate(range(70, 540, 30)):
            # like tab software: the line is blanked behind the number, which sits on it
            y = top - 9 * (j % 6)
            c.setFillColorRGB(1, 1, 1)
            c.rect(x - 1, y - 4, 7.5, 8, stroke=0, fill=1)
            c.setFillColorRGB(0, 0, 0)
            c.drawString(x, y - 3.2, str(j % 10))
    c.save()


gp7(os.path.join(src, "a", "1. Song A.gp"), "Song A", "Band")
gp7(os.path.join(src, "b", "Song A Guitar Tab.gp"), "Song A", "Band", pad="<!-- other export -->")
tab_pdf(os.path.join(src, "a", "Song A.pdf"))
tab_pdf(os.path.join(src, "b", "Other Song.pdf"), lines=5)
with zipfile.ZipFile(os.path.join(src, "copies.zip"), "w") as z:          # an exact copy inside a zip
    z.write(os.path.join(src, "b", "Other Song.pdf"), "inside/Other Song.pdf")
with wave.open(os.path.join(src, "a", "Song A Backing Track.wav"), "wb") as w:
    w.setnchannels(1), w.setsampwidth(2), w.setframerate(8000), w.writeframes(b"\0\0" * 800)
open(os.path.join(src, "notes.txt"), "w").write("not a tab")


def snapshot():
    out = {}
    for root, _, files in os.walk(src):
        for f in files:
            p = os.path.join(root, f)
            out[p] = (hashlib.sha1(open(p, "rb").read()).hexdigest(), os.path.getmtime(p))
    return out


before = snapshot()
lib = Library(os.path.join(tmp, "library"))
sources, skipped = imports.collect([src])
assert skipped == ["notes.txt"], skipped
songs, dupes, loose = imports.plan(sources, lib.known())
assert dupes == 1 and not loose, (dupes, loose)
kinds = sorted((s.kind, s.title) for s in songs)
assert kinds == [("gp", "Song A"), ("pdf", "Other Song")], kinds
a = next(s for s in songs if s.kind == "gp")
assert len([f for f in a.files if f.ext == ".gp"]) == 2, "both exports of Song A should be one song"
assert a.pdf and a.pdf.name == "Song A.pdf" and a.audio and a.audio.ext == ".wav"

for s in songs:
    imports.build(lib, s)
by = {m["kind"]: m for m in lib.manifests()}
assert by["gp"]["score"].startswith("score") and by["gp"]["backing"]["file"] == "backing.wav"
assert by["gp"]["info"]["artist"] == "Band" and by["gp"]["need_scan"]
assert os.path.exists(by["gp"]["pdf"]) and os.path.dirname(by["gp"]["pdf"]) == lib.folder(by["gp"]["id"])
segs = by["pdf"]["segments"]
assert len(segs) >= 10 and all(sg["kind"] == "bar" for sg in segs), [sg["kind"] for sg in segs][:5]
assert lib.reader_data(by["pdf"]["id"])["pdf_src"]

# reading the PDF's tab into notes: the frets drawn above, string by string, in order
import difflib  # noqa: E402
import re  # noqa: E402
assert lib.transcribe(by["pdf"]["id"]) == "score.atex"
tex = open(os.path.join(lib.folder(by["pdf"]["id"]), "score.atex"), encoding="utf-8").read()
read = re.findall(r"(?<![\d.])(\d{1,2})\.([1-6])(?=[.\s)])", tex.split("\\tempo")[1])
drawn = [(str(j % 10), str(j % 6 + 1)) for _ in range(5) for j, _x in enumerate(range(70, 540, 30))]
match = difflib.SequenceMatcher(None, drawn, read, autojunk=False).ratio()
assert match > 0.85, (match, read[:20])

# again: everything is already in
assert imports.plan(imports.collect([src])[0], lib.known())[0] == []
assert snapshot() == before, "a source file changed"
shutil.rmtree(tmp, ignore_errors=True)
print("ok")
