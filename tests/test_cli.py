import sys, pathlib, json, importlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

from winhands import cli

PY = r"C:\x\Scripts\python.exe"
CMD = [PY, "-m", "winhands"]            # what setup registers: never the unsigned winhands.exe launcher
CLAUDE = r"C:\bin\claude.cmd"


class Run:
    """Fake subprocess.run: records argv, answers returncode per call index (default 0)."""
    def __init__(self, *codes):
        self.calls, self.codes = [], list(codes)

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        code = self.codes[len(self.calls) - 1] if len(self.calls) <= len(self.codes) else 0
        return type("R", (), {"returncode": code})()


def _which(**found):
    return lambda name: found.get(name)


@pytest.fixture(autouse=True)
def no_real_warm_up(monkeypatch):
    monkeypatch.setattr(cli, "_warm", lambda: None)       # the real one imports the whole server (seconds)


def test_setup_warms_up_the_install_so_the_first_mcp_start_is_fast(tmp_path):
    calls = []
    cli.setup(home=tmp_path, which=_which(claude=CLAUDE), run=Run(), command=CMD, out=lambda s: None,
              warm=lambda: calls.append(1))
    assert calls == [1]


def test_warm_up_is_skipped_for_dry_run_and_remove(tmp_path):
    def boom():
        pytest.fail("warmed up")
    for kw in ({"dry_run": True}, {"remove": True}):
        assert cli.setup(home=tmp_path, which=_which(), run=Run(), command=CMD, out=lambda s: None, warm=boom, **kw) == 0


def test_a_failing_warm_up_never_fails_setup(tmp_path):
    def boom():
        raise RuntimeError("no display")
    out = []
    assert cli.setup(home=tmp_path, which=_which(claude=CLAUDE), run=Run(), command=CMD, out=out.append, warm=boom) == 0
    assert any("Warm-up skipped" in line and "no display" in line for line in out)


def test_no_arguments_starts_the_server_and_never_builds_a_parser(monkeypatch):
    import argparse
    monkeypatch.setattr(argparse, "ArgumentParser", lambda *a, **k: pytest.fail("argparse touched"))
    served = []
    assert cli.main([], serve=lambda: served.append(1)) == 0
    assert served == [1]


def test_version_prints_the_package_version(capsys):
    import winhands
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"winhands {winhands.__version__}"


def test_package_version_falls_back_when_not_installed(monkeypatch):
    import importlib.metadata as md
    import winhands

    def missing(name):
        raise md.PackageNotFoundError(name)
    monkeypatch.setattr(md, "version", missing)
    importlib.reload(winhands)
    try:
        assert winhands.__version__ == "unknown"
    finally:
        monkeypatch.undo()
        importlib.reload(winhands)
    assert winhands.__version__ != "unknown"


def test_skill_text_is_the_packaged_skill():
    text = cli.skill_text()
    assert text.startswith("---") and "name: winhands" in text


def test_setup_registers_mcp_and_installs_skill(tmp_path):
    run, out = Run(), []
    assert cli.setup(home=tmp_path, which=_which(claude=CLAUDE), run=run, command=CMD, out=out.append) == 0
    assert run.calls == [[CLAUDE, "mcp", "remove", "winhands", "--scope", "user"],
                         [CLAUDE, "mcp", "add", "winhands", "--scope", "user", "--", PY, "-m", "winhands"]]
    skill = tmp_path / ".claude" / "skills" / "winhands" / "SKILL.md"
    assert skill.read_text(encoding="utf-8") == cli.skill_text()
    assert any("Restart Claude Code" in line for line in out)


def test_setup_is_idempotent_when_remove_fails(tmp_path):
    run = Run(1, 0)                                    # the entry did not exist yet: remove fails, add works
    assert cli.setup(home=tmp_path, which=_which(claude=CLAUDE), run=run, command=CMD, out=lambda s: None) == 0
    assert len(run.calls) == 2


def test_setup_reports_a_failed_add(tmp_path):
    out = []
    run = Run(0, 3)
    assert cli.setup(home=tmp_path, which=_which(claude=CLAUDE), run=run, command=CMD, out=out.append) == 1
    assert any("claude mcp add" in line for line in out)


def test_setup_without_claude_prints_the_json_snippet(tmp_path):
    out = []
    run = Run()
    assert cli.setup(home=tmp_path, which=_which(), run=run, command=CMD, out=out.append) == 0
    assert run.calls == []
    blob = next(line for line in out if line.lstrip().startswith("{"))
    assert json.loads(blob) == {"mcpServers": {"winhands": {"command": PY, "args": ["-m", "winhands"]}}}
    assert (tmp_path / ".claude" / "skills" / "winhands" / "SKILL.md").is_file()


def test_setup_dry_run_changes_nothing(tmp_path):
    out = []
    assert cli.setup(dry_run=True, home=tmp_path, which=_which(claude=CLAUDE),
                     run=lambda *a, **k: pytest.fail("ran a command"), command=CMD, out=out.append) == 0
    assert list(tmp_path.iterdir()) == []
    assert any("dry run" in line.lower() for line in out)


def test_remove_unregisters_and_deletes_the_skill(tmp_path):
    d = tmp_path / ".claude" / "skills" / "winhands"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("x", encoding="utf-8")
    run = Run()
    assert cli.setup(remove=True, home=tmp_path, which=_which(claude=CLAUDE), run=run, command=CMD,
                     out=lambda s: None) == 0
    assert run.calls == [[CLAUDE, "mcp", "remove", "winhands", "--scope", "user"]]
    assert not d.exists()


def test_remove_is_fine_with_nothing_installed_and_no_claude(tmp_path):
    assert cli.setup(remove=True, home=tmp_path, which=_which(), run=Run(), command=CMD, out=lambda s: None) == 0


def test_main_passes_setup_flags_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "setup", lambda **kw: seen.update(kw) or 7)
    assert cli.main(["setup", "--remove", "--dry-run"]) == 7
    assert seen == {"remove": True, "dry_run": True}
    assert cli.main(["setup"]) == 7
    assert seen == {"remove": False, "dry_run": False}


def test_unknown_command_is_an_argparse_error():
    with pytest.raises(SystemExit) as e:
        cli.main(["bogus"])
    assert e.value.code == 2


def test_command_is_the_interpreter_dash_m_never_the_launcher_exe():
    # Windows App Control / Smart App Control can block the unsigned winhands.exe launcher, so the registered
    # command must not depend on it, whichever way setup was started.
    assert cli.command_for(executable=PY) == CMD
    assert cli.command_for() == [sys.executable, "-m", "winhands"]
    assert not any(pathlib.PureWindowsPath(p).name.lower() == "winhands.exe" for p in cli.command_for())


TOOL_PY = r"C:\Users\u\AppData\Roaming\uv\tools\winhands\Scripts\python.exe"
UVX_PY = r"C:\Users\u\AppData\Local\uv\cache\environments-v2\winhands-3f9a\Scripts\python.exe"


@pytest.mark.parametrize("path, ephemeral", [
    (TOOL_PY, False),                                                                          # uv tool install: stable
    (r"C:\Users\u\.winhands\venv\Scripts\python.exe", False),                                  # a plain venv
    (r"C:\Python312\python.exe", False),
    (UVX_PY, True),                                                                            # uvx
    (r"C:\Users\u\AppData\Local\uv\cache\archive-v0\abc\Scripts\python.exe", True),
    (r"C:\Users\u\AppData\Local\uv\cache\builds-v0\.tmpX\Scripts\python.exe", True),
    (r"C:\work\archive-v1\venv\Scripts\python.exe", False),                                    # not under a cache dir
])
def test_ephemeral_uv_environments_are_detected(path, ephemeral):
    assert cli.is_ephemeral(path) is ephemeral


def test_setup_refuses_to_register_a_throwaway_uvx_environment(tmp_path):
    out = []
    code = cli.setup(home=tmp_path, which=_which(claude=CLAUDE), run=lambda *a, **k: pytest.fail("ran a command"),
                     command=[UVX_PY, "-m", "winhands"], out=out.append)
    assert code == 1
    assert any("uv tool install winhands" in line for line in out)
    assert list(tmp_path.iterdir()) == []                       # nothing was installed either


def test_remove_works_even_from_a_throwaway_environment(tmp_path):
    assert cli.setup(remove=True, home=tmp_path, which=_which(), run=Run(), command=[UVX_PY, "-m", "winhands"],
                     out=lambda s: None) == 0
