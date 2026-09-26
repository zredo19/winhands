# astra-cu

Astra-style computer use for Windows: a small MCP server that lets Claude (or any MCP client)
operate any desktop app, draw in canvases and drive games.

It replicates the harness GPT-6 Astra uses in the Codex/ChatGPT desktop app, and goes past it
where that harness is weak (no key holds, no raw mouse, no local control loops):

1. **Accessibility tree first, pixels when needed.** A Codex-style UI Automation tree
   (`12 button "Save" (focused)`), plus screenshots automatically when a window is canvas-like
   (games, Paint), OCR, grid rulers, Set-of-Marks and burst montages.
2. **Code mode.** The model writes Python in a persistent REPL and batches many actions per turn,
   including local perceive→act loops that run at 10–30 Hz without model round-trips.
3. **Built-in validation.** Every `run` returns a UI diff (or the full tree of a new dialog).
4. **Fail closed.** Element ids are bound to the latest snapshot; changed or vanished targets raise
   `StaleTarget` instead of clicking the wrong thing.
5. **Takeover UX and safety.** A red border and banner while acting, excluded from screenshots.
   Ctrl+LeftAlt+Q kill switch, and an automatic abort when the user touches mouse or keyboard.
   Held inputs are released on abort, risky actions need confirmation, and some windows are denylisted.
6. **Memory.** Per-app notes and reusable skills (Voyager-style) that persist across sessions.

## Tools (2, ~815 tokens of schema)

| Tool | Modes / helpers |
|---|---|
| `observe(target, mode, region, grid, marks, frames, scale)` | `auto` (tree + shot if canvas-like), `tree`, `both`, `diff`, `shot`, `ocr`, `burst`, `windows` |
| `run(code, timeout, confirm)` | UIA: `click set_value action type key scroll find wait_for focus app windows sh` · Input: `press hold key_down key_up type_keys move move_rel click_at drag wheel release_all` · Vision: `show grab pixel find_color locate save_template ocr find_text click_text click_xy wait_change wait_stable` · Memory: `note notes save_skill skills` |

Input uses `SendInput` with scan codes (DirectInput/raw-input games see real keys) and raw relative
mouse for game cameras. Window capture uses `PrintWindow(PW_RENDERFULLCONTENT)`, so covered windows
are read correctly. Apps launched with `app()` survive the MCP session (launched through WMI,
outside the client's kill-on-close job).

## End-to-end results (real desktop, Windows 10, Spanish UI)

| Test | Result |
|---|---|
| Paint: draw a house (shapes by drag, palette swatches found by color, bucket fills), save PNG | 6/6 layout and color checks pass; one `run` of 11 s |
| Game probe (Chrome, pointer lock): scan codes W/A/S/D/Space/ShiftLeft | all six received as physical key codes |
| Raw relative mouse under pointer lock | sent (70, 30) → page received DX 70, DY 30 exactly |
| Buttons / wheel | L, M, R and wheel −3 received |
| 10 s aim test driven by one local loop (`find_color` → `click_at`) | 88 hits, 6 misses |
| OCR of known Notepad text | 30/34 words (88%) in 118 ms. Misses: the first glyph touching the edit border, and I/l confusion |

## Benchmark vs Windows-MCP

The same tasks run on the same machine, with each server driven the way an optimal agent would use it.
There were 3 reps per task. Script: [`bench/bench.py`](bench/bench.py), raw data:
[`bench/results.json`](bench/results.json).

| Task | Server | Success | Tool calls | Tool time | Tokens returned |
|---|---|---|---|---|---|
| Calculator 123×456 | **astra-cu** | **3/3** | **2** | **8.0 s** | **603** |
| | Windows-MCP 0.8.5 | 2/3 | 12 | 31.0 s | 6,060 |
| Notepad: type, Save As, verify file | **astra-cu** | **3/3** | **3** | **12.9 s** | **2,037** |
| | Windows-MCP 0.8.5 | 3/3 | 7 | 21.6 s | 8,601 |

The tool schema is sent on every turn: astra-cu uses about 815 tokens and Windows-MCP about 4,780.

Notes on method:
- Tokens are estimated: text is chars / 3.5, and images are `ceil(w/28)·ceil(h/28)` per Anthropic's vision docs.
- Tool time excludes model thinking. Each extra call also costs a model turn in a real agent.
- Windows-MCP's snapshot lists only interactive elements of the focused window, so reading the
  calculator result needs a screenshot. Its failed rep missed the calculator in that snapshot.
- Windows-MCP dropped rapid consecutive clicks. A 1 s pause between its clicks, not counted, mimics agent pacing.

## Install

```bash
# Python 3.12 venv on an SSD (cold imports from an HDD can exceed the 30 s MCP startup timeout)
python -m venv %USERPROFILE%\.astra-cu\venv
%USERPROFILE%\.astra-cu\venv\Scripts\pip install -e .
claude mcp add astra-cu --scope user -- %USERPROFILE%\.astra-cu\venv\Scripts\python.exe C:\path\to\astra-cu\server.py
```

Copy `SKILL.md` to `~/.claude/skills/astra-cu/SKILL.md` so Claude follows the perceive → batch →
validate protocol, the game guidance and the safety rules. Set `ASTRA_OVERLAY=0` to disable the banner.

## Safety

- **Ctrl + Left Alt + Q** aborts the running action. AltGr+Q still types `@`.
- Physical keyboard or mouse input during a run aborts it (`UserInterrupt`). The tool never fights the user for control.
- Keys are only sent to a verified foreground target. All held keys and buttons are released on any abort.
- `sh()` and clicks on elements named like Send, Buy, Pay, Delete, Install or Allow require `run(..., confirm=True)`.
- Denylisted windows (password managers, banking) are refused.
- `run` executes arbitrary Python: the same privilege as Claude Code's shell tool, gated by the client's permission prompts.
- Online games with anti-cheat: automation may violate their terms and risk a ban. astra-cu makes
  no attempt to hide or evade detection.

## Limits

- The harness makes a model faster and cheaper on the desktop. It does not make it smarter.
- Reflex gameplay needs local loops written by the model, or pausing the game. Model round-trips take seconds.
- Exclusive-fullscreen games may capture black; use borderless mode. Windows OCR skips isolated single
  characters and glyphs touching strong borders. Template matching is exact-scale.
- Elevated (admin) windows ignore input from a non-elevated server (Windows UIPI).

## Tests

```bash
python -m pytest -q
```
