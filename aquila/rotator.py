# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# MIT licence: free for anyone to use and modify.
"""Live dish pointing from a rotator over the Hamlib rotctld protocol.

Anything that speaks rotctld works: Hamlib's own rotctld in front of a Yaesu, SPID
or EasyComm controller, Gpredict-style setups, or the Aquila dish bridge. One
setting, "host:port". Blank means the dish is fixed and the az/el in Settings is
used. A failed read keeps the last known pointing; nothing stops observing."""
import socket
import time

_last = {}
STALE_S = 600       # a reading older than this is not reused; the dish may have moved


def parse(addr):
    addr = (addr or "").strip()
    if not addr:
        return None
    host, _, port = addr.rpartition(":")
    if not host:
        host, port = port, "4533"
    try:
        return host.strip("[]"), int(port)
    except ValueError:
        return None


def read(addr, timeout=2.0):
    """(az, el) from the rotator, or None. 'p' answers two lines, or 'RPRT n'."""
    hp = parse(addr)
    if not hp:
        return None
    end = time.monotonic() + timeout           # one deadline for the whole read
    try:
        with socket.create_connection(hp, timeout=timeout) as s:
            s.sendall(b"p\n")
            data = b""
            while b"\n" not in data or (not data.startswith(b"RPRT") and data.count(b"\n") < 2):
                left = end - time.monotonic()
                if left <= 0 or len(data) > 256:
                    return None
                s.settimeout(left)
                chunk = s.recv(256)
                if not chunk:
                    break
                data += chunk
            with _suppress():
                s.sendall(b"q\n")
        lines = data.decode(errors="replace").split()
        if not lines or lines[0] == "RPRT":
            return None
        az, el = float(lines[0]), float(lines[1])
        if not (-360 <= az <= 720 and -10 <= el <= 100):
            return None                         # not a position: garbage on the line
        az %= 360.0
        _last[addr] = (az, el, time.time())
        return az, el
    except (OSError, ValueError, IndexError):
        return None


def pointing(dish, timeout=2.0):
    """dish dict with az/el replaced by the rotator's, when one is configured.
    Keeps the last good reading if the rotator does not answer right now."""
    addr = str((dish or {}).get("rotator") or "")
    if not addr.strip():
        return dish
    # last block's live flags must not ride along into this reading
    out = {k: v for k, v in dish.items()
           if k not in ("live", "rotator_stale_s", "pointing_unknown")}
    r = read(addr, timeout)
    if r is None:
        r = _last.get(addr)
        if r is None or time.time() - r[2] > STALE_S:
            out["pointing_unknown"] = True   # rotator gone: Settings az/el, flagged
            return out
        out["rotator_stale_s"] = round(time.time() - r[2])
    out["az"], out["el"] = round(float(r[0]), 1), round(float(r[1]), 1)
    out["live"] = True
    return out


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return True
