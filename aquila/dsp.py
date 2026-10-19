# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# GPL-3.0-or-later: free to use and modify, derived works must stay open.
"""The signal chain. Streaming channelizer, per-channel median normalisation,
integer-shift de-Doppler search, hit clustering. Pure numpy, memory bounded."""
import numpy as np
from scipy.ndimage import uniform_filter1d


def channelize(src, n_chan, rows, dwell_s, settle_skip_s, log=print, raw_sink=None):
    """Stream one dwell of IQ through the FFT into a fixed (rows x n_chan) waterfall.
    Returns (waterfall fftshifted, chan_bw_hz, row_s) or (None, None, None)."""
    rate = src.rate
    total_ffts = int((dwell_s * rate) // n_chan)
    skip_ffts = int((settle_skip_s * rate) // n_chan)
    fpr = max(1, (total_ffts - skip_ffts) // rows)
    row_s = fpr * n_chan / rate
    nsamples = (skip_ffts + fpr * rows) * n_chan

    src.start(nsamples, n_chan)
    win = np.hanning(n_chan).astype(np.float32)
    wf = np.zeros((rows, n_chan), dtype=np.float32)
    gen = src.frames(n_chan)

    def next_frame():
        return next(gen, None)

    for _ in range(skip_ffts):
        if next_frame() is None:
            break
    measured = False
    for r in range(rows):
        for _ in range(fpr):
            fr = next_frame()
            if fr is None:
                err = src.finish()
                log("SDR ended early: " + (err.strip()[-200:] or "no output"))
                return None, None, None
            if raw_sink is not None:
                raw_sink.write(fr)
            z = src.to_complex(fr)
            if r == 0 and not measured:
                src.input_rms = input_rms(z)
                measured = True
            X = np.fft.fft(z * win)
            wf[r] += X.real * X.real + X.imag * X.imag
    err = src.finish()
    if "overrun" in err.lower():
        log("WARNING: SDR overrun during capture: " + err.strip()[-80:])
    return np.fft.fftshift(wf, axes=1), rate / n_chan, row_s


def input_rms(z):
    """Noise per I/Q component, in the SDR's own counts."""
    return float(np.sqrt(np.mean(z.real * z.real + z.imag * z.imag) / 2))


WEAK_INPUT_COUNTS = 3.0
WEAK_INPUT_TEXT = ("input very weak ({:.1f} counts of noise): the SDR is hearing little more "
                   "than itself. Check the LNA is connected and powered (bias tee on, or its "
                   "own supply) and the cable is good. Line and SETI results will be poor.")


def normalize(wf):
    """Divide every channel by its median over time. Anything fixed in frequency
    (passband shape, internal combs, steady carriers) divides to 1. A drifting
    signal only visits a channel for a few rows, so it survives."""
    ref = np.median(wf, axis=0)
    wf /= (ref + 1e-9)
    return wf


def blank_set(n_chan, chan_bw, dc_notch_hz, exclude_if_khz):
    dcn = int(dc_notch_hz / chan_bw)
    blank = set(range(n_chan // 2 - dcn, n_chan // 2 + dcn + 1))
    for lo_khz, hi_khz in exclude_if_khz or []:
        a = n_chan // 2 + int(lo_khz * 1e3 / chan_bw)
        b = n_chan // 2 + int(hi_khz * 1e3 / chan_bw)
        blank |= set(range(max(0, a), min(n_chan, b + 1)))
    return {c for c in blank if 0 <= c < n_chan}


def _shift_add(acc, row, s):
    n = row.size
    if s == 0:
        acc += row
    elif s > 0:
        acc[:n - s] += row[s:]
    else:
        acc[-s:] += row[:n + s]


def drift_search(wf, chan_bw, row_s, max_drift, snr_thresh, blank_chans=(),
                 edge_frac=0.10, max_trials=801, log=print, cancel=None):
    """Try every integer channel shift across the block, sum along each drift
    line, flag channels above snr_thresh (MAD based). Each hit carries its
    width in Hz and its coverage (how many rows the track really occupies)."""
    rows, nch = wf.shape
    lo, hi = int(nch * edge_frac), int(nch * (1 - edge_frac))
    total_s = max(rows - 1, 1) * row_s
    m = int(np.floor(max_drift * total_s / chan_bw))
    if m < 1:
        log(f"WARNING: drift unmeasurable (chan_bw {chan_bw:.2f} Hz, T {total_s:.0f} s)")
        m = 0
    if 2 * m + 1 > max_trials:
        m = (max_trials - 1) // 2
    blank = np.array(sorted(c for c in blank_chans if lo <= c < hi), dtype=int)
    keep = np.ones(hi - lo, dtype=bool)
    if blank.size:
        keep[blank - lo] = False
    ridx = np.arange(rows)
    hits = {}
    clip = None
    for dch in range(-m, m + 1):
        if cancel is not None and cancel():
            return []                     # stopping: the block is abandoned anyway
        shifts = np.round(ridx * (dch / max(rows - 1, 1))).astype(int)
        acc = np.zeros(nch, dtype=np.float32)
        for s in np.unique(shifts):
            _shift_add(acc, wf[shifts == s].sum(axis=0), int(s))
        seg = acc[lo:hi].copy()
        if blank.size:
            seg[blank - lo] = np.median(seg)
        if clip is None:
            # one ceiling per block: noise sums have the same spread at every drift,
            # and a signal must not raise its own baseline (or its neighbours')
            g0 = seg[keep] if blank.size else seg
            m0 = np.median(g0)
            clip = m0 + 5.0 * 1.4826 * np.median(np.abs(g0 - m0))
        base = uniform_filter1d(np.minimum(seg, clip), 301, mode="nearest")
        norm = seg / (base + 1e-9)
        if blank.size:
            norm[blank - lo] = 1.0
        good = norm[keep] if blank.size else norm
        if good.size < 100:
            continue
        med = np.median(good)
        mad = np.median(np.abs(good - med)) * 1.4826 + 1e-9
        snr = (norm - med) / mad
        drift = dch * chan_bw / total_s
        above = snr > snr_thresh
        e = np.diff(np.concatenate(([0], above.astype(np.int8), [0])))
        starts = np.flatnonzero(e == 1)
        ends = np.flatnonzero(e == -1)
        for c in np.flatnonzero(above):
            ch = c + lo
            if hits.get(ch, {"snr": -1})["snr"] < snr[c]:
                k = np.searchsorted(starts, c, side="right") - 1
                hits[ch] = {"chan": int(ch), "drift_hz_s": float(drift),
                            "snr": float(snr[c]),
                            "width_hz": float((ends[k] - starts[k]) * chan_bw)}
    out = []
    for ch in sorted(hits):
        h = hits[ch]
        if out and ch - out[-1]["chan"] <= 3:
            if h["snr"] > out[-1]["snr"]:
                out[-1] = h
        else:
            out.append(h)
    out = drop_ghosts(out, chan_bw, total_s)
    floor = float(np.median(wf))
    for h in out:
        dch = int(round(h["drift_hz_s"] * total_s / chan_bw))
        sh = np.round(ridx * (dch / max(rows - 1, 1))).astype(int)
        track = wf[ridx, np.clip(h["chan"] + sh, 0, nch - 1)]
        x = np.clip(track - floor, 0.0, None)
        h["coverage"] = float((x.sum() ** 2) / (np.sum(x * x) + 1e-12)) if x.sum() > 0 else 0.0
    return out


GHOST_RATIO = 3.0


def drop_ghosts(hits, chan_bw, total_s, near_ch=2):
    """Any drift line that crosses a strong track collects a row or two of it, so a
    strong signal reports weak copies at other drifts up to twice the drift reach
    away. A hit whose track meets a much stronger hit's track inside the block is
    that crossing. Two real signals of similar strength that cross are both kept."""
    if len(hits) < 2:
        return hits
    span = [(h["chan"], h["drift_hz_s"] * total_s / chan_bw) for h in hits]
    order = sorted(range(len(hits)), key=lambda i: -hits[i]["snr"])
    keep = []
    for i in order:
        c, d = span[i]
        ghost = False
        for j in keep:
            if hits[j]["snr"] < GHOST_RATIO * hits[i]["snr"]:
                continue
            a = c - span[j][0]                 # separation at the first row
            b = a + d - span[j][1]             # and at the last
            if min(abs(a), abs(b)) <= near_ch or a * b < 0:
                ghost = True
                break
        if not ghost:
            keep.append(i)
    return [hits[i] for i in sorted(keep)]


def cluster_hits(hits, gap=200):
    """Collapse hits within `gap` channels into one (keep the peak)."""
    if not hits:
        return []
    hits = sorted(hits, key=lambda h: h["chan"])
    out = [hits[0]]
    for h in hits[1:]:
        if h["chan"] - out[-1]["chan"] <= gap:
            if h["snr"] > out[-1]["snr"]:
                out[-1] = h
        else:
            out.append(h)
    return out


def sky_freq(center_hz, n_chan, chan_bw, chan):
    return center_hz + (chan - n_chan / 2) * chan_bw
