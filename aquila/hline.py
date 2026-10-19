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


def fine_twin(dat):
    return dat.replace(".dat", "_fine.dat")


def plot(dat, png, obs, h, cal_file=""):
    _patch_matplotlib()
    import virgo
    rows = int(round(obs["duration"] / obs["t_sample"]))
    m = min(int(h["m"]), max(3, rows - 1))
    virgo.plot(obs_parameters=obs, obs_file=dat, cal_file=cal_file, f_rest=h["f_rest_hz"],
               dB=True, n=int(h["n"]), m=m, slope_correction=bool(cal_file), plot_file=png)


def peak_percent(dat, obs, h, cal_file=""):
    """Line strength in percent above the baseline, and its velocity.

    With a receiver cal the spectrum is divided by it first, which removes the
    passband, and a straight line is enough for what is left. Without a cal the
    raw passband is fitted with a cubic, which only works when the band holds
    baseline on BOTH sides of the line; at 2.4 or 3 Msps with the default centre
    it does not, and extrapolating across the passband peak invents lines, so
    the function returns None ("no measurement") rather than a wrong number."""
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
    pct = 100.0 * (w / base - 1.0)
    if not cal_file and np.abs(pct[fit]).max() > 1.0:
        return None           # the raw passband is not fitted well enough to trust a number
    i = np.argmax(np.where(line, pct, -1e9))
    if abs(v[i]) > 130.0:                         # at the window edge: baseline, not gas
        return None
    return float(pct[i]), float(v[i])


class _PlotsOff(Exception):
    pass


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
        obs = obs_parameters(self.cfg, self.src, self.h)
        self.status("capturing", scans=self.n_scans)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dat = os.path.join(self.dir, f"obs_{stamp}.dat")
        if not observe(self.src, dat, obs, self.log):
            return False
        return self.record(dat, obs, stamp)

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

    def record(self, dat, obs, stamp, block=False):
        save = self.cfg.get("save") or {}
        cal = self.cal_for(block)
        raw_png = dat.replace(".dat", "_raw.png")
        cal_png = dat.replace(".dat", ".png")
        try:
            if not save.get("plots", True) or getattr(self, "_no_virgo", False):
                raise _PlotsOff
            plot(dat, raw_png, obs, self.h)
            _copy(raw_png, os.path.join(self.dir, "latest_raw.png"))
            usable, why = self.cal_usable(obs)
            if usable:
                plot(dat, cal_png, obs, self.h, cal_file=cal)
                _copy(cal_png, os.path.join(self.dir, "latest.png"))
            elif os.path.exists(self.cal):
                if why != getattr(self, "_cal_warned", None):
                    self.log("calibrated plot skipped: " + why)
                    self._cal_warned = why
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(self.dir, "latest.png"))
        except _PlotsOff:
            pass
        except ImportError:
            self._no_virgo = True
            self.log("hydrogen line plots off: virgo is not installed "
                     "(pip install 'aquila-radio-astronomy[hline]')")
        except Exception as e:
            self.log(f"virgo plot failed: {e}")
        meas = None
        usable_cal = self.cal_usable(obs)[0]
        try:
            meas = peak_percent(dat, obs, self.h, cal if usable_cal else "")
            if meas is None and not usable_cal and not getattr(self, "_meas_warned", False):
                self._meas_warned = True
                self.log("line strength not measured: without a calibration the raw passband "
                         "cannot be fitted reliably on this receiver. Take a calibration "
                         "(50 ohm terminator or cold sky) and every scan gets a number.")
        except Exception as e:
            self.log(f"measure failed: {e}")
        if not save.get("spectra", True):
            for f in (dat, dat.replace(".dat", ".header")):
                with contextlib.suppress(OSError):
                    os.remove(f)
        if save.get("csv", True):
            new = not os.path.exists(self.csv)
            if not new:
                self._upgrade_csv_header()
            with open(self.csv, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["local", "file", "peak_pct", "peak_kms", "calibrated", "utc"])
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), os.path.basename(dat),
                            "" if meas is None else f"{meas[0]:.2f}",
                            "" if meas is None else f"{meas[1]:.1f}",
                            int(self.cal_usable(obs)[0]),
                            time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())])
        if meas is None:
            self.log(f"scan {stamp}: no line measurement")
        else:
            how = "calibrated" if usable_cal else "raw"
            self.log(f"scan {stamp}: {how} peak {meas[0]:.2f}% at {meas[1]:.0f} km/s")
        if self.own_status:
            self.status("idle", scans=self.n_scans, last_peak_pct=None if meas is None else meas[0])
        self.trim()
        return True

    def _upgrade_csv_header(self):
        """A scans.csv from before the utc column: rewrite its header line once."""
        try:
            with open(self.csv, "r+", newline="") as f:
                head = f.readline()
                if "utc" in head:
                    return
                rest = f.read()
                f.seek(0)
                f.write("local,file,peak_pct,peak_kms,calibrated,utc\n" + rest)
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
        for pat in ("obs_*.dat", "obs_*.header", "obs_*.png", "obs_*_raw.png"):
            files = sorted(glob.glob(os.path.join(self.dir, pat)))
            if pat == "obs_*.png":                 # the raw twins are counted on their own
                files = [f for f in files if not f.endswith("_raw.png")]
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
