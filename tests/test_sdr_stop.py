"""A stop request reaches the receiver from another thread (or a signal) at any moment,
also while a capture is being started. None of these may raise or leave a tool running."""
import subprocess
import sys
import time

import numpy as np
import pytest

from aquila import dsp, sdr

TOOL = [sys.executable, "-c",
        "import sys, time; sys.stdout.buffer.write(bytes(4096)); sys.stdout.flush();"
        " time.sleep(60)"]


class Fake(sdr.Source):
    name = "fake"
    label = "Fake"

    def command(self, nsamples):
        return list(TOOL)


def _src(cls=Fake):
    return cls({"sdr": "fake", "sample_rate": 0, "center_mhz": 1420.0,
                "advanced": {"gain": "preset", "bias_tee": True}})


def test_stop_between_start_and_frames_is_a_clean_end():
    src = _src()
    src.start(1 << 20, 256)
    src.stop()
    assert list(src.frames(256)) == []
    assert src.finish() == ""


def test_no_capture_starts_after_stop():
    src = _src()
    src.stop()
    src.start(1 << 20, 256)
    assert src.proc is None
    assert list(src.frames(256)) == []


def test_stop_while_the_tool_is_being_launched(monkeypatch):
    src = _src()
    made = []
    real = subprocess.Popen

    def popen(*a, **kw):
        made.append(real(*a, **kw))
        src.stop()                      # the stop thread runs before start() has the process
        return made[-1]
    monkeypatch.setattr(sdr.subprocess, "Popen", popen)
    src.start(1 << 20, 256)
    monkeypatch.undo()
    assert list(src.frames(256)) == []
    assert made[0].wait(timeout=5) is not None      # not left running with the radio


def test_stop_just_before_the_tool_is_launched(monkeypatch):
    src = _src()
    real = subprocess.Popen

    def popen(*a, **kw):
        src.stop()                      # closes the error file the tool was to write to
        return real(*a, **kw)
    monkeypatch.setattr(sdr.subprocess, "Popen", popen)
    src.start(1 << 20, 256)
    monkeypatch.undo()
    assert src.proc is None
    assert list(src.frames(256)) == []


def test_windows_stop_while_the_tool_opens_the_pipe(monkeypatch):
    src = _src()
    made = []

    class Pipe:
        path = "-"

        def __init__(self, bufsize):
            pass

        timed_out = False

        def connect(self, proc, *a):
            made.append(proc)
            src.stop()                  # arrives while start() waits for the tool
            return open(__file__, "rb")
    monkeypatch.setattr(sdr, "_NamedPipe", Pipe)
    monkeypatch.setattr(sdr, "_bind_to_this_process", lambda proc: True)
    monkeypatch.setattr(sdr.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(sdr.sys, "platform", "win32")
    try:
        src.start(1 << 20, 256)
    finally:
        monkeypatch.undo()
    assert list(src.frames(256)) == []
    assert made[0].poll() is not None


@pytest.mark.skipif(sys.platform != "win32", reason="the named pipe is the Windows path")
def test_windows_tool_that_never_opens_its_output_is_ended(monkeypatch):
    """A tool hung on the radio before it opens the pipe must not hold the node for ever."""
    monkeypatch.setattr(Fake, "STALL_S", 2.0)
    src = _src()
    t0 = time.time()
    proc = src.start(1 << 20, 256)
    assert time.time() - t0 < 12
    assert proc.poll() is not None
    assert list(src.frames(256)) == []
    assert "no samples" in src.finish()


@pytest.mark.skipif(sys.platform != "win32", reason="the named pipe is the Windows path")
def test_windows_samples_arrive_through_the_pipe(monkeypatch):
    """Bytes 0x0A and 0x0D must come through unchanged, and the stream must end."""
    body = bytes(range(256)) * 64
    tool = [sys.executable, "-c",
            "import sys; f = open(sys.argv[1], 'wb'); f.write(bytes(range(256)) * 64); f.close()",
            "-"]
    monkeypatch.setattr(Fake, "command", lambda self, n: list(tool))
    monkeypatch.setattr(Fake, "bytes_per_sample", 4)
    src = _src()
    src.start(len(body) // 4, 256)
    got = b"".join(src.frames(256))
    assert got == body
    assert src.finish() == ""


def test_a_stopped_capture_is_not_logged_as_a_receiver_fault():
    src = _src()
    src.stop()
    lines = []
    assert dsp.channelize(src, 256, 4, 1.0, 0.0, lines.append)[0] is None
    assert lines == []


def test_synthetic_stops_too():
    src = sdr.Synthetic({"sdr": "synthetic", "sample_rate": 0, "center_mhz": 1420.0,
                         "advanced": {"gain": "preset", "bias_tee": True}})
    src.stop()
    src.start(1 << 16, 256)
    assert list(src.frames(256)) == []


def test_injected_test_signal_reaches_every_receiver():
    spec = {"sky_hz": 1420.0e6 + 1000.0, "drift_hz_s": 0.0, "amp": 5.0,
            "peak_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "width_s": 3600}
    for cls in (sdr.Airspy, sdr.HydraSDR, sdr.RtlSdr, sdr.HackRF):
        src = cls({"sdr": cls.name, "sample_rate": 0, "center_mhz": 1420.0,
                   "advanced": {"gain": "preset", "bias_tee": True, "inject": spec}})
        raw = np.zeros(4096 * src.bytes_per_sample, np.uint8).tobytes()
        quiet = cls({"sdr": cls.name, "sample_rate": 0, "center_mhz": 1420.0,
                     "advanced": {"gain": "preset", "bias_tee": True}}).to_complex(raw)
        z = src.to_complex(raw)
        assert np.abs(z - quiet).max() > 1.0, cls.name


class StoppedWhileStarting(Fake):
    """The node's stop arrives while the capture is being started."""
    node = None

    def command(self, nsamples):
        self.node.stop()
        return list(TOOL)


def _node_cfg(tmp_path):
    from aquila import config as cfgmod
    cfg = cfgmod.load(str(tmp_path / "c.json"))
    cfg.update(sdr="synthetic", sample_rate=250_000, state_dir=str(tmp_path / "s"))
    cfg["advanced"].update(n_chan=1 << 12, rows=4, dwell_s=2, settle_skip_s=0.1,
                           require_ntp=False)
    cfg["save"]["plots"] = False
    cfg["center_mhz"] = 1420.4
    return cfg


def _log(cfg):
    import os
    with open(os.path.join(cfg["state_dir"], "survey.log"), errors="replace") as f:
        return f.read()


def test_hline_node_stopped_while_starting_ends_without_an_error(tmp_path):
    from aquila.hline import HLine
    cfg = _node_cfg(tmp_path)
    src = _src(StoppedWhileStarting)
    src.rate = 250_000
    h = src.node = HLine(cfg, source=src)
    t0 = time.time()
    h.run()
    assert time.time() - t0 < 10
    log = _log(cfg)
    assert "error" not in log and "short" not in log, log


def test_search_node_stopped_while_starting_ends_without_an_error(tmp_path):
    from aquila.survey import Survey
    cfg = _node_cfg(tmp_path)
    src = _src(StoppedWhileStarting)
    src.rate = 250_000
    s = src.node = Survey(cfg, source=src)
    t0 = time.time()
    s.run()
    assert time.time() - t0 < 10
    log = _log(cfg)
    assert "error" not in log and "ended early" not in log, log
