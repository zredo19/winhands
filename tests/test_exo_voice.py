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
