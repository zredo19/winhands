import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from uia import Registry, format_line, render, diff


def n(rid, role="Button", name="Ok", value="", focused=False, enabled=True):
    return {"rid": rid, "role": role, "name": name, "value": value,
            "focused": focused, "enabled": enabled}


def test_format_line_flags_and_truncation():
    assert format_line(3, n(1)) == '[3] Button "Ok"'
    assert format_line(3, n(1, focused=True, enabled=False)) == '[3] Button "Ok" *~'
    assert format_line(1, n(1, role="Edit", name="", value="hi")) == '[1] Edit ="hi"'
    long = format_line(1, n(1, name="x" * 100))
    assert long.count("x") == 40 and long.endswith('…"')


def test_registry_ids_stable_across_snapshots():
    r = Registry()
    assert r.id_for((1, 2)) == 1
    assert r.id_for((9,)) == 2
    assert r.id_for((1, 2)) == 1


def test_render_caps_nodes():
    r = Registry()
    text = render([n((i,), name=f"b{i}") for i in range(5)], r, cap=3)
    lines = text.splitlines()
    assert lines[:3] == ['[1] Button "b0"', '[2] Button "b1"', '[3] Button "b2"']
    assert lines[3] == "... +2 more (use find())"


def test_diff_added_removed_changed():
    r = Registry()
    old = [n((1,), name="A"), n((2,), name="B"), n((3,), role="Edit", name="E")]
    new = [n((1,), name="A"), n((3,), role="Edit", name="E", value="typed"), n((4,), name="C")]
    render(old, r)
    out = diff(old, new, r).splitlines()
    assert "- [2] Button \"B\"" in out
    assert "~ [3] Edit \"E\" =\"typed\"" in out
    assert "+ [4] Button \"C\"" in out
    assert not any("[1]" in l for l in out)


def test_diff_ignores_runtime_id_churn():
    # some controls (menu items) get a fresh RuntimeId on every snapshot
    r = Registry()
    old = [n((1,), role="MenuItem", name="File")]
    new = [n((2,), role="MenuItem", name="File")]
    render(old, r)
    assert diff(old, new, r) == "(no change)"
    assert r.id_for((2,)) == 1  # same element keeps its id


def test_diff_no_change():
    r = Registry()
    s = [n((1,))]
    assert diff(s, s, r) == "(no change)"
