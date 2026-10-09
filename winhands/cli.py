"""winhands command line.

No arguments: run the MCP stdio server (what MCP clients launch; argparse is never touched on that path).
`winhands setup` registers the server with Claude Code and installs the skill; `winhands --version`.
"""
import json, pathlib, shutil, subprocess, sys
from importlib import resources

from . import __version__

SKILL_PARTS = (".claude", "skills", "winhands")


def command_for(executable=None):
    """argv that launches this winhands: `<python> -m winhands`. Never the winhands.exe launcher: it is unsigned and
    Windows App Control / Smart App Control can block it (one friend's PC did), while the interpreter keeps working."""
    return [executable or sys.executable, "-m", "winhands"]


def is_ephemeral(executable):
    """True for a throwaway uv environment (uvx, uv run --with): its path disappears when uv cleans its cache."""
    parts = [p.lower() for p in pathlib.PureWindowsPath(executable).parts]
    return "cache" in parts and any(p.startswith(("environments-v", "archive-v", "builds-v")) for p in parts)


def skill_text():
    return resources.files("winhands").joinpath("SKILL.md").read_text(encoding="utf-8")


def _warm():
    """Import the server once so the .pyc files exist: the first MCP start of a fresh install took ~7.7 s, with this ~1.5 s."""
    import importlib
    importlib.import_module("uiautomation")
    importlib.import_module("winhands.server")


def setup(remove=False, dry_run=False, home=None, which=shutil.which, run=subprocess.run, command=None, out=print, warm=None):
    """Register (or with remove=True unregister) the MCP server in Claude Code and the skill folder. Returns an exit code."""
    skill = pathlib.Path(home or pathlib.Path.home()).joinpath(*SKILL_PARTS)
    command = command or command_for()
    if not remove and is_ephemeral(command[0]):
        out("winhands is running from a temporary uvx environment, so there is no stable command to register.\n"
            "Run `uv tool install winhands` first, then `winhands setup`.")
        return 1
    claude = which("claude")
    tag = "(dry run) " if dry_run else ""

    def claude_mcp(*args):
        cmd = [claude, "mcp", *args]
        out(f"{tag}$ {' '.join(cmd)}")
        return None if dry_run else run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True)

    if remove:
        if claude:
            claude_mcp("remove", "winhands", "--scope", "user")
        out(f"{tag}remove {skill}")
        if not dry_run:
            shutil.rmtree(skill, ignore_errors=True)
        out("Dry run finished. Nothing was changed." if dry_run else "Removed the MCP entry and the skill.")
        return 0

    out(f"{tag}install the skill into {skill}")
    if not dry_run:
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(skill_text(), encoding="utf-8")
    if claude:
        claude_mcp("remove", "winhands", "--scope", "user")  # re-running setup refreshes the entry; failure = it was not there
        r = claude_mcp("add", "winhands", "--scope", "user", "--", *command)
        if r is not None and r.returncode:
            out(f"`claude mcp add` failed (exit {r.returncode}): {(getattr(r, 'stderr', '') or '').strip()}")
            return 1
    else:
        out("Claude Code CLI not found. For other MCP clients add this server to their config:")
        out(json.dumps({"mcpServers": {"winhands": {"command": command[0], **({"args": command[1:]} if command[1:] else {})}}}, indent=2))
    if not dry_run:
        out("Warming up (so the first start in Claude Code is fast)...")
        try:
            (warm or _warm)()
        except Exception as e:  # best effort: a missing display or a broken import must not fail the setup
            out(f"Warm-up skipped: {e}")
    out("Dry run finished. Nothing was changed." if dry_run else
        'Done. Restart Claude Code, then ask it: "open Notepad and type hello".')
    return 0


def main(argv=None, serve=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        if serve is None:
            from .server import main as serve
        serve()
        return 0
    import argparse
    p = argparse.ArgumentParser(prog="winhands", description="Computer use MCP server for Windows. Run without arguments to start it.")
    p.add_argument("--version", action="store_true", help="print the version and exit")
    s = p.add_subparsers(dest="cmd").add_parser("setup", help="register winhands with Claude Code and install its skill")
    s.add_argument("--remove", action="store_true", help="undo setup (MCP entry and skill folder)")
    s.add_argument("--dry-run", action="store_true", help="show what would be done without changing anything")
    a = p.parse_args(argv)
    if a.version:
        print(f"winhands {__version__}")
        return 0
    if a.cmd == "setup":
        return setup(remove=a.remove, dry_run=a.dry_run)
    p.print_help()
    return 0
