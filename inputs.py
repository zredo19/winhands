"""Game-grade input via SendInput: scan codes, virtual-desktop absolute and raw relative mouse.

Scan codes make DirectInput/raw-input games (Minecraft, SDL/GLFW) see real keys; relative
moves reach raw-input games 1:1 (camera look) but are DPI/speed-scaled for the cursor, so
position the cursor with move() (absolute) and use move_rel() only for game cameras.
"""
import ctypes, time
from ctypes import wintypes as W

_u32 = ctypes.WinDLL("user32", use_last_error=True)  # private instance: argtypes never clash
try:
    _u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # physical px (no-op if already aware)
except Exception:
    pass


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD), ("dwFlags", W.DWORD),
                ("time", W.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD),
                ("time", W.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _U(ctypes.Union):  # MOUSEINPUT sizes the union: omitting it breaks SendInput (error 87)
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", W.DWORD), ("u", _U)]  # x64: 4 + 4 pad + 32 = 40 bytes


assert ctypes.sizeof(INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
_u32.SendInput.argtypes = (W.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
_u32.SendInput.restype = W.UINT

# Set-1 make codes by physical key (US names); 0xE0xx = E0-prefixed (sent with EXTENDEDKEY).
SCAN = {
    "esc": 0x01, "escape": 0x01, "-": 0x0C, "=": 0x0D, "backspace": 0x0E, "back": 0x0E, "tab": 0x0F,
    "[": 0x1A, "]": 0x1B, "enter": 0x1C, "return": 0x1C, "ctrl": 0x1D, "lctrl": 0x1D, ";": 0x27,
    "'": 0x28, "`": 0x29, "shift": 0x2A, "lshift": 0x2A, "\\": 0x2B, ",": 0x33, ".": 0x34, "/": 0x35,
    "rshift": 0x36, "alt": 0x38, "lalt": 0x38, "space": 0x39, "capslock": 0x3A, "numlock": 0x45,
    "scrolllock": 0x46, "f11": 0x57, "f12": 0x58,
    "rctrl": 0xE01D, "ralt": 0xE038, "altgr": 0xE038, "win": 0xE05B, "lwin": 0xE05B, "rwin": 0xE05C,
    "apps": 0xE05D, "up": 0xE048, "down": 0xE050, "left": 0xE04B, "right": 0xE04D, "home": 0xE047,
    "end": 0xE04F, "pgup": 0xE049, "pgdn": 0xE051, "ins": 0xE052, "insert": 0xE052,
    "del": 0xE053, "delete": 0xE053,
    **{f"f{i}": 0x3A + i for i in range(1, 11)},
    **dict(zip("1234567890", range(0x02, 0x0C))),
    **dict(zip("qwertyuiop", range(0x10, 0x1A))),
    **dict(zip("asdfghjkl", range(0x1E, 0x27))),
    **dict(zip("zxcvbnm", range(0x2C, 0x33))),
}
BTN = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
TAP = 0.04       # games poll key state per frame: taps shorter than ~30 ms get lost
check = None     # set by the server: raises on kill switch / user interrupt
held = set()     # ("key", scan) / ("btn", name) currently pressed by us
last = [0.0]     # monotonic time of our latest real input or focus steal
guard = None     # set by the server (shared mode): raises if the user is using the PC right now
on_move = None   # set by the server: (x, y) of each absolute move, drawn as the overlay cursor
on_click = None  # set by the server: button name of each press, played as the overlay click ripple


# ---------- pure helpers ----------

def _norm(p, w):
    """Absolute coordinate normalization aimed at the pixel centre (hits every pixel)."""
    return (p * 65536 + 32768) // w


def interpolate(path, max_step=8):
    """Densify a polyline so no step exceeds max_step px (smooth drags/strokes)."""
    pts = [tuple(map(round, path[0]))]
    for (x0, y0), (x1, y1) in zip(path, path[1:]):
        n = max(1, int(max(abs(x1 - x0), abs(y1 - y0)) // max_step) + 1)
        pts += [(round(x0 + (x1 - x0) * i / n), round(y0 + (y1 - y0) * i / n)) for i in range(1, n + 1)]
    return pts


def scan_of(k):
    """Key name / char / raw scan code -> Set-1 scan code (layout-aware for other chars)."""
    if isinstance(k, int):
        return k
    s = SCAN.get(k.lower())
    if s is not None:
        return s
    if len(k) == 1:
        vk = _u32.VkKeyScanW(ord(k)) & 0xFF
        s = _u32.MapVirtualKeyW(vk, 0) if vk != 0xFF else 0
        if s:
            return s
    raise ValueError(f"unknown key {k!r}")


def combo(spec):
    """'ctrl+shift+s' -> [scan, ...] in press order."""
    return [scan_of(p) for p in spec.split("+")] if isinstance(spec, str) else [scan_of(spec)]


def key_input(scan, up=False):
    flags = 0x0008 | (0x0001 if scan > 0xFF else 0) | (0x0002 if up else 0)  # SCANCODE|EXTENDED|KEYUP
    return INPUT(1, _U(ki=KEYBDINPUT(0, scan & 0xFF, flags, 0, 0)))


def mouse_input(dx=0, dy=0, flags=0, data=0):
    return INPUT(0, _U(mi=MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)))  # extra=0 (SDL3 reads it)


# ---------- raw send ----------

def busy(window=0.5):
    """We drive the real mouse/keyboard right now: something held, or injected very recently."""
    return bool(held) or time.monotonic() - last[0] < window


def touch():
    """Before any real input or focus steal: yield to a busy user (guard), then mark the moment."""
    if guard:
        guard()
    last[0] = time.monotonic()


def send(*inputs):
    """Atomic batch. Input into elevated windows is silently dropped by UIPI (not reported)."""
    touch()
    _raw(*inputs)


def _raw(*inputs):
    n = len(inputs)
    if _u32.SendInput(n, (INPUT * n)(*inputs), ctypes.sizeof(INPUT)) != n:
        raise ctypes.WinError(ctypes.get_last_error())


def sleep(secs):
    """Sleep in small chunks so the kill switch / user interrupt can abort."""
    end = time.monotonic() + secs
    while True:
        if check:
            check()
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(left, 0.02))


# ---------- keyboard ----------

def key_down(k):
    s = scan_of(k)
    send(key_input(s))
    held.add(("key", s))


def key_up(k):
    s = scan_of(k)
    send(key_input(s, up=True))
    held.discard(("key", s))


def press(spec, times=1, hold=TAP, gap=TAP):
    """press('ctrl+shift+s'), press('space', times=3). Holds each tap `hold` s (games poll)."""
    scans = combo(spec)
    for i in range(times):
        try:
            for s in scans:
                key_down(s)
            sleep(hold)
        finally:
            for s in reversed(scans):
                key_up(s)
        if i < times - 1:
            sleep(gap)


def hold(k, secs):
    """Hold a key for secs (e.g. walk forward in a game)."""
    key_down(k)
    try:
        sleep(secs)
    finally:
        key_up(k)


def text(s):
    """Type any unicode text (VK_PACKET -> WM_CHAR); newlines become Enter."""
    for ch in s:
        if check:
            check()
        if ch == "\n":
            press("enter")
            continue
        b = ch.encode("utf-16-le")  # surrogate pairs -> two units
        units = [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b), 2)]
        send(*[INPUT(1, _U(ki=KEYBDINPUT(0, u, 0x0004 | up, 0, 0)))  # KEYEVENTF_UNICODE
               for u in units for up in (0, 0x0002)])


def type_keys(s, gap=0.02):
    """Type through physical scan codes (for games that ignore unicode input)."""
    for ch in s:
        if ch == "\n":
            press("enter")
            continue
        r = _u32.VkKeyScanW(ord(ch))
        if r == -1 or (r & 0xFF) == 0xFF:
            raise ValueError(f"char {ch!r} not on the current keyboard layout")
        st, sc = (r >> 8) & 0xFF, _u32.MapVirtualKeyW(r & 0xFF, 0)
        # shift state bits: 1 shift, 2 ctrl, 4 alt; ctrl+alt = AltGr (right Alt)
        mods = ([0xE038] if st & 6 == 6 else [0x1D] * bool(st & 2) + [0x38] * bool(st & 4)) \
            + [0x2A] * bool(st & 1)
        for m in mods:
            key_down(m)
        try:
            press(sc, hold=0.02)
        finally:
            for m in reversed(mods):
                key_up(m)
        sleep(gap)


# ---------- mouse ----------

def _virtual():
    g = _u32.GetSystemMetrics
    return g(76), g(77), g(78), g(79)  # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CX.., SM_CY..


def move(x, y):
    """Absolute move to physical virtual-desktop pixel (monitor left of primary => negative x)."""
    vx, vy, vw, vh = _virtual()
    send(mouse_input(_norm(round(x) - vx, vw), _norm(round(y) - vy, vh), 0x0001 | 0x4000 | 0x8000))
    if on_move:
        on_move(round(x), round(y))


def move_rel(dx, dy, steps=1, duration=0.0):
    """Relative move for game cameras (raw input gets exact counts). Split into steps for smooth turns."""
    steps = max(1, steps)
    sx = [round(dx * (i + 1) / steps) - round(dx * i / steps) for i in range(steps)]
    sy = [round(dy * (i + 1) / steps) - round(dy * i / steps) for i in range(steps)]
    for a, b in zip(sx, sy):
        send(mouse_input(a, b, 0x0001))
        if duration:
            sleep(duration / steps)


def mouse_down(btn="left"):
    send(mouse_input(flags=BTN[btn][0]))
    held.add(("btn", btn))
    if on_click:
        on_click(btn)


def mouse_up(btn="left"):
    send(mouse_input(flags=BTN[btn][1]))
    held.discard(("btn", btn))


def click_at(x, y, btn="left", count=1, hold=0.03):
    """Real click at screen px (count=2 double click)."""
    move(x, y)
    for i in range(count):
        mouse_down(btn)
        try:
            sleep(hold)
        finally:
            mouse_up(btn)
        if i < count - 1:
            sleep(0.05)


def wheel(clicks):
    """Vertical wheel at the cursor: +up/away, -down/toward you."""
    send(mouse_input(flags=0x0800, data=120 * clicks))


def drag(path, btn="left", duration=0.3, max_step=8):
    """Press at path[0], move through every point (densified), release at the end. For Paint
    strokes and drag&drop. The button is always released, even on errors/abort."""
    pts = interpolate(path, max_step)
    move(*pts[0])
    sleep(0.03)
    mouse_down(btn)
    try:
        dt = duration / max(1, len(pts) - 1)
        for p in pts[1:]:
            move(*p)
            if dt:
                sleep(dt)
        sleep(0.03)
    finally:
        mouse_up(btn)


def release_all():
    """Release everything we hold (on kill/timeout/error)."""
    for kind, v in sorted(held, reverse=True):
        try:
            _raw(mouse_input(flags=BTN[v][1]) if kind == "btn" else key_input(v, up=True))  # no guard
        except Exception:
            pass
    held.clear()


def cursor():
    p = W.POINT()
    _u32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y
