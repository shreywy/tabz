"""Song details for the header: song, artist, capo, tuning, key, tempo, chords used.

Sources, in order of trust:
  - text on screen (Windows' built-in OCR on a few frames and on the label row above
    the staff in every strip)
  - the video description
  - the video title (song and artist)
Only these short facts are kept. Any other text OCR sees is thrown away.
"""
import re
from collections import Counter

import cv2
import numpy as np

from .vision import read_frame, video_duration

try:
    import winocr
    from PIL import Image
except ImportError:          # not on Windows, or OCR pack missing: description and title only
    winocr = None

CAPO = re.compile(r"\b(no\s+capo)\b|\bcapo\W{0,3}(?:on\s+)?(?:fret\s*)?(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
TUNING = re.compile(r"\btuning\W{0,3}([a-z#♭ ]{3,24}?)(?=\s*(?:capo|$|[|,;/(]))", re.I)
TUNING_BEFORE = re.compile(r"\b(standard|drop\s+[a-g]|open\s+[a-g]|dadgad|half\s+step\s+down)\s+tuning\b", re.I)
KEY = re.compile(r"\b[Kk][Ee][Yy]\W{1,3}([A-G][#b♭]?\s?(?:major|minor|maj|min|m)?)\b")
TEMPO = re.compile(r"(?:♩|bpm|tempo)\W{0,3}(\d{2,3})|\b(\d{2,3})\s*bpm\b", re.I)
# a tempo mark as OCR sees it on a score: "♩ = 97", "J = 97", "= 97"
TEMPO_MARK = re.compile(r"(?:^|\s)\W{0,2}[J♩dj]?\s*=\s*(\d{2,3})\b")
CHORD = re.compile(r"^[A-G](?:#|b|♯|♭)?(?:maj|min|m|M|dim|aug|sus|add|°|ø)?\d{0,2}"
                   r"(?:sus\d|add\d{1,2}|b5|#5|b9|#9)?(?:/[A-G](?:#|b|♯|♭)?)?$")

NOISE = re.compile(
    r"\b(fingerstyle|finger\s*style|acoustic|electric|classical|guitar|bass|ukulele|uke|cover|covers|"
    r"tutorial|lesson|how\s+to\s+play|easy|beginner|intermediate|advanced|tabs?|tablature|chords?|"
    r"lyrics|sheet\s+music|score|arrangement|instrumental|solo|free|on\s+screen|with|play\s*along|"
    r"playthrough|full|version\s+\d+|\+|&|official|video|hd|4k)\b", re.I)


# ---- text facts --------------------------------------------------------------

def facts_from_text(lines):
    """Capo, tuning, key, tempo from free text. Missing ones are None."""
    out = {"capo": None, "tuning": None, "key": None, "tempo": None}
    for line in lines:
        if out["capo"] is None:
            m = CAPO.search(line)
            if m:
                out["capo"] = 0 if m.group(1) else int(m.group(2))
        if out["tuning"] is None:
            m = TUNING.search(line) or TUNING_BEFORE.search(line)
            if m:
                out["tuning"] = _tuning_name(m.group(1))
        if out["key"] is None and (m := KEY.search(line)):
            out["key"] = _key_name(m.group(1))
        if out["tempo"] is None:
            m = TEMPO.search(line) or TEMPO_MARK.search(line.strip())
            if m:
                bpm = int(next(g for g in m.groups() if g))
                if 40 <= bpm <= 260:
                    out["tempo"] = bpm
    return out


def _tuning_name(s):
    s = " ".join(s.split()).strip(" .:-")
    words = [w for w in s.split()[:3] if w.lower() != "tuning"]
    return " ".join(w.capitalize() if w.isalpha() and len(w) > 2 else w for w in words) or None


def _key_name(s):
    s = s.replace(" ", "")
    s = re.sub(r"(?i)minor|min$", "m", s)
    s = re.sub(r"(?i)major|maj$", "", s)
    return s


# ---- title -> song / artist ----------------------------------------------------

def _tidy(p):
    p = NOISE.sub(" ", p)
    # brackets left holding a single word ("( in )", "(Simple )") were all noise
    p = re.sub(r"\(\s*([^()\s]*)\s*\)", lambda m: "" if len(m.group(1)) < 12 and " " not in m.group(1).strip() else m.group(0), p)
    return re.sub(r"\s{2,}", " ", p).strip(" -–—:,.")


def title_parts(title, channel=""):
    """The meaningful pieces of a video title, noise and the uploader's name removed."""
    chan = re.sub(r"\W+", "", channel or "").lower()
    parts = []
    for section in re.split(r"\s*[|｜/【】\[\]]\s*", title):
        for p in re.split(r"\s+[-–—]\s+", section):
            p = _tidy(p)
            if p and re.sub(r"\W+", "", p).lower() not in ("", chan):
                parts.append(p)
    return parts


def parse_title(title, channel=""):
    """Best guess at (song, artist) from a video title like
    "blue - yung kai | Fingerstyle Guitar | TAB + Chords". Order is a guess; lookup()
    settles it when it can."""
    parts = title_parts(title, channel)
    if not parts:
        return title.strip(), ""
    by = [re.match(r"(.+?)\s+by\s+(.+)$", p, re.I) for p in parts]
    by = next((m for m in by if m), None)
    if by:                                                       # "PASILYO by Sunkissed Lola"
        return by.group(1).strip(), by.group(2).strip()
    if len(parts) == 1:
        m = re.match(r"(.+?)\s*\(([^()]+)\)$", parts[0])      # "Love Story (Taylor Swift)"
        return (m.group(1), m.group(2)) if m else (parts[0], "")
    return " - ".join(parts[:-1]), parts[-1]


def _fold(s):
    """Lowercase without accents, so "Für Elise" matches "Fur Elise"."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c)).lower()


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", _fold(s))


def lookup(title, channel=""):
    """Song, artist, album, year, genre and cover art from Apple's public iTunes search.
    Tries each way round, since titles put the artist first or last. None if nothing
    matches or there's no connection."""
    import json
    import urllib.parse
    import urllib.request
    parts = title_parts(title, channel)
    song, artist = parse_title(title, channel)
    pairs = [(song, artist), (artist, song)]
    for p in parts:                        # "Kimi no Na wa - Kataware Doki - RADWIMPS"
        for q in parts:
            if p != q:
                pairs.append((p, q))
    pairs = [(a, b) for a, b in pairs if a and b]
    if not pairs:
        return None
    try:
        q = urllib.parse.urlencode({"term": f"{song} {artist}"[:120], "entity": "song", "limit": 25})
        with urllib.request.urlopen("https://itunes.apple.com/search?" + q, timeout=5) as r:
            results = json.load(r).get("results", [])
    except Exception:
        return None
    def words(s):
        return set(re.findall(r"[a-z0-9]+", _fold(s)))

    best, best_score = None, 0
    for s, a in pairs:
        ns, na = _norm(re.sub(r"\(.*?\)", "", s)), _norm(a)
        if not ns or not na:
            continue
        for x in results:
            name = x.get("trackName") or ""
            t, ar = _norm(re.sub(r"\(.*?\)", "", name)), _norm(x.get("artistName"))
            # artist: exact beats "Ludwig van Beethoven" beats "Beethoven's Wig"
            if ar == na:
                score = 3
            elif words(a) <= words(x.get("artistName")):
                score = 2
            else:
                continue
            if t == ns:
                score += 3
            elif t.startswith(ns) or ns.startswith(t):
                score += 1
            else:
                continue
            # the studio original over live takes, covers and special versions
            extra = name.lower()
            if re.search(r"\b(live|karaoke|instrumental|cover|tribute|remix|acoustic|demo)\b", extra):
                score -= 2
            if "(" in name and "(" not in s:
                score -= 1
            if score > best_score:
                best, best_score = x, score
    if best_score < 4:                   # a weak match is worse than the title's own guess
        return None
    art = best.get("artworkUrl100") or ""
    return {"song": best.get("trackName"), "artist": best.get("artistName"),
            "album": re.sub(r"\s*-\s*(Single|EP)$", "", best.get("collectionName") or "") or None,
            "year": (best.get("releaseDate") or "")[:4] or None,
            "genre": best.get("primaryGenreName"),
            "artwork": art.replace("100x100bb", "600x600bb") or None}


# ---- OCR ---------------------------------------------------------------------------

def _ocr(img):
    """Lines of words from a BGR or grey image. Empty when OCR isn't available."""
    if winocr is None:
        return []
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    try:
        r = winocr.recognize_pil_sync(Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)), "en")
    except Exception:
        return []
    return [[w["text"] for w in ln.get("words", [])] or [ln["text"]] for ln in r.get("lines", [])]


def _frame_lines(video_path):
    """OCR text lines from a few frames, upright and rotated (for vertical labels)."""
    cap = cv2.VideoCapture(video_path)
    dur = video_duration(cap)
    lines = []
    for t in (0.3, 1.5, 4, 8, dur * 0.3, dur * 0.6):
        f = read_frame(cap, t)
        if f is None:
            continue
        for img in (f, cv2.rotate(f, cv2.ROTATE_90_CLOCKWISE)):
            lines += [" ".join(words) for words in _ocr(img)]
    cap.release()
    return lines


def _labels(band, sp):
    """Text labels (chord names, tempo marks) in the band above a staff, left to right.
    Each is a binary crop."""
    m = (band > 0.45).astype(np.uint8)
    # merge the letters of one label, not neighbouring labels
    joined = cv2.dilate(m, np.ones((max(3, int(sp * 0.5)), max(3, int(sp * 0.25))), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(joined)
    out = []
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        if h < sp * 0.3 or h > sp * 2.5 or w > sp * 8:
            continue                     # chord names are text-sized; stems and beams aren't
        crop = m[y:y + h, x:x + w]
        if crop.sum() >= 6:
            out.append((x, crop))
    out.sort(key=lambda c: c[0])
    return [c for _, c in out]


def _context_line(crop):
    """One label after a printed word. Windows OCR often returns nothing for a lone
    letter like "A", but reads it fine as the second word of "Chord A"."""
    H, pad, gap = 40, 30, 28
    s = H / crop.shape[0]
    size = (max(1, round(crop.shape[1] * s)), H)
    c = cv2.resize(crop.astype(np.uint8) * 255, size, interpolation=cv2.INTER_CUBIC) > 127
    (tw, _), _ = cv2.getTextSize("Chord", cv2.FONT_HERSHEY_DUPLEX, 1.3, 2)
    img = np.full((H + 2 * pad, pad + tw + gap + c.shape[1] + pad), 255, np.uint8)
    cv2.putText(img, "Chord", (pad, pad + H), cv2.FONT_HERSHEY_DUPLEX, 1.3, 0, 2, cv2.LINE_AA)
    x = pad + tw + gap
    img[pad:pad + c.shape[0], x:x + c.shape[1]][c] = 0
    return img


def _shape_key(crop):
    """Labels drawn by the same renderer look identical, so a small resampled copy is
    enough to tell repeats apart."""
    return cv2.resize(crop.astype(np.float32), (48, 24), interpolation=cv2.INTER_AREA) > 0.35


def _same(a, b):
    """Overlap of the strokes themselves; white space doesn't count."""
    union = (a | b).sum()
    return union and (a & b).sum() / union > 0.72


def strip_text(bands, sp):
    """Chords (in order of first appearance, with counts) and any tempo mark found in
    the label rows above the staff. bands: ink images (0..1) of those rows.

    A song repeats the same few chord labels dozens of times, drawn identically. Labels
    are grouped by shape first, then one copy of each group is read, which is quicker
    and steadier than reading every label where it sits.
    """
    groups = []        # [crop, shape, aspect, count]
    for band in bands:
        for crop in _labels(band, sp):
            key, aspect = _shape_key(crop), crop.shape[1] / crop.shape[0]
            for g in groups:
                if abs(g[2] - aspect) < 0.12 * g[2] and _same(g[1], key):
                    g[3] += 1
                    break
            else:
                groups.append([crop, key, aspect, 1])

    chords = []
    tempo = None
    for crop, _, _, count in groups:
        words = [w for line in _ocr(_context_line(crop)) for w in line]
        if words and words[0].lower().startswith("chor"):
            words = words[1:]
        text = " ".join(words)
        m = TEMPO_MARK.search(text) or re.search(r"=\s*(\d{2,3})\b", text)
        if m and tempo is None and 40 <= int(m.group(1)) <= 260:
            tempo = int(m.group(1))
        toks = [w.strip("()[],.^vV*'\"").replace("♯", "#").replace("♭", "b") for w in words]
        toks = ["C" + t[1:] if t[:1] == "c" else t for t in toks]   # C is the one root OCR lowercases
        toks = [t for t in toks if CHORD.match(t)]
        if toks:
            chords.append((Counter(toks).most_common(1)[0][0], count))

    merged = Counter()
    order = []
    for c, n in chords:             # same chord, slightly different render: add up
        if c not in merged:
            order.append(c)
        merged[c] += n
    return [(c, merged[c]) for c in order], tempo


# ---- key from chords ------------------------------------------------------------------

NOTES = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5, "F#": 6, "Gb": 6,
         "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11}
SHARP = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]


def _chord_root(ch):
    m = re.match(r"([A-G][#b]?)(.*)", ch)
    root, rest = m.group(1), m.group(2)
    minor = rest.startswith("m") and not rest.startswith("maj")
    dim = "dim" in rest or "°" in rest
    return NOTES[root], "dim" if dim else "min" if minor else "maj"


def guess_key(chords):
    """Most likely key from [(chord, count)]: the major scale whose diatonic chords cover
    the most of the song. Reported as the relative minor if the song starts and ends
    there. None when nothing clearly wins."""
    if not chords:
        return None
    parsed = [(*_chord_root(c), n) for c, n in chords]
    scores = []
    for k in range(12):
        fit = {(k, "maj"), ((k + 2) % 12, "min"), ((k + 4) % 12, "min"), ((k + 5) % 12, "maj"),
               ((k + 7) % 12, "maj"), ((k + 9) % 12, "min"), ((k + 11) % 12, "dim")}
        s = sum(n for r, q, n in parsed if (r, q) in fit)
        s += 0.5 * sum(n for r, q, n in parsed if r == k and q == "maj")   # tonic weight
        scores.append(s)
    order = sorted(range(12), key=lambda k: -scores[k])
    total = sum(n for *_, n in parsed)
    if scores[order[0]] < total * 0.6 or scores[order[0]] - scores[order[1]] < total * 0.04:
        return None
    k = order[0]
    first = parsed[0][:2]
    if first == ((k + 9) % 12, "min"):
        return SHARP[(k + 9) % 12] + "m"
    return SHARP[k]


def sounding_key(key, capo):
    """The key you hear when playing `key` shapes with a capo."""
    if not key or not capo:
        return None
    m = re.match(r"([A-G][#b]?)(m?)", key)
    return SHARP[(NOTES[m.group(1)] + capo) % 12] + m.group(2)


# ---- all together ---------------------------------------------------------------------

def song_info(video_path, title, channel="", description="", gemini_key=None):
    """Everything we can get before scanning: song, artist, album, capo, tuning, key, tempo.

    Text on screen wins for playing details, since it describes this exact arrangement.
    With a Gemini key, the model reads the title and description too; without one the
    regex and lookup path below covers the same ground less flexibly.
    """
    song, artist = parse_title(title, channel)
    screen = facts_from_text(_frame_lines(video_path))
    desc = facts_from_text((description or "").splitlines())
    ai = gemini(gemini_key, title, channel, description) if gemini_key else {}
    if ai.get("song") and ai.get("artist"):
        song, artist = ai["song"], ai["artist"]
    found = lookup(f"{song} - {artist}" if artist else title, channel) or lookup(title, channel) or {}
    facts = {}
    for k in ("capo", "tuning", "key", "tempo"):
        facts[k] = next((v for v in (screen[k], ai.get(k), desc[k]) if v not in (None, "")), None)
    return {"song": found.get("song") or song, "artist": found.get("artist") or artist,
            "album": found.get("album"), "year": found.get("year"), "genre": found.get("genre"),
            "artwork": found.get("artwork"), **facts, "chords": [], "key_guessed": False,
            "arranger": ai.get("arranger") or channel or None}


GEMINI_MODEL = "gemini-flash-latest"
GEMINI_PROMPT = """You read YouTube guitar tab videos' titles and descriptions and pull out facts.
Return JSON with these keys, null when the text doesn't say:
  song      the song's real title, no "fingerstyle", "tab", "cover" or channel names
  artist    the original artist (not the person who made the tab)
  arranger  who made this arrangement or video, if stated
  capo      fret number as an integer, 0 for "no capo"
  tuning    short name like "Standard", "Drop D", "DADGAD", "Half step down"
  key       like "D" or "Em", only if stated
  tempo     beats per minute as an integer, only if stated
Only use what the text says. Do not guess."""


def gemini(key, title, channel, description):
    """Ask Gemini to read the title and description. {} on any failure: the app never
    depends on it."""
    import json as _json
    import urllib.request
    text = f"Title: {title}\nChannel: {channel}\nDescription:\n{(description or '')[:4000]}"
    body = {
        "systemInstruction": {"parts": [{"text": GEMINI_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
    }
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        data=_json.dumps(body).encode(), headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            out = _json.load(r)
        raw = out["candidates"][0]["content"]["parts"][0]["text"]
        d = _json.loads(raw)
    except Exception:
        return {}
    clean = {}
    for k in ("song", "artist", "arranger", "tuning", "key"):
        if isinstance(d.get(k), str) and d[k].strip():
            clean[k] = d[k].strip()[:120]
    for k, lo, hi in (("capo", 0, 12), ("tempo", 30, 300)):
        try:
            v = int(d.get(k))
            if lo <= v <= hi:
                clean[k] = v
        except (TypeError, ValueError):
            pass
    return clean


def file_name(meta):
    """"Song - Artist" when we know them, else the video title."""
    i = meta.get("info") or {}
    if i.get("song") and i.get("artist"):
        return f"{i['song']} - {i['artist']}"
    return i.get("song") or meta["title"]


def add_strip_info(info, bands, sp):
    """After scanning: chords used, a tempo mark from the score, and a key guess."""
    chords, tempo = strip_text(bands, sp)
    # drop one-off reads when the song clearly has a vocabulary (likely OCR noise)
    total = sum(n for _, n in chords)
    info["chords"] = [c for c, n in chords if n > 1 or total < 12]
    if len(info["chords"]) < 2:          # one stray read isn't a chord chart
        info["chords"] = []
        chords = []
    if info.get("tempo") is None and tempo:
        info["tempo"] = tempo
    if not info.get("key"):
        info["key"] = guess_key([(c, n) for c, n in chords if c in info["chords"]])
        info["key_guessed"] = bool(info["key"])
    return info


def summary(info):
    """One line for places that only have room for text: "Capo 2 · Standard tuning · 97 bpm"."""
    bits = []
    if info.get("capo") is not None:
        bits.append("No capo" if info["capo"] == 0 else f"Capo {info['capo']}")
    if info.get("tuning"):
        bits.append(f"{info['tuning']} tuning")
    if info.get("key"):
        bits.append(f"Key of {info['key']}")
    if info.get("tempo"):
        bits.append(f"{info['tempo']} bpm")
    return " · ".join(bits)


if __name__ == "__main__":
    t = facts_from_text
    assert t(["CAPO: 2ND FRET LEGEND: X - slap"])["capo"] == 2
    assert t(["Capo : G7th capo performance 3 | https://x"])["capo"] is None
    assert t(["Tuning: Standard"])["tuning"] == "Standard"
    assert t(["Capo. fret 2"])["capo"] == 2 and t(["CAPO FRET 5"])["capo"] == 5
    assert t(["Drop D tuning, capo on 3"]) == {"capo": 3, "tuning": "Drop D", "key": None, "tempo": None}
    assert t(["no capo needed"])["capo"] == 0
    assert t(["Key: Em  |  92 bpm"])["key"] == "Em" and t(["Key: Em  |  92 bpm"])["tempo"] == 92
    assert t(["J = 97"])["tempo"] == 97 and t(["= 97"])["tempo"] == 97

    p = parse_title
    assert p("blue - yung kai | Fingerstyle Guitar | TAB + Chords", "Kenneth Acoustic") == ("blue", "yung kai")
    assert p("Cupid (Twin Version) - FIFTY FIFTY | Fingerstyle Guitar | TAB + Chords + Lyrics") == ("Cupid (Twin Version)", "FIFTY FIFTY")
    assert p("pasilyo fingerstyle tabs - abz collado | sunkissed lola", "Abz Collado") == ("pasilyo", "sunkissed lola")
    assert p("Kimi no Na wa (Your name) - Kataware Doki - RADWIMPS - Fingerstyle Guitar Cover | TAB Tutorial")[1] == "RADWIMPS"
    assert p("Love Story (Taylor Swift) - Easy Beginner Ukulele Tab With Play Along") == ("Love Story", "Taylor Swift")
    assert p("Fur Elise - Beethoven (Simple Guitar Tab)", "Simple Guitar Tabs") == ("Fur Elise", "Beethoven")
    assert p("Arctic Monkeys - Do I Wanna Know (bass cover with tabs in video)") == ("Arctic Monkeys", "Do I Wanna Know")
    assert p("PASILYO by Sunkissed Lola | Fingerstyle Guitar Tab") == ("PASILYO", "Sunkissed Lola")

    g = guess_key
    assert g([("D", 24), ("Bm7", 24), ("A", 17), ("A6", 6), ("G", 5)]) == "D"
    assert g([("Em", 9), ("C", 4), ("G", 4), ("D", 3)]) == "Em"
    assert sounding_key("D", 2) == "E" and sounding_key("Em", 5) == "Am"
    print("info ok")
