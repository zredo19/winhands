import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

def _speech(*segments):
    """Fake VAD: the speech segments (seconds) that lie inside the audio pushed so far."""
    def detect(audio, rate):
        n = len(audio) / rate
        return [(s, min(e, n)) for s, e in segments if s < n]
    return detect


def blocks(ep, seconds, block=0.1, rate=100):
    import numpy as np
    return [ep.push(np.zeros(int(block * rate), np.float32)) for _ in range(round(seconds / block))]


def test_speech_endpoint_waits_through_a_thinking_pause():
    from winhands.exo.voice import SpeechEndpoint
    # "sácate una captura ... (1.6 s thinking) ... y mándasela a Iturra"
    ep = SpeechEndpoint(_speech((0.5, 3.0), (4.6, 7.0)), rate=100, silence=2.0, every=1)
    out = blocks(ep, 9.0)
    assert not any(out[:89]) and out[89] is True and ep.heard          # ends 2 s after the last word, not at the pause


def test_speech_endpoint_gives_up_on_silence_and_caps_length():
    from winhands.exo.voice import SpeechEndpoint
    ep = SpeechEndpoint(_speech(), rate=100, max_wait=1.0, every=1)
    out = blocks(ep, 1.0)
    assert out[-1] is True and not any(out[:-1]) and not ep.heard
    ep = SpeechEndpoint(_speech((0.0, 99.0)), rate=100, max_len=2.0, every=1)
    assert blocks(ep, 2.0)[-1] is True


def test_sentences_split_a_reply_so_speech_can_start_early():
    from winhands.exo.voice import sentences
    assert sentences("Entendido, abro Discord. Listo, ya está abierto.") == ["Entendido, abro Discord.", "Listo, ya está abierto."]
    assert sentences("¿Seguro? ¡Dale! Son las 3.45 pm") == ["¿Seguro?", "¡Dale!", "Son las 3.45 pm"]
    assert sentences("  ") == []
    long = "uno, " * 60 + "fin."                                          # no full stop for a long stretch
    assert all(len(c) <= 220 for c in sentences(long)) and "".join(sentences(long)).replace(" ", "") == long.replace(" ", "")


def test_ears_run_whisper_in_a_child_process_that_close_ends():
    import numpy as np
    from winhands.exo.voice import Ears
    fake = [sys.executable, "-c",
            "from winhands.exo.voice import serve; serve(lambda a: f'{len(a)} muestras {a[1]:.1f} ñ', 'test')"]
    ears = Ears(cmd=fake)
    assert ears.transcribe(np.array([0, .5, 0], np.float32)) == "3 muestras 0.5 ñ" and ears.device == "test"
    proc = ears.proc
    ears.close()                                                   # its memory (CUDA runtime included) goes back
    assert ears.proc is None and proc.wait(5) is not None
    assert ears.transcribe(np.zeros(5, np.float32)) == "5 muestras 0.0 ñ"   # starts again on demand
    ears.close()


class _Local:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio):
        self.calls.append("transcribe")
        return "local"

    def close(self):
        self.calls.append("close")


def test_cloud_ears_use_groq_and_fall_back_to_local_whisper_so_no_request_is_lost():
    import numpy as np
    from winhands.exo.voice import CloudEars
    sent, local = [], _Local()
    ears = CloudEars("k", send=lambda audio, key: sent.append(key) or "abre discord", local=local)
    ears.start()                                                       # nothing loads on this PC
    assert ears.transcribe(np.zeros(5, np.float32)) == "abre discord" and sent == ["k"] and local.calls == []

    def offline(audio, key):
        raise OSError("no internet")
    ears.send = offline
    assert ears.transcribe(np.zeros(5, np.float32)) == "local" and local.calls == ["transcribe"]
    ears.close()
    assert local.calls[-1] == "close"


def test_groq_request_is_a_spanish_whisper_upload_of_a_16khz_wav(monkeypatch):
    import io, json, urllib.request, wave
    import numpy as np
    from winhands.exo import voice
    seen = {}

    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def urlopen(req, timeout):
        seen["req"] = req
        return Reply(json.dumps({"text": " abre discord "}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert voice.groq(np.zeros(voice.RATE, np.float32), "k") == "abre discord"
    req = seen["req"]
    assert req.full_url == voice.GROQ_URL and req.get_header("Authorization") == "Bearer k"
    body = req.data
    for field in (b'name="model"\r\n\r\nwhisper-large-v3\r\n', b'name="language"\r\n\r\nes\r\n'):
        assert field in body
    wav = body[body.index(b"RIFF"):]
    with wave.open(io.BytesIO(wav)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (voice.RATE, 1, 2)
