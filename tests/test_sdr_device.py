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


# ---- listing the receivers, for the list in Settings

LISTING = """Found 2 device(s):
  0:  Realtek, RTL2838UHIDIR, SN: 00000001
  1:  RTLSDRBlog, Blog V4, SN: NESDR 7

No matching devices found.
"""


def _tool_says(monkeypatch, stderr, seen=None):
    class Done:
        returncode = 1
        stdout = ""

    Done.stderr = stderr

    def fake_run(cmd, **kwargs):
        if seen is not None:
            seen.append(cmd)
        return Done()

    monkeypatch.setattr(sdr, "which", lambda tool: "/usr/bin/" + tool)
    monkeypatch.setattr(sdr.subprocess, "run", fake_run)


def test_receivers_are_listed_without_opening_one(monkeypatch):
    seen = []
    _tool_says(monkeypatch, LISTING, seen)
    assert sdr.rtl_devices() == [(0, "00000001"), (1, "NESDR 7")]
    # asked for a receiver that cannot exist: the tool lists them and stops there
    assert seen[0][1] == "-d" and not seen[0][2].isdigit()


def test_no_receiver_or_no_tool_lists_nothing(monkeypatch):
    _tool_says(monkeypatch, "No supported devices found.\n")
    assert sdr.rtl_devices() == []
    monkeypatch.setattr(sdr, "which", lambda tool: None)
    assert sdr.rtl_devices() == []


def test_a_serial_that_names_one_receiver_is_what_is_passed():
    assert sdr.rtl_choices([(0, "NESDR"), (1, "V4")]) == [
        ("RTL-SDR #1 (serial NESDR)", "NESDR"), ("RTL-SDR #2 (serial V4)", "V4")]


def test_the_same_serial_twice_falls_back_to_the_number():
    assert [d for _, d in sdr.rtl_choices([(0, "00000001"), (1, "00000001")])] == ["0", "1"]


def test_a_serial_the_tool_would_read_as_a_number_is_not_passed():
    """rtl_sdr tries -d as a device number first: "00000001" with two receivers plugged
    in opens number 1, whichever that is. "00000008" is no number to it (octal)."""
    got = [d for _, d in sdr.rtl_choices([(0, "00000001"), (1, "00000008")])]
    assert got == ["0", "00000008"]
    assert [d for _, d in sdr.rtl_choices([(0, "77"), (1, "")])] == ["77", "1"]


def test_the_listing_is_read_from_a_real_run_of_the_tool(tmp_path, monkeypatch):
    """A stand-in rtl_sdr on PATH that answers as the real one does, run for real."""
    import os
    import sys
    if sys.platform == "win32":
        tool = tmp_path / "rtl_sdr.bat"
        lines = "".join(f">&2 echo {ln}\r\n" if ln else ">&2 echo.\r\n"
                        for ln in LISTING.splitlines())
        tool.write_text("@echo off\r\n" + lines + "exit /b 1\r\n")
    else:
        tool = tmp_path / "rtl_sdr"
        tool.write_text("#!/bin/sh\n[ \"$1\" = -d ] || exit 9\ncat >&2 <<'END'\n"
                        + LISTING + "END\nexit 1\n")
        tool.chmod(0o755)
    monkeypatch.setattr(sdr, "which", lambda name: str(tool) if name == "rtl_sdr" else None)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    assert sdr.rtl_devices() == [(0, "00000001"), (1, "NESDR 7")]
