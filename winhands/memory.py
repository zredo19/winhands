"""Cross-session memory: per-app notes and reusable skills (Voyager-style) under ~/.winhands."""
import os, pathlib, re

HOME = pathlib.Path(os.path.expanduser("~")) / ".winhands"


def _app(app):
    return re.sub(r"\.exe$", "", app.strip().lower())


def note(text, app, home=HOME):
    """Remember a fact about an app (shown on the first observe of that app)."""
    p = pathlib.Path(home) / "notes"
    p.mkdir(parents=True, exist_ok=True)
    with open(p / f"{_app(app)}.md", "a", encoding="utf-8") as f:
        f.write(f"- {' '.join(text.split())}\n")


def notes(app=None, query=None, home=HOME):
    """Notes of one app, or lines matching query across all apps ('app: line')."""
    p = pathlib.Path(home) / "notes"
    if app:
        f = p / f"{_app(app)}.md"
        return f.read_text(encoding="utf-8").strip() if f.exists() else ""
    q = (query or "").lower()
    return "\n".join(f"{f.stem}: {line[2:]}" for f in sorted(p.glob("*.md"))
                     for line in f.read_text(encoding="utf-8").splitlines() if q in line.lower())


def save_skill(name, code, doc="", home=HOME):
    """Persist reusable code (functions) that is auto-loaded into the REPL next sessions."""
    if not name.isidentifier():
        raise ValueError(f"skill name must be a Python identifier: {name!r}")
    compile(code, name, "exec")  # syntax check before saving
    p = pathlib.Path(home) / "skills"
    p.mkdir(parents=True, exist_ok=True)
    (p / f"{name}.py").write_text(f'"""{doc.strip()}"""\n{code.rstrip()}\n', encoding="utf-8")
    return str(p / f"{name}.py")


def skills(home=HOME):
    """'name: doc' for every saved skill."""
    out = []
    for f in sorted((pathlib.Path(home) / "skills").glob("*.py")):
        m = re.match(r'"""(.*?)"""', f.read_text(encoding="utf-8"), re.S)
        out.append(f"{f.stem}: {m.group(1).strip() if m else ''}")
    return "\n".join(out) or "(no skills yet)"


def load_skills(ns, home=HOME):
    """Exec every skill file into ns; a broken skill is reported, never raised."""
    loaded = []
    for f in sorted((pathlib.Path(home) / "skills").glob("*.py")):
        try:
            exec(compile(f.read_text(encoding="utf-8"), str(f), "exec"), ns)
            loaded.append(f.stem)
        except Exception as e:
            loaded.append(f"{f.stem} (error: {type(e).__name__}: {e})")
    return loaded
