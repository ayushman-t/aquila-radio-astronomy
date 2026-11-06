"""Hydrogen line over many days: one row per scan kept in a small file, drawn as a surface
of velocity against time, in a low priority process so observing never waits for it."""
import contextlib
import glob
import os
import subprocess
import sys
import time
import warnings

import numpy as np

V_EDGES = np.arange(-301.0, 302.0, 2.0)
V_MID = 0.5 * (V_EDGES[:-1] + V_EDGES[1:])
ROW = np.dtype([("t", "<f8"), ("spec", "<f4", (V_MID.size,))])
HISTORY = "history.dat"
PNG = "map3d.png"
MIN_ROWS = 3
YOUNG_ROWS = 24
V_SHOW = 150.0


def binned(v, db):
    """A scan's spectrum on the fixed velocity grid, so scans at any rate or tuning line up."""
    ok = np.isfinite(db)
    n, _ = np.histogram(v[ok], V_EDGES)
    s, _ = np.histogram(v[ok], V_EDGES, weights=db[ok])
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n > 0, s / n, np.nan).astype(np.float32)


def append(hdir, t, v, db):
    row = np.zeros(1, ROW)
    row["t"], row["spec"] = t, binned(np.asarray(v, float), np.asarray(db, float))
    p = os.path.join(hdir, HISTORY)
    if os.path.exists(p) and os.path.getsize(p) % ROW.itemsize:
        # a crash or power cut mid write: without this every later row would be misread
        with open(p, "r+b") as f:
            f.truncate(os.path.getsize(p) // ROW.itemsize * ROW.itemsize)
    with open(p, "ab") as f:
        row.tofile(f)


def load(hdir):
    p = os.path.join(hdir, HISTORY)
    if not os.path.exists(p):
        return np.zeros(0, ROW)
    # a row still being written leaves a part row at the end: the whole rows are used
    return np.fromfile(p, ROW, count=os.path.getsize(p) // ROW.itemsize)


def trim(hdir, keep_days, now=None):
    rows = load(hdir)
    cut = (time.time() if now is None else now) - keep_days * 86400.0
    if rows.size and rows["t"].min() < cut:
        p = os.path.join(hdir, HISTORY)
        rows[rows["t"] >= cut].tofile(p + ".tmp")
        os.replace(p + ".tmp", p)


def backfill(hdir, measure, log=print):
    """A node updated today already holds frequency switched scans: they start the history."""
    done = 0
    rows = []
    for ref in sorted(glob.glob(os.path.join(hdir, "obs_*_ref.dat"))):
        dat = ref[:-8] + ".dat"
        try:
            hdr = dict(ln.strip().split("=", 1) for ln in open(dat[:-4] + ".header") if "=" in ln)
            m = measure(dat, ref, {"frequency": float(hdr["frequency"]),
                                   "bandwidth": float(hdr["bandwidth"]),
                                   "channels": int(hdr["channels"])})
            t = (float(hdr["mjd"]) - 40587.0) * 86400.0
        except (OSError, KeyError, ValueError):
            continue
        if m is not None:
            rows.append((t, m["v"], m["spec_db"]))
    for t, v, db in sorted(rows, key=lambda r: r[0]):
        append(hdir, t, v, db)
        done += 1
    if done:
        log(f"multi-day plot started from {done} stored scans")
    return done


def grid(rows, days, now=None):
    """Median of the scans in each time bin, on a regular time axis, so a gap in observing
    stays a gap and one bad scan does not stand up as a ridge."""
    # a Pi boots with its clock in 1970, and a wrong clock can write a future time: such a
    # row must not stretch the time axis
    rows = rows[(rows["t"] > 1.5e9) & (rows["t"] < (time.time() if now is None else now) + 3600)]
    if not rows.size:
        return None
    now = float(rows["t"].max()) if now is None else now
    rows = rows[rows["t"] >= now - days * 86400.0]
    if rows.size < MIN_ROWS:
        return None
    t0, t1 = float(rows["t"].min()), float(rows["t"].max())
    if t1 <= t0:
        return None
    step = max(900.0, (t1 - t0) / 240.0)
    if t1 - t0 < 2 * step:                 # the first scans: no bins yet, so no empty ones
        step = float(np.diff(np.sort(rows["t"])).max()) * 1.001
    idx = ((rows["t"] - t0) / step + 1e-6).astype(int)
    nb = int(idx.max()) + 1
    z = np.full((nb, V_MID.size), np.nan, np.float32)
    for i in np.unique(idx):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)     # a velocity with no data
            z[i] = np.nanmedian(rows["spec"][idx == i], axis=0)
    have = np.isfinite(rows["spec"]).mean(0) > 0.5
    if not have.any():
        return None
    vi = np.where(have)[0]
    vi = np.arange(vi[0], vi[-1] + 1)
    return t0 + step * (np.arange(nb) + 0.5), V_MID[vi], z[:, vi], int(rows.size)


def _ticks(x0, x1, most):
    """Round date ticks, and a label format that suits them. x is in days."""
    span = x1 - x0
    for step in (1 / 24, 2 / 24, 3 / 24, 6 / 24, 12 / 24, 1, 2, 3, 5, 7, 14, 30):
        if span >= 3 and step < 1:
            continue
        ticks = np.arange(np.ceil(x0 / step) * step, x1 + 1e-9, step)
        if 2 <= ticks.size <= most:
            break
    else:
        ticks = np.array([x0, x1])
        step = 0
    if step >= 1:
        return ticks, "%d %b"
    return ticks, "%H:%M" if span < 1.5 else "%d %b %H:%M"


def render(hdir, title="", days=7, now=None):
    """Write map3d.png. Returns True when there was enough to draw."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    fig = Figure(figsize=(15, 6.5), dpi=100)
    FigureCanvasAgg(fig)
    if draw(fig, load(hdir), title, days, now) is None:
        return False
    out = os.path.join(hdir, PNG)
    fig.savefig(out + ".tmp.png")
    os.replace(out + ".tmp.png", out)
    return True


def _lighter(x, z, most):
    """At most `most` time rows, by averaging neighbours, for a plot that turns smoothly."""
    k = int(np.ceil(x.size / most))
    if k <= 1:
        return x, z
    n = x.size // k * k
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        zz = np.nanmean(z[:n].reshape(-1, k, z.shape[1]), axis=1)
    return x[:n].reshape(-1, k).mean(1), zz


def draw(fig, rows, title="", days=7, now=None, light=False):
    """The 3D surface and the map beside it, on fig. Returns the 3D axes, or None when
    there is not enough to draw. light: a coarser surface for a plot the user turns."""
    import datetime as dt

    from matplotlib import colormaps
    from matplotlib.colors import Normalize
    from matplotlib.dates import DateFormatter, date2num
    from matplotlib.ticker import FixedLocator
    g = grid(rows, days, now)
    if g is None:
        return None
    t, v, z, n = g
    if t.size < 2:
        return None
    # Scans made at other settings reach less far: keep what nearly all of them share.
    seen = np.flatnonzero(np.isfinite(z[np.isfinite(z).any(1)]).mean(0) >= 0.9)
    if seen.size >= 10:
        v, z = v[seen[0]:seen[-1] + 1], z[:, seen[0]:seen[-1] + 1]
    # Milky Way hydrogen seen from here lies within about +-150 km/s: a wider axis only
    # squashes the line and hides how it moves through the day.
    keep = np.abs(v) <= V_SHOW
    v, z = v[keep], z[:, keep]
    if v.size < 4:
        return None
    # One offset for the whole axis: local time steps back when clocks change.
    x = date2num(dt.datetime.fromtimestamp(t[-1])) - (t[-1] - t) / 86400.0
    finite = z[np.isfinite(z)]
    lo, hi = float(np.percentile(finite, 1)), float(np.percentile(finite, 99.9))
    lo = min(lo, 0.0)
    fig.clear()
    fig.suptitle(f"{title}   {n} scans".strip(), fontsize=12)
    gs = fig.add_gridspec(1, 2, width_ratios=(1.7, 1))
    ax = fig.add_subplot(gs[0], projection="3d", computed_zorder=False)
    xs, vs, zs = x, v, z
    if light:
        xs, zs = _lighter(x, z, 150)
        vs, zs = v[::2], zs[:, ::2]
    # A hole in the surface reads as a tear, so a break lies flat on the floor in grey.
    missing = ~np.isfinite(zs)
    zc = np.where(missing, lo, np.clip(zs, lo, hi))
    # A few scans make a few wide flat cells: fill in between them so the ridge is smooth.
    up = max(1, int(np.ceil(60 / max(xs.size - 1, 1))))
    k = np.repeat(np.arange(xs.size - 1), up)
    f = np.tile(np.arange(up) / up, xs.size - 1)
    k, f = np.append(k, xs.size - 2), np.append(f, 1.0)
    xf = xs[k] + f * (xs[k + 1] - xs[k])
    Z = zc[k] + f[:, None] * (zc[k + 1] - zc[k])
    gone = missing[k] | (missing[k + 1] & (f > 0)[:, None])
    Z[gone] = lo
    # each cell coloured by its own mean height, not by one corner
    cell = 0.25 * (Z[:-1, :-1] + Z[1:, :-1] + Z[:-1, 1:] + Z[1:, 1:])
    colors = np.zeros(Z.shape + (4,))
    colors[:-1, :-1] = colormaps["viridis"](Normalize(lo, hi)(cell))
    # the edge of a break shows the last scan's profile, like the ends of the plot
    broken = gone[:-1, :-1] & gone[1:, :-1] & gone[:-1, 1:] & gone[1:, 1:]
    colors[:-1, :-1][broken] = (0.82, 0.82, 0.82, 1.0)
    X, Y = np.meshgrid(xf, vs, indexing="ij")
    ax._aquila_surface = (X, Y, Z, colors)
    # Time runs across, as in the map beside it; like the sky frequency, low at the front,
    # the receding (positive) velocities are nearest the viewer.
    ax.view_init(elev=32, azim=-60)
    ax.set_xlabel("Time", labelpad=16)
    ax.set_ylabel("Velocity (km/s)", labelpad=8)
    ax.set_zlabel("dB", labelpad=4)
    ax.set_xlim(x[0], x[-1])
    ax.set_ylim(v[-1], v[0])
    ax.set_box_aspect((1.8, 1.0, 0.7))
    ax.set_zlim(lo, hi)
    ticks, fmt = _ticks(x[0], x[-1], 5)
    ax.xaxis.set_major_locator(FixedLocator(ticks))
    ax.xaxis.set_major_formatter(DateFormatter(fmt))
    ax.tick_params(axis="x", labelsize=8)
    lay(ax)
    b = fig.add_subplot(gs[1])
    m = b.pcolormesh(x, v, np.ma.masked_invalid(z.T), cmap="viridis", vmin=lo, vmax=hi,
                     shading="nearest")
    fig.colorbar(m, ax=b, label="Power above baseline (dB)", pad=0.01)
    b.set_ylabel("Velocity (km/s, positive = receding)")
    b.set_xlabel("Time")
    ticks, fmt = _ticks(x[0], x[-1], 7)
    b.xaxis.set_major_locator(FixedLocator(ticks))
    b.xaxis.set_major_formatter(DateFormatter(fmt))
    b.tick_params(axis="x", labelsize=8)
    fig.subplots_adjust(left=0.02, right=0.98, top=0.93, bottom=0.11, wspace=0.08)
    return ax


def lay(ax):
    """(Re)lay the surface for the current view. Matplotlib sorts a surface by mean
    depth, which puts tall cells in front of what really hides them, so it goes down a
    strip at a time, far to near, the strips running across the line of sight."""
    from mpl_toolkits.mplot3d import proj3d
    X, Y, Z, colors = ax._aquila_surface
    for c in getattr(ax, "_aquila_strips", []):
        c.remove()
    M = ax.get_proj()
    corner = [proj3d.proj_transform(X[i, j], Y[i, j], Z[i, j], M)[2]
              for i, j in ((0, 0), (-1, 0), (0, -1))]
    along_v = abs(corner[2] - corner[0]) >= abs(corner[1] - corner[0])
    strips = []
    m = Y.shape[1] if along_v else X.shape[0]
    for i in range(m - 1):
        s = (slice(None), slice(i, i + 2)) if along_v else (slice(i, i + 2), slice(None))
        strips.append(ax.plot_surface(X[s], Y[s], Z[s], facecolors=colors[s], shade=False,
                                      rstride=1, cstride=1, linewidth=0, antialiased=False))
    # the strip whose middle is farthest goes down first (larger depth is farther)
    mid = [proj3d.proj_transform(X[s].mean(), Y[s].mean(), float(Z[s].min()), M)[2]
           for s in ((slice(None), slice(i, i + 2)) if along_v else (slice(i, i + 2), slice(None))
                     for i in range(m - 1))]
    for r, i in enumerate(np.argsort(mid)[::-1]):
        strips[i].set_zorder(r)
    ax._aquila_strips = strips


def render_cmd(cfg):
    """A frozen app takes a verb; a normal install runs the function directly."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable]
        if cfg.get("_path"):
            cmd += ["-c", cfg["_path"]]
        return cmd + ["map3d"]
    pkg = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (f"import sys;sys.path.append({pkg!r});from aquila import map3d;"
            f"map3d.render({os.path.join(cfg['state_dir'], 'hline')!r},"
            f"{str(cfg.get('name') or '')!r},{int((cfg.get('hline') or {}).get('map_days', 7))})")
    return [sys.executable, "-c", code]


def start(cfg, proc=None):
    """Start a render unless one is running. Returns the process to pass next time."""
    if proc is not None and proc.poll() is None:
        return proc
    err = open(os.path.join(cfg["state_dir"], "hline", "map3d.err"), "w")
    kw = {"stdout": subprocess.DEVNULL, "stderr": err}
    if sys.platform == "win32":
        kw["creationflags"] = 0x00004000 | subprocess.CREATE_NO_WINDOW   # below normal priority
    try:
        p = subprocess.Popen(render_cmd(cfg), **kw)
    finally:
        err.close()
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            os.setpriority(os.PRIO_PROCESS, p.pid, 15)
    return p
