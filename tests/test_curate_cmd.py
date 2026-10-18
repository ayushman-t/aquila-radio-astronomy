"""The hit curator child process: a frozen build must call its own `curate` verb,
not `python -c`, which the executable would parse as a verb."""
import json
import sys

from aquila import config, survey


def _cfg(tmp_path):
    cfg = config.load(str(tmp_path / "config.json"))
    cfg["state_dir"] = str(tmp_path / "node")
    return cfg


def test_frozen_build_uses_the_curate_verb(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cfg = _cfg(tmp_path)
    cmd = survey.curate_cmd(cfg, 1420e6)
    assert cmd == [sys.executable, "-c", cfg["_path"], "curate"]
    assert "import" not in " ".join(cmd)


def test_source_install_runs_curate_directly(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    cfg = _cfg(tmp_path)
    cmd = survey.curate_cmd(cfg, 1420e6)
    assert cmd[:2] == [sys.executable, "-c"]
    assert cmd[3] == cfg["state_dir"]
    args = json.loads(cmd[4])
    assert args["center_hz"] == 1420e6 and args["beam_deg"] > 0
