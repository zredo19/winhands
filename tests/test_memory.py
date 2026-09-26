import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
import memory


def test_notes_append_read_and_search(tmp_path):
    memory.note("Ctrl+A is Abrir in Spanish Notepad", app="Notepad.exe", home=tmp_path)
    memory.note("Use menu items by name", app="notepad", home=tmp_path)
    memory.note("F3 shows XYZ", app="javaw", home=tmp_path)
    txt = memory.notes(app="notepad.exe", home=tmp_path)
    assert "Abrir" in txt and "menu items" in txt and "F3" not in txt
    hits = memory.notes(query="xyz", home=tmp_path)
    assert "javaw" in hits and "F3 shows XYZ" in hits


def test_notes_missing_app_is_empty(tmp_path):
    assert memory.notes(app="nothing", home=tmp_path) == ""


def test_skills_save_list_and_load(tmp_path):
    memory.save_skill("double", "def double(x):\n    return 2 * x\n", doc="Doubles x.", home=tmp_path)
    assert "double: Doubles x." in memory.skills(home=tmp_path)
    ns = {}
    loaded = memory.load_skills(ns, home=tmp_path)
    assert loaded == ["double"] and ns["double"](21) == 42


def test_save_skill_rejects_bad_name_and_syntax(tmp_path):
    with pytest.raises(ValueError):
        memory.save_skill("../evil", "x = 1", home=tmp_path)
    with pytest.raises(SyntaxError):
        memory.save_skill("broken", "def f(:\n", home=tmp_path)


def test_load_skills_reports_broken_skill_without_raising(tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "bad.py").write_text("raise RuntimeError('boom')\n")
    ns = {}
    assert memory.load_skills(ns, home=tmp_path) == ["bad (error: RuntimeError: boom)"]
