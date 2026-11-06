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
