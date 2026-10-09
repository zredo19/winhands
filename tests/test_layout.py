import pathlib, tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULES = ["server", "uia", "inputs", "vision", "memory", "overlay"]


def test_modules_live_in_the_package_not_at_the_top_level():
    for m in MODULES:
        assert (ROOT / "winhands" / f"{m}.py").is_file(), m
        assert not (ROOT / f"{m}.py").exists(), m          # flat modules pollute site-packages


def test_there_is_one_skill_file_and_it_ships_with_the_package():
    assert (ROOT / "winhands" / "SKILL.md").is_file()
    assert not (ROOT / "SKILL.md").exists()


def test_pyproject_ships_the_package_and_points_the_script_at_the_cli():
    p = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert p["project"]["scripts"] == {"winhands": "winhands.cli:main"}
    st = p["tool"]["setuptools"]
    assert st["packages"] == ["winhands"] and "py-modules" not in st
    assert "SKILL.md" in st["package-data"]["winhands"]
