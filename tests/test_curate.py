# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# MIT licence: free for anyone to use and modify.
"""The curator must drop a switching-supply comb, keep a real transit as tier A, and
never promote a persistent carrier."""
import os
import tempfile
import time

import numpy as np

from aquila import curate


def make_hits(path):
    rng = np.random.default_rng(1)
    t0 = 1_790_000_000
    rows = []
    period, phase = 42672.5, 3555.0
    for b in range(400):
        t = t0 + b * 155
        utc = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(t))
        local = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t))
        # comb: 12 lines every block, each within 300 Hz of its harmonic, wandering slowly
        for n in rng.choice(np.arange(-20, 20), 12, replace=False):
            f = 1420e6 + phase + n * period + rng.normal(0, 300) + b * 2.0
            rows.append((utc, local, f, rng.normal(0, 0.3), 40 + rng.random() * 50, 2.9, 20,
                         "x.png"))
        # a persistent carrier all day
        rows.append((utc, local, 1420.0081e6 + rng.normal(0, 20), 0.01, 80, 8.6, 22, "x.png"))
        # a real transit: 9 blocks, rising and falling, drifting 0.15 Hz/s vs the LO
        if 200 <= b < 209:
            k = b - 200
            snr = 25 + 30 * np.sin(np.pi * (k + 0.5) / 9)
            f = 1420.3517e6 + 0.25 * (b - 200) * 155
            rows.append((utc, local, f, 0.25, snr, 4.8, 24, "x.png"))
    with open(path, "w") as fh:
        fh.write("utc,local,sky_freq_hz,drift_hz_s,snr,width_hz,coverage,thumb,sdr\n")
        for r in rows:
            fh.write("{},{},{:.0f},{:.4f},{:.2f},{:.1f},{:.1f},{},airspy\n".format(*r))


def test_comb_cut_and_transit_kept():
    d = tempfile.mkdtemp()
    make_hits(os.path.join(d, "hits.csv"))
    s = curate.run(d, beam_deg=6.0, center_hz=1420e6, comb_window=(10e3, 100e3))
    assert s["comb_source"] == "fit"
    assert abs(s["comb_period_hz"] - 42672.5) < 5
    assert s["excluded_comb"] > 4000
    assert s["tierA"] >= 1
    cand = open(os.path.join(d, "candidates.csv")).read().splitlines()
    a_rows = [r for r in cand if r.startswith("A,")]
    assert all(abs(float(r.split(",")[3]) - 1420.3517e6) < 2000 for r in a_rows)
    assert not any(abs(float(r.split(",")[3]) - 1420.0081e6) < 1000 for r in cand[1:])
    assert os.path.exists(os.path.join(d, "comb_history.csv"))
    assert os.path.exists(os.path.join(d, "candidates_oncomb.csv"))


def test_clean_list_invents_no_comb():
    d = tempfile.mkdtemp()
    rng = np.random.default_rng(2)
    with open(os.path.join(d, "hits.csv"), "w") as fh:
        fh.write("utc,local,sky_freq_hz,drift_hz_s,snr,width_hz,coverage,thumb,sdr\n")
        for b in range(300):
            utc = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(1_790_000_000 + b * 155))
            for _ in range(3):
                fh.write(f"{utc},x,{1420e6 + rng.uniform(-1.2e6, 1.2e6):.0f},"
                         f"{rng.normal(0, 1):.4f},30,4.8,5,x.png,airspy\n")
    s = curate.run(d, beam_deg=6.0)
    assert s["comb_source"] == "none" and s["excluded_comb"] == 0


def test_few_carriers_do_not_make_a_comb():
    """A quiet node with a handful of fixed carriers must not get a false comb."""
    d = tempfile.mkdtemp()
    rng = np.random.default_rng(5)
    lines = 1420e6 + np.array([-900e3, -412e3, 8.1e3, 377e3, 690e3, 1.05e6])
    with open(os.path.join(d, "hits.csv"), "w") as fh:
        fh.write("utc,local,sky_freq_hz,drift_hz_s,snr,width_hz,coverage,thumb,sdr\n")
        for b in range(400):
            utc = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(1_790_000_000 + b * 155))
            for f in lines:
                fh.write(f"{utc},x,{f + rng.normal(0, 20):.0f},0.0100,60,4.8,25,x.png,airspy\n")
            if b % 4 == 0:
                fh.write(f"{utc},x,{1420e6 + rng.uniform(-1.2e6, 1.2e6):.0f},"
                         f"{rng.normal(0, 1):.4f},28,4.8,5,x.png,airspy\n")
    s = curate.run(d, beam_deg=6.0)
    assert s["comb_source"] == "none" and s["excluded_comb"] == 0
    assert s["oncomb_review"] == 0


def test_lo_drift_from_fixed_lines_not_from_neighbours():
    """Lines fixed in frequency fit a coarse track at any drift up to ~0.08 Hz/s, so the
    shared receiver drift must come from each track's best fit. A strong sky transit at
    +0.16 Hz/s next to a +0.05 one must not drag it up and lose the transit as LO drift."""
    d = tempfile.mkdtemp()
    rng = np.random.default_rng(3)
    t0, dt = 1_790_000_000, 185
    fwhm = 4200.0
    with open(os.path.join(d, "hits.csv"), "w") as fh:
        fh.write("utc,local,sky_freq_hz,drift_hz_s,snr,width_hz,coverage,thumb,sdr\n")
        for b in range(50):
            t = b * dt
            utc = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(t0 + t))
            for f in (-9e3, -3e3, 792e3):
                fh.write(f"{utc},x,{1420e6 + f + rng.normal(0, 3):.0f},0.0000,90,2.3,9,x,rtl\n")
            g = np.exp(-4 * np.log(2) * ((t - 25 * dt) / fwhm) ** 2)
            for f0, drift in ((-611e3, 0.16), (512e3, 0.05), (-244e3, 0.20)):
                if g > 0.15 or f0 == -244e3:
                    snr = 30 + 60 * g
                    fh.write(f"{utc},x,{1420e6 + f0 + drift * t:.0f},{drift:.4f},{snr:.2f},"
                             "2.3,26,x,rtl\n")
    s = curate.run(d, beam_deg=16.4)
    assert abs(s["lo_common_drift_hz_s"]) < 0.015
    cand = open(os.path.join(d, "candidates.csv")).read().splitlines()[1:]
    a = [r for r in cand if r.startswith("A,")]
    assert a and all(abs(float(r.split(",")[3]) - 1420e6 + 611e3) < 3000 for r in a)


def test_fast_drifting_transit_reaches_tier_a():
    """The block-to-block search covers +-0.4 Hz/s on its grid; a transit drifting faster
    is still detected in every block and must still be tracked and promoted."""
    d = tempfile.mkdtemp()
    rng = np.random.default_rng(4)
    t0, dt = 1_790_000_000, 175
    with open(os.path.join(d, "hits.csv"), "w") as fh:
        fh.write("utc,local,sky_freq_hz,drift_hz_s,snr,width_hz,coverage,thumb,sdr\n")
        for b in range(50):
            t = b * dt
            utc = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(t0 + t))
            g = np.exp(-4 * np.log(2) * ((t - 25 * dt) / 4200.0) ** 2)
            for f0, drift in ((-455e3, 0.82), (212e3, -1.6)):
                if g > 0.15:
                    fh.write(f"{utc},x,{1420e6 + f0 + drift * t:.0f},"
                             f"{drift + rng.normal(0, 0.01):.4f},{30 + 60 * g:.2f},2.3,26,x,rtl\n")
            for _ in range(4):               # unrelated noise hits at random drifts
                fh.write(f"{utc},x,{1420e6 + rng.uniform(-1e6, 1e6):.0f},"
                         f"{rng.uniform(-4, 4):.4f},27,2.3,5,x,rtl\n")
    s = curate.run(d, beam_deg=16.4)
    cand = open(os.path.join(d, "candidates.csv")).read().splitlines()[1:]
    a = [float(r.split(",")[3]) - 1420e6 for r in cand if r.startswith("A,")]
    assert s["tierA"] >= 10
    assert any(f < -400e3 for f in a) and any(f > 150e3 for f in a)
    assert all(f < -400e3 or 150e3 < f < 260e3 for f in a)


def _write(path, rows):
    with open(path, "w") as fh:
        fh.write("utc,local,sky_freq_hz,drift_hz_s,snr,width_hz,coverage,thumb,sdr\n")
        for utc, f, drift, snr in rows:
            fh.write(f"{utc},x,{f:.0f},{drift:.4f},{snr:.2f},2.3,26,x,rtl\n")


def _transit(rows, t0, dt, nb, f0, drift, centre, fwhm=4200.0, quant=0.0):
    for b in range(nb):
        t = b * dt
        g = np.exp(-4 * np.log(2) * ((t - centre) / fwhm) ** 2)
        if g > 0.15:
            d = round(drift / quant) * quant if quant else drift
            rows.append((time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(t0 + t)),
                         1420e6 + f0 + drift * t, d, 30 + 60 * g))


def test_lone_transit_on_quiet_node_is_not_taken_as_receiver_drift():
    """With nothing else on the band, the one track there must not set the receiver drift."""
    d = tempfile.mkdtemp()
    rows = []
    _transit(rows, 1_790_000_000, 175, 60, 300e3, 0.18, 30 * 175)
    _write(os.path.join(d, "hits.csv"), rows)
    s = curate.run(d, beam_deg=16.4)
    assert s["lo_common_drift_hz_s"] == 0.0 and s["tierA"] >= 10


def test_transit_is_not_made_persistent_by_a_later_track():
    """An unrelated track hours later that lines up with a transit's track key must not
    stretch the transit into a day-long persistent signal."""
    d = tempfile.mkdtemp()
    rows = []
    t0, dt = 1_790_000_000, 175
    _transit(rows, t0, dt, 60, 300e3, 0.18, 30 * 175)
    # 200 blocks on, at the same f - D t
    _transit(rows, t0 + 200 * dt, dt, 60, 300e3 + 0.18 * 200 * dt, 0.18, 30 * 175)
    _write(os.path.join(d, "hits.csv"), rows)
    s = curate.run(d, beam_deg=16.4)
    assert s["excluded_persistent"] == 0 and s["tierA"] >= 20


def test_fast_track_with_coarse_drift_steps_stays_whole():
    """A 5 Msps receiver measures in-block drift in 0.034 Hz/s steps."""
    d = tempfile.mkdtemp()
    rows = []
    _transit(rows, 1_790_000_000, 175, 60, -200e3, 1.23, 30 * 175, quant=0.0341)
    _write(os.path.join(d, "hits.csv"), rows)
    s = curate.run(d, beam_deg=16.4)
    assert s["tierA"] == len(rows)


def test_transit_with_sidebands_is_not_taken_as_receiver_drift():
    """A carrier with sidebands is several tracks at one drift, all at once."""
    d = tempfile.mkdtemp()
    rows = []
    for off in (-1000, 0, 1000):
        _transit(rows, 1_790_000_000, 175, 60, 300e3 + off, 0.18, 30 * 175)
    _write(os.path.join(d, "hits.csv"), rows)
    s = curate.run(d, beam_deg=16.4)
    assert s["lo_common_drift_hz_s"] == 0.0 and s["tierA"] == len(rows)


def test_torn_rows_are_skipped():
    """A power cut can leave a short last row, or glue the next row onto a torn one."""
    d = tempfile.mkdtemp()
    rows = []
    _transit(rows, 1_790_000_000, 175, 60, 300e3, 0.18, 30 * 175)
    p = os.path.join(d, "hits.csv")
    _write(p, rows)
    with open(p, "a") as fh:
        fh.write("20260924T11" + "20260924T100000Z,x,1420300100,0.18,40,2.3,26,x,rtl\n")
        fh.write("20260924T120000Z,x,nan,0.0,30,4,20,x,rtl\n")
        fh.write("20260924T120000Z,x,1420100000,0.01\n")
    s = curate.run(d, beam_deg=16.4)
    assert s["excluded_persistent"] == 0 and s["tierA"] == len(rows)


def test_persistence_does_not_reset_at_utc_midnight():
    """A ground carrier keyed twice in one bin, 100 min apart, either side of 00:00 UTC."""
    import calendar
    d = tempfile.mkdtemp()
    base = calendar.timegm((2026, 10, 9, 22, 50, 0))
    rows = []
    for start, f0 in ((base, 1420.4e6 + 100), (base + 6000, 1420.4e6 + 500)):
        for k in range(8):
            rows.append((time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(start + k * 215)),
                         f0 + 0.2 * k * 215, 0.2, 25 + 30 * np.sin(np.pi * (k + 0.5) / 8)))
    _write(os.path.join(d, "hits.csv"), rows)
    s = curate.run(d, beam_deg=6.0)
    assert s["tierA"] == 0 and s["excluded_persistent"] == len(rows)


def test_history_rows_without_center_column():
    """A history written before the center column existed reads back with None in
    that field; the fallback must skip or accept such rows, never crash."""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "comb_history.csv")
    now = time.time()
    with open(p, "w") as f:
        f.write("run_epoch,run_utc,period,phase,R,source,n_oncomb,n_lines,center\n")
        f.write(f"{now - 60:.0f},x,42672.500,10.00,0.99000,fit,100,20\n")
    got = curate.load_last_good_comb(p, now, 1420.6e6, (10e3, 100e3))
    assert got is not None and abs(got[0] - 42672.5) < 1e-6
