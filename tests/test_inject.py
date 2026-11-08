import time

import numpy as np

from aquila import config, dsp, sdr


def test_injected_tone_lands_in_its_channel_and_drifts():
    rate, nch = 250_000, 1 << 16
    cfg = config._merge(config.DEFAULTS, {
        "sdr": "synthetic", "sample_rate": rate, "center_mhz": 1420.4,
        "advanced": {"n_chan": nch, "rows": 30, "dwell_s": 60, "inject": {
            "sky_hz": 1420.45e6, "drift_hz_s": 2.0, "amp": 0.05,
            "peak_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "width_s": 1e9}}})
    src = sdr.Synthetic(cfg, tone_amp=0.0)
    wf, bw, row_s = dsp.channelize(src, nch, 30, 60, 0.5, log=lambda *a: None)
    k = int(nch / 2 + 50e3 / bw)
    first = wf[0, k - 60:k + 60].argmax() - 60
    last = wf[-1, k - 60:k + 60].argmax() - 60
    assert abs(first) <= 1                                  # the sky frequency maps to the channel
    assert 0.7 < (last - first) * bw / (29 * row_s) / 2.0 < 1.3   # and it drifts at the set rate
    dsp.normalize(wf)
    hits = dsp.drift_search(wf, bw, row_s, 4.0, 25.0, log=lambda *a: None)
    assert any(abs(h["chan"] - k) <= 2 and abs(h["drift_hz_s"] - 2.0) < 0.3 for h in hits)


def test_injector_is_silent_far_from_its_peak():
    from aquila.inject import Injector
    inj = Injector({"sky_hz": 1420.5e6, "amp": 0.1, "peak_utc": "2026-01-01T00:00:00Z",
                    "width_s": 600, "sidereal": False}, 250_000)
    z = np.zeros(1000, dtype=np.complex64)
    assert inj.apply(z, 1420.4e6, 100.0) is z and not inj.active


def test_one_shot_fires_in_the_first_search_block_after_its_time_only():
    """A one-shot waits for the survey to start a search block (a reference block does
    not count), then peaks after_start_s into it, and is logged as touched."""
    import time

    from aquila.inject import Injectors
    rate = 100_000.0
    inj = Injectors({"sky_hz": 1000.0, "drift_hz_s": 0.0, "amp": 0.01,
                     "peak_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60)),
                     "width_s": 2, "after_start_s": 10}, rate)
    z = np.zeros(int(rate), dtype=np.complex64)
    assert np.all(inj.apply(z.copy(), 0.0, 1.0) == 0)          # no block started: silent
    assert not inj.touched and inj.items[0].armed is None
    inj.begin_block()
    out = inj.apply(z.copy(), 0.0, 1.0)                         # first second of the block
    assert inj.items[0].armed is not None
    assert abs(inj.items[0].peak - inj.items[0].armed - 10.0) < 0.01
    assert np.all(out == 0)                                     # 10 s away at 2 s sigma: off
    for _ in range(9):
        out = inj.apply(z.copy(), 0.0, 1.0)
    assert np.abs(out).max() > 0.005                            # at its peak, 10 s in
    assert inj.touched == inj.items
    for _ in range(20):                                         # the rest of the block
        inj.apply(z.copy(), 0.0, 1.0)
    inj.begin_block()                                           # next block: nothing more
    assert not inj.touched
    for _ in range(12):
        out = inj.apply(z.copy(), 0.0, 1.0)
    assert np.all(out == 0) and not inj.touched
