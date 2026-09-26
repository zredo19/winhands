"""astra-cu: Astra-style computer use MCP server (a11y tree first, code mode, auto-validate)."""
import ast, ctypes, ctypes.wintypes, io, threading, traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout

from mcp.server.mcpserver import Image, MCPServer

from uia import Desk

DENY = ["bitwarden", "1password", "keepass", "lastpass", "banco", "bank"]

mcp = MCPServer("astra-cu")
state = {"desk": None, "tid": None, "ns": None, "busy": False, "killed": False}


class Killed(BaseException):
    pass


def _init():
    import uiautomation as auto, mss, PIL.Image  # noqa: F401  warm imports: first shot fast
    state["com"] = auto.UIAutomationInitializerInThread()
    state["tid"] = threading.get_ident()
    d = Desk(DENY)
    d.stop = _check
    state["desk"] = d


# one dedicated thread owns COM/UIA for the whole server life (fast, no re-init)
EXEC = ThreadPoolExecutor(1, initializer=_init)


def _check():
    if state["killed"]:
        raise Killed("stopped by Ctrl+Alt+Q")


def _interrupt(exc):
    """Raise exc inside the worker thread (timeout / kill switch)."""
    if state["busy"] and state["tid"]:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(state["tid"]), ctypes.py_object(exc))


def _hotkey_loop():
    u = ctypes.windll.user32
    if not u.RegisterHotKey(None, 1, 0x0002 | 0x0001, ord("Q")):  # MOD_CONTROL|MOD_ALT
        return
    msg = ctypes.wintypes.MSG()
    while u.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == 0x0312:  # WM_HOTKEY
            state["killed"] = True
            _interrupt(Killed)


HELPERS = ("click", "dclick", "rclick", "type", "key", "scroll", "click_xy", "find",
           "wait_for", "focus", "app", "sh")


def _exec(code):
    d = state["desk"]
    if state["ns"] is None:
        state["ns"] = {"desk": d, **{f: getattr(d, f) for f in HELPERS}}
    ns, out = state["ns"], io.StringIO()

    def _print(*a, **k):
        k.pop("file", None)
        print(*a, file=out, **k)
    ns["print"] = _print  # per-call buffer; never touches the MCP stdout
    ns["observe"] = lambda target=None, mode="tree": _print(d.observe(target, mode))
    state["killed"] = False
    d.mark()
    err = ""
    try:
        tree = ast.parse(code)
        last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
        exec(compile(tree, "<run>", "exec"), ns)
        if last is not None:
            v = eval(compile(ast.Expression(last.value), "<run>", "eval"), ns)
            if v is not None:
                print(repr(v), file=out)
    except BaseException as e:  # includes Killed / timeout interrupt
        tb = traceback.format_exception(type(e), e, e.__traceback__)
        err = "ERROR: " + "".join(tb[-2:]).strip()
    text = out.getvalue()
    if len(text) > 2000:
        text = text[:2000] + f"\n... [{len(text) - 2000} chars cut]"
    try:
        after = d.after_action()
    except Exception as e:
        after = f"(validate failed: {e})"
    return "\n".join(p for p in (text.strip(), err, "--- state ---\n" + after) if p)


def _submit(fn, *a, timeout=60):
    def job():
        state["busy"] = True
        try:
            return fn(*a)
        finally:
            state["busy"] = False
    fut = EXEC.submit(job)
    try:
        return fut.result(timeout=timeout)
    except FutTimeout:
        _interrupt(TimeoutError)
        return fut.result(timeout=10)


@mcp.tool()
def observe(target: str | None = None, mode: str = "tree", region: list[int] | None = None):
    """See a window. mode: tree (a11y elements with [id]s, cheap, default) | diff (changes since
    last look) | shot (JPEG, only if tree is empty/insufficient; region=[x0,y0,x1,y1] screen px
    = zoom). target: window title substring; default = current target or foreground window."""
    d = lambda: state["desk"]
    if mode == "shot":
        data, size = _submit(lambda: d().shot(target, region))
        return [f"shot {size[0]}x{size[1]}; click_xy(x, y) takes these image coords", Image(data=data, format="jpeg")]
    return _submit(lambda: d().observe(target, mode))


@mcp.tool()
def run(code: str, timeout: int = 30) -> str:
    """Run Python in a persistent REPL to act on the UI. Batch all steps in one call.
    Helpers: click(id|name=,role=) dclick rclick type(text, id=None|name=, enter=False)
    key("ctrl+s", times=1) scroll(id, dir="down", n=3) click_xy(x, y) find(role=, name=)
    wait_for(name, role=None, timeout=5) focus(title) app(cmd, title=None) sh(ps_cmd)
    observe(target, mode). Returns printed output + auto diff of the UI after running."""
    return _submit(_exec, code, timeout=timeout)


if __name__ == "__main__":
    threading.Thread(target=_hotkey_loop, daemon=True).start()
    mcp.run()
