import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from winhands import inputs
from winhands.inputs import _norm, interpolate, scan_of, combo


def test_norm_hits_every_pixel():
    # Windows maps normalized n -> pixel floor(n*w/65536); aiming at pixel centres hits all
    for w in (768, 1080, 1920, 3840):
        assert all(_norm(p, w) * w >> 16 == p for p in range(w))


def test_interpolate_limits_step_and_keeps_endpoints():
    pts = interpolate([(0, 0), (100, 0), (100, 30)], max_step=10)
    assert pts[0] == (0, 0) and pts[-1] == (100, 30)
    assert (100, 0) in pts
    assert all(abs(x1 - x0) <= 10 and abs(y1 - y0) <= 10 for (x0, y0), (x1, y1) in zip(pts, pts[1:]))


def test_interpolate_single_point():
    assert interpolate([(5, 5)]) == [(5, 5)]


def test_scan_codes_named_and_extended():
    assert scan_of("w") == 0x11 and scan_of("W") == 0x11
    assert scan_of("space") == 0x39 and scan_of("shift") == 0x2A
    assert scan_of("up") == 0xE048 and scan_of("delete") == 0xE053  # E0-prefixed
    assert scan_of(0x1C) == 0x1C  # raw scan code passes through


def test_combo_parses_modifiers_in_order():
    assert combo("ctrl+shift+s") == [0x1D, 0x2A, 0x1F]
    assert combo("alt+f4") == [0x38, 0x3E]


def test_extended_flag_only_for_e0_codes():
    assert inputs.key_input(0xE048).u.ki.dwFlags & 0x1          # EXTENDEDKEY
    assert not inputs.key_input(0x38).u.ki.dwFlags & 0x1        # left Alt is NOT extended
    assert inputs.key_input(0x38, up=True).u.ki.dwFlags & 0x2   # KEYUP


def test_busy_only_while_injecting_or_holding(monkeypatch):
    monkeypatch.setattr(inputs, "held", set())
    inputs.last[0] = 0.0
    assert not inputs.busy()
    inputs.last[0] = inputs.time.monotonic()
    assert inputs.busy()
    inputs.last[0] = 0.0
    inputs.held.add(("key", 0x11))
    assert inputs.busy()


def test_touch_asks_guard_before_marking(monkeypatch):
    def refuse():
        raise RuntimeError("user is busy")
    monkeypatch.setattr(inputs, "guard", refuse)
    inputs.last[0] = 0.0
    try:
        inputs.touch()
    except RuntimeError:
        pass
    assert inputs.last[0] == 0.0


def test_mouse_down_reports_the_click_to_the_overlay(monkeypatch):
    sent, clicks = [], []
    monkeypatch.setattr(inputs, "send", lambda *a: sent.append(a))
    monkeypatch.setattr(inputs, "on_click", clicks.append, raising=False)
    monkeypatch.setattr(inputs, "held", set())
    inputs.mouse_down("right")
    assert clicks == ["right"] and len(sent) == 1
    inputs.mouse_up("right")
    assert clicks == ["right"]                                  # a release is not a click
