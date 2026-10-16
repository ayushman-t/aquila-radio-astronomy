# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# MIT licence: free for anyone to use and modify.
"""Learned spur mask. A channel that hits at the same place with no drift in
several recent blocks is fixed to the instrument, not the sky, and is blanked
before the search so it stops being rediscovered every block."""
import contextlib
import json
import os
from collections import Counter, deque


class SpurMask:
    def __init__(self, bin_ch=8, min_sightings=3, keep_blocks=12, path=None, tuning=None):
        self.bin = bin_ch
        self.min_sightings = min_sightings
        self.recent = deque(maxlen=keep_blocks)
        self.mask = set()
        self.path = path
        self.tuning = dict(tuning or {})
        if path and os.path.exists(path):
            with contextlib.suppress(Exception):
                d = json.load(open(path))
                if isinstance(d, dict) and d.get("tuning") == self.tuning:
                    self.mask = set(d.get("bins", []))

    def update(self, hit_channels):
        self.recent.append({c // self.bin for c in hit_channels})
        counts = Counter()
        for s in self.recent:
            for b in s:
                counts[b] += 1
        self.mask = {b for b, c in counts.items() if c >= self.min_sightings}
        if self.path:
            with contextlib.suppress(Exception):
                with open(self.path + ".tmp", "w") as f:
                    json.dump({"tuning": self.tuning, "bins": sorted(self.mask)}, f)
                os.replace(self.path + ".tmp", self.path)

    def blank_channels(self, n_chan, gap_close_bins=30):
        """Learned bins plus the gaps between close neighbours, so a whole comb
        region is masked once a few of its teeth are learned."""
        if not self.mask:
            return set()
        bins = sorted(self.mask)
        filled = set(bins)
        for a, b in zip(bins, bins[1:], strict=False):
            if b - a <= gap_close_bins:
                filled.update(range(a, b + 1))
        out = set()
        for bn in filled:
            for d in range(self.bin):
                c = bn * self.bin + d
                if 0 <= c < n_chan:
                    out.add(c)
        return out
