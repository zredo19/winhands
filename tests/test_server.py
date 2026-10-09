import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from winhands import server


def test_run_observe_prints_and_returns_text():
    said = []

    class D:
        def observe(self, target=None, mode="tree"):
            return {"text": f"tree {target} {mode}"}
    out = server._echoing_observe(D(), said.append)(5, "diff")
    assert out == "tree 5 diff" and said == ["tree 5 diff"]
    assert isinstance(out, server.Echoed)


def test_echo_last_skips_values_already_printed():
    said = []
    for v in (server.Echoed("seen"), None, "s", 3):
        server._echo_last(v, said.append)
    assert said == ["s", "3"]


def test_show_accepts_region_keyword(monkeypatch):
    from winhands import uia, vision, memory
    monkeypatch.setattr(memory, "load_skills", lambda ns: [])
    monkeypatch.setattr(vision, "pending", [])
    calls = []

    class Shot:
        id, size, box = 1, (10, 10), (0, 0, 10, 10)
    monkeypatch.setattr(vision, "make_shot", lambda region, edge, grid, items=None, hwnd=None:
                        (calls.append((region, hwnd)), (Shot(), b"jpeg"))[1])
    ns = server._namespace(object.__new__(uia.Desk))
    assert ns["show"](region=(1, 2, 3, 4)) == 1
    assert ns["show"]((5, 6, 7, 8)) == 1                       # positional keeps working
    assert calls == [((1, 2, 3, 4), None), ((5, 6, 7, 8), None)]


class _Ov:
    def __init__(self):
        self.calls = []

    def set_state(self, s):
        self.calls.append(("state", s))

    def hide(self, linger=0.0):
        self.calls.append(("hide", linger))


def test_overlay_is_acting_while_a_run_acts():
    ov = _Ov()
    server._overlay_state(ov, "acting")
    assert ov.calls == [("state", "acting")]
    server._overlay_state(None, "acting")                      # no overlay: silently nothing


def test_run_end_thinks_while_lingering_hides_when_done_and_stops_on_abort():
    ov = _Ov()
    server._overlay_end(ov, stopped=False, done=False)
    assert ov.calls == [("state", "thinking"), ("hide", server.LINGER)]
    ov.calls.clear()
    server._overlay_end(ov, stopped=False, done=True)
    assert ov.calls == [("hide", 0)]
    ov.calls.clear()
    server._overlay_end(ov, stopped=True, done=False)          # Ctrl+Alt+Q / user took over: the overlay runs its own stop sequence
    assert ov.calls == [("state", "stopped")]
    server._overlay_end(None, stopped=True, done=False)


def test_overlay_failures_are_cosmetic():
    class Bad(_Ov):
        def set_state(self, s):
            raise OSError("window gone")
    server._overlay_state(Bad(), "acting")
    server._overlay_end(Bad(), stopped=False, done=False)
