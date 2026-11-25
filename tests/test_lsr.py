"""The LSR velocity correction and the VLSR 3D tab."""
import os
import time
import types

import numpy as np
import pytest

from aquila import lsr, map3d, skymap

# (ra, dec, unix time, lat, lon, km/s) from astropy: the barycentric correction plus its
# LSRK solar motion along the line of sight
ASTROPY = ((83.0, 22.0, 1764000000, 40.0, -75.0, -1.722),
           (300.0, 40.0, 1750000000, -30.0, 20.0, 30.428),
           (10.0, -60.0, 1790000000, 52.0, 0.0, -17.602),
           (190.0, 5.0, 1772000000, 10.0, 100.0, 19.020))


@pytest.mark.parametrize("ra,dec,t,lat,lon,want", ASTROPY)
def test_correction_agrees_with_astropy(ra, dec, t, lat, lon, want):
    assert abs(float(lsr.correction(ra, dec, t, lat, lon)) - want) < 0.05


def test_correction_takes_arrays():
    a = np.array(ASTROPY)
    got = lsr.correction(a[:, 0], a[:, 1], a[:, 2], 40.0, -75.0)
    assert got.shape == (4,)
    assert abs(got[0] - ASTROPY[0][5]) < 0.05


def test_earth_moves_at_orbital_speed():
    v = lsr.earth_velocity(np.linspace(1.75e9, 1.75e9 + 365.25 * 86400, 50))
    speed = np.sqrt((v ** 2).sum(0))
    assert 29.2 < speed.min() < speed.max() < 30.4


def test_no_site_no_correction():
    t = np.array([1.76e9, 1.76e9 + 900])
    assert lsr.of_dish({"dish": {"az": 180, "el": 60}, "site": {}}, t) is None
    assert lsr.of_dish({"dish": {"az": 180, "el": 60}, "site": {"lat": "x", "lon": 1}}, t) is None
    assert lsr.of_dish({"dish": {}, "site": {"lat": 10, "lon": 20}}, t) is None
    got = lsr.of_dish({"dish": {"az": 180, "el": 60}, "site": {"lat": 10, "lon": 20}}, t)
    assert got.shape == (2,) and np.all(np.abs(got) < 55)


def test_of_dish_follows_the_sky_the_dish_sees():
    view = {"dish": {"az": 255.0, "el": 73.0}, "site": {"lat": 24.0, "lon": 81.0}}
    t = 1.7638e9
    ra, dec = skymap.altaz_to_radec(255.0, 73.0, 24.0, 81.0, t)
    want = float(lsr.correction(ra, dec, t, 24.0, 81.0))
    assert abs(float(lsr.of_dish(view, t)[0]) - want) < 1e-9


def _history(n=40, shift=0.0, t0=1.7638e9):
    rows = np.zeros(n, map3d.ROW)
    v = map3d.V_MID
    rows["t"] = t0 + 900.0 * np.arange(n)
    for i in range(n):
        rows["spec"][i] = 0.5 * np.exp(-0.5 * ((v - 10.0 - shift) / 8.0) ** 2)
    return rows


def test_shifted_moves_each_row_by_its_own_correction():
    rows = _history(3)
    corr = np.array([0.0, 20.0, -30.0])
    out = lsr.shifted(rows, corr)
    v = map3d.V_MID
    peak = v[np.nanargmax(out["spec"], axis=1)]
    assert np.allclose(peak, 10.0 + corr, atol=1.0)
    assert np.array_equal(out["t"], rows["t"])
    assert np.isnan(out["spec"][1][:5]).all()            # nothing was measured out there
    assert np.allclose(rows["spec"][0], out["spec"][0])  # the rows given are left alone


def test_plot_says_lsr():
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    rows = _history()
    for flag, word in ((False, "Velocity (km/s)"), (True, "LSR velocity (km/s)")):
        fig = Figure(figsize=(12, 6))
        FigureCanvasAgg(fig)
        ax = map3d.draw(fig, rows, "x", now=float(rows["t"].max()), lsr=flag)
        assert ax.get_ylabel() == word
        assert fig.axes[1].get_ylabel().startswith(word[:-7])


def test_vlsr_tab(tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from aquila import gui
    app = QApplication.instance() or QApplication([])
    pane = gui.Map3DPane(gui.Main.MAP_EMPTY, lsr=True)
    pane.resize(1100, 600)
    pane.show()
    me = types.SimpleNamespace(lsr_pane=pane, LSR_NO_SITE=gui.Main.LSR_NO_SITE,
                               node=lambda: types.SimpleNamespace(name="dish"))
    rows = _history(t0=time.time() - 40 * 900)
    gui.Main._refresh_lsr(me, rows, {"dish": {"az": 180, "el": 60}, "site": {}})
    assert pane.text() == gui.Main.LSR_NO_SITE and pane.ax is None
    view = {"dish": {"az": 180.0, "el": 60.0}, "site": {"lat": 10.0, "lon": 20.0}}
    gui.Main._refresh_lsr(me, rows, view)
    app.processEvents()
    assert pane.ax is not None and pane.ax.get_ylabel() == "LSR velocity (km/s)"
    corr = lsr.of_dish(view, rows["t"])
    v = map3d.V_MID
    assert abs(v[np.nanargmax(pane._rows["spec"][0])] - (10.0 + corr[0])) <= 1.0
    drawn = pane._drawn
    gui.Main._refresh_lsr(me, rows, view)                # nothing new: not drawn again
    assert pane._drawn is drawn
    gui.Main._refresh_lsr(me, rows, dict(view, dish={"az": 90.0, "el": 60.0}))
    assert pane._key != drawn                            # the dish moved: new velocities
    gui.Main._refresh_lsr(me, None, view)
    assert pane._rows is None
    pane.close()
