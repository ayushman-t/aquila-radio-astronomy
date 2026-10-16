# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# MIT licence: free for anyone to use and modify.
"""Offline curator. Reads hits.csv, writes candidates.csv with a tier per hit,
candidates_oncomb.csv for the review lane, comb_history.csv as an audit trail and
curate_summary.json. Runs beside the survey in its own process, reads one file,
can never touch the capture.

Ported from the 2.4 m Mauganj node's curator, where every rule below was measured
against thousands of known-terrestrial hits.

A: on a multi-block track that drifts relative to the receiver's own reference,
   sustained inside its blocks, AND rose and fell like a source crossing the beam.
B: on a track but weaker evidence, or sustained in one block, unconfirmed.
C: everything else that passed the search.
Excluded: present at widely separated times of the same day (a parked beam sees a
sky position once per day), on a track flat against the LO (a spur), inside a
blanked band edge, or on the receiver's own switching-supply comb. A hit that
looks real on its own merits but sits on a comb line is kept in the on-comb
review lane rather than thrown away."""
import bisect
import calendar
import collections
import csv
import functools
import json
import math
import os
import time

import numpy as np

from .store import replace

RECENT_WINDOW = 400          # blocks, about a day
COV_STRONG = 18.0
COV_REVIEW = 8.0
COV_A = 10.0
TRACK_BIN_HZ = 90.0
CROSS_DRIFT_MAX = 0.40
CROSS_DRIFT_STEP = 0.01
FAST_NEAR = 3                # beyond the grid: in-block drift is good to ~2 steps
FAST_POOL_HZ_S = 0.08        # hits within this of a fast trial drift (5 Msps steps 0.034)
LO_MIN_TRACKS = 2
LO_MIN_SEP_HZ = 5000.0       # tracks closer than this may be one source's own lines
DRIFT_MIN = 0.10             # |drift - D_LO| above this moves like the sky (the BL cut)
TRACK_MIN_BLK = 6
TRACK_MAX_SPAN = 25
NOMINAL_DT_S = 215.0
TRACK_MAX_SPAN_S = TRACK_MAX_SPAN * NOMINAL_DT_S
PERSIST_BIN_HZ = 5000.0
PERSIST_MAX_SPAN_S = 2700.0  # scaled up for wide beams by persist_limit()
PERSIST_WINDOW_S = 20 * 3600.0  # a span longer than this is a sidereal repeat, not one signal

# transit envelope: a real track is a beam pattern cut at the threshold, so its ends sit
# near the threshold and its peak is interior. Measured on 2504 terrestrial tracks: median
# edge ratio 0.87. Simulated transits: 0.08 to 0.25.
ENV_EDGE_MAX = 0.45
ENV_INTERIOR_MIN = 0.25
ENV_MIN_BLK = 5

# switching-supply comb: a ladder of equally spaced lines generated inside the receiver.
# Re-fitted from the data on every run because the period walks with temperature.
COMB_MIN_R = 0.50            # below this no comb dominates and nothing is cut
COMB_MIN_LINES = 8           # a comb is a ladder: fewer distinct lines than this is just carriers
COMB_TOL_HZ = 1000.0
COMB_FALLBACK_MAX_S = 6 * 3600
COMB_HISTORY_KEEP = 8000     # about 28 days at a 5 minute cadence


@functools.lru_cache(maxsize=8192)
def block_time(utc):
    try:
        return calendar.timegm(time.strptime(utc, "%Y%m%dT%H%M%SZ"))
    except (ValueError, TypeError):
        try:
            return int(utc) * NOMINAL_DT_S
        except ValueError:
            return None


def load(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        for r in csv.DictReader(f):
            extra = r.get(None) or []
            if (r.get("sdr") or (extra[-1] if extra else "")) == "synthetic":
                continue
            try:
                # a power cut can leave a short or torn row: drop it, never guess
                row = {"utc": r["utc"], "local": r.get("local") or r.get("ist", ""),
                       "freq": float(r["sky_freq_hz"]), "drift": float(r["drift_hz_s"]),
                       "snr": float(r["snr"]), "width": float(r.get("width_hz") or 0),
                       "cov": float(r.get("coverage") or -1), "thumb": r.get("thumb") or ""}
            except (ValueError, KeyError, TypeError):
                continue
            if block_time(row["utc"]) is None or not all(
                    math.isfinite(row[k]) for k in ("freq", "drift", "snr", "width", "cov")):
                continue
            rows.append(row)
    return rows


def persist_limit(beam_deg):
    """1.8x the beam transit at the equator (15 deg/h), never below 45 min."""
    if not beam_deg:
        return PERSIST_MAX_SPAN_S
    return max(PERSIST_MAX_SPAN_S, 1.8 * 3600.0 * beam_deg / 15.0)


# ---------------------------------------------------------------- comb
def fit_comb(freqs, center, window):
    """(period_hz, phase_hz, R) of the dominant comb, or (None, 0, 0) if none dominates."""
    x = np.asarray(freqs, dtype=np.float64) - center
    if len(x) < 200:
        return None, 0.0, 0.0
    lo, hi = float(window[0]), float(window[1])
    P = np.arange(lo, hi, max(25.0, (hi - lo) / 4000.0))
    R = np.array([abs(np.exp(2j * np.pi * (x / p)).mean()) for p in P])
    p0 = P[R.argmax()]
    Pf = np.arange(p0 - 40, p0 + 40, 0.05)
    Rf = np.array([abs(np.exp(2j * np.pi * (x / p)).mean()) for p in Pf])
    p = float(Pf[Rf.argmax()])
    r = float(Rf.max())
    if r < COMB_MIN_R:
        return None, 0.0, 0.0
    c = np.exp(2j * np.pi * (x / p)).mean()
    phi = float(np.angle(c) / (2 * np.pi) * p)
    # a few persistent carriers can score a high R at some period; a real supply comb
    # is a ladder of many lines. Count the distinct lines the hits actually sit on.
    d = ((x - phi) + p / 2) % p - p / 2
    on = np.abs(d) < COMB_TOL_HZ
    if len({int(v) for v in np.round((x[on] - phi) / p)}) < COMB_MIN_LINES:
        return None, 0.0, 0.0
    return p, phi, r


def off_comb(freq, comb, center):
    p, phi, _ = comb
    if not p:
        return 1e12
    d = ((freq - center - phi) + p / 2) % p - p / 2
    return abs(d)


def comb_index(freq, comb, center):
    p, phi, _ = comb
    if not p:
        return None
    return int(round((freq - center - phi) / p))


def load_last_good_comb(path, now, center, window=(0.0, 1e12)):
    if not os.path.exists(path):
        return None
    best = None
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                # a row from before the center column existed reads as None, not missing
                if (r.get("source") == "fit" and float(r["R"]) >= COMB_MIN_R
                        and abs(float(r.get("center") or center) - center) < 1
                        and window[0] <= float(r["period"]) <= window[1]):
                    best = r
            except (ValueError, KeyError, TypeError):
                continue
    if not best or now - float(best["run_epoch"]) > COMB_FALLBACK_MAX_S:
        return None
    return float(best["period"]), float(best["phase"]), float(best["R"])


def log_comb(path, now, comb, source, n_oncomb, n_lines, center):
    try:
        header = "run_epoch,run_utc,period,phase,R,source,n_oncomb,n_lines,center\n"
        p, phi, r = comb if comb and comb[0] else (0.0, 0.0, 0.0)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
        line = (f"{now:.0f},{stamp},{p:.3f},{phi:.2f},{r:.5f},{source},"
                f"{n_oncomb},{n_lines},{center:.0f}\n")
        rows = []
        if os.path.exists(path):
            with open(path) as f:
                rows = f.readlines()[1:]
        rows.append(line)
        rows = rows[-COMB_HISTORY_KEEP:]
        with open(path + ".tmp", "w") as f:
            f.write(header)
            f.writelines(rows)
        replace(path + ".tmp", path)
    except OSError:
        pass


# ---------------------------------------------------------------- envelope
def track_groups(rows, t0):
    """Hits sharing a best drift and track cell, split where the track is silent for longer
    than a track may span: the same key hours later is another signal."""
    groups = collections.defaultdict(list)
    for r in rows:
        if r.get("_D") is not None:
            key = (r["_D"],
                   round((r["freq"] - r["_D"] * (block_time(r["utc"]) - t0)) / TRACK_BIN_HZ))
            groups[key].append(r)
    out = []
    for key, members in groups.items():
        members.sort(key=lambda m: block_time(m["utc"]))
        run = [members[0]]
        for m in members[1:]:
            if block_time(m["utc"]) - block_time(run[-1]["utc"]) > TRACK_MAX_SPAN_S:
                out.append((key, run))
                run = []
            run.append(m)
        out.append((key, run))
    return out


def track_envelope(rows, t0, snr_thresh):
    for r in rows:
        r["_env"] = None
        r["_span"] = 0.0
    for _, members in track_groups(rows, t0):
        span = block_time(members[-1]["utc"]) - block_time(members[0]["utc"])
        for m in members:
            m["_span"] = span
        s = [m["snr"] for m in members]
        n = len(s)
        if n < ENV_MIN_BLK:
            continue
        i = max(range(n), key=lambda k: s[k])
        pf = i / (n - 1)
        interior = min(pf, 1.0 - pf)
        pe = s[i] - snr_thresh
        er = (max(s[0], s[-1]) - snr_thresh) / pe if pe > 0 else 1.0
        env = {"n": n, "interior": round(interior, 3), "edge_ratio": round(er, 3),
               "pass": bool(er < ENV_EDGE_MAX and interior >= ENV_INTERIOR_MIN)}
        for m in members:
            m["_env"] = env


# ---------------------------------------------------------------- the curator
def curate(rows, center_hz=1420e6, exclude_if_khz=(), snr_thresh=25.0,
           persist_max_s=PERSIST_MAX_SPAN_S, comb_window=(10e3, 100e3),
           history_path=None, now=None):
    now = time.time() if now is None else now
    empty = {"A": [], "B": [], "C": [], "rfi": 0, "rfi_persist": 0, "rfi_comb": 0,
             "rfi_edge": 0, "oncomb_review": []}
    blocks = sorted({r["utc"] for r in rows})
    recent = set(blocks[-RECENT_WINDOW:])
    rows = [r for r in rows if r["utc"] in recent]
    if not rows:
        return empty, 0, 0.0, (None, 0.0, 0.0), {"source": "none", "n_lines": 0}

    t0 = min(block_time(r["utc"]) for r in rows)
    for r in rows:
        r["_best"] = (0, 0, 0.0)
        r["_D"] = None

    def max_run(ts):
        """Most blocks inside any window a track may span, in real time: on a quiet
        band the blocks with hits can be hours apart."""
        best = 0
        for i in range(len(ts)):
            j = i
            while j < len(ts) and ts[j] - ts[i] <= TRACK_MAX_SPAN_S:
                j += 1
            best = max(best, j - i)
        return best

    # 1. cross-block de-Doppler: every hit's best track drift. The grid covers the slow
    # drifts every line can have; beyond it a track's hits all carry about the same
    # in-block drift, so only those drifts are tried, and only on those hits
    ndg = int(round(CROSS_DRIFT_MAX / CROSS_DRIFT_STEP))
    trials = [(round(gi * CROSS_DRIFT_STEP, 4), rows) for gi in range(-ndg, ndg + 1)]
    fast = collections.Counter(round(r["drift"] / CROSS_DRIFT_STEP) for r in rows
                               if abs(r["drift"]) > CROSS_DRIFT_MAX)
    near = collections.Counter()
    by_drift = sorted(rows, key=lambda r: r["drift"])
    drifts = [r["drift"] for r in by_drift]
    for k, n in fast.items():
        for j in range(k - FAST_NEAR, k + FAST_NEAR + 1):
            near[j] += n
    for k in sorted(near):
        D = round(k * CROSS_DRIFT_STEP, 4)
        if near[k] < TRACK_MIN_BLK or abs(D) <= CROSS_DRIFT_MAX:
            continue
        lo = bisect.bisect_left(drifts, D - FAST_POOL_HZ_S)
        hi = bisect.bisect_right(drifts, D + FAST_POOL_HZ_S)
        trials.append((D, by_drift[lo:hi]))
    for D, pool in trials:
        cells = collections.defaultdict(list)
        for r in pool:
            cells[round((r["freq"] - D * (block_time(r["utc"]) - t0)) / TRACK_BIN_HZ)].append(r)
        for members in cells.values():
            sz = max_run(sorted({block_time(m["utc"]) for m in members}))
            if sz >= TRACK_MIN_BLK:
                # neighbouring drifts often hold the same run; the true one holds it tightest
                off = [m["freq"] - D * (block_time(m["utc"]) - t0) for m in members]
                key = (sz, -round(max(off) - min(off)), -abs(D))
                for m in members:
                    if key > m["_best"]:
                        m["_best"] = key
                        m["_D"] = D
    # 2. the receiver's own drift (always slow) is the drift most tracks share. Each hit
    # counts at its best drift only: a coarse cell holds a fixed line for 6 blocks at any
    # drift up to ~0.08 Hz/s, so counting every drift that fits gives a plateau. And the
    # receiver moves every line it hears at once, so only tracks that overlap another
    # track in time count: a lone transit, or one source seen on two passes, never sets it
    spans = collections.defaultdict(list)
    for (D, _), members in track_groups(rows, t0):
        if abs(D) <= CROSS_DRIFT_MAX:
            f = float(np.median([m["freq"] - D * (block_time(m["utc"]) - t0) for m in members]))
            spans[D].append((block_time(members[0]["utc"]), block_time(members[-1]["utc"]),
                             len(members), f))
    score = {}
    for D, tr in spans.items():
        # a carrier with sidebands is several tracks at one drift, all at once; the
        # receiver's drift shows on lines spread across the band
        shared = [n for i, (a, b, n, f) in enumerate(tr)
                  if any(a <= b2 and a2 <= b and abs(f - f2) >= LO_MIN_SEP_HZ
                         for j, (a2, b2, _, f2) in enumerate(tr) if j != i)]
        if len(shared) >= LO_MIN_TRACKS:
            score[D] = (len(shared), sum(shared), -abs(D))
    D_LO = max(score, key=score.get) if score else 0.0

    track_envelope(rows, t0, snr_thresh)

    # 3. persistence: longest span per frequency bin inside any 20 h window (so it does
    # not reset at UTC midnight, and a source seen again next sidereal day is not "on")
    bybin = collections.defaultdict(list)
    for r in rows:
        bybin[round(r["freq"] / PERSIST_BIN_HZ)].append(block_time(r["utc"]))
    span = {}
    for b, ts in bybin.items():
        ts.sort()
        best = i = 0
        for j in range(len(ts)):
            while ts[j] - ts[i] > PERSIST_WINDOW_S:
                i += 1
            best = max(best, ts[j] - ts[i])
        span[b] = best

    # 4. the comb, fitted last from the same rows, applied first below
    comb = fit_comb([r["freq"] for r in rows], center_hz, comb_window)
    source = "fit"
    if not comb[0]:
        fb = (load_last_good_comb(history_path, now, center_hz, comb_window)
              if history_path else None)
        comb, source = (fb, "fallback") if fb else ((None, 0.0, 0.0), "none")

    edges = [(lo * 1e3, hi * 1e3) for lo, hi in (exclude_if_khz or [])]

    def in_edge(freq):
        d = freq - center_hz
        return any(lo <= d <= hi for lo, hi in edges)

    def merit(r):
        # on longer than a beam crossing, in its frequency bin or along its drifting track
        if max(span.get(round(r["freq"] / PERSIST_BIN_HZ), 0.0), r["_span"]) > persist_max_s:
            return "RFI", "persist"
        D = r["_D"]
        if D is not None and abs(D - D_LO) >= DRIFT_MIN:
            if not (r["cov"] < 0 or r["cov"] >= COV_A):
                return "B", "track"
            env = r.get("_env")
            if env is not None and env.get("pass"):
                return "A", "track+envelope"
            return "B", "track_noenv"
        if D is not None:
            return "RFI", "flatlo"
        if r["cov"] < 0 or r["cov"] >= COV_STRONG or r["cov"] >= COV_REVIEW:
            return "B", "sustained"
        return "C", "weak"

    out = dict(empty, A=[], B=[], C=[], oncomb_review=[])
    lines = collections.Counter()
    for r in rows:
        if in_edge(r["freq"]):
            out["rfi"] += 1
            out["rfi_edge"] += 1
            continue
        tier, why = merit(r)
        if off_comb(r["freq"], comb, center_hz) < COMB_TOL_HZ:
            n = comb_index(r["freq"], comb, center_hz)
            if n is not None:
                lines[n] += 1
            out["rfi"] += 1
            out["rfi_comb"] += 1
            if tier in ("A", "B"):
                rr = dict(r)
                rr["tier"] = tier
                rr["comb_n"] = n if n is not None else 0
                out["oncomb_review"].append(rr)
            continue
        if tier == "RFI":
            out["rfi"] += 1
            if why == "persist":
                out["rfi_persist"] += 1
            continue
        rr = dict(r)
        rr["tier"] = tier
        out[tier].append(rr)
    return out, len(recent), D_LO, comb, {"source": source, "n_lines": len(lines)}


def run(state_dir, beam_deg=None, center_hz=1420e6, exclude_if_khz=(), snr_thresh=25.0,
        comb_window=(10e3, 100e3)):
    now = time.time()
    hits = os.path.join(state_dir, "hits.csv")
    cand = os.path.join(state_dir, "candidates.csv")
    cand_oncomb = os.path.join(state_dir, "candidates_oncomb.csv")
    history = os.path.join(state_dir, "comb_history.csv")
    summ_path = os.path.join(state_dir, "curate_summary.json")
    out, nb, d_lo, comb, stats = curate(load(hits), center_hz, exclude_if_khz, snr_thresh,
                                        persist_limit(beam_deg), comb_window, history, now)
    order = {"A": 0, "B": 1, "C": 2}

    def row(r, extra=""):
        return "{},{},{},{:.0f},{:.4f},{:.2f},{:.1f},{:.1f},{}{}\n".format(
            r["tier"], r["utc"], r["local"], r["freq"], r["drift"], r["snr"], r["width"],
            r["cov"], extra, r["thumb"])

    with open(cand + ".tmp", "w") as f:
        f.write("tier,utc,local,sky_freq_hz,drift_hz_s,snr,width_hz,coverage,thumb\n")
        for r in sorted(out["A"] + out["B"] + out["C"], key=lambda x: (order[x["tier"]], x["utc"])):
            f.write(row(r))
    replace(cand + ".tmp", cand)
    with open(cand_oncomb + ".tmp", "w") as f:
        f.write("tier,utc,local,sky_freq_hz,drift_hz_s,snr,width_hz,coverage,comb_n,thumb\n")
        for r in sorted(out["oncomb_review"], key=lambda x: -x["snr"]):
            f.write(row(r, f"{r.get('comb_n', 0)},"))
    replace(cand_oncomb + ".tmp", cand_oncomb)
    if nb:
        log_comb(history, now, comb, stats["source"], out["rfi_comb"], stats["n_lines"], center_hz)
    summ = {"recent_blocks": nb, "lo_common_drift_hz_s": round(d_lo, 3),
            "tierA": len(out["A"]), "tierB": len(out["B"]), "tierC": len(out["C"]),
            "excluded_rfi": out["rfi"], "excluded_persistent": out["rfi_persist"],
            "excluded_comb": out["rfi_comb"], "excluded_edge": out["rfi_edge"],
            "comb_period_hz": round(comb[0], 2) if comb[0] else None,
            "comb_phase_hz": round(comb[1], 1) if comb[0] else None,
            "comb_R": round(comb[2], 4) if comb[0] else None,
            "comb_source": stats["source"], "comb_active_lines": stats["n_lines"],
            "oncomb_review": len(out["oncomb_review"]),
            "band_blind_pct": round(100.0 * 2 * COMB_TOL_HZ / comb[0], 2) if comb[0] else 0.0,
            "ts": now}
    with open(summ_path + ".tmp", "w") as f:
        json.dump(summ, f, indent=2)
    replace(summ_path + ".tmp", summ_path)
    return summ
