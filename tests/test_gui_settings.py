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
    a.dev_rates = []
    monkeypatch.setattr(sdr, "airspy_rates", lambda: list(a.dev_rates))
    a.units = []
    monkeypatch.setattr(sdr, "rtl_devices", lambda: list(a.units))
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


def _settings(qapp, tmp_path, **over):
    cfg = config.load(scratch(tmp_path, **over))
    dlg = gui.Settings(cfg, None, None)
    assert pump(qapp, lambda: not dlg._scan.isRunning())
    pump(qapp, lambda: False, 0.2)
    return dlg


def _shown_rates(dlg):
    return [dlg.rate.itemData(i) for i in range(dlg.rate.count())]


@pytest.mark.parametrize("own,default,listed", [
    ([3_000_000, 6_000_000], "3 Msps", [0, 3_000_000, 6_000_000]),          # Airspy Mini
    ([2_500_000, 10_000_000], "2.5 Msps", [0, 2_500_000, 10_000_000]),      # Airspy R2
    ([], "3 Msps", [0, 2_500_000, 3_000_000, 6_000_000, 10_000_000]),       # could not be asked
])
def test_settings_lists_the_rates_the_plugged_airspy_has(app, tmp_path, own, default, listed):
    app.plugged = ["airspy"]
    app.dev_rates = own
    dlg = _settings(app, tmp_path, sdr="airspy")
    assert _shown_rates(dlg) == listed
    assert dlg.rate.itemText(0) == f"Device default ({default})"
    assert dlg.apply()["sample_rate"] == 0


def test_settings_keeps_the_list_of_a_receiver_that_is_not_the_one_plugged_in(app, tmp_path):
    """A HydraSDR is plugged in and Airspy is picked: its rates are not the Airspy's."""
    app.installed = {"airspy", "hydrasdr"}
    app.plugged = ["hydrasdr"]
    app.dev_rates = [2_500_000, 5_000_000, 10_000_000]
    dlg = _settings(app, tmp_path, sdr="airspy")
    assert _shown_rates(dlg) == [0, 2_500_000, 3_000_000, 6_000_000, 10_000_000]
    dlg.sdr.setCurrentIndex(dlg.sdr.findData("hydrasdr"))
    assert _shown_rates(dlg) == [0, 2_500_000, 5_000_000, 10_000_000]
    assert dlg.rate.itemText(0) == "Device default (5 Msps)"


def test_settings_keeps_a_saved_rate_the_receiver_does_not_list(app, tmp_path):
    app.plugged = ["airspy"]
    app.dev_rates = [2_500_000, 10_000_000]
    dlg = _settings(app, tmp_path, sdr="airspy", sample_rate=3_000_000)
    assert 3_000_000 in _shown_rates(dlg)
    assert dlg.apply()["sample_rate"] == 3_000_000


def test_settings_greys_out_what_only_the_search_uses(app, tmp_path):
    dlg = _settings(app, tmp_path, app={"observe_on_open": False, "narrowband": False},
                    save={"raw_iq": True}, center_mhz=1421.5)
    assert not dlg.freq.isEnabled() and not dlg.s_raw.isEnabled() and not dlg.fft.isEnabled()
    assert "Not used" in dlg.fnote.text() and "Not used" in dlg.fft_info.text()
    c = dlg.apply()
    assert c["center_mhz"] == 1421.5 and c["save"]["raw_iq"] is True     # kept, not wiped
    dlg.narrow.click()
    assert dlg.freq.isEnabled() and dlg.s_raw.isEnabled() and dlg.fft.isEnabled()
    assert "away from the hydrogen line" in dlg.fnote.text()
    assert "both run" in dlg.fft_info.text()


def test_quick_setup_centre_frequency_is_the_search_s(app, tmp_path):
    cfg = config.load(scratch(tmp_path, app={"observe_on_open": False, "narrowband": False}))
    dlg = gui.QuickSetup(cfg, None, None)
    assert not dlg.freq.isEnabled()
    dlg.mode.setCurrentIndex(1)
    assert dlg.freq.isEnabled()


# ---- two receivers of the same kind

def _entries(dlg):
    return [(dlg.sdr.itemText(i), dlg.sdr.itemData(i)) for i in range(dlg.sdr.count())
            if str(dlg.sdr.itemData(i)).startswith("rtlsdr")]


@pytest.mark.parametrize("dialog", [gui.Settings, gui.QuickSetup])
def test_one_rtl_sdr_adds_nothing_to_the_list(app, tmp_path, dialog):
    app.plugged = ["rtlsdr"]
    app.units = [(0, "00000001")]
    dlg = dialog(config.load(scratch(tmp_path, sdr="rtlsdr")), None, None)
    assert pump(app, lambda: "onnected" in dlg.sdr_result.text())
    assert _entries(dlg) == [("RTL-SDR", "rtlsdr")]
    assert dlg.apply()["advanced"]["device"] == ""
    retire(app, dlg)


@pytest.mark.parametrize("dialog", [gui.Settings, gui.QuickSetup])
def test_two_rtl_sdrs_are_listed_one_by_one(app, tmp_path, dialog):
    app.plugged = ["rtlsdr"]
    app.units = [(0, "NESDR"), (1, "V4-ROOF")]
    dlg = dialog(config.load(scratch(tmp_path, sdr="rtlsdr")), None, None)
    assert pump(app, lambda: dlg.sdr.findData("rtlsdr|NESDR") >= 0)
    assert _entries(dlg) == [("RTL-SDR", "rtlsdr"),
                             ("RTL-SDR #1 (serial NESDR)", "rtlsdr|NESDR"),
                             ("RTL-SDR #2 (serial V4-ROOF)", "rtlsdr|V4-ROOF")]
    assert dlg.sdr.currentData() == "rtlsdr"          # nothing picked for the user
    assert "same serial" not in dlg.sdr_result.text()
    dlg.sdr.setCurrentIndex(dlg.sdr.findData("rtlsdr|V4-ROOF"))
    out = dlg.apply()
    assert out["sdr"] == "rtlsdr" and out["advanced"]["device"] == "V4-ROOF"
    assert sdr.RtlSdr(out).device_args() == ["-d", "V4-ROOF"]
    dlg.sdr.setCurrentIndex(dlg.sdr.findData("rtlsdr"))
    assert dlg.apply()["advanced"]["device"] == ""
    retire(app, dlg)


def test_two_rtl_sdrs_with_one_serial_are_picked_by_number(app, tmp_path):
    app.plugged = ["rtlsdr"]
    app.units = [(0, "00000001"), (1, "00000001")]
    dlg = gui.Settings(config.load(scratch(tmp_path, sdr="auto")), None, None)
    assert pump(app, lambda: dlg.sdr.findData("rtlsdr|1") >= 0)
    assert _entries(dlg)[1:] == [("RTL-SDR #1 (serial 00000001)", "rtlsdr|0"),
                                 ("RTL-SDR #2 (serial 00000001)", "rtlsdr|1")]
    assert dlg.sdr.currentData() == "auto"
    assert gui.SAME_SERIAL_TEXT in dlg.sdr_result.text()
    retire(app, dlg)


@pytest.mark.parametrize("dialog", [gui.Settings, gui.QuickSetup])
def test_the_saved_rtl_sdr_stays_picked(app, tmp_path, dialog):
    """Shown at once, before the receivers are looked for, and after: OK changes nothing."""
    app.plugged = ["rtlsdr"]
    app.units = [(0, "NESDR"), (1, "V4-ROOF")]
    cfg = config.load(scratch(tmp_path, sdr="rtlsdr", advanced={"device": "V4-ROOF"}))
    dlg = dialog(cfg, None, None)
    assert dlg.sdr.currentData() == "rtlsdr|V4-ROOF"
    assert dlg.apply()["advanced"]["device"] == "V4-ROOF"
    assert pump(app, lambda: dlg.sdr.findData("rtlsdr|NESDR") >= 0)
    assert _entries(dlg) == [("RTL-SDR", "rtlsdr"),
                             ("RTL-SDR #1 (serial NESDR)", "rtlsdr|NESDR"),
                             ("RTL-SDR #2 (serial V4-ROOF)", "rtlsdr|V4-ROOF")]
    assert dlg.sdr.currentData() == "rtlsdr|V4-ROOF"
    out = dlg.apply()
    assert out["sdr"] == "rtlsdr" and out["advanced"]["device"] == "V4-ROOF"
    retire(app, dlg)


def test_a_saved_rtl_sdr_stays_while_observing_or_unplugged(app, tmp_path):
    cfg = config.load(scratch(tmp_path, sdr="rtlsdr", advanced={"device": "NESDR"}))
    dlg = gui.Settings(cfg, None, "rtlsdr")              # observing: nothing is looked for
    assert _entries(dlg) == [("RTL-SDR", "rtlsdr"), ("RTL-SDR (NESDR)", "rtlsdr|NESDR")]
    assert dlg.gain_for is sdr.SOURCES["rtlsdr"]
    assert dlg.apply()["advanced"]["device"] == "NESDR"
    retire(app, dlg)


def test_another_receiver_picked_leaves_the_saved_rtl_sdr_alone(app, tmp_path):
    cfg = config.load(scratch(tmp_path, sdr="rtlsdr", advanced={"device": "NESDR"}))
    dlg = gui.Settings(cfg, None, None)
    dlg.sdr.setCurrentIndex(dlg.sdr.findData("airspy"))
    out = dlg.apply()
    assert out["sdr"] == "airspy" and out["advanced"]["device"] == "NESDR"
    retire(app, dlg)


def test_a_saved_rtl_sdr_is_not_offered_without_its_software(app, tmp_path):
    app.installed = {"airspy"}
    cfg = config.load(scratch(tmp_path, sdr="rtlsdr", advanced={"device": "NESDR"}))
    dlg = gui.QuickSetup(cfg, None, None)
    assert dlg.sdr.findData("rtlsdr|NESDR") < 0
    out = dlg.apply()
    assert out["sdr"] == "auto" and out["advanced"]["device"] == "NESDR"   # left as saved
    retire(app, dlg)


# ---- receivers read through their maker's library

def test_a_library_receiver_is_listed_only_when_its_library_is_installed(app, tmp_path):
    cfg = config.load(scratch(tmp_path))
    dlg = gui.Settings(cfg, None, "auto")
    assert all(dlg.sdr.findData(n) < 0 for n in ("sdrplay", "pluto", "bladerf", "limesdr"))
    retire(app, dlg)
    app.installed |= {"sdrplay"}
    dlg = gui.Settings(cfg, None, "auto")
    at = dlg.sdr.findData("sdrplay")
    assert at >= 0 and dlg.sdr.itemText(at) == "SDRplay RSP (untested)"
    assert dlg.sdr.findData("pluto") < 0
    dlg.sdr.setCurrentIndex(at)
    app.processEvents()
    # its own gain boxes at most gain, its own rates, and no voltage on the coax
    assert {k: b.value() for k, b in dlg.gain_boxes.items()} == {"lna": 0, "if": 20}
    assert not dlg.bias.isChecked()
    out = dlg.apply()
    assert out["sdr"] == "sdrplay"
    retire(app, dlg)


def test_a_plugged_library_receiver_is_what_auto_shows(app, tmp_path):
    app.installed = {"airspy", "rtlsdr", "sdrplay"}
    app.plugged = ["sdrplay", "pluto"]
    dlg = gui.Settings(config.load(scratch(tmp_path, sdr="auto")), None, None)
    assert pump(app, lambda: "Connected" in dlg.sdr_result.text())
    assert dlg.sdr_result.text() == ("Not observing. Connected: SDRplay RSP (untested), "
                                     "ADALM-Pluto (untested) (needs libiio installed)")
    assert dlg.auto_sdr == "sdrplay" and set(dlg.gain_boxes) == {"lna", "if"}
    retire(app, dlg)
