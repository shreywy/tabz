"""Rasterise the logo to tabz/ui/icon.ico (same geometry as logo.svg)."""
from PIL import Image, ImageDraw

S = 1024
k = S / 64
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(13 * k), fill="#1c1b19")
for y in (16.5, 22.7, 28.9, 35.1, 41.3, 47.5):
    d.line([(10 * k, y * k), (54 * k, y * k)], fill="#615f5b", width=int(1.6 * k))
pts = [(19 * k, 16.5 * k), (45 * k, 16.5 * k), (19 * k, 47.5 * k), (45 * k, 47.5 * k)]
w = int(5.2 * k)
d.line(pts, fill="#e2643f", width=w, joint="curve")
for x, y in (pts[0], pts[-1]):
    d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill="#e2643f")
img.save("tabz/ui/icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.resize((256, 256), Image.LANCZOS).save("out/icon_preview.png")
