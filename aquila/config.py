# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# MIT licence: free for anyone to use and modify.
"""Node configuration. One JSON file. The user-facing keys are the first block;
everything below `advanced` has defaults that work and rarely needs touching."""
import json
import os

MIN_SETI_CHANNELS = 1 << 18      # below this a real drift cannot be resolved in a block

USER_KEYS = ("name", "sdr", "center_mhz", "sample_rate", "dish", "site", "state_dir")

DEFAULTS = {
    "name": "my-dish",
    "sdr": "auto",                 # auto | airspy | hydrasdr | rtlsdr | hackrf | synthetic
    "center_mhz": 1420.0,          # H1 (1420.4058) lands +400 kHz, off the DC spur
    "sample_rate": 0,              # 0 = the SDR's proven preset rate
    "dish": {"az": 0.0, "el": 90.0, "diameter_m": 1.0, "rotator": ""},
    "site": {"lat": None, "lon": None, "alt": 0},   # only for velocity axes, optional
    "state_dir": "",               # empty = ~/aquila-radio-astronomy/<name>

    "advanced": {
        "n_chan": 1 << 20,         # 2^20 channels
        "seti_min_chan": 1 << 18,  # below this the SETI search is off, H-line still runs
        "rows": 30,
        "dwell_s": 150,
        "settle_skip_s": 3.0,
        "gain": "preset",          # preset | {"lna":..,"mixer":..,"vga":..} etc
        "bias_tee": True,
        "max_drift_hz_s": 4.0,
        "snr_thresh": 25.0,
        "dc_notch_hz": 3000,
        "strong_carrier_snr": 300,
        "exclude_if_khz": [],
        "cluster_gap_ch": 200,
        "comb_period_hz": [10000, 100000],   # curator searches for a supply comb in this range
        "adaptive_mask": False,    # learn fixed spurs; off, normalisation removes them
        "spur_bin_ch": 8,
        "spur_min_sightings": 3,
        "spur_keep_blocks": 12,
        "footprint_cap_gb": 3.0,
        "free_floor_gb": 2.0,
        "critical_free_gb": 1.0,
        "csv_cap_mb": 50,
        "csv_keep_rows": 100000,
        "keep_waterfalls": 4000,
        "keep_snippets": 300,
        "snippet_halfwidth_ch": 2000,
        "max_snippet_hits": 5,
        "flood_hits": 200,
        "require_ntp": False,
        "api_bind": "127.0.0.1",   # 0.0.0.0 to let the desktop app reach this node
        "api_port": 8090,
        "api_daily_mb": 0,         # metered site: cap bytes served per day (0 = no cap)
        "api_token": "",           # set a word to let the desktop app request a calibration
                                   # remotely (POST /cal with it); blank = view only
    },
    "hline": {},                   # overrides for aquila.hline.DEFAULTS
    "app": {
        "observe_on_open": False,  # the desktop app starts observing as soon as it opens
    },
    "save": {
        "csv": True,               # hits.csv, rfi.csv, scans.csv
        "plots": True,             # block thumbnails and virgo plots
        "spectra": True,           # virgo .dat scans and hit snippets
        "raw_iq": False,           # raw samples per block: 1 to 3 GB each. Fills a disk in hours.
    },
}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


APP_DIR = "aquila-radio-astronomy"     # ~/aquila-radio-astronomy: config and node data
OLD_APP_DIR = "aquila-seti"             # the folder of installs from before the rename


def home_dir():
    """~/aquila-radio-astronomy, or the old ~/aquila-seti while that still exists and
    the new one does not: an upgraded install keeps its config and data in place."""
    home = os.path.expanduser("~")
    new, old = os.path.join(home, APP_DIR), os.path.join(home, OLD_APP_DIR)
    if not os.path.isdir(new) and os.path.isdir(old):
        return old
    return new


def default_path():
    return os.path.join(home_dir(), "config.json")


def load(path=None):
    path = path or default_path()
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(path):
        try:
            with open(path) as f:
                user = json.load(f)
        except ValueError as e:
            raise RuntimeError(f"config {path} is not valid JSON: {e}") from None
        if not isinstance(user, dict):
            raise RuntimeError(f"config {path} must be a JSON object")
        # a section of the wrong kind ("dish": null) would only fail later, mid-run
        bad = [k for k, v in user.items()
               if isinstance(DEFAULTS.get(k), dict) and not isinstance(v, dict)]
        if bad:
            raise RuntimeError(f"config {path}: {', '.join(bad)} must be a {{...}} section")
        cfg = _merge(cfg, user)
    name = str(cfg.get("name") or "")
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise RuntimeError(f"config {path}: name {name!r} must be a plain name")
    cfg["_state_dir_set"] = bool(cfg["state_dir"])
    if not cfg["state_dir"]:
        cfg["state_dir"] = derived_state_dir(cfg["name"])
    cfg["_path"] = path
    return cfg


def derived_state_dir(name):
    return os.path.join(home_dir(), name)


def rename(cfg, name):
    """Change the node name; the data folder follows unless it was set by hand."""
    name = str(name).replace("/", "-").replace("\\", "-").strip(" .") or "my-dish"
    cfg["name"] = name                   # it names a folder: no path tricks
    if not cfg.get("_state_dir_set"):
        cfg["state_dir"] = derived_state_dir(name)


def save(cfg, path=None):
    path = path or cfg.get("_path") or default_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    out = {k: v for k, v in cfg.items() if not k.startswith("_")}
    if not cfg.get("_state_dir_set"):
        out["state_dir"] = ""
    tmp = path + ".tmp"
    # it holds the control token: owner only, and never a half-written file
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(out, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def pointing(cfg):
    """Fresh dish and site from the config file, so a running node follows a pointing
    change without a restart. Falls back to what it already has."""
    try:
        fresh = load(cfg.get("_path"))
        out = {"dish": fresh["dish"], "site": fresh["site"]}
    except Exception:
        out = {"dish": cfg["dish"], "site": cfg.get("site")}
    from . import rotator
    out["dish"] = rotator.pointing(out["dish"])
    return out


def user_view(cfg):
    """The handful of keys a normal user ever sees."""
    return {k: cfg[k] for k in USER_KEYS}
