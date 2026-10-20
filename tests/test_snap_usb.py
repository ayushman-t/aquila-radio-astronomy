"""Inside a snap the app asks snapd whether raw-usb is connected and only then
offers the permission dialog; outside a snap it never runs snapctl."""
import subprocess

from aquila import gui


def test_outside_a_snap_nothing_is_checked(monkeypatch):
    monkeypatch.delenv("SNAP_NAME", raising=False)
    def never(*a, **k):
        raise AssertionError("snapctl ran outside a snap")
    monkeypatch.setattr(subprocess, "run", never)
    assert gui.snap_usb_blocked() is False


def test_connected_plug_is_quiet(monkeypatch):
    monkeypatch.setenv("SNAP_NAME", "aquila-radio-astronomy")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    assert gui.snap_usb_blocked() is False


def test_disconnected_plug_is_reported(monkeypatch):
    monkeypatch.setenv("SNAP_NAME", "aquila-radio-astronomy")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1))
    assert gui.snap_usb_blocked() is True


def test_missing_snapctl_does_not_nag(monkeypatch):
    monkeypatch.setenv("SNAP_NAME", "aquila-radio-astronomy")
    def boom(*a, **k):
        raise FileNotFoundError("snapctl")
    monkeypatch.setattr(subprocess, "run", boom)
    assert gui.snap_usb_blocked() is False
