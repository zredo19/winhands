"""Exo desktop widget: a translucent, always-on-top Qt WebEngine window docked at the screen edge.

Mic (click or Ctrl+Alt+Space) -> record until you stop talking -> Whisper -> Claude Code (+ winhands) ->
speech bubble + Pablo's voice. The window never takes the keyboard focus (unless you type in it), so
"this window" keeps meaning the app you were using. Fully transparent pixels are click-through.
"""
import ctypes, ctypes.wintypes as wt, json, os, pathlib, sys, threading, time

UI = pathlib.Path(__file__).with_name("ui") / "index.html"
SIZES = {"compact": (240, 360), "open": (430, 660)}  # small when idle: a translucent window repaints every pixel
TITLE = "Exo"
HOTKEY = "Ctrl+Alt+Space"
_u32 = ctypes.windll.user32


def log(line):
    from .brain import HOME
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        with open(HOME / "exo.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except OSError:
        pass


def single_instance():
    """False when another Exo is already running (named mutex lives as long as this process)."""
    global _mutex
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\ExoVoiceWidget")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def foreground():
    """(title, exe) of the window the user was using when they called Exo (context for "this window")."""
    from ..uia import _exe, _pid, _title
    h = _u32.GetForegroundWindow()
    return (_title(h), _exe(_pid(h))) if h else ("", "")


class Exo:
    """Glue between the page (bridge slots), the voice pipeline and Claude Code. emit() is thread-safe."""

    def __init__(self, emit):
        from .brain import Brain
        from .voice import Ears, Mouth, Recorder
        self._emit = emit
        self.brain = Brain(self._on_brain)
        self.ears, self.mouth = Ears(), Mouth()
        self.rec = Recorder(on_level=lambda lv: self.emit("level", round(min(1.0, lv * 12), 3)))
        self.state, self.listening, self.reply, self.voice_on = "idle", False, "", True
        self._ctx = ("", "")

    def emit(self, kind, data=None):
        self._emit(f"window.exo && exo.on({json.dumps(kind)}, {json.dumps(data, ensure_ascii=False)})")

    def set_state(self, s):
        self.state = s
        self.emit("state", s)

    # --- page actions ---
    def toggle(self):
        if self.listening:
            self.rec.stop()
            return
        if self.state in ("thinking", "working"):
            self.brain.cancel()
            self.set_state("idle")
            self.emit("bubble", {"who": "exo", "text": "Ok, paré."})
            return
        self.mouth.stop()
        threading.Thread(target=self._listen, daemon=True).start()

    def send_text(self, text):
        text = (text or "").strip()
        if text:
            self._ctx = ("", "")
            self._ask(text)

    def set_voice(self, on):
        self.voice_on = bool(on)
        if not on:
            self.mouth.stop()

    def reset(self):
        self.brain.reset()
        self.mouth.stop()
        self.set_state("idle")
        self.emit("reset")

    def shutdown(self):
        self.brain.cancel()
        self.mouth.stop()

    # --- flow ---
    def _listen(self):
        self.listening = True
        self._ctx = foreground()
        self.set_state("listening")
        try:
            audio = self.rec.record()
        except Exception as e:
            audio = None
            self.emit("bubble", {"who": "exo", "text": f"No pude usar el micrófono: {e}", "ok": False})
        finally:
            self.listening = False
        if audio is None:
            self.set_state("idle")
            return
        self.set_state("thinking")
        text = self.ears.transcribe(audio)
        if not text:
            self.set_state("idle")
            return
        self._ask(text)

    def _ask(self, text):
        self.emit("bubble", {"who": "user", "text": text})
        title, exe = self._ctx
        ctx = f'[The user was in the window "{title}" ({exe}) when they spoke.]\n' if title else ""
        self.reply = ""
        self.set_state("thinking")
        try:
            self.brain.ask(ctx + text)
        except Exception as e:
            self.emit("bubble", {"who": "exo", "text": f"No pude hablar con Claude Code: {e}", "ok": False})
            self.set_state("error")

    def _on_brain(self, kind, data):
        from .brain import spoken
        if kind in ("log", "exit", "ready", "tool", "done"):
            log(f"brain {kind}: {str(data)[:500]}")
        if kind == "delta":
            self.reply += data
            self.emit("stream", self.reply)
        elif kind == "tool" and data.startswith("mcp__winhands"):
            self.set_state("working")
        elif kind == "text":
            self.reply = ""
        elif kind == "done":
            text = spoken(data["text"]) or ("Listo." if data["ok"] else "Algo falló.")
            self.emit("bubble", {"who": "exo", "text": text, "ok": data["ok"]})
            if not data["ok"]:
                self.set_state("error")
                return
            if self.voice_on:
                self.set_state("speaking")
                try:
                    secs = self.mouth.say(text)
                except Exception:
                    secs = 0
                end = time.time() + secs
                while time.time() < end and self.state == "speaking":
                    time.sleep(0.1)
            if self.state in ("speaking", "thinking", "working"):
                self.set_state("success")
                time.sleep(1.4)
                if self.state == "success":
                    self.set_state("idle")


def _take_focus(hwnd):
    """SetForegroundWindow is refused to a window that never had the focus: borrow the input
    queue of the current foreground thread for the call."""
    fg = _u32.GetForegroundWindow()
    them, me = _u32.GetWindowThreadProcessId(fg, None), ctypes.windll.kernel32.GetCurrentThreadId()
    attached = them != me and _u32.AttachThreadInput(me, them, True)
    try:
        _u32.BringWindowToTop(hwnd)
        _u32.SetForegroundWindow(hwnd)
        _u32.SetFocus(hwnd)
    finally:
        if attached:
            _u32.AttachThreadInput(me, them, False)


def _hotkey(on_press):
    """Ctrl+Alt+Space from anywhere (RegisterHotKey needs its own message loop)."""
    if not _u32.RegisterHotKey(None, 1, 0x0002 | 0x0001 | 0x4000, 0x20):  # CTRL|ALT|NOREPEAT, VK_SPACE
        return
    msg = wt.MSG()
    while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == 0x0312:  # WM_HOTKEY
            on_press()


def main():
    # Software compositing: GPU compositing of a translucent window drops a blank frame every few seconds (Exo
    # "blinks"); measured 3 blank frames / 40 s on GPU vs 0 / 90 s in software.
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --disable-gpu-compositing")
    from PySide6.QtCore import QObject, Qt, QUrl, Signal, Slot
    from PySide6.QtGui import QColor, QIcon
    from PySide6.QtWebChannel import QWebChannel
    from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication

    if not single_instance():
        return
    log("start")
    app = QApplication(sys.argv)
    app.setApplicationName("Exo")
    view = QWebEngineView()

    class Bridge(QObject):
        js = Signal(str)  # worker threads -> GUI thread

        def __init__(self):
            super().__init__()
            self.js.connect(lambda code: view.page().runJavaScript(code))
            self.exo = Exo(self.js.emit)

        @Slot()
        def toggle(self):
            self.exo.toggle()

        @Slot(str)
        def send_text(self, text):
            self.exo.send_text(text)

        @Slot(bool)
        def set_voice(self, on):
            self.exo.set_voice(on)

        @Slot()
        def reset(self):
            self.exo.reset()

        @Slot(bool)
        def keyboard(self, on):
            """Typing needs the focus: drop WS_EX_NOACTIVATE while the text box is in use."""
            _no_activate(not on)
            if on:
                _take_focus(int(view.winId()))

        @Slot(str)
        def resize(self, size):
            """Grow for the conversation panel, shrink back when it closes (bottom-right corner stays put)."""
            if size not in SIZES:
                return
            w, h = SIZES[size]
            g = view.geometry()
            view.setGeometry(g.right() - w + 1, g.bottom() - h + 1, w, h)

        @Slot()
        def drag(self):
            view.windowHandle().startSystemMove()

        @Slot()
        def quit(self):
            self.exo.shutdown()
            app.quit()

        @Slot()
        def ready(self):
            self.exo.emit("hello", {"hotkey": HOTKEY})
            self.exo.set_state("idle")

    def _no_activate(on):
        h = int(view.winId())
        ex = _u32.GetWindowLongW(h, -20)
        ex = ex | 0x08000000 if on else ex & ~0x08000000  # WS_EX_NOACTIVATE
        _u32.SetWindowLongW(h, -20, ex)

    bridge = Bridge()
    channel = QWebChannel(view.page())
    channel.registerObject("bridge", bridge)
    view.page().setWebChannel(channel)

    view.setWindowTitle(TITLE)
    icon = UI.with_name("icon.png")
    if icon.exists():
        view.setWindowIcon(QIcon(str(icon)))
    view.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
    view.setAttribute(Qt.WA_TranslucentBackground)
    view.setAttribute(Qt.WA_ShowWithoutActivating)
    view.page().setBackgroundColor(QColor(0, 0, 0, 0))
    view.setContextMenuPolicy(Qt.NoContextMenu)
    w, h = SIZES["compact"]
    geo = app.primaryScreen().availableGeometry()
    view.setGeometry(geo.right() - w - 4, geo.bottom() - h - 2, w, h)
    view.load(QUrl.fromLocalFile(str(UI)))
    view.show()
    _no_activate(True)

    threading.Thread(target=_hotkey, args=(bridge.exo.toggle,), daemon=True).start()
    threading.Thread(target=bridge.exo.brain.start, daemon=True).start()  # warm up Claude Code (~8 s)
    app.aboutToQuit.connect(bridge.exo.shutdown)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
