"""Frequency switching: the receiver's own shape must cancel and the line come out
at its true height, whatever the passband looks like."""
import os
import time
import warnings

import numpy as np
import pytest

from aquila import hline

F0 = hline.DEFAULTS["f_rest_hz"]


def _sky(f, peak, wing, slope):
    v = 299792.458 * (F0 - f) / F0
    line = peak * np.exp(-0.5 * ((v - 3.0) / 6.0) ** 2) + wing * np.exp(-0.5 * (v / 45.0) ** 2)
    return (1.0 + slope * (f - F0) / 1e6) * (1.0 + line)


def _scan(tmp_path, rate, peak=0.06, wing=0.01, slope=0.02, drift=1.003, noise=2e-4, seed=0):
    rng = np.random.default_rng(seed)
    chans = 1024
    f_sig, f_ref = hline.fs_tunings(hline.DEFAULTS, rate)
    off = (np.arange(chans) - chans / 2.0) * rate / chans
    x = off / rate
    # an ugly receiver: curved, tilted, rippled, with spurs fixed in its own channels
    band = (1 + 0.8 * x ** 2 - 0.4 * x ** 3) * (1 + 0.03 * np.sin(2 * np.pi * x * 7))
    band[[200, 513, 777]] *= [3.0, 9.0, 2.0]
    out = {}
    for name, fc, g in (("sig", f_sig, 1.0), ("ref", f_ref, drift)):
        p = g * band * _sky(fc + off, peak, wing, slope)
        rows = p * (1 + noise * rng.standard_normal((20, chans)))
        path = tmp_path / f"{name}.dat"
        rows.astype(np.float32).tofile(path)
        out[name] = str(path)
    obs = {"frequency": f_sig, "bandwidth": rate, "channels": chans,
           "fs_throw_hz": f_ref - f_sig}
    return out, obs


@pytest.mark.parametrize("rate", [2_400_000, 3_000_000, 5_000_000, 6_000_000, 10_000_000])
def test_line_height_independent_of_receiver(tmp_path, rate):
    files, obs = _scan(tmp_path, rate)
    m = hline.measure_fs(files["sig"], files["ref"], obs, dict(hline.DEFAULTS))
    assert m is not None
    truth = 10 * np.log10(1.06 + 0.01 * np.exp(-0.5 * (3 / 45) ** 2))
    assert m["db"] == pytest.approx(truth, abs=0.01)
    assert m["kms"] == pytest.approx(3.0, abs=1.5)
    assert m["rms_db"] < 0.005


def test_weak_line(tmp_path):
    files, obs = _scan(tmp_path, 6_000_000, peak=0.004, wing=0.0)
    m = hline.measure_fs(files["sig"], files["ref"], obs, dict(hline.DEFAULTS))
    assert m["db"] == pytest.approx(10 * np.log10(1.004), abs=0.002)


def test_outside_carrier_does_not_change_the_line(tmp_path):
    files, obs = _scan(tmp_path, 5_000_000)
    s = np.fromfile(files["sig"], dtype=np.float32).reshape(-1, 1024)
    s[:, 700] *= 1.5                                  # a carrier in the signal tuning only
    s.tofile(files["sig"])
    m = hline.measure_fs(files["sig"], files["ref"], obs, dict(hline.DEFAULTS))
    truth = 10 * np.log10(1.06 + 0.01 * np.exp(-0.5 * (3 / 45) ** 2))
    assert m["db"] == pytest.approx(truth, abs=0.01)
    assert np.nanmax(m["spec_db"][690:710]) < 0.05


def test_fs_scan_end_to_end(tmp_path, monkeypatch):
    """The whole scan: alternating retunes, the settling row dropped, the radio back
    on its own frequency afterwards, and a measured line in the log and the csv."""
    from aquila import config as cfgmod
    from aquila.sdr import Synthetic
    cfg = cfgmod.load(str(tmp_path / "c.json"))
    cfg.update(sdr="synthetic", sample_rate=2_400_000, state_dir=str(tmp_path / "s"))
    cfg["hline"] = {"duration_s": 40, "fs_phase_s": 10}
    cfg["save"]["plots"] = False
    src = Synthetic(cfg)
    src.rate = 2_400_000
    home = src.center_hz
    tuned, logs = [], []
    rng = np.random.default_rng(1)
    chans = 1024
    off = (np.arange(chans) - chans / 2.0) * src.rate / chans
    band = 1 + 0.5 * (off / src.rate) ** 2

    def fake_observe(s, dat, obs, log=print, fine=0, **_):
        tuned.append(s.center_hz)
        n = int(obs["duration"] / obs["t_sample"])
        rows = band * _sky(s.center_hz + off, 0.05, 0.0, 0.0) * (
            1 + 1e-4 * rng.standard_normal((n, chans)))
        rows[0] *= 5.0                                   # junk while the tuner settles
        rows.astype(np.float32).tofile(dat)
        return True
    monkeypatch.setattr(hline, "observe", fake_observe)
    hl = hline.HLine(cfg, source=src, log=logs.append)
    assert hl.scan()
    f_sig, f_ref = hline.fs_tunings(hl.h, src.rate)
    assert tuned == [f_sig, f_ref] * 2
    assert src.center_hz == home
    d = tmp_path / "s" / "hline"
    sig = np.fromfile(next(d.glob("obs_*[0-9].dat")), np.float32).reshape(-1, chans)
    assert sig.shape[0] == 18 and sig.max() < 3.0
    assert any("frequency switched peak" in m for m in logs), logs
    row = open(d / "scans.csv").read().splitlines()[-1].split(",")
    assert float(row[6]) == pytest.approx(10 * np.log10(1.05), abs=0.01)


def _sdr_band(model, off, rate, tuning_index):
    """Passband shapes of the radios the app drives, as measured or as their filters
    behave: what matters is what is fixed in the receiver and what is not."""
    x = off / 1e6
    if model == "rtlsdr":           # steep tuner IF filter edges, mild ripple
        edge = 1.05 * rate / 2.4e6
        band = (1 + 0.02 * np.sin(x * 9)) / (1 + (np.abs(x) / edge) ** 16)
    elif model == "airspy_mini":    # +-0.7 dB ripple, a bump before the upper edge
        band = 10 ** ((0.7 * np.sin(x * 3.1) + 1.5 * np.exp(-((x - 0.8) / 0.08) ** 2)) / 10)
        band /= 1 + ((x + 0.05) / 0.98) ** 20
    elif model == "hydrasdr":       # gentle shoulder below -1.3 MHz where the image leaks
        band = 1 / (1 + (np.abs(x) / 2.2) ** 12)
        sh = x < -1.3
        band[sh] *= 10 ** (-1.3 / 10) * (1 + 0.1 * tuning_index)   # differs per tuning
    elif model == "hackrf":         # baseband filter at ~0.75 of the rate
        band = 1 / (1 + (np.abs(x) / (0.75 * rate / 2e6)) ** 10)
    band = band.copy()
    dcamp = {"rtlsdr": 4.0, "hackrf": 20.0}.get(model, 1.5)
    band[len(band) // 2] *= dcamp * (1 + 0.6 * tuning_index)       # DC: not the same twice
    return band


@pytest.mark.parametrize("model,rate", [("rtlsdr", 2_400_000), ("rtlsdr", 3_200_000),
                                        ("airspy_mini", 3_000_000), ("airspy_mini", 6_000_000),
                                        ("hydrasdr", 5_000_000), ("hydrasdr", 10_000_000),
                                        ("hackrf", 5_000_000), ("hackrf", 10_000_000),
                                        ("hackrf", 20_000_000)])
def test_each_radio(tmp_path, model, rate):
    rng = np.random.default_rng(3)
    chans = 1024
    f_sig, f_ref = hline.fs_tunings(hline.DEFAULTS, rate)
    off = (np.arange(chans) - chans / 2.0) * rate / chans
    files = {}
    for k, (name, fc, g) in enumerate((("sig", f_sig, 1.0), ("ref", f_ref, 1.002))):
        p = g * _sdr_band(model, off, rate, k) * _sky(fc + off, 0.05, 0.008, 0.02)
        rows = p * (1 + 3e-4 * rng.standard_normal((20, chans)))
        rows.astype(np.float32).tofile(tmp_path / f"{name}.dat")
        files[name] = str(tmp_path / f"{name}.dat")
    obs = {"frequency": f_sig, "bandwidth": rate, "channels": chans}
    m = hline.measure_fs(files["sig"], files["ref"], obs, dict(hline.DEFAULTS))
    assert m is not None
    truth = 10 * np.log10(1.05 + 0.008 * np.exp(-0.5 * (3 / 45) ** 2))
    tol = 0.02 if rate >= 20_000_000 else 0.01       # 20 kHz channels round off a 14 km/s line
    assert m["db"] == pytest.approx(truth, abs=tol)
    assert m["kms"] == pytest.approx(3.0, abs=2.0)
    # nothing left at the DC channel (-53 km/s) or at the band edges
    ok = ~np.isnan(m["spec_db"])
    far = ok & (np.abs(m["v"] - 3.0) > 120)
    assert np.abs(m["spec_db"][far]).max() < 0.01
    dcv = np.abs(m["v"] + 52.8) < 2
    sky = 1 + 0.008 * np.exp(-0.5 * (55.8 / 45) ** 2) + 0.05 * np.exp(-0.5 * (55.8 / 6) ** 2)
    assert np.abs(m["spec_db"][dcv] - 10 * np.log10(sky)).max() < 0.01


def test_old_switching_off_setting_is_ignored(tmp_path, monkeypatch):
    """Frequency switching is the only mode: a config from before 0.2.16 that turned it
    off still switches, and the status says so."""
    import json

    from aquila import config as cfgmod
    from aquila.sdr import Synthetic
    cfg = cfgmod.load(str(tmp_path / "c.json"))
    cfg.update(sdr="synthetic", sample_rate=2_400_000, state_dir=str(tmp_path / "s"))
    cfg["hline"] = {"freq_switch": False}
    node = hline.HLine(cfg, source=Synthetic(cfg), log=lambda m: None)
    monkeypatch.setattr(node, "fs_scan", lambda: "switched")
    assert node.scan() == "switched"
    node.status("capturing")
    st = json.load(open(tmp_path / "s" / "status.json"))
    assert st["calibration"] == "frequency switching"


class _PulsedRadio:
    """Noise, with an optional burst 100 times stronger in a 5 ms frame, on for 3.7 ms."""
    rate = 5_000_000

    def __init__(self, pulsed, seed=1):
        self.pulsed, self.rng, self.t = pulsed, np.random.default_rng(seed), 0

    def start(self, nsamples, frame):
        pass

    def frames(self, n):
        while True:
            z = self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n)
            if self.pulsed:
                on = ((self.t + np.arange(n)) % 25000) < 18500
                z = z * np.where(on, 10.0, 1.0)
            self.t += n
            yield z

    def to_complex(self, z):
        return z

    def finish(self):
        return ""


def _row_level(tmp_path, pulsed, blank):
    obs = {"channels": 1024, "duration": 2, "t_sample": 1.0, "bandwidth": 5_000_000}
    dat = str(tmp_path / "x.dat")
    kept = []
    assert hline.observe(_PulsedRadio(pulsed), dat, obs, blank=blank, kept=kept)
    return float(np.median(np.fromfile(dat, dtype=np.float32))), kept


def test_blanking_recovers_the_quiet_level(tmp_path):
    clean, kept = _row_level(tmp_path, False, True)
    assert min(kept) > 0.99                         # no pulses: nothing is thrown away
    gated, kept = _row_level(tmp_path, True, True)
    assert 0.15 < np.median(kept) < 0.27            # the gaps, less the frames on their edges
    assert abs(10 * np.log10(gated / clean)) < 0.1  # back at the true sky level
    plain, _ = _row_level(tmp_path, True, False)
    assert 10 * np.log10(plain / clean) > 15


def test_blanking_keeps_the_scan_going_when_every_frame_is_loud(tmp_path):
    frames = np.ones((100, 64), complex) * np.linspace(1, 3, 100)[:, None]
    assert hline.quiet_frames(frames).sum() >= 5


def test_plot_smoothing_changes_only_the_drawing(tmp_path, monkeypatch):
    plt = pytest.importorskip("matplotlib.pyplot")
    v = np.linspace(200, -300, 500)
    spec = np.exp(-0.5 * (v / 10.0) ** 2) + 0.02 * np.random.default_rng(0).standard_normal(500)
    m = {"v": v, "spec_db": spec.copy(), "db": float(spec.max()), "kms": 0.0, "rms_db": 0.02}
    drawn = []
    real = plt.Axes.plot

    def spy(self, x, y, *a, **k):
        drawn.append(np.array(y))
        return real(self, x, y, *a, **k)
    monkeypatch.setattr(plt.Axes, "plot", spy)
    hline.plot_db(m, str(tmp_path / "off.png"))
    hline.plot_db(m, str(tmp_path / "on.png"), smooth_kms=2.0)
    assert (tmp_path / "on.png").stat().st_size > 0
    off, on = drawn[0], drawn[2]
    assert np.array_equal(off, spec)
    flat = v < -100                                 # away from the line, only noise moves
    assert np.std(np.diff(on[flat])) < 0.3 * np.std(np.diff(off[flat]))
    assert np.array_equal(m["spec_db"], spec)      # the measurement is untouched
    assert drawn[3][0] == m["db"]                  # the marked peak stays as measured


@pytest.mark.parametrize("rate", [2_400_000, 3_000_000, 5_000_000, 6_000_000, 10_000_000])
def test_reference_band_stays_clear_of_the_gas(rate):
    f_sig, f_ref = hline.fs_tunings(hline.DEFAULTS, rate)
    lowest_seen = f_ref - min(0.344 * rate, 1.6e6)
    # 150 km/s of gas on the receding-negative side stays out of the reference
    assert lowest_seen > F0 * (1 + 150.0 / 299792.458)


def test_one_sided_baseline_is_not_used(tmp_path):
    files, obs = _scan(tmp_path, 5_000_000)
    h = dict(hline.DEFAULTS, line_window_kms=900)
    wide = hline.measure_fs(files["sig"], files["ref"], obs, h)
    assert wide is None or wide["line_kms"][0] < 400


def test_quiet_rows_drops_a_raised_floor_only():
    from aquila import hline
    rng = np.random.default_rng(3)
    rows = 1 + 0.001 * rng.normal(size=(145, 64))
    assert hline.quiet_rows(rows).sum() == 145
    rows[20:50] *= 1.6
    keep = hline.quiet_rows(rows)
    assert not keep[20:50].any() and keep[50:].all()
    assert hline.quiet_rows(1.6 * rows[:5]).all()


def test_map3d_history_lines_up_and_draws(tmp_path):
    from aquila import map3d
    d = str(tmp_path)
    assert map3d.render(d) is False
    v = np.linspace(-250, 250, 1024)
    t0 = 1_700_000_000.0
    for i in range(30):
        db = 0.5 * np.exp(-0.5 * ((v + 15) / 10) ** 2)
        db[:5] = np.nan
        map3d.append(d, t0 + i * 3600, v, db)
    rows = map3d.load(d)
    assert rows.size == 30
    k = np.argmin(np.abs(map3d.V_MID + 15))
    assert abs(rows["spec"][0][k] - 0.5) < 0.01
    assert np.isnan(rows["spec"][0][np.argmin(np.abs(map3d.V_MID + 280))])
    with open(os.path.join(d, map3d.HISTORY), "ab") as f:
        f.write(b"\0" * 10)                    # a row still being written
    assert map3d.load(d).size == 30
    map3d.append(d, t0 + 30 * 3600, v, np.where(np.isnan(db), np.nan, 2 * db))
    rows = map3d.load(d)
    assert rows.size == 31 and rows["t"][-1] == t0 + 30 * 3600  # still lined up after the crash
    assert abs(rows["spec"][-1][k] - 1.0) < 0.02
    rows = rows[:-1]
    rows.tofile(os.path.join(d, map3d.HISTORY))
    assert map3d.render(d, "t", now=t0 + 30 * 3600) is True
    assert os.path.getsize(os.path.join(d, map3d.PNG)) > 10000
    map3d.trim(d, 1, now=t0 + 29 * 3600)
    assert 20 <= map3d.load(d).size <= 26


def test_map3d_gap_stays_a_gap():
    from aquila import map3d
    rows = np.zeros(40, map3d.ROW)
    t0 = 1_700_000_000.0
    rows["t"] = t0 + np.r_[np.arange(20) * 900.0, 86400 + np.arange(20) * 900.0]
    rows["spec"] = 0.1
    t, v, z, n = map3d.grid(rows, 7, now=t0 + 2 * 86400)
    assert n == 40 and np.isnan(z).all(axis=1).any() and np.isfinite(z).all(axis=1).any()
    empty = np.isnan(z).all(axis=1)
    assert not empty[t < t0 + 15000].any() and not empty[t > t0 + 86400 + 900].any()
    assert empty[(t > t0 + 20000) & (t < t0 + 80000)].all()


def test_map3d_ignores_bad_clocks():
    from aquila import map3d
    rows = np.zeros(10, map3d.ROW)
    t0 = 1_700_000_000.0
    rows["t"] = t0 + np.arange(10) * 900.0
    rows["t"][0] = 86400.0                     # a Pi before its clock is set
    rows["t"][1] = t0 + 500 * 86400            # a wrong clock in the future
    rows["spec"] = 0.1
    t, v, z, n = map3d.grid(rows, 7, now=t0 + 9000)
    assert n == 8 and t[-1] - t[0] < 7200


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs tzset")
@pytest.mark.parametrize("hours", [1, 3, 20, 48, 72, 168, 700])
def test_map_draws_cleanly_at_any_span_and_across_a_clock_change(tmp_path, monkeypatch, hours):
    from aquila import map3d
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        now = 1793887923.0  # 5 Nov 2026, four days after clocks went back there
        v = np.arange(-300, 300, 1.0)
        db = np.exp(-(((v + 17) / 20) ** 2))
        for t in np.arange(now - hours * 3600, now, 900.0):
            map3d.append(str(tmp_path), t, v, db)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert map3d.render(str(tmp_path), "x", days=30, now=now)
    finally:
        monkeypatch.undo()
        time.tzset()


def test_map_first_scans_fill_the_plot(tmp_path):
    from aquila import map3d
    now = 1793887923.0
    v = np.arange(-300, 300, 1.0)
    for i in range(3):
        map3d.append(str(tmp_path), now - 450 + i * 215, v, np.exp(-((v / 20) ** 2)))
    t, _, z, n = map3d.grid(map3d.load(str(tmp_path)), 7, now)
    assert n == 3 and len(t) >= 2 and np.isfinite(z).any(axis=1).all()
    assert t[-1] <= now + 300
    assert map3d.render(str(tmp_path), "x", now=now)


def test_search_frequency_switches_by_default_only_on_the_line():
    """The search frequency switches, except when its band does not hold the hydrogen
    line; the old freq_switch setting no longer turns it off."""
    from aquila.hline import fs_search_blocks
    cfg = {"center_mhz": 1420.0, "hline": {}}
    assert fs_search_blocks(cfg, 3e6) == 3
    assert fs_search_blocks({**cfg, "center_mhz": 1420.405}, 2.4e6) == 3
    assert fs_search_blocks({**cfg, "hline": {"freq_switch": False}}, 3e6) == 3
    assert fs_search_blocks({**cfg, "hline": {"fs_search_blocks": 0}}, 3e6) == 0
    assert fs_search_blocks({**cfg, "center_mhz": 1500.0}, 3e6) == 0
    assert fs_search_blocks({**cfg, "center_mhz": 11000.0}, 10e6) == 0
    assert fs_search_blocks(cfg, 250e3) == 0        # line outside so narrow a band
