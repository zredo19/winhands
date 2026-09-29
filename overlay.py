"""Takeover overlay: orange glow border + banner on the monitor being controlled (like Claude in Chrome).

Per-pixel-alpha layered window (UpdateLayeredWindow): click-through (WS_EX_TRANSPARENT), topmost,
never activated (WS_EX_NOACTIVATE) and excluded from screen capture (WDA_EXCLUDEFROMCAPTURE), so it
never steals focus or appears in shots. show(hwnd) covers hwnd's monitor (the primary one when 0 or
closed). point(x, y) moves an orange-edged cursor to where Claude acts (its own small layered window).
Own thread and message loop; show()/hide()/point() are thread-safe. Failure to start is non-fatal.
"""
import ctypes, threading
from ctypes import wintypes as W

GLOW = 26                                     # glow depth in px at 100% scaling
EDGE, INNER = (255, 140, 60), (217, 119, 87)  # bright orange at the edge -> Claude orange inward
WM_APP = 0x8000
ARROW = [(0, 0), (0, 21), (5, 16.5), (8.5, 24.5), (12, 23), (8.5, 15.5), (15, 15.5)]  # pointer, 1x units

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
    (_u.SetWindowPos, W.BOOL, (W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.UINT)),
    (_u.IsWindow, W.BOOL, (W.HWND,)),
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


def frame(w, h, scale=1.0, text=""):
    """Premultiplied BGRA pixels: orange glow fading inward from every edge (+ a banner pill on top)."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (w, h))
    dr = ImageDraw.Draw(img)
    n = max(2, round(GLOW * scale))
    for i in range(n):  # one 1-px ring per depth; colour and alpha fade towards the inside
        t = i / n
        c = tuple(round(e + (k - e) * t) for e, k in zip(EDGE, INNER))
        dr.rectangle((i, i, w - 1 - i, h - 1 - i), outline=(*c, round(255 * (1 - t) ** 2)))
    if text:
        size = round(14 * scale)
        try:
            font = ImageFont.truetype("segoeuib.ttf", size)
        except OSError:
            font = ImageFont.load_default(size)
        x0, y0, x1, y1 = dr.textbbox((0, 0), text, font=font)
        pw, ph = x1 - x0 + round(32 * scale), y1 - y0 + round(16 * scale)
        left, top = (w - pw) // 2, round(10 * scale)
        dr.rounded_rectangle((left, top, left + pw, top + ph), ph // 2, fill=(*INNER, 240))
        dr.text((left + pw / 2, top + ph / 2), text, fill="white", font=font, anchor="mm")
    return img.tobytes("raw", "BGRa")


def cursor_frame(scale=1.0):
    """(w, h, premultiplied BGRA, hotspot) of a white arrow with an orange gradient border and soft glow."""
    from PIL import Image, ImageDraw, ImageFilter
    ss, k, m = 4, 1.2 * scale, 5                     # supersampling, size, margin for the glow (1x units)
    f = ss * k
    W_, H_ = round((15 + 2 * m) * f), round((24.5 + 2 * m) * f)
    body = Image.new("L", (W_, H_))
    ImageDraw.Draw(body).polygon([((x + m) * f, (y + m) * f) for x, y in ARROW], fill=255)
    inner = body.filter(ImageFilter.MinFilter(2 * round(1.3 * f) + 1))     # body shrunk by ~2.6 px
    ring = Image.composite(Image.new("L", body.size), body, inner)          # border = body - inner
    grad = Image.linear_gradient("L").resize(body.size)                      # 0 top -> 255 bottom
    edge = Image.merge("RGB", [Image.eval(grad, lambda v, a=a, b=b: round(a + (b - a) * v / 255))
                               for a, b in zip(EDGE, INNER)])
    glow = body.filter(ImageFilter.MaxFilter(2 * round(1.2 * f) + 1)).filter(ImageFilter.GaussianBlur(2.2 * f))
    out = Image.new("RGBA", body.size)
    out.paste(Image.new("RGBA", body.size, (*INNER, 255)), mask=Image.eval(glow, lambda v: v * 150 // 255))
    out.paste(Image.new("RGBA", body.size, (255, 255, 255, 255)), mask=inner)
    out.paste(Image.merge("RGBA", (*edge.split(), Image.new("L", body.size, 255))), mask=ring)
    out = out.resize((round(W_ / ss), round(H_ / ss)), Image.Resampling.LANCZOS)
    return out.width, out.height, out.tobytes("raw", "BGRa"), (round(m * k), round(m * k))


def _ulw(hwnd, x, y, w, h, buf):
    """Put premultiplied BGRA pixels on a layered window at screen (x, y)."""
    bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32)  # top-down 32-bit
    bits = W.LPVOID()
    sdc = _u.GetDC(None)
    mdc = _g.CreateCompatibleDC(sdc)
    bmp = _g.CreateDIBSection(mdc, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
    try:
        ctypes.memmove(bits, buf, len(buf))
        old = _g.SelectObject(mdc, bmp)
        blend = (ctypes.c_ubyte * 4)(0, 0, 255, 1)  # AC_SRC_OVER, constant 255, AC_SRC_ALPHA
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


class Overlay:
    def __init__(self, text="astra-cu is controlling this PC  -  Ctrl+Alt+Q to stop"):
        self.text, self.hwnd, self.error, self.painted = text, None, None, None
        self.cur, self.hot, self.pos = None, (0, 0), None
        self.ready = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="astra-overlay").start()

    def show(self, hwnd=0):
        """Cover hwnd's monitor (the primary one when 0 or closed)."""
        if self.hwnd:
            _u.PostMessageW(self.hwnd, WM_APP, 1, hwnd or 0)

    def hide(self):
        if self.hwnd:
            _u.PostMessageW(self.hwnd, WM_APP, 0, 0)

    def point(self, x, y):
        """Show Claude's cursor at screen (x, y)."""
        self.pos = (x, y)
        if self.hwnd:
            _u.PostMessageW(self.hwnd, WM_APP + 1, 0, 0)

    def _place(self, target):
        """Move onto the target's monitor; re-render only when that monitor (or its scaling) changes."""
        (x, y, x1, y1), scale = key = monitor(target)
        if key == self.painted:
            return
        _ulw(self.hwnd, x, y, x1 - x, y1 - y, frame(x1 - x, y1 - y, scale, self.text))
        self.painted = key

    def _run(self):
        try:
            h, cur = _layered(), _layered()
            cw, ch, cbuf, self.hot = cursor_frame(monitor(0)[1])
            _ulw(cur, 0, 0, cw, ch, cbuf)
            self.hwnd, self.cur = h, cur
        except Exception as e:  # overlay is cosmetic: never break the server
            self.error = repr(e)
            return
        finally:
            self.ready.set()
        msg = W.MSG()
        while _u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_APP + 1 and self.pos:
                x, y = self.pos
                _u.SetWindowPos(cur, -1, x - self.hot[0], y - self.hot[1], 0, 0, 0x11)  # TOPMOST, NOSIZE|NOACTIVATE
                _u.ShowWindow(cur, 4)
                continue
            if msg.message != WM_APP:
                _u.DispatchMessageW(ctypes.byref(msg))
                continue
            try:
                if msg.wParam:
                    self._place(msg.lParam)
                    _u.ShowWindow(h, 4)                       # SW_SHOWNOACTIVATE
                    _u.SetWindowPos(h, -1, 0, 0, 0, 0, 0x13)  # HWND_TOPMOST, NOSIZE|NOMOVE|NOACTIVATE
                else:
                    _u.ShowWindow(h, 0)                       # SW_HIDE
                    _u.ShowWindow(cur, 0)
            except Exception as e:
                self.error = repr(e)
