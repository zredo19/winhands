---
name: astra-cu
description: Control Windows desktop apps via the astra-cu MCP (a11y tree + code mode). Trigger when the user asks to operate a native Windows app/GUI (open, click, fill, navigate, read values) that has no CLI/API.
---

# astra-cu — desktop control protocol

Tool order: CLI/Bash > app-specific MCP > `claude-in-chrome` (web pages) > **astra-cu** (native GUI).

## Loop (observe → act in one batch → validate)
1. `observe()` once → element list `[id] Role "Name" ="value"` (`*` focused, `~` disabled).
2. ONE `run(code)` per step, batching every action you are confident about:
   ```python
   app("notepad.exe")
   type("hello", id=find(role="Edit", raw=True)[0])
   click(name="Archivo"); click(name="Guardar como", role="MenuItem")
   wait_for("Nombre de archivo")
   ```
3. Read the `--- state ---` diff returned by `run` (it is the validation; a new dialog shows as a
   full tree). Do NOT call `observe` again unless the diff is insufficient.
4. Mismatch → re-plan from the diff. Tree empty (canvas, games, custom-drawn) →
   `observe(mode="shot")`, then `click_xy(x, y)` with image coords; `region=[x0,y0,x1,y1]` to zoom.

## Token rules
- Prefer `find(role=, name=)` / `click(name=...)` over re-reading the full tree.
- Never request `shot` when the tree has what you need.
- Keep printed output short; `run` returns the last expression's repr.

## Gotchas
- Prefer clicking menu items by name over keyboard shortcuts: shortcuts are locale-dependent
  (Spanish Notepad: Ctrl+A = Abrir, Ctrl+E = select all, Ctrl+G = Guardar).
- `type(text, id=...)` uses UIA SetValue (instant, no focus needed); without id it types keys
  into the foreground target.
- Keys only go to the target: if it can't be focused, `key`/`type` raise instead of typing elsewhere.
- IDs persist across diffs within a session; after a new window opens use its new ids.
- User kill switch: Ctrl+Alt+Q aborts the running `run`. Denylisted windows (password managers,
  banking) raise PermissionError — do not work around it.
- Never automate games or anything with anti-cheat.
