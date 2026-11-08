# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# GPL-3.0-or-later: free to use and modify, derived works must stay open.
"""Positive control: a drifting tone in noise must come out as one hit at the right
frequency and drift, and a survey block on the synthetic source must record it."""
import json
import os
import tempfile

import numpy as np

from aquila import config, dsp
from aquila.sdr import Synthetic
from aquila.survey import Survey


def small_cfg(tmp):
    cfg = config.load(os.path.join(tmp, "config.json"))
    cfg["name"] = "test"
    cfg["sdr"] = "synthetic"
    cfg["sample_rate"] = 250_000
    cfg["state_dir"] = os.path.join(tmp, "state")
    adv = cfg["advanced"]
    adv.update({"n_chan": 1 << 15, "rows": 20, "dwell_s": 12, "settle_skip_s": 0.5,
                "max_drift_hz_s": 12.0, "snr_thresh": 15.0, "seti_min_chan": 1024})
    return cfg


def test_drift_search_finds_tone():
    tmp = tempfile.mkdtemp()
    cfg = small_cfg(tmp)
    src = Synthetic(cfg, tone_offset_hz=5000.0, drift_hz_s=6.0, tone_amp=0.15)
    adv = cfg["advanced"]
    wf, chan_bw, row_s = dsp.channelize(src, adv["n_chan"], adv["rows"], adv["dwell_s"],
                                        adv["settle_skip_s"], log=lambda m: None)
    assert wf is not None
    dsp.normalize(wf)
    blank = dsp.blank_set(adv["n_chan"], chan_bw, adv["dc_notch_hz"], [])
    hits = dsp.cluster_hits(dsp.drift_search(wf, chan_bw, row_s, adv["max_drift_hz_s"],
                                             adv["snr_thresh"], blank, log=lambda m: None))
    assert len(hits) >= 1
    best = max(hits, key=lambda h: h["snr"])
    f_off = (best["chan"] - adv["n_chan"] / 2) * chan_bw
    assert abs(f_off - 5000.0) < 12 * chan_bw
    assert abs(best["drift_hz_s"] - 6.0) < 0.4
    assert best["width_hz"] < 6 * chan_bw
    assert best["coverage"] > adv["rows"] * 0.5


def test_survey_block_records_hit():
    tmp = tempfile.mkdtemp()
    cfg = small_cfg(tmp)
    src = Synthetic(cfg, tone_offset_hz=-8000.0, drift_hz_s=6.0, tone_amp=0.04)
    s = Survey(cfg, source=src)
    assert s.one_block()
    st = s.store
    assert os.path.exists(st.hits)
    rows = open(st.hits).read().strip().splitlines()
    assert len(rows) >= 2
    sky = float(rows[1].split(",")[2])
    assert abs(sky - (src.center_hz - 8000.0)) < 120
    assert os.path.exists(st.latest)
    d = json.load(open(st.status))
    assert d["state"] == "idle" and d["blocks"] == 1
    assert os.listdir(st.snip_dir)


def test_noise_only_is_quiet():
    tmp = tempfile.mkdtemp()
    cfg = small_cfg(tmp)
    src = Synthetic(cfg, tone_amp=0.0)
    adv = cfg["advanced"]
    wf, chan_bw, row_s = dsp.channelize(src, adv["n_chan"], adv["rows"], adv["dwell_s"],
                                        adv["settle_skip_s"], log=lambda m: None)
    dsp.normalize(wf)
    blank = dsp.blank_set(adv["n_chan"], chan_bw, adv["dc_notch_hz"], [])
    hits = dsp.drift_search(wf, chan_bw, row_s, adv["max_drift_hz_s"], adv["snr_thresh"],
                            blank, log=lambda m: None)
    assert len(hits) <= 2


def _track(wf, ch0, dch, amp):
    rows = wf.shape[0]
    for r in range(rows):
        wf[r, ch0 + int(round(r * dch / (rows - 1)))] += amp


def test_strong_carrier_does_not_hide_its_neighbour():
    # the SNR baseline must not include the signals themselves: a carrier 1000x the
    # noise next to a weak track used to cut the weak one's SNR and cap its own
    import numpy as np
    rng = np.random.default_rng(3)
    rows, nch = 20, 1 << 14
    noise = rng.chisquare(20, size=(rows, nch)).astype(np.float32) / 20

    def best_near(wf, ch):
        w = wf.copy()
        dsp.normalize(w)
        hits = dsp.drift_search(w, 1.0, 1.0, 3.0, 8.0, log=lambda m: None)
        near = [h for h in hits if abs(h["chan"] - ch) <= 3]
        return max(h["snr"] for h in near) if near else 0.0

    weak = noise.copy()
    _track(weak, 8000, 20, 2.0)
    alone = best_near(weak, 8000)
    both = weak.copy()
    _track(both, 8140, -20, 300.0)   # inside the 301-channel baseline, past drift reach
    beside = best_near(both, 8000)
    assert alone > 15
    assert beside > 0.85 * alone
    strong = noise.copy()
    _track(strong, 8140, -20, 300.0)
    loud = best_near(strong, 8140)
    assert loud > 0.6 * 150 * alone   # 150x the power reads close to 150x the SNR


def test_strong_track_leaves_no_ghosts():
    # every drift line that crosses a strong track picks up one row of it; past the
    # cluster gap those crossings used to be reported as candidates of their own
    import numpy as np
    rng = np.random.default_rng(5)
    rows, nch = 30, 1 << 14
    wf = rng.chisquare(20, size=(rows, nch)).astype(np.float32) / 20
    _track(wf, 8000, 6, 60.0)
    _track(wf, 4000, -15, 1.0)          # a weak real one, far away
    _track(wf, 12000, 25, 6.0)          # two of similar strength that cross
    _track(wf, 12020, -25, 6.0)
    dsp.normalize(wf)
    hits = dsp.drift_search(wf, 1.0, 1.0, 1.5, 8.0, log=lambda m: None)
    hits = dsp.cluster_hits(hits, gap=10)
    near = [h["chan"] for h in hits if 7900 < h["chan"] < 8100]
    assert len(near) == 1 and abs(near[0] - 8000) <= 3, near
    assert any(abs(h["chan"] - 4000) <= 3 for h in hits)
    assert any(abs(h["chan"] - 12000) <= 3 for h in hits)
    assert any(abs(h["chan"] - 12020) <= 3 for h in hits)


def test_two_block_reference_keeps_a_half_block_burst_and_kills_carriers():
    """A 72 s burst with no drift (the Wow! event) fills half a 150 s block, so dividing
    by this block's median alone erases it. With the previous block's median in the
    reference it survives, while a steady or flickering carrier still divides to 1."""
    rng = np.random.default_rng(7)
    rows, nch, navg = 30, 4096, 24
    row_s = 5.0
    t = (np.arange(rows) + 0.5) * row_s

    def block():
        return rng.chisquare(2 * navg, (rows, nch)).astype(np.float32) / (2 * navg)

    def best(wf, ch):
        hits = dsp.drift_search(wf, 4.768, row_s, 4.0, 25.0, log=lambda m: None)
        return max((h["snr"] for h in hits if abs(h["chan"] - ch) < 8), default=0.0)

    prev, cur = block(), block()
    cur[:, 2048] += 8.0 * np.exp(-0.5 * ((t - 40.0) / 22.0) ** 2)     # the burst
    flick = 20.0 * np.exp(rng.normal(0, 0.8, rows))                    # a flickering carrier
    prev[:, 1000] += 20.0 * np.exp(rng.normal(0, 0.8, rows))
    cur[:, 1000] += flick
    prev[:, 3000] += 20.0                                              # a steady carrier
    cur[:, 3000] += 20.0
    alone = cur.copy()
    dsp.normalize(alone)
    assert best(alone, 2048) < 25.0            # the shipped single-block median misses it
    ref = dsp.normalize(prev)
    med = dsp.normalize(cur, ref)
    assert best(cur, 2048) > 30.0
    assert best(cur, 1000) == 0.0 and best(cur, 3000) == 0.0
    assert med.shape == (nch,)                 # this block's median is handed on

    nxt = block()
    dsp.normalize(nxt, np.ones(nch + 1, dtype=np.float32))    # a mismatched reference is ignored
    assert abs(float(np.median(nxt)) - 1.0) < 0.05


def test_flat_full_block_line_is_marked_as_a_carrier_not_a_burst():
    """With the two-block reference a carrier that rose since the previous block divides
    to a level above 1 in every row. That is a carrier, flagged flat; the Wow burst in the
    same block is not."""
    rng = np.random.default_rng(11)
    rows, nch, navg, row_s = 30, 4096, 24, 5.0
    t = (np.arange(rows) + 0.5) * row_s
    prev, cur = (rng.chisquare(2 * navg, (rows, nch)).astype(np.float32) / (2 * navg)
                 for _ in range(2))
    prev[:, 1000] += 2.0
    cur[:, 1000] += 20.0                                     # rose tenfold since the last block
    cur[:, 2048] += 8.0 * np.exp(-0.5 * ((t - 40.0) / 22.0) ** 2)
    ref = dsp.normalize(prev)
    dsp.normalize(cur, ref)
    hits = {h["chan"]: h for h in dsp.drift_search(cur, 4.768, row_s, 4.0, 25.0,
                                                   log=lambda m: None)}
    line = [h for c, h in hits.items() if abs(c - 1000) < 4]
    burst = [h for c, h in hits.items() if abs(c - 2048) < 4]
    assert line and line[0]["flat"]
    assert burst and not burst[0]["flat"]
