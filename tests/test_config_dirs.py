import os

from aquila import config


def _home(monkeypatch, path):
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))      # what expanduser reads on Windows


def test_new_install_uses_new_folder(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    assert config.default_path() == str(tmp_path / "aquila-radio-astronomy" / "config.json")
    assert config.derived_state_dir("dish") == str(tmp_path / "aquila-radio-astronomy" / "dish")


def test_old_install_keeps_its_folder(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    os.makedirs(tmp_path / "aquila-seti")
    assert config.default_path() == str(tmp_path / "aquila-seti" / "config.json")
    os.makedirs(tmp_path / "aquila-radio-astronomy")       # once the new one exists it wins
    assert config.default_path() == str(tmp_path / "aquila-radio-astronomy" / "config.json")
