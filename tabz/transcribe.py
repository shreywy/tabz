"""Read tab images into notes, so tab from videos, PDFs and pictures can be played.

A rough reading, kept next to the picture of the tab, never instead of it:
  - fret numbers are found on the string lines and read against digit shapes drawn in
    common fonts (Windows' fonts and the app's own)
  - numbers stacked at the same place are one chord
  - barlines split bars; inside a bar, the space before the next note sets how long a note
    lasts, rounded to sixteenths. Tab spacing only roughly follows rhythm, so rhythm is
    the weakest part. Frets and their order are what this gets right most of the time.
Output is alphaTex, which the window's score player reads directly.
"""
import os
import re

import cv2
import numpy as np

from . import layout
from .vision import _odd

GLYPHS = "0123456789x"
REJECT = "TABHPhpsl/~()<>"           # marks that sit on strings but aren't frets
FONTS = ["arial.ttf", "arialbd.ttf", "times.ttf", "segoeui.ttf", "consola.ttf", "cour.ttf", "verdana.ttf",
         "tahoma.ttf", "calibri.ttf", "lucon.ttf"]
TW, TH = 16, 24                      # template size


def _templates():
    from PIL import Image, ImageDraw, ImageFont
    win = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    here = os.path.join(os.path.dirname(__file__), "ui", "fonts")
    paths = [os.path.join(win, f) for f in FONTS] + [os.path.join(here, f) for f in
                                                      ("IBMPlexSans-Regular.ttf", "IBMPlexMono-Regular.ttf")]
    out = []
    for p in paths:
        if not os.path.exists(p):
            continue
        font = ImageFont.truetype(p, 64)
        for ch in GLYPHS + REJECT:
            im = Image.new("L", (120, 120), 0)
            ImageDraw.Draw(im).text((20, 10), ch, fill=255, font=font)
            a = np.array(im)
            ys, xs = np.nonzero(a > 100)
            if not len(ys):
                continue
            crop = a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
            out.append((ch, _norm(crop > 100), crop.shape[1] / crop.shape[0]))
    return out


def _norm(mask):
    """A glyph mask scaled into the template box, keeping its shape centred."""
    h, w = mask.shape
    k = min(TW / w, TH / h)
    im = cv2.resize(mask.astype(np.float32), (max(1, round(w * k)), max(1, round(h * k))), interpolation=cv2.INTER_AREA)
    box = np.zeros((TH, TW), np.float32)
    y, x = (TH - im.shape[0]) // 2, (TW - im.shape[1]) // 2
    box[y:y + im.shape[0], x:x + im.shape[1]] = im
    box -= box.mean()
    return box / (np.linalg.norm(box) or 1)


_TPL = None


def read_glyph(mask):
    """(character, confidence) for one connected mark."""
    global _TPL
    if _TPL is None:
        _TPL = _templates()
    g, aspect = _norm(mask), mask.shape[1] / mask.shape[0]
    best, score = None, -1.0
    for ch, t, ta in _TPL:
        s = float((g * t).sum()) - 0.35 * abs(np.log(max(aspect, 0.05) / max(ta, 0.05)))
        if s > score:
            best, score = ch, s
    return best, score


def _strip_notes(info, n):
    """Notes in one cleaned strip: [(x, string 1..n from the top, fret or "x")] and barline xs."""
    img, sp, y0 = info.img, info.sp, info.y_first
    y1 = y0 + (n - 1) * sp
    m = (img > 0.45).astype(np.uint8)
    # take out the string lines (and anything else long and flat) and full-height barlines
    flat = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (_odd(sp * 1.6), 1)))
    tall = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, _odd((y1 - y0) * 0.85))))
    line = cv2.dilate(flat, np.ones((3, 1), np.uint8))
    # rhythm stems hang off the numbers and make them look too tall: longer than any digit stroke
    stems = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, _odd(sp * 1.25))))
    off = m & ~line & ~cv2.dilate(tall, np.ones((1, 3), np.uint8)) & ~stems
    # A number sits across its string. Put back the line pixels that have ink of the number
    # just above or below them, so 8 and 9 keep the stroke the line runs through.
    near = cv2.dilate(off, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 7)))
    marks = off | (m & line & near)
    bars = info.cuts[0] if info.cuts[1] else []
    # where each string really runs (the fitted staff can be a pixel or two out)
    prof = flat.sum(axis=1).astype(np.float64)
    strings = []
    for k in range(n):
        c = y0 + k * sp
        a, b = max(0, int(c - 0.35 * sp)), min(len(prof), int(c + 0.35 * sp) + 1)
        w = prof[a:b]
        strings.append(float((np.arange(a, b) * w).sum() / w.sum()) if w.sum() > 0 else c)
    strings = np.array(strings)
    k, lab, stats, cent = cv2.connectedComponentsWithStats(marks, connectivity=8)
    top, bot = strings[0] - 0.75 * sp, strings[-1] + 0.75 * sp
    # A slanted stroke (in 2 or 7) can still come apart where the line ran: join pieces
    # stacked a few pixels apart that overlap side to side.
    boxes = [[int(v) for v in stats[i][:4]] + [{i}] for i in range(1, k)
             if stats[i][4] >= 2 and top <= stats[i][1] and stats[i][1] + stats[i][3] <= bot]
    boxes.sort(key=lambda b: (b[0], b[1]))
    merged = True
    while merged:
        merged = False
        for a in range(len(boxes)):
            for b in range(a + 1, len(boxes)):
                A, B = boxes[a], boxes[b]
                if B[0] > A[0] + A[2]:
                    break
                ov = min(A[0] + A[2], B[0] + B[2]) - max(A[0], B[0])
                gap = max(A[1], B[1]) - min(A[1] + A[3], B[1] + B[3])
                if ov >= 0.5 * min(A[2], B[2]) and -4 <= gap <= 5 and max(A[1] + A[3], B[1] + B[3]) - min(A[1], B[1]) <= 1.15 * sp:
                    x, y = min(A[0], B[0]), min(A[1], B[1])
                    boxes[a] = [x, y, max(A[0] + A[2], B[0] + B[2]) - x, max(A[1] + A[3], B[1] + B[3]) - y, A[4] | B[4]]
                    del boxes[b]
                    merged = True
                    break
            if merged:
                break
    found = []
    for x, y, w, h, ids in boxes:
        if w * h < 4:
            continue
        if not (0.3 * sp <= h <= 1.15 * sp) or w > 1.3 * sp:
            continue
        cy = y + h / 2
        s = int(np.argmin(np.abs(strings - cy)))
        if abs(cy - strings[s]) > 0.32 * sp:
            continue
        ch, conf = read_glyph(np.isin(lab[y:y + h, x:x + w], list(ids)))
        if conf >= 0.42:
            found.append([x, x + w, s, ch, h])
    # small print next to a fret (a harmonic's <12>, a bend's "full") isn't a fret
    digit_h = np.percentile([f[4] for f in found if f[3].isdigit()] or [sp], 85)
    found = [f for f in found if not (f[3].isdigit() and f[4] < 0.78 * digit_h)]
    # Digits side by side on one string make one number. Anything else written next to a
    # fret (harmonic marks like <12>, ghost-note brackets, h/p/sl) ends the number.
    found.sort(key=lambda f: (f[2], f[0]))
    out, cur = [], []

    def close():
        if not cur:
            return
        text = "".join(f[3] for f in cur)
        if text == "x" or (text.isdigit() and int(text) <= 24):
            out.append(((cur[0][0] + cur[-1][1]) / 2, cur[0][2] + 1, "x" if text == "x" else int(text)))
        cur.clear()

    for f in found:
        joins = cur and f[2] == cur[-1][2] and f[0] - cur[-1][1] < 0.45 * sp
        if f[3] in REJECT or (f[3] == "x" and cur) or (cur and cur[-1][3] == "x"):
            if not joins or f[3] in REJECT:
                close()
            if f[3] in REJECT:
                continue
        elif not joins:
            close()
        cur.append(f)
    close()
    # a harmonic's <12> right after its fret reads as a second note on that string: drop
    # notes that start within a digit's width of the one before them on the same string
    out.sort(key=lambda o: (o[1], o[0]))
    keep = [o for k, o in enumerate(out) if not (k and out[k - 1][1] == o[1] and o[0] - out[k - 1][0] < 0.9 * sp)]
    return keep, bars


def _beats(notes, sp):
    """Notes stacked at one place make one beat: [(x, {string: fret})]."""
    out = []
    for x, s, f in sorted(notes):
        if out and x - out[-1][0] < 0.45 * sp:
            out[-1][1].setdefault(s, f)
        else:
            out.append([x, {s: f}])
    return out


SIXTEENTHS = {1: ".16", 2: ".8", 3: ".8{d}", 4: ".4", 6: ".4{d}", 8: ".2", 12: ".2{d}", 16: ".1"}


def _rhythm(xs, x0, x1, total=16):
    """Sixteenth counts for beats at xs in a bar from x0 to x1: the space up to the next
    note, rounded to lengths a score can write, adding up to the bar."""
    if not xs:
        return []
    gaps = [b - a for a, b in zip(xs, xs[1:])]
    gaps.append(max(x1 - xs[-1], (np.median(gaps) if gaps else x1 - x0)) if gaps else 1.0)
    gaps = np.maximum(gaps, 1e-3)
    raw = np.array(gaps) / sum(gaps) * total
    allowed = np.array(sorted(SIXTEENTHS))
    q = [int(allowed[np.argmin(np.abs(allowed - r))]) for r in raw]
    while sum(q) > total and max(q) > 1:            # too long: shorten the longest
        i = int(np.argmax(q))
        q[i] = int(allowed[allowed < q[i]].max())
    if len(xs) > total:                             # more notes than sixteenths: cram them
        q = [1] * len(xs)
    return q


def _tuning(info, n):
    t = (info or {}).get("tuning") or ""
    if n == 4:
        return "G2 D2 A1 E1"
    if n == 7:
        return "E4 B3 G3 D3 A2 E2 B1"
    if n == 5:
        return "G2 D2 A1 E1 B0"
    if re.search(r"drop\s*d", t, re.I):
        return "E4 B3 G3 D3 A2 D2"
    if re.search(r"half|eb|e\s*flat", t, re.I):
        return "Eb4 Bb3 Gb3 Db3 Ab2 Eb2"
    return "E4 B3 G3 D3 A2 E2"


def _q(s):
    return '"' + str(s).replace('"', "'").replace("\\", "/") + '"'


def transcribe(images, sp, n, info=None, beats_per_bar=4):
    """images: cleaned strips (ink 0..1) in order. Returns (alphaTex, stats) or (None, stats)."""
    bars, notes_total = [], 0
    for img in images:
        si = layout.StripInfo(img, None, sp, n)
        if not n:
            continue
        notes, cuts = _strip_notes(si, n)
        notes_total += len(notes)
        beats = _beats(notes, si.sp)
        edges = sorted(set([si.x_start] + [c for c in cuts if si.x_start < c < si.x_end] + [si.x_end]))
        if len(edges) < 2:
            continue
        spans = list(zip(edges, edges[1:]))
        for k, (a, b) in enumerate(spans):
            inside = [bt for bt in beats if a <= bt[0] < b]
            # nothing before the first barline but the clef, or after the last one: not a bar
            if not inside and (b - a < 1.5 * si.sp or (cuts and (k == 0 or k == len(spans) - 1))):
                continue
            bars.append((inside, a, b))
    if notes_total < 4:
        return None, {"notes": notes_total, "bars": len(bars)}

    total = beats_per_bar * 4
    lines = []
    for inside, a, b in bars:
        q = _rhythm([bt[0] for bt in inside], a, b, total)
        parts = []
        for (x, frets), d in zip(inside, q):
            ns = [("0" if f == "x" else str(f)) + f".{s}" + ("{x}" if f == "x" else "") for s, f in sorted(frets.items())]
            head = ns[0] if len(ns) == 1 else "(" + " ".join(ns) + ")"
            parts.append(head + SIXTEENTHS.get(d, ".16"))
        rest = total - sum(q)
        while rest > 0:                              # fill the bar out with rests
            d = max(k for k in SIXTEENTHS if k <= rest and "{d}" not in SIXTEENTHS[k])
            parts.append("r" + SIXTEENTHS[d])
            rest -= d
        lines.append(" ".join(parts) + " |")
    i = info or {}
    tempo = int(i.get("tempo") or 100)
    head = [
        f"\\title {_q(i.get('song') or 'Tab')}",
        *([f"\\artist {_q(i['artist'])}"] if i.get("artist") else []),
        f'\\track ("Guitar") {{\n  instrument {"acousticbass" if n in (4, 5) else "acousticguitarsteel"}\n}}',
        "\\staff {tabs}",
        f"\\tuning ({_tuning(i, n)})",
        f"\\ts ({beats_per_bar} 4)",
        f"\\tempo {tempo}",
    ]
    if i.get("capo"):
        head.append(f"\\capo {int(i['capo'])}")
    found = [[sorted(f.items()) for _, f in inside] for inside, _, _ in bars]
    return "\n".join(head + lines) + "\n", {"notes": notes_total, "bars": len(bars), "found": found}
