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


def redraw_staves(img, sp):
    """Draw every detected staff line solid. Some renderers draw 1px grey lines that come
    out faint or broken after cleaning; since we know where the lines are, re-ink them so
    every source prints equally crisp. Notation staves above the tab get the same."""
    m = cv2.morphologyEx(img, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (_odd(sp * 0.9), 1)))
    out = img.copy()
    thick = 1 if sp < 22 else 2
    for y0, y1, gsp, n in staff_groups(row_peaks(m.mean(axis=1), 0.25, sp * 0.35)):
        for y in np.linspace(y0, y1, n):
            y = int(round(y))
            row = m[max(0, y - 1):y + 2].max(axis=0) > 0.2
            # only along stretches where the line really runs, not across gaps between systems
            edges = np.flatnonzero(np.diff(np.r_[0, row.astype(np.int8), 0]))
            for a, b in zip(edges[::2], edges[1::2]):
                if b - a >= sp * 2:
                    band = out[max(0, y - thick // 2):y - thick // 2 + thick, a:b]
                    np.maximum(band, 0.92, out=band)
    return out


def despeckle(img, sp):
    """Drop small marks with nothing else near them: video noise or the edge of something
    behind the panel. Dots and strum marks sit close to notes or lines, so they stay."""
    m = (img > 0.35).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    if n < 2:
        return img
    w, h, area = stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT], stats[:, cv2.CC_STAT_AREA]
    small = (w < sp * 0.6) & (h < sp * 0.6) & (area < (sp * 0.35) ** 2)
    small[0] = False
    if not small.any():
        return img
    big = small.copy()
    big[:] = ~small
    big[0] = False
    near = cv2.dilate(big[lab].astype(np.uint8), np.ones((_odd(sp * 1.6), _odd(sp * 1.6)), np.uint8)) > 0
    kill = small[lab] & ~near
    if not kill.any():
        return img
    out = img.copy()
    out[cv2.dilate(kill.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0] = 0
    return out


class StripInfo:
    def __init__(self, img, color, sp_hint):
        img = despeckle(redraw_staves(img, sp_hint), sp_hint)
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
        # Walk outward from the staff and keep anything connected to it by gaps smaller
        # than a few string-gaps: chord names, bar numbers, a notation staff above,
        # rhythm stems below. A bigger empty gap means unrelated stuff, so stop there.
        rows = (self.img > 0.5).sum(axis=1) > 2
        top = int(self.y_first)
        for y in range(int(self.y_first), -1, -1):
            if rows[y]:
                top = y
            elif top - y > 3 * self.sp:
                break
        bot = int(self.y_last)
        for y in range(int(self.y_last), len(rows)):
            if rows[y]:
                bot = y
            elif y - bot > 2 * self.sp:
                break
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


MIN_GAP_PT = 3.6    # smallest string spacing "Auto" will print (about 1.3 mm)


def auto_per_row(sp, unit, content_w=A4[0] - 80):
    """As many screens per row as fit without the strings getting closer than MIN_GAP_PT."""
    return int(max(1, min(3, content_w * sp / (MIN_GAP_PT * unit))))


def build_rows(strips, screen_w, per_row="auto", keep_color=False):
    """strips: [(ink_img, color_img_or_None, sp)]. screen_w: width of the cropped box in
    source pixels. per_row: how many screen-widths of tab fit across one row."""
    infos = [StripInfo(img, col, sp) for img, col, sp in strips]
    sp_ref = float(np.median([i.sp for i in infos]))
    for i in infos:
        i.norm = sp_ref / i.sp
    # one "screen" = the widest single page after scaling, so per_row of them always fit
    unit = max(min(i.x_end - i.x_start, screen_w) * i.norm for i in infos)
    per_row = auto_per_row(sp_ref, unit) if per_row == "auto" else int(per_row)
    gap = unit * 0.03
    row_px = unit * per_row + gap * (per_row - 1)
    rows = flow(infos, row_px, gap)
    return [render_row(r, sp_ref, gap, keep_color) for r in rows], row_px


def label_bands(images, sp):
    """The rows above each staff where chord names and tempo marks live. Stops half a
    string-gap above the top line so string labels drawn on the lines stay out."""
    out = []
    for img in images:
        i = StripInfo(img, None, sp)
        out.append(i.img[i.top:max(i.top + 1, int(i.y_first - 0.5 * i.sp))])
    return out


def segments(strips, screen_w):
    """Units for the Play view, which reflows them to any width.

    Scrolled strips are cut into single bars so lines can break anywhere a bar ends.
    Swapped pages stay whole. Everything is scaled to one string spacing.
    Returns ([{"img", "staff_y", "group", "kind"}], unit_px, sp_ref).
    """
    infos = [StripInfo(img, None, sp) for img, sp in strips]
    sp_ref = float(np.median([i.sp for i in infos]))
    out = []
    for g, i in enumerate(infos):
        s = sp_ref / i.sp
        scrolled = (i.x_end - i.x_start) > screen_w * 1.3
        bounds = [i.x_start, i.x_end]
        if scrolled:
            cuts = [c for c in i.cuts[0] if i.x_start < c < i.x_end]
            bounds = [i.x_start]
            for c in cuts + [i.x_end]:
                if c - bounds[-1] < screen_w * 0.12 and c != i.x_end:
                    continue                      # too thin to stand alone: merge forward
                bounds.append(c)
        for x0, x1 in zip(bounds, bounds[1:]):
            src = (255 - i.img[i.top:i.bottom, x0:x1] * (255 - INK)).astype(np.uint8)
            im = cv2.resize(src, None, fx=s, fy=s, interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
            out.append({"img": im, "staff_y": round((i.y_first - i.top) * s, 1),
                        "staff_h": round((i.y_last - i.y_first) * s, 1),
                        "group": g, "kind": "bar" if scrolled else "page"})
    unit = max(min(i.x_end - i.x_start, screen_w) * sp_ref / i.sp for i in infos)
    return out, unit, sp_ref


def write_pdf(path, rows, row_px, meta, paper="a4", art=None):
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

    info = meta.get("info") or {}
    foot = " — ".join(b for b in (info.get("song"), info.get("artist")) if b) or meta["title"]

    def footer(n):
        c.setFont("Plex", 7.5)
        c.setFillGray(0.55)
        c.drawString(margin, 22, foot[:110])
        c.drawRightString(W - margin, 22, str(n))

    y = draw_header(c, meta, margin, H - margin, content_w, art)

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


ACCENT = (0.72, 0.23, 0.13)


def header_cells(info):
    """(label, value) pairs for the info row, only for what we know."""
    from .info import sounding_key
    cells = []
    if info.get("capo") is not None:
        cells.append(("Capo", "None" if info["capo"] == 0 else f"Fret {info['capo']}"))
    if info.get("tuning"):
        cells.append(("Tuning", info["tuning"]))
    if info.get("key"):
        snd = sounding_key(info["key"], info.get("capo"))
        cells.append(("Key (guess)" if info.get("key_guessed") else "Key",
                      f"{info['key']} shapes, sounds {snd}" if snd else info["key"]))
    if info.get("tempo"):
        cells.append(("Tempo", f"{info['tempo']} bpm"))
    return cells


def artist_line(info):
    """"yung kai" plus whatever else we know: "Arctic Monkeys · AM · 2013 · Alternative"."""
    return "   ·   ".join(b for b in (info.get("artist"), info.get("album"), info.get("year"), info.get("genre")) if b)


ART = 62    # cover art size in the header, points


def draw_header(c, meta, x, top, width, art=None):
    """Song, artist, info row, chords, source line. With c=None nothing is drawn and
    only the height is worked out (the review preview uses that to paginate)."""
    info = meta.get("info") or {}
    song = info.get("song") or meta["title"]
    artist = artist_line(info)
    y = top
    text_w = width - (ART + 16 if art else 0)
    if art and c is not None:
        try:
            import io
            c.drawImage(ImageReader(io.BytesIO(art)), x + width - ART, top - ART, ART, ART)
        except Exception:
            pass

    def text(font, size, gray, s, tx, ty, color=None):
        if c is None:
            return
        c.setFont(font, size)
        if color:
            c.setFillColorRGB(*color)
        else:
            c.setFillGray(gray)
        c.drawString(tx, ty, s)

    for line in _wrap(song, "Plex-SemiBold", 21, text_w)[:2]:
        y -= 24
        text("Plex-SemiBold", 21, 0.08, line, x, y)
    if artist:
        y -= 17
        text("Plex", 12, 0.38, artist, x, y)
    if art:
        y = min(y, top - ART + 4)       # keep the info row below the cover

    cells = header_cells(info)
    if cells:
        y -= 26
        cx = x
        for label, value in cells:
            w = max(pdfmetrics.stringWidth(label.upper(), "PlexMono", 6.8) + 0.6 * len(label),
                    pdfmetrics.stringWidth(value, "Plex-Medium", 10.5))
            if cx + w > x + width:
                break
            if c is not None:
                c.setFont("PlexMono", 6.8)
                c.setFillGray(0.5)
                c.drawString(cx, y + 13, label.upper(), charSpace=0.6)
            text("Plex-Medium", 10.5, 0.1, value, cx, y)
            cx += w + 24

    chords = info.get("chords") or []
    if chords:
        y -= 24
        if c is not None:
            c.setFont("PlexMono", 6.8)
            c.setFillGray(0.5)
            c.drawString(x, y + 13, "CHORDS", charSpace=0.6)
        cx = x
        for ch in chords:
            w = pdfmetrics.stringWidth(ch, "Plex-Medium", 10.5)
            if cx + w > x + width:
                break
            text("Plex-Medium", 10.5, 0.1, ch, cx, y)
            cx += w + 14

    y -= 22
    bits = [b for b in (f"Tab from {meta['channel']}" if meta.get("channel") else "",
                        _dur(meta.get("duration")),
                        datetime.date.today().strftime("%d %b %Y").lstrip("0")) if b]
    lead = "   ·   ".join(bits) + "   ·   "
    text("Plex", 8.5, 0.42, lead, x, y)
    url = meta["url"].replace("https://", "").replace("www.", "")
    ux = x + pdfmetrics.stringWidth(lead, "Plex", 8.5)
    text("PlexMono", 8.5, 0, url, ux, y, ACCENT)
    if c is not None:
        c.linkURL(meta["url"], (ux, y - 3, ux + pdfmetrics.stringWidth(url, "PlexMono", 8.5), y + 9))
    y -= 12
    if c is not None:
        c.setStrokeGray(0.82)
        c.setLineWidth(0.6)
        c.line(x, y, x + width, y)
    return y - 16


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
