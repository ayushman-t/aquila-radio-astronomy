"""Settings, Quick setup and the window's own actions, on a scratch config with every
receiver, USB and login call replaced: nothing here opens a radio or the user's data."""
import json
import os
import sys
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QProcess  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

from aquila import autostart, config, gui, sdr  # noqa: E402


@pytest.fixture
def app(monkeypatch):
    """A Qt application, with the tools for Airspy and RTL-SDR 'installed' and no receiver
    plugged in. Tests change `app.installed` and `app.plugged` as they need."""
    a = QApplication.instance() or QApplication(["test"])
    a.installed = {"airspy", "rtlsdr"}
    a.plugged = []
    for name, cls in sdr.SOURCES.items():
        if name != "synthetic":
            monkeypatch.setattr(cls, "available", classmethod(lambda c: c.name in a.installed))
    monkeypatch.setattr(sdr, "attached", lambda: list(a.plugged))
    monkeypatch.setattr(sdr, "usb_devices", lambda: [])
    monkeypatch.setattr(sdr, "probe", lambda *x, **k: (False, "not opened in a test"))
    monkeypatch.setattr(gui, "_lan_ip", lambda: "lan-ip")
    monkeypatch.setattr(autostart, "enabled", lambda: False)
    monkeypatch.setattr(autostart, "set_enabled", lambda *x, **k: None)
    monkeypatch.delenv("SNAP_NAME", raising=False)
    for m in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, m, staticmethod(lambda *x, **k: QMessageBox.Ok))
    return a


def scratch(tmp_path, **over):
    """A config file in a scratch folder with its data folder beside it; returns its path."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    base = {"name": "test-node", "sdr": "synthetic", "state_dir": str(data),
            "app": {"observe_on_open": False}}
    base.update(over)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(base))
    return str(path)


def pump(app, until, seconds=8.0):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until():
            return True
        time.sleep(0.02)
    return False


def retire(app, dlg):
    """Let the dialog's USB scan end before the dialog goes."""
    t = getattr(dlg, "_scan", None)
    if t is not None:
        t.wait(5000)
    app.processEvents()
    dlg.deleteLater()


# ---- Settings

def test_auto_shows_the_receiver_the_node_is_using(app, tmp_path):
    """Auto with an RTL-SDR observing: the page shows the RTL's gain and rates, not those of
    the first tool installed, and OK with nothing touched changes nothing."""
    gain = {"gain": 30.0, "for": "rtlsdr"}
    cfg = config.load(scratch(tmp_path, sdr="auto", sample_rate=2048000,
                              advanced={"gain": gain}))
    dlg = gui.Settings(cfg, None, "rtlsdr")
    assert dlg.gain_for is sdr.SOURCES["rtlsdr"]
    assert dlg.gain_boxes["gain"].value() == 30.0
    assert dlg.rate.currentData() == 2048000
    out = dlg.apply()
    assert out["advanced"]["gain"] == gain and out["sample_rate"] == 2048000
    dlg.gain_boxes["gain"].setValue(20.0)
    assert dlg.apply()["advanced"]["gain"] == {"gain": 20.0, "for": "rtlsdr"}
    retire(app, dlg)


def test_auto_shows_the_receiver_plugged_in(app, tmp_path):
    app.plugged = ["rtlsdr"]
    cfg = config.load(scratch(tmp_path, sdr="auto"))
    dlg = gui.Settings(cfg, None, None)
    assert pump(app, lambda: dlg.auto_sdr == "rtlsdr")
    assert dlg.gain_for is sdr.SOURCES["rtlsdr"]
    assert [dlg.rate.itemData(i) for i in range(1, dlg.rate.count())] == list(
        sdr.SOURCES["rtlsdr"].rates)
    dlg.gain_boxes["gain"].setValue(10.0)
    assert dlg.apply()["advanced"]["gain"] == {"gain": 10.0, "for": "rtlsdr"}
    retire(app, dlg)


def test_ok_with_no_receiver_known_keeps_what_was_saved(app, tmp_path):
    """Receiver unplugged, Auto: the page can only show the first installed receiver's
    boxes. OK must not replace the gain and rate saved for the real one."""
    app.installed = {"airspy", "hydrasdr", "rtlsdr"}
    gain = {"lna": 9, "mixer": 8, "vga": 7, "for": "hydrasdr"}
    cfg = config.load(scratch(tmp_path, sdr="auto", sample_rate=5000000,
                              advanced={"gain": gain}))
    dlg = gui.Settings(cfg, None, None)
    assert dlg.gain_for is sdr.SOURCES["airspy"]
    out = dlg.apply()
    assert out["advanced"]["gain"] == gain
    assert out["sample_rate"] == 5000000
    retire(app, dlg)


def test_another_receiver_chosen_does_not_keep_a_rate_it_lacks(app, tmp_path):
    cfg = config.load(scratch(tmp_path, sdr="airspy", sample_rate=10000000))
    dlg = gui.Settings(cfg, None, None)
    assert dlg.rate.currentData() == 10000000
    dlg.sdr.setCurrentIndex(dlg.sdr.findData("rtlsdr"))
    assert dlg.apply()["sample_rate"] == 0
    retire(app, dlg)


def test_ok_keeps_values_the_page_cannot_show(app, tmp_path):
    cfg = config.load(scratch(tmp_path, advanced={"api_bind": "192.168.1.5",
                                                  "n_chan": 1 << 17}))
    dlg = gui.Settings(cfg, None, None)
    out = dlg.apply()
    assert out["advanced"]["api_bind"] == "192.168.1.5"
    assert out["advanced"]["n_chan"] == 1 << 17
    dlg.share.setChecked(False)
    assert dlg.apply()["advanced"]["api_bind"] == "127.0.0.1"
    retire(app, dlg)


def test_check_does_not_open_the_receiver_the_node_holds(app, tmp_path, monkeypatch):
    def no_probe(cfg):
        raise AssertionError("the check opened the receiver in use")
    monkeypatch.setattr(gui, "Probe", no_probe)
    cfg = config.load(scratch(tmp_path, sdr="auto"))
    for dlg in (gui.Settings(cfg, None, "rtlsdr"), gui.QuickSetup(cfg, None, "rtlsdr")):
        dlg.check_sdr()
        assert dlg.sdr_result.text() == gui.IN_USE_TEXT
        assert dlg.check.isEnabled()
        retire(app, dlg)


# ---- Quick setup

def test_quick_setup_name_with_a_slash_still_loads(app, tmp_path):
    path = scratch(tmp_path)
    cfg = config.load(path)
    dlg = gui.QuickSetup(cfg, None)
    dlg.name.setText("roof/0.9m")
    config.save(dlg.apply(), path)
    assert config.load(path)["name"] == "roof-0.9m"
    retire(app, dlg)


def test_windows_folder_characters_leave_the_name(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    cfg = {"_state_dir_set": True}
    config.rename(cfg, 'dish: 0.9 m "east"?')
    assert cfg["name"] == "dish- 0.9 m -east--"


def test_quick_setup_drops_the_last_receivers_rate(app, tmp_path):
    cfg = config.load(scratch(tmp_path, sdr="airspy", sample_rate=10000000))
    dlg = gui.QuickSetup(cfg, None)
    assert dlg.apply()["sample_rate"] == 10000000          # same receiver: kept
    dlg.sdr.setCurrentIndex(dlg.sdr.findData("rtlsdr"))
    assert dlg.apply()["sample_rate"] == 0
    retire(app, dlg)


def test_quick_setup_keeps_the_synthetic_source(app, tmp_path):
    cfg = config.load(scratch(tmp_path, sdr="synthetic"))
    dlg = gui.QuickSetup(cfg, None)
    assert dlg.apply()["sdr"] == "synthetic"
    retire(app, dlg)


# ---- the window

@pytest.fixture
def window(app, tmp_path):
    made = []

    def make(**over):
        w = gui.Main(scratch(tmp_path, **over))
        w.timer.stop()
        made.append(w)
        return w
    yield make
    for w in made:
        w._closing = True
        w.close()
        w.deleteLater()
    app.processEvents()


def settle(app, w):
    pump(app, lambda: not w._poll.isRunning(), 10)
    app.processEvents()


def test_start_is_off_for_a_remote_node_that_does_not_answer(app, window):
    w = window(remote_nodes=[{"name": "far", "url": "http://127.0.0.1:9"}])
    settle(app, w)
    assert w.a_seti.isEnabled() and w.a_hline.isEnabled()
    w.node_box.setCurrentIndex(1)
    settle(app, w)
    assert w.st["state"].text() == "unreachable"
    assert not w.a_seti.isEnabled() and not w.a_hline.isEnabled()


def test_an_address_that_is_no_node_does_not_stop_the_window(app, window, monkeypatch):
    """Whatever answers at a far address, the window shows what it can and goes on."""
    odd = {"status": {"state": "observing", "ts": "soon", "center_hz": "1420", "dwell_s": [],
                      "footprint_mb": "", "chan_bw_hz": {}, "cal_s": 10 ** 400,
                      "dish": {"az": "west"}, "site": {"lat": 1, "lon": 2}},
           "curate": "none", "hline": 3, "files": [], "tail": True}
    answer = [b""]
    get = gui.Node.get
    monkeypatch.setattr(gui.Node, "get", lambda self, rel, *a, **k: (
        (answer[0] if rel == "index.json" else None) if self.remote
        else get(self, rel, *a, **k)))
    w = window(remote_nodes=[{"name": "far", "url": "http://127.0.0.1:9"}])
    settle(app, w)
    w.node_box.setCurrentIndex(1)
    for body, shown in ((json.dumps(odd).encode(), "stale"), (b'{"status": "ok"}', "not running"),
                        (b'{"status": null, "curate": 5}', "not running"),
                        (b"[1, 2]", "unreachable"), (b'"hello"', "unreachable")):
        answer[0] = body
        w.st["state"].setText("?")
        w.st["center"].setText("?")
        w.refresh(force=True)
        settle(app, w)
        assert w.st["state"].text().startswith(shown), body
        if shown != "unreachable":
            assert w.st["center"].text() == "-", body


def test_a_candidate_row_cut_short_is_not_listed(app, window):
    w = window()
    settle(app, w)
    head = ["tier", "utc", "local", "sky_freq_hz", "drift_hz_s", "snr", "width_hz", "coverage",
            "thumb"]
    whole = "B,20261112T125735Z,2026-11-12 18:27:35,1420300000,0.2000,30.00,2.9,20.0,x.png"
    w._refresh_cands((head, [whole.split(","), whole.split(",")[:4], whole.split(",")]))
    assert w.c_model.rowCount() == 2
    for r in range(2):
        w.c_table.selectRow(r)
        w.show_cand()                               # every row listed can be opened
        assert w._c_sel == "x.png"


def test_a_scan_row_with_a_day_that_does_not_exist_has_no_time():
    row = ["2026-11-12 09:00:00", "obs_20261112_090000.dat", "1", "2", "3", "20261345T999999Z"]
    assert gui.scan_unix(row) is None
    assert gui.node_tz([row]) is None
    row[0] = "2026-13-45 99:99:99"
    row[5] = "20261112T033000Z"
    assert gui.scan_unix(row) == 1794454200
    assert gui.node_tz([row]) is None
    row[0] = "2026-11-12 09:00:00"
    assert gui.node_tz([row]) == 19800


def test_log_tab_is_left_alone_when_the_log_has_not_changed(app, window, tmp_path):
    """A log written on Windows has CRLF line ends; the tab must not be refilled, and the
    reader thrown back to the bottom, on every poll."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "status.json").write_text(json.dumps(
        {"state": "stopped", "ts": time.time(), "sdr": "synthetic", "name": "test-node"}))
    lines = [f"2026-11-12 09:00:{i % 60:02d}  block {i}: 0 hits" for i in range(300)]
    (data / "survey.log").write_bytes(("\r\n".join(lines) + "\r\n").encode())
    w = window()
    w.tabs.setCurrentIndex(w.log_tab)
    fills = []
    fill = w.log.setPlainText
    w.log.setPlainText = lambda t: (fills.append(1), fill(t))[1]
    for _ in range(4):
        w.refresh(force=True)
        settle(app, w)
    assert len(fills) == 1


def test_download_folder_is_saved_without_undoing_a_hand_edit(app, window, tmp_path, monkeypatch):
    w = window()
    on_disk = json.loads(open(w.cfg_path).read())
    on_disk["center_mhz"] = 1421.5                   # edited while the app is open
    open(w.cfg_path, "w").write(json.dumps(on_disk))
    dest = str(tmp_path / "out")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: dest))

    class NoDownload:
        def __init__(self, *a, **k):
            self.done = self

        def connect(self, fn):
            pass

        def start(self):
            pass

        def isRunning(self):
            return False
    monkeypatch.setattr(gui, "Downloader", NoDownload)
    w._download("hline", "x", "scan x")
    saved = json.loads(open(w.cfg_path).read())
    assert saved["last_download_dir"] == dest
    assert saved["center_mhz"] == 1421.5


# An observer as the app sees it: at start-up it clears a stop file left in the folder (as
# the real one does), then ends cleanly when one appears.
NODE = """
import os, sys, time
stop = os.path.join(sys.argv[1], "stop")
time.sleep(1.5)
if os.path.exists(stop):
    os.remove(stop)
while not os.path.exists(stop):
    time.sleep(0.1)
os.remove(stop)
"""


def start_node(w, monkeypatch):
    monkeypatch.setattr(gui, "STOP_WAIT_S", 12)
    w.proc = QProcess(w)
    w._proc_dir = w.cfg["state_dir"]
    w.proc.start(sys.executable, ["-c", NODE, w._proc_dir])
    assert w.proc.waitForStarted(5000)
    w.proc.terminate = lambda: None          # Windows has no signal to send: the file alone
    return w.proc


def test_stop_during_start_up_is_not_lost(app, window, monkeypatch):
    w = window()
    p = start_node(w, monkeypatch)
    w.stop(refresh=False)
    assert pump(app, lambda: p.state() == QProcess.NotRunning, 8)
    assert p.exitStatus() == QProcess.NormalExit and p.exitCode() == 0
    w._clear_stop()
    assert not os.path.exists(os.path.join(w._proc_dir, "stop"))


def test_closing_during_start_up_does_not_hang(app, window, monkeypatch):
    w = window()
    p = start_node(w, monkeypatch)
    t0 = time.time()
    w._closing = True
    w.close()
    assert time.time() - t0 < 8
    assert p.exitStatus() == QProcess.NormalExit and p.exitCode() == 0
    assert not os.path.exists(os.path.join(w._proc_dir, "stop"))
