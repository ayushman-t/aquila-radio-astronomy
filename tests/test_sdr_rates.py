"""The receiver is asked for a rate it has: an Airspy Mini and an R2 list different
ones, and a HydraSDR driven as an Airspy refuses the Airspy default."""
import json

import pytest

from aquila import config, hline, sdr

MINI = ("airspy_lib_version: 1.0.10\n\nFound AirSpy board 1\nBoard ID Number: 0 (AIRSPY)\n"
        "Firmware Version: AirSpy MINI v1.0.0-rc10-6-g4008185 2020-05-08\n"
        "Supported sample rates:\n\t6.000000 MSPS\n\t3.000000 MSPS\nClose board 1\n")
R2 = MINI.replace("MINI", "NOS").replace("6.000000", "10.000000").replace("3.000000", "2.500000")
HYDRA = ("airspy_lib_version: 1.0.10\n\nFound AirSpy board 1\n"
         "Firmware Version: HydraSDR RFOne v1.0.0-rc2 2024-09-15\n"
         "Supported sample rates:\n\t10.000000 MSPS\n\t5.000000 MSPS\n\t2.500000 MSPS\n")


@pytest.mark.parametrize("text,want", [
    (MINI, [3_000_000, 6_000_000]), (R2, [2_500_000, 10_000_000]),
    (HYDRA, [2_500_000, 5_000_000, 10_000_000]),
    ("airspy_lib_version: 1.0.10\n", []), ("", []),         # in use, or no tool
])
def test_rates_read_from_airspy_info(monkeypatch, text, want):
    monkeypatch.setattr(sdr, "_airspy_info", lambda: text)
    assert sdr.airspy_rates() == want


def test_nearest_rate():
    assert sdr.nearest_rate(3_000_000, []) == 3_000_000
    assert sdr.nearest_rate(3_000_000, [3_000_000, 6_000_000]) == 3_000_000
    assert sdr.nearest_rate(3_000_000, [2_500_000, 10_000_000]) == 2_500_000


def _cfg(tmp_path, **over):
    cfg = config.load(str(tmp_path / "c.json"))
    cfg.update(state_dir=str(tmp_path / "s"), **over)
    return cfg


@pytest.fixture
def tools(monkeypatch):
    for name in ("airspy", "hydrasdr"):
        monkeypatch.setattr(sdr.SOURCES[name], "available", classmethod(lambda c: True))
    asked = []
    monkeypatch.setattr(sdr, "_airspy_info", lambda: asked.append(1) or tools.info)
    tools.asked = asked
    return tools


def test_airspy_picked_with_a_hydrasdr_plugged_in_runs_as_hydrasdr(tmp_path, monkeypatch, tools):
    monkeypatch.setattr(sdr, "attached", lambda: ["hydrasdr"])
    tools.info = HYDRA
    src = sdr.make(_cfg(tmp_path, sdr="airspy"))
    assert src.name == "hydrasdr" and src.rate == 5_000_000
    cfg = _cfg(tmp_path, sdr="airspy")
    cfg["advanced"]["gain"] = {"lna": 14, "mixer": 15, "vga": 5, "for": "airspy"}
    assert sdr.make(cfg).gain == {"lna": 14, "mixer": 15, "vga": 5}      # gains go with it
    assert cfg["advanced"]["gain"]["for"] == "airspy"                    # the config is not touched


@pytest.mark.parametrize("info,want", [(MINI, 3_000_000), (R2, 2_500_000), ("", 3_000_000)])
def test_airspy_default_rate_is_one_the_receiver_lists(tmp_path, monkeypatch, tools, info, want):
    monkeypatch.setattr(sdr, "attached", lambda: ["airspy"])
    tools.info = info
    src = sdr.make(_cfg(tmp_path, sdr="airspy"))
    assert src.name == "airspy" and src.rate == want


def test_a_rate_set_by_the_user_is_not_changed_or_looked_up(tmp_path, monkeypatch, tools):
    monkeypatch.setattr(sdr, "attached", lambda: ["airspy"])
    tools.info = R2
    src = sdr.make(_cfg(tmp_path, sdr="airspy", sample_rate=10_000_000))
    assert src.rate == 10_000_000 and not tools.asked


def test_no_airspy_plugged_in_asks_no_tool(tmp_path, monkeypatch, tools):
    monkeypatch.setattr(sdr, "attached", lambda: [])
    tools.info = R2
    src = sdr.make(_cfg(tmp_path, sdr="airspy"))
    assert src.rate == 3_000_000 and not tools.asked


def test_hline_reports_the_tuning_it_uses_not_the_config_centre(tmp_path):
    """Hydrogen line scans tune themselves: the log and the status must say where."""
    cfg = _cfg(tmp_path, sdr="synthetic", sample_rate=2_400_000, center_mhz=1400.0)
    logs = []
    node = hline.HLine(cfg, log=logs.append)
    node.src.rate = 2_400_000
    f_sig = hline.fs_tunings(node.h, 2_400_000)[0]
    node.status("capturing")
    st = json.load(open(node.store.status))
    assert st["center_hz"] == f_sig and abs(f_sig - 1420.4e6) < 1.3e6
    node.running = False
    assert node.run() == 0
    assert any(f"{f_sig / 1e6:.4f} MHz" in m for m in logs)
    assert not any("1400.0000" in m for m in logs)


def test_hline_blocks_exit_code_tells_a_receiver_that_never_gave_a_scan(tmp_path, monkeypatch):
    from aquila import api, cli
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"sdr": "synthetic", "state_dir": str(tmp_path / "s")}))
    monkeypatch.setattr(api, "serve", lambda *a, **k: None)
    monkeypatch.setattr(hline.HLine, "scan", lambda self: False)
    monkeypatch.setattr(hline.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit) as e:
        cli.main(["-c", str(path), "hline", "--blocks", "1"])
    assert e.value.code == 1
    monkeypatch.setattr(hline.HLine, "scan", lambda self: True)
    cli.main(["-c", str(path), "hline", "--blocks", "1"])
