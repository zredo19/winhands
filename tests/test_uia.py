import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from uia import (role_name, prune, collapse_lists, Registry, render, diff, canvas_like,
                 stale_reason, risky, flatten)

_rid = iter(range(1000, 10 ** 6))


def n(role, name="", value="", children=(), rid=None, rect=(0, 0, 10, 10), **kw):
    d = {"rid": rid or (next(_rid),), "role": role, "name": name, "value": value, "rect": rect,
         "focused": False, "enabled": True, "selected": None, "checked": None, "expanded": None,
         "settable": False, "scrollable": False, "children": list(children)}
    d.update(kw)
    return d


def test_role_name_from_control_type():
    assert role_name("MenuItemControl") == "menu item"
    assert role_name("EditControl") == "edit"
    assert role_name("TabItemControl") == "tab item"


def test_prune_drops_scrollbars_flattens_unnamed_containers_keeps_named():
    tree = n("window", "App", children=[
        n("pane", children=[n("group", children=[n("button", "OK")])]),     # redundant nesting
        n("scroll bar", "Vertical", children=[n("button", "Line up")]),     # noise
        n("pane", "Sidebar", children=[n("text", "Hi"), n("button", "Go")]),  # named: kept
        n("text"),                                                          # empty leaf
        n("button", enabled=False),                                         # empty disabled
    ])
    out = prune(tree, win_area=10_000)[0]
    kids = out["children"]
    assert [k["role"] for k in kids] == ["button", "pane"]
    assert kids[0]["name"] == "OK" and kids[1]["children"][0]["name"] == "Hi"


def test_prune_merges_single_child_containers_and_hides_container_values():
    tree = n("window", "Explorer", children=[
        n("pane", "Ribbon", children=[n("pane", "Ribbon", children=[
            n("tool bar", "Quick", value="Quick Access Toolbar", settable=True,
              children=[n("button", "A"), n("button", "B")])])]),
        n("group", "Campo", children=[n("text", "8 elementos")]),
    ])
    kids = prune(tree, win_area=1)[0]["children"]
    assert [k["role"] for k in kids] == ["tool bar", "text"]
    assert kids[0]["value"] == "" and kids[0]["settable"] is False


def test_prune_keeps_big_unnamed_canvas():
    tree = n("window", "Paint", rect=(0, 0, 100, 100),
             children=[n("pane", rect=(0, 10, 100, 90), cls="MSPaintView")])
    out = prune(tree, win_area=100 * 100)[0]
    assert out["children"][0].get("canvas") is True


def test_prune_marks_big_named_childless_surface_as_canvas():
    tree = n("window", "Paint", rect=(0, 0, 100, 100), children=[
        n("pane", "Using the brush on the canvas", rect=(0, 20, 100, 95)), n("button", "Save")])
    kids = prune(tree, win_area=100 * 100)[0]["children"]
    assert kids[0].get("canvas") is True and not kids[1].get("canvas")


def test_prune_merges_adjacent_text_leaves():
    tree = n("window", "W", children=[n("text", "Línea 1"), n("text", "UTF-8"), n("button", "B")])
    kids = prune(tree, win_area=1)[0]["children"]
    assert kids[0]["name"] == "Línea 1 · UTF-8" and kids[1]["role"] == "button"


def test_collapse_lists_folds_cells_and_caps_items():
    items = [n("list item", f"file{i}", children=[n("edit", "Tipo", value="Carpeta"),
                                                   n("edit", "Fecha", value=f"0{i % 9 + 1}-2026")])
             for i in range(30)]
    tree = n("list", "Items", children=items)
    out = collapse_lists(tree, max_items=25)
    assert len(out["children"]) == 25 and out["more"] == (25, 30)
    first = out["children"][0]
    assert first["children"] == [] and first["extra"] == ["Carpeta", "01-2026"]


def test_render_indents_states_value_and_summary():
    reg = Registry()
    edit = n("edit", "Nombre:", value="a.txt", settable=True, focused=True)
    lst = n("list", "Files", children=[n("list item", "x", selected=True)])
    lst["more"] = (1, 7)
    tree = n("window", "Save", children=[edit, n("check box", "Hidden", checked="off"), lst])
    reg.assign(1, flatten(tree))
    lines = render(tree, reg).splitlines()
    assert lines[0] == f'{reg.id_of(tree)} window "Save"'
    assert lines[1] == f'  {reg.id_of(edit)} edit "Nombre:" Value: "a.txt" (focused, settable)'
    assert lines[2].endswith('check box "Hidden" (unchecked)')
    assert lines[4].endswith('list item "x" (selected)')
    assert lines[5] == "    (showing 1-1 of 7; use find())"


def test_registry_ids_stable_and_survive_runtime_id_churn():
    reg = Registry()
    a, b = n("menu item", "File", rid=(1,)), n("button", "OK", rid=(2,))
    reg.assign(10, [a, b])
    ida, idb = reg.id_of(a), reg.id_of(b)
    a2 = n("menu item", "File", rid=(99,))           # same element, fresh RuntimeId
    reg.assign(10, [a2, b])
    assert reg.id_of(a2) == ida and reg.id_of(b) == idb
    assert reg.current[10] == {ida, idb}


def test_registry_reused_runtime_id_for_other_element_gets_new_id():
    # MSAA-proxied UIs (Win32 ribbons) reuse RuntimeIds for different elements after re-layout
    reg = Registry()
    undo, custom = n("button", "Undo", rid=(5,)), n("button", "Customize", rid=(7,))
    reg.assign(1, [undo, custom])
    iu, ic = reg.id_of(undo), reg.id_of(custom)
    custom2, undo2 = n("button", "Customize", rid=(5,)), n("button", "Undo", rid=(8,))
    reg.assign(1, [undo2, custom2])
    assert reg.id_of(undo2) == iu and reg.id_of(custom2) == ic
    assert len(reg.current[1]) == 2                  # never two nodes with one id


def test_diff_added_removed_changed():
    reg = Registry()
    old = n("window", "W", children=[n("button", "A", rid=(1,)), n("edit", "E", rid=(2,)),
                                     n("button", "B", rid=(3,))])
    reg.assign(5, flatten(old))
    before = reg.lines(flatten(old))
    before_ids = {1: reg.id_of(old["children"][2])}
    new = n("window", "W", rid=old["rid"], children=[n("button", "A", rid=(1,)),
                                                     n("edit", "E", value="typed", rid=(2,)),
                                                     n("button", "C", rid=(4,))])
    reg.assign(5, flatten(new))
    out = diff(before, reg.lines(flatten(new))).splitlines()
    b_old, e_new, c_new = old["children"][2], new["children"][1], new["children"][2]
    assert f'- {before_ids[1]} button "B"' in out
    assert f'~ {reg.id_of(e_new)} edit "E" Value: "typed"' in out
    assert f'+ {reg.id_of(c_new)} button "C"' in out
    assert len(out) == 3


def test_diff_no_change():
    assert diff({1: "x"}, {1: "x"}) == "(no change)"


def test_canvas_like():
    few = n("window", "Minecraft", rect=(0, 0, 100, 100), children=[n("button", "Close")])
    assert canvas_like(flatten(few), (0, 0, 100, 100))
    rich = n("window", "Form", children=[n("button", str(i)) for i in range(8)])
    assert not canvas_like(flatten(rich), (0, 0, 100, 100))
    big = n("window", "Paint", children=[n("button", str(i)) for i in range(8)]
            + [n("pane", rect=(0, 0, 90, 90), canvas=True)])
    assert canvas_like(flatten(big), (0, 0, 100, 100))


def test_stale_reason():
    rec = {"role": "button", "name": "OK", "rect": (10, 10, 50, 30)}
    assert stale_reason(rec, {"role": "button", "name": "OK", "rect": (10, 10, 50, 30)}) is None
    assert "gone" in stale_reason(rec, None)
    assert "name" in stale_reason(rec, {"role": "button", "name": "Cancel", "rect": (10, 10, 50, 30)})
    moved = {"role": "button", "name": "OK", "rect": (60, 10, 100, 30)}
    assert stale_reason(rec, moved) is None                       # patterns don't care
    assert "moved" in stale_reason(rec, moved, need_rect=True)    # coordinate clicks do


def test_risky_names():
    assert risky("Enviar") and risky("Comprar ahora") and risky("Delete file") and risky("Instalar")
    assert not risky("Guardar") and not risky("Abrir") and not risky("")


def test_cap_lines_truncates_long_dumps():
    from uia import cap_lines
    out = cap_lines("\n".join(str(i) for i in range(100)), 40).splitlines()
    assert len(out) == 41 and out[-1].startswith("... +60 lines")
    assert cap_lines("a\nb", 40) == "a\nb"


def test_usable_window_skips_broken_and_shell_windows(monkeypatch):
    import uia

    class Boom:
        NativeWindowHandle = 5
        @property
        def Name(self):
            raise OSError("COM event failed")

    class Toast:
        NativeWindowHandle, Name, ProcessId = 6, "Nueva notificación", 1
    monkeypatch.setattr(uia, "_exe", lambda pid: "ShellExperienceHost.exe")
    assert uia.Desk._usable(Boom()) is False and uia.Desk._usable(Toast()) is False


def test_after_action_skips_tree_diff_on_canvas_windows(monkeypatch):
    import uia
    d = object.__new__(uia.Desk)

    class Win:
        NativeWindowHandle, Name = 42, "Minecraft"
    d.win, d.before, d.canvas = Win(), {42}, {42: True}
    d._wins = lambda: {42: Win()}
    d._alive = lambda w: True
    d.observe = lambda *a, **k: (_ for _ in ()).throw(AssertionError("tree diff on a canvas window"))
    monkeypatch.setattr(uia.time, "sleep", lambda s: None)
    out = d.after_action()
    assert "Minecraft" in out and "skipped" in out


def test_is_chat_matches_exe_or_window_title():
    from uia import is_chat
    assert is_chat("Discord.exe", "valorador | servidor - Discord")
    assert is_chat("chrome.exe", "(1) WhatsApp - Google Chrome")           # WhatsApp Web in a browser
    assert is_chat("ms-teams.exe", "") and is_chat("slack.exe", "") and is_chat("Signal.exe", "")
    assert is_chat("Telegram.exe", "") and is_chat("Messenger.exe", "")
    assert not is_chat("chrome.exe", "Cursos - Universidad Adolfo Ibáñez - Google Chrome")
    assert not is_chat("notepad.exe", "notes.txt")


def test_route_line_flags_target_foreground_mismatch():
    from uia import route_line
    t = {"title": "UAI Online", "hwnd": 1, "exe": "chrome.exe"}
    f = {"title": "Discord", "hwnd": 2, "exe": "Discord.exe"}
    assert "MISMATCH" in route_line(t, f) and "MISMATCH" in route_line(None, f)
    ok = route_line(t, t)
    assert "MISMATCH" not in ok and "hwnd=1" in ok and ok.startswith("input: target")


def test_route_note_only_after_real_input(monkeypatch):
    import uia
    d = object.__new__(uia.Desk)
    info = {"title": "W", "hwnd": 1, "exe": "x.exe"}
    d.target_info, d.foreground_info = lambda: info, lambda: info
    monkeypatch.setattr(uia.inputs, "last", [100.0])
    assert d.route_note(since=200.0) == ""                    # nothing was injected during this run
    assert d.route_note(since=50.0).startswith("input: target")


def _chat_desk(monkeypatch, exe, title, confirmed=False):
    import uia

    class W:
        NativeWindowHandle, Name, ProcessId = 7, title, 1
    w = W()
    monkeypatch.setattr(uia, "_exe", lambda pid: exe)
    d, sent = object.__new__(uia.Desk), []
    d.confirmed, d.win = confirmed, w
    d.resolve = lambda target=None: w
    d._front = lambda w=None: None
    d.foreground_info = lambda: {"title": title, "hwnd": 7, "exe": exe}
    monkeypatch.setattr(uia.inputs, "press", lambda spec, times=1, **k: sent.append(spec))
    monkeypatch.setattr(uia.inputs, "text", lambda s: sent.append(s))
    return d, sent


def test_enter_in_chat_app_needs_confirm(monkeypatch):
    import pytest, uia
    d, sent = _chat_desk(monkeypatch, "Discord.exe", "general - Discord")
    with pytest.raises(uia.GuardBlocked):
        d.type("hi", enter=True)
    for spec in ("enter", "ctrl+enter"):
        with pytest.raises(uia.GuardBlocked):
            d.key(spec)
    assert sent == []                                          # not even the text was typed
    d.type("draft")                                            # typing a draft is harmless
    d.key("ctrl+s")
    assert sent == ["draft", "ctrl+s"]


def test_enter_in_chat_app_allowed_when_confirmed_or_not_chat(monkeypatch):
    d, sent = _chat_desk(monkeypatch, "Discord.exe", "general - Discord", confirmed=True)
    d.type("hi", enter=True)
    assert sent == ["hi", "enter"]
    d, sent = _chat_desk(monkeypatch, "notepad.exe", "notes.txt")
    d.type("hi", enter=True)
    d.key("enter")
    assert sent == ["hi", "enter", "enter"]


def test_enter_in_whatsapp_web_tab_needs_confirm(monkeypatch):
    import pytest, uia
    d, _ = _chat_desk(monkeypatch, "chrome.exe", "(1) WhatsApp - Google Chrome")
    with pytest.raises(uia.GuardBlocked):
        d.key("enter")


def test_app_notes_when_window_could_not_be_brought_to_front(monkeypatch):
    import uia

    class Win:
        NativeWindowHandle, Name, ProcessId = 9, "Paint", 1

    def desk(front):
        d, calls = object.__new__(uia.Desk), []
        d._wins = lambda: {9: Win()} if calls else (calls.append(1) or {})
        d._usable, d._guard, d.stop, d._spawn, d._front = (lambda w: True), (lambda w: None), (lambda: None), \
            (lambda cmd: None), front
        d.win = None
        return d
    monkeypatch.setattr(uia.time, "sleep", lambda s: None)

    def boom(w=None):
        raise RuntimeError("could not bring 'Paint' to front; input not sent")
    out = desk(boom).app("mspaint.exe")
    assert out.startswith("Paint") and "not brought to front" in out and "could not bring" in out
    assert desk(lambda w=None: None).app("mspaint.exe") == "Paint"


def test_focus_accepts_hwnd_and_title_keywords():
    import uia
    d, seen = object.__new__(uia.Desk), []

    class W:
        NativeWindowHandle, Name = 1, "Chrome"
    d.resolve = lambda target=None: (seen.append(target), W())[1]
    d._front = lambda w=None: None
    assert d.focus(5) == "Chrome"
    d.focus(hwnd=6)
    d.focus(title="Chrome")
    d.focus()
    assert seen == [5, 6, "Chrome", None]
