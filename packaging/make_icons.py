"""Build the platform icons from aquila/assets/icon-512.png into build/icons/:
aquila.ico (Windows), aquila.icns (macOS), and the PNG sizes the Linux desktop
file and the MSIX manifest want. Needs Pillow only."""
import os
import sys

from PIL import Image

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "aquila", "assets", "icon-512.png")
OUT = os.path.join(ROOT, "build", "icons")


def main():
    os.makedirs(OUT, exist_ok=True)
    im = Image.open(SRC).convert("RGBA")
    im.save(os.path.join(OUT, "aquila.ico"),
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    im.save(os.path.join(OUT, "aquila.icns"))
    for s in (16, 24, 32, 44, 48, 50, 64, 71, 128, 150, 256, 310, 512):
        im.resize((s, s), Image.LANCZOS).save(os.path.join(OUT, f"aquila-{s}.png"))
    print("icons in", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
