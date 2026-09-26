"""Takeover overlay: red border + banner while a run controls the PC (like Codex's banner).

Click-through (WS_EX_TRANSPARENT), topmost, never activated (WS_EX_NOACTIVATE) and excluded
from screen capture (WDA_EXCLUDEFROMCAPTURE), so it never steals focus or appears in shots.
Runs its own Tk thread; show()/hide() are thread-safe. Failure to start is non-fatal.
"""
import ctypes, queue, threading


class Overlay:
    def __init__(self, text="astra-cu is controlling this PC  -  Ctrl+Alt+Q to stop"):
        self.text, self.q, self.hwnd, self.error = text, queue.Queue(), None, None
        self.ready = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="astra-overlay").start()

    def show(self):
        self.q.put(True)

    def hide(self):
        self.q.put(False)

    def _run(self):
        try:
            import tkinter as tk
            u = ctypes.windll.user32
            vx, vy, vw, vh = (u.GetSystemMetrics(i) for i in (76, 77, 78, 79))
            key = "#ff00fe"
            root = tk.Tk()
            root.overrideredirect(True)
            root.configure(bg=key)
            root.attributes("-transparentcolor", key, "-topmost", True)
            root.geometry(f"{vw}x{vh}+{vx}+{vy}")
            c = tk.Canvas(root, bg=key, highlightthickness=0, width=vw, height=vh)
            c.pack()
            c.create_rectangle(3, 3, vw - 4, vh - 4, outline="#ff3030", width=6)
            px, pw = -vx, u.GetSystemMetrics(0)          # primary monitor origin in canvas coords
            c.create_rectangle(px + pw // 2 - 290, 10 - vy, px + pw // 2 + 290, 44 - vy, fill="#ff3030", outline="")
            c.create_text(px + pw // 2, 27 - vy, text=self.text, fill="white", font=("Segoe UI", 11, "bold"))
            root.update_idletasks()
            h = u.GetParent(root.winfo_id()) or root.winfo_id()
            ex = u.GetWindowLongW(h, -20)
            # LAYERED | TRANSPARENT (click-through) | NOACTIVATE | TOOLWINDOW (no taskbar) | TOPMOST
            u.SetWindowLongW(h, -20, ex | 0x80000 | 0x20 | 0x8000000 | 0x80 | 0x8)
            u.SetWindowDisplayAffinity(h, 0x11)          # WDA_EXCLUDEFROMCAPTURE (Win10 2004+)
            u.ShowWindow(h, 0)
            self.hwnd = h
            self.ready.set()

            def poll():
                try:
                    while True:
                        on = self.q.get_nowait()
                        u.ShowWindow(h, 4 if on else 0)  # SW_SHOWNOACTIVATE / SW_HIDE
                        if on:
                            u.SetWindowPos(h, -1, 0, 0, 0, 0, 0x13)  # TOPMOST, NOSIZE|NOMOVE|NOACTIVATE
                except queue.Empty:
                    pass
                root.after(30, poll)

            poll()
            root.mainloop()
        except Exception as e:  # overlay is cosmetic: never break the server
            self.error = repr(e)
            self.ready.set()
