# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow>=11"]
# ///
"""Draws the app's icon, a cassette being cut with scissors:  uv run packaging/icon.py <out.icns | out.png | out.ico>

Everything is drawn four times too big and scaled down at the end, which is what smooths the edges.
Positions are in the 1024-point square that macOS icons are designed in.
"""
import math
import sys

from PIL import Image, ImageDraw, ImageFilter

S = 4
N = 1024 * S
INK, CREAM, SHADE = "#2b2622", "#f7f0e1", "#e3d8c2"
AMBER, RUST, TAPE = "#ffcf73", "#c9552a", "#6b4630"
STEEL, EDGE, GRIP = "#eef1f4", "#b9c0c9", "#0f4c5c"
SLANT, OPEN = 20, 15  # degrees: the cut line off upright, and each blade off the cut line
PIVOT = (716, 706)    # where the blades cross


def layer():
    image = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    return image, ImageDraw.Draw(image)


def pts(*xy):
    return [v * S for v in xy]


def disc(d, x, y, r, **style):
    d.ellipse(pts(x - r, y - r, x + r, y + r), **style)


def shadow(image, blur=14, drop=12, strength=0.38):
    """The same shape in soft black, a little lower, to sit underneath."""
    alpha = image.getchannel("A").point(lambda a: int(a * strength))
    soft = Image.new("RGBA", image.size, (0, 0, 0, 0))
    soft.putalpha(alpha.filter(ImageFilter.GaussianBlur(blur * S)))
    return soft.transform(soft.size, Image.Transform.AFFINE, (1, 0, 0, 0, 1, -drop * S))


def background():
    """The rounded square every macOS icon sits on, lit from above."""
    fade = Image.linear_gradient("L").resize((N, N))
    square = Image.composite(Image.new("RGBA", (N, N), "#d9662b"), Image.new("RGBA", (N, N), "#f7ae62"), fade)
    mask = Image.new("L", (N, N), 0)
    ImageDraw.Draw(mask).rounded_rectangle(pts(100, 100, 924, 924), radius=186 * S, fill=255)
    square.putalpha(mask)
    return square


def cassette():
    image, d = layer()
    d.rounded_rectangle(pts(182, 216, 842, 636), radius=42 * S, fill=CREAM, outline=INK, width=12 * S)
    # The label, with a band across the top to write the title on.
    d.rounded_rectangle(pts(222, 250, 802, 486), radius=24 * S, fill=AMBER)
    d.rounded_rectangle(pts(222, 250, 802, 336), radius=24 * S, fill=RUST)
    d.rectangle(pts(222, 310, 802, 346), fill=AMBER)
    d.line(pts(256, 280, 610, 280), fill=CREAM, width=9 * S)
    # The window, and in it the two reels: a full one and one nearly run out.
    d.rounded_rectangle(pts(332, 342, 692, 466), radius=30 * S, fill=INK)
    for x, wound in ((410, 55), (614, 40)):
        disc(d, x, 404, wound, fill=TAPE)
        disc(d, x, 404, 27, fill=CREAM)
        disc(d, x, 404, 11, fill=INK)
    # The foot, where the tape runs past the heads.
    d.polygon(pts(296, 630, 330, 520, 694, 520, 728, 630), fill=SHADE, outline=INK, width=10 * S)
    for x in (410, 512, 614):
        disc(d, x, 582, 13, fill=INK)
    for x, y in ((214, 248), (810, 248), (214, 604), (810, 604)):
        disc(d, x, y, 9, fill=SHADE, outline=INK, width=3 * S)
    return image


def cut_line():
    """Dashes along the line the scissors are following, from above the cassette down to the blades."""
    image, d = layer()
    lean = math.tan(math.radians(SLANT))
    at = lambda y: (PIVOT[0] + (y - PIVOT[1]) * lean, y)
    for width, colour in ((20, CREAM), (10, INK)):  # a pale edge keeps the dashes in sight where they cross something dark
        for y in range(150, PIVOT[1] - 60, 52):
            d.line(pts(*at(y), *at(y + 30)), fill=colour, width=width * S)
    return image


def half(side):
    """One half of the scissors standing upright: blade above the pivot, grip below. side is 1 or -1."""
    image, d = layer()
    x, y = PIVOT
    d.line(pts(x, y, x, y + 100), fill=GRIP, width=34 * S)
    d.ellipse(pts(x - 52, y + 80, x + 52, y + 196), outline=GRIP, width=26 * S)
    # A straight cutting edge on the side facing the other blade, a curved back on the other.
    blade = [(x + side * 15, y + 34), (x + side * 15, y - 262), (x - side * 9, y - 200),
             (x - side * 27, y - 116), (x - side * 34, y - 30), (x - side * 34, y + 34)]
    d.polygon([v * S for p in blade for v in p], fill=STEEL, outline=INK, width=7 * S)
    d.line(pts(x - side * 16, y - 10, x - side * 2, y - 196), fill=EDGE, width=7 * S)
    return image.rotate(side * OPEN, resample=Image.Resampling.BICUBIC, center=pts(x, y))


def scissors():
    image, d = layer()
    image.alpha_composite(half(1))
    image.alpha_composite(half(-1))
    disc(ImageDraw.Draw(image), *PIVOT, 15, fill=CREAM, outline=INK, width=6 * S)
    return image.rotate(SLANT, resample=Image.Resampling.BICUBIC, center=pts(*PIVOT))


def icon():
    image = background()
    for part in (cassette(), cut_line(), scissors()):
        under = shadow(part)
        under.putalpha(Image.composite(under.getchannel("A"), Image.new("L", (N, N), 0), image.getchannel("A")))  # no shadow off the square
        image.alpha_composite(under)
        image.alpha_composite(part)
    return image.resize((1024, 1024), Image.Resampling.LANCZOS)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    icon().save(sys.argv[1])
