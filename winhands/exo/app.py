"""Exo desktop widget: a translucent, always-on-top Qt WebEngine window docked at the screen edge.

Mic (click or Ctrl+Alt+Space) -> record until you stop talking -> Whisper -> Claude Code (+ winhands) ->
speech bubble + Pablo's voice. The window never takes the keyboard focus (unless you type in it), so
"this window" keeps meaning the app you were using. Fully transparent pixels are click-through.
"""
import ctypes, ctypes.wintypes as wt, json, os, pathlib, sys, threading, time

UI = pathlib.Path(__file__).with_name("ui") / "index.html"
SIZES = {"compact": (240, 360), "open": (430, 672)}  # small when idle: a translucent window repaints every pixel
TITLE = "Exo"
HOTKEY = "Ctrl+Alt+Space"
IDLE = 5 * 60  # unused this long: Whisper, Kokoro and Claude Code go (~1 GB) until the next request
# Software compositing: GPU compositing of a translucent window drops a blank frame every few seconds (Exo
# "blinks"); measured 3 blank frames / 40 s on GPU vs 0 / 90 s in software. expose-gc: mascot.js collects garbage.
CHROMIUM_FLAGS = "--disable-gpu --disable-gpu-compositing --js-flags=--expose-gc"
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
        from .brain import HOME, Brain, load_settings
        from .voice import CloudEars, Ears, Mouth, Recorder
        self._emit = emit
        self.prefs_path, self.chats_dir, self.chat = HOME / "preferences.json", HOME / "chats", None
        self.prefs = load_settings(self.prefs_path)
        self.brain = Brain(self._on_brain, model=os.environ.get("EXO_MODEL") or self.prefs["model"])
        key = os.environ.get("GROQ_API_KEY")  # speech-to-text in the cloud (free tier) instead of on this PC
        self.ears, self.mouth = CloudEars(key) if key else Ears(), Mouth(voice=os.environ.get("EXO_VOICE") or self.prefs["voice_name"])
        self.rec = Recorder(on_level=lambda lv: self.emit("level", round(min(1.0, lv * 12), 3)))
        self.state, self.listening, self.reply, self.voice_on = "idle", False, "", self.prefs["voice"]
        self._ctx = ("", "")
        self.turn = 0  # bumped by a stop: work started before it is dropped
        self.reply, self.ack = "", None
        self.awake, self.last_used = False, time.time()

    def emit(self, kind, data=None):
        if kind == "bubble":
            self._record(data)
        self._emit(f"window.exo && exo.on({json.dumps(kind)}, {json.dumps(data, ensure_ascii=False)})")

    def set_state(self, s):
        self.state = s
        self.emit("state", s)

    # --- page actions ---
    def toggle(self):
        if self.listening:
            self.rec.stop()
            return
        if self.state in ("thinking", "working", "speaking"):  # busy: the mic button is a stop button
            self.turn += 1                                     # drops a transcription still in flight
            self.brain.interrupt()                             # Claude stops; the session stays warm
            self.mouth.stop()
            self.set_state("idle")
            self.emit("bubble", {"who": "exo", "text": "Ok, paré.", "kind": "status"})
            return
        self.wake()  # Whisper and Claude Code load while the user talks
        threading.Thread(target=self._listen, daemon=True).start()

    def send_text(self, text):
        text = (text or "").strip()
        if text:
            self.wake()
            self._ctx = ("", "")
            self._ask(text)

    # --- memory: let the heavy parts go when unused ---
    def wake(self):
        self.last_used = time.time()
        if not self.awake:
            threading.Thread(target=self._warm, daemon=True).start()

    def _warm(self):
        self.awake = True
        self.ears.start()
        self.mouth.warm()
        self.brain.start()

    def doze(self, now=None):
        """True when it just let Whisper, Kokoro and Claude Code go (never mid-turn; the session resumes later)."""
        busy = self.listening or self.state in ("thinking", "working", "speaking") or self.mouth.busy() or self.brain.busy
        if not self.awake or busy or (now or time.time()) - self.last_used < IDLE:
            return False
        self.awake = False
        self.ears.close()
        self.mouth.unload()
        self.brain.cancel()
        return True

    def set_voice(self, on):
        self.voice_on = bool(on)
        if not on:
            self.mouth.stop()
        self._save()

    def set_model(self, model):
        from .brain import MODELS
        if model not in MODELS or model == self.brain.model:
            return
        if self.state in ("thinking", "working"):
            self.turn += 1
        self.brain.set_model(model)  # same conversation, new model (Claude Code restarts in the background)
        self.set_state("idle")
        self.emit("bubble", {"who": "exo", "text": f"Ahora pienso con {MODELS[model]['label']}", "kind": "status"})
        self._save()

    def set_voice_name(self, key):
        from .voice import VOICES
        if key not in VOICES:
            return
        self.mouth.voice, self.voice_on = key, True
        self._save()
        self.wake()
        self.mouth.say("Hola, soy Exo. Así sueno ahora.")

    def settings(self):
        from .brain import MODELS
        from .voice import VOICES
        return {"model": self.brain.model, "voice": self.voice_on, "voice_name": self.mouth.voice,
                "models": [{"key": k, **v} for k, v in MODELS.items()],
                "voices": [{"key": k, "label": v[2]} for k, v in VOICES.items()]}

    def _save(self):
        from .brain import save_settings
        save_settings(self.prefs_path, {"model": self.brain.model, "voice": self.voice_on, "voice_name": self.mouth.voice})

    def reset(self):
        self.chat = None
        self.brain.reset()
        self.mouth.stop()
        self.set_state("idle")
        self.emit("reset")

    # --- history: every bubble also lands in ~/.exo/chats/<id>.json ---
    def _record(self, bubble):
        if self.chat is None:
            if bubble.get("who") != "user":  # a status chip before anyone spoke starts no chat
                return
            self.chat = {"id": str(time.time_ns()), "title": bubble["text"][:80], "messages": []}
        self.chat["messages"].append({**bubble, "at": int(time.time() * 1000)})
        self.chat["session"] = self.brain.session  # the Claude Code session to --resume when it is reopened
        try:
            self.chats_dir.mkdir(parents=True, exist_ok=True)
            (self.chats_dir / f"{self.chat['id']}.json").write_text(json.dumps(self.chat, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            log(f"history: {e}")

    def _load(self, chat_id):
        try:
            return json.loads((self.chats_dir / f"{chat_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def chats(self):
        """Saved conversations, newest first. ponytail: reads every file, an index file if there are thousands."""
        current = self.chat and self.chat["id"]
        out = []
        for p in self.chats_dir.glob("*.json"):
            c = self._load(p.stem)
            if c and c.get("messages"):
                out.append({"id": c["id"], "title": c["title"], "at": c["messages"][-1]["at"], "on": c["id"] == current})
        return sorted(out, key=lambda c: c["at"], reverse=True)

    def open_chat(self, chat_id):
        """Show a saved conversation; talking again continues it on its Claude Code session."""
        chat = self._load(chat_id) if str(chat_id).isdigit() else None
        if not (chat and chat.get("messages")):
            return
        self.turn += 1  # drops a transcription still in flight
        self.reset()
        self.chat, self.brain.session = chat, chat.get("session")
        self.emit("history", chat["messages"])

    def shutdown(self):
        self.brain.cancel()
        self.ears.close()
        self.mouth.stop()

    # --- flow ---
    def _listen(self):
        turn = self.turn
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
        try:
            text = self.ears.transcribe(audio)
        except Exception as e:
            log(f"whisper: {e}")
            self.emit("bubble", {"who": "exo", "text": "No pude entender el audio.", "ok": False})
            self.set_state("error")
            return
        if turn != self.turn:  # stopped while transcribing
            return
        if not text:
            self.set_state("idle")
            return
        self._ask(text)

    def _ask(self, text):
        self.emit("bubble", {"who": "user", "text": text})
        title, exe = self._ctx
        ctx = f'[The user was in the window "{title}" ({exe}) when they spoke.]\n' if title else ""
        self.reply, self.ack = "", None
        self.set_state("thinking")
        try:
            self.brain.ask(ctx + text)
        except Exception as e:
            self.emit("bubble", {"who": "exo", "text": f"No pude hablar con Claude Code: {e}", "ok": False})
            self.set_state("error")

    def _say(self, text):
        """Speak without blocking (mouth.busy() tells when it is done)."""
        if self.voice_on:
            self.mouth.say(text)

    def _on_brain(self, kind, data):
        from .brain import first_sentence, spoken, without_ack
        if kind in ("log", "exit", "ready", "tool", "done"):
            log(f"brain {kind}: {str(data)[:500]}")
        if kind == "delta":
            self.reply += data
            if self.ack is None and first_sentence(self.reply):
                self.ack = first_sentence(self.reply)  # "Entendido, abro Discord.": say it now, act after
                self.emit("bubble", {"who": "exo", "text": spoken(self.ack), "kind": "ack"})
                self._say(spoken(self.ack))
            rest = without_ack(self.reply, self.ack)
            if rest:
                self.emit("stream", rest)
        elif kind == "tool" and data.startswith("mcp__winhands"):
            self.set_state("working")
        elif kind == "text":
            self.reply = ""
        elif kind == "done":
            self.last_used = time.time()
            if data.get("cancelled"):  # stopped by the user: "Ok, paré." was already shown
                return
            ack = spoken(self.ack) if self.ack else None
            text = without_ack(spoken(data["text"]), ack)
            if not data["ok"]:
                self.emit("bubble", {"who": "exo", "text": text or "Algo falló.", "ok": False})
                self.set_state("error")
                return
            if text or not ack:
                text = text or "Listo."
                self.emit("bubble", {"who": "exo", "text": text, "ok": True})
            if self.voice_on and text:
                while self.mouth.busy() and self.state != "idle":  # let the acknowledgement finish
                    time.sleep(0.1)
                self.set_state("speaking")
                self._say(text)
                while self.mouth.busy() and self.state == "speaking":
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


def _doze_loop(exo):
    while True:
        time.sleep(30)
        if exo.doze():
            log("doze: Whisper, voice and Claude Code unloaded")


def main():
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", CHROMIUM_FLAGS)
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

        @Slot(str)
        def set_voice_name(self, key):
            self.exo.set_voice_name(key)

        @Slot(str)
        def set_model(self, model):
            threading.Thread(target=self.exo.set_model, args=(model,), daemon=True).start()

        @Slot()
        def reset(self):
            self.exo.reset()

        @Slot()
        def list_chats(self):
            self.exo.emit("chats", self.exo.chats())

        @Slot(str)
        def open_chat(self, chat_id):
            self.exo.open_chat(chat_id)

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
            self.exo.emit("settings", self.exo.settings())
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
    bridge.exo.wake()  # load Whisper, the voice and Claude Code (~8 s) so the first request is quick
    threading.Thread(target=_doze_loop, args=(bridge.exo,), daemon=True).start()
    app.aboutToQuit.connect(bridge.exo.shutdown)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
