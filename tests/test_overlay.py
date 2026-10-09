import ctypes, math, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np
import pytest

from winhands import overlay

W, H = 400, 300
A, B = overlay.PALETTES["claude"]


def _arr(buf, w=W, h=H):
    return np.frombuffer(buf, np.uint8).reshape(h, w, 4)  # premultiplied B, G, R, A


def _px(buf, x, y, w=W):
    i = (y * w + x) * 4
    return tuple(buf[i:i + 4])


def _bloom(d):
    """Spec alpha at logical distance d from the edge: A term + two B terms."""
    return min(1.0, 0.42 * math.exp(-d / 3.5) + 0.22 * math.exp(-d / 12) + 0.10 * math.exp(-d / 30))


def _straight_rgb(px):
    b, g, r, a = px
    return tuple(c * 255 / a for c in (r, g, b))


def _close(c1, c2, tol=14):
    return all(abs(x - y) <= tol for x, y in zip(c1, c2))


def _run(mask):
    """(first, last) index of the True run in a 1-D bool array."""
    idx = np.flatnonzero(mask)
    return idx[0], idx[-1]


# --- edge glow ---

def test_frame_length_is_w_h_4():
    assert len(overlay.frame(W, H)) == W * H * 4
    assert len(overlay.frame(W, H, scale=2.0)) == W * H * 4


def test_edge_line_is_two_px_with_a_to_b_gradient():
    buf = overlay.frame(W, H)
    assert _px(buf, 0, H // 2)[3] >= 0.94 * 255 and _px(buf, 1, H // 2)[3] >= 0.94 * 255
    assert _px(buf, 2, H // 2)[3] < 0.75 * 255                                  # line ends at 2px
    assert _close(_straight_rgb(_px(buf, 0, 0)), A)                             # A at top-left
    assert _close(_straight_rgb(_px(buf, W - 1, H - 1)), B)                     # B at bottom-right


def test_line_scales_with_dpi():
    buf = overlay.frame(2 * W, 2 * H, scale=2.0)
    px = lambda x, y: _px(buf, x, y, 2 * W)
    assert px(3, H)[3] >= 0.94 * 255 and px(5, H)[3] < 0.75 * 255               # 4px line at 200%


def test_bloom_alpha_follows_the_distance_formula_and_is_clipped():
    buf = overlay.frame(W, H)
    for d in (4, 10, 30, 60):
        assert abs(_px(buf, d, H // 2)[3] - 255 * _bloom(d + 0.5)) <= 3
    alphas = [_px(buf, x, H // 2)[3] for x in range(2, 96)]
    assert alphas == sorted(alphas, reverse=True)
    assert all(_px(buf, x, H // 2) == (0, 0, 0, 0) for x in (96, 100, W // 2))  # clipped at 96px


@pytest.mark.parametrize("state,k", [("acting", 1.0), ("thinking", 0.7), ("paused", 0.4)])
def test_state_scales_the_glow(state, k):
    assert abs(_px(overlay.frame(W, H, state=state), 10, H // 2)[3] - k * 255 * _bloom(10.5)) <= 3


@pytest.mark.parametrize("state,k", [("thinking", 0.7), ("paused", 0.4)])
def test_state_scales_the_edge_line_too(state, k):
    line = lambda s: _px(overlay.frame(W, H, state=s), 0, H // 2)[3]
    la = .95 * k
    assert abs(line(state) - 255 * (la + k * _bloom(.5) * (1 - la))) <= 3
    assert line(state) < line("acting") - 20


def test_stopped_has_no_glow():
    assert set(overlay.frame(W, H, state="stopped")) == {0}


def test_frame_is_premultiplied_in_every_state():
    for state in overlay.STATES:
        a = _arr(overlay.frame(W, H, text="is controlling this PC", state=state))
        assert (a[..., :3].max(-1) <= a[..., 3]).all()


# --- banner ---

def _pill_rows(buf, w, h):
    col = _arr(buf, w, h)[:, w // 2, 3]
    first, last = _run(col[6:h // 2] >= 230)              # skip the 2px edge lines at the top and bottom
    return first + 6, last + 6


def _pill_cols(buf, w, h, y):
    return _run(_arr(buf, w, h)[y, :, 3] >= 230)


def test_banner_is_36px_tall_centred_12px_below_the_top():
    w, h = 800, 300
    buf = overlay.frame(w, h, text="is controlling this PC")
    top, bottom = _pill_rows(buf, w, h)
    assert abs(top - 12) <= 1 and abs((bottom - top + 1) - 36) <= 2
    left, right = _pill_cols(buf, w, h, 12 + 18)
    assert abs((left + right) / 2 - w / 2) <= 2


def test_banner_scales_with_dpi():
    w, h = 1600, 600
    top, bottom = _pill_rows(overlay.frame(w, h, scale=2.0, text="is controlling this PC"), w, h)
    assert abs(top - 24) <= 2 and abs((bottom - top + 1) - 72) <= 3


def test_no_text_means_no_banner():
    assert _arr(overlay.frame(800, H), 800, H)[12:48, 400, 3].max() < 230


def _pill_width(state, w=1000, h=200):
    buf = overlay.frame(w, h, text="is paused", state=state)
    left, right = _pill_cols(buf, w, h, 30)
    return right - left + 1


def test_shortcut_group_is_present_unless_stopped():
    stopped = _pill_width("stopped")
    for state in ("acting", "thinking", "paused"):
        assert _pill_width(state) - stopped >= 60          # Stop label + Ctrl/Alt/Q chips + divider


def test_pill_grows_with_the_status_text():
    buf = overlay.frame(1000, 200, text="is controlling this PC and a lot more words", state="stopped")
    left, right = _pill_cols(buf, 1000, 200, 30)
    assert right - left + 1 > _pill_width("stopped") + 100


def test_status_texts_cover_every_state():
    assert set(overlay.STATUS) == set(overlay.STATES) == {"acting", "thinking", "paused", "stopped"}
    assert overlay.STATES == {"acting": 1.0, "thinking": 0.7, "paused": 0.4, "stopped": 0.0}


# --- palettes / providers ---

def test_palette_stops():
    assert overlay.PALETTES["claude"] == ((255, 140, 60), (217, 119, 87))
    assert overlay.PALETTES["antigravity"] == ((96, 165, 250), (37, 99, 235))
    assert overlay.PALETTES["codex"] == ((205, 205, 212), (112, 112, 122))


def test_frame_palette_blue_and_gray():
    b, g, r, a = _px(overlay.frame(W, H, palette=overlay.PALETTES["antigravity"]), 0, H // 2)
    assert a >= 0.94 * 255 and b > g > r                                        # blue edge
    b, g, r, a = _px(overlay.frame(W, H, palette=overlay.PALETTES["codex"]), 0, H // 2)
    assert a >= 0.94 * 255 and max(b, g, r) - min(b, g, r) <= 12                # gray edge


def test_monitor_defaults_to_primary():
    u = ctypes.windll.user32
    primary = (0, 0, u.GetSystemMetrics(0), u.GetSystemMetrics(1))
    assert overlay.monitor(0)[0] == primary
    assert overlay.monitor(0xDEAD)[0] == primary                     # closed target -> primary


def test_provider_for_maps_client_names(monkeypatch):
    monkeypatch.delenv("WINHANDS_PROVIDER", raising=False)
    assert overlay.provider_for("claude-code") == "claude"
    assert overlay.provider_for("Antigravity") == "antigravity"
    assert overlay.provider_for("agy-cli") == "antigravity"
    assert overlay.provider_for("codex-mcp-client") == "codex"
    assert overlay.provider_for("openai-chatgpt") == "codex"
    assert overlay.provider_for(None) == "claude" and overlay.provider_for("whatever") == "claude"


def test_provider_env_override(monkeypatch):
    monkeypatch.setenv("WINHANDS_PROVIDER", "codex")
    assert overlay.provider_for("claude-code") == "codex"


# --- cursor ---

def _cpx(res, x, y):
    w, h, buf, _ = res
    return tuple(buf[(y * w + x) * 4:(y * w + x) * 4 + 4])


def test_cursor_bitmap_is_64px_with_hotspot_at_32_26():
    w, h, buf, hot = overlay.cursor_frame()
    assert (w, h, hot, len(buf)) == (64, 64, (32, 26), 64 * 64 * 4)
    w, h, buf, hot = overlay.cursor_frame(scale=2.0)
    assert (w, h, hot, len(buf)) == (128, 128, (64, 52), 128 * 128 * 4)


def test_cursor_arrow_is_opaque_near_the_tip_and_corners_are_clear():
    res = overlay.cursor_frame()
    assert _cpx(res, 36, 33)[3] == 255                    # inside the arrow, just below the tip
    assert _cpx(res, 0, 0) == (0, 0, 0, 0)                # far outside the halo
    assert _cpx(res, 63, 63) == (0, 0, 0, 0)


def test_cursor_halo_fades_out_from_the_hotspot():
    res = overlay.cursor_frame()
    near, far = _cpx(res, 20, 26)[3], _cpx(res, 14, 26)[3]   # r12 vs r18 on the clear side of the arrow
    assert 0 < far < near < 80
    assert _cpx(res, 8, 26)[3] == 0                       # r24 > 20


def test_cursor_fill_follows_the_palette():
    b, g, r, a = _cpx(overlay.cursor_frame(), 36, 33)
    assert r > g > b
    b, g, r, a = _cpx(overlay.cursor_frame(palette=overlay.PALETTES["antigravity"]), 36, 33)
    assert b > g > r


def test_cursor_moving_draws_a_trail_behind_the_motion():
    idle, moving = overlay.cursor_frame(), overlay.cursor_frame(motion=(1, 0))
    assert _cpx(moving, 17, 26)[3] > _cpx(idle, 17, 26)[3] + 20      # tail 15px behind the hotspot
    assert _cpx(moving, 47, 24)[3] <= _cpx(idle, 47, 24)[3] + 5      # nothing ahead of it


def test_cursor_click_draws_a_ripple_ring():
    idle, click = overlay.cursor_frame(), overlay.cursor_frame(click=0.05)   # 90ms into the 1.8s cycle
    ring = max(_cpx(click, x, 26)[3] for x in range(44, 53))          # r12..20: the ring, clear of flash and arrow
    assert ring >= 100 and ring > max(_cpx(idle, x, 26)[3] for x in range(44, 53)) + 60


def test_cursor_click_ripple_is_over_after_25_percent_of_the_cycle():
    assert overlay.cursor_frame(click=0.5) == overlay.cursor_frame()       # halo r20 only, arrow back to 1.0


def test_cursor_click_presses_the_arrow_to_90_percent_at_54ms():
    tip = lambda **kw: _cpx(overlay.cursor_frame(**kw), 46, 34)[3]         # the right wing tip of the 1.0 arrow
    assert tip() == 255 and tip(click=0.03) < 255


def test_cursor_is_premultiplied():
    for kw in ({}, {"motion": (1, 1)}, {"click": 0.3}):
        w, h, buf, _ = overlay.cursor_frame(**kw)
        a = _arr(buf, w, h)
        assert (a[..., :3].max(-1) <= a[..., 3]).all()
