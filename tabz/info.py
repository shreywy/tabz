"""Capo / tuning / key / tempo notes for the PDF header.

Sources: the video description, and Windows' built-in OCR on a handful of frames (title
cards, corner labels, vertical labels next to the staff). Only short facts are kept;
other on-screen text is never stored.
"""
import re

import cv2

from .vision import read_frame, video_duration

try:
    import winocr
    from PIL import Image
except ImportError:          # not on Windows, or OCR pack missing: description only
    winocr = None

CAPO = re.compile(r"\b(no\s+capo)\b|\bcapo\W{0,3}(?:on\s+)?(?:fret\s*)?(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
TUNING = re.compile(r"\btuning\W{0,3}([a-z#♭ ]{3,24}?)(?=\s*(?:capo|$|[|,;/(]))", re.I)
TUNING_BEFORE = re.compile(r"\b(standard|drop\s+[a-g]|open\s+[a-g]|dadgad|half\s+step\s+down)\s+tuning\b", re.I)
KEY = re.compile(r"\b[Kk][Ee][Yy]\W{1,3}([A-G][#b♭]?\s?(?:major|minor|maj|min|m)?)\b")
TEMPO = re.compile(r"(?:♩|bpm|tempo)\W{0,3}(\d{2,3})|\b(\d{2,3})\s*bpm\b", re.I)


def facts_from_text(lines):
    """Pull normalised facts out of free text. Returns a list in a fixed order."""
    capo = tuning = key = tempo = None
    for line in lines:
        for m in CAPO.finditer(line):
            if capo is None:
                capo = "No capo" if m.group(1) else f"Capo {int(m.group(2))}"
        if tuning is None:
            m = TUNING.search(line) or TUNING_BEFORE.search(line)
            if m:
                tuning = _tuning_name(m.group(1))
        if key is None and (m := KEY.search(line)):
            key = "Key of " + m.group(1).strip()
        if tempo is None and (m := TEMPO.search(line)):
            bpm = int(m.group(1) or m.group(2))
            if 40 <= bpm <= 260:
                tempo = f"{bpm} bpm"
    return [f for f in (capo, tuning, key, tempo) if f]


def _tuning_name(s):
    s = " ".join(s.split()).strip(" .:-")
    words = s.split()[:3]
    name = " ".join(w.capitalize() if w.isalpha() and len(w) > 2 else w for w in words)
    return name if name.lower().endswith("tuning") else f"{name} tuning"


def _ocr_lines(bgr):
    if winocr is None:
        return []
    out = []
    for img in (bgr, cv2.rotate(bgr, cv2.ROTATE_90_CLOCKWISE)):   # vertical labels too
        try:
            r = winocr.recognize_pil_sync(Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)), "en")
        except Exception:
            return out
        out += [ln["text"] for ln in r.get("lines", [])]
    return out


def song_info(video_path, description=""):
    """Facts like ["Capo 2", "Standard tuning"] from the description and a few frames."""
    lines = (description or "").splitlines()
    cap = cv2.VideoCapture(video_path)
    dur = video_duration(cap)
    for t in (0.3, 1.5, 4, 8, dur * 0.3, dur * 0.6):
        f = read_frame(cap, t)
        if f is not None:
            # on-screen text first: it's about this exact arrangement
            lines = _ocr_lines(f) + lines
    cap.release()
    return facts_from_text(lines)


if __name__ == "__main__":
    tests = {
        "CAPO: 2ND FRET LEGEND: X - slap": ["Capo 2"],
        "Capo : G7th capo performance 3 | https://x": [],
        "Tuning: Standard": ["Standard tuning"],
        "Capo. fret 2": ["Capo 2"],
        "CAPO FRET 5": ["Capo 5"],
        "Drop D tuning, capo on 3": ["Capo 3", "Drop D tuning"],
        "no capo needed": ["No capo"],
        "Key: Em  |  92 bpm": ["Key of Em", "92 bpm"],
    }
    for text, want in tests.items():
        got = facts_from_text([text])
        assert got == want, (text, got, want)
    print("info ok")
