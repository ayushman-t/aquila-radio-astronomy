# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# GPL-3.0-or-later: free to use and modify, derived works must stay open.
"""The survey loop. Capture a block, normalise, search, record, repeat.
Raw IQ never touches disk. Memory is fixed. Disk is capped."""
import contextlib
import json
import os
import signal
import subprocess
import sys
import time

import numpy as np

from . import api, config, dsp, sdr
from .config import MIN_SETI_CHANNELS
from .hline import HLine
from .lock import acquire_lock, release_lock, watch_stop
from .plot import decimate, save_waterfall
from .spur import SpurMask
from .store import Store, replace


def ntp_synced():
    """On a systemd box the answer must come from timedatectl; any failure means "not
    synced" (a Pi with no RTC boots in 1970). Elsewhere there is nothing to ask."""
    import shutil
    if shutil.which("timedatectl") is None:
        return True
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=8).stdout.strip()
        return out == "yes"
    except Exception:
        return False


class Survey:
    def __init__(self, cfg, source=None):
        self.cfg = cfg
        self.adv = cfg["advanced"]
        self.src = source or sdr.make(cfg)
        self.store = Store(cfg["state_dir"])
        self.running = True
        self.start_ts = time.time()
        tuning = {"n_chan": int(self.adv["n_chan"]), "rate": int(self.src.rate),
                  "center_hz": int(self.src.center_hz), "sdr": self.src.name}
        self.spur = SpurMask(int(self.adv["spur_bin_ch"]), int(self.adv["spur_min_sightings"]),
                             int(self.adv["spur_keep_blocks"]), self.store.spur_path, tuning)
        self.tot = {"blocks": 0, "obs_s": 0.0}
        self.tot.update(self.store.read_json(self.store.totals, {}))
        self.last = {}
        self.api = None
        self.hline = HLine(cfg, source=self.src, log=self.store.log)
        self.hline.own_status = False

    def start_api(self):
        try:
            self.api = api.serve(self.cfg["state_dir"], self.adv["api_bind"], self.adv["api_port"],
                                 daily_mb=self.adv.get("api_daily_mb", 0),
                                 token=self.adv.get("api_token", ""))
            self.api_addr = f"{self.adv['api_bind']}:{self.adv['api_port']}"
        except OSError as e:
            self.store.log(f"api not started: {e}")
            self.api_addr = "not started"

    def stop(self, *_):
        self.running = False
        self.src.kill()

    def install_signals(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

    def status(self, state, **extra):
        d = {"state": state, "name": self.cfg["name"], "sdr": self.src.name,
             "center_hz": self.src.center_hz, "sample_rate": self.src.rate,
             "n_chan": int(self.adv["n_chan"]),
             "chan_bw_hz": round(self.src.rate / int(self.adv["n_chan"]), 3),
             "dwell_s": self.adv["dwell_s"],
             "dish": {k: v for k, v in self.cfg["dish"].items() if k != "rotator"},
             "site": self.cfg.get("site"),
             "blocks": self.tot["blocks"], "obs_s": round(self.tot["obs_s"]),
             "hits_total": self.store.count_hits(), "seti": getattr(self, "seti", None),
             "api": getattr(self, "api_addr", ""),
             "footprint_mb": round(self.store.footprint_bytes() / 1e6, 1),
             "uptime_s": int(time.time() - self.start_ts), "ts": time.time()}
        d.update(extra)
        try:
            self.store.atomic_json(self.store.status, d)
        except OSError as e:
            self.store.log(f"status write failed: {e}")

    def one_block(self):
        adv, log, st = self.adv, self.store.log, self.store
        nch = int(adv["n_chan"])
        t0 = time.time()
        self.cfg["dish"] = config.pointing(self.cfg)["dish"]
        self.status("capturing")
        save = self.cfg.get("save") or {}
        raw = None
        wf = None
        raw_path = ""
        if save.get("raw_iq"):
            need = self.src.rate * self.src.bytes_per_sample * float(adv["dwell_s"])
            need += float(adv["free_floor_gb"]) * 1e9
            if st.free_bytes() > need:
                try:
                    os.makedirs(st.raw_dir, exist_ok=True)
                    raw_path = os.path.join(st.raw_dir,
                                            time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
                    raw = open(raw_path + ".iq.tmp", "wb")
                except OSError as e:
                    log(f"raw IQ not saved: {e}")
                    raw = None
            else:
                log(f"raw IQ not saved: a block needs {need / 1e9:.1f} GB free")
        try:
            wf, chan_bw, row_s = dsp.channelize(self.src, nch, int(adv["rows"]),
                                                float(adv["dwell_s"]), float(adv["settle_skip_s"]),
                                                log, raw_sink=raw)
        finally:
            if raw is not None:
                with contextlib.suppress(OSError):
                    raw.close()
                try:
                    if wf is not None:
                        os.replace(raw_path + ".iq.tmp", raw_path + ".iq")
                        st.atomic_json(raw_path + ".json", {
                            "sdr": self.src.name, "format": self.src.raw_format,
                            "sample_rate": self.src.rate, "center_hz": self.src.center_hz,
                            "settle_skip_s": adv["settle_skip_s"]})
                    else:
                        os.remove(raw_path + ".iq.tmp")
                except OSError as e:
                    log(f"raw IQ file error: {e}")
        if wf is None:
            return False
        rms = getattr(self.src, "input_rms", None)
        if (rms is not None and rms < dsp.WEAK_INPUT_COUNTS
                and not getattr(self, "_weak_warned", False)):
            self._weak_warned = True
            log("WARNING: " + dsp.WEAK_INPUT_TEXT.format(rms))
        seti = nch >= int(adv.get("seti_min_chan") or MIN_SETI_CHANNELS)
        self.seti = seti
        if not seti and not getattr(self, "_seti_warned", False):
            log(f"SETI search off: FFT {nch} is too small. Hydrogen line only.")
            self._seti_warned = True
        block_s = wf.shape[0] * row_s
        raw_mean = wf.mean(axis=0)
        try:
            self.hline.record_block(wf, row_s)
        except Exception as e:
            log(f"h-line product failed: {e}")
        dsp.normalize(wf)

        blank = dsp.blank_set(nch, chan_bw, adv["dc_notch_hz"], adv.get("exclude_if_khz"))
        if adv.get("adaptive_mask"):
            blank |= self.spur.blank_channels(nch)
        mc = np.fromiter(blank, dtype=int, count=len(blank))
        if mc.size:
            wf[:, mc] = 1.0

        hits = []
        if seti:
            hits = dsp.drift_search(wf, chan_bw, row_s, adv["max_drift_hz_s"],
                                    adv["snr_thresh"], blank_chans=blank, log=log,
                                    cancel=lambda: not self.running)
            if not self.running:
                return False
            hits = dsp.cluster_hits(hits, gap=int(adv["cluster_gap_ch"]))
        # stamped when the capture began: sky_freq is the frequency at the first row,
        # and the search time (minutes on a Pi) must not shift a transit
        utc = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(t0))
        local = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0))

        writable = st.can_write(adv)
        if not writable:
            log("DISK CRITICAL (%.2f GB free): observing but not writing" % (st.free_bytes() / 1e9))

        thumb = ""
        if writable and seti:
            # latest.npz is what the desktop app draws; it is cheap, so it is written
            # whether or not the picture files are
            try:
                dec = decimate(wf)
                np.savez(st.latest_npz + ".tmp.npz",
                         waterfall=np.log10(dec + 1e-6).astype(np.float16),
                         spectrum=decimate(raw_mean[None, :])[0].astype(np.float32),
                         chan_bw=chan_bw, center_hz=self.src.center_hz, n_chan=nch, row_s=row_s,
                         utc=utc, local=local)
                replace(st.latest_npz + ".tmp.npz", st.latest_npz)
            except Exception as e:
                log(f"latest.npz error: {e}")
        if writable and save.get("plots", True) and seti:
            thumb = os.path.join(st.wf_dir, utc + ".png")
            x_hi = None
            half = self.src.rate / 2e3
            edge = [lo for lo, hi in (adv.get("exclude_if_khz") or []) if hi >= half and lo > 0]
            if edge:
                x_hi = (self.src.center_hz + min(edge) * 1e3) / 1e6
            try:
                title = f"{local}  ({utc[9:11]}:{utc[11:13]}:{utc[13:15]} UTC)"
                save_waterfall(wf, thumb, chan_bw, self.src.center_hz, row_s, x_hi, title)
                save_waterfall(wf, st.latest, chan_bw, self.src.center_hz, row_s, x_hi, title)
            except Exception as e:
                log(f"thumb error: {e}")
                thumb = ""

        block = {"utc": utc, "local": local, "center_hz": self.src.center_hz, "n_chan": nch,
                 "chan_bw": chan_bw, "thumb": os.path.basename(thumb), "sdr": self.src.name}
        strong = adv["strong_carrier_snr"]
        carriers = [h for h in hits if h["snr"] >= strong]
        cands = [h for h in hits if h["snr"] < strong]
        if writable and carriers and save.get("csv", True):
            st.append_hits(carriers, block, st.rfi)
        if writable and cands and save.get("csv", True):
            st.append_hits(cands, block, st.hits)
        if writable and seti and save.get("csv", True):
            try:
                st.append_block(block, len(cands), len(carriers),
                                max((h["snr"] for h in hits), default=0.0))
            except OSError as e:
                log(f"blocks.csv write failed: {e}")
        if adv.get("adaptive_mask"):
            flat = chan_bw / block_s * 1.5
            self.spur.update([h["chan"] for h in hits if abs(h["drift_hz_s"]) < flat])

        if writable and cands and save.get("spectra", True) and len(cands) <= adv["flood_hits"]:
            hw = int(adv["snippet_halfwidth_ch"])
            try:
                os.makedirs(st.snip_dir, exist_ok=True)
                for h in sorted(cands, key=lambda h: -h["snr"])[:adv["max_snippet_hits"]]:
                    lo_c, hi_c = max(0, h["chan"] - hw), min(nch, h["chan"] + hw)
                    np.savez_compressed(os.path.join(st.snip_dir, f"{utc}_{h['chan']}.npz"),
                                        waterfall=wf[:, lo_c:hi_c].astype(np.float32),
                                        chan0=lo_c, chan_bw=chan_bw,
                                        center_hz=self.src.center_hz, n_chan=nch, row_s=row_s)
            except OSError as e:
                log(f"snippet write failed: {e}")
        if hits:
            top = max(h["snr"] for h in hits)
            flood = "  [flood, snippets skipped]" if len(cands) > adv["flood_hits"] else ""
            log(f"{len(cands)} candidate(s) + {len(carriers)} carrier(s), top SNR {top:.0f}{flood}")

        self.last = {"utc": utc, "hits": hits, "raw_mean": raw_mean, "chan_bw": chan_bw,
                     "row_s": row_s}
        del wf
        try:
            st.enforce_caps(adv)
        except OSError as e:
            log(f"disk cap check failed: {e}")
        self.tot["blocks"] += 1
        self.tot["obs_s"] += block_s
        try:
            st.atomic_json(st.totals, self.tot)
        except OSError as e:
            log(f"totals write failed: {e}")
        self.status("idle", seti=seti, block_s=round(block_s, 1),
                    drift_step_hz_s=round(chan_bw / block_s, 4),
                    capture_s=round(time.time() - t0, 1), last_hits=len(cands),
                    last_carriers=len(carriers))
        return True

    def curate_async(self):
        """Tier the hits in a separate low-priority process. A flooded day can take
        minutes on a Pi; capture must never wait for it. Skipped while one is running."""
        p = getattr(self, "_curator", None)
        if p is not None:
            rc = p.poll()
            if rc is None:
                return
            if rc != 0:
                err = os.path.join(self.cfg["state_dir"], "curate.err")
                tail = ""
                with contextlib.suppress(OSError):
                    lines = open(err).read().strip().splitlines()
                    tail = lines[-1] if lines else "killed, no message (out of memory?)"
                self.store.log(f"curate failed ({rc}): {tail}")
        cmd = curate_cmd(self.cfg, self.src.center_hz)
        kw = {"stdout": subprocess.DEVNULL}
        if sys.platform == "win32":
            kw["creationflags"] = 0x00004000 | subprocess.CREATE_NO_WINDOW   # below normal priority
        try:
            kw["stderr"] = open(os.path.join(self.cfg["state_dir"], "curate.err"), "w")
            self._curator = subprocess.Popen(cmd, **kw)
            kw["stderr"].close()
            if sys.platform != "win32":
                with contextlib.suppress(OSError):
                    os.setpriority(os.PRIO_PROCESS, self._curator.pid, 10)
        except OSError as e:
            self.store.log(f"curate not started: {e}")

    def stop_requested(self):
        p = os.path.join(self.cfg["state_dir"], "stop")
        if os.path.exists(p):
            with contextlib.suppress(OSError):
                os.remove(p)
            return True
        return False

    def run(self, blocks=0):
        log = self.store.log
        nch = int(self.adv["n_chan"])
        done = fails = 0
        # lock first: a second copy started by mistake must not eat the running one's
        # stop or calibration request before it finds out it cannot run
        self.lock = acquire_lock(self.cfg["state_dir"], log)
        self.stop_requested()
        self.hline.cal_requested()           # a request left from before this start is void
        watch_stop(self.cfg["state_dir"], self.stop)
        save = self.cfg.get("save") or {}
        seti_ok = nch >= int(self.adv.get("seti_min_chan") or MIN_SETI_CHANNELS)
        if not seti_ok:
            for p in (self.store.latest, self.store.latest_npz):
                with contextlib.suppress(OSError):
                    os.remove(p)
        if not save.get("plots", True):
            for p in ("latest.png", "latest_raw.png"):
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(self.hline.dir, p))
        mhz = self.src.center_hz / 1e6
        log(f"=== aquila {self.cfg['name']}: {self.src.name} at {mhz:.4f} MHz, "
            f"{nch} ch @ {self.src.rate / nch:.3f} Hz, dwell {self.adv['dwell_s']}s ===")
        if self.spur.mask:
            log(f"loaded spur mask: {len(self.spur.mask)} bins")
        while self.running:
            if self.adv["require_ntp"] and not ntp_synced():
                log("waiting for NTP sync before observing")
                self.status("waiting-ntp")
                for _ in range(30):
                    if not self.running or self.stop_requested():
                        self.running = False
                        break
                    time.sleep(1)
                continue
            if self.hline.cal_requested():
                # asked for over the API (or by the local app): one reference scan, then
                # straight back to observing. No restart, nothing else changes.
                self.status("calibrating", cal_s=int(self.hline.h["duration_s"]))
                try:
                    self.hline.calibrate(take_lock=False)
                except Exception as e:
                    log(f"requested calibration failed: {e}")
                if self.stop_requested():
                    self.running = False
                continue
            try:
                ok = self.one_block()
            except Exception as e:
                log(f"block error: {e}")
                ok = False
            done += ok
            fails = 0 if ok else fails + 1
            if ok and self.seti and done % 5 == 0:
                try:
                    self.curate_async()
                except Exception as e:              # tiering is extra; observing goes on
                    log(f"curate not started: {e}")
            if blocks and (done >= blocks or fails >= 3):
                self.running = False
            if self.stop_requested():
                self.running = False
            if not ok and self.running:
                # after 20 straight failures (about 2 min) stop refreshing status.json on
                # purpose: a site watchdog that reboots on a stale status can then power-cycle
                # a dead USB hub. While the receiver fails, nothing observes anyway.
                if fails < 20:
                    self.status("retry")
                elif fails == 20:
                    log("20 capture failures in a row: status.json left stale for the watchdog")
                time.sleep(5)
        p = getattr(self, "_curator", None)
        if p is not None:
            # its writes are atomic, so a curator still busy after a short wait is just ended
            with contextlib.suppress(Exception):
                p.wait(timeout=10)
            with contextlib.suppress(Exception):
                p.kill()
        self.status("stopped")
        log("stopped")
        release_lock(self.lock)


def curate_cmd(cfg, center_hz):
    """The command line that tiers the hits in a child process.

    A frozen app (PyInstaller) is its own interpreter and takes a verb, so it runs its
    own `curate` command on the config file. A normal install runs the function
    directly; the package's own folder goes last on the path, so a checkout that was
    never installed still imports, and nothing installed is shadowed."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable]
        if cfg.get("_path"):
            cmd += ["-c", cfg["_path"]]
        return cmd + ["curate"]
    from .skymap import beam_deg
    adv = cfg["advanced"]
    args = {"beam_deg": beam_deg(cfg["dish"].get("diameter_m"), center_hz),
            "center_hz": center_hz,
            "exclude_if_khz": adv.get("exclude_if_khz") or [],
            "snr_thresh": float(adv["snr_thresh"]),
            "comb_window": adv.get("comb_period_hz") or [10000, 100000]}
    code = ("import sys, json; sys.path.append(sys.argv[3]); from aquila import curate; "
            "curate.run(sys.argv[1], **json.loads(sys.argv[2]))")
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [sys.executable, "-c", code, cfg["state_dir"], json.dumps(args), pkg_root]
