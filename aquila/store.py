# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# MIT licence: free for anyone to use and modify.
"""Everything that touches disk: state dir layout, hit CSVs, status.json,
footprint caps. The caps make it impossible for the survey to fill a card."""
import contextlib
import glob
import json
import os
import shutil
import time

import numpy as np


def replace(src, dst, tries=10):
    """os.replace that tolerates a reader holding dst open on Windows."""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.2)


def _by_age(paths):
    """Oldest first. A file deleted between the glob and the stat is skipped."""
    aged = []
    for p in paths:
        with contextlib.suppress(OSError):
            aged.append((os.path.getmtime(p), p))
    return [p for _, p in sorted(aged)]


class Store:
    def __init__(self, state_dir):
        self.dir = state_dir
        self.hits = os.path.join(state_dir, "hits.csv")
        self.rfi = os.path.join(state_dir, "rfi.csv")
        self.status = os.path.join(state_dir, "status.json")
        self.totals = os.path.join(state_dir, "totals.json")
        self.log_path = os.path.join(state_dir, "survey.log")
        self.wf_dir = os.path.join(state_dir, "waterfalls")
        self.snip_dir = os.path.join(state_dir, "snippets")
        self.raw_dir = os.path.join(state_dir, "raw")
        self.latest = os.path.join(state_dir, "latest.png")
        self.latest_npz = os.path.join(state_dir, "latest.npz")
        self.blocks = os.path.join(state_dir, "blocks.csv")
        self.spur_path = os.path.join(state_dir, "spur_mask.json")
        for d in (state_dir, self.wf_dir, self.snip_dir):
            os.makedirs(d, exist_ok=True)

    def log(self, msg):
        line = "{}  {}".format(time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        print(line, flush=True)
        try:
            if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > 5_000_000:
                replace(self.log_path, self.log_path + ".1")
            with open(self.log_path, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass

    @staticmethod
    def atomic_json(path, obj):
        def d(o):
            if isinstance(o, np.integer):
                return int(o)
            if isinstance(o, np.floating):
                return float(o)
            return str(o)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f, default=d)
            f.flush()
            os.fsync(f.fileno())        # survive a power cut: never a zero-length file
        replace(tmp, path)

    def read_json(self, path, default):
        try:
            return json.load(open(path))
        except Exception:
            return default

    def append_hits(self, hits, block, path):
        # a power cut can leave the file created but empty: it still needs its header
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a") as f:
            if new:
                f.write("utc,local,sky_freq_hz,drift_hz_s,snr,width_hz,coverage,thumb,sdr\n")
            for h in hits:
                sky = block["center_hz"] + (h["chan"] - block["n_chan"] / 2) * block["chan_bw"]
                f.write("{},{},{:.0f},{:.4f},{:.2f},{:.1f},{:.1f},{},{}\n".format(
                    block["utc"], block["local"], sky, h["drift_hz_s"], h["snr"],
                    h.get("width_hz", 0), h.get("coverage", -1), block["thumb"],
                    block.get("sdr", "")))

    def append_block(self, block, n_cands, n_carriers, top_snr):
        """One row per block so the app can list them like hydrogen-line scans.
        Bounded to the last 2000 rows (about 150 KB), same as scans.csv."""
        new = not os.path.exists(self.blocks) or os.path.getsize(self.blocks) == 0
        with open(self.blocks, "a") as f:
            if new:
                f.write("utc,local,thumb,hits,carriers,top_snr,center_hz\n")
            f.write("{},{},{},{},{},{:.1f},{:.0f}\n".format(
                block["utc"], block["local"], block["thumb"], n_cands, n_carriers,
                top_snr, block["center_hz"]))
        try:
            if os.path.getsize(self.blocks) > 200_000:
                lines = open(self.blocks).readlines()
                with open(self.blocks + ".tmp", "w") as f:
                    f.writelines(lines[:1] + lines[-2000:])
                replace(self.blocks + ".tmp", self.blocks)
        except OSError:
            pass

    def count_hits(self):
        try:
            with open(self.hits) as f:
                return max(0, sum(1 for _ in f) - 1)
        except OSError:
            return 0

    def footprint_bytes(self):
        b = 0
        for pat in ("*.csv", "*.png", "*.json", "*.log", "waterfalls/*.png", "snippets/*.npz",
                    "hline/*"):
            for f in glob.glob(os.path.join(self.dir, pat)):
                with contextlib.suppress(OSError):
                    b += os.path.getsize(f)
        return b

    def free_bytes(self):
        try:
            return shutil.disk_usage(self.dir).free
        except OSError:
            return 1 << 40

    def can_write(self, adv):
        return self.free_bytes() > float(adv["critical_free_gb"]) * 1e9

    def _rotatable(self):
        return _by_age(glob.glob(os.path.join(self.snip_dir, "*.npz")) +
                       glob.glob(os.path.join(self.wf_dir, "*.png")) +
                       glob.glob(os.path.join(self.dir, "hline", "obs_*")))

    def enforce_caps(self, adv):
        cap = float(adv["footprint_cap_gb"]) * 1e9
        free_floor = float(adv["free_floor_gb"]) * 1e9
        for d, pat, keep in ((self.wf_dir, "*.png", adv["keep_waterfalls"]),
                             (self.snip_dir, "*.npz", adv["keep_snippets"])):
            files = _by_age(glob.glob(os.path.join(d, pat)))
            for f in files[:max(0, len(files) - keep)]:
                with contextlib.suppress(OSError):
                    os.remove(f)
        csv_cap = int(float(adv["csv_cap_mb"]) * 1e6)
        keep_rows = int(adv["csv_keep_rows"])
        for p in (self.hits, self.rfi):
            try:
                if os.path.exists(p) and os.path.getsize(p) > csv_cap:
                    with open(p) as f:
                        lines = f.readlines()
                    with open(p + ".tmp", "w") as f:
                        f.writelines(lines[:1] + lines[-keep_rows:])
                    replace(p + ".tmp", p)
            except OSError:
                pass
        used = self.footprint_bytes()
        if used <= cap and self.free_bytes() >= free_floor:
            return
        for f in self._rotatable():
            if used <= cap and self.free_bytes() >= free_floor:
                break
            try:
                size = os.path.getsize(f)
                os.remove(f)
                used -= size
            except OSError:
                continue
