"""Frequency switching: the receiver's own shape must cancel and the line come out
at its true height, whatever the passband looks like."""
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

    def fake_observe(s, dat, obs, log=print, fine=0):
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
