"""Find the tab strip in a video frame, clean it to black-on-white, and track it over time.

Nothing here knows about specific channels. Everything is driven by two generic facts:
tab is a group of 4-7 evenly spaced horizontal lines, and ink is thin marks that
contrast with whatever flat background sits behind them.
"""
import cv2
import numpy as np


def luminance(bgr):
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)


def _odd(n):
    n = max(3, int(round(n)))
    return n if n % 2 else n + 1


def thin_marks(gray, k):
    """Strength of thin dark-on-light and light-on-dark marks, each in 0..255."""
    kern = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    dark = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kern) - gray
    light = gray - cv2.morphologyEx(gray, cv2.MORPH_OPEN, kern)
    return dark, light


def horizontal_rows(mask, min_len_frac=0.12, bridge=15):
    """Fraction of each row covered by long horizontal runs in a binary mask."""
    h, w = mask.shape
    m = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (bridge, 1)))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, int(w * min_len_frac)), 1)))
    return m.sum(axis=1) / 255.0 / w, m


def line_centers(coverage, thresh):
    """Merge runs of rows above thresh into line centre positions."""
    on = coverage > thresh
    lines, y = [], 0
    while y < len(on):
        if on[y]:
            y0 = y
            while y < len(on) and on[y]:
                y += 1
            lines.append((y0 + y - 1) / 2.0)
        y += 1
    return lines


def row_peaks(profile, thresh, min_dist):
    """Strongest rows above thresh, at least min_dist apart. A string with lots of notes
    on it spreads ink into neighbouring rows; this keeps just its centre."""
    peaks = []
    for i in np.argsort(profile)[::-1]:
        if profile[i] < thresh:
            break
        if all(abs(i - p) >= min_dist for p in peaks):
            peaks.append(int(i))
    return sorted(float(p) for p in peaks)


def staff_groups(lines, min_n=4, max_n=7, tol=0.1, min_gap=5):
    """Find runs of 4..7 evenly spaced lines. Returns [(first, last, spacing, count)]."""
    groups, i = [], 0
    while i < len(lines) - 1:
        best = None
        for j in range(i + 1, len(lines)):
            n = j - i + 1
            gaps = np.diff(lines[i:j + 1])
            sp = gaps.mean()
            if sp < min_gap or gaps.max() > sp * (1 + tol) or gaps.min() < sp * (1 - tol):
                break
            if min_n <= n <= max_n:
                best = (lines[i], lines[j], float(sp), n)
        if best:
            groups.append(best)
            i += best[3]
        else:
            i += 1
    return groups


def find_staves(bgr):
    """Tab staves in a frame: [(y_first, y_last, spacing, n, x0, x1)]."""
    gray = luminance(bgr)
    dark, light = thin_marks(gray, 9)
    mask = ((np.maximum(dark, light) > 30) * 255).astype(np.uint8)
    cov, hm = horizontal_rows(mask)
    lines = line_centers(cov, 0.25)
    out = []
    for y0, y1, sp, n in staff_groups(lines):
        ext = [np.nonzero(hm[int(y)])[0] for y in np.linspace(y0, y1, n)]
        ext = [e for e in ext if len(e)]
        x0 = int(np.median([e.min() for e in ext]))
        x1 = int(np.median([e.max() for e in ext]))
        out.append((y0, y1, sp, n, x0, x1))
    return out


def read_frame(cap, t):
    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
    ok, f = cap.read()
    return f if ok else None


def video_duration(cap):
    return cap.get(cv2.CAP_PROP_FRAME_COUNT) / (cap.get(cv2.CAP_PROP_FPS) or 30)


def vote_staff(path, box=None, samples=24):
    """The staff that shows up in the most sampled frames: (y0, y1, spacing, n, x0, x1).
    With a box, coordinates are relative to it. None if no frame has tab lines."""
    cap = cv2.VideoCapture(path)
    dur = video_duration(cap)
    found = []
    for t in np.linspace(dur * 0.08, dur * 0.92, samples):
        f = read_frame(cap, t)
        if f is not None:
            if box:
                x, y, w, h = box
                f = f[y:y + h, x:x + w]
            found += find_staves(f)
    cap.release()
    if not found:
        return None

    def near(o, s):
        return abs(o[0] - s[0]) < s[2] / 2 and abs(o[2] - s[2]) < s[2] * 0.15

    # group detections of the same staff across frames
    clusters = []
    for s in sorted(found, key=lambda s: -sum(near(o, s) for o in found)):
        if any(near(s, c[0]) for c in clusters):
            continue
        same = [o for o in found if near(o, s)]
        med = lambda i: float(np.median([o[i] for o in same]))
        clusters.append(((med(0), med(1), med(2), int(round(med(3))), int(med(4)), int(med(5))), len(same)))

    # The anchor is the most persistent staff, but a 5-line staff is usually standard
    # notation sitting above the tab, so a close-enough tab staff wins over it.
    top = clusters[0][1]
    tabby = [c for c in clusters if c[0][3] != 5 and c[1] >= top * 0.6]
    anchor = tabby[0] if tabby else clusters[0]
    (y0, y1, sp, n, x0, x1), count = anchor

    # Some videos draw each page a little higher or lower. Positions of one staff overlap
    # each other (two staves on one screen never do), so the box takes in all of them;
    # otherwise the pages drawn highest lose their top string.
    for fy0, fy1, fsp, fn, fx0, fx1 in found:
        if fn == n and abs(fsp - sp) < sp * 0.15 and fy0 < y1 and fy1 > y0                 and min(x1, fx1) - max(x0, fx0) > 0.6 * min(x1 - x0, fx1 - fx0):
            y0, y1, x0, x1 = min(y0, fy0), max(y1, fy1), min(x0, fx0), max(x1, fx1)

    # Staves drawn right above or below as part of the same system (notation over tab,
    # or two tab lines at once) come along, so the box never cuts a score in half.
    grew = True
    while grew:
        grew = False
        for (cy0, cy1, csp, _, cx0, cx1), c in clusters:
            overlap = min(x1, cx1) - max(x0, cx0)
            if c < count * 0.5 or overlap < 0.6 * min(x1 - x0, cx1 - cx0) or (cy0 >= y0 and cy1 <= y1):
                continue
            if 0 < y0 - cy1 <= 5 * sp or 0 < cy0 - y1 <= 5 * sp:
                y0, y1 = min(y0, cy0), max(y1, cy1)
                x0, x1 = min(x0, cx0), max(x1, cx1)
                grew = True
    return y0, y1, sp, n, x0, x1


def guess_region(path):
    """Best guess at the tab box as (x, y, w, h), or None."""
    staff = vote_staff(path)
    if not staff:
        return None
    y0, y1, sp, _, x0, x1 = staff
    cap = cv2.VideoCapture(path)
    dur = video_duration(cap)
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    x, y, w, h = grow_box(luminance(_median_frame(path, dur)), x0, x1, y0, y1, sp)
    # The median frame shows the panel where it sits most of the time. When it moves
    # between pages the growth can stop short of the staff's highest or lowest position,
    # so always keep every string plus the digits written on it.
    top, bot = max(0, min(y, int(y0 - 0.75 * sp))), min(H, max(y + h, int(y1 + 0.75 * sp) + 1))
    return x, top, w, bot - top


def _median_frame(path, dur, n=9):
    """Per-pixel median of a few frames: the static overlay stays, the player mostly drops out."""
    cap = cv2.VideoCapture(path)
    frames = [read_frame(cap, t) for t in np.linspace(dur * 0.2, dur * 0.8, n)]
    cap.release()
    return np.median(np.stack([f for f in frames if f is not None]), axis=0).astype(np.uint8)


def grow_box(gray, x0, x1, y0, y1, sp):
    """Grow outward from the staff while rows/columns look like the staff's own background.

    Stops at panel edges, title bars, borders. Caps keep it from running off on plain videos.
    """
    H, W = gray.shape
    inner = gray[int(y0):int(y1), x0:x1]
    bg = np.median(inner)

    def plain(strip):
        return abs(np.median(strip) - bg) < 28

    # Step over the thickness of the outer staff line itself (its rows aren't "plain"),
    # then grow while rows look like the panel. Starting any further out can land past a
    # panel that ends tight against the staff, and drag in the video behind it.
    # Start half a string-gap outside the staff (clear of the outer line and its soft
    # edges). If that row is already off the panel (a panel that ends tight against the
    # staff), walk back in until it isn't, so the video behind never gets in.
    top = max(0, int(y0 - sp / 2))
    while top < y0 - 1 and not plain(gray[top, x0:x1]):
        top += 1
    while top > 0 and y0 - top < 4.2 * sp and plain(gray[top - 1, x0:x1]):
        top -= 1
    bot = min(H - 1, int(y1 + sp / 2))
    while bot > y1 + 1 and not plain(gray[bot, x0:x1]):
        bot -= 1
    while bot < H - 1 and bot - y1 < 2.6 * sp and plain(gray[bot + 1, x0:x1]):
        bot += 1
    left = x0
    while left > 0 and x0 - left < 3 * sp and plain(gray[top:bot, left - 1]):
        left -= 1
    right = x1
    while right < W - 1 and right - x1 < 1.5 * sp and plain(gray[top:bot, right + 1]):
        right += 1
    return (left, top, right - left + 1, bot - top + 1)


def on_panel(crop):
    """The crop with rows at its top and bottom edge that aren't the tab panel filled in
    from the panel's edge. The box is sized for every page, so when a video draws a page
    lower, the top of the box shows the video behind the panel."""
    g = luminance(crop)
    plain = np.nonzero(np.abs(np.median(g, axis=1) - np.median(g)) < 28)[0]
    if not len(plain) or (plain[0] == 0 and plain[-1] == len(g) - 1):
        return crop
    top, bot = plain[0], plain[-1]
    crop = crop.copy()
    crop[:top], crop[bot + 1:] = crop[top], crop[bot]
    return crop


def ink(bgr, sp):
    """Ink strength 0..1 for a cropped strip: thin marks against the local flat background.

    Highlight boxes, panels and gradients are wider than any stroke, so the morphological
    background swallows them; digits, lines and symbols survive in either polarity.
    Coloured notes count as ink because only their contrast matters, not their hue.
    """
    gray = luminance(bgr)
    k = _odd(sp * 0.45)
    kern = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    if np.median(gray) > 110:            # light paper, dark ink
        bg = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kern)
        r = (bg - gray) / np.maximum(bg, 40)
    else:                                # dark panel, light ink
        bg = cv2.morphologyEx(gray, cv2.MORPH_OPEN, kern)
        r = (gray - bg) / np.maximum(255 - bg, 40)
    return np.clip((r - 0.12) / 0.3, 0, 1).astype(np.float32)


def has_staff(ink_img, sp):
    """Is there a tab staff in this cleaned strip, at roughly the expected string spacing?

    Rows are smeared sideways first so busy bars (arpeggio marks, stacked chords) don't
    break the lines up. Position isn't fixed: some videos redraw each page slightly
    bigger or lower.
    """
    m = cv2.morphologyEx(ink_img, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (_odd(sp * 0.9), 1)))
    groups = staff_groups(row_peaks(m.mean(axis=1), 0.3, sp * 0.6))
    return any(abs(g[2] - sp) < sp * 0.2 for g in groups)


def staff_rows(ink_img, sp):
    """Staff groups found in a cleaned strip (same tuple shape as staff_groups).
    Dense bars chop string lines into short runs, so bridge gaps up to about a digit wide."""
    cov, _ = horizontal_rows(((ink_img > 0.5) * 255).astype(np.uint8), min_len_frac=0.06, bridge=_odd(sp * 0.9))
    return staff_groups(line_centers(cov, 0.3))


def notes_only(ink_img, sp):
    """Ink with long horizontal lines removed. Staff lines look the same at every
    horizontal offset, so they'd make every shift look like a match."""
    m = (ink_img * 255).astype(np.uint8)
    # bridge dash gaps first so ASCII "------" counts as a line too
    joined = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (_odd(sp * 0.4), 1)))
    lines = cv2.morphologyEx(joined, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (_odd(sp * 3), 1)))
    return cv2.subtract(m, cv2.dilate(lines, np.ones((3, 1), np.uint8))).astype(np.float32) / 255


class Strip:
    """A growing canvas. Each accepted frame adds its ink at an x offset; the final image
    keeps ink seen in most frames, so playheads and other moving marks drop out."""

    def __init__(self, img, crop, t):
        self.sum = img.copy()
        self.cnt = np.ones_like(img)
        self.color = crop.copy()        # latest raw pixels, for "keep original colours"
        self.x = 0
        self.t0 = self.t1 = t
        self.looks = []         # per-column brightness of recent frames, for played_again
        self.head = None        # furthest the playhead/highlight has got on this page, 0..1

    def played_again(self, crop):
        """A page played twice stays on screen unchanged; only the highlight or playhead
        jumps back to the start. Columns that differ from how this page usually looks show
        where it is. A big jump back to the left means the page is starting over."""
        if self.sum.shape[1] != crop.shape[1]:
            return False        # scrolled strip: the tab moves, not a playhead
        look = np.median(luminance(crop), axis=0)
        self.looks = self.looks[-40:] + [look]
        if len(self.looks) < 4:
            return False
        off = np.nonzero(np.abs(look - np.median(self.looks, axis=0)) > 20)[0]
        if len(off) < len(look) * 0.005:
            return False        # no highlight in sight
        pos = float(np.median(off)) / len(look)
        # against the furthest point reached: at 4 frames a second the jump back can be
        # caught halfway
        far = max(self.head, pos) if self.head is not None else pos
        self.head = pos if pos < far - 0.35 else far
        return pos < far - 0.35

    def add(self, img, crop, dx, t):
        self.x += dx
        h, w = img.shape
        need = self.x + w - self.sum.shape[1]
        if need > 0:
            pad = np.zeros((h, need), np.float32)
            self.sum = np.hstack([self.sum, pad]); self.cnt = np.hstack([self.cnt, pad])
            self.color = np.hstack([self.color, np.full((h, need, 3), 255, np.uint8)])
        self.sum[:, self.x:self.x + w] += img
        self.cnt[:, self.x:self.x + w] += 1
        self.color[:, self.x:self.x + w] = crop
        self.t1 = t

    def merge(self, other, offset):
        """Lay another strip onto this one starting at column `offset`."""
        h = self.sum.shape[0]
        need = offset + other.sum.shape[1] - self.sum.shape[1]
        if need > 0:
            pad = np.zeros((h, need), np.float32)
            self.sum = np.hstack([self.sum, pad]); self.cnt = np.hstack([self.cnt, pad])
            self.color = np.hstack([self.color, np.full((h, need, 3), 255, np.uint8)])
        w = other.sum.shape[1]
        self.sum[:, offset:offset + w] += other.sum
        self.cnt[:, offset:offset + w] += other.cnt
        self.color[:, offset:offset + w] = other.color
        self.t1 = other.t1

    def image(self):
        avg = self.sum / np.maximum(self.cnt, 1)
        return np.clip((avg - 0.22) / 0.3, 0, 1)


def match_shift(prev, cur, max_dy=4):
    """How far content moved left from prev to cur. Returns (dx, score); dx is None if no lock.

    Two windows: a wide one for small moves, a narrow one near the left edge so a jump of
    up to ~3/4 of the strip (a whole page of bars sliding in) still overlaps.
    """
    h, w = cur.shape
    cands = {}
    for a, b in ((0.12, 0.5), (0.03, 0.22)):
        a, b = int(w * a), int(w * b)
        tpl = cur[max_dy:h - max_dy, a:b]
        if tpl.sum() < 20:              # nothing distinctive to lock on to
            continue
        prof = cv2.matchTemplate(prev, tpl, cv2.TM_CCOEFF_NORMED).max(axis=0)
        # Repeating riffs give several near-equal peaks, and the highest isn't always
        # the right one, so keep the top few and let the full overlap decide.
        found = 0
        for px in np.argsort(prof)[::-1]:
            if prof[px] < 0.3 or found >= 4:
                break
            dx = int(px) - a
            if all(abs(dx - c) >= 6 for c in cands):   # skip the shoulders of a peak already taken
                cands[dx] = float(prof[px])
                found += 1
    if not cands:
        return None, 0.0
    # The window can also lock on to things that never change (string labels, a logo),
    # so the rest of the overlap has to agree too. "Didn't move" is always in the running
    # and wins near-ties: with a repeating riff, a shift of exactly one riff looks almost
    # as good as staying put, but after a real scroll staying put scores far lower.
    cands.setdefault(0, 1.0)
    scored = {dx: min(s, overlap_score(prev, cur, dx)) for dx, s in cands.items()}
    top = max(scored.values())
    near = [dx for dx, v in scored.items() if v >= top - 0.06]
    dx = min(near, key=abs)
    return dx, scored[dx]


def overlap_score(prev, cur, dx):
    w = cur.shape[1]
    if dx >= 0:
        a, b = prev[:, dx:], cur[:, :w - dx]
    else:
        a, b = prev[:, :w + dx], cur[:, -dx:]
    if a.shape[1] < w * 0.2:
        return 0.0
    a = cv2.GaussianBlur(a, (5, 5), 0).ravel(); b = cv2.GaussianBlur(b, (5, 5), 0).ravel()
    a = a - a.mean(); b = b - b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / d) if d > 0 else 0.0


def page_systems(page, target_sp=22.0):
    """Tab on a still page (a PDF page, a screenshot): one cropped BGR image per system,
    top to bottom, plus the string spacing and line count. A system is a tab staff and
    whatever is written over it, notation staff included, down to the rhythm stems below.

    The page is scaled so strings sit about target_sp apart, the same range video strips
    come in at, so the reader and its bar images don't end up enormous.
    Returns ([crop], sp, n) or ([], None, None)."""
    staves = find_staves(page)
    if not staves:
        return [], None, None
    counts = [s[3] for s in staves]
    n = 6 if 6 in counts else max(set(counts), key=counts.count)
    sp = float(np.median([s[2] for s in staves if s[3] == n]))
    main = [s for s in staves if s[3] == n and abs(s[2] - sp) < sp * 0.2]
    k = min(1.0, target_sp / sp)
    if k < 1:
        page = cv2.resize(page, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
        main = [(a * k, b * k, c * k, m, int(x0 * k), int(x1 * k)) for a, b, c, m, x0, x1 in main]
        staves = [(a * k, b * k, c * k, m, int(x0 * k), int(x1 * k)) for a, b, c, m, x0, x1 in staves]
        sp *= k
    H, W = page.shape[:2]
    # anything staff-like between one tab staff and the next sits on top of the next
    tops, lefts, rights = [], [], []
    for i, (y0, y1, _, _, x0, x1) in enumerate(main):
        prev = main[i - 1][1] if i else -1
        above = [o for o in staves if o not in main and prev < o[0] and o[1] < y0]
        tops.append(min([o[0] for o in above] + [y0]))
        lefts.append(min([o[4] for o in above] + [x0]))
        rights.append(max([o[5] for o in above] + [x1]))
    out = []
    for i, (y0, y1, _, _, x0, x1) in enumerate(main):
        lo = tops[i] - 4 * sp
        if i:
            lo = max(lo, (main[i - 1][1] + tops[i]) / 2)
        hi = y1 + 4 * sp
        if i + 1 < len(main):
            hi = min(hi, (y1 + tops[i + 1]) / 2)
        a, b = max(0, int(lo)), min(H, int(hi))
        l, r = max(0, int(lefts[i] - sp)), min(W, int(rights[i] + sp))
        out.append(page[a:b, l:r])
    return out, sp, n


# Measured on the test set: unchanged strips score ~0.99, swaps between similar-looking
# strips reach ~0.64, so "same" needs a high bar. Scrolls blur a little, so they get less.
SAME = 0.85
SCROLL = 0.7


def scan(path, region, fps=4.0, on_progress=None, on_strip=None):
    """Walk the video and return a list of Strip objects in playback order."""
    x, y, w, h = region
    cap = cv2.VideoCapture(path)
    vfps = cap.get(cv2.CAP_PROP_FPS) or 30
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    step = max(1, round(vfps / fps))

    staff = vote_staff(path, region)
    if not staff:
        return [], None
    sp = staff[2]

    strips, cur, ref, pending = [], None, None, None
    i = 0
    while True:
        if not cap.grab():
            break
        if i % step:
            i += 1
            continue
        ok, frame = cap.retrieve()
        t = i / vfps
        i += 1
        if not ok:
            continue
        if on_progress and i % (step * 8) == 1:
            on_progress(min(1.0, i / total))

        crop = on_panel(frame[y:y + h, x:x + w])
        img = ink(crop, sp)
        if not has_staff(img, sp):
            continue                     # intro, talking head, end screen
        small = cv2.resize(notes_only(img, sp), None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)

        if cur is not None:
            dx, score = match_shift(ref, small)
            # unchanged, or a small step of a smooth scroll: take it straight away
            same = dx is not None and abs(dx) <= 2 and score >= SAME
            if same and cur.played_again(crop):
                cur = Strip(img, crop, t)           # a repeat: the page goes in again
                strips.append(cur)
                if on_strip:
                    on_strip(len(strips))
                ref, pending = small, None
                continue
            if same or (dx is not None and 2 < dx < small.shape[1] * 0.25 and score >= SCROLL):
                cur.add(img, crop, 0 if dx <= 2 else dx * 2, t)   # back to full resolution
                ref, pending = small, None
                continue
        # Anything bigger (a page jump, a swap, a crossfade) waits until the picture
        # settles. Half-faded frames can each look like "moved by a page", and adding
        # every one of them would repeat bars.
        if pending is not None:
            dx, score = match_shift(pending[1], small)
            if dx is not None and score >= SAME and abs(dx) <= 2:
                if cur is not None:
                    jump, js = match_shift(ref, small)          # settled view vs last clean one
                    if jump is not None and jump > 2 and js >= SCROLL:
                        cur.add(img, crop, jump * 2, t)
                        ref, pending = small, None
                        continue
                cur = Strip(pending[0], pending[3], pending[2])
                cur.add(img, crop, 0, t)
                strips.append(cur)
                if on_strip:
                    on_strip(len(strips))
                ref, pending = small, None
                continue
        pending = (img, small, t, crop)
    cap.release()
    strips = rejoin(strips, sp, w)
    for s in strips:
        s.n = staff[3]          # string count, so layout can fit exactly that many lines
    if on_progress:
        on_progress(1.0)
    return strips, sp


def rejoin(strips, sp, screen_w):
    """Merge a strip into the one before it when it starts partway through it.

    Happens when a scrolling view jumps and the settled picture didn't line up with the
    last clean frame (fades, highlight boxes). Only partial overlaps merge: two identical
    pages in a row are a real repeat and stay separate.
    """
    out = []
    for s in strips:
        if out:
            a = out[-1]
            tail = a.image()[:, -screen_w:]
            head = s.image()[:, :screen_w]
            if tail.shape[1] == screen_w and head.shape[1] == screen_w:
                small = lambda im: cv2.resize(notes_only(im, sp), None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
                dx, score = match_shift(small(tail), small(head))
                # identical pages back to back are a repeat on swap-style videos, but at
                # the seam of a scrolled strip they're the same screen captured twice
                scrolled = max(a.sum.shape[1], s.sum.shape[1]) > screen_w * 1.3
                lo = -1 if scrolled else screen_w * 0.05
                if dx is not None and lo < dx * 2 < screen_w * 0.9 and score >= 0.75:
                    a.merge(s, a.sum.shape[1] - screen_w + dx * 2)
                    continue
        out.append(s)
    return out
