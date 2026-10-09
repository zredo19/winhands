import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np
from PIL import Image
from winhands.vision import (tokens, fit, Shot, montage_layout, montage, changed, color_blobs,
                    format_ocr, grid, marks)


def test_tokens_28px_patches():
    assert tokens(1920, 1080) == 2691 and tokens(1456, 819) == 1560 and tokens(28, 28) == 1


def test_fit_downscales_long_edge():
    assert fit(1920, 1080, 1280) == (1280, 720, 1.5)
    assert fit(800, 600, 1280) == (800, 600, 1.0)


def test_shot_maps_image_to_screen_with_scale_margin_and_origin():
    s = Shot(id=1, box=(-1920, 0, 0, 1080), scale=1.5, margin=22, size=(1302, 742))
    assert s.to_screen(22, 22) == (-1920, 0)                 # top-left of content
    assert s.to_screen(22 + 100, 22 + 10) == (-1770, 15)
    assert s.contains_screen(-1000, 500) and not s.contains_screen(10, 10)


def test_montage_layout_tiles_are_multiples_of_28():
    cols, (tw, th) = montage_layout(4, 1920, 1080)
    assert cols == 2 and tw % 28 == 0 and th % 28 == 0 and cols * tw <= 1344
    cols9, _ = montage_layout(9, 1920, 1080)
    assert cols9 == 3


def test_montage_image_size():
    frames = [Image.new("RGB", (1920, 1080), (i * 20, 0, 0)) for i in range(4)]
    m = montage(frames, [f"#{i}" for i in range(4)])
    cols, (tw, th) = montage_layout(4, 1920, 1080)
    assert m.size == (cols * tw, 2 * th)


def test_changed_counts_cells_and_bbox():
    a = np.zeros((1080, 1920, 3), np.uint8)
    b = a.copy()
    b[400:480, 800:960] = 255                                   # a "dialog" appears
    cells, bbox = changed(a, b)
    assert cells > 0 and bbox[0] <= 800 and bbox[2] >= 960 and bbox[1] <= 400 and bbox[3] >= 480
    assert changed(a, a.copy()) == (0, None)


def test_color_blobs_finds_red_target_centroid():
    arr = np.full((600, 800, 3), 255, np.uint8)
    yy, xx = np.ogrid[:600, :800]
    arr[(xx - 500) ** 2 + (yy - 300) ** 2 <= 28 ** 2] = (255, 0, 0)
    arr[10:14, 10:14] = (250, 5, 5)                             # tiny noise blob, below min_px
    blobs = color_blobs(arr, (255, 0, 0), tol=40, min_px=50)
    assert len(blobs) == 1 and abs(blobs[0][0] - 500) <= 1 and abs(blobs[0][1] - 300) <= 1


def test_format_ocr_lines_in_screen_coords():
    lines = [("Play Game", [("Play", (10, 20, 50, 40)), ("Game", (60, 20, 110, 40))])]
    out = format_ocr(lines, origin=(100, 200))
    assert out == [("Play Game", (110, 220, 210, 240))]


def test_grid_and_marks_render_without_error():
    img = Image.new("RGB", (400, 300), (240, 240, 240))
    g = grid(img, step=100, origin=(0, 0), scale=1.0)
    assert g.size == (422, 322)
    m = marks(img, [(1, (10, 10, 60, 40)), (2, (100, 100, 180, 140))])
    assert m.size == img.size


def test_region_capture_crops_covered_target_window(monkeypatch):
    from winhands import vision
    win = Image.new("RGB", (200, 200), (255, 0, 0))
    monkeypatch.setattr(vision, "cover", lambda region: 123)
    monkeypatch.setattr(vision, "grab_window", lambda h: (win, (100, 100, 300, 300)))
    monkeypatch.setattr(vision, "_screen", lambda region: (_ for _ in ()).throw(AssertionError("screen used")))
    img = vision.grab_img((150, 150, 160, 170))
    assert img.size == (10, 20) and img.getpixel((0, 0)) == (255, 0, 0)


def test_region_capture_uses_screen_when_target_is_visible(monkeypatch):
    from winhands import vision
    monkeypatch.setattr(vision, "cover", lambda region: None)
    monkeypatch.setattr(vision, "_screen", lambda region: Image.new("RGB", (5, 5), (0, 0, 255)))
    assert vision.grab_img((0, 0, 5, 5)).getpixel((2, 2)) == (0, 0, 255)


def test_pixel_font_prep_keeps_hard_edges():
    from winhands import vision
    img = Image.new("L", (2, 1))
    img.putpixel((1, 0), 255)
    big = vision.prep_ocr(img, 3, pixel=True)
    assert big.size == (6, 3) and set(big.tobytes()) == {0, 255}   # no blurred grey
    assert vision.fix_pixel_text("(—73.4 / –1.4)") == "(-73.4 / -1.4)"
