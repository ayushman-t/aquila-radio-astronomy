# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# MIT licence: free for anyone to use and modify.
"""Per-block thumbnail: waterfall on top, integrated spectrum below, absolute dB,
x axis in MHz. Small, fast, readable by eye."""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

BG = "#0b0f18"


def decimate(wf, max_cols=2048):
    rows, nch = wf.shape
    if nch <= max_cols:
        return wf
    f = nch // max_cols
    return wf[:, :f * max_cols].reshape(rows, max_cols, f).max(axis=2)


def save_waterfall(wf, path, chan_bw, center_hz, row_s, x_hi_mhz=None, title=""):
    dec = decimate(wf)
    nch0 = wf.shape[1]
    lg = 10 * np.log10(dec + 1e-6)
    sdb = 10 * np.log10(dec.mean(axis=0) + 1e-6)
    floor = float(np.median(sdb))
    ncols = dec.shape[1]
    kept = (nch0 // ncols) * ncols if nch0 > ncols else nch0
    f_lo = (center_hz - nch0 * chan_bw / 2.0) / 1e6
    f_hi = f_lo + kept * chan_bw / 1e6
    xs = np.linspace(f_lo, f_hi, sdb.size)
    dur = wf.shape[0] * row_s

    fig = plt.figure(figsize=(4.8, 3.9), dpi=100)
    ax = fig.add_axes([0.11, 0.42, 0.86, 0.46])
    ax2 = fig.add_axes([0.11, 0.11, 0.86, 0.26], sharex=ax)
    vmin, vmax = np.percentile(lg, 5), np.percentile(lg, 99.5)
    ax.imshow(lg, aspect="auto", origin="lower", cmap="inferno", vmin=vmin, vmax=vmax,
              extent=[f_lo, f_hi, 0, dur])
    ax.set_ylabel("time (s)", fontsize=6)
    ax.tick_params(labelsize=5, colors="#999", labelbottom=False)
    ax2.plot(xs, sdb, lw=0.6, color="#ffb000")
    ax2.axhline(floor, color="#7799bb", lw=0.5, ls=":")
    ax2.text(0.99, 0.06, f"floor {floor:.2f} dB   peak {sdb.max():.2f} dB",
             transform=ax2.transAxes, ha="right", fontsize=5, color="#9fb6cc")
    ax2.set_xlabel("MHz", fontsize=6)
    ax2.set_ylabel("dB", fontsize=6)
    ax2.ticklabel_format(axis="x", useOffset=False, style="plain")
    ax2.xaxis.set_major_locator(plt.MaxNLocator(6))
    if x_hi_mhz and f_lo < x_hi_mhz < f_hi:
        ax.set_xlim(f_lo, x_hi_mhz)
    ax2.tick_params(labelsize=5, colors="#999")
    ax2.set_facecolor(BG)
    for sp in ax2.spines.values():
        sp.set_color("#334")
    fig.patch.set_facecolor(BG)
    if title:
        fig.text(0.5, 0.955, title, ha="center", va="center", fontsize=6.5, color="#cfd8e3")
    tmp = path + ".tmp"
    fig.savefig(tmp, facecolor=BG, format="png")
    plt.close(fig)
    from .store import replace
    replace(tmp, path)
