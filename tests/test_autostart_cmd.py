"""The login entry must start the install that wrote it: a frozen build takes the
verb directly, a source install goes through python -m."""
import sys

from aquila import autostart


def test_frozen_build_takes_the_verb(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cfg = str(tmp_path / "config.json")
    assert autostart._command(cfg) == [sys.executable, "-c", cfg, "gui"]


def test_source_install_uses_the_module(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    cmd = autostart._command(str(tmp_path / "config.json"))
    assert cmd[1:3] == ["-m", "aquila.cli"] and cmd[-1] == "gui"
