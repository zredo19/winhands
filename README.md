# astra-cu

Astra-style computer use for Windows, as a tiny MCP server for Claude Code (or any MCP client).

It copies the harness ideas behind GPT-6 Astra's computer use instead of the classic
"screenshot → click pixel" loop:

1. **Accessibility tree first.** Windows UI Automation gives every button, field and menu as
   compact text (`[17] Button "Save"`). Screenshots are an explicit fallback.
2. **Code mode.** The model writes one Python snippet that batches many actions
   (`click(name="Save")`, `type(...)`, `key("ctrl+s")`) in a persistent REPL.
3. **Built-in validation.** Every `run` returns a diff of the UI after acting (or the full tree
   of a newly opened dialog), so checking the result costs no extra turn.

## Tools

| Tool | What it does |
|---|---|
| `observe(target, mode)` | `tree` (default), `diff` since last look, or `shot` (JPEG, optional `region` zoom) |
| `run(code, timeout)` | Executes Python with helpers: `click dclick rclick type key scroll click_xy find wait_for focus app sh observe` |

Only two tools, so the schema the model carries every turn stays small (~370 tokens).

## Benchmark vs Windows-MCP

Same tasks, same machine (Windows 10, Spanish UI), each server driven the way an optimal agent
would use it. 3 reps, all 12 runs succeeded. Script: [`bench/bench.py`](bench/bench.py),
raw data: [`bench/results.json`](bench/results.json).

| Task | Server | Tool calls | Tool time | Tokens returned |
|---|---|---|---|---|
| Calculator 123×456 | **astra-cu** | **2** | **5.0 s** | **389** |
| | Windows-MCP 0.8.5 | 12 | 25.8 s | 5,948 |
| Notepad: type, Save As, verify file | **astra-cu** | **3** | **7.9 s** | **1,866** |
| | Windows-MCP 0.8.5 | 7 | 19.5 s | 7,735 |

Tool schema carried every turn: astra-cu ~370 tokens vs Windows-MCP ~4,780 tokens.

Notes on method:
- Tokens are estimated (text chars / 3.5; images w·h / 750).
- Tool time excludes model thinking. In a real agent each extra call also adds a model turn,
  so the gap in wall time is larger than shown.
- Windows-MCP's snapshot lists only interactive elements, so reading the calculator result
  needs a screenshot; astra-cu's post-action diff already contains it.
- Windows-MCP's rapid consecutive clicks dropped inputs; a 1 s pause (not counted) was added
  between its clicks to mimic agent pacing.

## Install

```bash
pip install "mcp>=2.2" uiautomation mss Pillow
claude mcp add astra-cu --scope user -- python C:\path\to\astra-cu\server.py
```

Optional: copy `SKILL.md` to `~/.claude/skills/astra-cu/SKILL.md` so Claude follows the
observe → batch → validate loop.

## Safety

- **Ctrl+Alt+Q** aborts the running action immediately.
- Keystrokes are only sent if the target window is in front; otherwise the call fails instead
  of typing into another window.
- Denylisted windows (password managers, banking) are refused.
- `run` executes arbitrary Python: the same privilege as Claude Code's own shell tool, and it
  goes through the client's permission prompts.
- Not for games or anything with anti-cheat.

## Limits

- Apps without UI Automation support (games, canvas apps) fall back to screenshots.
- The harness can make a model cheaper and faster on the desktop; it does not make it smarter.

## Tests

```bash
python -m pytest -q
```
