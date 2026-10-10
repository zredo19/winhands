import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from winhands.exo.brain import events, user_line, spoken


def test_events_map_claude_stream_json_to_exo_events():
    assert events({"type": "system", "subtype": "init", "session_id": "s1"}) == [("ready", "s1")]
    delta = {"type": "stream_event", "event": {"type": "content_block_delta",
                                               "delta": {"type": "text_delta", "text": "Hola"}}}
    assert events(delta) == [("delta", "Hola")]
    msg = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Abriendo."}, {"type": "tool_use", "name": "mcp__winhands__run"}]}}
    assert events(msg) == [("text", "Abriendo."), ("tool", "mcp__winhands__run")]
    ok = {"type": "result", "subtype": "success", "is_error": False, "result": "Listo.", "session_id": "s1"}
    assert events(ok) == [("done", {"ok": True, "text": "Listo.", "session": "s1"})]
    bad = {"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": "s1"}
    assert events(bad)[0][1]["ok"] is False
    assert events({"type": "user"}) == [] and events({"type": "stream_event", "event": {}}) == []


def test_user_line_is_one_stream_json_message():
    import json
    line = user_line('abre "notas"\ny escribe')
    assert line.endswith("\n") and line.count("\n") == 1
    assert json.loads(line) == {"type": "user", "message": {"role": "user", "content": 'abre "notas"\ny escribe'}}


def test_spoken_strips_markdown_for_text_to_speech():
    assert spoken("**Listo**, abrí `notepad.exe`.\n- uno\n- dos") == "Listo, abrí notepad.exe. uno. dos"
    assert spoken("Mira [la página](https://x.cl) 😀") == "Mira la página"


class _Proc:
    def __init__(self, lines=()):
        import io
        self.stdin, self.stdout, self.returncode = io.StringIO(), iter(lines), 0

    def poll(self):
        return None


def test_interrupt_sends_a_control_request_and_the_turn_ends_as_cancelled():
    import json
    from winhands.exo.brain import Brain
    seen = []
    b = Brain(lambda k, p: seen.append((k, p)))
    b.proc, b.busy = _Proc(), True
    b.interrupt()
    msg = json.loads(b.proc.stdin.getvalue())
    assert msg["type"] == "control_request" and msg["request"] == {"subtype": "interrupt"}
    b._handle({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": "s1"})
    assert seen[-1] == ("done", {"ok": False, "text": "", "session": "s1", "cancelled": True}) and not b.busy
    b.busy = True                                                     # the next turn is a normal one again
    b._handle({"type": "result", "subtype": "success", "is_error": False, "result": "Listo.", "session_id": "s1"})
    assert seen[-1] == ("done", {"ok": True, "text": "Listo.", "session": "s1"})


def test_a_replaced_process_never_reports_a_lost_connection():
    import json
    from winhands.exo.brain import Brain
    seen = []
    b = Brain(lambda k, p: seen.append((k, p)))
    old = _Proc([json.dumps({"type": "result", "subtype": "success", "result": "late", "session_id": "s"}) + "\n"])
    b.proc, b.busy = _Proc(), True                                    # old was killed and replaced
    b._read(old)
    assert seen == [] and b.busy


def test_settings_round_trip_and_survive_a_broken_file(tmp_path):
    from winhands.exo.brain import load_settings, save_settings, DEFAULTS
    f = tmp_path / "settings.json"
    assert load_settings(f) == DEFAULTS                                # missing file
    save_settings(f, {"model": "opus", "voice": False})
    assert load_settings(f) == {**DEFAULTS, "model": "opus", "voice": False}
    f.write_text("{broken", encoding="utf-8")
    assert load_settings(f) == DEFAULTS
    save_settings(f, {"model": "gpt-9"})                               # unknown models are ignored
    assert load_settings(f)["model"] == DEFAULTS["model"]


def test_set_model_restarts_claude_on_the_same_conversation(monkeypatch):
    from winhands.exo import brain
    monkeypatch.setattr(brain, "_files", lambda: ("mcp.json", "prompt.md"))
    monkeypatch.setattr(brain, "_claude", lambda: "claude")
    b = brain.Brain(lambda *a: None, model="sonnet")
    b.session, started = "s1", []
    b.start = lambda: started.append(b.command())
    b.set_model("opus")
    cmd = started[-1]
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5" and cmd[cmd.index("--resume") + 1] == "s1"


def test_models_have_an_exact_id_and_a_versioned_label():
    from winhands.exo.brain import MODELS
    assert list(MODELS) == ["haiku", "sonnet", "opus"]                 # fastest to most capable
    assert MODELS["sonnet"]["id"] == "claude-sonnet-5-5" and MODELS["sonnet"]["label"] == "Sonnet 5.5"
    assert all(m["id"].startswith("claude-") and m["blurb"] for m in MODELS.values())


def test_first_sentence_is_found_as_soon_as_it_is_complete():
    from winhands.exo.brain import first_sentence
    assert first_sentence("Entendido, abro") is None                   # still streaming
    assert first_sentence("Entendido, abro Discord. Ya") == "Entendido, abro Discord."
    assert first_sentence("  ¡Vamos! Abriendo") == "¡Vamos!"
    assert first_sentence("Versión 2.5 lista") is None                # a dot inside a number is not an end
    assert first_sentence("Listo.") == "Listo."


def test_without_ack_drops_the_confirmation_already_spoken():
    from winhands.exo.brain import without_ack
    assert without_ack("Entendido, abro Discord. Listo, ya está abierto.", "Entendido, abro Discord.") == \
        "Listo, ya está abierto."
    assert without_ack("Listo, ya está abierto.", "Entendido, abro Discord.") == "Listo, ya está abierto."
    assert without_ack("Entendido, abro Discord.", "Entendido, abro Discord.") == ""
    assert without_ack("Hola.", None) == "Hola."


def test_voice_choice_is_remembered_and_matches_the_voice_engine(tmp_path):
    from winhands.exo.brain import DEFAULTS, VOICE_KEYS, load_settings, save_settings
    from winhands.exo.voice import VOICES
    assert set(VOICE_KEYS) == set(VOICES) and DEFAULTS["voice_name"] == "alex"
    f = tmp_path / "p.json"
    save_settings(f, {"voice_name": "dora"})
    assert load_settings(f)["voice_name"] == "dora"
    save_settings(f, {"voice_name": "siri"})
    assert load_settings(f)["voice_name"] == "alex"
