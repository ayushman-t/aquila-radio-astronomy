"""The command line each receiver's tool is started with, locked word for word."""
import sys

from aquila import sdr


def test_the_other_receivers_are_started_exactly_as_before(monkeypatch):
    """The command line of each, word for word. A change here changes what a receiver
    that has been measured is told: it must be meant."""
    monkeypatch.setattr(sdr, "which", lambda tool: None)
    monkeypatch.setattr(sys, "platform", "linux")
    cfg = {"sdr": "x", "sample_rate": 0, "center_mhz": 1420.2058, "advanced": {"gain": "preset"}}
    assert sdr.Airspy(cfg).command(1000) == [
        "airspy_rx", "-r", "/dev/stdout", "-f", "1420.205800", "-a", "3000000", "-t", "2",
        "-b", "1", "-l", "14", "-m", "15", "-v", "15", "-n", "1000"]
    assert sdr.HydraSDR(cfg).command(1000) == [
        "hydrasdr_rx", "-r", "/dev/stdout", "-f", "1420.205800", "-a", "5000000", "-t", "2",
        "-b", "1", "-l", "14", "-m", "15", "-v", "15", "-n", "1000"]
    assert sdr.RtlSdr(cfg).command(1000) == [
        "rtl_sdr", "-f", "1420205800", "-s", "2400000", "-g", "49.6", "-n", "1000", "-"]
    assert sdr.HackRF(cfg).command(1000) == [
        "hackrf_transfer", "-r", "-", "-f", "1420205800", "-s", "5000000", "-a", "1",
        "-p", "1", "-l", "40", "-g", "62", "-n", "1000"]
    node = dict(cfg, advanced={"gain": {"lna": 14, "mixer": 15, "vga": 5}, "bias_tee": True})
    assert sdr.HydraSDR(node).command(1000)[11:17] == ["-l", "14", "-m", "15", "-v", "5"]
