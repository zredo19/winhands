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
    i = reg.ids
    assert lines[0] == f'{i[tree["rid"]]} window "Save"'
    assert lines[1] == f'  {i[edit["rid"]]} edit "Nombre:" Value: "a.txt" (focused, settable)'
    assert lines[2].endswith('check box "Hidden" (unchecked)')
    assert lines[4].endswith('list item "x" (selected)')
    assert lines[5] == "    (showing 1-1 of 7; use find())"


def test_registry_ids_stable_and_survive_runtime_id_churn():
    reg = Registry()
    a, b = n("menu item", "File", rid=(1,)), n("button", "OK", rid=(2,))
    reg.assign(10, [a, b])
    ida, idb = reg.ids[(1,)], reg.ids[(2,)]
    a2 = n("menu item", "File", rid=(99,))           # same element, fresh RuntimeId
    reg.assign(10, [a2, b])
    assert reg.ids[(99,)] == ida and reg.ids[(2,)] == idb
    assert reg.current[10] == {ida, idb}


def test_diff_added_removed_changed():
    reg = Registry()
    old = n("window", "W", children=[n("button", "A", rid=(1,)), n("edit", "E", rid=(2,)),
                                     n("button", "B", rid=(3,))])
    reg.assign(5, flatten(old))
    before = reg.lines(flatten(old))
    new = n("window", "W", rid=old["rid"], children=[n("button", "A", rid=(1,)),
                                                     n("edit", "E", value="typed", rid=(2,)),
                                                     n("button", "C", rid=(4,))])
    reg.assign(5, flatten(new))
    out = diff(before, reg.lines(flatten(new))).splitlines()
    ids = reg.ids
    assert f'- {ids[(3,)]} button "B"' in out
    assert f'~ {ids[(2,)]} edit "E" Value: "typed"' in out
    assert f'+ {ids[(4,)]} button "C"' in out
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
