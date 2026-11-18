"""Reads IQ from receivers that have no capture tool of their own, through the maker's
library: ADALM-Pluto (libiio), bladeRF (libbladeRF), LimeSDR (LimeSuite) and SDRplay
(sdrplay_api). Written from the libraries' headers; see sdr.py for what has been tried
on real hardware.

Run as its own process by sdr.py, the same way the other receivers are read through
their vendor tools, so a library that hangs or crashes ends this process and not the
node. It writes int16 IQ, full scale 16 bit, whatever the receiver's own word is.

    vendor_rx NAME find
    vendor_rx NAME rx --freq HZ --rate HZ --n SAMPLES --out -|PATH
                      [--dev SERIAL] [--gain KEY=VALUE ...] [--bias 0|1]

The gains that were set, as the library reports them back, go to stderr as
"gains set: ...". Exit 3: the receiver does not give the sample rate asked for.
Exit 4: no samples came.
"""
import argparse
import contextlib
import ctypes
import glob
import json
import os
import sys
import time
from ctypes import (
    POINTER,
    byref,
    c_bool,
    c_char,
    c_char_p,
    c_double,
    c_float,
    c_int,
    c_int64,
    c_longlong,
    c_size_t,
    c_ssize_t,
    c_ubyte,
    c_uint,
    c_uint8,
    c_uint32,
    c_uint64,
    c_ushort,
    c_void_p,
)

P = c_void_p
CHUNK = 32768               # samples asked of the library at a time
QUIET_S = 10.0              # no samples this long: give up


class Fail(Exception):
    """Something the user can act on; its text is the whole message."""


# ---- finding the maker's library

# what to look for, and what to tell the user to install
LIBS = {
    "pluto": {"what": "libiio", "win": ("libiio.dll",), "mac": ("libiio*.dylib", "iio"),
              "so": ("libiio.so*",)},
    "bladerf": {"what": "the bladeRF software (libbladeRF)", "win": ("bladeRF.dll",
                "libbladeRF.dll"), "mac": ("libbladeRF*.dylib",), "so": ("libbladeRF.so*",)},
    "limesdr": {"what": "LimeSuite", "win": ("LimeSuite.dll", "libLimeSuite.dll"),
                "mac": ("libLimeSuite*.dylib",), "so": ("libLimeSuite.so*",)},
    "sdrplay": {"what": "the SDRplay API (from sdrplay.com)", "win": ("sdrplay_api.dll",),
                "mac": ("libsdrplay_api*.dylib",), "so": ("libsdrplay_api.so*",)},
}


def _lib_dirs():
    exe_dir = os.path.dirname(os.path.abspath(sys.argv[0] or "."))
    dirs = [os.path.join(exe_dir, "tools")]
    if getattr(sys, "frozen", False):
        dirs.append(os.path.join(getattr(sys, "_MEIPASS", exe_dir), "tools"))
    if sys.platform == "win32":
        pf = [os.environ.get(k, "") for k in ("ProgramFiles", "ProgramW6432")]
        for base in dict.fromkeys(p for p in pf if p):
            dirs += [os.path.join(base, "SDRplay", "API", "x64"),
                     os.path.join(base, "bladeRF", "x64"),
                     os.path.join(base, "PothosSDR", "bin"),
                     os.path.join(base, "LimeSuite")]
        dirs.append(os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32"))
        dirs += os.environ.get("PATH", "").split(os.pathsep)
    elif sys.platform == "darwin":
        dirs += ["/opt/homebrew/lib", "/usr/local/lib", "/opt/local/lib",
                 "/Library/Frameworks/iio.framework"]
    else:
        snap = os.environ.get("SNAP", "")
        roots = ([snap] if snap else []) + [""]
        for root in roots:
            dirs += glob.glob(root + "/usr/lib/*-linux-gnu*")
            dirs += [root + "/usr/local/lib", root + "/usr/lib64", root + "/usr/lib"]
    return [d for d in dirs if d]


def lib_path(name):
    """The file of this receiver's library, or None. Only files are looked at."""
    named = os.environ.get("AQUILA_RX_LIB_" + name.upper(), "")
    if named:
        return named if os.path.isfile(named) else None
    spec = LIBS[name]
    pats = spec["win" if sys.platform == "win32" else "mac" if sys.platform == "darwin" else "so"]
    for d in _lib_dirs():
        for pat in pats:
            found = sorted(f for f in glob.glob(os.path.join(d, pat)) if os.path.isfile(f))
            if found:
                return found[0]
    return None


def load(name):
    path = lib_path(name)
    if not path:
        raise Fail(f"{LIBS[name]['what']} is not installed")
    try:
        return ctypes.CDLL(path, use_errno=True)
    except OSError as e:
        raise Fail(f"{LIBS[name]['what']} could not be loaded: {e}") from None


def _declare(lib, table):
    for fn, (res, args) in table.items():
        f = getattr(lib, fn)
        f.restype, f.argtypes = res, args


def _text(b):
    return b.decode("utf-8", "replace").strip() if b else ""


# ---- writing the samples

class Out:
    """Takes interleaved int16 IQ until the count asked for is written."""

    def __init__(self, fh, nsamples):
        self.fh, self.left = fh, int(nsamples)

    def put(self, iq16):
        """Write what is still wanted of this block. True once all of it is out."""
        n = min(len(iq16) // 2, self.left)
        if n > 0:
            self.fh.write(iq16[:2 * n].tobytes())
            self.left -= n
        return self.left <= 0


def _within(rate, got):
    return abs(got - rate) <= max(2.0, rate * 1e-6)


def _rate_or_fail(say, rate, got):
    if _within(rate, got):
        return True
    # a rate the receiver rounds to another one would put every frequency wrong
    say(f"the receiver gives {got:.0f} samples a second, not the {rate:.0f} asked for")
    return False


# ---- ADALM-Pluto, libiio

def _iio(lib):
    _declare(lib, {
        "iio_create_scan_context": (P, [c_char_p, c_uint]),
        "iio_scan_context_destroy": (None, [P]),
        "iio_scan_context_get_info_list": (c_ssize_t, [P, POINTER(POINTER(P))]),
        "iio_context_info_list_free": (None, [POINTER(P)]),
        "iio_context_info_get_description": (c_char_p, [P]),
        "iio_context_info_get_uri": (c_char_p, [P]),
        "iio_create_context_from_uri": (P, [c_char_p]),
        "iio_context_destroy": (None, [P]),
        "iio_context_set_timeout": (c_int, [P, c_uint]),
        "iio_context_find_device": (P, [P, c_char_p]),
        "iio_device_find_channel": (P, [P, c_char_p, c_bool]),
        "iio_channel_attr_write_longlong": (c_int, [P, c_char_p, c_longlong]),
        "iio_channel_attr_read_longlong": (c_int, [P, c_char_p, POINTER(c_longlong)]),
        "iio_channel_attr_write": (c_ssize_t, [P, c_char_p, c_char_p]),
        "iio_channel_attr_read": (c_ssize_t, [P, c_char_p, c_char_p, c_size_t]),
        "iio_channel_attr_write_double": (c_int, [P, c_char_p, c_double]),
        "iio_channel_attr_read_double": (c_int, [P, c_char_p, POINTER(c_double)]),
        "iio_channel_enable": (None, [P]),
        "iio_device_create_buffer": (P, [P, c_size_t, c_bool]),
        "iio_buffer_destroy": (None, [P]),
        "iio_buffer_refill": (c_ssize_t, [P]),
        "iio_buffer_start": (P, [P]),
        "iio_buffer_end": (P, [P]),
        "iio_buffer_step": (c_ssize_t, [P]),
        "iio_strerror": (None, [c_int, c_char_p, c_size_t]),
    })
    return lib


def _iio_err(lib, code):
    buf = ctypes.create_string_buffer(200)
    lib.iio_strerror(abs(int(code)), buf, len(buf))
    return _text(buf.value) or f"error {code}"


def _iio_serial(text):
    """The serial in a libiio description, "" when it has none: a Pluto whose serial
    could not be read is listed as "... serial=" with nothing after it."""
    words = text.rpartition("serial=")[2].split() if "serial=" in text else []
    return words[0].strip(",") if words else ""


def _pluto_list(lib):
    """(description, uri, serial) of each Pluto on USB."""
    out = []
    scan = lib.iio_create_scan_context(b"usb", 0)
    if not scan:
        return out
    info = POINTER(P)()
    n = lib.iio_scan_context_get_info_list(scan, byref(info))
    for i in range(max(int(n), 0)):
        text = _text(lib.iio_context_info_get_description(info[i]))
        uri = _text(lib.iio_context_info_get_uri(info[i]))
        if "0456:b673" in text.lower() or "pluto" in text.lower():
            out.append((text, uri, _iio_serial(text)))
    if n >= 0:
        lib.iio_context_info_list_free(info)
    lib.iio_scan_context_destroy(scan)
    return out


def pluto_find(lib):
    _iio(lib)
    return [{"label": "ADALM-Pluto", "serial": s} for _, _, s in _pluto_list(lib)]


def pluto_rx(lib, a, say, out):
    import numpy as np
    _iio(lib)
    dev_id = a.dev or ""
    if dev_id.startswith(("usb:", "ip:")):
        uri = dev_id                                # an address given by hand
    else:
        units = _pluto_list(lib)
        pick = [u for u in units if not dev_id or u[2] == dev_id]
        if not pick:
            raise Fail("no ADALM-Pluto found" + (f" with serial {dev_id}" if dev_id else ""))
        uri = pick[0][1]
    ctx = lib.iio_create_context_from_uri(uri.encode())
    if not ctx:
        raise Fail(f"the Pluto at {uri} did not open: {_iio_err(lib, ctypes.get_errno())}")
    buf = None
    try:
        lib.iio_context_set_timeout(ctx, 5000)
        phy = lib.iio_context_find_device(ctx, b"ad9361-phy")
        rxd = lib.iio_context_find_device(ctx, b"cf-ad9361-lpc")
        if not phy or not rxd:
            raise Fail("this is not a Pluto: it has no AD936x receiver")
        lo = lib.iio_device_find_channel(phy, b"altvoltage0", True)
        rf = lib.iio_device_find_channel(phy, b"voltage0", False)
        ci = lib.iio_device_find_channel(rxd, b"voltage0", False)
        cq = lib.iio_device_find_channel(rxd, b"voltage1", False)
        if not (lo and rf and ci and cq):
            raise Fail("this is not a Pluto: a receive channel is missing")

        def must(code, what):
            if code < 0:
                raise Fail(f"{what}: {_iio_err(lib, code)}")

        must(lib.iio_channel_attr_write_longlong(rf, b"sampling_frequency", int(a.rate)),
             f"sample rate {a.rate:.0f}")
        got = c_longlong(0)
        must(lib.iio_channel_attr_read_longlong(rf, b"sampling_frequency", byref(got)),
             "reading the sample rate")
        if not _rate_or_fail(say, a.rate, float(got.value)):
            return 3
        must(lib.iio_channel_attr_write_longlong(rf, b"rf_bandwidth", int(a.rate)),
             "filter width")
        must(lib.iio_channel_attr_write_longlong(lo, b"frequency", int(a.freq)), "frequency")
        # automatic gain off: the level must hold
        must(lib.iio_channel_attr_write(rf, b"gain_control_mode", b"manual"), "manual gain")
        top = 73.0
        text = ctypes.create_string_buffer(200)
        if lib.iio_channel_attr_read(rf, b"hardwaregain_available", text, len(text)) > 0:
            nums = _text(text.value).strip("[]").split()
            if len(nums) == 3:
                top = float(nums[2])
        want = min(a.gains.get("gain", top), top)
        must(lib.iio_channel_attr_write_double(rf, b"hardwaregain", float(want)), "gain")
        set_to = c_double(0)
        must(lib.iio_channel_attr_read_double(rf, b"hardwaregain", byref(set_to)),
             "reading the gain")
        say(f"gains set: gain={set_to.value:g}")
        say("bias tee: none on this receiver")
        lib.iio_channel_enable(ci)
        lib.iio_channel_enable(cq)
        buf = lib.iio_device_create_buffer(rxd, CHUNK, False)
        if not buf:
            raise Fail(f"no sample buffer: {_iio_err(lib, ctypes.get_errno())}")
        first = True
        while True:
            n = lib.iio_buffer_refill(buf)
            if n < 0:
                say(f"no samples from the receiver: {_iio_err(lib, n)}")
                return 4
            if lib.iio_buffer_step(buf) != 4:
                raise Fail("the Pluto sends samples in a form this does not read")
            start, end = lib.iio_buffer_start(buf), lib.iio_buffer_end(buf)
            if first:
                first = False                       # samples from before the tuning
                continue
            raw = np.frombuffer(ctypes.string_at(start, end - start), "<i2")
            if out.put(raw << 4):                   # 12 bits in the low end of the word
                return 0
    finally:
        if buf:
            lib.iio_buffer_destroy(buf)
        lib.iio_context_destroy(ctx)


# ---- bladeRF, libbladeRF

class _BrfInfo(ctypes.Structure):
    _fields_ = [("backend", c_int), ("serial", c_char * 33), ("usb_bus", c_uint8),
                ("usb_addr", c_uint8), ("instance", c_uint), ("manufacturer", c_char * 33),
                ("product", c_char * 33)]


class _BrfRange(ctypes.Structure):
    _fields_ = [("min", c_int64), ("max", c_int64), ("step", c_int64), ("scale", c_float)]


BRF_RX0 = 0                 # BLADERF_CHANNEL_RX(0)
BRF_MGC = 1                 # BLADERF_GAIN_MGC: manual gain
BRF_NODEV = -7


def _brf(lib):
    R = POINTER(POINTER(_BrfRange))
    _declare(lib, {
        "bladerf_get_device_list": (c_int, [POINTER(POINTER(_BrfInfo))]),
        "bladerf_free_device_list": (None, [POINTER(_BrfInfo)]),
        "bladerf_open": (c_int, [POINTER(P), c_char_p]),
        "bladerf_close": (None, [P]),
        "bladerf_get_board_name": (c_char_p, [P]),
        "bladerf_strerror": (c_char_p, [c_int]),
        "bladerf_set_frequency": (c_int, [P, c_int, c_uint64]),
        "bladerf_set_sample_rate": (c_int, [P, c_int, c_uint, POINTER(c_uint)]),
        "bladerf_set_bandwidth": (c_int, [P, c_int, c_uint, POINTER(c_uint)]),
        "bladerf_set_gain_mode": (c_int, [P, c_int, c_int]),
        "bladerf_set_gain": (c_int, [P, c_int, c_int]),
        "bladerf_get_gain": (c_int, [P, c_int, POINTER(c_int)]),
        "bladerf_get_gain_range": (c_int, [P, c_int, R]),
        "bladerf_get_gain_stages": (c_int, [P, c_int, POINTER(c_char_p), c_size_t]),
        "bladerf_get_gain_stage_range": (c_int, [P, c_int, c_char_p, R]),
        "bladerf_set_gain_stage": (c_int, [P, c_int, c_char_p, c_int]),
        "bladerf_get_gain_stage": (c_int, [P, c_int, c_char_p, POINTER(c_int)]),
        "bladerf_set_bias_tee": (c_int, [P, c_int, c_bool]),
        "bladerf_sync_config": (c_int, [P, c_int, c_int, c_uint, c_uint, c_uint, c_uint]),
        "bladerf_enable_module": (c_int, [P, c_int, c_bool]),
        "bladerf_sync_rx": (c_int, [P, c_void_p, c_uint, c_void_p, c_uint]),
    })
    return lib


def bladerf_find(lib):
    _brf(lib)
    lst = POINTER(_BrfInfo)()
    n = lib.bladerf_get_device_list(byref(lst))
    out = [{"label": _text(lst[i].product) or "bladeRF", "serial": _text(lst[i].serial)}
           for i in range(max(n, 0))]
    if n > 0:
        lib.bladerf_free_device_list(lst)
    return out


def _brf_top(rng):
    r = rng.contents
    return int(round(r.max * (r.scale or 1.0)))


def bladerf_rx(lib, a, say, out):
    import numpy as np
    _brf(lib)

    def must(code, what):
        if code < 0:
            raise Fail(f"{what}: {_text(lib.bladerf_strerror(code))}")

    dev = P()
    ident = f"*:serial={a.dev}" if a.dev else ""
    code = lib.bladerf_open(byref(dev), ident.encode())
    if code == BRF_NODEV:
        raise Fail("no bladeRF found" + (f" with serial {a.dev}" if a.dev else ""))
    must(code, "the bladeRF did not open")
    on = False
    try:
        got = c_uint(0)
        must(lib.bladerf_set_sample_rate(dev, BRF_RX0, int(a.rate), byref(got)),
             f"sample rate {a.rate:.0f}")
        if not _rate_or_fail(say, a.rate, float(got.value)):
            return 3
        must(lib.bladerf_set_bandwidth(dev, BRF_RX0, int(a.rate), byref(got)), "filter width")
        must(lib.bladerf_set_frequency(dev, BRF_RX0, int(a.freq)), "frequency")
        if lib.bladerf_set_gain_mode(dev, BRF_RX0, BRF_MGC) < 0:
            say("automatic gain not switched off: this bladeRF has no such switch")
        rng = POINTER(_BrfRange)()
        must(lib.bladerf_get_gain_range(dev, BRF_RX0, byref(rng)), "gain range")
        top = _brf_top(rng)
        want = a.gains.get("gain")
        names = (c_char_p * 16)()
        count = max(lib.bladerf_get_gain_stages(dev, BRF_RX0, names, 16), 0)
        stages = [names[i] for i in range(min(count, 16)) if names[i]]
        if want is None or want >= top:
            # full gain: every stage to its own top, by name
            for st in stages:
                must(lib.bladerf_get_gain_stage_range(dev, BRF_RX0, st, byref(rng)),
                     f"range of {_text(st)}")
                must(lib.bladerf_set_gain_stage(dev, BRF_RX0, st, _brf_top(rng)),
                     f"gain of {_text(st)}")
            if not stages:
                must(lib.bladerf_set_gain(dev, BRF_RX0, top), "gain")
        else:
            must(lib.bladerf_set_gain(dev, BRF_RX0, int(round(want))), "gain")
        told = []
        val = c_int(0)
        if lib.bladerf_get_gain(dev, BRF_RX0, byref(val)) >= 0:
            told.append(f"gain={val.value}")
        for st in stages:
            if lib.bladerf_get_gain_stage(dev, BRF_RX0, st, byref(val)) >= 0:
                told.append(f"{_text(st)}={val.value}")
        say("gains set: " + (", ".join(told) or "none"))
        if lib.bladerf_set_bias_tee(dev, BRF_RX0, bool(a.bias)) < 0:
            say("bias tee: none on this receiver")
        else:
            say(f"bias tee: {'on' if a.bias else 'off'}")
        # 16 buffers of 8192 samples, 8 in flight; SC16 Q11 words
        must(lib.bladerf_sync_config(dev, 0, 0, 16, 8192, 8, 3500), "stream set-up")
        must(lib.bladerf_enable_module(dev, BRF_RX0, True), "switching the receiver on")
        on = True
        block = np.empty(2 * 8192, np.int16)
        while True:
            code = lib.bladerf_sync_rx(dev, block.ctypes.data, 8192, None, int(QUIET_S * 1000))
            if code < 0:
                say(f"no samples from the receiver: {_text(lib.bladerf_strerror(code))}")
                return 4
            if out.put(block << 4):                 # 12 bits in the low end of the word
                return 0
    finally:
        if on:
            lib.bladerf_enable_module(dev, BRF_RX0, False)
        lib.bladerf_close(dev)


# ---- LimeSDR, LimeSuite

class _LmsStream(ctypes.Structure):
    _fields_ = [("handle", c_size_t), ("isTx", c_bool), ("channel", c_uint32),
                ("fifoSize", c_uint32), ("throughputVsLatency", c_float),
                ("dataFmt", c_int), ("linkFmt", c_int)]


LMS_RX = False
LMS_PATH_AUTO = 255
LMS_FMT_I16 = 1
_LmsInfo = c_char * 256
_LmsName = c_char * 16


def _lms(lib):
    _declare(lib, {
        "LMS_GetDeviceList": (c_int, [POINTER(_LmsInfo)]),
        "LMS_Open": (c_int, [POINTER(P), c_char_p, c_void_p]),
        "LMS_Close": (c_int, [P]),
        "LMS_Init": (c_int, [P]),
        "LMS_EnableChannel": (c_int, [P, c_bool, c_size_t, c_bool]),
        "LMS_SetSampleRate": (c_int, [P, c_double, c_size_t]),
        "LMS_GetSampleRate": (c_int, [P, c_bool, c_size_t, POINTER(c_double),
                                      POINTER(c_double)]),
        "LMS_SetLOFrequency": (c_int, [P, c_bool, c_size_t, c_double]),
        "LMS_GetAntennaList": (c_int, [P, c_bool, c_size_t, POINTER(_LmsName)]),
        "LMS_SetAntenna": (c_int, [P, c_bool, c_size_t, c_size_t]),
        "LMS_GetAntenna": (c_int, [P, c_bool, c_size_t]),
        "LMS_SetGaindB": (c_int, [P, c_bool, c_size_t, c_uint]),
        "LMS_GetGaindB": (c_int, [P, c_bool, c_size_t, POINTER(c_uint)]),
        "LMS_SetLPFBW": (c_int, [P, c_bool, c_size_t, c_double]),
        "LMS_Calibrate": (c_int, [P, c_bool, c_size_t, c_double, c_uint]),
        "LMS_SetupStream": (c_int, [P, POINTER(_LmsStream)]),
        "LMS_DestroyStream": (c_int, [P, POINTER(_LmsStream)]),
        "LMS_StartStream": (c_int, [POINTER(_LmsStream)]),
        "LMS_StopStream": (c_int, [POINTER(_LmsStream)]),
        "LMS_RecvStream": (c_int, [POINTER(_LmsStream), c_void_p, c_size_t, c_void_p, c_uint]),
        "LMS_GetLastErrorMessage": (c_char_p, []),
    })
    return lib


def _lms_list(lib):
    n = lib.LMS_GetDeviceList(None)
    if n <= 0:
        return []
    lst = (_LmsInfo * n)()
    n = lib.LMS_GetDeviceList(lst)
    return [_text(lst[i].value) for i in range(max(n, 0))]


def _lms_serial(info):
    for part in info.split(","):
        k, _, v = part.strip().partition("=")
        if k == "serial":
            return v.strip()
    return ""


def limesdr_find(lib):
    _lms(lib)
    return [{"label": info.split(",")[0].strip() or "LimeSDR", "serial": _lms_serial(info)}
            for info in _lms_list(lib)]


LMS_TOP_DB = 73


def limesdr_rx(lib, a, say, out):
    import numpy as np
    _lms(lib)

    def must(code, what):
        if code < 0:
            raise Fail(f"{what}: {_text(lib.LMS_GetLastErrorMessage())}")

    units = [u for u in _lms_list(lib) if not a.dev or _lms_serial(u) == a.dev]
    if not units:
        raise Fail("no LimeSDR found" + (f" with serial {a.dev}" if a.dev else ""))
    dev = P()
    must(lib.LMS_Open(byref(dev), units[0].encode(), None), "the LimeSDR did not open")
    stream = None
    try:
        must(lib.LMS_Init(dev), "start-up")
        must(lib.LMS_EnableChannel(dev, LMS_RX, 0, True), "switching the receiver on")
        must(lib.LMS_SetSampleRate(dev, float(a.rate), 0), f"sample rate {a.rate:.0f}")
        host, rf = c_double(0), c_double(0)
        must(lib.LMS_GetSampleRate(dev, LMS_RX, 0, byref(host), byref(rf)),
             "reading the sample rate")
        if not _rate_or_fail(say, a.rate, host.value):
            return 3
        must(lib.LMS_SetLOFrequency(dev, LMS_RX, 0, float(a.freq)), "frequency")
        # the input that suits this frequency, chosen by the library where it can
        if lib.LMS_SetAntenna(dev, LMS_RX, 0, LMS_PATH_AUTO) < 0:
            say("input not chosen by frequency: the receiver's default input is used")
        ant = lib.LMS_GetAntenna(dev, LMS_RX, 0)
        names = (_LmsName * 16)()
        count = lib.LMS_GetAntennaList(dev, LMS_RX, 0, names)
        if 0 <= ant < min(count, 16):
            say(f"input: {_text(names[ant].value)}")
        want = int(round(min(max(a.gains.get("gain", LMS_TOP_DB), 0), LMS_TOP_DB)))
        must(lib.LMS_SetGaindB(dev, LMS_RX, 0, want), "gain")
        got = c_uint(0)
        must(lib.LMS_GetGaindB(dev, LMS_RX, 0, byref(got)), "reading the gain")
        say(f"gains set: gain={got.value}")
        say("bias tee: none on this receiver")
        width = max(float(a.rate), 1.5e6)
        must(lib.LMS_SetLPFBW(dev, LMS_RX, 0, width), "filter width")
        if lib.LMS_Calibrate(dev, LMS_RX, 0, max(width, 2.5e6), 0) < 0:
            say(f"not calibrated: {_text(lib.LMS_GetLastErrorMessage())}")
        stream = _LmsStream(0, False, 0, 1 << 20, 1.0, LMS_FMT_I16, 0)
        must(lib.LMS_SetupStream(dev, byref(stream)), "stream set-up")
        must(lib.LMS_StartStream(byref(stream)), "stream start")
        block = np.empty(2 * CHUNK, np.int16)
        quiet = time.monotonic()
        while True:
            n = lib.LMS_RecvStream(byref(stream), block.ctypes.data, CHUNK, None, 1000)
            if n < 0:
                say(f"no samples from the receiver: {_text(lib.LMS_GetLastErrorMessage())}")
                return 4
            if n == 0:
                if time.monotonic() - quiet > QUIET_S:
                    say("no samples from the receiver")
                    return 4
                continue
            quiet = time.monotonic()
            if out.put(block[:2 * n]):              # already full scale
                return 0
    finally:
        if stream is not None:
            lib.LMS_StopStream(byref(stream))
            lib.LMS_DestroyStream(dev, byref(stream))
        lib.LMS_Close(dev)


# ---- SDRplay RSP, sdrplay_api 3

class _SpDevice(ctypes.Structure):
    _fields_ = [("SerNo", c_char * 64), ("hwVer", c_ubyte), ("tuner", c_int),
                ("rspDuoMode", c_int), ("valid", c_ubyte), ("rspDuoSampleFreq", c_double),
                ("dev", c_void_p)]


class _SpFs(ctypes.Structure):
    _fields_ = [("fsHz", c_double), ("syncUpdate", c_ubyte), ("reCal", c_ubyte)]


class _SpSync(ctypes.Structure):
    _fields_ = [("sampleNum", c_uint), ("period", c_uint)]


class _SpReset(ctypes.Structure):
    _fields_ = [("resetGainUpdate", c_ubyte), ("resetRfUpdate", c_ubyte),
                ("resetFsUpdate", c_ubyte)]


class _SpRsp1a(ctypes.Structure):
    _fields_ = [("rfNotchEnable", c_ubyte), ("rfDabNotchEnable", c_ubyte)]


class _SpRsp2(ctypes.Structure):
    _fields_ = [("extRefOutputEn", c_ubyte)]


class _SpDuo(ctypes.Structure):
    _fields_ = [("extRefOutputEn", c_int)]


class _SpDx(ctypes.Structure):
    _fields_ = [("hdrEnable", c_ubyte), ("biasTEnable", c_ubyte), ("antennaSel", c_int),
                ("rfNotchEnable", c_ubyte), ("rfDabNotchEnable", c_ubyte)]


class _SpDevParams(ctypes.Structure):
    _fields_ = [("ppm", c_double), ("fsFreq", _SpFs), ("syncUpdate", _SpSync),
                ("resetFlags", _SpReset), ("mode", c_int), ("samplesPerPkt", c_uint),
                ("rsp1aParams", _SpRsp1a), ("rsp2Params", _SpRsp2), ("rspDuoParams", _SpDuo),
                ("rspDxParams", _SpDx)]


class _SpGainVals(ctypes.Structure):
    _fields_ = [("curr", c_float), ("max", c_float), ("min", c_float)]


class _SpGain(ctypes.Structure):
    _fields_ = [("gRdB", c_int), ("LNAstate", c_ubyte), ("syncUpdate", c_ubyte),
                ("minGr", c_int), ("gainVals", _SpGainVals)]


class _SpRf(ctypes.Structure):
    _fields_ = [("rfHz", c_double), ("syncUpdate", c_ubyte)]


class _SpDcTuner(ctypes.Structure):
    _fields_ = [("dcCal", c_ubyte), ("speedUp", c_ubyte), ("trackTime", c_int),
                ("refreshRateTime", c_int)]


class _SpTuner(ctypes.Structure):
    _fields_ = [("bwType", c_int), ("ifType", c_int), ("loMode", c_int), ("gain", _SpGain),
                ("rfFreq", _SpRf), ("dcOffsetTuner", _SpDcTuner)]


class _SpDc(ctypes.Structure):
    _fields_ = [("DCenable", c_ubyte), ("IQenable", c_ubyte)]


class _SpDecim(ctypes.Structure):
    _fields_ = [("enable", c_ubyte), ("decimationFactor", c_ubyte),
                ("wideBandSignal", c_ubyte)]


class _SpAgc(ctypes.Structure):
    _fields_ = [("enable", c_int), ("setPoint_dBfs", c_int), ("attack_ms", c_ushort),
                ("decay_ms", c_ushort), ("decay_delay_ms", c_ushort),
                ("decay_threshold_dB", c_ushort), ("syncUpdate", c_int)]


class _SpCtrl(ctypes.Structure):
    _fields_ = [("dcOffset", _SpDc), ("decimation", _SpDecim), ("agc", _SpAgc),
                ("adsbMode", c_int)]


class _SpRsp1aTuner(ctypes.Structure):
    _fields_ = [("biasTEnable", c_ubyte)]


class _SpRsp2Tuner(ctypes.Structure):
    _fields_ = [("biasTEnable", c_ubyte), ("amPortSel", c_int), ("antennaSel", c_int),
                ("rfNotchEnable", c_ubyte)]


class _SpDuoReset(ctypes.Structure):
    _fields_ = [("resetGainUpdate", c_ubyte), ("resetRfUpdate", c_ubyte)]


class _SpDuoTuner(ctypes.Structure):
    _fields_ = [("biasTEnable", c_ubyte), ("tuner1AmPortSel", c_int),
                ("tuner1AmNotchEnable", c_ubyte), ("rfNotchEnable", c_ubyte),
                ("rfDabNotchEnable", c_ubyte), ("resetSlaveFlags", _SpDuoReset)]


class _SpDxTuner(ctypes.Structure):
    _fields_ = [("hdrBw", c_int)]


class _SpRxChannel(ctypes.Structure):
    _fields_ = [("tunerParams", _SpTuner), ("ctrlParams", _SpCtrl),
                ("rsp1aTunerParams", _SpRsp1aTuner), ("rsp2TunerParams", _SpRsp2Tuner),
                ("rspDuoTunerParams", _SpDuoTuner), ("rspDxTunerParams", _SpDxTuner)]


class _SpParams(ctypes.Structure):
    _fields_ = [("devParams", POINTER(_SpDevParams)), ("rxChannelA", POINTER(_SpRxChannel)),
                ("rxChannelB", POINTER(_SpRxChannel))]


_SpStreamCb = ctypes.CFUNCTYPE(None, c_void_p, c_void_p, c_void_p, c_uint, c_uint, c_void_p)
_SpEventCb = ctypes.CFUNCTYPE(None, c_int, c_int, c_void_p, c_void_p)


class _SpCallbacks(ctypes.Structure):
    _fields_ = [("StreamACbFn", _SpStreamCb), ("StreamBCbFn", _SpStreamCb),
                ("EventCbFn", _SpEventCb)]


SP_MODELS = {1: "RSP1", 255: "RSP1A", 2: "RSP2", 3: "RSPduo", 4: "RSPdx", 6: "RSP1B",
             7: "RSPdx-R2"}
SP_DUO, SP_TUNER_A, SP_TUNER_B, SP_SINGLE = 3, 1, 2, 1
SP_BW = (8000, 7000, 6000, 5000, 1536, 600, 300, 200)      # kHz, the filters there are
SP_OVERLOAD, SP_REMOVED, SP_ACK = 1, 2, 0x04000000
SP_IF_TOP, SP_IF_LAST = 20, 59                              # dB of reduction: 20 is most gain


def _sp(lib):
    _declare(lib, {
        "sdrplay_api_Open": (c_int, []),
        "sdrplay_api_Close": (c_int, []),
        "sdrplay_api_ApiVersion": (c_int, [POINTER(c_float)]),
        "sdrplay_api_LockDeviceApi": (c_int, []),
        "sdrplay_api_UnlockDeviceApi": (c_int, []),
        "sdrplay_api_GetDevices": (c_int, [POINTER(_SpDevice), POINTER(c_uint), c_uint]),
        "sdrplay_api_SelectDevice": (c_int, [POINTER(_SpDevice)]),
        "sdrplay_api_ReleaseDevice": (c_int, [POINTER(_SpDevice)]),
        "sdrplay_api_GetErrorString": (c_char_p, [c_int]),
        "sdrplay_api_GetDeviceParams": (c_int, [c_void_p, POINTER(POINTER(_SpParams))]),
        "sdrplay_api_Init": (c_int, [c_void_p, POINTER(_SpCallbacks), c_void_p]),
        "sdrplay_api_Uninit": (c_int, [c_void_p]),
        "sdrplay_api_Update": (c_int, [c_void_p, c_int, c_uint, c_uint]),
    })
    return lib


def _sp_must(lib, code, what):
    if code != 0:
        raise Fail(f"{what}: {_text(lib.sdrplay_api_GetErrorString(code))}")


def _sp_open(lib):
    if lib.sdrplay_api_Open() != 0:
        raise Fail("the SDRplay API service is not running")


def _sp_devices(lib):
    devs = (_SpDevice * 16)()
    n = c_uint(0)
    _sp_must(lib, lib.sdrplay_api_GetDevices(devs, byref(n), 16), "listing receivers")
    return [devs[i] for i in range(min(n.value, 16))]


def sdrplay_find(lib):
    _sp(lib)
    _sp_open(lib)
    try:
        lib.sdrplay_api_LockDeviceApi()
        try:
            return [{"label": SP_MODELS.get(d.hwVer, "SDRplay RSP"), "serial": _text(d.SerNo)}
                    for d in _sp_devices(lib)]
        finally:
            lib.sdrplay_api_UnlockDeviceApi()
    finally:
        lib.sdrplay_api_Close()


def sdrplay_rx(lib, a, say, out):
    import collections

    import numpy as np
    _sp(lib)
    if not 2e6 <= a.rate <= 10.66e6:
        say(f"the receiver gives 2 to 10.66 million samples a second, not the {a.rate:.0f} "
            "asked for")
        return 3
    _sp_open(lib)
    dev = None
    started = False
    try:
        lib.sdrplay_api_LockDeviceApi()
        try:
            units = [d for d in _sp_devices(lib) if not a.dev or _text(d.SerNo) == a.dev]
            if not units:
                raise Fail("no SDRplay receiver found"
                           + (f" with serial {a.dev}" if a.dev else ""))
            pick = units[0]
            if pick.hwVer == SP_DUO:
                # one tuner alone; the bias tee of an RSPduo is on tuner 2
                pick.tuner = SP_TUNER_B if a.bias else SP_TUNER_A
                pick.rspDuoMode = SP_SINGLE
            _sp_must(lib, lib.sdrplay_api_SelectDevice(byref(pick)),
                     "the receiver did not open")
            dev = pick
        finally:
            lib.sdrplay_api_UnlockDeviceApi()
        pp = POINTER(_SpParams)()
        _sp_must(lib, lib.sdrplay_api_GetDeviceParams(dev.dev, byref(pp)), "its settings")
        tuner_b = dev.hwVer == SP_DUO and dev.tuner == SP_TUNER_B
        chp = pp.contents.rxChannelB if tuner_b else pp.contents.rxChannelA
        if not chp:
            raise Fail("the receiver gave no settings for its tuner")
        ch = chp.contents
        if pp.contents.devParams:
            pp.contents.devParams.contents.fsFreq.fsHz = float(a.rate)
        ch.tunerParams.rfFreq.rfHz = float(a.freq)
        ch.tunerParams.bwType = next((b for b in SP_BW if b * 1000 <= a.rate), SP_BW[-1])
        ch.tunerParams.ifType = 0                   # zero IF
        ch.ctrlParams.agc.enable = 0                # automatic gain off: the level must hold
        lna = int(min(max(a.gains.get("lna", 0), 0), 27))
        ifgr = int(min(max(a.gains.get("if", SP_IF_TOP), SP_IF_TOP), SP_IF_LAST))
        ch.tunerParams.gain.LNAstate = lna
        ch.tunerParams.gain.gRdB = ifgr
        bias = 1 if a.bias else 0
        model = SP_MODELS.get(dev.hwVer, "")
        note = f"bias tee: {'on' if bias else 'off'}"
        if model in ("RSP1A", "RSP1B"):
            ch.rsp1aTunerParams.biasTEnable = bias
        elif model == "RSP2":
            ch.rsp2TunerParams.biasTEnable = bias
            if bias:
                ch.rsp2TunerParams.antennaSel = 6   # the bias tee is on antenna B
                note += " (antenna B)"
        elif model == "RSPduo":
            ch.rspDuoTunerParams.biasTEnable = bias if tuner_b else 0
            note += " (tuner 2)" if tuner_b else ""
        elif model in ("RSPdx", "RSPdx-R2") and pp.contents.devParams:
            dx = pp.contents.devParams.contents.rspDxParams
            dx.biasTEnable = bias
            if bias:
                dx.antennaSel = 1                   # the bias tee is on antenna B
                note += " (antenna B)"
        else:
            note = "bias tee: none on this receiver"

        blocks = collections.deque()
        state = {"overloads": 0, "gone": False}
        handle = dev.dev

        def on_samples(xi, xq, _params, n, _reset, _ctx):
            # the library's own thread: only copy, the main thread does the rest
            if n:
                blocks.append((ctypes.string_at(xi, 2 * n), ctypes.string_at(xq, 2 * n)))

        def on_event(event, which, params, _ctx):
            if event == SP_OVERLOAD:
                if params and ctypes.cast(params, POINTER(c_int))[0] == 0:
                    state["overloads"] += 1
                lib.sdrplay_api_Update(handle, which, SP_ACK, 0)     # it waits for this
            elif event == SP_REMOVED:
                state["gone"] = True

        # one tuner alone comes on stream A, whichever tuner it is; both are taken
        stream_cb = _SpStreamCb(on_samples)
        cbs = _SpCallbacks(stream_cb, stream_cb, _SpEventCb(on_event))
        code = lib.sdrplay_api_Init(handle, byref(cbs), None)
        if code in (3, 4) and lna:
            raise Fail(f"the receiver did not start: LNA reduction {lna} may be past its "
                       "last step at this frequency")
        _sp_must(lib, code, "the receiver did not start")
        started = True
        g = ch.tunerParams.gain
        say(f"gains set: lna={g.LNAstate}, if={g.gRdB}, total={g.gainVals.curr:g}")
        say(note)
        quiet = time.monotonic()
        code = 0
        while True:
            if not blocks:
                if state["gone"] or time.monotonic() - quiet > QUIET_S:
                    say("no samples from the receiver")
                    code = 4
                    break
                time.sleep(0.002)
                continue
            quiet = time.monotonic()
            i, q = blocks.popleft()
            iq = np.empty(len(i), np.int16)         # len(i) bytes: two int16 per sample
            iq[0::2] = np.frombuffer(i, np.int16)
            iq[1::2] = np.frombuffer(q, np.int16)
            if out.put(iq):
                break
        if state["overloads"]:
            say(f"overloaded {state['overloads']} times: the gain is too high for this signal")
        return code
    finally:
        if dev is not None:
            if started:
                lib.sdrplay_api_Uninit(dev.dev)
            lib.sdrplay_api_ReleaseDevice(byref(dev))
        lib.sdrplay_api_Close()


RECEIVERS = {"pluto": (pluto_find, pluto_rx), "bladerf": (bladerf_find, bladerf_rx),
             "limesdr": (limesdr_find, limesdr_rx), "sdrplay": (sdrplay_find, sdrplay_rx)}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vendor_rx")
    ap.add_argument("name", choices=sorted(RECEIVERS))
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("find")
    r = sub.add_parser("rx")
    r.add_argument("--dev", default="")
    r.add_argument("--freq", type=float, required=True)
    r.add_argument("--rate", type=float, required=True)
    r.add_argument("--n", type=int, required=True)
    r.add_argument("--out", default="-")
    r.add_argument("--gain", action="append")
    r.add_argument("--bias", type=int, default=0)
    a = ap.parse_args(argv)

    def say(*x):
        # by file number: a windowed app has no sys.stderr, but it is handed this one
        with contextlib.suppress(OSError):
            os.write(2, (" ".join(str(v) for v in x) + "\n").encode("utf-8", "replace"))

    find, rx = RECEIVERS[a.name]
    fh = None
    try:
        if a.verb == "rx" and a.out == "-":
            # the samples get the real stdout to themselves: anything a library prints
            # there would land among them, so that is sent to stderr
            if sys.stdout is not None:
                sys.stdout.flush()
            fh = os.fdopen(os.dup(1), "wb")
            os.dup2(2, 1)
        lib = load(a.name)
        if a.verb == "find":
            os.write(1, (json.dumps(find(lib)) + "\n").encode())
            return 0
        a.gains = {}
        for item in a.gain or []:
            k, _, v = item.partition("=")
            a.gains[k] = float(v)
        if fh is None:
            fh = open(a.out, "wb")
        code = rx(lib, a, say, Out(fh, a.n))
        fh.flush()
        return code
    except Fail as e:
        say(str(e))
        return 1
    except BrokenPipeError:
        return 0                                    # the node stopped reading
    except Exception as e:
        say(f"{type(e).__name__}: {e}")
        return 1
    finally:
        if fh is not None:
            with contextlib.suppress(Exception):
                fh.close()


if __name__ == "__main__":
    sys.exit(main())
