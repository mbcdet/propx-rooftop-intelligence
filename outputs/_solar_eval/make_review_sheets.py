"""Render the human review sheets: queue rows only, 9 per sheet, larger panels.

Each panel is headed with review_order, OBJECTID and address only. The assistant's
label, confidence and reason are deliberately NOT on the image, so the roof is
judged before the proposal is read; they live in labels.csv alone.
"""

import csv
import os

from PIL import Image, ImageDraw, ImageFont

EVAL = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(EVAL, "review")

COLS, ROWS = 3, 3
BOX = 880           # panel image box, square
HEAD = 62           # per-panel header strip
PAD = 22
TITLE = 74

FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_REG = "/System/Library/Fonts/Supplemental/Arial.ttf"

CROP_DIR = os.path.join(EVAL, "crops")
ZONE_PREFIX = {
    "Spengergasse, 1050": "spengergasse",
    "TU Wien / Karlsplatz, 1040": "tu_karlsplatz",
    "WU Wien campus, Welthandelsplatz 1, 1020": "wu_wien",
}


def load_queue():
    with open(os.path.join(EVAL, "labels.csv")) as fh:
        while True:
            pos = fh.tell()
            if not fh.readline().startswith("#"):
                fh.seek(pos)
                break
        return [r for r in csv.DictReader(fh) if r["in_review_queue"] == "yes"]


def main():
    os.makedirs(OUT, exist_ok=True)
    for stale in os.listdir(OUT):
        if stale.startswith("review_sheet_") and stale.endswith(".png"):
            os.remove(os.path.join(OUT, stale))

    queue = load_queue()
    per = COLS * ROWS
    sheets = [queue[i:i + per] for i in range(0, len(queue), per)]

    f_title = ImageFont.truetype(FONT_BOLD, 34)
    f_sub = ImageFont.truetype(FONT_REG, 24)
    f_head = ImageFont.truetype(FONT_BOLD, 27)
    f_addr = ImageFont.truetype(FONT_REG, 25)

    w = COLS * BOX + (COLS + 1) * PAD
    h = TITLE + ROWS * (BOX + HEAD) + (ROWS + 1) * PAD

    for n, sheet in enumerate(sheets, start=1):
        canvas = Image.new("RGB", (w, h), (24, 24, 26))
        d = ImageDraw.Draw(canvas)
        d.text(
            (PAD, 16),
            f"ROOFTOP PV REVIEW - sheet {n} of {len(sheets)}",
            font=f_title,
            fill=(255, 255, 255),
        )
        d.text(
            (PAD, 52),
            "Judge each roof from the image first, then record human_label in "
            "labels.csv. true / false / unclear. 'unclear' is always acceptable.",
            font=f_sub,
            fill=(170, 170, 175),
        )

        for k, row in enumerate(sheet):
            cx, cy = k % COLS, k // COLS
            x0 = PAD + cx * (BOX + PAD)
            y0 = TITLE + PAD + cy * (BOX + HEAD + PAD)

            d.rectangle([x0, y0, x0 + BOX, y0 + HEAD], fill=(48, 48, 54))
            d.text((x0 + 12, y0 + 6), f"#{row['review_order']}   OBJECTID {row['objectid']}",
                   font=f_head, fill=(255, 214, 82))
            d.text((x0 + 12, y0 + 33), row["adresse"], font=f_addr, fill=(225, 225, 230))

            stem = f"{ZONE_PREFIX[row['zone']]}_{row['objectid']}"
            im = Image.open(os.path.join(CROP_DIR, stem + ".png")).convert("RGB")
            scale = min(BOX / im.width, BOX / im.height)
            im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                           Image.LANCZOS)
            iy = y0 + HEAD
            canvas.paste(im, (x0 + (BOX - im.width) // 2, iy + (BOX - im.height) // 2))
            d.rectangle([x0, y0, x0 + BOX, iy + BOX], outline=(90, 90, 96), width=2)

        path = os.path.join(OUT, f"review_sheet_{n:02d}.png")
        canvas.save(path)
        print(path, canvas.size, f"{os.path.getsize(path)/1e6:.1f} MB", len(sheet), "panels")

    print(f"{len(queue)} queue rows over {len(sheets)} sheets")


if __name__ == "__main__":
    main()
