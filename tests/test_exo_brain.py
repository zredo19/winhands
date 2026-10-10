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
