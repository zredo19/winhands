import ctypes, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import overlay

W, H = 400, 300


def _px(buf, x, y):
    i = (y * W + x) * 4
    return tuple(buf[i:i + 4])  # premultiplied B, G, R, A


def test_frame_is_an_orange_glow_fading_inward():
    buf = overlay.frame(W, H)
    b, g, r, a = _px(buf, 0, H // 2)
    assert a == 255 and r > g > b                                    # solid orange on the edge
    alphas = [_px(buf, x, H // 2)[3] for x in range(overlay.GLOW + 1)]
    assert alphas == sorted(alphas, reverse=True) and alphas[-1] == 0  # gradient to transparent
    assert _px(buf, W // 2, H // 2) == (0, 0, 0, 0)                  # centre untouched
    assert all(max(p[:3]) <= p[3] for p in (_px(buf, x, H // 2) for x in range(overlay.GLOW)))  # premultiplied


def test_frame_draws_the_banner_top_centre():
    top = 10 + 4
    assert _px(overlay.frame(W, H, text="Ctrl+Alt+Q"), W // 2, top)[3] >= 230
    assert _px(overlay.frame(W, H), W // 2, top)[3] < 230


def test_monitor_defaults_to_primary():
    u = ctypes.windll.user32
    primary = (0, 0, u.GetSystemMetrics(0), u.GetSystemMetrics(1))
    assert overlay.monitor(0)[0] == primary
    assert overlay.monitor(0xDEAD)[0] == primary                     # closed target -> primary


def test_cursor_is_white_arrow_with_orange_gradient_border():
    w, h, buf, (hx, hy) = overlay.cursor_frame()
    px = lambda x, y: tuple(buf[(y * w + x) * 4:(y * w + x) * 4 + 4])
    b, g, r, a = px(hx + 1, hy + 3)                        # just inside the tip: orange border
    assert a > 200 and r > g > b
    assert px(hx + 7, hy + 14)[:3] == (255, 255, 255)     # body is white
    assert px(w - 1, 0) == (0, 0, 0, 0)                   # outside the arrow: transparent
