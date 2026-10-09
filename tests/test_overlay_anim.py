import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np
import pytest

from winhands import overlay

W, H = 400, 300


def _arr(buf, w=W, h=H):
    return np.frombuffer(buf, np.uint8).reshape(h, w, 4)  # premultiplied B, G, R, A


def _cpx(res, x, y):
    w, h, buf, _ = res
    return tuple(buf[(y * w + x) * 4:(y * w + x) * 4 + 4])


def _over(top, bottom, w=W, h=H):
    """Premultiplied 'over' of two BGRA buffers, like the compositor stacking two layered windows."""
    t, b = _arr(top, w, h).astype(np.float32), _arr(bottom, w, h).astype(np.float32)
    return np.rint(t + b * (1 - t[..., 3:] / 255)).astype(np.uint8)


def _fade(buf, k, w=W, h=H):
    """What SourceConstantAlpha k does to a premultiplied bitmap."""
    return np.rint(_arr(buf, w, h).astype(np.float32) * k).astype(np.uint8).tobytes()


# --- animation math (pure) ---

def test_ease_curves_hit_their_endpoints_and_are_monotonic():
    for name in overlay.CURVES:
        assert overlay.ease(0, name) == pytest.approx(0, abs=1e-6)
        assert overlay.ease(1, name) == pytest.approx(1, abs=1e-6)
        ys = [overlay.ease(i / 20, name) for i in range(21)]
        assert ys == sorted(ys)
    assert overlay.ease(.5, "ease-in-out") == pytest.approx(.5, abs=1e-3)   # symmetric
    assert overlay.ease(.1, "ping") > .3                                    # cubic-bezier(0,0,.2,1) starts fast
    assert overlay.ease(.5, "ease-out") > .5


def test_alternate_is_a_triangle_wave():
    f = lambda t: overlay.alternate(t, 3.2)
    assert (f(0), f(3.2), f(4.8), f(6.4), f(8.0)) == pytest.approx((0, 1, .5, 0, .5))


def test_edge_breathing_runs_072_to_1_over_32s_each_way():
    assert overlay.breath(0) == pytest.approx(.72) and overlay.breath(3.2) == pytest.approx(1.0)
    assert overlay.breath(6.4) == pytest.approx(.72)
    ys = [overlay.breath(i * .16) for i in range(21)]
    assert ys == sorted(ys) and ys[0] < ys[10] < ys[-1]


def test_ping_ring_grows_to_2_6x_and_fades_in_16s_and_loops():
    assert overlay.ping(0) == pytest.approx((1.0, .55))
    s, a = overlay.ping(1.599)
    assert s == pytest.approx(2.6, abs=.02) and a == pytest.approx(0, abs=.01)
    assert overlay.ping(1.6) == pytest.approx((1.0, .55)) and overlay.ping(2.4) == pytest.approx(overlay.ping(.8))
    assert overlay.ping(.4)[1] > overlay.ping(.8)[1] > overlay.ping(1.2)[1]


def test_thinking_dot_breathes_08_to_1_in_14s():
    assert overlay.dot_breath(0) == pytest.approx((.8, .45)) and overlay.dot_breath(1.4) == pytest.approx((.8, .45))
    assert overlay.dot_breath(.7) == pytest.approx((1.0, 1.0))
    assert .45 < overlay.dot_breath(.35)[1] < 1.0


def test_idle_cursor_halo_breathes_055_to_1_over_32s():
    assert overlay.halo_breath(0) == pytest.approx(.55) and overlay.halo_breath(3.2) == pytest.approx(1.0)
    assert overlay.halo_breath(6.4) == pytest.approx(.55)


def test_edge_levels_per_state_only_acting_breathes_and_never_the_line():
    assert overlay.edge_levels("acting", 0) == pytest.approx((1.0, .72))
    assert overlay.edge_levels("acting", 3.2) == pytest.approx((1.0, 1.0))
    for state, k in (("thinking", .7), ("paused", .4), ("stopped", 0.0)):
        assert overlay.edge_levels(state, 1.234) == pytest.approx((k, k))


def test_stop_sequence_fades_edge_in_320ms_holds_banner_2s_then_rises_8px_in_240ms():
    edge, alpha, rise, done = overlay.stop_phase(0)
    assert (edge, alpha, rise, done) == (pytest.approx(1.0), 1.0, 0.0, False)
    assert overlay.stop_phase(.16)[0] < .5                                  # ease-out: past half already
    assert overlay.stop_phase(.32)[0] == pytest.approx(0, abs=1e-6)
    assert overlay.stop_phase(1.99)[1:] == (1.0, 0.0, False) and overlay.stop_phase(2.0)[1:] == (1.0, 0.0, False)
    _, alpha, rise, done = overlay.stop_phase(2.12)
    assert 0 < alpha < 1 and 0 < rise < 8 and not done
    assert overlay.stop_phase(2.24)[1:] == (0.0, 8.0, True) and overlay.stop_phase(5)[3] is True


def test_click_progress_is_the_1_8s_cycle_and_ends_at_450ms():
    assert overlay.click_progress(0) == 0 and overlay.click_progress(.45 - 1e-6) == pytest.approx(.25, abs=1e-3)
    assert overlay.click_progress(.45) is None and overlay.click_progress(1.0) is None


def test_trail_is_visible_until_120ms_without_motion():
    assert overlay.moving(10.1, 10.0) and not overlay.moving(10.121, 10.0) and not overlay.moving(10.0, None)


# --- layered renderers: static bitmaps whose alpha is animated ---

@pytest.mark.parametrize("scale,state", [(1.0, "acting"), (1.5, "acting"), (1.0, "thinking"), (1.0, "paused")])
def test_line_over_bloom_equals_the_single_frame(scale, state):
    w, h = round(400 * scale), round(300 * scale)
    pal = overlay.PALETTES["antigravity"]
    k = overlay.STATES[state]
    both = _over(_fade(overlay.line_frame(w, h, scale, pal), k, w, h),
                 _fade(overlay.bloom_frame(w, h, scale, pal), k, w, h), w, h)
    ref = _arr(overlay.frame(w, h, scale, palette=pal, state=state), w, h)
    assert np.abs(both.astype(int) - ref.astype(int)).max() <= 3


def test_bloom_and_line_are_premultiplied_and_clipped():
    for fn in (overlay.bloom_frame, overlay.line_frame):
        a = _arr(fn(W, H))
        assert (a[..., :3].max(-1) <= a[..., 3]).all()
        assert (a[H // 2, 100:W - 100] == 0).all()              # nothing in the middle
    assert _arr(overlay.line_frame(W, H))[H // 2, 3, 3] == 0    # the line stops after 2px
    bloom = _arr(overlay.bloom_frame(W, H))
    assert bloom[H // 2, 0, 3] > 150 and bloom[H // 2, 96, 3] == 0


def test_small_screens_do_not_break_the_strips():
    for w, h in ((50, 40), (96, 96), (97, 30)):
        assert len(overlay.bloom_frame(w, h)) == len(overlay.line_frame(w, h)) == w * h * 4


def test_full_screen_edge_render_is_cheap():
    import time
    t0 = time.perf_counter()
    overlay.bloom_frame(1920, 1080), overlay.line_frame(1920, 1080)
    assert time.perf_counter() - t0 < 1.5       # the old single frame took ~0.3s (0.6s with the banner)


# --- banner dot layer ---

def _dot_centre(m, scale=1.0):
    return m + round(19 * scale), m + round(18 * scale)


def test_animated_banner_has_no_dot_and_dot_layer_restores_it():
    pal = overlay.PALETTES["claude"]
    static, m = overlay._banner(1.0, "is controlling this PC", "thinking", pal)
    bare, m2 = overlay._banner(1.0, "is controlling this PC", "thinking", pal, animated=True)
    cx, cy = _dot_centre(m)
    assert m == m2 and bare.size == static.size
    r, g, b, a = bare.getpixel((cx, cy))
    assert abs(r - 0x20) < 24 and a > 200                                    # plain pill fill, no orange
    layer, (ox, oy) = overlay._dot_layer(1.0, "thinking", pal, .7, m)        # dot at full scale and opacity
    comp = bare.copy()
    comp.alpha_composite(layer, (ox, oy))
    diff = max(abs(p - q) for x in range(cx - 8, cx + 9) for y in range(cy - 8, cy + 9)
               for p, q in zip(comp.getpixel((x, y)), static.getpixel((x, y))))
    assert diff <= 6


def test_paused_and_stopped_have_no_dot_layer():
    pal = overlay.PALETTES["claude"]
    assert overlay._dot_layer(1.0, "paused", pal, 0, 32) is None and overlay._dot_layer(1.0, "stopped", pal, 0, 32) is None
    bare, _ = overlay._banner(1.0, "is paused", "paused", pal, animated=True)
    plain, _ = overlay._banner(1.0, "is paused", "paused", pal)
    assert bare.tobytes() == plain.tobytes()                                 # static states always draw their own dot


def test_ping_ring_shows_mid_cycle_and_is_gone_at_the_end():
    pal = overlay.PALETTES["claude"]
    cx, cy = _dot_centre(32)
    ring = lambda t: (lambda im, o: im.getpixel((cx + 6 - o[0], cy - o[1])))(*overlay._dot_layer(1.0, "acting", pal, t, 32))[3]
    assert ring(.3) > 50 and ring(1.58) < 20          # 6..7px right of the dot centre: just past the halo edge (alpha ~11 from its rim)


def test_thinking_dot_is_dimmer_at_the_start_of_its_breath():
    pal = overlay.PALETTES["claude"]
    cx, cy = _dot_centre(32)
    centre = lambda t: (lambda im, o: im.getpixel((cx - o[0], cy - o[1])))(*overlay._dot_layer(1.0, "thinking", pal, t, 32))[3]
    assert centre(0) < 140 < centre(.7) and centre(.7) == 255


# --- cursor layers ---

def test_cursor_halo_and_arrow_layers_recompose_the_full_cursor():
    pal = overlay.PALETTES["claude"]
    halo = overlay.cursor_frame(1.0, pal, arrow=False)
    arrow = overlay.cursor_frame(1.0, pal, halo=0)
    ref = _arr(overlay.cursor_frame(1.0, pal)[2], 64, 64).astype(np.float32)
    both = _over(arrow[2], halo[2], 64, 64).astype(np.float32)
    assert np.abs(both - ref).max() <= 3
    assert _cpx(arrow, 14, 26) == (0, 0, 0, 0)                               # no halo in the arrow layer
    assert _cpx(halo, 36, 33)[3] < 120                                       # no arrow in the halo layer


def test_cursor_modes_map_to_cursor_frame_kwargs():
    assert overlay.cursor_mode_kwargs(("idle",)) == {"halo": 0}                  # arrow only: the halo is its own window
    kw = overlay.cursor_mode_kwargs(("move", 4))                                 # 4 * 22.5deg = straight down
    assert kw["motion"] == pytest.approx((0, 1), abs=1e-9)
    assert overlay.cursor_mode_kwargs(("click", 3)) == {"click": pytest.approx(3 * overlay.TICK / 1000 / 1.8)}
    for mode in (("idle",), ("move", 15), ("click", 13)):                        # each one renders
        assert len(overlay.cursor_frame(1.0, **overlay.cursor_mode_kwargs(mode))[2]) == 64 * 64 * 4


def test_cursor_halo_multiplier_scales_the_halo_only():
    full, half = overlay.cursor_frame(halo=1.0), overlay.cursor_frame(halo=.5)
    assert abs(_cpx(half, 20, 26)[3] - _cpx(full, 20, 26)[3] / 2) <= 2
    assert _cpx(half, 36, 33) == _cpx(full, 36, 33)
