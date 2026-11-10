"""The Hydrogen line tab's drawn plots: one scan, and the last 24 h of scans."""
import time

import matplotlib

matplotlib.use("Agg")
import numpy as np
from matplotlib.figure import Figure

from aquila import lineview
from aquila.map3d import ROW, V_MID


def _rows(n, end, step=300.0, gap=()):
    rows = np.zeros(n, ROW)
    rows["t"] = end - step * np.arange(n)[::-1]
    rows["spec"] = 0.4 * np.exp(-0.5 * ((V_MID - 8.0) / 12.0) ** 2)
    return rows[[i for i in range(n) if i not in gap]]


def test_row_at_takes_the_scan_written_with_the_csv_line():
    rows = _rows(10, 1_800_000_000.0)
    assert lineview.row_at(rows, 1_800_000_000.0 - 300 + 4)["t"] == rows["t"][-2]
    assert lineview.row_at(rows, 1_800_000_000.0 + lineview.MATCH_S + 1) is None
    assert lineview.row_at(rows, None) is None
    assert lineview.row_at(None, 1.0) is None
    assert lineview.row_at(rows[:0], 1.0) is None


def test_draw_line_draws_the_row_and_refuses_an_empty_one():
    fig = Figure()
    spec = _rows(1, 0.0)["spec"][0].copy()
    spec[:5] = np.nan                                   # velocities the scan did not cover
    assert lineview.draw_line(fig, spec, 0.01, 8.0, 0.4, ("node", "scan x"))
    ax = fig.axes[0]
    x, y = ax.lines[1].get_data()                       # after the zero line
    assert len(x) == V_MID.size - 5 and np.isfinite(y).all()
    assert ax.get_xlim()[0] > ax.get_xlim()[1]          # receding on the left, as the node draws it
    assert ax.get_title() == "node   scan\u00a0x"         # breaks between parts only
    fig.canvas.draw()
    assert not lineview.draw_line(Figure(), np.full(V_MID.size, np.nan), 0.01, 0.0, 0.0)


def test_day_grid_keeps_a_gap_a_gap():
    end = 1_800_000_000.0
    rows = _rows(288, end, gap=range(100, 160))         # 5 h without a scan
    te, v, z = lineview.day_grid(rows, end)
    assert te.size == lineview.DAY_BINS + 1 and te[-1] == end and te[0] == end - 86400.0
    empty = ~np.isfinite(z).any(axis=1)
    assert 18 <= empty.sum() <= 21                      # the 5 h, in 15 minute columns
    assert np.abs(v).max() <= 150 and z.shape == (lineview.DAY_BINS, v.size)
    assert abs(float(np.nanmax(z)) - 0.4) < 0.01


def test_day_grid_ends_now_so_a_stopped_node_shows_its_silence():
    now = time.time()
    te, _, z = lineview.day_grid(_rows(100, now - 6 * 3600.0))
    assert abs(te[-1] - now) < 5
    assert not np.isfinite(z[-20:]).any()               # the last 5 h are empty
    assert lineview.day_grid(_rows(100, now - 2 * 86400.0)) is None      # nothing in the 24 h
    assert lineview.day_grid(_rows(2, now)) is None                      # too few to draw
    assert lineview.day_grid(None) is None


def test_day_grid_shows_a_node_whose_clock_is_ahead():
    now = time.time()
    te, _, z = lineview.day_grid(_rows(50, now + 120.0))
    assert te[-1] == now + 120.0 and np.isfinite(z[-1]).any()


def test_draw_day_puts_whole_node_hours_on_the_time_axis():
    end = 1_800_000_000.0
    rows = _rows(288, end)
    fig = Figure(figsize=(7, 3))
    assert lineview.draw_day(fig, rows, tz_s=19800, mark=end - 3600.0, end=end)
    ax = fig.axes[0]
    fig.canvas.draw()
    lo, hi = ax.get_xlim()
    assert abs((hi - lo) - 24.0) < 1e-6
    ticks = [t for t in ax.get_xticks() if lo <= t <= hi]
    assert len(ticks) >= 3 and all(float(t).is_integer() and int(t) % 3 == 0 for t in ticks)
    # the right edge is the newest scan, on the node's clock (UTC + 5:30)
    want = time.gmtime(end + 19800)
    assert abs((hi % 24) - (want.tm_hour + want.tm_min / 60 + want.tm_sec / 3600)) < 1e-6
    assert len(ax.lines) == 1                           # the scan on show is pointed out
    assert ax.get_xlabel() == "Node time"
    assert not lineview.draw_day(Figure(), rows[:2], end=end)


def test_draw_day_without_a_node_clock_says_so():
    end = 1_800_000_000.0
    fig = Figure()
    assert lineview.draw_day(fig, _rows(50, end), end=end)
    assert fig.axes[0].get_xlabel() == "Time" and not fig.axes[0].lines


def _texts(fig):
    out = []
    for ax in fig.axes:
        for axis in (ax.xaxis, ax.yaxis):
            for tk in axis._update_ticks():
                out += [tk.label1, tk.label2]
            out.append(axis.label)
        out.append(ax.title)
        if ax.get_legend() is not None:
            out += list(ax.get_legend().get_texts())
    return [t for t in out if t.get_visible() and t.get_text().strip()]


def _all_text_inside(fig):
    fig.canvas.draw()
    r = fig.canvas.get_renderer()
    for t in _texts(fig):
        b = t.get_window_extent(r)
        assert b.x0 >= -0.5 and b.y0 >= -0.5, t.get_text()
        assert b.x1 <= fig.bbox.x1 + 0.5 and b.y1 <= fig.bbox.y1 + 0.5, t.get_text()


def test_no_text_is_cut_in_the_smallest_boxes():
    """The sizes the plots have in the smallest window the tab allows."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    end = 1_800_000_000.0
    rows = _rows(288, end)
    line = Figure(figsize=(6.2, 2.3), dpi=100, layout="constrained")
    FigureCanvasAgg(line)
    title = ("a node with a long name", "scan 2026-11-10 21:16:34",
             "noise 0.0100 dB rms per channel")
    assert lineview.draw_line(line, rows["spec"][0], 0.01, -148.0, 0.404, title)
    _all_text_inside(line)
    day = Figure(figsize=(2.9, 1.77), dpi=100, layout="constrained")
    FigureCanvasAgg(day)
    assert lineview.draw_day(day, rows, tz_s=19800, mark=end - 3600, end=end)
    _all_text_inside(day)
    ticks = [t.label1.get_window_extent() for t in day.axes[0].xaxis._update_ticks()]
    assert all(a.x1 < b.x0 for a, b in zip(ticks, ticks[1:], strict=False))   # hours apart
