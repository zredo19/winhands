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
