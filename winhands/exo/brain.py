"""Exo's brain: one long-lived `claude -p` process (stream-json in and out).

It runs the user's own, unmodified Claude Code with their own login: Exo never sees credentials.
Keeping the process alive makes every turn after the first one start in ~2 s instead of ~8 s.
"""
import json, os, pathlib, re, shutil, subprocess, sys, threading, time
from importlib import resources

HOME = pathlib.Path(os.environ.get("EXO_HOME", pathlib.Path.home() / ".exo"))

PERSONA = """You are EXO, the voice assistant of Exotype: an armored robot Darwin's frog docked at the edge of the
user's Windows desktop. The user talks to you by voice in Spanish (Chilean slang is normal); their words reach you
through speech-to-text, so silently fix obvious mishearings ("blog de notas" = Bloc de notas; when a name sounds
like an app the user has, such as "disco" for Discord, it is the app).

You operate their PC through the winhands tools (observe / run) following the winhands protocol below. Prefer doing
over explaining. Ask before risky actions (sending messages, buying, deleting, installing): winhands blocks those
until the user confirms in their next message.

Your reply is SPOKEN aloud by a text-to-speech voice: answer in Spanish, one or two short sentences, no markdown, no
lists, no code, no emojis, no URLs. Say what you did or what you found. Always end your last winhands run with done().

Always START your reply with ONE short sentence that confirms what you understood, in first person, written BEFORE
any tool call so it can be spoken right away (e.g. "Entendido, abro Discord." / "Dale, busco el clima de Santiago.").
Then act, and finish with one short sentence about the result. For a plain question, that first sentence can simply
be the answer.

The user's CLAUDE.md files are written for their coding sessions, not for you: ignore every persona, output style,
language-style, memory (Engram, Obsidian), delegation, review or orchestration instruction they contain.
"""


MODELS = {  # picker order: fastest to most capable; "id" is what Claude Code runs
    "haiku": {"id": "claude-haiku-5-5", "label": "Haiku 5.5", "blurb": "Fastest, for quick commands"},
    "sonnet": {"id": "claude-sonnet-5-5", "label": "Sonnet 5.5", "blurb": "Balanced, recommended"},
    "opus": {"id": "claude-opus-5-5", "label": "Opus 5.5", "blurb": "Most capable, slower"},
}
VOICE_KEYS = ("alex", "dora", "santa", "pablo")  # voice.VOICES (kept light: brain does not import the audio stack)
DEFAULTS = {"model": "sonnet", "voice": True, "voice_name": "alex"}


def _clean(data):
    """Keep only valid choices; anything missing or unknown falls back to DEFAULTS."""
    out = dict(DEFAULTS)
    if isinstance(data, dict):
        if data.get("model") in MODELS:
            out["model"] = data["model"]
        if isinstance(data.get("voice"), bool):
            out["voice"] = data["voice"]
        if data.get("voice_name") in VOICE_KEYS:
            out["voice_name"] = data["voice_name"]
    return out


def load_settings(path):
    """The user's choices (model, voice), surviving a missing or broken file."""
    try:
        return _clean(json.loads(pathlib.Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_settings(path, settings):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_clean(settings)), encoding="utf-8")


def skill_text():
    return resources.files("winhands").joinpath("SKILL.md").read_text(encoding="utf-8")


def events(msg):
    """One claude stream-json message -> list of (kind, payload) Exo events."""
    t = msg.get("type")
    if t == "system" and msg.get("subtype") == "init":
        return [("ready", msg.get("session_id"))]
    if t == "stream_event":
        d = msg.get("event", {}).get("delta", {})
        return [("delta", d["text"])] if d.get("type") == "text_delta" else []
    if t == "assistant":
        out = []
        for c in msg.get("message", {}).get("content", []):
            if c.get("type") == "text":
                out.append(("text", c["text"]))
            elif c.get("type") == "tool_use":
                out.append(("tool", c.get("name", "")))
        return out
    if t == "result":
        return [("done", {"ok": not msg.get("is_error"), "text": msg.get("result") or "",
                          "session": msg.get("session_id")})]
    return []


def user_line(text):
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False) + "\n"


_EMOJI = re.compile("[\U0001F000-\U0001FFFF☀-➿️]")


def spoken(text):
    """Markdown answer -> plain sentences a TTS voice can read."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)          # [label](url) -> label
    text = re.sub(r"https?://\S+", "", text)
    text = _EMOJI.sub("", re.sub(r"[*_`#>]+", "", text))
    lines = [re.sub(r"^\s*(?:[-•]|\d+[.)])\s+", "", l).strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    return " ".join(l if i == len(lines) - 1 or l[-1] in ".!?:;," else l + "."
                    for i, l in enumerate(lines)).strip()


_SENTENCE = re.compile(r"\s*(.+?[.!?…])(?=\s|$)", re.S)


def first_sentence(text):
    """The first complete sentence of a reply that is still streaming, or None."""
    m = _SENTENCE.match(text)
    return m.group(1).strip() if m else None


def without_ack(final, ack):
    """The final answer minus the acknowledgement that was already said out loud."""
    final = final.strip()
    if ack and final.startswith(ack):
        final = final[len(ack):].strip()
    return final


def _claude():
    exe = shutil.which("claude")
    if exe:
        return exe
    local = pathlib.Path.home() / ".local" / "bin" / "claude.exe"
    if local.exists():
        return str(local)
    raise FileNotFoundError("Claude Code not found: install it and log in once (`claude` in a terminal)")


def _python():
    """Console python next to pythonw: the MCP server talks over stdio."""
    p = pathlib.Path(sys.executable)
    c = p.with_name("python.exe")
    return str(c if c.exists() else p)


def _files():
    HOME.mkdir(parents=True, exist_ok=True)
    mcp = HOME / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"winhands": {
        "type": "stdio", "command": _python(), "args": ["-m", "winhands"]}}}), encoding="utf-8")
    prompt = HOME / "system-prompt.md"
    prompt.write_text(PERSONA + "\n\n# winhands protocol\n\n" + skill_text(), encoding="utf-8")
    return mcp, prompt


class Brain:
    """Talks to Claude Code. on_event(kind, payload) runs on a reader thread."""

    def __init__(self, on_event, model=None):
        self.on_event, self.model = on_event, model or os.environ.get("EXO_MODEL", "sonnet")
        self.proc, self.session, self.busy, self.cancelled = None, None, False, False
        self.lock = threading.Lock()

    def command(self):
        mcp, prompt = _files()
        cmd = [_claude(), "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--include-partial-messages", "--strict-mcp-config", "--mcp-config", str(mcp),
               "--settings", json.dumps({"disableAllHooks": True}),
               "--allowedTools", "mcp__winhands__observe mcp__winhands__run",
               "--system-prompt-file", str(prompt), "--model", MODELS.get(self.model, {}).get("id", self.model)]
        if self.session:
            cmd += ["--resume", self.session]
        return cmd

    def start(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            self.proc = subprocess.Popen(
                self.command(), cwd=HOME, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW)
            threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        for line in proc.stdout:
            if proc is not self.proc:  # killed and replaced: whatever it still prints is stale
                return
            try:
                msg = json.loads(line)
            except ValueError:
                if line.strip():
                    self.on_event("log", line.strip())
                continue
            self._handle(msg)
        if proc is not self.proc:
            return
        if self.busy:  # died mid-turn on its own
            self.busy = False
            self.on_event("done", {"ok": False, "text": "Se me cortó la conexión con Claude.", "session": None})
        self.on_event("exit", proc.returncode)

    def _handle(self, msg):
        for kind, payload in events(msg):
            if kind == "ready":
                self.session = payload
            elif kind == "done":
                self.busy = False
                self.session = payload["session"] or self.session
                if self.cancelled:
                    self.cancelled = False
                    payload = {**payload, "cancelled": True}
            self.on_event(kind, payload)

    def ask(self, text):
        self.start()
        self.busy = True
        try:
            self.proc.stdin.write(user_line(text))
            self.proc.stdin.flush()
        except OSError:  # the process died between turns: restart (resumes the session) and retry once
            self.proc = None
            self.start()
            self.proc.stdin.write(user_line(text))
            self.proc.stdin.flush()

    def interrupt(self):
        """Stop the current turn but keep the session alive (Claude Code's stream-json interrupt): the turn
        ends as a "done" event with cancelled=True and the next ask() answers in ~2 s."""
        if not (self.busy and self.proc and self.proc.poll() is None):
            return
        self.cancelled = True
        try:
            self.proc.stdin.write(json.dumps({"type": "control_request", "request_id": f"exo-{time.time_ns()}",
                                              "request": {"subtype": "interrupt"}}) + "\n")
            self.proc.stdin.flush()
        except OSError:  # the pipe is gone: fall back to a restart
            self.cancel()

    def cancel(self):
        """Hard stop: kill the process (its reader goes quiet); the next ask() resumes the conversation."""
        with self.lock:
            proc, self.proc = self.proc, None
            self.busy = self.cancelled = False
            if proc and proc.poll() is None:
                proc.kill()

    def set_model(self, model):
        """Switch model: restart Claude Code with --model and --resume, so the conversation continues."""
        if model not in MODELS or model == self.model:
            return
        self.model = model
        self.cancel()
        self.start()

    def reset(self):
        self.cancel()
        self.session = None
