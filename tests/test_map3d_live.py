"""The rotatable 3D plot: the node sends only new rows, the app merges them, and the
surface is laid again for any view without failing."""
import os
import time

import numpy as np
import pytest

from aquila import api, map3d


def _rows(hdir, t0, n, step=900.0):
    v = np.linspace(-200, 200, 400)
    for i in range(n):
        db = 0.5 * np.exp(-0.5 * ((v + 20 + 5 * np.sin(i / 10)) / 8) ** 2)
        map3d.append(hdir, t0 + i * step, v, db)


def test_node_sends_only_newer_rows_and_app_merges(tmp_path):
    pytest.importorskip("PySide6")
    from aquila.gui import Node
    hdir = tmp_path / "hline"
    hdir.mkdir()
    now = time.time()
    _rows(str(hdir), now - 6 * 3600, 20)
    srv = api.serve(str(tmp_path), port=0)
    try:
        n = Node("x", f"http://127.0.0.1:{srv.server_address[1]}")
        first = n.history()
        assert first is not None and first.size == 20
        _rows(str(hdir), now - 30, 2, step=10.0)
        n._hist_at = 0                       # skip the once a minute wait
        both = n.history()
        assert both.size == 22
        assert np.all(np.diff(both["t"]) > 0)
        n._hist_at = 0
        assert n.history().size == 22        # nothing new: nothing added twice
    finally:
        srv.shutdown()


def test_bad_after_is_refused(tmp_path):
    import urllib.error
    import urllib.request
    os.makedirs(tmp_path / "hline")
    _rows(str(tmp_path / "hline"), time.time() - 3600, 3)
    srv = api.serve(str(tmp_path), port=0)
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(
                f"http://127.0.0.1:{srv.server_address[1]}/hline/history.dat?after=x")
        assert e.value.code == 400
    finally:
        srv.shutdown()


@pytest.mark.parametrize("azim", [-60, 30, 120, -150])
def test_surface_lays_for_any_view(tmp_path, azim):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    _rows(str(tmp_path), time.time() - 86400, 96)
    fig = Figure()
    FigureCanvasAgg(fig)
    ax = map3d.draw(fig, map3d.load(str(tmp_path)), "x", light=True)
    assert ax is not None
    ax.view_init(elev=30, azim=azim)
    map3d.lay(ax)
    z = sorted(c.get_zorder() for c in ax._aquila_strips)
    assert z == list(range(len(z)))
    fig.savefig(tmp_path / "v.png")


def _view(tmp_path, size):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    _rows(str(tmp_path), time.time() - 86400, 24)
    fig, fig2 = Figure(figsize=size, dpi=100), Figure()
    FigureCanvasAgg(fig)
    FigureCanvasAgg(fig2)
    ax = map3d.draw(fig, map3d.load(str(tmp_path)), "x", light=True, fig2=fig2)
    assert ax is not None
    return fig, ax


def _cut(fig, ax):
    """Pixels of the view's box, numbers and labels that fall outside the figure."""
    fig.draw_without_rendering()
    bb, f = map3d._extent(ax, fig.canvas.get_renderer()), fig.bbox
    return max(f.x0 - bb.x0, bb.x1 - f.x1, f.y0 - bb.y0, bb.y1 - f.y1, 0.0)


@pytest.mark.parametrize("size", [(6.96, 6.15), (6.96, 7.24), (9.36, 9.04), (6.82, 4.04)])
def test_3d_numbers_and_labels_stay_inside_the_box(tmp_path, size):
    fig, ax = _view(tmp_path, size)
    for azim in range(-180, 180, 45):
        for elev in (10, 32, 75):
            ax.view_init(elev=elev, azim=azim)
            z = map3d.fit(ax, 1.15)
            assert 0.3 < z <= 1.15
            assert _cut(fig, ax) == 0.0, (azim, elev)


def test_3d_view_in_a_narrow_box_was_cut_and_a_wide_one_is_left_alone(tmp_path):
    fig, ax = _view(tmp_path, (6.96, 6.15))             # the 3D box of the default window
    ax.set_box_aspect(map3d.BOX, zoom=1.15)
    assert _cut(fig, ax) > 20
    assert map3d.fit(ax, 1.15) < 1.15 and _cut(fig, ax) == 0.0
    (tmp_path / "wide").mkdir()
    fig, ax = _view(tmp_path / "wide", (10.88, 8.04))
    ax.set_box_aspect(map3d.BOX, zoom=1.15)
    assert _cut(fig, ax) == 0.0
    before = map3d._extent(ax, fig.canvas.get_renderer()).extents
    assert map3d.fit(ax, 1.15) == 1.15
    fig.draw_without_rendering()
    assert np.allclose(map3d._extent(ax, fig.canvas.get_renderer()).extents, before)
