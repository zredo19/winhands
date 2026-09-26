"""UIA snapshot/diff (pure) + desktop helpers exposed to the run() REPL."""
import ctypes, os, subprocess, time

MAX_NAME = 40
KEEP = {"Button", "CheckBox", "ComboBox", "Edit", "Hyperlink", "ListItem", "MenuItem",
        "RadioButton", "Slider", "Spinner", "TabItem", "TreeItem", "Text", "Document",
        "DataItem", "SplitButton", "MenuBar", "ScrollBar", "Header", "HeaderItem"}


# ---------- pure logic (unit tested) ----------

def _clip(s):
    s = " ".join(s.split())
    return s if len(s) <= MAX_NAME else s[:MAX_NAME] + "…"


def format_line(i, n):
    s = f"[{i}] {n['role']}"
    if n["name"]:
        s += f' "{_clip(n["name"])}"'
    if n["value"] and n["value"] != n["name"]:
        s += f' ="{_clip(n["value"])}"'
    flags = ("*" if n["focused"] else "") + ("" if n["enabled"] else "~")
    return s + (" " + flags if flags else "")


class Registry:
    """Stable small int ids per UIA RuntimeId, so diffs reuse the same ids."""
    def __init__(self):
        self.ids, self.els = {}, {}   # rid -> id, rid -> live element

    def id_for(self, rid):
        return self.ids.setdefault(rid, len(self.ids) + 1)

    def element(self, id):
        """Latest live element that carries this id (ids survive RuntimeId churn)."""
        for rid in reversed([r for r, i in self.ids.items() if i == id]):
            if rid in self.els:
                return self.els[rid]
        return None


def render(nodes, reg, cap=150):
    lines = [format_line(reg.id_for(n["rid"]), n) for n in nodes[:cap]]
    if len(nodes) > cap:
        lines.append(f"... +{len(nodes) - cap} more (use find())")
    return "\n".join(lines)


def diff(old, new, reg, cap=80):
    o = {n["rid"]: n for n in old}
    nw = {n["rid"]: n for n in new}
    sig = lambda n: (n["role"], n["name"], n["value"])
    # RuntimeId churn: pair a vanished rid with a new rid of identical content, keep old id
    gone = {}
    for r, n in o.items():
        if r not in nw:
            gone.setdefault(sig(n), []).append(r)
    removed = set(r for rs in gone.values() for r in rs)
    for r, n in nw.items():
        if r not in o and gone.get(sig(n)):
            old_r = gone[sig(n)].pop(0)
            reg.ids[r] = reg.id_for(old_r)
            removed.discard(old_r)
            o[r] = n  # treat as unchanged
    out = [f"- {format_line(reg.id_for(r), o[r])}" for r in removed]
    for r, n in nw.items():
        if r not in o:
            out.append(f"+ {format_line(reg.id_for(r), n)}")
        elif o[r] != n:
            out.append(f"~ {format_line(reg.id_for(r), n)}")
    if len(out) > cap:
        out = out[:cap] + [f"... +{len(out) - cap} more changes"]
    return "\n".join(out) or "(no change)"


# ---------- desktop (live UIA) ----------

class Desk:
    def __init__(self, deny=()):
        import uiautomation as auto
        self.auto, self.deny = auto, [d.lower() for d in deny]
        self.reg = Registry()
        self.last = {}          # hwnd -> nodes at last observe (diff baseline)
        self.cache = {}         # hwnd -> latest nodes seen (for find/click lookups)
        self.win = None         # current target window (uiautomation Control)
        self.shot_tf = None     # (scale, ox, oy) of last screenshot
        self.stop = None        # callable raising if kill switch pressed
        from uiautomation.uiautomation import _AutomationClient
        ua = _AutomationClient.instance()
        self.ia, self.uc = ua.IUIAutomation, ua.UIAutomationCore
        cr = self.ia.CreateCacheRequest()
        for pid in (30000, 30001, 30003, 30005, 30008, 30010, 30045):
            cr.AddProperty(pid)
        self.cr = cr
        self.cond = self.ia.CreateAndCondition(
            self.ia.ControlViewCondition,
            self.ia.CreatePropertyCondition(30022, False))  # IsOffscreen == False
        self.roles = {k: v.replace("Control", "") for k, v in auto.ControlTypeNames.items()}

    # --- window selection ---
    def _top(self, target=None):
        a = self.auto
        if target:
            t = target.lower()
            for w in a.GetRootControl().GetChildren():
                if t in (w.Name or "").lower():
                    return w
            raise LookupError(f"no window matching {target!r}")
        return a.GetForegroundControl().GetTopLevelControl()

    def _wins(self):
        """hwnd -> top-level window control (visible ones)."""
        return {w.NativeWindowHandle: w for w in self.auto.GetRootControl().GetChildren()
                if w.NativeWindowHandle}

    def _alive(self, w):
        try:
            return w is not None and bool(ctypes.windll.user32.IsWindow(w.NativeWindowHandle))
        except Exception:
            return False

    def mark(self):
        """Remember current top-level windows, to spot dialogs opened by an action."""
        self.before = set(self._wins())

    def _guard(self, w):
        name = (w.Name or "").lower()
        if any(d in name for d in self.deny):
            raise PermissionError(f"window {w.Name!r} is on the denylist")

    # --- snapshot ---
    def nodes(self, w):
        arr = w.Element.FindAllBuildCache(4, self.cond, self.cr)  # TreeScope_Descendants
        out = []
        for i in range(arr.Length):
            e = arr.GetElement(i)
            role = self.roles.get(e.CachedControlType, "?")
            name = e.CachedName or ""
            if role not in KEEP or (role == "Text" and not name):
                continue
            try:
                val = e.GetCachedPropertyValue(30045) or ""
            except Exception:
                val = ""
            rid = tuple(e.GetRuntimeId() or (id(e),))
            n = {"rid": rid, "role": role, "name": name, "value": str(val),
                 "focused": bool(e.CachedHasKeyboardFocus), "enabled": bool(e.CachedIsEnabled)}
            self.reg.els[rid] = e
            out.append(n)
        return out

    def observe(self, target=None, mode="tree"):
        if target:
            w = self._top(target)
        elif self._alive(self.win):
            w = self.win
        else:
            w = self._top()
        self._guard(w)
        self.win = w
        key, nodes = w.NativeWindowHandle, self.nodes(w)
        head = f'window: "{_clip(w.Name or "")}" ({w.ControlTypeName.replace("Control", "")})'
        if mode == "diff" and key in self.last:
            body = diff(self.last[key], nodes, self.reg)
        else:
            body = render(nodes, self.reg) or "(empty tree: try mode='shot')"
        self.last[key] = self.cache[key] = nodes
        return head + "\n" + body

    def after_action(self):
        """Validate step: full tree of a newly opened window, else diff of the target."""
        time.sleep(0.15)
        new = [w for h, w in self._wins().items() if h not in getattr(self, "before", ())]
        if new:
            self.win = new[0]
            return self.observe()
        if not self._alive(self.win):
            self.win = None
            return "target window closed\n" + self.observe()
        return self.observe(mode="diff")

    # --- screenshot ---
    def shot(self, target=None, region=None, max_edge=1280):
        import io, mss
        from PIL import Image
        if region:
            box = dict(left=region[0], top=region[1],
                       width=region[2] - region[0], height=region[3] - region[1])
        else:
            w = self._top(target)
            self._guard(w)
            r = w.BoundingRectangle
            box = dict(left=r.left, top=r.top, width=r.width(), height=r.height())
        with mss.mss() as s:
            raw = s.grab(box)
        img = Image.frombytes("RGB", raw.size, raw.rgb)
        scale = max(img.size) / max_edge if max(img.size) > max_edge else 1.0
        if scale > 1:
            img = img.resize((round(img.width / scale), round(img.height / scale)))
        self.shot_tf = (scale, box["left"], box["top"])
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=60)
        return buf.getvalue(), img.size

    # --- actions (REPL helpers) ---
    def _ctl(self, id=None, name=None, role=None):
        self.stop()
        if id is None:
            # cached tree first (no COM walk); fall back to a fresh snapshot
            ids = self.find(role=role, name=name, raw=True, fresh=False) or                 self.find(role=role, name=name, raw=True)
            if not ids:
                raise LookupError(f"no element name={name!r} role={role!r}")
            id = ids[0]
        e = self.reg.element(id)
        if e is None:
            raise LookupError(f"unknown id {id}: observe first")
        return self.auto.Control.CreateControlFromElement(e)

    def click(self, id=None, name=None, role=None, button="left", double=False):
        c = self._ctl(id, name, role)
        if button == "left" and not double:
            for get, act in (("GetInvokePattern", "Invoke"), ("GetTogglePattern", "Toggle"),
                             ("GetSelectionItemPattern", "Select"),
                             ("GetExpandCollapsePattern", "Expand")):
                try:
                    p = getattr(c, get)()
                    if p:
                        getattr(p, act)(waitTime=0)  # lib default sleeps 0.5s
                        return
                except Exception:
                    pass
        f = {"left": c.DoubleClick if double else c.Click, "right": c.RightClick}[button]
        f(simulateMove=False, waitTime=0.05)

    def dclick(self, id=None, **kw):
        self.click(id, double=True, **kw)

    def rclick(self, id=None, **kw):
        self.click(id, button="right", **kw)

    def type(self, text, id=None, name=None, role=None, enter=False):
        if id is not None or name is not None:
            c = self._ctl(id, name, role)
            try:
                vp = c.GetValuePattern()
                if vp and not vp.IsReadOnly:
                    vp.SetValue(text, waitTime=0)
                    if enter:
                        self.key("enter")
                    return
            except Exception:
                pass
            c.SetFocus()
        self._front()
        for ch in text:
            self.stop()
            if ch == "\n":
                self.key("enter")
            else:
                self.auto.SendUnicodeChar(ch)
        if enter:
            self.key("enter")

    VK = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
          "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "back": 0x08,
          "backspace": 0x08, "del": 0x2E, "delete": 0x2E, "ins": 0x2D, "insert": 0x2D,
          "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
          "pgup": 0x21, "pgdn": 0x22, "space": 0x20, **{f"f{i}": 0x6F + i for i in range(1, 13)}}

    def _vk(self, k):
        if k in self.VK:
            return self.VK[k]
        if len(k) == 1:
            return ctypes.windll.user32.VkKeyScanW(ord(k)) & 0xFF
        raise ValueError(f"unknown key {k!r}")

    def _front(self):
        """Keystrokes go to the foreground window: make sure it is our target."""
        w = self.win if self._alive(self.win) else None
        if w is None:
            return
        u = ctypes.windll.user32
        if u.GetAncestor(u.GetForegroundWindow(), 3) != w.NativeWindowHandle:
            h = w.NativeWindowHandle
            if not u.SetForegroundWindow(h):
                # background processes can't take focus; minimize+restore activates reliably
                u.ShowWindow(h, 6)  # SW_MINIMIZE
                u.ShowWindow(h, 9)  # SW_RESTORE
            time.sleep(0.3)
        fg = u.GetForegroundWindow()
        if fg != w.NativeWindowHandle and u.GetAncestor(fg, 3) != w.NativeWindowHandle:
            raise RuntimeError(f"could not bring {w.Name!r} to front; keys not sent")

    EXT = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B}

    @classmethod
    def _kb(cls, vk, up):
        # real scan code; extended flag only for nav keys (else Alt becomes AltGr, Ctrl RCtrl)
        u = ctypes.windll.user32
        flags = (2 if up else 0) | (1 if vk in cls.EXT else 0)
        u.keybd_event(vk, u.MapVirtualKeyW(vk, 0), flags, 0)

    def key(self, combo, times=1):
        """key("ctrl+shift+s"), key("enter", times=3)."""
        self._front()
        vks = [self._vk(p) for p in combo.lower().split("+")]
        for _ in range(times):
            self.stop()
            for v in vks:
                self._kb(v, False)
            for v in reversed(vks):
                self._kb(v, True)
            time.sleep(0.03)

    def scroll(self, id=None, dir="down", n=3, name=None):
        c = self._ctl(id, name)
        c.MoveCursorToMyCenter(simulateMove=False)
        (self.auto.WheelDown if dir == "down" else self.auto.WheelUp)(wheelTimes=n, waitTime=0.05)

    def click_xy(self, x, y, button="left", image=True):
        """Click at screenshot-image coords (image=True) or raw screen coords."""
        self.stop()
        if image and self.shot_tf:
            s, ox, oy = self.shot_tf
            x, y = round(x * s + ox), round(y * s + oy)
        {"left": self.auto.Click, "right": self.auto.RightClick}[button](x, y, waitTime=0.05)

    def _match(self, w, role, name, fresh):
        h = w.NativeWindowHandle
        if fresh or h not in self.cache:
            self.cache[h] = self.nodes(w)
        hits = [n for n in self.cache[h]
                if (not role or n["role"].lower() == role.lower())
                and (not name or name.lower() in (n["name"] + " " + n["value"]).lower())]
        if name:  # exact name matches first
            hits.sort(key=lambda n: n["name"].lower() != name.lower())
        return hits

    def find(self, role=None, name=None, raw=False, fresh=True):
        if not self._alive(self.win):
            self.win = self._top()
        hits = self._match(self.win, role, name, fresh)
        if not hits and fresh:
            # dialogs (Save As, popups) are separate top-level windows: look there too
            for h, w in self._wins().items():
                if h not in getattr(self, "before", ()) and h != self.win.NativeWindowHandle:
                    hits = self._match(w, role, name, True)
                    if hits:
                        self.win = w
                        break
        ids = [self.reg.id_for(n["rid"]) for n in hits]
        if raw:
            return ids
        return "\n".join(format_line(i, n) for i, n in zip(ids, hits)) or "(none)"

    def wait_for(self, name, role=None, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            self.stop()
            ids = self.find(role=role, name=name, raw=True)
            if ids:
                return ids[0]
            time.sleep(0.2)
        raise TimeoutError(f"{name!r} not found in {timeout}s")

    def focus(self, title):
        w = self._top(title)
        self._guard(w)
        w.SetActive()
        w.SetFocus()
        self.win = w
        return w.Name

    def app(self, cmd, title=None, timeout=10.0):
        before = set(self._wins())
        (os.startfile if os.path.exists(cmd) else self._spawn)(cmd)
        end = time.time() + timeout
        while time.time() < end:
            self.stop()
            time.sleep(0.3)
            new = [w for h, w in self._wins().items() if h not in before
                   and (not title or title.lower() in (w.Name or "").lower())]
            if new:
                w = new[0]
                self._guard(w)
                try:
                    w.SetActive()
                except Exception:
                    pass
                self.win = w
                self.before = set(self._wins())
                return w.Name
        raise TimeoutError(f"no new window after launching {cmd!r}")

    @staticmethod
    def _spawn(cmd):
        # never inherit our stdio: under MCP stdio it would corrupt/hold the protocol pipe
        subprocess.Popen(cmd, shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True,
                         creationflags=subprocess.DETACHED_PROCESS)

    def sh(self, cmd, timeout=30):
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()[:2000]
