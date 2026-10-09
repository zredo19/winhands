"""Render PNG previews of the overlay (every banner state and palette, cursor variants) over dark and light desktops.

    python tools/overlay_preview.py OUT_DIR
"""
import pathlib, sys

import numpy as np
from PIL import Image

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import overlay

DARK, LIGHT = (24, 25, 33), (232, 234, 245)


def over(buf, w, h, bg):
    """Premultiplied BGRA bytes composited over a flat background -> RGB image."""
    p = np.frombuffer(buf, np.uint8).reshape(h, w, 4).astype(np.float32)
    rgb = p[..., 2::-1] + np.array(bg, np.float32) * (1 - p[..., 3:] / 255)
    return Image.fromarray(np.clip(np.rint(rgb), 0, 255).astype(np.uint8))


def sheet(tiles, cols, bg):
    w, h = tiles[0].size
    img = Image.new("RGB", (cols * w, -(-len(tiles) // cols) * h), bg)
    for i, t in enumerate(tiles):
        img.paste(t, (i % cols * w, i // cols * h))
    return img


def main(out):
    out = pathlib.Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for name, pal in overlay.PALETTES.items():
        for tone, bg in (("dark", DARK), ("light", LIGHT)):
            tiles = [over(overlay.frame(760, 460, 1.0, overlay.STATUS[s], pal, s), 760, 460, bg) for s in overlay.STATES]
            sheet(tiles, 2, bg).save(out / f"states_{name}_{tone}.png")
    for scale in (1.0, 2.0):
        for tone, bg in (("dark", DARK), ("light", LIGHT)):
            tiles = []
            for pal in overlay.PALETTES.values():
                for kw in ({}, {"motion": (31, 11)}, {"click": 0.05}, {"click": 0.1}):
                    w, h, buf, _ = overlay.cursor_frame(scale, pal, **kw)
                    tiles.append(over(buf, w, h, bg))
            sheet(tiles, 4, bg).save(out / f"cursor_{int(scale)}x_{tone}.png")
    print("wrote", sorted(p.name for p in out.iterdir()))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "overlay_preview")
