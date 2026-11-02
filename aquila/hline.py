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

from . import config, sdr
from .lock import acquire_lock, release_lock, watch_stop
from .store import Store, replace

DEFAULTS = {
    "duration_s": 300,
    "channels": 1024,
    "t_sample": 1.0,
    "f_rest_hz": 1420405751.768,
    "n": 20,           # virgo median filter (frequency)
    "m": 35,           # virgo median filter (time)
    "keep_scans": 500,
    "day_file": True,    # one 1024-channel row per block appended to h1/h1_<day>_<kHz>k.dat (4 KB)
    "day_keep_days": 30,
    # frequency switching (H-line-only scans): the receiver alternates between two
    # tunings a throw apart, so its own passband cancels channel by channel and no
    # calibration is needed. throw 0 = twice the flat half of the band, so the
    # reference tuning holds no line.
    "freq_switch": True,
    "fs_phase_s": 30,
    "fs_throw_hz": 0,
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


def observe(src, dat, obs, log=print, fine=0):
    """Stream one scan into dat (float32 rows) and write the .header. Returns True
    when at least 95 percent of the rows arrived.

    fine > 0 also writes dat's "_fine" twin from the same samples, made the way a SETI
    block makes its H-line scan (fine-channel FFT, binned down). A cal must be divided
    into scans made the same way, or the two channelizers' shapes leave a slope."""
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
        sp = np.abs(np.fft.fft(z[:k * chans].reshape(k, chans) * win, axis=1)) ** 2
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


def fine_twin(dat):
    return dat.replace(".dat", "_fine.dat")


def plot(dat, png, obs, h, cal_file=""):
    _patch_matplotlib()
    import virgo
    rows = int(round(obs["duration"] / obs["t_sample"]))
    m = min(int(h["m"]), max(3, rows - 1))
    virgo.plot(obs_parameters=obs, obs_file=dat, cal_file=cal_file, f_rest=h["f_rest_hz"],
               dB=True, n=int(h["n"]), m=m, slope_correction=bool(cal_file), plot_file=png)


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
    # the line is ~20 channels wide; a spur is 1 or 2. A 5-channel running median
    # removes spurs and the DC spike before the fit and the peak search.
    from scipy.ndimage import median_filter
    w = median_filter(w, size=5, mode="nearest")
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
    return _result(ratio - 1.0, v, edge, fit, i)


def _result(x, v, valid, fit, i):
    """x: fractional power above the baseline per channel. Noise is the channel to
    channel scatter on the line free channels (a smooth leftover shape is not noise)."""
    from scipy.ndimage import uniform_filter1d
    spec_db = np.where(valid, 10.0 * np.log10(np.clip(1.0 + x, 1e-6, None)), np.nan)
    hp = x - uniform_filter1d(x, 15, mode="nearest")
    rms = float(np.std(hp[fit])) if fit.sum() > 10 else float("nan")
    return {"pct": float(100.0 * x[i]), "db": float(spec_db[i]), "kms": float(v[i]),
            "rms_db": float(10.0 * np.log10(1.0 + rms)), "v": v, "spec_db": spec_db}


FS_LINE_IF_HZ = -250e3      # where the line sits in the signal band: clear of the DC channel


def fs_throw(rate, throw_hz=0):
    """Frequency switching throw: twice the flat half of the band, so the reference
    tuning sees none of the sky the signal band measures and holds no line."""
    flat = min(0.344 * rate, 1.6e6)
    return float(throw_hz) if throw_hz else 2.0 * flat


def fs_tunings(h, rate):
    """(signal, reference) centre frequencies in Hz."""
    f_sig = h["f_rest_hz"] - FS_LINE_IF_HZ
    return int(round(f_sig)), int(round(f_sig + fs_throw(rate, h.get("fs_throw_hz", 0))))


def _flat_band(R, off_hz, bw):
    """Channels in the flat part of the passband, read from the reference itself: out
    from the centre while the smoothed band stays within 0.5 dB of its middle. Beyond
    the filter shoulders the image leaks in and differs between the two tunings."""
    from scipy.ndimage import median_filter
    lim = min(0.344 * bw, 1.6e6)
    r = median_filter(R, size=15, mode="nearest")
    mid = np.median(r[np.abs(off_hz) < 0.25 * lim])
    ok = np.abs(10.0 * np.log10(np.clip(r, 1e-30, None) / mid)) < 0.5
    c = len(R) // 2
    lo = hi = c
    while lo > 0 and ok[lo - 1] and abs(off_hz[lo - 1]) < lim:
        lo -= 1
    while hi < len(R) - 1 and ok[hi + 1] and abs(off_hz[hi + 1]) < lim:
        hi += 1
    valid = np.zeros(len(R), bool)
    valid[lo + 8:hi - 7] = True        # a little clear of the shoulder
    return valid


def measure_fs(sig_dat, ref_dat, obs, h):
    """Frequency switched spectrum, reduced the standard way.

    S is taken with the line in the band, R with the receiver retuned by the throw,
    so R sees only line free sky. In the same receiver channel both see the same
    passband, spurs and DC offset, so S/R is the sky with the receiver shape gone,
    and needs no calibration. What is left (the sky continuum differing between the
    two tunings, gain drift between phases) is a straight line, fitted on the line
    free channels and divided out, so the line is relative to the continuum under
    it. The line free channels are found by growing the line window out from the
    peak until the line meets the noise."""
    from scipy.ndimage import median_filter, uniform_filter1d
    chans = int(obs["channels"])
    S = np.fromfile(sig_dat, dtype=np.float32).reshape(-1, chans).mean(0)
    R = np.fromfile(ref_dat, dtype=np.float32).reshape(-1, chans).mean(0)
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
    xs = np.where(spike, wide, xs)
    f = obs["frequency"] + off * chw
    v = 299792.458 * (h["f_rest_hz"] - f) / h["f_rest_hz"]
    x = off * chw / bw
    line = valid & (np.abs(v) < 40.0)
    lo = hi = 0
    for _ in range(10):
        fit = valid & ~line
        if fit.sum() < 60:
            return None
        # divide, not subtract: the line is then relative to the continuum under it
        res = (1.0 + xs) / (1.0 + np.polyval(np.polyfit(x[fit], xs[fit], 1), x)) - 1.0
        sig = np.std((res - uniform_filter1d(res, 15, mode="nearest"))[fit])
        sm = uniform_filter1d(res, 9, mode="nearest")
        pk = int(np.argmax(np.where(valid & (np.abs(v) < 160.0), sm, -1e9)))
        lo = hi = pk
        # 2 sigma of a 9-channel mean; the floor (0.0004 dB) stops a noiseless
        # scan's tiny continuum curvature from passing for line
        thr = max(2.0 * sig / 3.0, 1e-4)
        while lo > 0 and valid[lo - 1] and sm[lo - 1] > thr:
            lo -= 1
        while hi < chans - 1 and valid[hi + 1] and sm[hi + 1] > thr:
            hi += 1
        new = np.zeros(chans, bool)
        new[max(lo - 8, 0):hi + 9] = True
        new &= valid
        if (new == line).all():
            break
        line = new
    fit = valid & ~line
    ipk = int(np.argmax(np.where(line, res, -1e9)))
    if not line[ipk] or abs(v[ipk]) > 160.0:
        return None
    m = _result(res, v, valid, fit, ipk)
    m["line_kms"] = (float(v[min(hi + 8, chans - 1)]), float(v[max(lo - 8, 0)]))
    m["baseline_ch"] = int(fit.sum())
    return m


def peak_percent(dat, obs, h, cal_file=""):
    """(peak percent, velocity) or None; see measure()."""
    m = measure(dat, obs, h, cal_file)
    return None if m is None else (m["pct"], m["kms"])


def plot_db(m, png, title=""):
    """The calibrated spectrum in dB above its baseline, against velocity."""
    _patch_matplotlib()
    import matplotlib.pyplot as plt
    ok = ~np.isnan(m["spec_db"])
    v, s = m["v"][ok], m["spec_db"][ok]
    fig, ax = plt.subplots(figsize=(10, 5.2), dpi=100)
    ax.axhspan(-m["rms_db"], m["rms_db"], color="0.85", lw=0, label="baseline noise (1 rms)")
    ax.axhline(0.0, color="0.45", lw=0.8)
    ax.plot(v, s, color="#1f5fa8", lw=1.2, label="calibrated spectrum")
    ax.plot([m["kms"]], [m["db"]], "o", color="#c0392b", ms=6,
            label=f"peak {m['db']:.3f} dB at {m['kms']:.0f} km/s")
    ax.set_xlim(v.max(), v.min())            # approaching gas on the right, as frequency rises
    ax.set_xlabel("Velocity (km/s, positive = receding)")
    ax.set_ylabel("Power above baseline (dB)")
    note = f"noise {m['rms_db']:.4f} dB rms per channel"
    if m.get("method"):
        note = m["method"] + "    " + note
    ax.set_title((title + "\n" if title else "") + note, fontsize=10)
    ax.grid(alpha=0.3)
    ax.legend(loc="upper left", fontsize=9, frameon=False)
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
                         fine=int(self.cfg["advanced"].get("n_chan") or 0))
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
             "ts": time.time()}
        d.update(extra)
        try:
            self.store.atomic_json(self.store.status, d)
        except OSError as e:
            self.log(f"status write failed: {e}")

    def scan(self):
        self.cfg.update(config.pointing(self.cfg))
        if self.h.get("freq_switch", True):
            return self.fs_scan()
        obs = obs_parameters(self.cfg, self.src, self.h)
        self.status("capturing", scans=self.n_scans)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dat = os.path.join(self.dir, f"obs_{stamp}.dat")
        if not observe(self.src, dat, obs, self.log):
            return False
        return self.record(dat, obs, stamp)

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
                    if not observe(src, tmp, obs, self.log):
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
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dat = os.path.join(self.dir, f"obs_{stamp}.dat")
        binned = wf_raw.reshape(rows, chans, nch // chans).mean(axis=2).astype(np.float32)
        binned.tofile(dat + ".tmp")
        replace(dat + ".tmp", dat)
        with open(dat.replace(".dat", ".header"), "w") as f:
            f.write(f"mjd={time.time() / 86400.0 + 40587.0 - rows * row_s / 86400.0:.8f}\n")
            for k2, v2 in obs.items():
                f.write(f"{k2}={v2}\n")
        return self.record(dat, obs, stamp, block=True)

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
        try:
            if ref:
                meas = measure_fs(dat, ref, obs, self.h)
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
                    plot_db(meas, cal_png, f"{self.cfg.get('name') or 'node'}   scan {stamp}")
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
                            num("pct", "{:.2f}"), num("kms", "{:.1f}"), int(usable_cal),
                            time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
                            num("db", "{:.3f}"), num("rms_db", "{:.4f}")])
        if meas is None:
            self.log(f"scan {stamp}: no line measurement")
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
        return True

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
        cal = "yes" if os.path.exists(self.cal) else "no"
        self.log(f"=== h-line: {self.src.name} at {self.src.center_hz / 1e6:.4f} MHz, "
                 f"{self.h['duration_s']} s scans, cal {cal}")
        while self.running and (not blocks or n < blocks) and fails < 5:
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
            else:
                fails += 1
                time.sleep(5)
        self.status("stopped", scans=self.n_scans)
        release_lock(self.lock)


def _copy(src, dst):
    import shutil
    shutil.copyfile(src, dst + ".tmp")
    replace(dst + ".tmp", dst)
