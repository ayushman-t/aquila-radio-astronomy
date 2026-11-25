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
BIN_S = 900.0                     # one time row of the plot
MAX_COLS = 1000                   # time rows the map draws at most
BOX = (1.8, 1.0, 0.7)             # the 3D box: time, velocity, dB


def binned(v, db):
    """A scan's spectrum on the fixed velocity grid, so scans at any rate or tuning line up."""
    ok = np.isfinite(db) & np.isfinite(v)
    n, _ = np.histogram(v[ok], V_EDGES)
    s, _ = np.histogram(v[ok], V_EDGES, weights=db[ok])
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(n > 0, s / n, np.nan)
    # at 10 Msps and up a channel is wider than a bin, so some bins get no channel: those
    # take the value between the two channels either side. A hole in the data stays a hole.
    if v.size > 2 and ok.sum() > 1:
        chan = float(np.median(np.abs(np.diff(v))))
        order = np.argsort(v[ok])
        vs, ds = v[ok][order], db[ok][order]
        empty = np.where((n == 0) & (vs[0] < V_MID) & (vs[-1] > V_MID))[0]
        if chan > V_EDGES[1] - V_EDGES[0] and empty.size:
            j = np.searchsorted(vs, V_MID[empty])
            near = (vs[j] - vs[j - 1]) <= 1.5 * chan
            out[empty[near]] = np.interp(V_MID[empty[near]], vs, ds)
    return out.astype(np.float32)


def together(spec):
    """The scans of one time bin as one row. Three or more: their median, so one bad scan
    does not stand up as a ridge. Two: the stronger one as measured, since a median of two
    is their average and would lower a short peak whenever two scans share a bin."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)         # a velocity with no data
        if spec.shape[0] >= 3:
            return np.nanmedian(spec, axis=0)
        peak = np.nanmax(spec, axis=1)
    return spec[int(np.argmax(np.where(np.isfinite(peak), peak, -np.inf)))]


def bridge(z, t0, step, rows, cols=slice(None)):
    """Scans that take longer than one time bin leave bins that no scan ended in. Such a
    bin, between two scans that followed one another, shows the scan recorded across it.
    A break in observing stays empty."""
    if rows.size < 3:
        return
    order = np.argsort(rows["t"])
    ts = rows["t"][order]
    usual = float(np.median(np.diff(ts)))
    if usual <= step:
        return
    for i in np.where(~np.isfinite(z).any(axis=1))[0]:
        j = int(np.searchsorted(ts, t0 + (i + 1) * step))     # the first scan to end after it
        if 0 < j < ts.size and ts[j] - ts[j - 1] <= 1.5 * usual:
            z[i] = rows["spec"][order[j]][cols]


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
    # always 15 minutes: a wider bin averages a peak that passes in half an hour with the
    # weaker sky beside it, by an amount that changes from day to day. draw() thins what
    # is too much to draw, and keeps the peaks when it does.
    step = BIN_S
    if t1 - t0 < 2 * step:                 # the first scans: no bins yet, so no empty ones
        step = float(np.diff(np.sort(rows["t"])).max()) * 1.001
    idx = ((rows["t"] - t0) / step + 1e-6).astype(int)
    nb = int(idx.max()) + 1
    z = np.full((nb, V_MID.size), np.nan, np.float32)
    for i in np.unique(idx):
        z[i] = together(rows["spec"][idx == i])
    bridge(z, t0, step, rows)
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
    if span < 1.5:
        return ticks, "%H:%M"
    # over several days the date goes on the midnight ticks only, the others get the
    # hour, so neighbouring labels never run into each other
    return ticks, _day_or_hour


def _day_or_hour(x, pos=None):
    from matplotlib.dates import num2date
    d = num2date(x)
    return d.strftime("%d %b") if d.hour == 0 and d.minute == 0 else d.strftime("%H:%M")


def _format(axis, fmt):
    from matplotlib.dates import DateFormatter
    from matplotlib.ticker import FuncFormatter
    axis.set_major_formatter(FuncFormatter(fmt) if callable(fmt) else DateFormatter(fmt))


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


def thin(x, z, most):
    """At most `most` time rows for the drawing. Each group of neighbouring rows is shown
    by its strongest one, as measured: an average would lower a peak that lasts less than
    the group, by an amount that depends on where the group edges fall, so the same sky
    would read differently from day to day. Groups are counted back from the newest row."""
    k = int(np.ceil(x.size / most))
    if k <= 1:
        return x, z
    pad = -x.size % k
    zp = np.concatenate([np.full((pad, z.shape[1]), np.nan, z.dtype), z]).reshape(-1, k, z.shape[1])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)         # a row with no data
        peak = np.nanmax(zp, axis=2)
    best = np.argmax(np.where(np.isfinite(peak), peak, -np.inf), axis=1)
    step = float(x[1] - x[0])
    xp = np.concatenate([x[0] - step * np.arange(pad, 0, -1), x]).reshape(-1, k)
    return xp.mean(1), zp[np.arange(zp.shape[0]), best]


def scale(z):
    """Bottom and top of the dB scale for z[time, velocity]. The top is the highest peak
    that the row measured before or after it also shows at half its height or more, so
    the sky's own peaks are never cut short and one bad scan standing alone does not set
    the scale."""
    finite = z[np.isfinite(z)]
    lo, most = float(np.percentile(finite, 1)), float(np.percentile(finite, 99.9))
    lo = min(lo, 0.0)
    rows = z[np.isfinite(z).any(axis=1)]                 # a break in observing is no neighbour
    peak = np.nanmax(np.where(np.isfinite(rows), rows, -np.inf), axis=1) - lo
    beside = np.maximum(np.r_[0.0, peak[:-1]], np.r_[peak[1:], 0.0])
    held = peak[beside >= 0.5 * peak]
    hi = lo + float(held.max()) if held.size else most
    return lo, hi if hi > lo else lo + 1e-3          # a flat history still needs a scale


def draw(fig, rows, title="", days=7, now=None, light=False, fig2=None, lsr=False):
    """The 3D surface and the map beside it, on fig. Returns the 3D axes, or None when
    there is not enough to draw. light: a coarser surface for a plot the user turns.
    fig2: draw the map on this second figure instead, and no title on either (the app
    puts the two in their own boxes). lsr: the rows are on the LSR velocity axis."""
    import datetime as dt

    from matplotlib import colormaps
    from matplotlib.colors import Normalize
    from matplotlib.dates import date2num
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
    lo, hi = scale(z)
    t, z = thin(t, z, MAX_COLS)      # a month of 15 minute rows is more than the map can show
    # One offset for the whole axis: local time steps back when clocks change.
    x = date2num(dt.datetime.fromtimestamp(t[-1])) - (t[-1] - t) / 86400.0
    fig.clear()
    if fig2 is None:
        fig.suptitle(f"{title}   {n} scans".strip(), fontsize=12)
        gs = fig.add_gridspec(1, 2, width_ratios=(1.7, 1))
        ax = fig.add_subplot(gs[0], projection="3d", computed_zorder=False)
    else:
        ax = fig.add_subplot(111, projection="3d", computed_zorder=False)
    ax._aquila_scans = n
    xs, zs = thin(x, z, 150 if light else 240)
    vs = v
    if light and v.size >= 4:
        n2 = v.size // 2 * 2                       # pairs of velocities, the higher of each
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            zs = np.nanmax(zs[:, :n2].reshape(zs.shape[0], -1, 2), axis=2)
        vs = v[:n2].reshape(-1, 2).mean(1)
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
    # Time runs across and velocity increases the same way as in the map beside it, so a
    # feature sits at the same place in both when the view is turned flat.
    ax.view_init(elev=32, azim=-60)
    ax.set_xlabel("Time", labelpad=16)
    ax.set_ylabel("LSR velocity (km/s)" if lsr else "Velocity (km/s)", labelpad=8)
    ax.set_zlabel("dB", labelpad=4)
    ax.set_xlim(x[0], x[-1])
    ax.set_ylim(v[0], v[-1])
    ax.set_box_aspect(BOX)
    ax.set_zlim(lo, hi)
    ticks, fmt = _ticks(x[0], x[-1], 5)
    ax.xaxis.set_major_locator(FixedLocator(ticks))
    _format(ax.xaxis, fmt)
    ax.tick_params(axis="x", labelsize=8)
    lay(ax)
    if fig2 is None:
        b = fig.add_subplot(gs[1])
    else:
        fig2.clear()
        b = fig2.add_subplot(111)
    m = b.pcolormesh(x, v, np.ma.masked_invalid(z.T), cmap="viridis", vmin=lo, vmax=hi,
                     shading="nearest")
    (fig if fig2 is None else fig2).colorbar(m, ax=b, label="Power above baseline (dB)", pad=0.01)
    b.set_ylabel(("LSR velocity" if lsr else "Velocity") + " (km/s, positive = receding)")
    b.set_xlabel("Time")
    ticks, fmt = _ticks(x[0], x[-1], 7)
    b.xaxis.set_major_locator(FixedLocator(ticks))
    _format(b.xaxis, fmt)
    b.tick_params(axis="x", labelsize=8)
    if fig2 is None:
        fig.subplots_adjust(left=0.02, right=0.98, top=0.93, bottom=0.11, wspace=0.08)
    else:
        fig.subplots_adjust(left=0.0, right=1.0, top=1.0, bottom=0.06)
    return ax


def _extent(ax, renderer):
    """The box of the 3D view with its numbers and labels, in pixels."""
    from matplotlib.transforms import Bbox
    from mpl_toolkits.mplot3d import proj3d
    x, y, z = np.meshgrid(ax.get_xlim3d(), ax.get_ylim3d(), ax.get_zlim3d())
    px, py, _ = proj3d.proj_transform(x.ravel(), y.ravel(), z.ravel(), ax.get_proj())
    parts = [Bbox.null()]
    parts[0].update_from_data_xy(ax.transData.transform(np.column_stack([px, py])))
    for axis in (ax.xaxis, ax.yaxis, ax.zaxis):
        b = axis.get_tightbbox(renderer)
        if b is not None and np.all(np.isfinite(b.extents)):
            parts.append(b)
    return Bbox.union(parts)


def fit(ax, zoom, edge=3.0):
    """Keep the numbers and labels of the 3D view inside its figure as it is turned now:
    moved over where that is enough, drawn smaller where the figure is too narrow for it.
    Returns the zoom, `zoom` at most."""
    from matplotlib.transforms import Bbox
    fig = ax.figure
    strips = getattr(ax, "_aquila_strips", [])
    home = getattr(ax, "_aquila_home", None)
    if home is None:
        home = ax._aquila_home = ax.get_position(original=True).frozen()
    z, move = zoom, np.zeros(2)
    try:
        renderer = fig.canvas.get_renderer()
        for c in strips:
            c.set_visible(False)        # the surface is slow to draw and stays inside the box
        for _ in range(8):
            ax.set_box_aspect(BOX, zoom=z)
            ax.set_position(Bbox(home.get_points() + move / fig.bbox.size))
            fig.draw_without_rendering()
            bb, f = _extent(ax, renderer), fig.bbox
            low = bb.min - (f.min + edge)           # room left over on each side, x and y
            high = (f.max - edge) - bb.max
            fits = low + high >= 0
            # where it fits it moves just far enough, where it cannot it sits in the middle
            step = np.where(fits, np.maximum(-low, 0) - np.maximum(-high, 0), 0.5 * (high - low))
            move += step
            if fits.all():
                if step.any():
                    ax.set_position(Bbox(home.get_points() + move / fig.bbox.size))
                break
            # the labels keep their distance from the box, so one step falls a little short
            z *= max(float(np.min((f.size - 2 * edge) / bb.size)), 0.5) * 0.99
    except Exception:
        z = zoom                        # an older matplotlib: the view as it always was
        ax.set_position(home)
    finally:
        for c in strips:
            c.set_visible(True)
    return z


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
