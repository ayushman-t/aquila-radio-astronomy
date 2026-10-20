"""A snap cannot read /sys/bus/usb/devices: the scan must fall back to lsusb and
tell a HydraSDR from an Airspy by the firmware line, or the wrong rate is asked for."""
import os
import subprocess
import sys

import pytest

from aquila import sdr

LSUSB = ("Bus 001 Device 060: ID 1d50:60a1 OpenMoko, Inc. Airspy\n"
         "Bus 001 Device 048: ID 0bda:2838 Realtek Semiconductor Corp. RTL2838 DVB-T\n")
INFO = ("airspy_lib_version: 1.0.11\n\nFound AirSpy board 1\n"
        "Firmware Version: HydraSDR RFOne v1.0.0-rc2 2024-09-15\n")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="linux scan")
def test_sysfs_denied_falls_back_to_lsusb_and_firmware(monkeypatch):
    def denied(path):
        raise PermissionError(13, "Permission denied", path)
    monkeypatch.setattr(os, "listdir", denied)

    def fake_run(cmd, **k):
        out = {"lsusb": LSUSB, "airspy_info": INFO}[cmd[0]]
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert sdr.attached() == ["hydrasdr", "rtlsdr"]
