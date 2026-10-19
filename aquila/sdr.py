# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# GPL-3.0-or-later: free to use and modify, derived works must stay open.
"""SDR sources. Each one runs the vendor's command line tool and streams raw IQ
through a pipe. Raw IQ is never written to disk. A source knows how to start a
capture of N samples and how to turn one raw frame into complex64.

Adding a device = one small class here. Nothing else in the app changes."""
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np

EXTRA_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin",
              os.path.expanduser("~/.local/bin"))


def which(tool):
    """shutil.which plus the places vendor tools live when the app is started from
    Finder, the Dock or a desktop launcher, which do not inherit the shell PATH.
    A 'tools' folder next to the app (for bundled installers) is checked first."""
    if not tool:
        return None
    exe_dir = os.path.dirname(os.path.abspath(sys.argv[0] or "."))
    dirs = [os.path.join(exe_dir, "tools")]
    if getattr(sys, "frozen", False):
        dirs.append(os.path.join(getattr(sys, "_MEIPASS", exe_dir), "tools"))
    path = os.pathsep.join(dirs + [os.environ.get("PATH", "")] + list(EXTRA_DIRS))
    return shutil.which(tool, path=path)


class _NamedPipe:
    """Server end of a Windows named pipe; the SDR tool writes to its path."""
    PIPE_ACCESS_INBOUND = 0x1
    ERROR_PIPE_CONNECTED = 535

    def __init__(self, bufsize):
        import ctypes
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k.CreateNamedPipeW.restype = ctypes.c_void_p
        self.k.CreateNamedPipeW.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                            ctypes.c_uint32, ctypes.c_void_p)
        self.k.ConnectNamedPipe.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        self.k.CreateFileW.restype = ctypes.c_void_p
        self.k.CreateFileW.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                       ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                       ctypes.c_void_p)
        self.k.CloseHandle.argtypes = (ctypes.c_void_p,)
        self.path = rf"\\.\pipe\aquila-{os.getpid()}-{id(self)}"
        h = self.k.CreateNamedPipeW(self.path, self.PIPE_ACCESS_INBOUND, 0, 1,
                                    0, bufsize, 0, None)
        if h is None or h == 2 ** 64 - 1:
            raise OSError(ctypes.get_last_error(), "CreateNamedPipe failed")
        self.handle = h

    def connect(self, proc):
        """Wait for the tool to open the pipe, then return a binary file object.
        If the tool dies first, the pipe is completed from this side so the
        reader sees a clean end of stream instead of waiting forever."""
        import ctypes
        import msvcrt
        done = threading.Event()
        ok = [False]

        def wait():
            r = self.k.ConnectNamedPipe(self.handle, None)
            ok[0] = bool(r) or ctypes.get_last_error() == self.ERROR_PIPE_CONNECTED
            done.set()
        threading.Thread(target=wait, daemon=True).start()
        while not done.wait(0.2):
            if proc.poll() is not None:
                GENERIC_WRITE, OPEN_EXISTING = 0x40000000, 3
                c = self.k.CreateFileW(self.path, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
                done.wait(2.0)
                if c not in (None, 2 ** 64 - 1):
                    self.k.CloseHandle(c)
                break
        fd = msvcrt.open_osfhandle(self.handle, os.O_RDONLY)
        return os.fdopen(fd, "rb", buffering=0)


class Source:
    name = "base"
    label = "base"
    tool = ""
    install = ""
    bytes_per_sample = 4
    raw_format = "int16 IQ interleaved"
    default_rate = 2_500_000
    rates = ()
    preset = {}

    def __init__(self, cfg):
        self.cfg = cfg
        self.proc = None
        adv = cfg["advanced"]
        self.rate = int(cfg["sample_rate"] or self.default_rate)
        self.center_hz = int(round(cfg["center_mhz"] * 1e6))
        self.bias = bool(adv.get("bias_tee", True))
        g = adv.get("gain", "preset")
        self.gain = dict(self.preset) if g == "preset" else dict(g)

    @classmethod
    def available(cls):
        return which(cls.tool) is not None

    def command(self, nsamples):
        raise NotImplementedError

    def start(self, nsamples, frame_samples):
        if self.proc is not None:
            self.kill()
        cmd = self.command(nsamples)
        cmd[0] = which(cmd[0]) or cmd[0]
        self.errf = tempfile.TemporaryFile()
        bufsize = frame_samples * self.bytes_per_sample * 4
        if sys.platform != "win32":
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=self.errf,
                                         bufsize=bufsize)
            return self.proc
        # The Windows builds of the vendor tools leave stdout in text mode, so a
        # stream sent to "-" grows a 0x0D before every 0x0A byte and the IQ pairs
        # drift out of step. A named pipe is opened as a file, in binary mode.
        pipe = _NamedPipe(bufsize)
        cmd = [pipe.path if a in ("-", "/dev/stdout") else a for a in cmd]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=self.errf,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        self.proc.stdout = pipe.connect(self.proc)
        return self.proc

    def to_complex(self, raw):
        s = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        return s[0::2] + 1j * s[1::2]

    STALL_S = 20.0      # no samples this long: the tool is alive but the radio is not

    def frames(self, frame_samples):
        """Yield raw frames of frame_samples until the tool stops. A tool that hangs
        (USB stall, radio pulled without the tool exiting) is killed, which ends the
        pipe, so the caller sees an early end instead of waiting forever."""
        need = frame_samples * self.bytes_per_sample
        buf = bytearray()
        proc = self.proc
        out = proc.stdout
        self.stalled = False
        last = [time.monotonic()]
        done = threading.Event()

        def watch():
            while not done.wait(1.0):
                if proc.poll() is not None or proc is not self.proc:
                    return                      # finished, or a newer capture took over
                if time.monotonic() - last[0] > self.STALL_S:
                    self.stalled = True
                    with contextlib.suppress(Exception):
                        proc.kill()
                    return
        threading.Thread(target=watch, daemon=True).start()
        try:
            while True:
                while len(buf) < need:
                    chunk = out.read(need - len(buf))
                    if not chunk:
                        return
                    last[0] = time.monotonic()
                    buf.extend(chunk)
                yield bytes(buf[:need])
                del buf[:need]
                last[0] = time.monotonic()      # time spent by the caller is not a stall
        finally:
            done.set()

    def finish(self):
        p = self.proc
        if p is None:
            return ""
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
            with contextlib.suppress(Exception):
                p.wait(timeout=5)
        with contextlib.suppress(Exception):
            p.stdout.read()
        try:
            self.errf.seek(0)
            err = self.errf.read().decode(errors="replace")
            self.errf.close()
        except Exception:
            err = ""
        self.proc = None
        if getattr(self, "stalled", False):
            self.stalled = False
            err = f"no samples for {self.STALL_S:.0f} s, tool stopped. " + err
        return err

    def kill(self):
        if self.proc is not None:
            with contextlib.suppress(Exception):
                self.proc.kill()
                self.proc.wait(timeout=5)
            self.proc = None
        errf = getattr(self, "errf", None)
        if errf is not None:
            with contextlib.suppress(Exception):
                errf.close()
            self.errf = None


class Airspy(Source):
    """airspy_rx, INT16 IQ. Mini: 3 or 6 Msps. R2: 2.5 or 10 Msps."""
    name = "airspy"
    label = "Airspy (Mini, R2)"
    tool = "airspy_rx"
    install = "airspy host tools (airspy_rx)"
    default_rate = 3_000_000
    rates = (2_500_000, 3_000_000, 6_000_000, 10_000_000)
    preset = {"lna": 14, "mixer": 15, "vga": 15}   # maximum, LNA range is 0-14

    def command(self, nsamples):
        g = self.gain
        # /dev/stdout is the path proven on the Pi nodes; "-" is what Windows builds accept
        out = "-" if sys.platform == "win32" else "/dev/stdout"
        return [self.tool, "-r", out, "-f", "%.6f" % (self.center_hz / 1e6),
                "-a", str(self.rate), "-t", "2", "-b", "1" if self.bias else "0",
                "-l", str(g["lna"]), "-m", str(g["mixer"]), "-v", str(g["vga"]),
                "-n", str(nsamples)]


class HydraSDR(Airspy):
    """hydrasdr_rx, same flags as airspy_rx. LNA max is 14. The RFOne is
    Airspy-compatible (same USB id and library), so airspy_rx drives it too:
    that is how the Windows node ran it. Either tool will do."""
    name = "hydrasdr"
    label = "HydraSDR"
    tool = "hydrasdr_rx"
    alt_tools = ("airspy_rx",)
    install = "hydrasdr_rx or airspy_rx"

    @classmethod
    def found_tool(cls):
        for t in (cls.tool,) + cls.alt_tools:
            if which(t):
                return t
        return None

    @classmethod
    def available(cls):
        return cls.found_tool() is not None

    def command(self, nsamples):
        cmd = super().command(nsamples)
        cmd[0] = self.found_tool() or self.tool
        return cmd
    default_rate = 5_000_000
    rates = (2_500_000, 5_000_000, 10_000_000)
    preset = {"lna": 14, "mixer": 15, "vga": 15}


class RtlSdr(Source):
    """rtl_sdr, UINT8 IQ. Bias tee needs rtl_biast, run once before start."""
    name = "rtlsdr"
    label = "RTL-SDR"
    tool = "rtl_sdr"
    install = "rtl-sdr tools (rtl_sdr)"
    bytes_per_sample = 2
    raw_format = "uint8 IQ interleaved"
    default_rate = 2_400_000
    rates = (1_024_000, 2_048_000, 2_400_000, 2_560_000, 3_200_000)
    preset = {"gain": 49.6}   # maximum tuner gain, dB

    def command(self, nsamples):
        biast = which("rtl_biast")
        if self.bias and biast:
            subprocess.run([biast, "-b", "1"], capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return [self.tool, "-f", str(self.center_hz), "-s", str(self.rate),
                "-g", str(self.gain["gain"]), "-n", str(nsamples), "-"]

    def to_complex(self, raw):
        s = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5
        return s[0::2] + 1j * s[1::2]


class HackRF(Source):
    """hackrf_transfer, INT8 IQ."""
    name = "hackrf"
    label = "HackRF"
    tool = "hackrf_transfer"
    install = "hackrf tools (hackrf_transfer)"
    bytes_per_sample = 2
    raw_format = "int8 IQ interleaved"
    default_rate = 5_000_000
    rates = (2_000_000, 4_000_000, 5_000_000, 8_000_000, 10_000_000, 20_000_000)
    preset = {"amp": 1, "lna": 40, "vga": 62}   # maximum

    def command(self, nsamples):
        g = self.gain
        return [self.tool, "-r", "-", "-f", str(self.center_hz), "-s", str(self.rate),
                "-a", str(g["amp"]), "-p", "1" if self.bias else "0",
                "-l", str(g["lna"]), "-g", str(g["vga"]), "-n", str(nsamples)]

    def to_complex(self, raw):
        s = np.frombuffer(raw, dtype=np.int8).astype(np.float32)
        return s[0::2] + 1j * s[1::2]


class Synthetic(Source):
    """No hardware. Gaussian noise plus one drifting tone, for tests and demos.
    Pretends to be INT16 IQ so the rest of the chain is exercised unchanged."""
    name = "synthetic"
    label = "Synthetic (no hardware, test signal)"
    tool = ""
    default_rate = 250_000
    rates = (250_000, 1_000_000)
    preset = {}

    def __init__(self, cfg, tone_offset_hz=5000.0, drift_hz_s=0.2, tone_amp=0.2, seed=1):
        super().__init__(cfg)
        self.tone_offset = tone_offset_hz
        self.drift = drift_hz_s
        self.amp = tone_amp
        self.rng = np.random.default_rng(seed)
        self.t = 0
        self.phase = 0.0

    @classmethod
    def available(cls):
        return True

    def start(self, nsamples, frame_samples):
        self.remaining = int(nsamples)
        self.frame = int(frame_samples)
        self.proc = None
        return self

    def frames(self, frame_samples):
        self.frame = int(frame_samples)
        while True:
            fr = self.read_frame()
            if fr is None:
                return
            yield fr

    def read_frame(self):
        if self.remaining <= 0:
            return None
        n = self.frame
        self.remaining -= n
        t = (self.t + np.arange(n)) / self.rate
        self.t += n
        f = self.tone_offset + self.drift * t
        ph = self.phase + 2 * np.pi * np.cumsum(f) / self.rate
        self.phase = float(ph[-1] % (2 * np.pi))
        tone = self.amp * np.exp(1j * ph)
        iq = tone + (self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n))
        iq16 = np.empty(2 * n, dtype=np.int16)
        iq16[0::2] = np.clip(iq.real * 2000, -32000, 32000)
        iq16[1::2] = np.clip(iq.imag * 2000, -32000, 32000)
        return iq16.tobytes()

    def finish(self):
        return ""

    def kill(self):
        self.remaining = 0


SOURCES = {c.name: c for c in (Airspy, HydraSDR, RtlSdr, HackRF, Synthetic)}


def detect():
    """Names of SDR tools found on this machine, hardware ones first."""
    return [n for n, c in SOURCES.items() if n != "synthetic" and c.available()]


# USB vendor:product of each receiver. Airspy and HydraSDR share an id; the product
# string tells them apart.
USB_IDS = {("1d50", "60a1"): ("airspy", "hydrasdr"), ("0bda", "2838"): ("rtlsdr",),
           ("0bda", "2832"): ("rtlsdr",), ("1d50", "6089"): ("hackrf",)}


def usb_devices():
    """(vendor, product, description) of every USB device present, lower case hex.
    Linux reads sysfs; macOS and Windows ask the system (a second or two)."""
    out = []
    try:
        if sys.platform.startswith("linux"):
            root = "/sys/bus/usb/devices"
            for d in os.listdir(root):
                p = os.path.join(root, d)
                try:
                    with open(os.path.join(p, "idVendor")) as f:
                        vid = f.read().strip().lower()
                    with open(os.path.join(p, "idProduct")) as f:
                        pid = f.read().strip().lower()
                except OSError:
                    continue
                text = ""
                for k in ("manufacturer", "product"):
                    with contextlib.suppress(OSError), open(os.path.join(p, k)) as f:
                        text += f.read().strip() + " "
                out.append((vid, pid, text.strip()))
        elif sys.platform == "darwin":
            import json
            r = subprocess.run(["system_profiler", "SPUSBDataType", "-json"],
                               capture_output=True, text=True, timeout=15)
            def walk(items):
                for it in items or []:
                    vid, pid = it.get("vendor_id", ""), it.get("product_id", "")
                    if vid and pid:
                        out.append((vid.lower().replace("0x", "")[:4].rjust(4, "0"),
                                    pid.lower().replace("0x", "")[:4].rjust(4, "0"),
                                    f"{it.get('manufacturer', '')} {it.get('_name', '')}".strip()))
                    walk(it.get("_items"))
            walk(json.loads(r.stdout or "{}").get("SPUSBDataType"))
        elif sys.platform == "win32":
            import json
            import re
            cmd = ("Get-PnpDevice -PresentOnly | Where-Object {$_.InstanceId -like 'USB\\VID_*'} "
                   "| Select-Object InstanceId,FriendlyName | ConvertTo-Json -Compress")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                               capture_output=True, text=True, timeout=20,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            items = json.loads(r.stdout or "[]")
            for it in items if isinstance(items, list) else [items]:
                m = re.search(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})",
                              it.get("InstanceId", ""))
                if m:
                    out.append((m.group(1).lower(), m.group(2).lower(),
                                it.get("FriendlyName") or ""))
    except Exception:
        pass
    return out


def match_usb(devices):
    """Source names whose hardware is in the USB device list, in SOURCES order."""
    found = set()
    for vid, pid, text in devices:
        names = USB_IDS.get((vid, pid))
        if not names:
            continue
        if len(names) > 1:
            found.add("hydrasdr" if "hydra" in text.lower() else names[0])
        else:
            found.add(names[0])
    return [n for n in SOURCES if n in found]


def attached():
    """Names of receivers plugged in right now (empty when none, or when the platform
    could not be asked)."""
    return match_usb(usb_devices())


def choices():
    """(name, label, available, install hint) for every source, for a settings list."""
    return [(n, c.label, c.available(), c.install) for n, c in SOURCES.items()]


def probe(cfg, seconds=0.5):
    """Open the chosen SDR for a moment. Returns (ok, message) without touching disk."""
    try:
        src = make(cfg)
    except RuntimeError as e:
        return False, str(e)
    n = max(int(src.rate * seconds), 65536)
    try:
        src.start(n, 65536)
        got = 0
        last = None
        for fr in src.frames(65536):
            got += len(fr)
            last = fr
            if got >= n * src.bytes_per_sample // 2:
                break
        with contextlib.suppress(Exception):
            src.proc.kill()
        err = src.finish()
        src.kill()
    except Exception as e:
        src.kill()
        return False, f"{src.label}: {e}"
    if got == 0:
        msg = f"{src.label}: no samples. {err.strip()[-160:] or 'no output from the tool'}"
        if sys.platform == "win32" and src.name in attached():
            # seen on the USB bus but the tool cannot open it: the Windows driver is missing
            msg += ("\n\nThe receiver is plugged in but Windows has not given it the WinUSB "
                    "driver. Run Zadig once (zadig.akeo.ie), pick this device in its list, "
                    "choose WinUSB and press Install, then unplug and replug it.")
        return False, msg
    g = ", ".join(f"{k} {v}" for k, v in src.gain.items()) or "default"
    msg = (f"{src.label} OK: {src.rate / 1e6:g} Msps at {src.center_hz / 1e6:.3f} MHz, "
           f"gains {g}, bias tee {'on' if src.bias else 'off'}")
    if last is not None:
        from .dsp import WEAK_INPUT_COUNTS, WEAK_INPUT_TEXT, input_rms
        rms = input_rms(src.to_complex(last))
        msg += f", noise {rms:.1f} counts"
        if rms < WEAK_INPUT_COUNTS:
            msg += ".\n\nWarning: " + WEAK_INPUT_TEXT.format(rms)
    return True, msg


def make(cfg):
    name = cfg.get("sdr", "auto")
    if name == "auto":
        found = detect()
        if not found:
            raise RuntimeError("no SDR tool found "
                               "(airspy_rx, hydrasdr_rx, rtl_sdr, hackrf_transfer)")
        name = found[0]
        if len(found) > 1:
            # several tool sets installed: use the first one with a radio actually on it
            for n in found:
                if probe(dict(cfg, sdr=n), seconds=0.2)[0]:
                    name = n
                    break
    if name not in SOURCES:
        raise RuntimeError(f"unknown sdr '{name}'")
    cls = SOURCES[name]
    if not cls.available():
        raise RuntimeError(f"{name} needs '{cls.tool}' on PATH")
    return cls(cfg)
