import json, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]

from exo_import_sessions import chat_from_session, import_sessions


def _line(kind, content, at):
    return json.dumps({"type": kind, "timestamp": at, "message": {"role": kind, "content": content}})


def _session(path):
    path.write_text("\n".join([
        json.dumps({"type": "queue-operation"}),
        _line("user", '[The user was in the window "Netflix" (msedge.exe) when they spoke.]\nabre discord',
              "2026-10-15T01:26:27.352Z"),
        _line("assistant", [{"type": "text", "text": "Entendido, abro Discord."}], "2026-10-15T01:26:30.000Z"),
        _line("assistant", [{"type": "tool_use", "name": "mcp__winhands__run"}], "2026-10-15T01:26:31.000Z"),
        _line("user", [{"type": "tool_result", "content": "ok"}], "2026-10-15T01:26:32.000Z"),
        _line("assistant", [{"type": "text", "text": "Sigo en eso."}], "2026-10-15T01:26:33.000Z"),
        _line("assistant", [{"type": "text", "text": "Listo, **Discord** abierto."}], "2026-10-15T01:26:40.000Z"),
        _line("user", "eres un genio", "2026-10-15T01:27:00.000Z"),
        _line("assistant", [{"type": "text", "text": "¡Gracias!"}], "2026-10-15T01:27:02.000Z"),
    ]), encoding="utf-8")


def test_a_claude_code_session_becomes_the_chat_exo_would_have_shown(tmp_path):
    f = tmp_path / "s1.jsonl"
    _session(f)
    chat = chat_from_session(f)
    assert chat["session"] == "s1" and chat["title"] == "abre discord" and chat["id"].isdigit()
    assert [(m["who"], m["text"], m.get("kind")) for m in chat["messages"]] == [
        ("user", "abre discord", None),
        ("exo", "Entendido, abro Discord.", "ack"),          # said out loud right away
        ("exo", "Listo, Discord abierto.", None),             # the final answer; "Sigo en eso." was only streamed
        ("user", "eres un genio", None),
        ("exo", "¡Gracias!", "ack"),
    ]
    assert chat["messages"][0]["at"] < chat["messages"][-1]["at"]


def test_only_sessions_the_exo_app_opened_are_imported_once(tmp_path):
    sessions, chats = tmp_path / "sessions", tmp_path / "chats"
    sessions.mkdir()
    _session(sessions / "s1.jsonl")
    _session(sessions / "test-script.jsonl")                  # a dev script, never logged by the app
    log = tmp_path / "exo.log"
    log.write_text("2026-10-15 01:26:30 brain ready: s1\n", encoding="utf-8")
    assert import_sessions(sessions, log, chats) == 1
    assert import_sessions(sessions, log, chats) == 0          # running it again adds nothing
    [saved] = chats.glob("*.json")
    assert json.loads(saved.read_text(encoding="utf-8"))["session"] == "s1"
