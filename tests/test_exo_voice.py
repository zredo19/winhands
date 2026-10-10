import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from winhands.exo.voice import Endpoint


def feed(ep, levels):
    return [ep.push(x) for x in levels]


def test_endpoint_stops_after_speech_then_silence():
    ep = Endpoint(block=0.1, silence=0.5, threshold=0.02, max_wait=5)
    out = feed(ep, [0.001] * 10 + [0.1] * 8 + [0.001] * 5)
    assert out[:-1] == [False] * 22 and out[-1] is True


def test_endpoint_ignores_short_pauses_and_gives_up_without_speech():
    ep = Endpoint(block=0.1, silence=0.5, threshold=0.02, max_wait=5)
    assert not any(feed(ep, [0.1] * 5 + [0.001] * 3 + [0.1] * 5 + [0.001] * 4))   # 0.3 s / 0.4 s pauses
    ep = Endpoint(block=0.1, silence=0.5, threshold=0.02, max_wait=1.0)
    out = feed(ep, [0.001] * 10)
    assert out[-1] is True and not any(out[:-1]) and not ep.heard


def test_endpoint_has_a_hard_cap():
    ep = Endpoint(block=0.1, silence=0.5, threshold=0.02, max_wait=5, max_len=1.0)
    assert feed(ep, [0.1] * 10)[-1] is True
