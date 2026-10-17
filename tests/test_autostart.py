import os
import shlex
import sys

import pytest

from aquila import autostart


@pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="XDG autostart is Linux")
def test_login_entry_is_written_and_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = tmp_path / "my node" / "config.json"          # a space must survive the quoting
    assert not autostart.enabled()
    autostart.set_enabled(True, str(cfg))
    assert autostart.enabled()
    text = (tmp_path / "autostart" / "aquila-radio-astronomy.desktop").read_text()
    exec_line = next(line for line in text.splitlines() if line.startswith("Exec="))
    assert shlex.split(exec_line[5:]) == [sys.executable, "-m", "aquila.cli",
                                          "-c", str(cfg), "gui"]
    autostart.set_enabled(False)
    assert not autostart.enabled()
    autostart.set_enabled(False)                        # removing twice is not an error


def test_desktop_quoting_escapes_specials():
    assert autostart._desktop_quote("/usr/bin/python3") == "/usr/bin/python3"
    assert autostart._desktop_quote('a "b" $c') == '"a \\"b\\" \\$c"'
    assert autostart._desktop_quote("50%") == "50%%"


def test_observe_on_open_defaults_on(tmp_path):
    from aquila import config
    cfg = config.load(str(tmp_path / "none.json"))
    assert cfg["app"]["observe_on_open"] is True
    assert os.path.basename(cfg["state_dir"]) == cfg["name"]
