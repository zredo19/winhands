"""Scripted A/B benchmark: astra-cu vs Windows-MCP on identical desktop tasks.

Each task is driven the way an optimal agent would use each server (fewest calls the
tool design allows). We measure tool calls, wall time, and the size of what each call
returns to the model (the tokens an agent must read).

Token estimate: text = chars / 3.5; images = ceil(w/28) * ceil(h/28) (Anthropic vision docs).

Usage: python bench.py <path-to-windows-mcp.exe> [reps]
"""
import asyncio, io, json, math, os, re, subprocess, sys, tempfile, time
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))
ASTRA = StdioServerParameters(command=sys.executable, args=[os.path.join(HERE, "..", "server.py")])
OUT = tempfile.gettempdir()


def tokens(content):
    t = 0
    for c in content:
        if c.type == "text":
            t += len(c.text) / 3.5
        elif c.type == "image":
            import base64
            from PIL import Image
            w, h = Image.open(io.BytesIO(base64.b64decode(c.data))).size
            t += math.ceil(w / 28) * math.ceil(h / 28)
    return t


def text_of(content):
    return "\n".join(c.text for c in content if c.type == "text")


class Meter:
    def __init__(self, session):
        self.s, self.calls, self.tok, self.ms, self.log = session, 0, 0.0, 0.0, []

    async def __call__(self, tool, **args):
        t = time.perf_counter()
        res = await self.s.call_tool(tool, args)
        ms = (time.perf_counter() - t) * 1000
        tk = tokens(res.content)
        self.calls += 1; self.tok += tk; self.ms += ms
        self.log.append((tool, round(ms), round(tk)))
        if os.environ.get("BENCH_DUMP"):
            with open(os.path.join(OUT, "bench_dump.txt"), "a", encoding="utf-8") as f:
                f.write(f"\n===== {tool} {args}\n{text_of(res.content)}\n")
        return text_of(res.content)


def cleanup():
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-Process notepad,CalculatorApp -ErrorAction SilentlyContinue | Stop-Process -Force"],
                   capture_output=True)
    time.sleep(1)


def loc(tree, name):
    """Windows-MCP tree line: '(x,y) <role> "name"' -> [x, y]."""
    if tree.startswith("["):  # Windows-MCP wraps the snapshot in a JSON string list
        tree = "\n".join(json.loads(tree))
    m = re.search(r'\((-?\d+),(-?\d+)\) [^"\n]*"' + re.escape(name) + r'"', tree)
    if not m:
        raise LookupError(name)
    return [int(m.group(1)), int(m.group(2))]


def calc_display():
    """Ground truth read of the calculator display (not counted in metrics).
    Separate process: in-process COM clashed with the asyncio client."""
    code = ("import uiautomation as a;"
            "t=a.WindowControl(searchDepth=1,Name='Calculadora')"
            ".TextControl(searchDepth=12,AutomationId='CalculatorResults');"
            "print(t.Name if t.Exists(3) else '')")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    CHECKS.append(r.stdout.strip())
    return r.stdout.strip()


CHECKS = []


DIGITS = ["Uno", "Dos", "Tres", "Multiplicar por", "Cuatro", "Cinco", "Seis", "Es igual a"]


# ---------------- tasks: astra-cu ----------------
async def a_calc(call):
    await call("run", code='app("calc.exe", title="Calculadora")')
    out = await call("run", code="\n".join(f'click(name="{n}", role="Button")' for n in DIGITS))
    return "56.088" in out and "56.088" in calc_display()


async def a_notepad(call, path):
    await call("run", code='app("notepad.exe")')
    await call("run", code='type("benchmark astra-cu", id=find(role="Edit", raw=True)[0])\n'
                           'key("ctrl+shift+s")\nwait_for("Nombre:", role="Edit")')
    out = await call("run", confirm=True,  # sh() is Guardian-gated; this check is the benchmark's own
                     code=f'type(r"{path}", name="Nombre:", role="Edit", enter=True)\n'
                          f'import time; time.sleep(0.8)\nsh(r"Test-Path \'{path}\'")')
    return "True" in out


# ---------------- tasks: Windows-MCP ----------------
async def w_calc(call):
    await call("App", mode="launch", name="Calculadora")
    await call("App", mode="switch", name="Calculadora")  # snapshot only covers the focused window
    tree = await call("Snapshot", use_vision=False)
    for n in DIGITS:
        await call("Click", loc=loc(tree, n))
        await asyncio.sleep(1)  # real agents think between calls; not counted in ms
    ok = "56.088" in calc_display()  # read first: its vision snapshot hides the window from UIA
    await call("Snapshot", use_vision=True)  # result text is not in its tree: agent must look
    return ok


async def w_notepad(call, path):
    await call("App", mode="launch", name="Bloc de notas")
    tree = await call("Snapshot", use_vision=False)
    await call("Type", loc=loc(tree, "Editor de texto"), text="benchmark astra-cu")
    await call("Shortcut", shortcut="ctrl+shift+s")
    tree = await call("Snapshot", use_vision=False)
    await call("Type", loc=loc(tree, "Nombre:"), text=path, clear=True, press_enter=True)
    out = await call("PowerShell", command=f"Start-Sleep -Milliseconds 800; Test-Path '{path}'")
    return "True" in out


async def run_suite(params, tasks, label, reps):
    rows = []
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        schema = sum(len(json.dumps({"n": t.name, "d": t.description, "s": t.input_schema}))
                     for t in (await s.list_tools()).tools)
        for rep in range(reps):
            for name, fn in tasks:
                cleanup()
                m = Meter(s)
                path = os.path.join(OUT, f"bench_{label}_{rep}.txt")
                if os.path.exists(path):
                    os.remove(path)
                try:
                    ok = await (fn(m, path) if "notepad" in name else fn(m))
                except Exception as e:
                    ok, m.log = False, m.log + [("ERROR", repr(e)[:80], 0)]
                check = CHECKS.pop() if CHECKS else None
                rows.append(dict(server=label, task=name, rep=rep, ok=ok, check=check, calls=m.calls,
                                 ms=round(m.ms), tokens=round(m.tok), log=m.log))
                print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    cleanup()
    return schema, rows


async def main():
    wexe, reps = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 2
    W = StdioServerParameters(command=wexe, args=["serve"])
    sa, ra = await run_suite(ASTRA, [("calc", a_calc), ("notepad", a_notepad)], "astra-cu", reps)
    sw, rw = await run_suite(W, [("calc", w_calc), ("notepad", w_notepad)], "windows-mcp", reps)
    print("SCHEMA_CHARS", json.dumps({"astra-cu": sa, "windows-mcp": sw}))
    with open(os.path.join(HERE, "results.json"), "w", encoding="utf-8") as f:
        json.dump({"schema_chars": {"astra-cu": sa, "windows-mcp": sw}, "rows": ra + rw}, f,
                  ensure_ascii=False, indent=1)


if __name__ == "__main__":
    asyncio.run(main())
