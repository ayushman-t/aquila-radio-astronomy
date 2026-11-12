"""The Hydrogen line tab's drawn plots: the calibrated line, and the last 24 h of it as
velocity against time. Both draw on a figure handed in, at whatever size it has, so the
app can fill a box with them instead of fitting a picture into it."""
import time
import warnings

import numpy as np

from .map3d import MIN_ROWS, V_MID, V_SHOW, bridge

DAY_S = 86400.0
DAY_BINS = 96               # 15 minutes each
MATCH_S = 90.0              # a scan's row is written within seconds of its line in scans.csv
BIN_KMS = float(V_MID[1] - V_MID[0])
# the app's plots keep their own text sizes: virgo, where it has drawn in the same
# process, leaves matplotlib's set for its large pictures
TEXT = {"font.size": 10.0, "axes.labelsize": 10.0, "axes.titlesize": 10.0,
        "legend.fontsize": 9.0, "xtick.labelsize": 10.0, "ytick.labelsize": 10.0}


def line_axes(ax, v, s, rms, kms, db, title="", label="calibrated spectrum"):
    """The calibrated spectrum in dB above its baseline, against velocity."""
    ax.axhspan(-rms, rms, color="0.85", lw=0, label="baseline noise (1 rms)")
    ax.axhline(0.0, color="0.45", lw=0.8)
    ax.plot(v, s, color="#1f5fa8", lw=1.2, label=label)
    ax.plot([kms], [db], "o", color="#c0392b", ms=6,
            label=f"peak {db:.3f} dB at {kms:.0f} km/s")
    ax.set_xlim(v.max(), v.min())            # approaching gas on the right, as frequency rises
    ax.set_xlabel("Velocity (km/s, positive = receding)")
    ax.set_ylabel("Power above baseline (dB)")
    if title:
        ax.set_title(title, fontsize=10)
    ax.grid(alpha=0.3)
    ax.legend(loc="upper left", fontsize=9, frameon=False)


def row_at(rows, t):
    """The history row of the scan that ended at t, or None when it is not held."""
    if rows is None or t is None or not rows.size:
        return None
    i = int(np.argmin(np.abs(rows["t"] - t)))
    return rows[i] if abs(float(rows["t"][i]) - t) <= MATCH_S else None


def draw_line(fig, spec, rms, kms, db, title=()):
    """One scan from its history row. title: its parts; a box too narrow for all of them
    on one line breaks it between two parts. False when the row holds too little to draw."""
    spec = np.asarray(spec, float)
    ok = np.isfinite(spec)
    if ok.sum() < 4:
        return False
    import matplotlib
    fig.clear()
    with matplotlib.rc_context(TEXT):
        ax = fig.add_subplot(111)
        line_axes(ax, V_MID[ok], spec[ok], rms, kms, db,
                  "   ".join(str(p).replace(" ", "\u00a0") for p in title),
                  f"calibrated spectrum, {BIN_KMS:g} km/s bins")
    ax.title.set_wrap(True)
    ax.tick_params(labelsize=TEXT["font.size"])        # ticks are made when first drawn
    return True


def day_grid(rows, end=None):
    """The 24 h up to `end` (now when not given) as (time edges, v, z[time, v]): the median
    of the scans in each 15 minutes, so a gap in observing stays a gap. None when there
    are too few scans in it."""
    if rows is None or not rows.size:
        return None
    now = time.time() if end is None else float(end)
    # a node whose clock runs a little ahead still shows its newest scan
    t = rows["t"]
    ahead = t[(t > now) & (t < now + 3600)]
    if ahead.size:
        now = float(ahead.max())
    rows = rows[(t > now - DAY_S) & (t <= now)]
    if rows.size < MIN_ROWS:
        return None
    step = DAY_S / DAY_BINS
    t0 = now - DAY_S
    idx = np.clip(((rows["t"] - t0) / step).astype(int), 0, DAY_BINS - 1)
    keep = np.abs(V_MID) <= V_SHOW
    z = np.full((DAY_BINS, int(keep.sum())), np.nan, np.float32)
    for i in np.unique(idx):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)     # a velocity with no data
            z[i] = np.nanmedian(rows["spec"][idx == i][:, keep], axis=0)
    bridge(z, t0, step, rows, keep)
    if not np.isfinite(z).any():
        return None
    return t0 + step * np.arange(DAY_BINS + 1), V_MID[keep], z


def draw_day(fig, rows, tz_s=None, mark=None, end=None):
    """Velocity against time for the last 24 h. tz_s: the node's clock, seconds east of
    UTC (this computer's when not known). mark: a time to point out, the scan on show.
    False when there is not enough to draw."""
    g = day_grid(rows, end)
    if g is None:
        return False
    te, v, z = g
    finite = z[np.isfinite(z)]
    lo, hi = float(np.percentile(finite, 1)), float(np.percentile(finite, 99.9))
    lo = min(lo, 0.0)
    if hi <= lo:
        hi = lo + 1e-3
    own = tz_s is None
    if own:
        tz_s = time.localtime(te[-1]).tm_gmtoff
    import matplotlib
    from matplotlib.ticker import FuncFormatter, Locator

    class Hours(Locator):
        """Whole hours of the clock, as many as the axis has room for."""

        def __call__(self):
            return self.tick_values(*self.axis.get_view_interval())

        def tick_values(self, vmin, vmax):
            room = max(2.0, 0.75 * self.axis.get_tick_space())     # 'hh:mm' is a wide label
            step = next((h for h in (3, 6, 12) if (vmax - vmin) / h <= room), 24)
            return np.arange(np.ceil(vmin / step) * step, vmax + 1e-9, step)

    fig.clear()
    with matplotlib.rc_context(TEXT):
        ax = fig.add_subplot(111)
    half = BIN_KMS / 2
    # time runs in hours on the node's clock, so the ticks fall on its whole hours and
    # thin out by themselves when the box is narrow
    x = (te + tz_s) / 3600.0
    x -= 24.0 * (x[0] // 24.0)              # from the node's midnight: small whole numbers
    m = ax.pcolormesh(x, np.append(v - half, v[-1] + half), np.ma.masked_invalid(z.T),
                      cmap="viridis", vmin=lo, vmax=hi, shading="flat")
    if mark is not None and te[0] <= mark <= te[-1]:
        ax.axvline(x[0] + (mark - te[0]) / 3600.0, color="white", lw=1.0, ls="--")
    ax.set_xlim(x[0], x[-1])
    ax.xaxis.set_major_locator(Hours())
    ax.xaxis.set_major_formatter(FuncFormatter(lambda h, _: f"{int(round(h)) % 24:02d}:00"))
    with matplotlib.rc_context(TEXT):
        ax.set_xlabel("Time" if own else "Node time")
        ax.set_ylabel("Velocity (km/s)")
        bar = fig.colorbar(m, ax=ax, pad=0.01, aspect=18, label="dB above baseline")
    for a in (ax, bar.ax):
        a.tick_params(labelsize=TEXT["font.size"])     # ticks are made when first drawn
    return True
