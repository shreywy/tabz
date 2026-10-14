"""Turn cleaned strips into rows on pages, then into a PDF.

Every strip is scaled to the same string spacing and aligned on its top string, so rows
from different pages of a video line up. Long scrolled strips are broken at barlines
(or at gaps between notes when a style has no barlines) to fill each row.
"""
import datetime
import os

import cv2
import numpy as np
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdfcanvas

from .vision import _odd, row_peaks, staff_groups

FONT_DIR = os.path.join(os.path.dirname(__file__), "ui", "fonts")
for name, file in (("Plex", "IBMPlexSans-Regular.ttf"), ("Plex-Medium", "IBMPlexSans-Medium.ttf"),
                   ("Plex-SemiBold", "IBMPlexSans-SemiBold.ttf"), ("PlexMono", "IBMPlexMono-Regular.ttf")):
    pdfmetrics.registerFont(TTFont(name, os.path.join(FONT_DIR, file)))

PAPER = {"a4": A4, "letter": LETTER}
MARGIN = 40     # page margin, points
ROW_GAP = 14    # space between rows, points
INK = 0x1c  # near-black reads softer than pure #000 on screen and prints the same


class Piece:
    """A rectangle cut from a strip, in strip pixels, plus where its staff sits."""

    def __init__(self, strip, x0, x1):
        self.strip, self.x0, self.x1 = strip, x0, x1

    @property
    def width(self):
        return self.x1 - self.x0


class StripInfo:
    def __init__(self, img, color, sp_hint):
        self.img = img          # ink 0..1
        self.color = color      # BGR uint8 or None
        m = cv2.morphologyEx(img, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (_odd(sp_hint * 0.9), 1)))
        groups = staff_groups(row_peaks(m.mean(axis=1), 0.45, sp_hint * 0.6))
        groups = [g for g in groups if abs(g[2] - sp_hint) < sp_hint * 0.25] or groups
        if groups:
            g = max(groups, key=lambda g: g[3])
            self.y_first, self.y_last, self.sp = g[0], g[1], g[2]
        else:  # shouldn't happen (scan only keeps frames with a staff), but don't crash
            self.y_first, self.y_last, self.sp = img.shape[0] * 0.3, img.shape[0] * 0.8, sp_hint
        self.top, self.bottom = self._content_rows()
        self.cuts = self._cut_points()
        self.x_start, self.x_end = self._content_cols()

    def _content_rows(self):
        """Ink rows above/below the staff (chord names, bar numbers, rhythm marks)."""
        rows = (self.img > 0.5).sum(axis=1) > 2
        top = int(self.y_first)
        lim = max(0, int(self.y_first - 4.2 * self.sp))
        for y in range(int(self.y_first), lim - 1, -1):
            if rows[y]:
                top = y
        bot = int(self.y_last)
        lim = min(len(rows) - 1, int(self.y_last + 2.6 * self.sp))
        for y in range(int(self.y_last), lim + 1):
            if rows[y]:
                bot = y
        pad = int(self.sp * 0.3)
        return max(0, top - pad), min(len(rows), bot + pad + 1)

    def _content_cols(self):
        cols = np.nonzero((self.img[self.top:self.bottom] > 0.5).sum(axis=0) > 1)[0]
        return (int(cols[0]), int(cols[-1]) + 1) if len(cols) else (0, self.img.shape[1])

    def _cut_points(self):
        """Columns where it's safe to break: barlines first, else gaps between notes."""
        band = self.img[int(self.y_first):int(self.y_last) + 1] > 0.5
        full = band.mean(axis=0) > 0.9
        bars = _centres(full)
        if len(bars) >= 2:
            return bars, True
        # no barlines: a column is empty if nothing but the string lines crosses it
        lines = np.zeros(band.shape[0], bool)
        for y in np.linspace(0, band.shape[0] - 1, int(round((self.y_last - self.y_first) / self.sp)) + 1):
            lines[max(0, int(y) - 2):int(y) + 3] = True
        wide = self.img[max(0, int(self.y_first - self.sp)):int(self.y_last + self.sp)] > 0.5
        off = int(self.y_first) - max(0, int(self.y_first - self.sp))
        mask = np.ones(wide.shape[0], bool)
        mask[off:off + len(lines)] = ~lines
        empty = wide[mask].sum(axis=0) == 0
        # only keep the middle of runs at least ~half a string gap wide
        k = max(3, int(self.sp * 0.5))
        empty = np.convolve(empty, np.ones(k), "same") >= k
        return _centres(empty), False


def _centres(flags):
    out, x = [], 0
    while x < len(flags):
        if flags[x]:
            x0 = x
            while x < len(flags) and flags[x]:
                x += 1
            out.append((x0 + x - 1) // 2)
        x += 1
    return out


def flow(infos, row_px, gap):
    """Pack strips into rows of row_px (in normalised pixels). Returns [[Piece, ...], ...].

    Strips narrower than a row stay whole. Wider ones are broken at the cut point that
    fills the current row best.
    """
    rows, row, used = [], [], 0.0

    def close():
        nonlocal row, used
        if row:
            rows.append(row)
        row, used = [], 0.0

    for info in infos:
        scale = info.norm
        x = info.x_start
        cuts, _ = info.cuts
        while x < info.x_end:
            room = row_px - used - (gap if row else 0)
            rest = (info.x_end - x) * scale
            if rest <= room:
                row.append(Piece(info, x, info.x_end)); used += rest + (gap if len(row) > 1 else 0)
                break
            # cut at the furthest safe point that still fits
            fits = [c for c in cuts if x < c and (c - x) * scale <= room]
            if fits and (fits[-1] - x) * scale > row_px * 0.25:
                c = fits[-1]
                row.append(Piece(info, x, c)); x = c
                close()
            elif row:
                close()                                  # start the strip on a fresh row
            else:
                c = x + int(room / scale)                # nothing safe: hard cut
                row.append(Piece(info, x, c)); x = c
                close()
    close()
    return rows


def render_row(row, sp_px, gap, keep_color=False):
    """Paste a row of pieces into one image at a common string spacing."""
    gap = int(gap)
    parts, above, below = [], 0, 0
    for p in row:
        i = p.strip
        s = sp_px / i.sp
        src = (i.color[i.top:i.bottom, p.x0:p.x1] if keep_color and i.color is not None
               else (255 - i.img[i.top:i.bottom, p.x0:p.x1] * (255 - INK)).astype(np.uint8))
        im = cv2.resize(src, None, fx=s, fy=s, interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
        a = int(round((i.y_first - i.top) * s))
        parts.append((im, a))
        above, below = max(above, a), max(below, im.shape[0] - a)
    width = sum(im.shape[1] for im, _ in parts) + gap * (len(parts) - 1)
    out = np.full((above + below, width) + ((3,) if keep_color else ()), 255, np.uint8)
    x = 0
    for im, a in parts:
        out[above - a:above - a + im.shape[0], x:x + im.shape[1]] = im
        x += im.shape[1] + gap
    return out


def build_rows(strips, screen_w, per_row=2, keep_color=False):
    """strips: [(ink_img, color_img_or_None, sp)]. screen_w: width of the cropped box in
    source pixels. per_row: how many screen-widths of tab fit across one row."""
    infos = [StripInfo(img, col, sp) for img, col, sp in strips]
    sp_ref = float(np.median([i.sp for i in infos]))
    for i in infos:
        i.norm = sp_ref / i.sp
    # one "screen" = the widest single page after scaling, so per_row of them always fit
    unit = max(min(i.x_end - i.x_start, screen_w) * i.norm for i in infos)
    gap = unit * 0.03
    row_px = unit * per_row + gap * (per_row - 1)
    rows = flow(infos, row_px, gap)
    return [render_row(r, sp_ref, gap, keep_color) for r in rows], row_px


def write_pdf(path, rows, row_px, meta, paper="a4"):
    """rows: images from build_rows. meta: title, url, channel, duration."""
    W, H = PAPER[paper]
    margin = MARGIN
    content_w = W - 2 * margin
    pt = content_w / row_px                   # points per normalised pixel
    row_gap = ROW_GAP

    c = pdfcanvas.Canvas(path, pagesize=(W, H))
    c.setTitle(meta["title"])
    c.setAuthor(meta.get("channel", ""))
    c.setCreator("tabz")

    def footer(n):
        c.setFont("Plex", 7.5)
        c.setFillGray(0.55)
        c.drawString(margin, 22, meta["title"][:110])
        c.drawRightString(W - margin, 22, str(n))

    # header
    y = H - margin
    c.setFillGray(0.08)
    c.setFont("Plex-SemiBold", 17)
    for line in _wrap(meta["title"], "Plex-SemiBold", 17, content_w)[:2]:
        y -= 20
        c.drawString(margin, y, line)
    notes = meta.get("notes") or ""
    if notes:
        y -= 17
        c.setFont("Plex-Medium", 10.5)
        c.setFillGray(0.08)
        c.drawString(margin, y, notes[:120])
    y -= 15
    c.setFont("Plex", 9)
    c.setFillGray(0.4)
    bits = [b for b in (meta.get("channel"), _dur(meta.get("duration")),
                        datetime.date.today().strftime("%d %b %Y").lstrip("0")) if b]
    c.drawString(margin, y, "   ·   ".join(bits))
    y -= 14
    url = meta["url"]
    c.setFillColorRGB(0.72, 0.23, 0.13)
    c.setFont("PlexMono", 8.5)
    c.drawString(margin, y, url)
    c.linkURL(url, (margin, y - 3, margin + pdfmetrics.stringWidth(url, "PlexMono", 8.5), y + 9))
    y -= 12
    c.setStrokeGray(0.82)
    c.setLineWidth(0.6)
    c.line(margin, y, W - margin, y)
    y -= 16

    page = 1
    for im in rows:
        h = im.shape[0] * pt
        w = im.shape[1] * pt
        if y - h < margin + 10:
            footer(page)
            c.showPage()
            page += 1
            y = H - margin
        ok, png = cv2.imencode(".png", im)
        c.drawImage(ImageReader(_bytes(png)), margin, y - h, w, h)
        y -= h + row_gap
    footer(page)
    c.save()


def _bytes(arr):
    import io
    return io.BytesIO(arr.tobytes())


def _wrap(text, font, size, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if pdfmetrics.stringWidth(t, font, size) <= width or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    return lines + [cur] if cur else lines


def _dur(s):
    return f"{int(s) // 60}:{int(s) % 60:02d}" if s else ""
