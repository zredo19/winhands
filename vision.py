"""Pixel perception for UIs without an accessibility tree (games, canvases, custom-drawn apps).

Capture, shots (grid / Set-of-Marks / region zoom) with image->screen transforms, burst
montages, Windows OCR, template and color search, change/stability waits.
All coordinates in and out are physical screen px unless a function says otherwise.
"""
import ctypes, io, math, os, re, threading, time
from ctypes import wintypes as W
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

# private DLL instances with explicit 64-bit-safe signatures (other libs redefine windll's)
_u32, _gdi = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
try:
    _u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # physical px, same as UIA/mss
except Exception:
    pass
for _f, _a, _r in ((_u32.GetWindowRect, (W.HWND, ctypes.POINTER(W.RECT)), W.BOOL),
                   (_u32.GetWindowDC, (W.HWND,), W.HDC), (_u32.ReleaseDC, (W.HWND, W.HDC), ctypes.c_int),
                   (_u32.PrintWindow, (W.HWND, W.HDC, W.UINT), W.BOOL),
                   (_gdi.CreateCompatibleDC, (W.HDC,), W.HDC),
                   (_gdi.CreateCompatibleBitmap, (W.HDC, ctypes.c_int, ctypes.c_int), W.HBITMAP),
                   (_gdi.SelectObject, (W.HDC, W.HGDIOBJ), W.HGDIOBJ),
                   (_gdi.GetDIBits, (W.HDC, W.HBITMAP, W.UINT, W.UINT, ctypes.c_void_p, ctypes.c_void_p,
                                     W.UINT), ctypes.c_int),
                   (_gdi.DeleteObject, (W.HGDIOBJ,), W.BOOL), (_gdi.DeleteDC, (W.HDC,), W.BOOL)):
    _f.argtypes, _f.restype = _a, _r

FONTS = "C:/Windows/Fonts/"
HOME = os.path.join(os.path.expanduser("~"), ".winhands")
check = None      # set by the server: raises on kill switch / user interrupt
cover = None      # set by the server: region -> hwnd of a covered target window holding it, else None
pending = []      # (jpeg bytes, caption) queued by show() for the current run
SHOTS = {}        # shot id -> Shot (recent only)
_ids = iter(range(1, 10 ** 9))


# ---------- pure helpers ----------

def tokens(w, h):
    """Claude vision cost: one token per 28x28 patch."""
    return math.ceil(w / 28) * math.ceil(h / 28)


def fit(w, h, max_edge):
    """Downscale so the long edge <= max_edge -> (w2, h2, screen px per image px)."""
    s = max(w, h) / max_edge
    return (w, h, 1.0) if s <= 1 else (round(w / s), round(h / s), s)


@dataclass
class Shot:
    id: int
    box: tuple          # screen (x0, y0, x1, y1) captured
    scale: float        # screen px per image px
    margin: int         # ruler margin (grid) added at left/top of the image
    size: tuple         # image (w, h)

    def to_screen(self, x, y):
        return (round(self.box[0] + (x - self.margin) * self.scale),
                round(self.box[1] + (y - self.margin) * self.scale))

    def contains_screen(self, x, y):
        x0, y0, x1, y1 = self.box
        return x0 <= x < x1 and y0 <= y < y1


def montage_layout(n, fw, fh, width=1344):
    """Columns and tile size (multiples of 28 px: no patch waste) for n frames of fw x fh."""
    cols = 1 if n == 1 or fw / fh >= 3 else 2 if n <= 4 else 3 if n <= 9 else 4
    tw = max(28, (width // cols) // 28 * 28)
    th = max(28, round(tw * fh / fw / 28) * 28)
    return cols, (tw, th)


def _font(px, bold=True):
    try:
        return ImageFont.truetype(FONTS + ("segoeuib.ttf" if bold else "segoeui.ttf"), px)
    except OSError:
        return ImageFont.load_default()


def montage(frames, labels):
    cols, (tw, th) = montage_layout(len(frames), *frames[0].size)
    rows = math.ceil(len(frames) / cols)
    out = Image.new("RGB", (cols * tw, rows * th), (255, 255, 255))
    d, fnt = ImageDraw.Draw(out), _font(16)
    for k, (fr, lab) in enumerate(zip(frames, labels)):
        x, y = (k % cols) * tw, (k // cols) * th
        out.paste(fr.resize((tw, th), Image.Resampling.BOX), (x, y))
        tb = d.textbbox((x + 4, y + 3), lab, font=fnt)
        d.rectangle((tb[0] - 3, tb[1] - 2, tb[2] + 3, tb[3] + 2), fill=(0, 0, 0))
        d.text((x + 4, y + 3), lab, font=fnt, fill=(255, 255, 0))
        d.rectangle((x, y, x + tw - 1, y + th - 1), outline=(255, 255, 255), width=2)
    return out


def _sig(arr, k=8):
    return np.asarray(Image.fromarray(arr).convert("L").reduce(k), np.int16)


def changed(a, b, thr=12, k=8):
    """Changed k x k cells between two RGB frames -> (cells, bbox or None). Counting cells
    (not mean diff) ignores caret blinks (~3 cells) but catches hovers (~76) and dialogs."""
    d = np.abs(_sig(a, k) - _sig(b, k))
    ys, xs = np.nonzero(d > thr)
    if xs.size == 0:
        return 0, None
    return int(xs.size), (int(xs.min()) * k, int(ys.min()) * k, (int(xs.max()) + 1) * k, (int(ys.max()) + 1) * k)


def color_blobs(arr, rgb, tol=30, min_px=20):
    """Blobs of pixels within tol of rgb -> [(cx, cy, n_px)] largest first (array coords)."""
    mask = (np.abs(arr.astype(np.int16) - np.array(rgb, np.int16)) <= tol).all(axis=2)
    if not mask.any():
        return []
    try:
        import cv2
        n, _, stats, cents = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
        blobs = [(float(cents[i][0]), float(cents[i][1]), int(stats[i][4])) for i in range(1, n)]
    except ImportError:  # ponytail: single blob without cv2
        ys, xs = np.nonzero(mask)
        blobs = [(float(xs.mean()), float(ys.mean()), int(xs.size))]
    return sorted((b for b in blobs if b[2] >= min_px), key=lambda b: -b[2])


def format_ocr(lines, origin=(0, 0)):
    """[(line, [(word, box)])] in image coords -> [(line, union box)] in screen coords."""
    ox, oy = origin
    out = []
    for text, words in lines:
        if not words:
            continue
        xs0, ys0, xs1, ys1 = zip(*(b for _, b in words))
        out.append((text, (round(min(xs0) + ox), round(min(ys0) + oy),
                           round(max(xs1) + ox), round(max(ys1) + oy))))
    return out


def grid(img, step, origin=(0, 0), scale=1.0, margin=22, px=12):
    """Rulers in a margin (labels never cover content) + faint lines. Labels are SCREEN coords;
    scale = image px per screen px."""
    W, H = img.size
    out = Image.new("RGB", (W + margin, H + margin), (255, 255, 255))
    out.paste(img, (margin, margin))
    ov = Image.new("RGBA", out.size, (0, 0, 0, 0))
    d, fnt, small = ImageDraw.Draw(ov), _font(px), _font(px - 3)
    sx0, sy0 = origin
    for v in range(0, int(W / scale) + 1, step):
        x = margin + v * scale
        d.line((x, margin, x, margin + H), fill=(255, 0, 255, 90), width=1)
        d.text((x + 2, 4), str(sx0 + v), font=fnt, fill=(0, 0, 0, 255))
    for v in range(0, int(H / scale) + 1, step):
        y = margin + v * scale
        d.line((margin, y, margin + W, y), fill=(255, 0, 255, 90), width=1)
        d.text((1, y + 1), str(sy0 + v)[-4:], font=small, fill=(0, 0, 0, 255))
    return Image.alpha_composite(out.convert("RGBA"), ov).convert("RGB")


PALETTE = [(230, 25, 75), (60, 180, 75), (0, 130, 200), (245, 130, 48), (145, 30, 180), (70, 240, 240),
           (240, 50, 230), (210, 245, 60), (250, 190, 212), (0, 128, 128), (170, 110, 40), (128, 0, 0)]


def marks(img, items, px=14, stroke=2):
    """Set-of-Marks: draw labelled boxes AFTER downscaling. items = [(label, box img coords)]."""
    img = img.copy()
    d, fnt = ImageDraw.Draw(img), _font(px)
    W, H = img.size
    placed = []
    for n, (label, box) in enumerate(items):
        x0, y0, x1, y1 = box
        col = PALETTE[n % len(PALETTE)]
        d.rectangle((x0 - 2, y0 - 2, x1 + 2, y1 + 2), outline=col, width=stroke)
        tb = d.textbbox((0, 0), str(label), font=fnt)
        lw, lh = tb[2] - tb[0] + 6, tb[3] - tb[1] + 4
        r = (x0, y0, x0 + lw, y0 + lh)
        for lx, ly in ((x0, y0 - lh), (x0 - lw, y0), (x1, y0), (x1 - lw, y0 - lh), (x0, y1), (x0, y0)):
            c = (lx, ly, lx + lw, ly + lh)
            if c[0] >= 0 and c[1] >= 0 and c[2] <= W and c[3] <= H and not any(
                    c[0] < p[2] and p[0] < c[2] and c[1] < p[3] and p[1] < c[3] for p in placed):
                r = c
                break
        placed.append(r)
        d.rectangle(r, fill=col)
        lum = 0.299 * col[0] + 0.587 * col[1] + 0.114 * col[2]
        d.text((r[0] + 3 - tb[0], r[1] + 2 - tb[1]), str(label), font=fnt,
               fill=(0, 0, 0) if lum > 160 else (255, 255, 255))
    return img


def nice_step(span_screen):
    return next((s for s in (10, 20, 25, 50, 100, 200, 250, 500, 1000) if span_screen / s <= 12), 2000)


# ---------- capture ----------

_tls = threading.local()


def _sct():
    s = getattr(_tls, "sct", None)
    if s is None:
        import mss
        s = _tls.sct = (getattr(mss, "MSS", None) or mss.mss)()
    return s


def desktop():
    m = _sct().monitors[0]
    return m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"]


def _box(region):
    x0, y0, x1, y1 = map(round, region) if region else desktop()
    return {"left": x0, "top": y0, "width": max(1, x1 - x0), "height": max(1, y1 - y0)}


def _screen(region=None):
    raw = _sct().grab(_box(region))
    return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


def grab_img(region=None):
    """PIL RGB image of a screen region (x0, y0, x1, y1); None = whole virtual desktop.
    A region inside a covered target window is read from that window (PrintWindow), not the screen."""
    h = cover(region) if region and cover else None
    if not h:
        return _screen(region)
    img, wb = grab_window(h)
    x0, y0, x1, y1 = map(round, region)
    return img.crop((x0 - wb[0], y0 - wb[1], x1 - wb[0], y1 - wb[1]))


def grab(region=None):
    """numpy RGB array (H, W, 3) of a screen region."""
    return np.asarray(grab_img(region))


def grab_window(hwnd):
    """(PIL image, window rect) via PrintWindow(PW_RENDERFULLCONTENT): works while the window is
    covered and never changes focus. Black result (exclusive fullscreen, some GL/DX games) ->
    falls back to the on-screen pixels of the rect."""
    u, g = _u32, _gdi
    r = W.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    rect = (r.left, r.top, r.right, r.bottom)
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        raise ValueError(f"window {hwnd} has no area (minimized?)")
    wdc = u.GetWindowDC(hwnd)
    mdc = g.CreateCompatibleDC(wdc)
    bmp = g.CreateCompatibleBitmap(wdc, w, h)
    g.SelectObject(mdc, bmp)
    try:
        u.PrintWindow(hwnd, mdc, 2)  # PW_RENDERFULLCONTENT
        bi = (ctypes.c_uint32 * 10)(40, w, (-h) & 0xFFFFFFFF, 1 | (32 << 16), 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        g.GetDIBits(mdc, bmp, 0, h, buf, bi, 0)
        img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1).copy()
    finally:
        g.DeleteObject(bmp)
        g.DeleteDC(mdc)
        u.ReleaseDC(hwnd, wdc)
    if max(e[1] for e in img.getextrema()) < 8:
        img = _screen(rect)
    return img, rect


def capture(region=None, hwnd=None):
    """-> (PIL image, screen box). hwnd: window pixels (covered-safe); region: screen box
    (cropped from the window capture when both are given)."""
    if hwnd:
        img, wb = grab_window(hwnd)
        if not region:
            return img, wb
        x0, y0, x1, y1 = map(round, region)
        return img.crop((x0 - wb[0], y0 - wb[1], x1 - wb[0], y1 - wb[1])), (x0, y0, x1, y1)
    return grab_img(region), (tuple(map(round, region)) if region else desktop())


def pixel(x, y):
    return tuple(grab((x, y, x + 1, y + 1))[0, 0].tolist())


def _jpeg(img, quality=60):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def make_shot(region, max_edge=1280, with_grid=False, mark_items=None, quality=60, hwnd=None):
    """Capture region / window -> (Shot, jpeg). mark_items = [(label, screen box)] drawn as SoM."""
    img, box = capture(region, hwnd)
    w2, h2, s = fit(img.width, img.height, max_edge)
    if s > 1:
        img = img.resize((w2, h2), Image.Resampling.LANCZOS)
    if mark_items:
        to_img = lambda b: ((b[0] - box[0]) / s, (b[1] - box[1]) / s, (b[2] - box[0]) / s, (b[3] - box[1]) / s)
        img = marks(img, [(lab, to_img(b)) for lab, b in mark_items])
    margin = 0
    if with_grid:
        img = grid(img, nice_step(box[2] - box[0]), origin=box[:2], scale=1 / s)
        margin = 22
    shot = Shot(next(_ids), box, s, margin, img.size)
    SHOTS[shot.id] = shot
    for old in list(SHOTS)[:-50]:
        SHOTS.pop(old)
    return shot, _jpeg(img, quality)


def burst(region=None, frames=4, interval=0.25, crop_motion=True, hwnd=None):
    """Capture frames over time -> (montage image, caption). Crops to the moving area when motion
    is confined (much cheaper than full frames)."""
    imgs, stamps, t0 = [], [], time.monotonic()
    for i in range(frames):
        if check:
            check()
        img, box = capture(region, hwnd)
        imgs.append(img)
        stamps.append(round((time.monotonic() - t0) * 1000))
        if i < frames - 1:
            time.sleep(interval)
    note = f"burst {frames} frames every {interval}s of screen box {box}"
    if crop_motion and len(imgs) > 1:
        arrs = [np.asarray(im) for im in imgs]
        bbs = [b for a, c in zip(arrs, arrs[1:]) if (b := changed(a, c)[1])]
        if bbs:
            x0, y0 = min(b[0] for b in bbs), min(b[1] for b in bbs)
            x1, y1 = max(b[2] for b in bbs), max(b[3] for b in bbs)
            W, H = imgs[0].size
            x0, y0, x1, y1 = max(0, x0 - 24), max(0, y0 - 24), min(W, x1 + 24), min(H, y1 + 24)
            if (x1 - x0) * (y1 - y0) < 0.5 * W * H:
                imgs = [im.crop((x0, y0, x1, y1)) for im in imgs]
                note += f"; cropped to moving area screen {(box[0] + x0, box[1] + y0, box[0] + x1, box[1] + y1)}"
        else:
            note += "; no motion detected"
    return montage(imgs, [f"#{i + 1} t={t}ms" for i, t in enumerate(stamps)]), note


# ---------- OCR (Windows.Media.Ocr) ----------

_engines = {}


def _engine(lang=None):
    if lang not in _engines:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine
        e = (OcrEngine.try_create_from_language(Language(lang)) if lang
             else OcrEngine.try_create_from_user_profile_languages())
        if e is None:
            raise RuntimeError(f"OCR language pack not installed: {lang}")
        _engines[lang] = e
    return _engines[lang]


def prep_ocr(img, scale, pixel=False):
    """Grey + upscale for the engine. pixel=True (game HUD fonts): NEAREST keeps glyphs crisp, then
    binarize to dark text on white (the engine misreads thin pixel strokes on busy backgrounds)."""
    g = img.convert("L")
    if scale != 1:
        g = g.resize((round(g.width * scale), round(g.height * scale)),
                     Image.Resampling.NEAREST if pixel else Image.Resampling.LANCZOS)
    if pixel:
        a = np.asarray(g)
        lo, hi = np.percentile(a, (5, 95))
        light = a > (lo + hi) / 2
        text = light if light.mean() < 0.5 else ~light  # the minority class is the text
        g = Image.fromarray(np.where(text, 0, 255).astype(np.uint8))
    return g


def fix_pixel_text(t):
    """Pixel-font minus signs come back as dashes."""
    return re.sub(r"[—–−]", "-", t)


def ocr_image(img, scale=2.0, lang=None, pixel=False):
    """PIL image -> [(line, [(word, (x0, y0, x1, y1))])] in image coords. Polls the async op:
    .get() raises on STA threads (our UIA worker). One recognition at a time per engine."""
    from winrt.windows.foundation import AsyncStatus
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    g = prep_ocr(img, scale, pixel)
    pad = 24 if min(g.size) < 48 else 0  # engine returns nothing for inputs < 40 px on a side
    if pad:
        g = ImageOps.expand(g, pad, fill=g.getpixel((0, 0)))
    op = _engine(lang).recognize_async(
        SoftwareBitmap.create_copy_from_buffer(g.tobytes(), BitmapPixelFormat.GRAY8, g.width, g.height))
    while op.status == AsyncStatus.STARTED:
        time.sleep(0.002)
    res = op.get_results()
    fix = fix_pixel_text if pixel else (lambda t: t)
    return [(fix(ln.text), [(fix(w.text), (((r := w.bounding_rect).x - pad) / scale, (r.y - pad) / scale,
                                 (r.x + r.width - pad) / scale, (r.y + r.height - pad) / scale))
                       for w in ln.words]) for ln in res.lines]


def _ocr_raw(region=None, scale=None, lang=None, hwnd=None, pixel=False):
    img, box = capture(region, hwnd)
    if scale is None:
        scale = 2.0 if img.width * img.height <= 1_000_000 else 1.0
    return ocr_image(img, scale, lang, pixel), box[:2]


def ocr(region=None, scale=None, lang=None, hwnd=None, pixel=False):
    """Read text -> [(line, (x0, y0, x1, y1) screen)]. hwnd reads a (possibly covered) window.
    pixel=True for pixel fonts (game HUDs such as Minecraft F3).
    Isolated single characters (HUD digits) are not read: use locate() templates for those."""
    lines, origin = _ocr_raw(region, scale, lang, hwnd, pixel)
    return format_ocr(lines, origin)


def find_text(text, region=None, scale=None, hwnd=None, pixel=False):
    """Screen centre of the best match for text (exact word first, then substring), or None."""
    lines, (ox, oy) = _ocr_raw(region, scale, None, hwnd, pixel)
    t = text.lower()
    words = [(w, b) for _, ws in lines for w, b in ws]
    hit = next((b for w, b in words if w.lower() == t), None)
    if hit is None:
        for line, ws in lines:
            if t in line.lower():
                first = t.split()[0]
                span = [b for w, b in ws if first in w.lower()] or [b for _, b in ws]
                hit = span[0]
                break
    if hit is None:
        return None
    return round((hit[0] + hit[2]) / 2 + ox), round((hit[1] + hit[3]) / 2 + oy)


# ---------- template / color ----------

def _tpl_path(name):
    p = name if os.path.isabs(name) or os.path.exists(name) else os.path.join(HOME, "templates", name)
    return p if p.lower().endswith(".png") else p + ".png"


def save_template(name, region):
    os.makedirs(os.path.join(HOME, "templates"), exist_ok=True)
    p = _tpl_path(name)
    grab_img(region).save(p)
    return p


def locate(tpl, region=None, threshold=0.85):
    """Find a template (name in ~/.winhands/templates, path, PIL image or array) on screen ->
    (x, y, score) screen centre or None. Exact scale only (re-save templates if UI scale changes)."""
    import cv2
    if isinstance(tpl, str):
        t = np.asarray(Image.open(_tpl_path(tpl)).convert("L"))
    else:
        t = np.asarray((tpl if isinstance(tpl, Image.Image) else Image.fromarray(tpl)).convert("L"))
    hay = np.asarray(grab_img(region).convert("L"))
    if t.shape[0] > hay.shape[0] or t.shape[1] > hay.shape[1]:
        return None
    res = cv2.matchTemplate(hay, t, cv2.TM_CCOEFF_NORMED)
    _, score, _, (x, y) = cv2.minMaxLoc(res)
    if score < threshold:
        return None
    ox, oy = (tuple(map(round, region)) if region else desktop())[:2]
    return ox + x + t.shape[1] // 2, oy + y + t.shape[0] // 2, round(float(score), 3)


def find_color(rgb, tol=30, region=None, min_px=20):
    """Blobs of a color on screen -> [(x, y, n_px)] screen centres, largest first."""
    ox, oy = (tuple(map(round, region)) if region else desktop())[:2]
    return [(round(x + ox), round(y + oy), n) for x, y, n in color_blobs(grab(region), rgb, tol, min_px)]


# ---------- change waits ----------

def wait_change(region=None, timeout=5.0, min_cells=5, interval=0.05):
    """Block until the region visibly changes -> changed bbox (screen) or None on timeout."""
    base, end = grab(region), time.monotonic() + timeout
    ox, oy = (tuple(map(round, region)) if region else desktop())[:2]
    while time.monotonic() < end:
        if check:
            check()
        time.sleep(interval)
        cells, bb = changed(base, grab(region))
        if cells >= min_cells:
            return bb[0] + ox, bb[1] + oy, bb[2] + ox, bb[3] + oy
    return None


def wait_stable(region=None, timeout=5.0, interval=0.1, max_cells=4, n=3):
    """Block until n consecutive frames barely change (animations/loading done) -> bool."""
    prev, calm, end = grab(region), 0, time.monotonic() + timeout
    while time.monotonic() < end:
        if check:
            check()
        time.sleep(interval)
        cur = grab(region)
        calm = calm + 1 if changed(prev, cur)[0] <= max_cells else 0
        prev = cur
        if calm >= n:
            return True
    return False
