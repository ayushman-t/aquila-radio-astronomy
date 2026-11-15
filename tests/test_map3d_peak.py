"""The multi-day plot must show a day's peak as high as it was measured.

A bright region passes a 2.4 m dish in about half an hour, and comes four minutes
earlier each day. Averaged over time bins wider than that, its height on the plot fell
from 1.6 to 1.3 dB in a week while every scan measured 2.0 dB."""
import numpy as np
import pytest
from matplotlib.dates import num2date
from matplotlib.figure import Figure

from aquila import map3d

SIDEREAL = 86164.0905
T0 = 1_794_000_000.0


def _week(peak_min=14.0, every=796.0, days=7.0):
    """Scans of a line that is 2 dB high for a short time once a sidereal day."""
    t = T0 + np.arange(0.0, days * 86400.0, every)
    phase = (t - T0) % SIDEREAL - 0.4 * SIDEREAL
    height = 0.2 + 1.8 * np.exp(-0.5 * (phase / (peak_min * 60.0)) ** 2)
    rows = np.zeros(t.size, map3d.ROW)
    rows["t"] = t
    rows["spec"] = height[:, None] * np.exp(-0.5 * ((map3d.V_MID + 4.0) / 6.0) ** 2)[None, :]
    return rows


def _shown(rows, light):
    """Highest point of the drawn surface in each sidereal day, and the scale top."""
    ax = map3d.draw(Figure(), rows, light=light, fig2=Figure(), now=float(rows["t"].max()))
    X, _, Z, _ = ax._aquila_surface
    ts = np.array([num2date(x).timestamp() for x in X[:, 0]])
    day = ((ts - T0) // SIDEREAL).astype(int)
    return [float(Z[day == d].max()) for d in range(7)], ax.get_zlim()[1]


@pytest.mark.parametrize("light", [True, False])
def test_each_day_shows_the_peak_its_scans_measured(light):
    rows = _week()
    day = ((rows["t"] - T0) // SIDEREAL).astype(int)
    raw = [float(rows["spec"][day == d].max()) for d in range(7)]
    shown, top = _shown(rows, light)
    assert min(raw) > 1.75                               # the scans themselves are steady
    for r, s in zip(raw, shown, strict=True):
        assert abs(s - r) < 0.03 * r, (raw, shown)       # and so is the plot, day by day
    assert top >= max(raw) - 1e-6                        # the scale does not cut the peak


def test_one_bad_scan_does_not_set_the_scale():
    rows = _week()
    rows["spec"][300] += 40.0
    _, _, z, _ = map3d.grid(rows, 7, now=float(rows["t"].max()))
    lo, hi = map3d.scale(z)
    assert 1.9 < hi < 2.1


def test_thinning_keeps_the_newest_row_and_the_strongest_of_each_group():
    x = np.arange(10.0)
    z = np.zeros((10, 4), np.float32)
    z[3] = 5.0
    z[9] = 1.0
    z[0] = np.nan
    xs, zs = map3d.thin(x, z, 4)                         # groups of three, counted from the end
    assert zs.shape[0] == 4 and zs[-1].max() == 1.0 and np.nanmax(zs) == 5.0
    assert xs[-1] == pytest.approx(8.0) and np.isfinite(zs[1:]).all()
    assert map3d.thin(x, z, 10)[1] is z                  # nothing to thin


def test_time_rows_stay_15_minutes_for_a_week():
    t, _, _, _ = map3d.grid(_week(), 7, now=T0 + 7 * 86400.0)
    assert np.allclose(np.diff(t), 900.0)
