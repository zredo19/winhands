import sys, pathlib, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


class Part:
    """Stands in for Ears / Mouth / Brain: records every call."""
    busy = False

    def __init__(self, name, log):
        self.name, self.log = name, log

    def __getattr__(self, verb):
        return lambda *a: self.log.append(f"{self.name}.{verb}")


def exo_at_rest(log):
    from winhands.exo.app import Exo
    exo = object.__new__(Exo)
    exo.ears, exo.mouth, exo.brain = Part("ears", log), Part("mouth", log), Part("brain", log)
    exo.mouth.busy = lambda: False
    exo.state, exo.listening, exo.awake, exo.last_used = "idle", False, True, time.time()
    return exo


def test_exo_dozes_after_idle_minutes_and_lets_whisper_voice_and_claude_go():
    from winhands.exo.app import IDLE
    log = []
    exo = exo_at_rest(log)
    t = exo.last_used
    assert not exo.doze(now=t + IDLE - 1) and log == []                 # still in use
    exo.state = "working"
    assert not exo.doze(now=t + IDLE) and log == []                     # never in the middle of a turn
    exo.state = "idle"
    assert exo.doze(now=t + IDLE)
    assert sorted(log) == ["brain.cancel", "ears.close", "mouth.unload"]
    assert not exo.doze(now=t + 2 * IDLE)                                # already asleep: nothing more to free


def test_the_page_collects_its_garbage_while_the_character_animates():
    # every animation frame leaves garbage that idle Chromium never collects: +18 MB/min without this
    from winhands.exo.app import CHROMIUM_FLAGS, UI
    assert "--js-flags=--expose-gc" in CHROMIUM_FLAGS
    assert "gc()" in UI.with_name("mascot.js").read_text(encoding="utf-8")


def test_waking_up_brings_back_whisper_voice_and_claude():
    log = []
    exo = exo_at_rest(log)
    exo.awake = False
    exo._warm()
    assert log == ["ears.start", "mouth.warm", "brain.start"] and exo.awake


def exo_with_history(log, tmp_path):
    exo = exo_at_rest(log)
    exo.chats_dir, exo.chat, exo.turn, exo.sent = tmp_path, None, 0, []
    exo._emit = exo.sent.append
    exo.brain.session = "s1"
    return exo


def test_conversations_are_saved_and_reopen_on_their_claude_session(tmp_path):
    log = []
    exo = exo_with_history(log, tmp_path)
    exo.emit("bubble", {"who": "exo", "text": "Ahora pienso con Opus 5.5", "kind": "status"})
    assert exo.chats() == []                                             # nobody spoke yet: no chat
    exo.emit("bubble", {"who": "user", "text": "abre discord"})
    exo.emit("bubble", {"who": "exo", "text": "Listo.", "ok": True})
    exo.reset()
    exo.brain.session = None                                             # a new conversation
    [chat] = exo.chats()
    assert chat["title"] == "abre discord" and chat["at"] > 0
    exo.sent.clear()
    exo.open_chat(chat["id"])
    assert exo.brain.session == "s1"                                     # talking again continues it
    assert any('"reset"' in js for js in exo.sent)
    assert any('"history"' in js and "abre discord" in js and "Listo." in js for js in exo.sent)
    exo.emit("bubble", {"who": "user", "text": "y ahora spotify"})
    [again] = exo.chats()
    assert again["id"] == chat["id"] and again["on"]


def test_open_chat_ignores_anything_that_is_not_a_saved_chat(tmp_path):
    log = []
    exo = exo_with_history(log, tmp_path)
    (tmp_path / "123.json").write_text("{broken", encoding="utf-8")
    exo.open_chat("../preferences")
    exo.open_chat("123")
    exo.open_chat("999")
    assert log == [] and exo.sent == [] and exo.chats() == []
