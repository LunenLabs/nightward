"""Assemble the nightward demo GIF from the REAL petshop run.

The terminal text is the real output of `examples/petshop` (tax rate 0.10 ->
0.095; the group lines are condensed to one line each), and the dashboard frame
is a real screenshot of the rendered site (`nightward view`, headless browser).
Nothing is invented — this only re-renders a real run into a shareable animation.

    python scripts/build_gif.py <dashboard_png> <out_gif> [x0,y0,x1,y1]

The optional box crops the screenshot to the diff cards. For a 1280x1400 headless
screenshot of the petshop dashboard: 224,852,1056,1316 (the billing card and the
start of the next group).
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1000, 620
BG = (12, 12, 14)
PANEL = (20, 20, 23)
PANEL_2 = (28, 28, 33)
BORDER = (46, 46, 53)
FG = (237, 237, 240)
MUTED = (155, 155, 163)
GREEN = (74, 222, 128)
RED = (248, 113, 113)
AMBER = (251, 191, 36)
BLUE = (96, 165, 250)

# cross-platform font discovery (Linux CI / macOS / Windows)
_FONT_DIRS = {
    "mono": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
             "/System/Library/Fonts/Menlo.ttc", "C:/Windows/Fonts/consola.ttf"],
    "mono_b": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
               "/System/Library/Fonts/Menlo.ttc", "C:/Windows/Fonts/consolab.ttf"],
    "sans": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
             "/System/Library/Fonts/Helvetica.ttc", "C:/Windows/Fonts/segoeui.ttf"],
    "sans_b": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
               "/System/Library/Fonts/Helvetica.ttc", "C:/Windows/Fonts/segoeuib.ttf"],
}


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_DIRS[kind]:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    raise SystemExit(f"no usable {kind} font found - install dejavu fonts")


MONO = font("mono", 22)
MONO_S = font("mono", 18)
MONO_B = font("mono_b", 24)
SANS = font("sans", 21)
SANS_B = font("sans_b", 28)


def base():
    img = Image.new("RGB", (W, H), BG)
    return img, ImageDraw.Draw(img)


def window(d, x, y, w, h, title):
    d.rounded_rectangle([x, y, x + w, y + h], radius=12, fill=PANEL, outline=BORDER, width=1)
    d.rounded_rectangle([x, y, x + w, y + 38], radius=12, fill=PANEL_2)
    d.rectangle([x, y + 26, x + w, y + 38], fill=PANEL_2)
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([x + 16 + i * 22, y + 14, x + 28 + i * 22, y + 26], fill=c)
    d.text((x + 90, y + 10), title, font=MONO_S, fill=MUTED)


def lines(d, x, y, items, lh=30, fnt=MONO):
    """items: list of (text, color) or (text, color, font)."""
    for it in items:
        text, color = it[0], it[1]
        f = it[2] if len(it) > 2 else fnt
        d.text((x, y), text, font=f, fill=color)
        y += lh
    return y


def title_bar(d, kicker, color=BLUE):
    d.text((40, 32), kicker, font=SANS_B, fill=color)
    d.line([40, 84, W - 40, 84], fill=BORDER, width=1)


frames: list[tuple[Image.Image, int]] = []


def add(img, ms):
    frames.append((img, ms))


# ---- Scene 1: the one-line "AI fix" ---------------------------------------
def scene_change():
    img, d = base()
    title_bar(d, "An AI agent “fixes” one line", BLUE)
    window(d, 60, 120, W - 120, 250, "shop.py")
    lines(d, 100, 178, [
        ("def checkout_total(items):", FG),
        ("    subtotal = round(sum(i['price'] * i['qty'] ...), 2)", MUTED),
        ("-   TAX_RATE = 0.10", RED),
        ("+   TAX_RATE = 0.095        # precise 9.5% rate", GREEN),
        ("    tax = round(subtotal * TAX_RATE, 2)", MUTED),
    ], lh=34)
    d.text((60, 410), "One line. Looks harmless.", font=SANS, fill=MUTED)
    return img


# ---- Scene 2: unit tests pass ---------------------------------------------
def scene_tests():
    img, d = base()
    title_bar(d, "Every unit test passes", GREEN)
    window(d, 60, 120, W - 120, 200, "terminal")
    lines(d, 100, 178, [
        ("$ pytest -q", FG),
        ("...                                          [100%]", MUTED),
        ("3 passed in 0.02s", GREEN, MONO_B),
    ], lh=36)
    d.text((60, 360), "So nobody notices.", font=SANS_B, fill=GREEN)
    d.text((60, 412), "Tests never asked whether the values moved.", font=SANS, fill=MUTED)
    return img


# ---- Scene 3: nightward catches it -----------------------------------------
def scene_nightward():
    img, d = base()
    title_bar(d, "nightward catches the silent cascade", RED)
    window(d, 60, 120, W - 120, 400, "terminal")
    lines(d, 100, 172, [
        ("$ nightward run .", FG),
        ("Boundary: breached (3 unapproved)", RED, MONO_B),
        ("unchanged=0 changed=3 new=0 removed=0", MUTED),
        ("group: billing      - [CHANGED] checkout_total", AMBER),
        ("group: fulfillment  - [CHANGED] shipping_fee", AMBER),
        ("group: loyalty      - [CHANGED] loyalty_points", AMBER),
        ("next: `nightward review` shows the diffs", MUTED),
        ("", FG),
        ("$ nightward gate        -> exit 1  (CI stops here)", RED),
    ], lh=33)
    return img


# ---- Scene 4: the real dashboard ------------------------------------------
def scene_dashboard(dash_png, band=None):
    img, d = base()
    title_bar(d, "The blast radius — what else moved", BLUE)
    shot = Image.open(dash_png).convert("RGB")
    # crop to the most meaningful band (the diff cards) and fit width
    shot = shot.crop(band or (0, 0, shot.width, min(shot.height, 800)))
    scale = (W - 120) / shot.width
    shot = shot.resize((int(shot.width * scale), int(shot.height * scale)))
    if shot.height > H - 130:
        shot = shot.crop((0, 0, shot.width, H - 130))
    img.paste(shot, (60, 110))
    d.rectangle([60, 110, 60 + shot.width, 110 + shot.height], outline=BORDER, width=1)
    return img


# ---- Scene 5: the agent can pull the gate, not approve it ------------------
def scene_agent():
    img, d = base()
    title_bar(d, "Your agent can run the gate. Opening it takes a human.", BLUE)
    window(d, 60, 120, W - 120, 175, "nightward mcp  (what the agent sees)")
    lines(d, 100, 172, [
        ("tools: nightward_run, nightward_status", GREEN),
        ("       approve / reject: not exposed - human CLI only", MUTED),
        ("boundary: \"breached\" -> keep fixing, or ask a human", AMBER),
    ], lh=34)
    window(d, 60, 315, W - 120, 205, "terminal  (the human)")
    lines(d, 100, 367, [
        ("$ nightward approve --all", FG),
        ("error: this capture is not reviewed yet", RED),
        ("$ nightward review        # read every diff", FG),
        ("$ nightward approve --all # only what you were shown", GREEN),
    ], lh=34)
    d.text((60, 545), "The approval is a baseline diff your code owners review.", font=SANS_B,
           fill=FG)
    return img


# ---- Scene 6: the impact punchline ----------------------------------------
def scene_impact():
    img, d = base()
    title_bar(d, "One line moved three customer-facing behaviors", AMBER)
    rows = [
        ("checkout_total", "total 30.03", "total 29.89", FG),
        ("loyalty_points", "silver / 3p", "bronze / 2p", RED),
        ("shipping_fee", "free shipping", "+$3.50", RED),
    ]
    y = 150
    d.text((70, y), "behavior", font=MONO_S, fill=MUTED)
    d.text((420, y), "before (approved)", font=MONO_S, fill=MUTED)
    d.text((730, y), "after", font=MONO_S, fill=MUTED)
    y += 40
    for name, before, after, ac in rows:
        d.text((70, y), name, font=MONO, fill=FG)
        d.text((420, y), before, font=MONO, fill=MUTED)
        d.text((690, y), "->", font=MONO, fill=MUTED)
        d.text((730, y), after, font=MONO_B, fill=ac)
        y += 46
    y += 24
    d.text((70, y), "pytest: 3 passed", font=MONO_B, fill=GREEN)
    d.text((470, y), "nightward: breached", font=MONO_B, fill=RED)
    y += 56
    d.text((70, y), "Green tests. Red gate. That is the point.", font=SANS_B, fill=BLUE)
    return img


def main():
    dash = sys.argv[1] if len(sys.argv) > 1 else "examples/petshop/dash.png"
    out = sys.argv[2] if len(sys.argv) > 2 else "nightward-demo.gif"
    # optional "x0,y0,x1,y1": the band of the screenshot to show (the diff cards)
    band = tuple(int(v) for v in sys.argv[3].split(",")) if len(sys.argv) > 3 else None

    add(scene_change(), 2200)
    add(scene_tests(), 1900)
    add(scene_nightward(), 2800)
    add(scene_dashboard(dash, band), 3000)
    add(scene_agent(), 3400)
    add(scene_impact(), 3200)

    imgs = [f[0] for f in frames]
    durs = [f[1] for f in frames]
    imgs[0].save(out, save_all=True, append_images=imgs[1:], duration=durs,
                 loop=0, optimize=True)
    print(f"GIF -> {out}  ({Path(out).stat().st_size // 1024} KB, {len(imgs)} scenes)")


if __name__ == "__main__":
    main()
