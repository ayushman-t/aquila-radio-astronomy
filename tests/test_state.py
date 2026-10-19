# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# GPL-3.0-or-later: free to use and modify, derived works must stay open.
"""Settings changes must not leave stale state behind."""
import json
import os
import subprocess
import sys
import tempfile

from aquila import config, curate, lock
from aquila.spur import SpurMask


def test_rename_moves_folder_unless_set_by_hand():
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "c.json")
    cfg = config.load(p)
    assert not cfg["_state_dir_set"]
    config.rename(cfg, "dish-a")
    assert cfg["state_dir"].endswith("dish-a")
    config.save(cfg, p)
    assert json.load(open(p))["state_dir"] == ""
    cfg = config.load(p)
    config.rename(cfg, "dish-b")
    assert cfg["state_dir"].endswith("dish-b")
    cfg["state_dir"] = os.path.join(tmp, "custom")
    cfg["_state_dir_set"] = True
    config.rename(cfg, "dish-c")
    assert cfg["state_dir"].endswith("custom")


def test_lock_refuses_second_observer():
    tmp = tempfile.mkdtemp()
    lk = lock.acquire_lock(tmp, log=lambda m: None)
    assert lock.holder(tmp) == os.getpid()
    code = ("import sys; from aquila import lock\n"
            f"lock.acquire_lock({tmp!r}, log=lambda m: None)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True)
    assert r.returncode == 3                     # a second process is refused
    lock.release_lock(lk)
    assert lock.holder(tmp) == 0
    with open(os.path.join(tmp, "lock"), "w") as f:
        f.write("999999999")                     # stale file, nobody holds it: fine
    lk = lock.acquire_lock(tmp, log=lambda m: None)
    lock.release_lock(lk)


def test_spur_mask_discarded_on_tuning_change():
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "spur_mask.json")
    t1 = {"n_chan": 1 << 20, "rate": 5_000_000, "center_hz": 1_420_000_000, "sdr": "airspy"}
    m = SpurMask(path=p, tuning=t1)
    m.update([100, 101, 102])
    m.update([100, 101, 102])
    m.update([100, 101, 102])
    assert m.mask
    assert SpurMask(path=p, tuning=t1).mask == m.mask
    t2 = dict(t1, rate=3_000_000)
    assert SpurMask(path=p, tuning=t2).mask == set()


def test_curate_ignores_synthetic_rows():
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "hits.csv")
    with open(p, "w") as f:
        f.write("utc,local,sky_freq_hz,drift_hz_s,snr,width_hz,coverage,thumb,sdr\n")
        f.write("20260901T000000Z,x,1420005000,0.2,50,2.4,20,a.png,synthetic\n")
        f.write("20260901T000300Z,x,1420005000,0.2,50,2.4,20,b.png,airspy\n")
    rows = curate.load(p)
    assert len(rows) == 1


def test_h1_day_file_from_survey_block():
    import numpy as np

    from aquila import config as cfgmod
    from aquila.sdr import Synthetic
    from aquila.survey import Survey
    tmp = tempfile.mkdtemp()
    cfg = cfgmod.load(os.path.join(tmp, "c.json"))
    cfg.update(sdr="synthetic", sample_rate=250_000, state_dir=os.path.join(tmp, "s"))
    cfg["advanced"].update(n_chan=1 << 15, rows=6, dwell_s=3, settle_skip_s=0.2,
                           seti_min_chan=1024)
    cfg["save"]["plots"] = False
    s = Survey(cfg, source=Synthetic(cfg))
    assert s.one_block() and s.one_block()
    files = [f for f in os.listdir(os.path.join(cfg["state_dir"], "h1")) if f.endswith(".dat")]
    assert len(files) == 1
    a = np.fromfile(os.path.join(cfg["state_dir"], "h1", files[0]), np.float32)
    assert a.size == 2 * 1024
    hdr = open(os.path.join(cfg["state_dir"], "h1", files[0].replace(".dat", ".header"))).read()
    assert "channels=1024" in hdr and "mjd=" in hdr


def test_empty_hits_file_gets_its_header():
    """A power cut can leave hits.csv created but empty; rows must not lose their header."""
    from aquila import curate
    from aquila.store import Store
    d = tempfile.mkdtemp()
    p = os.path.join(d, "hits.csv")
    open(p, "w").close()
    block = {"utc": "20261010T000000Z", "local": "x", "center_hz": 1420e6, "n_chan": 1024,
             "chan_bw": 10.0, "thumb": "", "sdr": "rtl"}
    Store.append_hits(None, [{"chan": 600, "drift_hz_s": 0.2, "snr": 30}], block, p)
    assert len(curate.load(p)) == 1


def test_api_never_serves_config_or_outside_files():
    import urllib.error
    import urllib.request

    from aquila import api
    d = tempfile.mkdtemp()
    open(os.path.join(d, "config.json"), "w").write('{"advanced": {"api_token": "s"}}')
    open(os.path.join(d, "status.json"), "w").write("{}")
    outside = tempfile.NamedTemporaryFile(delete=False)
    outside.write(b"secret")
    outside.close()
    os.symlink(outside.name, os.path.join(d, "link.txt"))
    srv = api.serve(d, port=0)
    port = srv.server_address[1]
    try:
        def get(path):
            try:
                return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5).status
            except urllib.error.HTTPError as e:
                return e.code
        assert get("/status.json") == 200
        assert get("/config.json") == 404
        assert get("/link.txt") == 404
    finally:
        srv.shutdown()
