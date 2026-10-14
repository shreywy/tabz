"""Regression check against real videos. Run: python tests/test_videos.py

Videos are cached in testdata/ (gitignored). Missing ones are downloaded first.
Each entry: id, expected strip count range, expected stitching (True = one long scroll).
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tabz.download import download  # noqa: E402
from tabz.vision import guess_region, scan  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "..", "testdata")

CASES = [
    # plain ASCII tab, whole box swaps every ~6s
    ("YM6Jg-wStU0", (35, 39), False),
    # Guitar Pro style, yellow bar highlight, playhead, slides sideways
    ("do4loFrrmf0", (1, 2), True),
    ("OgjldkwoaDQ", (1, 2), True),
    # dark custom skin, notes change colour when played, swaps, end screen
    ("WHZzYJ1-aM0", (18, 22), False),
    # --- other channels, picked for looks not in the set above ---
    # bass: notation + 4-string tab on yellow paper, faint lines, swaps
    ("Rm6Qm45Q-OI", (12, 16), False),
    # bass: notation + tab, Guitar Pro, crossfades between pages, repetitive riff
    ("l54Hs_AWn4k", (1, 2), True),
    # long thin strip with rhythm stems, continuous scroll
    ("WeIWkV2X2Cg", (1, 2), True),
    # dark panel, orange note highlight, swaps, end screen
    ("9Ikb8QhVev0", (6, 8), False),
    # ukulele: notation + 4-string tab at the top, swaps
    ("Fqf2xNEfbz0", (9, 11), False),
    # notation + tab with subtitles just above, swaps
    ("ap519CPKyys", (32, 37), False),
    # Guitar Pro style from another channel, scrolls with highlight
    ("Ou_Jv_hJpjU", (1, 2), True),
    # metal tab, scrolling, used to 403 on download
    ("29dm1hKIKUc", (1, 2), True),
]


def path_for(vid):
    for ext in ("mp4", "webm", "mkv"):
        p = os.path.join(DATA, f"{vid}.{ext}")
        if os.path.exists(p):
            return p
    return download(f"https://www.youtube.com/watch?v={vid}", dest=DATA)[0]


def main():
    failed = 0
    for vid, (lo, hi), scrolls in CASES:
        t = time.time()
        p = path_for(vid)
        region = guess_region(p)
        strips, _ = scan(p, region)
        widths = [s.sum.shape[1] for s in strips]
        stitched = max(widths) > region[2] * 3
        ok = lo <= len(strips) <= hi and stitched == scrolls
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {vid}  strips={len(strips)} (want {lo}-{hi})  "
              f"stitched={stitched}  {time.time() - t:.0f}s")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
