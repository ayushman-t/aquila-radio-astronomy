"""Pinning the RTL-SDR by device, so a second dongle is never taken by mistake.

Two RTL2838s report the same vendor:product, so librtlsdr falls back to index 0
when nothing is asked for. On a machine that also runs a satellite receiver,
that means an observation can silently capture - and hold - the wrong radio.
`-d` takes a serial, which is stable across replugs and boots; an index is not.
"""

from aquila import sdr


def _cfg(device=None):
    advanced = {"gain": "preset", "bias_tee": True}
    if device is not None:
        advanced["device"] = device
    return {"sdr": "rtlsdr", "sample_rate": 0, "center_mhz": 1420.0,
            "advanced": advanced}


def test_pinned_device_reaches_rtl_sdr():
    cmd = sdr.RtlSdr(_cfg("NESDR")).command(10)
    assert cmd[cmd.index("-d") + 1] == "NESDR"


def test_pinned_device_reaches_rtl_biast(monkeypatch):
    """The bias tee is set by a separate call, which also needs the device.

    Without it, rtl_biast opens index 0 and switches the tee on whichever
    dongle that is - powering an LNA on a radio this app is not even using.
    """
    seen = []

    class Done:
        returncode = 0

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return Done()

    monkeypatch.setattr(sdr, "which", lambda tool: "/usr/bin/rtl_biast")
    monkeypatch.setattr(sdr.subprocess, "run", fake_run)
    sdr.RtlSdr(_cfg("NESDR")).command(10)

    assert seen, "rtl_biast was not called"
    assert "-d" in seen[0]
    assert seen[0][seen[0].index("-d") + 1] == "NESDR"


def test_unpinned_device_leaves_the_command_alone():
    """No device configured means no -d, so single-dongle setups are unchanged."""
    cmd = sdr.RtlSdr(_cfg()).command(10)
    assert "-d" not in cmd
    assert cmd[0] == "rtl_sdr"


def test_blank_device_is_the_same_as_unset():
    for blank in ("", "   "):
        assert "-d" not in sdr.RtlSdr(_cfg(blank)).command(10)


def test_device_is_stripped():
    """A trailing space from an edited config must not become a broken serial."""
    cmd = sdr.RtlSdr(_cfg("  NESDR  ")).command(10)
    assert cmd[cmd.index("-d") + 1] == "NESDR"


def test_other_receivers_are_unaffected():
    """Only the RTL-SDR has -d; Airspy and HackRF select by their own rules."""
    cmd = sdr.Airspy(_cfg("NESDR")).command(10)
    assert "-d" not in cmd
