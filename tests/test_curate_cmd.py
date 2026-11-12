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
    cmd = survey.curate_cmd(cfg, 1420e6, 3e6)
    assert cmd[:4] == [sys.executable, "-c", cfg["_path"], "curate"]
    assert "import" not in " ".join(cmd)


def test_frozen_curator_takes_the_tuning_and_leaves_the_receiver_alone(tmp_path, monkeypatch):
    """The child of a frozen build must tier with the tuning the search is using, the
    same as a source install does, and must not open or probe the receiver for it."""
    from aquila import cli, curate, sdr
    cfg = _cfg(tmp_path)
    cfg["hline"] = {"fs_search_blocks": 3}
    cfg["_state_dir_set"] = True
    config.save(cfg)
    center, rate = 1420_285_800, 3_000_000

    monkeypatch.delattr(sys, "frozen", raising=False)
    plain = survey.curate_cmd(cfg, center, rate)
    want = dict(json.loads(plain[4]), state_dir=plain[3])

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    frozen = survey.curate_cmd(cfg, center, rate)
    got = {}

    def run(state_dir, beam_deg, center_hz, exclude_if_khz, snr_thresh, comb_window,
            track_min_blk):
        got.update(state_dir=state_dir, beam_deg=beam_deg, center_hz=center_hz,
                   exclude_if_khz=exclude_if_khz, snr_thresh=snr_thresh,
                   comb_window=comb_window, track_min_blk=track_min_blk)
        return {}

    def no_receiver(*a, **kw):
        raise AssertionError("the curator child touched the receiver")
    monkeypatch.setattr(curate, "run", run)
    monkeypatch.setattr(sdr, "make", no_receiver)
    monkeypatch.setattr(sdr, "probe", no_receiver)
    cli.main(frozen[1:])
    assert got == want


def test_source_install_runs_curate_directly(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    cfg = _cfg(tmp_path)
    cmd = survey.curate_cmd(cfg, 1420e6, 3e6)
    assert cmd[:2] == [sys.executable, "-c"]
    assert cmd[3] == cfg["state_dir"]
    args = json.loads(cmd[4])
    assert args["center_hz"] == 1420e6 and args["beam_deg"] > 0


def test_curate_by_hand_does_not_open_the_receiver(tmp_path, monkeypatch):
    """`aquila curate` typed beside a running search: the receiver is busy and must be
    left alone, and the hits are tiered for the tuning the search reports."""
    from aquila import cli, curate, sdr
    from aquila.hline import DEFAULTS, fs_tunings
    monkeypatch.delattr(sys, "frozen", raising=False)
    cfg = _cfg(tmp_path)
    cfg["sdr"] = "auto"
    cfg["hline"] = {"fs_search_blocks": 3}
    cfg["_state_dir_set"] = True
    config.save(cfg)
    (tmp_path / "node").mkdir()
    (tmp_path / "node" / "status.json").write_text(json.dumps(
        {"state": "observing", "sdr": "rtlsdr", "sample_rate": 2400000}))
    got = {}

    def run(state_dir, beam_deg, center_hz, *a):
        got.update(state_dir=state_dir, center_hz=center_hz)
        return {}

    def no_receiver(*a, **kw):
        raise AssertionError("curate touched the receiver")
    monkeypatch.setattr(curate, "run", run)
    monkeypatch.setattr(sdr, "make", no_receiver)
    monkeypatch.setattr(sdr, "probe", no_receiver)
    monkeypatch.setattr(sdr, "detect", lambda: ["airspy", "rtlsdr"])
    cli.main(["-c", cfg["_path"], "curate"])
    assert got["state_dir"] == cfg["state_dir"]
    assert got["center_hz"] == fs_tunings({**DEFAULTS, **cfg["hline"]}, 2400000)[0]
