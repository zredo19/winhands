"""UI Automation: Codex-style window trees (pure formatting logic + live desktop helpers).

One BuildUpdatedCache call fetches the whole window subtree (TreeScope=Subtree, filtered to
the control view and on-screen elements); pure functions prune, collapse and render it.
Element ids are bound to the latest snapshot of their window: acting on an id re-reads the
live element and fails closed (StaleTarget) if it vanished or changed.
"""
import ctypes, os, re, subprocess, time

import inputs

MAX_NAME = 60
INTERACTIVE = {"button", "check box", "combo box", "edit", "hyperlink", "list item", "menu item",
               "radio button", "slider", "spinner", "tab item", "tree item", "data item",
               "split button", "document", "header item"}
DROP = {"scroll bar", "thumb", "separator", "tool tip"}
CANVAS_ROLES = {"pane", "custom", "image", "document", "group"}
CONTAINERS = {"pane", "tool bar", "tab", "group", "custom", "menu bar", "status bar", "title bar",
              "list", "tree", "table", "header", "window"}
SHELL_HOSTS = {"shellexperiencehost.exe", "startmenuexperiencehost.exe", "searchhost.exe"}  # toasts, flyouts
RISKY = re.compile(r"\b(enviar|send|comprar|buy|pagar|pay|purchase|checkout|eliminar|delete|remove|"
                   r"instalar|install|permitir|allow|transferir|transfer|place order|suscribir|subscribe)\b", re.I)


class StaleTarget(Exception):
    """The element changed or disappeared since the snapshot: observe again."""


class GuardBlocked(Exception):
    """Risky action needs run(..., confirm=True) after asking the user."""


# ---------- pure logic (unit tested) ----------

def role_name(control_type_name):
    """'MenuItemControl' -> 'menu item'."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", control_type_name.replace("Control", "")).lower()


def risky(name):
    return bool(name) and bool(RISKY.search(name))


def cap_lines(text, n=60):
    """Keep dumps short: first n lines + a count of the rest."""
    lines = text.splitlines()
    if len(lines) <= n:
        return text
    return "\n".join(lines[:n] + [f"... +{len(lines) - n} lines (observe() for all)"])


def _clip(s, n=MAX_NAME):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n] + "…"


def _is_text_leaf(n):
    return n["role"] == "text" and not n["children"]


def _prune_list(children, win_area):
    out = []
    for c in children:
        for k in prune(c, win_area):
            if out and _is_text_leaf(k) and _is_text_leaf(out[-1]) and len(out[-1]["name"]) < 180:
                out[-1]["name"] += " · " + k["name"]      # merge adjacent text-only siblings
            else:
                out.append(k)
    return out


def prune(node, win_area):
    """-> list of nodes replacing `node` (0 = dropped, children = flattened)."""
    if node["role"] in DROP:
        return []
    node = {**node, "children": _prune_list(node["children"], win_area)}
    if node["role"] in CONTAINERS:
        node["value"], node["settable"] = "", False  # legacy MSAA values on containers are noise
        if len(node["children"]) == 1 and node["role"] not in ("window", "list", "tree", "table"):
            return node["children"]                        # merge single-item groups
    has_kids = bool(node["children"])
    if not node["enabled"] and not node["name"] and not node["value"] and not has_kids:
        return []                                              # empty disabled
    x0, y0, x1, y1 = node["rect"]
    if node["role"] in CANVAS_ROLES and not has_kids and (x1 - x0) * (y1 - y0) >= 0.2 * win_area:
        return [{**node, "canvas": True}]                      # big childless surface: canvas
    descriptive = node["name"] or node["value"] or node["role"] in INTERACTIVE or node["focused"]
    if descriptive:
        return [node]
    return node["children"]                                    # unnamed container: flatten (or drop)


def collapse_lists(node, max_items=25):
    """Fold list/grid item cells into the item line; cap long lists with a summary."""
    kids = [collapse_lists(c, max_items) for c in node["children"]]
    items = [c for c in kids if c["role"] in ("list item", "data item")]
    for it in items:
        extra = []
        for d in flatten(it)[1:]:
            t = d["value"] or d["name"]
            if t and t != it["name"] and t not in extra:
                extra.append(t)
        it["extra"], it["children"] = extra, []
    capped = items or [c for c in kids if c["role"] == "tree item"]
    node = {**node, "children": kids}
    if len(capped) > max_items:
        drop = {id(c) for c in capped[max_items:]}
        node["children"] = [c for c in kids if id(c) not in drop]
        node["more"] = (max_items, len(capped))
    return node


def flatten(node):
    out = [node]
    for c in node["children"]:
        out += flatten(c)
    return out


def node_line(i, n):
    s = f"{i} {n['role']}"
    if n["name"]:
        s += f' "{_clip(n["name"], 200 if n["role"] == "text" else MAX_NAME)}"'  # merged texts carry data
    if n["value"] and n["value"] != n["name"]:
        s += f' Value: "{_clip(n["value"])}"'
    if n.get("extra"):
        s += " · " + " · ".join(_clip(e, 30) for e in n["extra"][:4])
    st = []
    if n["focused"]:
        st.append("focused")
    if not n["enabled"]:
        st.append("disabled")
    if n.get("selected"):
        st.append("selected")
    if n.get("checked"):
        st.append({"on": "checked", "off": "unchecked", "mixed": "mixed"}[n["checked"]])
    if n.get("expanded") is not None:
        st.append("expanded" if n["expanded"] else "collapsed")
    if n.get("settable"):
        st.append("settable")
    if n.get("scrollable"):
        st.append("scrollable")
    if n.get("canvas"):
        st.append("canvas")
    return s + (f" ({', '.join(st)})" if st else "")


class Registry:
    """Small ids that stay stable across snapshots; tracks the ids of each window's latest snapshot.
    Identity = (RuntimeId, role, name): MSAA-proxied UIs (Win32 ribbons) reuse RuntimeIds for other
    elements after re-layout, and some elements get a fresh RuntimeId each snapshot (paired back
    to their old id by role+name)."""
    def __init__(self):
        self.ids, self.key_of, self.rec, self.current, self.snap = {}, {}, {}, {}, {}
        self._next = 1

    @staticmethod
    def _key(n):
        return n["rid"], n["role"], n["name"]

    def id_of(self, n):
        return self.ids[self._key(n)]

    def assign(self, hwnd, flat):
        prev = self.snap.get(hwnd, {})
        keys = {self._key(n) for n in flat}
        gone = {}
        for i, sig in prev.items():
            if self.key_of.get(i) not in keys:
                gone.setdefault(sig, []).append(i)
        cur, snap = set(), {}
        for n in flat:
            k, sig = self._key(n), (n["role"], n["name"])
            i = self.ids.get(k)
            if i is None or i in cur:          # never two nodes with one id in a snapshot
                i = gone[sig].pop(0) if gone.get(sig) else self._new()
                self.ids[k] = i
            self.key_of[i], self.rec[i] = k, {**n, "hwnd": hwnd}
            cur.add(i)
            snap[i] = sig
        self.current[hwnd], self.snap[hwnd] = cur, snap

    def _new(self):
        self._next += 1
        return self._next - 1

    def lines(self, flat):
        return {self.id_of(n): node_line(self.id_of(n), n) for n in flat}


def render(node, reg, depth=0):
    lines = ["  " * depth + node_line(reg.id_of(node), node)]
    for c in node["children"]:
        lines.append(render(c, reg, depth + 1))
    if node.get("more"):
        shown, total = node["more"]
        lines.append("  " * (depth + 1) + f"(showing 1-{shown} of {total}; use find())")
    return "\n".join(lines)


def diff(before, after, cap=80):
    out = [f"- {line}" for i, line in before.items() if i not in after]
    for i, line in after.items():
        if i not in before:
            out.append(f"+ {line}")
        elif before[i] != line:
            out.append(f"~ {line}")
    if len(out) > cap:
        out = out[:cap] + [f"... +{len(out) - cap} more changes"]
    return "\n".join(out) or "(no change)"


def canvas_like(flat, win_rect):
    """True when the tree can't describe the UI: few useful nodes or a big canvas surface."""
    useful = [n for n in flat[1:] if n["role"] in INTERACTIVE or (n["role"] == "text" and n["name"])]
    if len(useful) < 5:
        return True
    x0, y0, x1, y1 = win_rect
    area = max(1, (x1 - x0) * (y1 - y0))
    return any(n.get("canvas") and (n["rect"][2] - n["rect"][0]) * (n["rect"][3] - n["rect"][1]) >= 0.4 * area
               for n in flat)


def stale_reason(rec, live, need_rect=False):
    if live is None:
        return "element gone"
    if live["role"] != rec["role"]:
        return f"role changed ({rec['role']} -> {live['role']})"
    if live["name"] != rec["name"]:
        return f"name changed ({rec['name']!r} -> {live['name']!r})"
    if need_rect and any(abs(a - b) > 4 for a, b in zip(rec["rect"], live["rect"])):
        return "moved"
    return None


# ---------- live desktop ----------

P_RID, P_RECT, P_PID, P_TYPE, P_NAME, P_FOCUS, P_ENABLED, P_CLASS = 30000, 30001, 30002, 30003, 30005, 30008, 30010, 30012
P_INVOKE, P_EXPAND_OK, P_SCROLL_OK, P_SEL_OK, P_TOGGLE_OK, P_VALUE_OK = 30031, 30028, 30034, 30036, 30041, 30043
P_VALUE, P_READONLY, P_EXPAND, P_SELECTED, P_TOGGLE = 30045, 30046, 30070, 30079, 30086
_u32 = ctypes.windll.user32


def _enum_titles(t):
    """Visible top-level hwnds whose title contains t (Win32 EnumWindows)."""
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(h, _):
        if _u32.IsWindowVisible(h):
            buf = ctypes.create_unicode_buffer(512)
            _u32.GetWindowTextW(h, buf, 512)
            if t in buf.value.lower():
                out.append(h)
        return True
    _u32.EnumWindows(cb, None)
    return out


def _exe(pid):
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return "?"
    try:
        buf, n = ctypes.create_unicode_buffer(1024), ctypes.c_ulong(1024)
        ok = k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
        return os.path.basename(buf.value) if ok else "?"
    finally:
        k.CloseHandle(h)


class Desk:
    def __init__(self, deny=()):
        import uiautomation as auto
        from uiautomation.uiautomation import _AutomationClient
        self.auto, self.deny = auto, [d.lower() for d in deny]
        self.reg = Registry()
        self.last = {}          # hwnd -> {id: line} at last observe (diff baseline)
        self.flat = {}          # hwnd -> nodes of the latest snapshot (find() cache)
        self.win = None         # current target window (uiautomation Control)
        self.before = set()     # top-level windows before the current action
        self.hidden = set()     # our own overlay windows
        self.confirmed = False  # guardian: risky actions allowed for this run
        self.stop = lambda: None
        self.ct = dict(auto.ControlTypeNames)
        ia = _AutomationClient.instance().IUIAutomation
        cr = ia.CreateCacheRequest()
        for pid in (P_RID, P_RECT, P_PID, P_TYPE, P_NAME, P_FOCUS, P_ENABLED, P_CLASS, P_INVOKE,
                    P_EXPAND_OK, P_SCROLL_OK, P_SEL_OK, P_TOGGLE_OK, P_VALUE_OK, P_VALUE, P_READONLY,
                    P_EXPAND, P_SELECTED, P_TOGGLE):
            cr.AddProperty(pid)
        cr.TreeScope = 7  # element + children + descendants, in one cross-process call
        cr.TreeFilter = ia.CreateAndCondition(ia.ControlViewCondition, ia.CreatePropertyCondition(30022, False))
        self.cr = cr

    # --- windows ---
    def _wins(self):
        out = {}
        for w in self.auto.GetRootControl().GetChildren():
            try:  # transient windows (toasts) can vanish mid-read: COMError
                h = w.NativeWindowHandle
            except Exception:
                continue
            if h and h not in self.hidden:
                out[h] = w
        return out

    @staticmethod
    def _usable(w):
        """A real app window: readable, and not a shell toast/flyout that popped up meanwhile."""
        try:
            return w.Name is not None and _exe(w.ProcessId).lower() not in SHELL_HOSTS
        except Exception:
            return False

    @staticmethod
    def _alive(w):
        try:
            return w is not None and bool(_u32.IsWindow(w.NativeWindowHandle))
        except Exception:
            return False

    def _guard(self, w):
        if any(d in (w.Name or "").lower() for d in self.deny):
            raise PermissionError(f"window {w.Name!r} is on the denylist")

    def resolve(self, target=None):
        """hwnd int, title substring, or None (current target, else foreground window)."""
        if isinstance(target, int):
            w = self.auto.ControlFromHandle(target)
            if not w:
                raise LookupError(f"no window with hwnd {target}")
        elif target:
            t = target.lower()
            ws = [w for w in self._wins().values() if t in (w.Name or "").lower()]
            if not ws:  # UIA root enumeration can transiently miss windows: ask Win32 directly
                ws = [self.auto.ControlFromHandle(h) for h in _enum_titles(t) if h not in self.hidden]
            if not ws:
                raise LookupError(f"no window matching {target!r}; see windows()")
            fg = _u32.GetForegroundWindow()
            w = next((x for x in ws if x.NativeWindowHandle == fg), ws[0])
        elif self._alive(self.win):
            w = self.win
        else:
            w = self.auto.GetForegroundControl().GetTopLevelControl()
        self._guard(w)
        return w

    def windows(self):
        """Top-level windows: hwnd, title, exe, bounds, state."""
        fg, out = _u32.GetForegroundWindow(), []
        for h, w in self._wins().items():
            if not w.Name:
                continue
            r = w.BoundingRectangle
            st = "min" if _u32.IsIconic(h) else "max" if _u32.IsZoomed(h) else "normal"
            out.append(f'hwnd={h} "{_clip(w.Name)}" exe={_exe(w.ProcessId)} '
                       f'[{r.left},{r.top},{r.right},{r.bottom}] {st}{" foreground" if h == fg else ""}')
        return "\n".join(out)

    def mark(self):
        self.before = set(self._wins())

    def _popups(self, w):
        """Owned dialogs and same-process menus shown as separate top-level windows."""
        h, pid, out = w.NativeWindowHandle, w.ProcessId, []
        for ph, p in self._wins().items():
            if ph != h and (_u32.GetAncestor(ph, 3) == h or (p.ProcessId == pid and p.ClassName == "#32768")):
                out.append(p)
        return out

    # --- snapshot ---
    @staticmethod
    def _v(e, pid, default):
        try:
            v = e.GetCachedPropertyValue(pid)
        except Exception:
            return default
        return v if isinstance(v, (bool, int, float, str, tuple)) else default

    def _node(self, e):
        r = e.CachedBoundingRectangle
        v = lambda pid, d=None: self._v(e, pid, d)
        exp = v(P_EXPAND) if v(P_EXPAND_OK, False) else None
        tog = v(P_TOGGLE) if v(P_TOGGLE_OK, False) else None
        sel = v(P_SELECTED) if v(P_SEL_OK, False) else None
        val = v(P_VALUE, "") if v(P_VALUE_OK, False) else ""
        rid = v(P_RID, None)
        return {"rid": tuple(rid) if rid else (id(e),),
                "role": role_name(self.ct.get(e.CachedControlType, "CustomControl")),
                "name": e.CachedName or "", "value": str(val or ""),
                "rect": (r.left, r.top, r.right, r.bottom), "focused": bool(e.CachedHasKeyboardFocus),
                "enabled": bool(e.CachedIsEnabled), "selected": bool(sel) if sel is not None else None,
                "checked": {0: "off", 1: "on", 2: "mixed"}.get(tog),
                "expanded": {0: False, 1: True, 2: True}.get(exp),
                "settable": bool(v(P_VALUE_OK, False)) and not v(P_READONLY, True),
                "scrollable": bool(v(P_SCROLL_OK, False)), "invoke": bool(v(P_INVOKE, False)),
                "cls": e.CachedClassName or "", "el": e, "children": []}

    def _walk(self, e):
        n = self._node(e)
        kids = e.GetCachedChildren()
        if kids:
            n["children"] = [self._walk(kids.GetElement(i)) for i in range(kids.Length)]
        return n

    def _tree(self, w):
        raw = self._walk(w.Element.BuildUpdatedCache(self.cr))
        x0, y0, x1, y1 = raw["rect"]
        raw["children"] = _prune_list(raw["children"], max(1, (x1 - x0) * (y1 - y0)))
        return collapse_lists(raw)

    def _snap(self, w):
        """Tree of the window + its popups; assigns ids (snapshot-bound) -> (trees, flat)."""
        trees = [self._tree(w)] + [self._tree(p) for p in self._popups(w)]
        flat = [n for t in trees for n in flatten(t)]
        self.reg.assign(w.NativeWindowHandle, flat)
        self.flat[w.NativeWindowHandle] = flat
        return trees, flat

    def observe(self, target=None, mode="tree"):
        """-> dict(text, canvas, hwnd, rect, exe)."""
        w = self.resolve(target)
        self.win = w
        h = w.NativeWindowHandle
        trees, flat = self._snap(w)
        lines = self.reg.lines(flat)
        exe = _exe(w.ProcessId)
        r = trees[0]["rect"]
        head = f'window "{_clip(w.Name or "")}" hwnd={h} exe={exe} [{r[0]},{r[1]},{r[2]},{r[3]}]'
        canvas = canvas_like(flatten(trees[0]), r)
        if mode == "diff" and h in self.last:
            body = diff(self.last[h], lines)
        else:
            body = "\n".join(render(t, self.reg) if i == 0 else "popup:\n" + render(t, self.reg, 1)
                             for i, t in enumerate(trees))
            focused = next((self.reg.id_of(n) for n in flat if n["focused"]), None)
            if focused:
                body += f"\nfocused: {focused}"
        self.last[h] = lines
        if canvas:
            body += "\n(canvas/low-a11y window: pixels matter -> observe(mode='shot'|'ocr') or show())"
        return {"text": head + "\n" + body, "canvas": canvas, "hwnd": h, "rect": r, "exe": exe}

    def after_action(self):
        """Validate step: full tree of a new unrelated window, else diff of the target (+popups)."""
        time.sleep(0.12)
        new = [w for h, w in self._wins().items() if h not in self.before]
        if new and self._alive(self.win):
            owned = [w for w in new if _u32.GetAncestor(w.NativeWindowHandle, 3) == self.win.NativeWindowHandle]
            if len(owned) < len(new):
                fg = _u32.GetForegroundWindow()
                self.win = next((w for w in new if w.NativeWindowHandle == fg), new[-1])
                return cap_lines(self.observe()["text"])
        elif new:
            self.win = new[-1]
            return cap_lines(self.observe()["text"])
        if not self._alive(self.win):
            self.win = None
            return "target window closed\n" + cap_lines(self.observe()["text"])
        return self.observe(mode="diff")["text"]

    # --- element access (fail closed) ---
    def _rec(self, i):
        rec = self.reg.rec.get(i)
        if rec is None:
            raise StaleTarget(f"unknown id {i}; observe first")
        if i not in self.reg.current.get(rec["hwnd"], ()):
            raise StaleTarget(f"id {i} is not in the latest snapshot; observe again")
        return rec

    def _live(self, rec):
        e = rec["el"]
        try:
            r = e.CurrentBoundingRectangle
            return {"role": role_name(self.ct.get(e.CurrentControlType, "CustomControl")),
                    "name": e.CurrentName or "", "rect": (r.left, r.top, r.right, r.bottom)}
        except Exception:
            return None

    def _checked(self, i, need_rect=False):
        rec = self._rec(i)
        live = self._live(rec)
        why = stale_reason(rec, live, need_rect)
        if why:
            raise StaleTarget(f"id {i} {why}; observe again")
        return rec, live

    def _match(self, flat, role, name):
        r = role.lower().replace(" ", "") if role else None
        t = name.lower() if name else None
        hits = [n for n in flat if (not r or n["role"].replace(" ", "") == r) and
                (not t or t in " ".join([n["name"], n["value"], *n.get("extra", [])]).lower())]
        if t:
            hits.sort(key=lambda n: n["name"].lower() != t)  # exact names first
        return hits

    def find(self, role=None, name=None, raw=False, fresh=False):
        """Search the target window (+popups, + windows opened by this action). Uses the latest
        snapshot first (hits are re-validated live before any action), then a fresh one."""
        w = self.resolve()
        h = w.NativeWindowHandle
        hits = []
        if not fresh and h in self.flat:
            hits = [n for n in self._match(self.flat[h], role, name) if self.reg.id_of(n) in self.reg.current[h]]
        if not hits:
            _, flat = self._snap(w)
            hits = self._match(flat, role, name)
        if not hits:
            for h, x in self._wins().items():
                if h not in self.before and h != w.NativeWindowHandle:
                    _, f2 = self._snap(x)
                    hits = self._match(f2, role, name)
                    if hits:
                        self.win = x
                        break
        ids = [self.reg.id_of(n) for n in hits]
        if raw:
            return ids
        return "\n".join(node_line(i, n) for i, n in zip(ids, hits)) or "(none)"

    def _id(self, i, name, role):
        if i is not None:
            return i
        ids = self.find(role=role, name=name, raw=True)
        if not ids:
            raise LookupError(f"no element name={name!r} role={role!r}")
        return ids[0]

    def _ctl(self, rec):
        return self.auto.Control.CreateControlFromElement(rec["el"])

    def _top_of(self, rec):
        return self.auto.ControlFromHandle(rec["hwnd"])

    def _front(self, w=None):
        """Keys go to the foreground window: bring the target there (keeps maximized state)."""
        w = w or self.resolve()
        h = w.NativeWindowHandle
        fg = _u32.GetForegroundWindow()
        if fg == h or _u32.GetAncestor(fg, 3) == h:
            return
        inputs.touch()  # stealing the foreground is real input: shared-mode guard applies
        if _u32.IsIconic(h):
            _u32.ShowWindow(h, 9)  # SW_RESTORE
        if not _u32.SetForegroundWindow(h):
            zoomed = _u32.IsZoomed(h)
            _u32.ShowWindow(h, 6)                    # SW_MINIMIZE: lifts the foreground lock
            _u32.ShowWindow(h, 3 if zoomed else 9)   # SW_MAXIMIZE / SW_RESTORE
        time.sleep(0.25)
        fg = _u32.GetForegroundWindow()
        if fg != h and _u32.GetAncestor(fg, 3) != h:
            raise RuntimeError(f"could not bring {w.Name!r} to front; input not sent")

    # --- actions (REPL helpers) ---
    def click(self, id=None, name=None, role=None, button="left", double=False):
        """Invoke when the element supports it (works in background), else a real mouse click."""
        self.stop()
        i = self._id(id, name, role)
        rec, _ = self._checked(i)
        if risky(rec["name"]) and not self.confirmed:
            raise GuardBlocked(f"clicking {rec['name']!r} is risky: ask the user, then run(..., confirm=True)")
        if button == "left" and not double and rec.get("invoke"):
            try:
                self._ctl(rec).GetInvokePattern().Invoke(waitTime=0)
                return
            except Exception:
                pass
        rec, live = self._checked(i, need_rect=True)
        x0, y0, x1, y1 = live["rect"]
        self._front(self._top_of(rec))
        inputs.click_at((x0 + x1) // 2, (y0 + y1) // 2, button, 2 if double else 1)

    def dclick(self, id=None, **kw):
        self.click(id, double=True, **kw)

    def rclick(self, id=None, **kw):
        self.click(id, button="right", **kw)

    def set_value(self, id=None, value="", name=None, role=None):
        rec, _ = self._checked(self._id(id, name, role))
        if not rec.get("settable"):
            raise ValueError(f"id {id} is not settable")
        self._ctl(rec).GetValuePattern().SetValue(value, waitTime=0)

    def action(self, id=None, what="invoke", name=None, role=None):
        """Semantic action: invoke | toggle | select | expand | collapse | scroll_into_view | focus."""
        rec, _ = self._checked(self._id(id, name, role))
        c = self._ctl(rec)
        if what == "focus":
            return c.SetFocus()
        get, meth = {"invoke": ("GetInvokePattern", "Invoke"), "toggle": ("GetTogglePattern", "Toggle"),
                     "select": ("GetSelectionItemPattern", "Select"),
                     "expand": ("GetExpandCollapsePattern", "Expand"),
                     "collapse": ("GetExpandCollapsePattern", "Collapse"),
                     "scroll_into_view": ("GetScrollItemPattern", "ScrollIntoView")}[what]
        p = getattr(c, get)()
        if not p:
            raise ValueError(f"element does not support {what}")
        m = getattr(p, meth)
        return m() if what == "scroll_into_view" else m(waitTime=0)

    def type(self, text, id=None, name=None, role=None, enter=False):
        """With a target: SetValue if settable (instant, background) else focus + keys.
        Without: unicode keystrokes into the foreground target window."""
        w = None
        if id is not None or name is not None:
            rec, _ = self._checked(self._id(id, name, role))
            w = self._top_of(rec)
            if rec.get("settable"):
                try:
                    self._ctl(rec).GetValuePattern().SetValue(text, waitTime=0)
                    if enter:
                        self._front(w)
                        inputs.press("enter")
                    return
                except Exception:
                    pass
            try:
                self._ctl(rec).SetFocus()
            except Exception:
                pass
        self._front(w)
        inputs.text(text)
        if enter:
            inputs.press("enter")

    def key(self, spec, times=1):
        """Shortcut into the target window via scan codes: key('ctrl+s'), key('enter', times=3)."""
        self._front()
        inputs.press(spec, times)

    def scroll(self, id=None, dir="down", n=3, name=None, role=None):
        _, live = self._checked(self._id(id, name, role))
        x0, y0, x1, y1 = live["rect"]
        inputs.move((x0 + x1) // 2, (y0 + y1) // 2)
        inputs.wheel(n if dir == "up" else -n)

    def wait_for(self, name, role=None, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            self.stop()
            ids = self.find(role=role, name=name, raw=True, fresh=True)
            if ids:
                return ids[0]
            time.sleep(0.2)
        raise TimeoutError(f"{name!r} not found in {timeout}s")

    def focus(self, target):
        w = self.resolve(target)
        self._front(w)
        self.win = w
        return w.Name

    def app(self, cmd, title=None, timeout=15.0):
        """Launch a program / path / URI (e.g. shell:AppsFolder\\<AUMID>) and target its window."""
        before = set(self._wins())
        if re.match(r"^[a-z][a-z0-9+.-]*:(?![\\/])", cmd, re.I):
            os.startfile(cmd)  # URI / shell: activation runs outside our process tree
        else:
            self._spawn(f'"{cmd}"' if os.path.exists(cmd) else cmd)
        end = time.time() + timeout
        while time.time() < end:
            self.stop()
            time.sleep(0.3)
            new = [w for h, w in self._wins().items() if h not in before and self._usable(w)
                   and (not title or title.lower() in (w.Name or "").lower())]
            if new:
                w = new[0]
                self._guard(w)
                try:
                    self._front(w)
                except Exception:
                    pass
                self.win = w
                self.before = set(self._wins())
                return w.Name
        raise TimeoutError(f"no new window after launching {cmd!r}")

    def wait_window(self, title=None, exe=None, timeout=30.0):
        """Block until a top-level window matching title substring and/or exe name exists, then target it."""
        t, e, end = (title or "").lower(), (exe or "").lower(), time.time() + timeout
        while time.time() < end:
            self.stop()
            for w in self._wins().values():
                try:
                    ok = self._usable(w) and t in (w.Name or "").lower() and (not e or _exe(w.ProcessId).lower() == e)
                except Exception:
                    continue
                if ok:
                    self._guard(w)
                    self.win = w
                    return w.Name
            time.sleep(0.5)
        raise TimeoutError(f"no window title~{title!r} exe={exe!r} after {timeout}s")

    @staticmethod
    def _spawn(cmd):
        """Launch outside our process tree: MCP clients put the server in a kill-on-close job, so
        a plain child app would die (unsaved work included) when the session ends. Never inherit
        our stdio either: under MCP stdio it would corrupt/hold the protocol pipe."""
        try:  # WMI (WmiPrvSE) creates it in this session, outside any job we are in (nested
            # jobs make CREATE_BREAKAWAY_FROM_JOB silently keep the child in the inner job)
            import win32com.client
            wmi = win32com.client.GetObject("winmgmts:")
            inp = wmi.Get("Win32_Process").Methods_("Create").InParameters.SpawnInstance_()
            inp.CommandLine = cmd
            if wmi.ExecMethod("Win32_Process", "Create", inp).ReturnValue == 0:
                return
        except Exception:
            pass
        io = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        try:  # shell syntax WMI can't run: best effort breakaway
            subprocess.Popen(cmd, shell=True, **io, creationflags=subprocess.DETACHED_PROCESS
                             | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB)
        except OSError:
            subprocess.Popen(cmd, shell=True, **io, creationflags=subprocess.DETACHED_PROCESS)

    def sh(self, cmd, timeout=30):
        if not self.confirmed:
            raise GuardBlocked("sh() runs shell commands: ask the user, then run(..., confirm=True)")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()[:2000]
