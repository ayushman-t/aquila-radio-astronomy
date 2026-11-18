"""Receivers read through their maker's library (ADALM-Pluto, bladeRF, LimeSDR, SDRplay).
A stand-in for each library is built from tests/fake_vendor, so the real reader process
runs end to end with no hardware: what it tells the library is logged, and the samples
it hands back are known. Where the maker's header is on this machine the stand-in is
built against it, which checks every call's form and, for SDRplay, the settings blocks.
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys

import numpy as np
import pytest

from aquila import config, sdr, vendor_rx

HERE = os.path.dirname(os.path.abspath(__file__))
FAKES = os.path.join(HERE, "fake_vendor")
SOURCE = {"pluto": "fake_iio.c", "bladerf": "fake_bladerf.c", "limesdr": "fake_lime.c",
          "sdrplay": "fake_sdrplay.c"}
ALL = sorted(SOURCE)
_built = {}


def _build(name, folder):
    """The stand-in library for one receiver, or None when this machine cannot build it."""
    if name in _built:
        return _built[name]
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    out = os.path.join(folder, f"fake_{name}" + (".dll" if sys.platform == "win32" else ".so"))
    path = None
    if cc:
        base = [cc, "-shared", "-fPIC", "-O1", "-o", out, os.path.join(FAKES, SOURCE[name])]
        hdr = os.environ.get("AQUILA_SDRPLAY_HDR", "")
        real = base + ["-DREAL_HDR"] + (["-I", hdr] if hdr else [])
        for cmd in (real, base):                    # against the maker's header when it is here
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0 and os.path.isfile(out):
                try:
                    ctypes.CDLL(out)                # a build this Python cannot load is no use
                except OSError:
                    continue
                path = out
                break
    _built[name] = path
    return path


@pytest.fixture(scope="session")
def fake_dir(tmp_path_factory):
    return str(tmp_path_factory.mktemp("fake_vendor"))


@pytest.fixture
def lib(monkeypatch, tmp_path, fake_dir):
    """Call with a receiver name: its library is 'installed' for this process and for
    the reader it starts. Returns a function giving what the library was told."""
    log = tmp_path / "told.log"
    monkeypatch.setenv("FAKE_RX_LOG", str(log))
    for n in ALL:
        monkeypatch.setenv("AQUILA_RX_LIB_" + n.upper(), str(tmp_path / "absent"))

    def install(name):
        path = _build(name, fake_dir)
        if not path:
            pytest.skip("no C compiler to build the stand-in library")
        monkeypatch.setenv("AQUILA_RX_LIB_" + name.upper(), path)
        return lambda: log.read_text().splitlines() if log.exists() else []
    return install


def _cfg(tmp_path, name, rate=0, **adv):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"sdr": name, "state_dir": str(tmp_path / "s"),
                             "center_mhz": 1420.0, "sample_rate": rate, "advanced": adv}))
    return config.load(str(p))


def _capture(src, n=98_304):
    src.start(n, 16384)
    raw = b"".join(src.frames(16384))
    err = src.finish()
    return np.frombuffer(raw, np.int16), err


def _expected(n):
    i = 16 * ((np.arange(n) % 2000) - 1000)
    return i, -i


# ---- offered only where they can work, and never by themselves

def test_listed_only_once_the_library_is_installed(lib):
    names = [c[0] for c in sdr.choices()]
    assert not set(ALL) & set(names)
    assert not any(sdr.SOURCES[n].available() for n in ALL)
    lib("pluto")
    assert sdr.SOURCES["pluto"].available()
    got = {c[0]: c for c in sdr.choices()}
    assert got["pluto"][1:3] == ("ADALM-Pluto (untested)", True) and "sdrplay" not in got


@pytest.fixture
def bench(monkeypatch):
    """Which tools are installed, what is plugged in and which receivers answer, for
    Auto to choose from. Returns the list of receivers Auto opened to ask."""
    asked = []

    def setup(tools, plugged, answering=()):
        monkeypatch.setattr(sdr, "which", lambda tool: tool if tool in tools else None)
        monkeypatch.setattr(sdr, "attached", lambda: list(plugged))

        def probe(cfg, seconds=0.5):
            asked.append(cfg["sdr"])
            return cfg["sdr"] in answering, ""
        monkeypatch.setattr(sdr, "probe", probe)
        return asked
    return setup


def _auto(tmp_path):
    return sdr.make(_cfg(tmp_path, "auto")).name


@pytest.mark.parametrize("name", ALL)
def test_auto_takes_one_that_is_plugged_in_when_no_other_receiver_is(lib, bench, tmp_path,
                                                                    name):
    lib(name)
    assert name not in sdr.detect()                 # not among the tools tried one by one
    # the packaged app: every tool is there, none has a radio on it
    bench(("airspy_rx", "rtl_sdr", "hackrf_transfer"), [name])
    assert _auto(tmp_path) == name
    # a source install with one tool, and with none
    bench(("rtl_sdr",), [name])
    assert _auto(tmp_path) == name
    bench((), [name])
    assert _auto(tmp_path) == name


def test_auto_stays_with_a_receiver_that_has_its_own_tool(lib, bench, tmp_path):
    lib("pluto")
    # both plugged in, the Airspy answers: it is taken and the USB list is not even read
    asked = bench(("airspy_rx", "rtl_sdr"), ["airspy", "pluto"], answering=("airspy",))
    assert _auto(tmp_path) == "airspy" and asked == ["airspy"]
    # one tool only: its receiver is plugged in beside the Pluto, and is not opened to ask
    asked = bench(("rtl_sdr",), ["rtlsdr", "pluto"])
    del asked[:]
    assert _auto(tmp_path) == "rtlsdr" and asked == []
    # nothing plugged in at all: as before
    bench(("airspy_rx", "rtl_sdr"), [])
    assert _auto(tmp_path) == "airspy"
    bench((), [])
    with pytest.raises(RuntimeError, match="no SDR tool found"):
        _auto(tmp_path)


def test_auto_does_not_take_one_whose_library_is_missing(lib, bench, tmp_path):
    lib("pluto")
    bench(("airspy_rx", "rtl_sdr"), ["sdrplay"])    # an RSP plugged in, no SDRplay API
    assert _auto(tmp_path) == "airspy"
    bench(("airspy_rx", "rtl_sdr"), ["sdrplay", "pluto"])
    assert _auto(tmp_path) == "pluto"


def test_one_without_its_library_says_what_to_install(lib, tmp_path):
    with pytest.raises(RuntimeError, match="SDRplay RSP .* needs SDRplay API installed"):
        sdr.make(_cfg(tmp_path, "sdrplay"))


def test_their_usb_ids_are_known():
    seen = sdr.match_usb([("0456", "b673", "PlutoSDR"), ("2cf0", "5250", "bladeRF 2.0"),
                          ("0403", "601f", "LimeSDR Mini"), ("1df7", "3000", "RSP1A"),
                          ("1d50", "6089", "HackRF One")])
    assert seen == ["hackrf", "sdrplay", "pluto", "bladerf", "limesdr"]


# ---- the samples

@pytest.mark.parametrize("name", ALL)
def test_samples_come_in_order_at_full_scale(lib, tmp_path, name):
    """I then Q, 16 bit full scale whatever the receiver's own word is, none lost and
    none from before the tuning."""
    lib(name)
    n = 98_304
    got, err = _capture(sdr.make(_cfg(tmp_path, name)), n)
    assert len(got) == 2 * n, err
    i, q = _expected(n)
    assert np.array_equal(got[0::2], i) and np.array_equal(got[1::2], q)


@pytest.mark.parametrize("name", ["pluto", "bladerf", "limesdr"])
def test_a_rate_the_receiver_does_not_have_is_an_error_not_a_wrong_spectrum(
        lib, tmp_path, monkeypatch, name):
    lib(name)
    monkeypatch.setenv("FAKE_RX_RATE", "2400000")
    got, err = _capture(sdr.make(_cfg(tmp_path, name, rate=2_500_000)), 20_000)
    assert len(got) == 0
    assert "gives 2400000 samples a second, not the 2500000 asked for" in err


def test_sdrplay_refuses_a_rate_outside_its_range(lib, tmp_path):
    told = lib("sdrplay")
    got, err = _capture(sdr.make(_cfg(tmp_path, "sdrplay", rate=1_000_000)), 20_000)
    assert len(got) == 0 and "not the 1000000 asked for" in err
    assert told() == []                             # the receiver was not even opened


@pytest.mark.parametrize("name", ["pluto", "bladerf", "limesdr"])
def test_a_silent_receiver_ends_with_a_reason(lib, tmp_path, monkeypatch, name):
    lib(name)
    monkeypatch.setenv("FAKE_RX_SILENT", "1")
    got, err = _capture(sdr.make(_cfg(tmp_path, name)), 20_000)
    assert len(got) == 0 and "no samples from the receiver" in err


@pytest.mark.parametrize("name,text", [("pluto", "no ADALM-Pluto found"),
                                       ("bladerf", "no bladeRF found"),
                                       ("limesdr", "no LimeSDR found"),
                                       ("sdrplay", "no SDRplay receiver found")])
def test_no_receiver_plugged_in_says_so(lib, tmp_path, monkeypatch, name, text):
    lib(name)
    monkeypatch.setenv("FAKE_RX_NONE", "1")
    got, err = _capture(sdr.make(_cfg(tmp_path, name)), 20_000)
    assert len(got) == 0 and text in err


def test_sdrplay_without_its_service_says_so(lib, tmp_path, monkeypatch):
    lib("sdrplay")
    monkeypatch.setenv("FAKE_RX_NOSERVICE", "1")
    _, err = _capture(sdr.make(_cfg(tmp_path, "sdrplay")), 20_000)
    assert "the SDRplay API service is not running" in err


# ---- what each library is told: tuning, full gain by default, automatic gain off

def test_pluto_settings(lib, tmp_path):
    told = lib("pluto")
    src = sdr.make(_cfg(tmp_path, "pluto"))
    _, err = _capture(src, 40_000)
    t = told()
    assert "open usb:1.2.5" in t
    assert "set voltage0.sampling_frequency 2500000" in t
    assert "set voltage0.rf_bandwidth 2500000" in t
    assert "set altvoltage0.frequency 1420000000" in t
    assert "set voltage0.gain_control_mode manual" in t
    assert "set voltage0.hardwaregain 71" in t      # the top the receiver gives here, not 73
    assert t.index("set voltage0.gain_control_mode manual") < t.index(
        "set voltage0.hardwaregain 71")
    assert "gains set: gain=71" in err and "bias tee: none on this receiver" in err
    assert src.gain == {"gain": 71.0} and t[-1] == "close"


def test_pluto_saved_gain_and_serial(lib, tmp_path):
    told = lib("pluto")
    cfg = _cfg(tmp_path, "pluto", gain={"for": "pluto", "gain": 40}, device="104473FAKE")
    _capture(sdr.make(cfg), 20_000)
    assert "set voltage0.hardwaregain 40" in told()
    other = _cfg(tmp_path, "pluto", device="SOMEONE-ELSE")
    _, err = _capture(sdr.make(other), 20_000)
    assert "no ADALM-Pluto found with serial SOMEONE-ELSE" in err


def test_bladerf_full_gain_sets_every_stage_by_name(lib, tmp_path):
    told = lib("bladerf")
    src = sdr.make(_cfg(tmp_path, "bladerf"))
    _, err = _capture(src, 40_000)
    t = told()
    assert "open []" in t and "rate 0 5000000" in t and "width 0 5000000" in t
    assert "frequency 0 1420000000" in t
    assert "gainmode 0 1" in t                      # manual
    assert [x for x in t if x.startswith("stage")] == [
        "stage 0 lna 6", "stage 0 rxvga1 30", "stage 0 rxvga2 30"]
    assert not any(x.startswith("gain 0") for x in t)       # not the shared-out overall gain
    assert "bias 0 0" in t                          # no voltage on the coax until asked
    assert "sync 0 0 16 8192 8 3500" in t and "module 0 1" in t
    assert t[-2:] == ["module 0 0", "close"]
    assert "gains set: gain=66, lna=6, rxvga1=30, rxvga2=30" in err
    assert src.gain == {"gain": 66.0, "lna": 6.0, "rxvga1": 30.0, "rxvga2": 30.0}


def test_bladerf_lower_gain_bias_and_serial(lib, tmp_path, monkeypatch):
    told = lib("bladerf")
    cfg = _cfg(tmp_path, "bladerf", gain={"for": "bladerf", "gain": 40}, bias_tee=True,
               device="f12fakef12fakef12fakef12fakef12f")
    _, err = _capture(sdr.make(cfg), 20_000)
    t = told()
    assert "open [*:serial=f12fakef12fakef12fakef12fakef12f]" in t
    assert "gain 0 40" in t and not any(x.startswith("stage") for x in t)
    assert "bias 0 1" in t and "bias tee: on" in err
    monkeypatch.setenv("FAKE_RX_NOBIAS", "1")
    _, err = _capture(sdr.make(cfg), 20_000)
    assert "bias tee: none on this receiver" in err


def test_limesdr_settings(lib, tmp_path):
    told = lib("limesdr")
    src = sdr.make(_cfg(tmp_path, "limesdr", device="1D3AFAKE0C7E51"))
    _, err = _capture(src, 40_000)
    t = told()
    assert t[0].startswith("open LimeSDR Mini,") and "serial=1D3AFAKE0C7E51" in t[0]
    assert "channel 0 0 1" in t and "rate 5000000 0" in t
    assert "frequency 0 0 1420000000" in t
    assert "antenna 0 0 255" in t and "input: LNAW" in err
    assert "gain 0 0 73" in t and "gains set: gain=70" in err      # as the library reports
    assert "width 0 0 5000000" in t and "calibrate 0 0 5000000 0" in t
    assert "stream tx=0 channel=0 fifo=1048576 bias=1.0 fmt=1 link=0" in t
    assert t[-4:] == ["start 77", "stop 77", "destroy 77", "close"]
    assert src.gain == {"gain": 70.0}


def test_sdrplay_settings(lib, tmp_path):
    told = lib("sdrplay")
    src = sdr.make(_cfg(tmp_path, "sdrplay"))
    _, err = _capture(src, 40_000)
    t = told()
    assert "select FAKE1A0001 tuner=0 duo=0" in t
    # most gain is the least reduction; automatic gain off; the widest filter the rate has
    assert "init fs=6000000 rf=1420000000 bw=6000 if=0 lna=0 grdb=20 agc=0" in t
    assert "bias rsp1a=0 rsp2=0 ant2=5 duo=0 dx=0 antdx=0" in t
    assert "update tuner=1 reason=0x04000000 ext=0" in t    # the overload was answered
    assert t[-3:] == ["uninit", "release FAKE1A0001", "close"]
    assert "gains set: lna=0, if=20, total=80" in err and "bias tee: off" in err
    assert "overloaded 1 times" in err
    assert src.gain == {"lna": 0.0, "if": 20.0, "total": 80.0}


def test_sdrplay_saved_gains_and_filter_for_a_low_rate(lib, tmp_path):
    told = lib("sdrplay")
    cfg = _cfg(tmp_path, "sdrplay", rate=2_500_000,
               gain={"for": "sdrplay", "lna": 3, "if": 40})
    _capture(sdr.make(cfg), 20_000)
    assert "init fs=2500000 rf=1420000000 bw=1536 if=0 lna=3 grdb=40 agc=0" in told()


def test_sdrplay_an_lna_step_past_the_last_is_explained(lib, tmp_path):
    lib("sdrplay")
    cfg = _cfg(tmp_path, "sdrplay", gain={"for": "sdrplay", "lna": 15})
    got, err = _capture(sdr.make(cfg), 20_000)
    assert len(got) == 0 and "LNA reduction 15 may be past its last step" in err


@pytest.mark.parametrize("model,select,bias,note", [
    (255, "tuner=0 duo=0", "bias rsp1a=1 rsp2=0 ant2=5 duo=0 dx=0 antdx=0", "bias tee: on"),
    (6, "tuner=0 duo=0", "bias rsp1a=1 rsp2=0 ant2=5 duo=0 dx=0 antdx=0", "bias tee: on"),
    (2, "tuner=0 duo=0", "bias rsp1a=0 rsp2=1 ant2=6 duo=0 dx=0 antdx=0",
     "bias tee: on (antenna B)"),
    (3, "tuner=2 duo=1", "bias rsp1a=0 rsp2=0 ant2=0 duo=1 dx=0 antdx=0",
     "bias tee: on (tuner 2)"),
    (4, "tuner=0 duo=0", "bias rsp1a=0 rsp2=0 ant2=5 duo=0 dx=1 antdx=1",
     "bias tee: on (antenna B)"),
    (1, "tuner=0 duo=0", "bias rsp1a=0 rsp2=0 ant2=5 duo=0 dx=0 antdx=0",
     "bias tee: none on this receiver"),
])
def test_sdrplay_bias_tee_goes_to_the_right_place_for_each_model(
        lib, tmp_path, monkeypatch, model, select, bias, note):
    told = lib("sdrplay")
    monkeypatch.setenv("FAKE_RX_MODEL", str(model))
    n = 32_768
    got, err = _capture(sdr.make(_cfg(tmp_path, "sdrplay", bias_tee=True)), n)
    t = told()
    assert "select FAKE1A0001 " + select in t and bias in t and note in err
    i, q = _expected(n)                             # an RSPduo's second tuner streams too
    assert np.array_equal(got[0::2], i) and np.array_equal(got[1::2], q)


def test_sdrplay_duo_without_bias_uses_its_first_tuner(lib, tmp_path, monkeypatch):
    told = lib("sdrplay")
    monkeypatch.setenv("FAKE_RX_MODEL", "3")
    _capture(sdr.make(_cfg(tmp_path, "sdrplay")), 20_000)
    assert "select FAKE1A0001 tuner=1 duo=1" in told()


# ---- the rest of the app on top of them

@pytest.mark.parametrize("name,serial", [("pluto", "104473FAKE"),
                                         ("bladerf", "f12fakef12fakef12fakef12fakef12f"),
                                         ("limesdr", "1D3AFAKE0C7E51"),
                                         ("sdrplay", "FAKE1A0001")])
def test_find_lists_the_receiver_with_its_serial(lib, capfd, name, serial):
    lib(name)
    assert vendor_rx.main([name, "find"]) == 0
    found = json.loads(capfd.readouterr().out)
    assert len(found) == 1 and found[0]["serial"] == serial and found[0]["label"]


@pytest.mark.parametrize("name,gains", [("pluto", "gain 71.0"), ("bladerf", "rxvga2 30.0"),
                                        ("limesdr", "gain 70.0"), ("sdrplay", "if 20.0")])
def test_check_reports_the_gains_the_library_set(lib, tmp_path, name, gains):
    lib(name)
    ok, msg = sdr.probe(_cfg(tmp_path, name), seconds=0.05)
    assert ok, msg
    assert gains in msg and "bias tee off" in msg and "(untested) OK" in msg


def test_the_packaged_app_reads_them_through_its_own_verb(lib, monkeypatch, capfd):
    from aquila import cli
    assert sdr.vendor_cmd("pluto")[0] == sys.executable and "-c" in sdr.vendor_cmd("pluto")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert sdr.vendor_cmd("pluto") == [sys.executable, "vendor-rx", "pluto"]
    lib("pluto")
    with pytest.raises(SystemExit) as e:
        cli.main(["vendor-rx", "pluto", "find"])
    assert e.value.code == 0
    assert json.loads(capfd.readouterr().out)[0]["serial"] == "104473FAKE"


def test_a_hydrogen_line_scan_runs_on_one(lib, tmp_path):
    from aquila import cli
    lib("bladerf")
    p = tmp_path / "c.json"
    p.write_text(json.dumps({
        "sdr": "bladerf", "state_dir": str(tmp_path / "s"),
        "advanced": {"api_port": 18474},
        "hline": {"duration_s": 8, "fs_phase_s": 2}, "save": {"plots": False},
        "app": {"narrowband": False}}))
    assert not cli.main(["-c", str(p), "hline", "--blocks", "1"])
    rows = (tmp_path / "s" / "hline" / "scans.csv").read_text().splitlines()
    assert len(rows) == 2


def test_packaged_app_knows_every_verb():
    """The packaged app adds "gui" to a command line with no verb it knows: a verb
    missing from its list never reaches the command line parser."""
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    entry = open(os.path.join(root, "packaging", "pyinstaller", "entry.py")).read()
    known = set(re.findall(r'"([a-z0-9-]+)"', entry.split("VERBS = {")[1].split("}")[0]))
    cli_src = open(os.path.join(root, "aquila", "cli.py")).read()
    assert set(re.findall(r'add_parser\("([a-z0-9-]+)"', cli_src)) == known


def test_pluto_without_a_serial_is_still_listed():
    """A Pluto whose serial cannot be read comes as "... serial=" and nothing more."""
    from aquila import vendor_rx
    head = "0456:b673 (Analog Devices Inc. PlutoSDR (ADALM-PLUTO))"
    assert vendor_rx._iio_serial(head + ", serial=104473FAKE") == "104473FAKE"
    assert vendor_rx._iio_serial(head + ", serial=") == ""
    assert vendor_rx._iio_serial(head + ", serial= ") == ""
    assert vendor_rx._iio_serial(head) == ""
