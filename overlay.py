"""Takeover overlay: edge glow + status banner on the monitor being controlled, plus an agent cursor.

Per-pixel-alpha layered windows (UpdateLayeredWindow): click-through (WS_EX_TRANSPARENT), topmost,
never activated (WS_EX_NOACTIVATE) and excluded from screen capture (WDA_EXCLUDEFROMCAPTURE), so they
never steal focus or appear in shots. show(hwnd) covers hwnd's monitor (the primary one when 0 or
closed). point(x, y) moves the agent cursor to where Claude acts, click() plays the click ripple.
Own thread and message loop; show()/hide()/point()/click()/set_state() are thread-safe. Failure to
start is non-fatal.
The look is the "Takeover Overlay V2" design. frame(), bloom_frame(), line_frame() and cursor_frame()
are pure renderers. Nothing animated is ever re-rendered full screen: the edge is two static bitmaps
(line, bloom) whose constant alpha breathes/fades, the banner is a cached base plus a small re-composited
dot, and the cursor is a halo window (alpha breathes) over an arrow window (cached frames). A ~30 fps
timer runs only while the overlay is visible. The timing curves are the pure functions below.
"""
import ctypes, functools, math, os, threading, time
from ctypes import wintypes as W

import numpy as np

LINE, CLIP = 2, 96                            # edge line width / bloom reach in px at 100% scaling
PALETTES = {  # (A light, B deep): the two stops of the edge line, bloom, banner dot and cursor
    "claude": ((255, 140, 60), (217, 119, 87)),       # orange
    "antigravity": ((96, 165, 250), (37, 99, 235)),   # blue
    "codex": ((205, 205, 212), (112, 112, 122)),      # gray
}
STATES = {"acting": 1.0, "thinking": 0.70, "paused": 0.40, "stopped": 0.0}  # edge intensity per state
STATUS = {"acting": "is controlling this PC", "thinking": "is thinking…", "paused": "is paused",
          "stopped": "stopped · you’re in control"}
KEYS = ("Ctrl", "Alt", "Q")                   # kill-switch chips; hidden once stopped
ANIM_DOT = ("acting", "thinking")             # states whose banner dot is animated (ping ring / breathing)
# CSS-like numbers of the design, 1x units; the arrow tip is the hotspot
HOT = (32, 26)
ARROW = [("M", 32, 26), ("L", 47, 33.2), ("C", 47.7, 33.5, 47.6, 34.5, 46.9, 34.7), ("L", 40.9, 36.3),
         ("L", 39.2, 42.3), ("C", 39, 43, 38, 43.1, 37.7, 42.4)]
WM_APP, WM_TIMER = 0x8000, 0x0113

def provider_for(client_name):
    """Palette key for an MCP client name (WINHANDS_PROVIDER overrides); unknown -> claude."""
    n = (os.environ.get("WINHANDS_PROVIDER") or client_name or "").lower()
    if n in PALETTES:
        return n
    if any(k in n for k in ("antigravity", "agy", "gemini")):
        return "antigravity"
    if any(k in n for k in ("codex", "openai", "chatgpt")):
        return "codex"
    return "claude"


_u = ctypes.WinDLL("user32", use_last_error=True)  # private instances: argtypes never clash
_g = ctypes.WinDLL("gdi32", use_last_error=True)
_shc = ctypes.WinDLL("shcore")


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT), ("dwFlags", W.DWORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD),
                ("biBitCount", W.WORD), ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
                ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD),
                ("biClrImportant", W.DWORD)]


_P = ctypes.POINTER
for _f, _res, _args in [  # typed: 64-bit handles and hwnds above 2**31 must not be truncated
    (_u.CreateWindowExW, W.HWND, (W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID)),
    (_u.SetWindowDisplayAffinity, W.BOOL, (W.HWND, W.DWORD)),
    (_u.PostMessageW, W.BOOL, (W.HWND, W.UINT, W.WPARAM, W.LPARAM)),
    (_u.GetMessageW, W.BOOL, (_P(W.MSG), W.HWND, W.UINT, W.UINT)),
    (_u.DispatchMessageW, W.LPARAM, (_P(W.MSG),)),
    (_u.ShowWindow, W.BOOL, (W.HWND, ctypes.c_int)),
    (_u.SetTimer, ctypes.c_size_t, (W.HWND, ctypes.c_size_t, W.UINT, W.LPVOID)),
    (_u.KillTimer, W.BOOL, (W.HWND, ctypes.c_size_t)),
    (_u.SetWindowPos, W.BOOL, (W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.UINT)),
    (_u.IsWindow, W.BOOL, (W.HWND,)),
    (_u.IsWindowVisible, W.BOOL, (W.HWND,)),
    (_u.GetCursorPos, W.BOOL, (_P(W.POINT),)),
    (_u.MonitorFromWindow, W.HMONITOR, (W.HWND, W.DWORD)),
    (_u.MonitorFromPoint, W.HMONITOR, (W.POINT, W.DWORD)),
    (_u.GetMonitorInfoW, W.BOOL, (W.HMONITOR, _P(MONITORINFO))),
    (_shc.GetDpiForMonitor, W.LONG, (W.HMONITOR, ctypes.c_int, _P(W.UINT), _P(W.UINT))),
    (_u.GetDC, W.HDC, (W.HWND,)),
    (_u.ReleaseDC, ctypes.c_int, (W.HWND, W.HDC)),
    (_g.CreateCompatibleDC, W.HDC, (W.HDC,)),
    (_g.CreateDIBSection, W.HBITMAP, (W.HDC, W.LPVOID, W.UINT, _P(W.LPVOID), W.HANDLE, W.DWORD)),
    (_g.SelectObject, W.HGDIOBJ, (W.HDC, W.HGDIOBJ)),
    (_g.DeleteObject, W.BOOL, (W.HGDIOBJ,)),
    (_g.DeleteDC, W.BOOL, (W.HDC,)),
    (_u.UpdateLayeredWindow, W.BOOL, (W.HWND, W.HDC, _P(W.POINT), _P(W.SIZE), W.HDC, _P(W.POINT),
                                      W.DWORD, W.LPVOID, W.DWORD)),
]:
    _f.restype, _f.argtypes = _res, _args


def monitor(hwnd=0):
    """((left, top, right, bottom), scale) of hwnd's monitor; the primary one when hwnd is 0 or gone."""
    hm = (_u.MonitorFromWindow(hwnd, 1) if hwnd and _u.IsWindow(hwnd)
          else _u.MonitorFromPoint(W.POINT(0, 0), 1))                    # MONITOR_DEFAULTTOPRIMARY
    mi = MONITORINFO(ctypes.sizeof(MONITORINFO))
    _u.GetMonitorInfoW(hm, ctypes.byref(mi))
    dpi = W.UINT(96)
    _shc.GetDpiForMonitor(hm, 0, ctypes.byref(dpi), ctypes.byref(W.UINT()))  # MDT_EFFECTIVE_DPI
    r = mi.rcMonitor
    return (r.left, r.top, r.right, r.bottom), dpi.value / 96


SS = 4                                        # supersampling of the banner and cursor shapes


@functools.lru_cache(maxsize=None)
def _font(weight, size):
    """Segoe UI (Variable when present) near the CSS weight; PIL's default as the last resort."""
    from PIL import ImageFont
    for name in ("seguisb.ttf", "segoeuib.ttf") if weight >= 600 else ("SegUIVar.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def _grad(size, box, c0, c1, diag=False):
    """RGBA canvas holding the gradient c0 -> c1 across box (x0, y0, x1, y1): CSS 180deg, CSS 135deg if
    diag is True, or SVG objectBoundingBox (0,0) -> (1,1) if diag is "bbox". Only the box (all columns
    for 180deg) is computed; the rest stays transparent, so paint it through a mask inside the box."""
    from PIL import Image
    x0, y0, x1, y1 = box
    ya, yb = max(int(y0) - 2, 0), min(int(y1) + 3, size[1])
    xa, xb = (max(int(x0) - 2, 0), min(int(x1) + 3, size[0])) if diag else (0, size[0])
    out = Image.new("RGBA", size)
    if yb <= ya or xb <= xa:
        return out
    X, Y = np.arange(xa, xb, dtype=np.float32)[None, :], np.arange(ya, yb, dtype=np.float32)[:, None]
    if diag == "bbox":
        t = ((X - x0) / max(x1 - x0, 1) + (Y - y0) / max(y1 - y0, 1)) / 2
    else:
        t = ((X - x0) + (Y - y0)) / max((x1 - x0) + (y1 - y0), 1) if diag else (Y - y0) / max(y1 - y0, 1)
    t = np.clip(t, 0, 1)[..., None]
    px = np.rint(np.array(c0, np.float32) * (1 - t) + np.array(c1, np.float32) * t).astype(np.uint8)
    out.paste(Image.fromarray(np.ascontiguousarray(np.broadcast_to(px, (yb - ya, xb - xa, 4))), "RGBA"), (xa, ya))
    return out


def _mask(size, draw):
    from PIL import Image, ImageDraw
    m = Image.new("L", size)
    draw(ImageDraw.Draw(m))
    return m


def _paint(img, mask, fill, a=1.0):
    """Alpha-composite fill (RGB tuple or RGBA image) onto img through an L mask scaled by a; only the
    mask's bounding box is touched."""
    from PIL import Image, ImageChops
    box = mask.getbbox()
    if box is None:
        return
    m = mask.crop(box)
    layer = fill.crop(box) if isinstance(fill, Image.Image) else Image.new("RGBA", m.size, (*fill, 255))
    layer.putalpha(ImageChops.multiply(layer.getchannel("A"), m.point(lambda v: round(v * a))))
    img.alpha_composite(layer, box[:2])


def _banner(scale, status, state, palette, animated=False):
    """(RGBA image, margin): the "winhands <status>" pill with its baked shadow, `margin` px in from every side.
    animated=True leaves the acting/thinking dot out: _dot_layer() draws it per frame."""
    from PIL import Image, ImageChops, ImageFilter
    A, B = palette
    u = SS * scale                                          # supersampled px per design px
    L = lambda v: v * u
    brand, stat = _font(600, round(L(13))), _font(400, round(L(13)))
    label, chip_f = _font(400, round(L(12))), _font(600, round(L(11)))
    keys = state != "stopped"                               # the kill-switch hint goes away once stopped
    chips = [max(L(18), chip_f.getlength(k) + L(10)) for k in KEYS]
    group = L(9) + label.getlength("Stop") + L(6) + sum(chips) + L(3) * len(chips)  # 3px gaps + right padding
    xb = L(34)                                              # padding 14 + dot box 10 + gap 10
    xs = xb + brand.getlength("winhands") + L(5)
    xd = xs + stat.getlength(status) + L(10)                # divider
    xg = xd + L(11)                                         # shortcut group
    end = xg + group + L(6) if keys else xd - L(10) + L(16)
    m, ph, pw = round(32 * scale), round(36 * scale), math.ceil(end / SS)
    size = ((pw + 2 * m) * SS, (ph + 2 * m) * SS)
    X0, Y0, X1, Y1 = m * SS, m * SS, (m + pw) * SS, (m + ph) * SS
    cy, r = (Y0 + Y1) / 2, (Y1 - Y0) / 2
    box = (X0, Y0, X1 - 1, Y1 - 1)
    img, white, ink = Image.new("RGBA", size), (255, 255, 255), (245, 245, 247)
    pill = _mask(size, lambda d: d.rounded_rectangle(box, r, fill=255))
    for dy, blur, a in ((8, 24, .28), (1, 2, .30)):         # box-shadows; sigma = blur / 2, never under the pill
        sh = _mask(size, lambda d: d.rounded_rectangle((X0, Y0 + L(dy), X1 - 1, Y1 - 1 + L(dy)), r, fill=255))
        _paint(img, ImageChops.subtract(sh.filter(ImageFilter.GaussianBlur(blur / 2 * u)), pill), (0, 0, 0), a)
    _paint(img, pill, _grad(size, (0, Y0, 0, Y1), (0x28, 0x28, 0x2E, 240), (0x18, 0x18, 0x1C, 240)))
    _paint(img, _mask(size, lambda d: d.rounded_rectangle(box, r, outline=255, width=round(u))), white, .09)
    _paint(img, ImageChops.subtract(pill, ImageChops.offset(pill, 0, round(u))), white, .07)  # inset 0 1px 0
    t = np.linspace(0, 1, max(int(X1 - X0 - 2 * L(22)), 2))
    band = np.zeros((size[1], size[0]), np.float32)         # 1px sheen at the top: 0 -> 1 -> 0 across the pill
    band[int(Y0):int(Y0 + u), int(X0 + L(22)):int(X0 + L(22)) + len(t)] = 1 - abs(2 * t - 1)
    _paint(img, Image.fromarray(np.rint(band * 255).astype(np.uint8), "L"), A if keys else white, .55 if keys else .14)

    def text(x, s, font, fill, a, anchor="ls"):             # baseline of a line box centred on the pill's midline
        asc, desc = font.getmetrics()
        _paint(img, _mask(size, lambda d: d.text((x, cy + (asc - desc) / 2), s, font=font, fill=255, anchor=anchor)), fill, a)

    dx, dr = X0 + L(14) + L(5), L(4)                        # status dot, in a 10x10 box
    if state == "paused":
        for i in (0, 1):
            bx = dx - L(3.75) + i * L(5)
            _paint(img, _mask(size, lambda d: d.rounded_rectangle((bx, cy - L(4.5), bx + L(2.5), cy + L(4.5)), L(1.25), fill=255)), A)
    elif not (animated and state in ANIM_DOT):
        if state in ANIM_DOT:                                # 2px halo at A 18% (ping ring / breathing: see _dot_layer)
            _paint(img, _mask(size, lambda d: d.ellipse((dx - L(6), cy - L(6), dx + L(6), cy + L(6)), fill=255)), A, .18)
        dot = (dx - dr, cy - dr, dx + dr, cy + dr)
        _paint(img, _mask(size, lambda d: d.ellipse(dot, fill=255)),
               (0x8B, 0x8B, 0x94) if state == "stopped" else _grad(size, dot, (*A, 255), (*B, 255), diag=True))
    text(X0 + xb, "winhands", brand, ink, 1)
    text(X0 + xs, status, stat, ink, .68)
    if keys:
        _paint(img, _mask(size, lambda d: d.rectangle((X0 + xd, cy - L(8), X0 + xd + L(1) - 1, cy + L(8) - 1), fill=255)), white, .10)
        gx, gh = X0 + xg, L(24)
        _paint(img, _mask(size, lambda d: d.rounded_rectangle((gx, cy - gh / 2, gx + group - 1, cy + gh / 2 - 1), gh / 2, fill=255)), white, .06)
        text(gx + L(9), "Stop", label, ink, .72)
        cx = gx + L(9) + label.getlength("Stop") + L(6)
        for k, cw in zip(KEYS, chips):
            chip = (cx, cy - L(9), cx + cw - 1, cy + L(9) - 1)
            _paint(img, _mask(size, lambda d: d.rounded_rectangle(chip, L(5), fill=255)),
                   _grad(size, (0, chip[1], 0, chip[3]), (*white, 36), (*white, 20)))
            _paint(img, _mask(size, lambda d: d.rounded_rectangle(chip, L(5), outline=255, width=round(u))), white, .10)
            cm = _mask(size, lambda d: d.rounded_rectangle(chip, L(5), fill=255))
            _paint(img, ImageChops.subtract(cm, ImageChops.offset(cm, 0, -round(u))), (0, 0, 0), .25)  # inset 0 -1px 0
            text(cx + cw / 2, k, chip_f, white, .90, "ms")
            cx += cw + L(3)
    return img.resize((pw + 2 * m, ph + 2 * m), Image.Resampling.BOX), m


def _dot_layer(scale, state, palette, t, m):
    """(RGBA image, (x, y)) of the acting/thinking status dot t seconds into its animation, to alpha-composite
    onto _banner(..., animated=True) (margin m) at (x, y); None for the static states. Acting: the dot with a
    filled ping ring growing behind it. Thinking: the dot and its 2px halo breathing as one."""
    from PIL import Image
    if state not in ANIM_DOT:
        return None
    A, B = palette
    n = math.ceil(30 * scale)                                # room for the 2.6x ping ring
    cx, cy = m + 19 * scale, m + round(36 * scale) / 2       # dot centre in banner px: padding 14 + half the 10px box
    x0, y0 = int(cx - n / 2), int(cy - n / 2)
    u, c = SS * scale, ((cx - x0) * SS, (cy - y0) * SS)
    size = (n * SS, n * SS)
    sc, al = (1.0, 1.0) if state == "acting" else dot_breath(t)
    disc = lambda r: _mask(size, lambda d: d.ellipse((c[0] - r * u, c[1] - r * u, c[0] + r * u, c[1] + r * u), fill=255))
    img = Image.new("RGBA", size)
    if state == "acting":
        ps, pa = ping(t)
        _paint(img, disc(4 * ps), A, pa)
    _paint(img, disc(6 * sc), A, .18 * al)
    dot = (c[0] - 4 * sc * u, c[1] - 4 * sc * u, c[0] + 4 * sc * u, c[1] + 4 * sc * u)
    _paint(img, disc(4 * sc), _grad(size, dot, (*A, 255), (*B, 255), diag=True), al)
    return img.resize((n, n), Image.Resampling.BOX), (x0, y0)


def _bloom_lut(scale, A, B):
    """(table, n): premultiplied colour (0..255) + alpha (0..1) of the bloom by px distance from the edge;
    row n (and any distance past it) is clear."""
    n = int(CLIP * scale)
    d = (np.arange(n) + .5) / scale
    a_a, a_b = .42 * np.exp(-d / 3.5), .22 * np.exp(-d / 12) + .10 * np.exp(-d / 30)
    tot = np.minimum(a_a + a_b, 1.0)
    lut = np.zeros((n + 1, 4), np.float32)
    lut[:n, :3] = (np.outer(a_a, A) + np.outer(a_b, B)) / np.maximum(a_a + a_b, 1e-9)[:, None] * tot[:, None]
    lut[:n, 3] = tot
    return lut, n


def bloom_frame(w, h, scale=1.0, palette=None):
    """Premultiplied BGRA of the edge bloom alone at full intensity (the layer whose alpha breathes); only
    the 96px band along the edges is computed."""
    lut, n = _bloom_lut(scale, *(palette or PALETTES["claude"]))
    a = np.rint(lut[:, 3] * 255)
    tab = np.stack([np.minimum(np.rint(lut[:, c]), a) for c in (2, 1, 0)] + [a], -1).astype(np.uint8)
    x, y = np.arange(w), np.arange(h)
    dx, dy = np.minimum(x, w - 1 - x), np.minimum(y, h - 1 - y)
    out = np.zeros((h, w, 4), np.uint8)
    r0 = min(n, (h + 1) // 2)
    r1 = max(h - n, r0)
    c0 = min(n, (w + 1) // 2)
    c1 = max(w - n, c0)
    out[:r0] = tab[np.minimum(dx[None, :], dy[:r0, None])]    # top and bottom bands (corners included)
    out[r1:] = tab[np.minimum(dx[None, :], dy[r1:, None])]
    out[r0:r1, :c0] = tab[dx[:c0]]                            # left and right bands: distance = column
    out[r0:r1, c1:] = tab[dx[c1:]]
    return out.tobytes()


def line_frame(w, h, scale=1.0, palette=None):
    """Premultiplied BGRA of the 2px edge line alone at full intensity: alpha .95, 135deg gradient A -> B."""
    A, B = (np.array(c, np.float32) for c in (palette or PALETTES["claude"]))
    out = np.zeros((h, w, 4), np.uint8)

    def fill(y0, y1, x0, x1):
        ys, xs = np.arange(y0, y1)[:, None], np.arange(x0, x1)[None, :]
        i = np.minimum(np.minimum(xs, w - 1 - xs), np.minimum(ys, h - 1 - ys))
        la = .95 * np.clip(LINE * scale - i, 0, 1)
        col = (A + (B - A) * ((xs + ys) / max(w + h - 2, 1))[..., None]) * la[..., None]
        a = np.rint(la * 255)
        out[y0:y1, x0:x1, 3] = a
        for c in range(3):
            out[y0:y1, x0:x1, c] = np.minimum(np.rint(col[..., 2 - c]), a)
    T = math.ceil(LINE * scale)
    r0 = min(T, (h + 1) // 2)
    r1 = max(h - T, r0)
    c0 = min(T, (w + 1) // 2)
    fill(0, r0, 0, w), fill(r1, h, 0, w), fill(r0, r1, 0, c0), fill(r0, r1, max(w - T, c0), w)
    return out.tobytes()


def frame(w, h, scale=1.0, text="", palette=None, state="acting"):
    """Premultiplied BGRA pixels: the edge glow of `state` and, when `text` is given, the status banner.
    (The overlay itself shows bloom_frame()/line_frame()/the banner as separate windows; this is the
    one-bitmap equivalent.)"""
    A, B = palette or PALETTES["claude"]
    k = STATES[state]
    lut, n = _bloom_lut(scale, A, B)                         # premultiplied colour (0..255) + alpha; last row = clipped
    x, y = np.arange(w), np.arange(h)
    i = np.minimum(np.minimum(x, w - 1 - x)[None, :], np.minimum(y, h - 1 - y)[:, None])
    bloom = lut[np.minimum(i, n)] * k
    la = (.95 * k * np.clip(LINE * scale - i, 0, 1))[..., None]                 # 2px line on top
    t = ((x[None, :] + y[:, None]) / max(w + h - 2, 1))[..., None]             # 135deg: A top-left -> B bottom-right
    out = np.empty((h, w, 4), np.float32)
    out[..., :3] = (np.array(A, np.float32) + (np.array(B, np.float32) - A) * t) * la + bloom[..., :3] * (1 - la)
    out[..., 3:] = la + bloom[..., 3:] * (1 - la)
    if text:
        img, m = _banner(scale, text, state, (A, B))
        px = np.asarray(img, np.float32)
        pa = px[..., 3:] / 255
        left, top = (w - (img.width - 2 * m)) // 2 - m, round(12 * scale) - m
        x0, y0, x1, y1 = max(left, 0), max(top, 0), min(left + img.width, w), min(top + img.height, h)
        if x1 > x0 and y1 > y0:                              # the shadow may poke out of the screen: clip it
            s, b = (slice(y0, y1), slice(x0, x1)), (slice(y0 - top, y1 - top), slice(x0 - left, x1 - left))
            out[s] = np.concatenate([px[b][..., :3] * pa[b], pa[b]], -1) + out[s] * (1 - pa[b])
    res = np.empty((h, w, 4), np.uint8)
    res[..., 3] = np.rint(out[..., 3] * 255)
    for c in range(3):                                       # BGRA; rounding must not push a colour above alpha
        res[..., c] = np.minimum(np.rint(out[..., 2 - c]), res[..., 3])
    return res.tobytes()


def _arrow(u, s=1.0):
    """ARROW flattened to a polygon in supersampled px (u per design px), scaled by s about the hotspot."""
    raw = []
    for cmd, *v in ARROW:
        if cmd == "C":
            p0, p1, p2, p3 = raw[-1], v[0:2], v[2:4], v[4:6]
            raw += [tuple((1 - t) ** 3 * a + 3 * (1 - t) ** 2 * t * b + 3 * (1 - t) * t ** 2 * c + t ** 3 * e
                          for a, b, c, e in zip(p0, p1, p2, p3)) for t in np.linspace(0, 1, 9)[1:]]
        else:
            raw.append(tuple(v))
    return [((HOT[0] + (x - HOT[0]) * s) * u, (HOT[1] + (y - HOT[1]) * s) * u) for x, y in raw]


TRAIL = math.hypot(31, 11)                    # length of the design's trail wedge


def _ease(t, x1=.2, y1=.7, x2=.2, y2=1.0):
    """CSS cubic-bezier(x1, y1, x2, y2) easing of t in 0..1 (bisection on the x curve)."""
    lo, hi = 0.0, 1.0
    for _ in range(30):
        s = (lo + hi) / 2
        lo, hi = (s, hi) if 3 * (1 - s) ** 2 * s * x1 + 3 * (1 - s) * s * s * x2 + s ** 3 < t else (lo, s)
    s = (lo + hi) / 2
    return 3 * (1 - s) ** 2 * s * y1 + 3 * (1 - s) * s * s * y2 + s ** 3


# ---------- animation timing (pure; the design's keyframes) ----------

CURVES = {"ease-in-out": (.42, 0, .58, 1), "ease-out": (0, 0, .58, 1), "breath": (.45, 0, .55, 1),
          "ping": (0, 0, .2, 1), "click": (.2, .7, .2, 1)}     # CSS cubic-bezier() control points by name
CLICK_CYCLE, CLICK_LIFE, TRAIL_HOLD = 1.8, .45, .12          # s: click keyframes, last visible instant, idle before the trail goes


def ease(t, curve="ease-in-out"):
    """Named CSS easing of t (clamped to 0..1; exactly 0 and 1 at the ends)."""
    return 0.0 if t <= 0 else 1.0 if t >= 1 else _ease(t, *CURVES[curve])


def alternate(t, period):
    """CSS `infinite alternate`: 0 -> 1 over `period` s, then back to 0, forever."""
    p = (t % (2 * period)) / period
    return p if p <= 1 else 2 - p


def breath(t):
    """Edge bloom multiplier at t s: .72 <-> 1 over 3.2s each way (the 2px line stays constant)."""
    return .72 + .28 * ease(alternate(t, 3.2), "breath")


def halo_breath(t):
    """Idle cursor halo opacity at t s: .55 <-> 1 over 3.2s each way."""
    return .55 + .45 * ease(alternate(t, 3.2))


def ping(t):
    """(scale, alpha) of the acting dot's filled ping ring: 1 -> 2.6 and .55 -> 0 every 1.6s."""
    e = ease((t % 1.6) / 1.6, "ping")
    return 1 + 1.6 * e, .55 * (1 - e)


def dot_breath(t):
    """(scale, alpha) of the thinking dot: .8 -> 1 and .45 -> 1 and back, 1.4s per cycle, each half eased."""
    e = ease(1 - abs(2 * ((t % 1.4) / 1.4) - 1))
    return .8 + .2 * e, .45 + .55 * e


def edge_levels(state, t):
    """(line, bloom) alpha multipliers of the edge for `state` t s after the overlay appeared: only the
    acting bloom breathes; thinking .70, paused .40 and stopped 0 are held."""
    k = STATES[state]
    return k, (k * breath(t) if state == "acting" else k)


def stop_phase(t):
    """(edge multiplier, banner alpha, banner rise in px, done) t s after stopping: the edge fades out in
    320ms, the banner holds 2s, then fades and rises 8px over 240ms."""
    p = ease((t - 2.0) / .24, "ease-out")
    return 1 - ease(t / .32, "ease-out"), 1 - p, 8 * p, t >= 2.24


def click_progress(elapsed):
    """Progress (0..1 of the 1.8s keyframes) of a click `elapsed` s old, None once it is over (450ms)."""
    return elapsed / CLICK_CYCLE if elapsed < CLICK_LIFE else None


def moving(now, last_move):
    """The cursor shows its trail until TRAIL_HOLD s after the last motion."""
    return last_move is not None and now - last_move < TRAIL_HOLD


def cursor_frame(scale=1.0, palette=None, motion=None, click=None, halo=1.0, arrow=True):
    """(w, h, premultiplied BGRA, hotspot) of the agent cursor: 64x64 at 1x, hotspot at the arrow tip.
    motion=(dx, dy) draws the moving look (trail behind the motion, r16 halo at 60%); click=0..1 is the
    progress of the 1.8s click cycle (ripple until .25, flash until .20, arrow press until .12). halo scales
    the radial halo (0 = none); arrow=False leaves the arrow out: the two idle layers whose halo breathes."""
    from PIL import Image
    A, B = palette or PALETTES["claude"]
    u, n = SS * scale, round(64 * scale)
    size = (n * SS, n * SS)
    hx, hy = HOT[0] * u, HOT[1] * u
    X, Y = np.meshgrid(np.arange(size[0]) + .5 - hx, np.arange(size[1]) + .5 - hy)
    img = Image.new("RGBA", size)
    L8 = lambda a: Image.fromarray(np.rint(a * 255).astype(np.uint8), "L")
    R, am = (16, .6) if motion else (20, 1.0)                # radial halo: A a.32 -> .12 at 45% -> 0
    _paint(img, L8(np.interp(np.hypot(X, Y) / u, [0, .45 * R, R], [.32, .12, 0]) * am * halo), A)
    if motion and any(motion):    # the design's wedge (0,15) (31,24.4) (31,27.6), turned onto the motion vector: A a 0 -> .5
        vx, vy = np.array(motion, float) / math.hypot(*motion)
        rx, ry = X / u + vx, Y / u + vy                      # from the wedge base, 1px behind the hotspot
        tt = 1 + (rx * vx + ry * vy) / TRAIL                 # 0 at the tail, 1 at the base
        _paint(img, L8(np.where((tt >= 0) & (tt <= 1) & (np.abs(-rx * vy + ry * vx) <= 1.6 * tt), .5 * tt, 0)), A)
    c = click
    if c is not None:        # c = progress of the 1.8s click cycle: ripple ring r22 + fill flash r8 + arrow press
        rr = 22 * (.2 + .8 * _ease(min(c / .25, 1))) * u
        _paint(img, _mask(size, lambda d: d.ellipse((hx - rr, hy - rr, hx + rr, hy + rr), outline=255, width=max(1, round(1.5 * u)))),
               A, np.interp(c, [0, .03, .25, 1], [0, .85, 0, 0]))
        _paint(img, _mask(size, lambda d: d.ellipse((hx - 8 * u, hy - 8 * u, hx + 8 * u, hy + 8 * u), fill=255)),
               A, np.interp(c, [0, .03, .20, 1], [0, .35, 0, 0]))
    if arrow:
        pts = _arrow(u, 1.0 if c is None else np.interp(c, [0, .03, .12, 1], [1, .9, 1, 1]))
        sh = [(x, y + 1.2 * u) for x, y in pts]
        _paint(img, _mask(size, lambda d: d.polygon(sh, fill=255)), (0, 0, 0), .28)
        _paint(img, _mask(size, lambda d: d.line(sh + sh[:1], fill=255, width=round(3 * u), joint="curve")), (0, 0, 0), .14)
        xs, ys = zip(*pts)
        _paint(img, _mask(size, lambda d: d.polygon(pts, fill=255)), _grad(size, (min(xs), min(ys), max(xs), max(ys)), (*A, 255), (*B, 255), diag="bbox"))
        _paint(img, _mask(size, lambda d: d.line(pts + pts[:1], fill=255, width=round(1.5 * u), joint="curve")), (250, 250, 251))
    return n, n,img.resize((n, n), Image.Resampling.BOX).tobytes("raw", "BGRa"), (round(HOT[0] * scale), round(HOT[1] * scale))


def _ulw(hwnd, x, y, w, h, buf, alpha=255):
    """Put premultiplied BGRA pixels on a layered window at screen (x, y), faded to a constant alpha 0..255."""
    bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32)  # top-down 32-bit
    bits = W.LPVOID()
    sdc = _u.GetDC(None)
    mdc = _g.CreateCompatibleDC(sdc)
    bmp = _g.CreateDIBSection(mdc, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
    try:
        ctypes.memmove(bits, buf, len(buf))
        old = _g.SelectObject(mdc, bmp)
        blend = (ctypes.c_ubyte * 4)(0, 0, alpha, 1)  # AC_SRC_OVER, constant alpha, AC_SRC_ALPHA
        ok = _u.UpdateLayeredWindow(hwnd, sdc, ctypes.byref(W.POINT(x, y)), ctypes.byref(W.SIZE(w, h)),
                                    mdc, ctypes.byref(W.POINT()), 0, blend, 2)  # ULW_ALPHA
        _g.SelectObject(mdc, old)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _g.DeleteObject(bmp)
        _g.DeleteDC(mdc)
        _u.ReleaseDC(None, sdc)


def _layered():
    """Hidden click-through, never-activated, topmost, capture-excluded popup (system Static class)."""
    # LAYERED | TRANSPARENT (click-through) | NOACTIVATE | TOOLWINDOW (no taskbar) | TOPMOST
    h = _u.CreateWindowExW(0x80000 | 0x20 | 0x8000000 | 0x80 | 0x8, "Static", None, 0x80000000,  # WS_POPUP
                           0, 0, 0, 0, None, None, None, None)
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    _u.SetWindowDisplayAffinity(h, 0x11)  # WDA_EXCLUDEFROMCAPTURE (Win10 2004+)
    return h


def _ulw_alpha(hwnd, alpha):
    """Change only the constant alpha of a layered window, its bitmap stays: the cheap fade. False if refused."""
    return bool(_u.UpdateLayeredWindow(hwnd, None, None, None, None, None, 0, (ctypes.c_ubyte * 4)(0, 0, alpha, 1), 2))


class _Layer:
    """One layered window plus its last bitmap: paint() uploads pixels, fade() only changes the constant alpha."""
    def __init__(self):
        self.h, self.box, self.buf, self.alpha, self.shown = _layered(), None, None, 255, False

    def paint(self, x, y, w, h, buf, alpha=255):
        _ulw(self.h, x, y, w, h, buf, alpha)
        self.box, self.buf, self.alpha = (x, y, w, h), buf, alpha

    def fade(self, a):
        a = max(0, min(255, round(a)))
        if self.buf is not None and a != self.alpha:
            if not _ulw_alpha(self.h, a):                    # refused: upload the cached bitmap with the new alpha
                _ulw(self.h, *self.box, self.buf, a)
            self.alpha = a

    def move(self, x, y, top=False):
        if self.box and (x, y) != self.box[:2]:
            _u.SetWindowPos(self.h, -1 if top else 0, x, y, 0, 0, 0x11 if top else 0x15)  # NOSIZE|NOACTIVATE (|NOZORDER)
            self.box = (x, y, *self.box[2:])

    def show(self):
        _u.ShowWindow(self.h, 4)                             # SW_SHOWNOACTIVATE
        _u.SetWindowPos(self.h, -1, 0, 0, 0, 0, 0x13)        # HWND_TOPMOST, NOSIZE|NOMOVE|NOACTIVATE: on top of the others
        self.shown = True

    def hide(self):
        if self.shown:
            _u.ShowWindow(self.h, 0)
            self.shown = False


ANGLES = [(math.cos(i * math.pi / 8), math.sin(i * math.pi / 8)) for i in range(16)]  # trail directions, 22.5deg apart
TICK = 33                                                                                # ms between animation frames


def cursor_mode_kwargs(mode):
    """cursor_frame() kwargs of an arrow-window mode: ("idle",) arrow only, ("move", angle index) with its
    trail, ("click", frame index) at that tick of the click cycle."""
    if mode[0] == "move":
        return {"motion": ANGLES[mode[1]]}
    if mode[0] == "click":
        return {"click": mode[1] * TICK / 1000 / CLICK_CYCLE}
    return {"halo": 0}


class Overlay:
    """The overlay windows, bottom to top: bloom, line, banner, cursor halo, cursor arrow. All the animation
    state below belongs to the overlay thread; the public methods only post messages."""
    def __init__(self, text=None, provider="claude", state="acting"):
        self.text, self.hwnd, self.error, self.painted = text, None, None, None  # text None -> STATUS[state]
        self.provider = provider if provider in PALETTES else "claude"
        self.state = state if state in STATES else "acting"
        self.cur, self.hot, self.pos, self.target, self.scale = None, (0, 0), None, 0, 1.0
        self.windows, self.layers, self.cache = (), {}, {}
        self._st, self._prov = self.state, self.provider        # what is applied (state/provider are set by any thread)
        self.visible, self.t_show, self.levels = False, 0.0, (0.0, 0.0)
        self.stop_t0, self.stop_from = None, (0.0, 0.0)          # stopped sequence: start time, edge levels it fades from
        self.t_click = self.t_move = self.last_xy = None
        self.angle, self.cmode, self.cpos, self.bkey, self.hkey = 0, None, (0, 0), None, None
        self.mon, self.mscale = (0, 0, 0, 0), 1.0
        self.ready = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="winhands-overlay").start()

    def _post(self, msg, w=0, l=0):
        if self.hwnd:
            _u.PostMessageW(self.hwnd, msg, w, l)

    def show(self, hwnd=0):
        """Cover hwnd's monitor (the primary one when 0 or closed)."""
        self._post(WM_APP, 1, hwnd or 0)

    def hide(self, linger=0.0):
        """Hide now, or after `linger` seconds unless show() comes first (the model is thinking)."""
        self._post(WM_APP, 0, round(linger * 1000))

    def point(self, x, y):
        """Show Claude's cursor at screen (x, y)."""
        self.pos = (x, y)
        self._post(WM_APP + 1)

    def click(self):
        """Play the cursor's click ripple (it lasts 450ms)."""
        self.t_click = time.monotonic()
        self._post(WM_APP + 3)

    def set_provider(self, provider):
        """Recolour border + cursor for an MCP client (claude orange, antigravity blue, codex gray)."""
        if provider in PALETTES and provider != self.provider:
            self.provider = provider
            self._post(WM_APP + 2)

    def set_state(self, state):
        """Switch the banner text and edge intensity: acting, thinking, paused or stopped (which plays its
        2.2s fade-out sequence and then hides the overlay)."""
        if state in STATES and state != self.state:
            self.state = state
            self._post(WM_APP + 2)

    # ---- overlay thread ----

    def _place(self):
        """Move onto the target's monitor; re-render the two full-screen bitmaps only when that monitor
        (or its scaling) or the palette changes. They are painted invisible: _tick() sets their alpha."""
        (x, y, x1, y1), scale = key = monitor(self.target)
        if key == self.painted:
            return
        w, h, pal = x1 - x, y1 - y, PALETTES[self.provider]
        self.layers["bloom"].paint(x, y, w, h, bloom_frame(w, h, scale, pal), 0)
        self.layers["line"].paint(x, y, w, h, line_frame(w, h, scale, pal), 0)
        self.painted, self.mon, self.mscale, self.bkey = key, (x, y, w, h), scale, None

    def _banner_base(self):
        """((key), (image, margin)): the cached banner of the current state, built on first use."""
        text = STATUS[self._st] if self.text is None else self.text
        key = ("banner", self.mscale, text, self._st, self.provider)
        if key not in self.cache:
            self.cache[key] = _banner(self.mscale, text, self._st, PALETTES[self.provider], animated=self._st in ANIM_DOT)
        return key, self.cache[key]

    def _draw_banner(self, t, alpha, rise):
        key, (img, m) = self._banner_base()
        x, y, w, h = self.mon
        bx = x + (w - (img.width - 2 * m)) // 2 - m
        by = y + round((12 - rise) * self.mscale) - m
        ban = self.layers["banner"]
        if self._st in ANIM_DOT:                             # the dot moves every frame: composite it onto the base
            layer, off = _dot_layer(self.mscale, self._st, PALETTES[self.provider], t, m)
            comp = img.copy()
            comp.alpha_composite(layer, off)
            ban.paint(bx, by, img.width, img.height, comp.tobytes("raw", "BGRa"), round(255 * alpha))
            self.bkey = None
        elif key != self.bkey:                               # static state: one upload, then only fade/move
            ban.paint(bx, by, img.width, img.height, img.tobytes("raw", "BGRa"), round(255 * alpha))
            self.bkey = key
        else:
            ban.move(bx, by)
            ban.fade(255 * alpha)

    def _cursor_bitmap(self, mode):
        key = ("cursor", mode, self.scale, self.provider)
        if key not in self.cache:
            self.cache[key] = cursor_frame(self.scale, PALETTES[self.provider], **cursor_mode_kwargs(mode))
        return self.cache[key]

    def _draw_cursor(self, now, t):
        if not self.pos or self.last_xy is None:
            return
        L, el = self.layers, now - self.t_click if self.t_click else CLICK_LIFE
        if click_progress(el) is not None:
            mode = ("click", int(el * 1000 / TICK))
        elif moving(now, self.t_move):
            mode = ("move", self.angle)
        else:
            mode = ("idle",)
        if mode != self.cmode:                               # arrow frames are cached per mode; the halo is its own window
            self.cmode = mode
            w, h, buf, _ = self._cursor_bitmap(mode)
            L["arrow"].paint(*self.cpos, w, h, buf)
            if mode == ("idle",):
                hk = ("halo", self.scale, self.provider)
                if self.hkey != hk:
                    self.cache.setdefault(hk, cursor_frame(self.scale, PALETTES[self.provider], arrow=False))
                    hw, hh, hbuf, _ = self.cache[hk]
                    L["halo"].paint(*self.cpos, hw, hh, hbuf, round(255 * halo_breath(t)))
                    self.hkey = hk
                L["halo"].show()
            else:
                L["halo"].hide()
            L["arrow"].show()
        if mode == ("idle",):
            L["halo"].fade(255 * halo_breath(t))

    def _tick(self):
        """One animation frame: cheap alpha updates, the banner dot, and the cursor when its mode changes."""
        if not self.visible:
            return
        now = time.monotonic()
        t, L = now - self.t_show, self.layers
        if self.stop_t0 is not None:
            edge, alpha, rise, done = stop_phase(now - self.stop_t0)
            if done:
                return self._hide()
            kl, kb = self.stop_from[0] * edge, self.stop_from[1] * edge
        else:
            (kl, kb), alpha, rise = edge_levels(self._st, t), 1.0, 0.0
            self.levels = (kl, kb)
        L["line"].fade(255 * kl)
        L["bloom"].fade(255 * kb)
        self._draw_banner(t, alpha, rise)
        if self.stop_t0 is None:
            self._draw_cursor(now, t)

    def _show(self, target):
        h = self.hwnd
        _u.KillTimer(h, 1)
        self.target = target
        if not self.visible:
            self.visible, self.t_show, self.stop_t0 = True, time.monotonic(), None
            if self._st == "stopped":                        # shown while stopped: just play the sequence
                self.stop_t0, self.stop_from = self.t_show, (0.0, 0.0)
        self._place()
        for k in ("bloom", "line", "banner"):
            self.layers[k].show()
        self.cmode = None                                    # the cursor windows go back on top on the next frame
        self._tick()
        _u.SetTimer(h, 2, TICK, None)

    def _hide(self):
        _u.KillTimer(self.hwnd, 1)
        _u.KillTimer(self.hwnd, 2)                           # no timer while hidden: zero idle cost
        self.visible, self.stop_t0, self.cmode, self.last_xy, self.t_move = False, None, None, None, None
        for layer in self.layers.values():
            layer.hide()

    def _moved(self):
        p = W.POINT()
        x, y = (p.x, p.y) if _u.GetCursorPos(ctypes.byref(p)) else self.pos  # sit exactly on the real cursor
        if not self.visible or self.stop_t0 is not None:
            return
        if self.last_xy not in (None, (x, y)):
            self.t_move = time.monotonic()
            self.angle = round(math.atan2(y - self.last_xy[1], x - self.last_xy[0]) / (math.pi / 8)) % 16
        self.last_xy, self.cpos = (x, y), (x - self.hot[0], y - self.hot[1])
        for k in ("halo", "arrow"):
            self.layers[k].move(*self.cpos, top=True)
        if self.cmode is None:
            now = time.monotonic()
            self._draw_cursor(now, now - self.t_show)

    def _sync(self):
        """State or palette changed (any thread set it): apply it on this thread."""
        if self.provider != self._prov:
            self._prov, self.painted, self.cmode, self.hkey = self.provider, None, None, None
            self.cache.clear()
            if self.visible:
                self._place()
        if self.state != self._st:
            self._st, self.cmode = self.state, None
            if self.state == "stopped" and self.visible:     # edge fades from where it is, the cursor goes at once
                self.stop_t0, self.stop_from = time.monotonic(), self.levels
                self.layers["halo"].hide()
                self.layers["arrow"].hide()
            else:
                self.stop_t0 = None
        self._tick()

    def _run(self):
        try:
            L = self.layers = {k: _Layer() for k in ("bloom", "line", "banner", "halo", "arrow")}
            self.scale = monitor(0)[1]
            self.hot = (round(HOT[0] * self.scale), round(HOT[1] * self.scale))
            self.windows = tuple(layer.h for layer in L.values())
            self.hwnd, self.cur = L["line"].h, L["arrow"].h     # hwnd receives the messages; cur is the cursor window
        except Exception as e:  # overlay is cosmetic: never break the server
            self.error = repr(e)
            return
        finally:
            self.ready.set()
        h, msg = self.hwnd, W.MSG()
        while _u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            try:
                m = msg.message
                if m == WM_TIMER and msg.hWnd == h:
                    if msg.wParam == 1:                       # linger elapsed: nothing new, go away
                        self._hide()
                    else:
                        self._tick()
                elif m == WM_APP:
                    if msg.wParam:
                        self._show(msg.lParam)
                    elif msg.lParam > 0:
                        _u.SetTimer(h, 1, msg.lParam, None)   # keep the border while the model thinks
                    else:
                        self._hide()
                elif m == WM_APP + 1:
                    self._moved()
                elif m == WM_APP + 2:
                    self._sync()
                elif m == WM_APP + 3:                         # click: the next frame plays it
                    self._tick()
                else:
                    _u.DispatchMessageW(ctypes.byref(msg))
            except Exception as e:
                self.error = repr(e)
