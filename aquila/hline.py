# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# GPL-3.0-or-later: free to use and modify, derived works must stay open.
"""Hydrogen line mode. One scan = a virgo style observation.dat (1024 channels,
one averaged row per second) plus its .header, then the plot straight from
virgo: calibrated against a receiver-only cal if one exists, and always the
uncalibrated one too. The cal is one scan taken with a 50 ohm terminator on
the LNA input, never the sky."""
import contextlib
import csv
import glob
import os
import time

import numpy as np

from . import config, lineview, map3d, sdr
from .lock import acquire_lock, release_lock, watch_stop
from .store import Store, replace

DEFAULTS = {
    "duration_s": 300,
    "channels": 1024,
    "t_sample": 1.0,
    "f_rest_hz": 1420405751.768,
    # median filters, set in Settings. n (channels, 0 = about 5 km/s at any rate)
    # smooths the dB spectrum and its peak: it removes the channel noise that would
    # otherwise lift the peak, wider starts to flatten the line top. m (samples)
    # smooths virgo's power-against-time panel; across the spectrum it only adds
    # noise (measured +50%), so it stays there.
    "n": 0,
    "m": 35,
    "keep_scans": 500,
    "day_file": True,    # one 1024-channel row per block appended to h1/h1_<day>_<kHz>k.dat (4 KB)
    "day_keep_days": 30,
    "map3d": True,       # the multi-day plot: about 1.2 KB per scan, redrawn every 30 minutes
    "map_days": 7,
    # frequency switching (H-line-only scans): the receiver alternates between two
    # tunings a throw apart, so its own passband cancels channel by channel and no
    # calibration is needed. throw 0 = twice the flat half of the band, so the
    # reference tuning holds no line (it is always above the line: below, the RF
    # chain's shape differs). Measured on the 2.4 m against its 50 ohm cal: baseline
    # 8 times flatter, same line shape. Since 0.2.16 this is the only mode: a stored
    # receiver reference (aquila cal) is used only where the band holds no line.
    "fs_phase_s": 30,
    "fs_throw_hz": 0,
    # search and calibrated line from one receiver: this many search blocks at the signal
    # tuning, then one reference block, not searched. 0: the line in search mode is
    # divided by a stored calibration. Measured on the 2.4 m: 150 s phases as flat as 30 s.
    "fs_search_blocks": 3,
    # frequency switched: baseline fitted outside +-this. Measured on the 0.9 m by retuning:
    # broad structure fixed in sky frequency reaches about -170 km/s, so +-120 fitted part of
    # it as baseline
    "line_window_kms": 180,
    # scan only between the bursts of pulsed interference (see quiet_frames). Off unless
    # a site needs it, and it needs a gain below compression: an overdriven receiver
    # squeezes the bursts down to the gaps, and the line was lost at full VGA (measured)
    "blank_pulses": False,
    # draw the calibrated line with a Gaussian of this sigma (the measured peak and the
    # saved spectrum never see it): rounds off the steps a median leaves in noisy data
    "plot_smooth": True,
    "plot_smooth_kms": 2.0,
}


def _patch_matplotlib():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.cbook as cb
    for name in ("GrouperView", "Grouper"):
        c = getattr(cb, name, None)
        if c is not None and not hasattr(c, "join"):
            c.join = lambda self, *a, **k: None


def obs_parameters(cfg, src, h, channels=None, t_sample=None, duration=None):
    site = cfg.get("site") or {}
    loc = ""
    if site.get("lat") is not None and site.get("lon") is not None:
        loc = f"{site['lat']} {site['lon']} {site.get('alt', 0)}"
    g = src.gain
    return {"dev_args": f"{src.name}=0,bias={int(src.bias)}",
            "rf_gain": g.get("lna", g.get("gain", 0)), "if_gain": g.get("mixer", 0),
            "bb_gain": g.get("vga", 0), "frequency": src.center_hz, "bandwidth": src.rate,
            "channels": int(channels or h["channels"]),
            "t_sample": float(t_sample or h["t_sample"]),
            "duration": int(duration or h["duration_s"]), "loc": loc, "ra_dec": "",
            "az_alt": f"{cfg['dish']['az']:.1f} {cfg['dish']['el']:.1f}"}


def quiet_frames(frames, factor=1.3):
    """Which FFT frames of one row hold no burst of pulsed interference.

    A strong pulsed signal (a TDD radio frame, a radar) buries the sky while it is on,
    changes from moment to moment and differs between two scans, so neither a
    calibration nor frequency switching can remove it. Sky noise is steady from frame
    to frame, so a frame well above the row's quietest ones holds a burst. Without
    pulses every frame stays and nothing changes."""
    power = (np.abs(frames) ** 2).mean(axis=1)
    keep = power < factor * np.percentile(power, 10)
    if keep.sum() < 5:
        keep = power <= np.percentile(power, 20)
    return keep


def quiet_rows(rows, factor=1.05):
    """Which rows of a scan have no raised floor. Interference that fills the gaps between
    pulses gets past quiet_frames and lowers the line without bending the baseline."""
    power = rows.mean(axis=1)
    keep = power < factor * np.percentile(power, 10)
    return keep if keep.sum() >= 10 else np.ones(len(rows), bool)


def observe(src, dat, obs, log=print, fine=0, blank=False, kept=None):
    """Stream one scan into dat (float32 rows) and write the .header. Returns True
    when at least 95 percent of the rows arrived.

    fine > 0 also writes dat's "_fine" twin from the same samples, made the way a SETI
    block makes its H-line scan (fine-channel FFT, binned down). A cal must be divided
    into scans made the same way, or the two channelizers' shapes leave a slope.

    blank averages only the frames quiet_frames keeps; the share kept in each row is
    appended to the list kept when one is given."""
    chans, secs, tsamp = int(obs["channels"]), int(obs["duration"]), float(obs["t_sample"])
    rate = src.rate
    per_row = int(rate * tsamp)
    per_row -= per_row % chans
    nrows = int(round(secs / tsamp))
    mjd0 = time.time() / 86400.0 + 40587.0
    win = np.hanning(chans).astype(np.float32)
    rows = []
    fine = int(fine) if fine and fine % chans == 0 and fine > chans else 0
    if fine:
        fwin = np.hanning(fine).astype(np.float32)
        facc = np.zeros(fine)
        fbuf = np.zeros(0, np.complex64)
        fn = 0
    src.start(per_row * nrows, per_row)
    for fr in src.frames(per_row):
        z = src.to_complex(fr)
        k = z.size // chans
        frames = z[:k * chans].reshape(k, chans)
        if blank and k:
            keep = quiet_frames(frames)
            frames = frames[keep]
            if kept is not None:
                kept.append(float(keep.mean()))
        sp = np.abs(np.fft.fft(frames * win, axis=1)) ** 2
        rows.append(np.fft.fftshift(sp.mean(0)).astype(np.float32))
        if fine:
            fbuf = np.concatenate((fbuf, z.astype(np.complex64)))
            while fbuf.size >= fine:
                X = np.fft.fft(fbuf[:fine] * fwin)
                facc += X.real * X.real + X.imag * X.imag
                fn += 1
                fbuf = fbuf[fine:]
        if len(rows) >= nrows:
            break
    err = src.finish()
    if len(rows) < nrows * 0.95:
        log(f"scan short: {len(rows)}/{nrows} rows. {err.strip()[-120:]}")
        return False
    outs = [(dat, np.array(rows, np.float32))]
    if fine and fn:
        row = np.fft.fftshift(facc / fn).reshape(chans, fine // chans).mean(axis=1)
        outs.append((fine_twin(dat), np.tile(row.astype(np.float32), (len(rows), 1))))
    for path, arr in outs:
        arr.tofile(path + ".tmp")
        replace(path + ".tmp", path)
        with open(path.replace(".dat", ".header"), "w") as h:
            h.write(f"mjd={mjd0:.8f}\n")
            for k2, v2 in obs.items():
                h.write(f"{k2}={v2}\n")
    return True


CSV_HEAD = "local,file,peak_pct,peak_kms,calibrated,utc,peak_db,rms_db"


def median_channels(h, obs):
    """The frequency median filter width in channels: n as set, or about 5 km/s."""
    n = int(h.get("n") or 0)
    if n > 0:
        return n
    kms = float(obs["bandwidth"]) / int(obs["channels"]) * 299792.458 / h["f_rest_hz"]
    return max(1, int(round(5.0 / kms)))


def fine_twin(dat):
    return dat.replace(".dat", "_fine.dat")


def plot(dat, png, obs, h, cal_file=""):
    _patch_matplotlib()
    import virgo
    rows = int(round(obs["duration"] / obs["t_sample"]))
    m = min(int(h["m"]), max(3, rows - 1))
    virgo.plot(obs_parameters=obs, obs_file=dat, cal_file=cal_file, f_rest=h["f_rest_hz"],
               dB=True, n=median_channels(h, obs), m=m, slope_correction=bool(cal_file),
               plot_file=png)


def measure(dat, obs, h, cal_file=""):
    """The line measured against its own baseline, or None.

    With a receiver cal the spectrum is divided by it first, which removes the
    passband, and a straight line is enough for what is left. Without a cal the
    raw passband is fitted with a cubic, which only works when the band holds
    baseline on BOTH sides of the line; at 2.4 or 3 Msps with the default centre
    it does not, and extrapolating across the passband peak invents lines, so
    the function returns None ("no measurement") rather than a wrong number.

    Returns the dict of _result(): pct and db (line peak above the baseline, the
    same number in percent and in dB), kms (its velocity), rms_db (channel noise),
    and the arrays v and spec_db (the whole spectrum, for the plot)."""
    chans = int(obs["channels"])
    w = np.fromfile(dat, dtype=np.float32).reshape(-1, chans).mean(0)
    f = obs["frequency"] + (np.arange(chans) - chans / 2.0) * obs["bandwidth"] / chans
    v = 299792.458 * (h["f_rest_hz"] - f) / h["f_rest_hz"]
    # flat part of the passband only: SDR filters step and bump beyond ~1.6 MHz
    edge = np.abs(f - obs["frequency"]) < min(0.344 * obs["bandwidth"], 1.6e6)
    dc = np.abs(np.arange(chans) - chans // 2) <= 2      # DC spike: never a line, never baseline
    line = (np.abs(v) < 160.0) & ~dc
    fit = edge & ~line & ~dc
    if fit.sum() < 50:
        return None
    if cal_file:
        c = np.fromfile(cal_file, dtype=np.float32).reshape(-1, chans).mean(0)
        w = w / (c + 1e-12)
        deg = 1
    else:
        below = int((fit & (v > 0)).sum())
        above = int((fit & (v < 0)).sum())
        if min(below, above) < 30:
            return None
        deg = 3
    # the line is ~20 channels wide; a spur is 1 or 2. A running median (n channels,
    # at least 5) removes spurs and the DC spike before the fit and the peak search.
    from scipy.ndimage import median_filter
    w = median_filter(w, size=max(5, median_channels(h, obs)), mode="nearest")
    x = (f - obs["frequency"]) / obs["bandwidth"]
    base = np.polyval(np.polyfit(x[fit], w[fit], deg), x)
    ratio = w / base
    if np.any(ratio[edge] <= 0):
        return None
    pct = 100.0 * (ratio - 1.0)
    if not cal_file and np.abs(pct[fit]).max() > 1.0:
        return None           # the raw passband is not fitted well enough to trust a number
    i = np.argmax(np.where(line, pct, -1e9))
    if abs(v[i]) > 130.0:                         # at the window edge: baseline, not gas
        return None
    m = _result(ratio - 1.0, v, edge, fit, i)
    m["n"] = max(5, median_channels(h, obs))
    return m


def _result(x, v, valid, fit, i, unsmoothed=None):
    """x: fractional power above the baseline per channel. Noise is the channel to
    channel scatter on the line free channels (a smooth leftover shape is not noise),
    taken before any median filter when that is given."""
    from scipy.ndimage import uniform_filter1d
    spec_db = np.where(valid, 10.0 * np.log10(np.clip(1.0 + x, 1e-6, None)), np.nan)
    u = x if unsmoothed is None else unsmoothed
    hp = u - uniform_filter1d(u, 15, mode="nearest")
    rms = float(np.std(hp[fit])) if fit.sum() > 10 else float("nan")
    return {"pct": float(100.0 * x[i]), "db": float(spec_db[i]), "kms": float(v[i]),
            "rms_db": float(10.0 * np.log10(1.0 + rms)), "v": v, "spec_db": spec_db}


# where the line sits in the signal band, as a share of the rate: near the middle, so there
# is line free band on both sides of it for the baseline, and clear of the DC channel
FS_LINE_IF = 0.04


def fs_throw(rate, throw_hz=0):
    """Frequency switching throw: twice the flat half of the band, so the reference
    tuning sees none of the sky the signal band measures and holds no line."""
    flat = min(0.344 * rate, 1.6e6)
    return float(throw_hz) if throw_hz else 2.0 * flat


def fs_tunings(h, rate):
    """(signal, reference) centre frequencies in Hz."""
    f_sig = h["f_rest_hz"] - FS_LINE_IF * rate
    return int(round(f_sig)), int(round(f_sig + fs_throw(rate, h.get("fs_throw_hz", 0))))


def fs_search_blocks(cfg, rate):
    """Search blocks between two reference blocks, 0 when the search does not frequency
    switch: a search tuned where its band does not hold the line."""
    h = {**DEFAULTS, **(cfg.get("hline") or {})}
    if abs(cfg["center_mhz"] * 1e6 - h["f_rest_hz"]) > min(0.344 * rate, 1.6e6):
        return 0
    return max(0, int(h.get("fs_search_blocks") or 0))


def _flat_band(R, off_hz, bw):
    """Channels inside the passband, read from the reference itself: out from the
    centre until the filter shoulder, where the band is both below its middle and
    falling steeply (beyond it the image leaks in and differs between the two
    tunings). Ripple is gradual and cancels anyway, so it does not stop the band."""
    from scipy.ndimage import median_filter
    lim = min(0.344 * bw, 1.6e6)
    r = median_filter(R, size=15, mode="nearest")
    mid = np.median(r[np.abs(off_hz) < 0.25 * lim])
    db = 10.0 * np.log10(np.clip(r, 1e-30, None) / mid)
    k = max(2, int(round(1e5 / abs(off_hz[1] - off_hz[0]))))     # channels in 100 kHz
    c = len(R) // 2

    def edge(step):
        i = c
        while 0 < i + step < len(R) and abs(off_hz[i + step]) < lim:
            j = i + step
            falling = db[j - k * step] - db[j] if 0 <= j - k * step < len(R) else 0.0
            if db[j] < -3.0 or (db[j] < -0.3 and falling > 0.25):
                break
            i = j
        return i
    lo, hi = edge(-1), edge(1)
    valid = np.zeros(len(R), bool)
    valid[lo + 16:hi - 15] = True       # clear of the shoulder
    return valid


# the line window never narrows below this: closer in, the baseline would sit on the line
FS_MIN_WINDOW_KMS = 80.0


def measure_fs(sig_dat, ref_dat, obs, h, why=None):
    """Frequency switched spectrum, reduced the standard way. None when the scan cannot
    be measured; the reason is then appended to the list why, when one is given.

    S is taken with the line in the band, R with the receiver retuned by the throw,
    so R sees only line free sky. In the same receiver channel both see the same
    passband, spurs and DC offset, so S/R is the sky with the receiver shape gone,
    and needs no calibration. What is left (the sky continuum differing between the
    two tunings, gain drift between phases) is a straight line, fitted on the line
    free channels (outside a fixed line window) and divided out, so the line is
    relative to the continuum under it."""
    from scipy.ndimage import median_filter
    chans = int(obs["channels"])
    S = np.fromfile(sig_dat, dtype=np.float32).reshape(-1, chans)
    R = np.fromfile(ref_dat, dtype=np.float32).reshape(-1, chans)
    if h.get("blank_pulses"):
        S, R = S[quiet_rows(S)], R[quiet_rows(R)]
    S, R = S.mean(0), R.mean(0)
    bw = float(obs["bandwidth"])
    chw = bw / chans
    xs = (S - R) / (R + 1e-30)
    off = np.arange(chans) - chans / 2.0
    valid = _flat_band(R, off * chw, bw)
    # spurs: the receiver's own sit in the same channel in both tunings and cancel;
    # a carrier fixed in sky or RF frequency (outside transmitters, clock harmonics at
    # round MHz) does not. They are a few channels wide, the line is not: a channel
    # standing far off a 9-channel median is clipped, and its two neighbours with it.
    med = median_filter(xs, size=9, mode="nearest")
    d = xs - med
    mad = np.median(np.abs(d[valid] - np.median(d[valid]))) * 1.4826 + 1e-12
    wide = median_filter(xs, size=31, mode="nearest")
    spike = (np.abs(d) > 6.0 * mad) & (np.abs(d) > 0.5 * np.abs(xs - wide))
    spike = np.convolve(spike, np.ones(5), mode="same") > 0
    # the DC channels: their offset changes with the tuning on some radios (HackRF,
    # RTL), so they need not cancel. Bridged from their neighbours.
    dc = np.abs(off) <= 2
    f = obs["frequency"] + off * chw
    v = 299792.458 * (h["f_rest_hz"] - f) / h["f_rest_hz"]
    win = float(h.get("line_window_kms", 180.0))
    if chw > 6e3:
        # channels so wide (above ~6 Msps) that a narrow line is a few channels too:
        # inside the line window a spur can no longer be told from gas
        spike &= np.abs(v) >= win
    xs = np.where(spike, wide, xs)
    xs[dc] = np.interp(off[dc], off[~dc], xs[~dc])
    x = off * chw / bw
    # a fixed line window, baseline outside it: a window grown from the data runs
    # away on broad structure under the line and the peak then depends on it
    # a line fitted from one side only swings freely across the other, so the window
    # narrows until the band has baseline on both sides of the line
    def sides(w):
        return min((valid & (v > w)).sum(), (valid & (v < -w)).sum())
    while win > 120 and sides(win) < 30:
        win -= 10
    if sides(win) < 30:
        # a receiver whose flat band is narrower than its rate suggests (an Airspy R2 at
        # 2.5 Msps: 1.25 MHz) has no room for that window. A narrower one, with baseline
        # on both sides, still measures the line; broad wings are then partly fitted away
        narrow = win
        while narrow > FS_MIN_WINDOW_KMS and sides(narrow) < 30:
            narrow -= 10
        if sides(narrow) >= 30:
            win = narrow
    line = valid & (np.abs(v) < win)
    fit = valid & ~line
    if fit.sum() < 60:
        if why is not None:
            why.append(f"the flat part of the receiver band, {valid.sum() * chw / 1e6:.2f} MHz "
                       f"at {bw / 1e6:g} Msps, is too narrow for a baseline beside the line. "
                       "Use a higher sample rate")
        return None
    # divide, not subtract: the line is then relative to the continuum under it
    res = (1.0 + xs) / (1.0 + np.polyval(np.polyfit(x[fit], xs[fit], 1), x)) - 1.0
    n = median_channels(h, obs)
    sm = median_filter(res, size=n, mode="nearest") if n > 1 else res
    ipk = int(np.argmax(np.where(line, sm, -1e9)))
    if not line[ipk]:
        if why is not None:
            why.append("the hydrogen line is outside the flat part of the receiver band")
        return None
    m = _result(sm, v, valid, fit, ipk, unsmoothed=res)
    m["n"] = n
    m["line_kms"] = (win, -win)
    m["baseline_ch"] = int(fit.sum())
    return m


def peak_percent(dat, obs, h, cal_file=""):
    """(peak percent, velocity) or None; see measure()."""
    m = measure(dat, obs, h, cal_file)
    return None if m is None else (m["pct"], m["kms"])


def plot_db(m, png, title="", smooth_kms=0.0):
    """The calibrated spectrum in dB above its baseline, against velocity. smooth_kms
    only changes the line drawn: the peak marked and quoted stays as measured."""
    _patch_matplotlib()
    import matplotlib.pyplot as plt
    ok = ~np.isnan(m["spec_db"])
    v, s = m["v"][ok], m["spec_db"][ok]
    if smooth_kms > 0 and v.size > 1:
        from scipy.ndimage import gaussian_filter1d
        s = gaussian_filter1d(s, smooth_kms / abs(v[1] - v[0]), mode="nearest")
    fig, ax = plt.subplots(figsize=(10, 5.2), dpi=100)
    note = f"noise {m['rms_db']:.4f} dB rms per channel"
    if m.get("n", 0) > 1:
        note += f"    median filter {m['n']} channels"
    if m.get("method"):
        note = m["method"] + "    " + note
    extra = []
    if smooth_kms > 0:
        extra.append(f"drawn with {smooth_kms:g} km/s Gaussian")
    if m.get("quiet_pct", 100) < 90:
        extra.append(f"pulsed interference removed, {m['quiet_pct']:.0f}% of the time kept")
    lines = ([title] if title else []) + [note] + (["    ".join(extra)] if extra else [])
    lineview.line_axes(ax, v, s, m["rms_db"], m["kms"], m["db"], "\n".join(lines))
    fig.tight_layout()
    fig.savefig(png)
    plt.close(fig)


class HLine:
    def __init__(self, cfg, source=None, log=print):
        self.cfg = cfg
        self.h = dict(DEFAULTS)
        self.h.update(cfg.get("hline") or {})
        self.dir = os.path.join(cfg["state_dir"], "hline")
        os.makedirs(self.dir, exist_ok=True)
        self.cal = os.path.join(self.dir, "cal.dat")
        self.csv = os.path.join(self.dir, "scans.csv")
        self.src = source or sdr.make(cfg)
        self.store = Store(cfg["state_dir"])
        self.log = self.store.log if log is print else log
        self.running = True
        self.start_ts = time.time()
        self.n_scans = 0
        self.own_status = True
        self.fs_rows = []
        self.fs_mjd = 0.0
        self.fs_search = 0          # set by the search when it frequency switches

    def stop(self, *_):
        self.running = False
        self.src.kill()

    def cal_for(self, block):
        """The cal to divide by: a SETI block's scan wants the cal made the same way
        (its fine twin); a cal from before the twin existed still serves, less exactly."""
        if block and os.path.exists(fine_twin(self.cal)):
            return fine_twin(self.cal)
        if block and os.path.exists(self.cal) and not getattr(self, "_twin_warned", False):
            self._twin_warned = True
            self.log("calibration predates this version: take a new one for the most "
                     "accurate line strength in SETI+H-line mode")
        return self.cal

    def cal_usable(self, obs):
        """The cal must have been taken at this centre and rate, else it is a different
        bandpass and dividing by it is wrong. Returns (usable, reason)."""
        if not os.path.exists(self.cal):
            return False, "no calibration yet"
        hdr = {}
        try:
            for line in open(self.cal.replace(".dat", ".header")):
                k, _, v = line.strip().partition("=")
                hdr[k] = v
        except OSError:
            return False, "calibration header missing"
        try:
            f, bw = float(hdr.get("frequency", 0)), float(hdr.get("bandwidth", 0))
        except ValueError:
            return False, "calibration header unreadable"
        if abs(f - obs["frequency"]) > 1 or abs(bw - obs["bandwidth"]) > 1:
            now = f"{obs['frequency'] / 1e6:.4f} MHz at {obs['bandwidth'] / 1e6:g} Msps"
            return False, (f"calibration is for {f / 1e6:.4f} MHz at {bw / 1e6:g} Msps, "
                           f"now {now}. Take a new one.")
        for k, what in (("dev_args", "receiver"), ("channels", "channel count"),
                        ("rf_gain", "gain"), ("if_gain", "gain"), ("bb_gain", "gain")):
            if hdr.get(k, "") != str(obs.get(k, "")):
                return False, (f"calibration was taken with a different {what} "
                               f"({k} {hdr.get(k, '?')}, now {obs.get(k)}). Take a new one.")
        return True, ""

    CAL_REQUEST = "cal_request"

    def cal_requested(self):
        """A remote (or local GUI) calibration request: the file the API writes. Removed
        when picked up, so a request runs once."""
        p = os.path.join(self.cfg["state_dir"], self.CAL_REQUEST)
        if os.path.exists(p):
            with contextlib.suppress(OSError):
                os.remove(p)
            return True
        return False

    def calibrate(self, take_lock=True):
        obs = obs_parameters(self.cfg, self.src, self.h)
        lk = acquire_lock(self.cfg["state_dir"], self.log) if take_lock else None
        try:
            self.log("taking calibration: 50 ohm terminator, "
                     "or cold sky well off the galactic plane")
            self.keep_previous_cal()
            ok = observe(self.src, self.cal, obs, self.log,
                         fine=int(self.cfg["advanced"].get("n_chan") or 0),
                         blank=self.h.get("blank_pulses", False))
            if ok:
                self.log(f"cal saved: {self.cal}")
            return ok
        finally:
            if lk is not None:
                release_lock(lk)

    def keep_previous_cal(self):
        """The cal about to be replaced is kept as cal.prev.*: a calibration asked for
        remotely is a sky cal wherever the dish points, and must not destroy a good
        terminator cal with no way back."""
        import shutil
        for src in (self.cal, fine_twin(self.cal)):
            for p in (src, src.replace(".dat", ".header")):
                if os.path.exists(p):              # cal.dat -> cal.prev.dat, and so on
                    d, name = os.path.split(p)
                    with contextlib.suppress(OSError):
                        shutil.copy2(p, os.path.join(d, "cal.prev" + name[len("cal"):]))

    def status(self, state, **extra):
        d = {"state": state, "mode": "hline", "name": self.cfg["name"], "sdr": self.src.name,
             "center_hz": self.src.center_hz, "sample_rate": self.src.rate,
             "n_chan": int(self.h["channels"]), "dwell_s": int(self.h["duration_s"]),
             "dish": {k: v for k, v in self.cfg["dish"].items() if k != "rotator"},
             "site": self.cfg.get("site"),
             "uptime_s": int(time.time() - self.start_ts),
             "calibration": "frequency switching",
             "ts": time.time()}
        d.update(extra)
        try:
            self.store.atomic_json(self.store.status, d)
        except OSError as e:
            self.log(f"status write failed: {e}")

    def scan(self):
        self.cfg.update(config.pointing(self.cfg))
        self._kept = []
        return self.fs_scan()

    def fs_scan(self):
        """One frequency switched scan: signal (the line in the band) and reference
        (retuned by the throw, no line) in alternating phases of fs_phase_s, so slow
        gain drift hits both equally. Written as obs_<stamp>.dat (signal) and
        obs_<stamp>_ref.dat, each a normal virgo scan of its own tuning."""
        src, h = self.src, self.h
        home = src.center_hz
        f_sig, f_ref = fs_tunings(h, src.rate)
        throw = f_ref - f_sig
        phase = max(5, int(h.get("fs_phase_s", 30)))
        pairs = max(1, int(round(h["duration_s"] / (2.0 * phase))))
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dat = os.path.join(self.dir, f"obs_{stamp}.dat")
        ref = dat.replace(".dat", "_ref.dat")
        tmp = dat.replace(".dat", "_phase.dat")
        acc = {f_sig: [], f_ref: []}
        self.status("capturing", scans=self.n_scans)
        mjd0 = time.time() / 86400.0 + 40587.0
        try:
            for _ in range(pairs):
                for fc in (f_sig, f_ref):
                    if not self.running:
                        return False
                    src.center_hz = fc
                    obs = obs_parameters(self.cfg, src, h, duration=phase)
                    if not observe(src, tmp, obs, self.log,
                                   blank=self.h.get("blank_pulses", False), kept=self._kept):
                        return False
                    rows = np.fromfile(tmp, dtype=np.float32).reshape(-1, int(obs["channels"]))
                    # the first row after a retune can hold the tuner settling and the
                    # tool's start up buffers: dropped in both tunings alike
                    acc[fc].append(rows[1:] if len(rows) > 2 else rows)
        finally:
            src.center_hz = home
            for p in (tmp, tmp.replace(".dat", ".header")):
                with contextlib.suppress(OSError):
                    os.remove(p)
        tsamp = float(h["t_sample"])
        kept = min(sum(len(a) for a in acc[fc]) for fc in acc)
        for fc, path in ((f_sig, dat), (f_ref, ref)):
            src.center_hz = fc
            obs = obs_parameters(self.cfg, src, h, duration=round(kept * tsamp))
            obs["fs_throw_hz"] = f"{throw:.0f}"
            obs["fs_ref"] = os.path.basename(ref)
            np.concatenate(acc[fc])[:kept].astype(np.float32).tofile(path + ".tmp")
            replace(path + ".tmp", path)
            with open(path.replace(".dat", ".header"), "w") as fh:
                fh.write(f"mjd={mjd0:.8f}\n")
                for k2, v2 in obs.items():
                    fh.write(f"{k2}={v2}\n")
        src.center_hz = f_sig
        obs = obs_parameters(self.cfg, src, h, duration=round(kept * tsamp))
        src.center_hz = home
        obs["fs_throw_hz"] = throw
        return self.record(dat, obs, stamp, ref=ref)

    def record_block(self, wf_raw, row_s):
        """H-line product from a SETI block: the raw waterfall binned to 1024 channels,
        written as a virgo scan. This is how one observation feeds both searches."""
        rows, nch = wf_raw.shape
        chans = int(self.h["channels"])
        if nch % chans:
            return False
        self.cfg.update(config.pointing(self.cfg))
        obs = obs_parameters(self.cfg, self.src, self.h, chans, row_s, rows * row_s)
        if self.h.get("day_file", True):
            self.day_file(wf_raw, obs, rows * row_s)
        binned = wf_raw.reshape(rows, chans, nch // chans).mean(axis=2).astype(np.float32)
        if self.fs_search:
            if not self.fs_rows:
                self.fs_mjd = time.time() / 86400.0 + 40587.0 - rows * row_s / 86400.0
            self.fs_rows.append(binned)
            return True
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dat = os.path.join(self.dir, f"obs_{stamp}.dat")
        binned.tofile(dat + ".tmp")
        replace(dat + ".tmp", dat)
        with open(dat.replace(".dat", ".header"), "w") as f:
            f.write(f"mjd={time.time() / 86400.0 + 40587.0 - rows * row_s / 86400.0:.8f}\n")
            for k2, v2 in obs.items():
                f.write(f"{k2}={v2}\n")
        return self.record(dat, obs, stamp, block=True)

    def record_reference(self, wf_raw, row_s, f_sig, f_ref):
        """The reference block ends a frequency switched scan: the search blocks held since
        the last one are the signal, this block the reference."""
        signal, self.fs_rows = self.fs_rows, []
        if not signal:
            return False
        rows, nch = wf_raw.shape
        chans = int(self.h["channels"])
        if nch % chans:
            return False
        reference = wf_raw.reshape(rows, chans, nch // chans).mean(axis=2).astype(np.float32)
        signal = np.concatenate(signal)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dat = os.path.join(self.dir, f"obs_{stamp}.dat")
        ref = dat.replace(".dat", "_ref.dat")
        src, home = self.src, self.src.center_hz
        try:
            for fc, path, data in ((f_ref, ref, reference), (f_sig, dat, signal)):
                src.center_hz = fc
                obs = obs_parameters(self.cfg, src, self.h, chans, row_s, len(data) * row_s)
                obs["fs_throw_hz"] = f"{f_ref - f_sig:.0f}"
                obs["fs_ref"] = os.path.basename(ref)
                data.tofile(path + ".tmp")
                replace(path + ".tmp", path)
                with open(path.replace(".dat", ".header"), "w") as fh:
                    fh.write(f"mjd={self.fs_mjd:.8f}\n")
                    for k2, v2 in obs.items():
                        fh.write(f"{k2}={v2}\n")
        finally:
            src.center_hz = home
        obs["fs_throw_hz"] = f_ref - f_sig
        return self.record(dat, obs, stamp, ref=ref)

    def day_file(self, wf_raw, obs, block_s):
        """The compact H-line product: one integrated 1024-channel row per block,
        appended to a per-day file. 4 KB per block, so a metered remote site can
        serve a day of it cheaply. Same layout the Mauganj node has produced since
        August, so the site publisher reads it unchanged."""
        try:
            rows, nch = wf_raw.shape
            chans = int(obs["channels"])
            if nch % chans:
                return
            row = wf_raw.mean(axis=0, dtype=np.float64)
            row = row.reshape(chans, nch // chans).mean(axis=1).astype(np.float32)
            d = os.path.join(self.cfg["state_dir"], "h1")
            os.makedirs(d, exist_ok=True)
            day = time.strftime("%Y%m%d", time.gmtime())
            dat = os.path.join(d, f"h1_{day}_{int(obs['frequency']) // 1000}k.dat")
            with open(dat, "ab") as fh:
                row.tofile(fh)
            hdr = dat.replace(".dat", ".header")
            if not os.path.exists(hdr) or os.path.getsize(hdr) == 0:
                with open(hdr, "w") as fh:
                    fh.write(f"mjd={40587.0 + time.time() / 86400.0:.9f}\n")
                    hdr_obs = dict(obs, duration=0, t_sample=round(block_s, 3),
                                   loc=obs.get("loc") or "0 0 0")   # virgo needs three numbers
                    for k2, v2 in hdr_obs.items():
                        fh.write(f"{k2}={v2}\n")
            cutoff = time.time() - float(self.h.get("day_keep_days", 30)) * 86400
            for fn in os.listdir(d):
                p = os.path.join(d, fn)
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
        except Exception as e:
            self.log(f"h1 day file skipped: {e}")

    def record(self, dat, obs, stamp, block=False, ref=None):
        save = self.cfg.get("save") or {}
        cal = self.cal_for(block)
        raw_png = dat.replace(".dat", "_raw.png")
        cal_png = dat.replace(".dat", ".png")
        plots = save.get("plots", True)
        usable_cal, why = (True, "") if ref else self.cal_usable(obs)
        meas = None
        no_meas = []
        try:
            if ref:
                meas = measure_fs(dat, ref, obs, self.h, no_meas)
                if meas is not None:
                    meas["method"] = "frequency switched"
            else:
                meas = measure(dat, obs, self.h, cal if usable_cal else "")
                if meas is not None:
                    meas["method"] = "calibrated" if usable_cal else "raw"
            if meas is None and not usable_cal and not getattr(self, "_meas_warned", False):
                self._meas_warned = True
                self.log("line strength not measured: without a calibration the raw passband "
                         "cannot be fitted reliably on this receiver. Take a calibration "
                         "(50 ohm terminator or cold sky) and every scan gets a number.")
            kept = getattr(self, "_kept", None)
            if meas is not None and kept and not block:
                meas["quiet_pct"] = 100.0 * float(np.median(kept))
        except Exception as e:
            self.log(f"measure failed: {e}")
        if plots and not getattr(self, "_no_virgo", False):
            try:
                plot(dat, raw_png, obs, self.h)
                _copy(raw_png, os.path.join(self.dir, "latest_raw.png"))
            except ImportError:
                self._no_virgo = True
                self.log("raw hydrogen line plots off: virgo is not installed "
                         "(pip install 'aquila-radio-astronomy[hline]')")
            except Exception as e:
                self.log(f"virgo plot failed: {e}")
        if plots:
            try:
                if usable_cal and meas is not None:
                    smooth = (float(self.h.get("plot_smooth_kms", 2.0))
                              if self.h.get("plot_smooth", True) else 0.0)
                    plot_db(meas, cal_png, f"{self.cfg.get('name') or 'node'}   scan {stamp}",
                            smooth)
                    _copy(cal_png, os.path.join(self.dir, "latest.png"))
                elif os.path.exists(self.cal) or ref:
                    if not usable_cal and why != getattr(self, "_cal_warned", None):
                        self.log("calibrated plot skipped: " + why)
                        self._cal_warned = why
                    with contextlib.suppress(OSError):
                        os.remove(os.path.join(self.dir, "latest.png"))
            except Exception as e:
                self.log(f"calibrated plot failed: {e}")
        if not save.get("spectra", True):
            for f in (dat, dat.replace(".dat", ".header")) + (
                    (ref, ref.replace(".dat", ".header")) if ref else ()):
                with contextlib.suppress(OSError):
                    os.remove(f)
        if save.get("csv", True):
            new = not os.path.exists(self.csv)
            if not new:
                self._upgrade_csv_header()

            def num(k, fmt):
                return "" if meas is None or meas.get(k) is None else fmt.format(meas[k])
            with open(self.csv, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(CSV_HEAD.split(","))
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), os.path.basename(dat),
                            num("pct", "{:.2f}"), num("kms", "{:.1f}"),
                            int(usable_cal and meas is not None),
                            time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
                            num("db", "{:.3f}"), num("rms_db", "{:.4f}")])
        if meas is None:
            self.log(f"scan {stamp}: no line measurement" + (f", {no_meas[0]}" if no_meas else ""))
        elif usable_cal:
            self.log(f"scan {stamp}: {meas['method']} peak {meas['db']:.3f} dB "
                     f"({meas['pct']:.2f}%) at {meas['kms']:.0f} km/s, "
                     f"noise {meas['rms_db']:.4f} dB rms")
        else:
            self.log(f"scan {stamp}: raw peak {meas['pct']:.2f}% at {meas['kms']:.0f} km/s")
        if self.own_status:
            self.status("idle", scans=self.n_scans,
                        last_peak_pct=None if meas is None else meas["pct"],
                        last_peak_db=None if meas is None or not usable_cal else meas["db"])
        self.trim()
        if usable_cal and meas is not None:
            self._map_scan(meas)
        return True

    def _map_scan(self, meas):
        """One row in the multi-day history; the plot is redrawn every 30 minutes, and
        after every scan while it is young, so a new node does not show its first three
        scans for half an hour."""
        if not self.h.get("map3d", True):
            return
        try:
            map3d.append(self.dir, time.time(), meas["v"], meas["spec_db"])
            now = time.time()
            if now - getattr(self, "_map_trim", 0) > 86400:
                self._map_trim = now
                map3d.trim(self.dir, float(self.h.get("day_keep_days", 30)))
            have = os.path.getsize(os.path.join(self.dir, map3d.HISTORY)) // map3d.ROW.itemsize
            wait = 0 if have < map3d.YOUNG_ROWS else 1800
            if now - getattr(self, "_map_at", 0) > wait and have >= map3d.MIN_ROWS:
                self._map_at = now
                self._map_proc = map3d.start(self.cfg, getattr(self, "_map_proc", None))
        except Exception as e:
            self.log(f"multi-day plot failed: {e}")

    def _map_backfill(self):
        if not self.h.get("map3d", True) or os.path.exists(os.path.join(self.dir, map3d.HISTORY)):
            return
        try:
            map3d.backfill(self.dir, lambda s, r, o: measure_fs(s, r, o, self.h), self.log)
        except Exception as e:
            self.log(f"multi-day plot not started from stored scans: {e}")

    def _upgrade_csv_header(self):
        """A scans.csv from an older version: rewrite its header line once. Old rows
        keep their fewer columns; readers treat missing ones as blank."""
        try:
            with open(self.csv, "r+", newline="") as f:
                head = f.readline()
                if head.strip() == CSV_HEAD:
                    return
                rest = f.read()
                f.seek(0)
                f.write(CSV_HEAD + "\n" + rest)
                f.truncate()
        except OSError:
            pass

    def trim(self):
        keep = int(self.h["keep_scans"])
        try:                                     # scans.csv: keep the last 2000 rows (~130 KB)
            if os.path.exists(self.csv) and os.path.getsize(self.csv) > 200_000:
                lines = open(self.csv).readlines()
                with open(self.csv + ".tmp", "w") as f:
                    f.writelines(lines[:1] + lines[-2000:])
                replace(self.csv + ".tmp", self.csv)
        except OSError:
            pass
        for pat in ("obs_*.dat", "obs_*.header", "obs_*.png", "obs_*_raw.png",
                    "obs_*_ref.dat", "obs_*_ref.header"):
            files = sorted(glob.glob(os.path.join(self.dir, pat)))
            if "_" not in pat[4:]:                 # twins (_raw, _ref) are counted on their own
                files = [f for f in files if not f.endswith(("_raw.png", "_ref.dat",
                                                             "_ref.header"))]
            for f in files[:max(0, len(files) - keep)]:
                with contextlib.suppress(OSError):
                    os.remove(f)

    def stop_requested(self):
        p = os.path.join(self.cfg["state_dir"], "stop")
        if os.path.exists(p):
            with contextlib.suppress(OSError):
                os.remove(p)
            return True
        return False

    def run(self, blocks=0):
        n = fails = 0
        self.lock = acquire_lock(self.cfg["state_dir"], self.log)
        self.stop_requested()
        self.cal_requested()                 # a request left from before this start is void
        watch_stop(self.cfg["state_dir"], self.stop)
        self._map_backfill()
        cal = "yes" if os.path.exists(self.cal) else "no"
        self.log(f"=== h-line: {self.src.name} at {self.src.center_hz / 1e6:.4f} MHz, "
                 f"{self.h['duration_s']} s scans, cal {cal}")
        while self.running and (not blocks or n < blocks) and not (blocks and fails >= 5):
            if self.cal_requested():
                self.status("calibrating", scans=self.n_scans, cal_s=int(self.h["duration_s"]))
                try:
                    self.calibrate(take_lock=False)
                except Exception as e:
                    self.log(f"requested calibration failed: {e}")
                if self.stop_requested():
                    self.running = False
                continue
            try:
                ok = self.scan()
            except Exception as e:
                self.log(f"scan error: {e}")
                ok = False
            if self.stop_requested():
                self.running = False
            if ok:
                n += 1
                self.n_scans += 1
                fails = 0
            elif self.running:
                # keep trying, as the survey does: a radio still held by an old capture or
                # a USB glitch must not end observing. After 20 failures status.json is
                # left stale on purpose, so a site watchdog can power-cycle the hub.
                fails += 1
                if fails < 20:
                    self.status("retry", scans=self.n_scans)
                elif fails == 20:
                    self.log("20 capture failures in a row: "
                             "status.json left stale for the watchdog")
                time.sleep(5)
        self.status("stopped", scans=self.n_scans)
        release_lock(self.lock)


def _copy(src, dst):
    import shutil
    shutil.copyfile(src, dst + ".tmp")
    replace(dst + ".tmp", dst)
