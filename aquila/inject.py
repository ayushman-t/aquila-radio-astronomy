# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# GPL-3.0-or-later: free to use and modify, derived works must stay open.
"""Test signal added to the receiver's own samples, so the whole search can be checked
end to end on a live node: real noise, real RFI, one known narrowband source on top.

Set `advanced.inject` in the node config and restart the node:

    "inject": {"sky_hz": 1420750000, "drift_hz_s": 0.15, "amp": 0.004,
               "peak_utc": "2026-11-08T03:40:00Z", "width_s": 1500, "sidereal": true}

sky_hz      the tone's sky frequency; it lands in the right channel at any tuning
drift_hz_s  its drift, so the de-Doppler search has to follow it
amp         its amplitude as a fraction of the input's own RMS
peak_utc    when it is strongest; it rises and falls like a source crossing the beam,
            a Gaussian of this width_s (one sigma)
sidereal    repeat every sidereal day (86164 s), like a fixed point on the sky

Every scan this touches is marked in survey.log, and status.json carries the spec, so
an injected hit can never be mistaken for a real one later."""
import calendar
import math
import time

import numpy as np

SIDEREAL_S = 86164.0905


def parse_utc(s):
    return calendar.timegm(time.strptime(str(s).replace("Z", ""), "%Y-%m-%dT%H:%M:%S"))


class Injector:
    """One test signal. `after_start_s` makes it a one-shot event instead: its peak is
    that many seconds into the first capture that starts after peak_utc, so a short
    burst (the 72 s of the Wow! signal) lands inside one block for certain."""

    def __init__(self, spec, rate):
        self.sky_hz = float(spec["sky_hz"])
        self.drift = float(spec.get("drift_hz_s", 0.0))
        self.amp = float(spec.get("amp", 0.004))
        self.peak = parse_utc(spec["peak_utc"])
        self.width = float(spec.get("width_s", 1500.0))
        self.sidereal = bool(spec.get("sidereal", True))
        self.after_start = spec.get("after_start_s")
        self.rate = float(rate)
        self.phase = 0.0
        self.active = False
        self.t0 = None          # wall time of the first sample of this capture
        self.n = 0              # samples since then: time runs on the sample clock
        self.last = 0.0
        self.armed = None       # one-shot: the search block it is tied to
        self.block_start = False  # set by the survey when a search block begins
        self.touched = False    # the tone was on at some point in this block

    def nearest_peak(self, t):
        if not self.sidereal:
            return self.peak
        k = round((t - self.peak) / SIDEREAL_S)
        return self.peak + k * SIDEREAL_S

    def envelope(self, t):
        return math.exp(-0.5 * ((t - self.nearest_peak(t)) / self.width) ** 2)

    def begin_block(self):
        """The survey calls this as a search block starts (a reference block does not),
        so a one-shot lands in a block that is searched."""
        self.block_start = True
        self.touched = False

    def apply(self, z, center_hz, rms):
        """Add the tone to one frame of complex samples. Time runs on the sample clock
        from the start of each capture, so a simulated receiver that runs faster than
        real time drifts the tone exactly as a real one would."""
        now = time.time()
        if self.t0 is None or now - self.last > 2.0:
            self.t0, self.n = now, 0          # a new capture: the stream has a new start
        if self.block_start:
            self.block_start = False
            if self.after_start is not None and self.armed is None and now >= self.peak:
                self.armed = now              # the first search block after peak_utc
                self.peak = now + float(self.after_start)
                self.sidereal = False
        self.last = now
        if self.after_start is not None and self.armed is None:
            return z                          # one-shot, not yet fired
        n = z.size
        t = self.t0 + self.n / self.rate
        self.n += n
        env = self.envelope(t)
        if env < 0.01:
            self.active = False
            self.phase = 0.0
            return z
        self.active = True
        self.touched = True
        # the frame's own duration is far shorter than the drift matters over
        f = self.sky_hz + self.drift * (t - self.nearest_peak(t)) - center_hz
        if abs(f) >= self.rate / 2:
            return z
        ph = self.phase + 2 * np.pi * f / self.rate * np.arange(1, n + 1)
        self.phase = float(ph[-1] % (2 * np.pi))
        a = self.amp * env * (rms if rms else 1.0)
        return z + (a * np.exp(1j * ph)).astype(np.complex64)

    def spec(self):
        return {"sky_hz": self.sky_hz, "drift_hz_s": self.drift, "amp": self.amp,
                "peak_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.peak)),
                "width_s": self.width, "sidereal": self.sidereal}


class Injectors:
    """Several test signals on one stream; `advanced.inject` may be one spec or a list."""

    def __init__(self, specs, rate):
        specs = specs if isinstance(specs, list) else [specs]
        self.items = [Injector(sp, rate) for sp in specs]

    def apply(self, z, center_hz, rms):
        for it in self.items:
            z = it.apply(z, center_hz, rms)
        return z

    def begin_block(self):
        for it in self.items:
            it.begin_block()

    @property
    def active(self):
        return [it for it in self.items if it.active]

    @property
    def touched(self):
        """Signals that were on at any time in the block just captured."""
        return [it for it in self.items if it.touched]

    def spec(self):
        return [it.spec() for it in self.items]
