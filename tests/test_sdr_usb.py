from aquila import sdr


def test_usb_match_tells_hydrasdr_from_airspy():
    devs = [("1d50", "60a1", "www.hydrasdr.com HYDRASDR"), ("0bda", "5411", "Realtek Hub")]
    assert sdr.match_usb(devs) == ["hydrasdr"]
    assert sdr.match_usb([("1d50", "60a1", "OpenMoko, Inc. Airspy")]) == ["airspy"]


def test_usb_match_order_and_unknowns():
    devs = [("1d50", "6089", "Great Scott Gadgets HackRF One"), ("0bda", "2838", "RTL2838UHIDIR"),
            ("046d", "c52b", "Logitech")]
    assert sdr.match_usb(devs) == ["rtlsdr", "hackrf"]
    assert sdr.match_usb([]) == []


def test_usb_devices_runs_here():
    assert isinstance(sdr.usb_devices(), list)


def _cfg_gain(name, gain):
    return {"sdr": name, "sample_rate": 0, "center_mhz": 1420.0,
            "advanced": {"gain": gain, "bias_tee": True}}


def test_preset_gain_is_the_maximum():
    assert sdr.HydraSDR(_cfg_gain("hydrasdr", "preset")).gain == {"lna": 14, "mixer": 15, "vga": 15}


def test_chosen_gain_reaches_the_tool():
    cmd = sdr.HydraSDR(_cfg_gain("hydrasdr", {"lna": 14, "mixer": 15, "vga": 5})).command(10)
    assert cmd[cmd.index("-v") + 1] == "5"
    assert cmd[cmd.index("-l") + 1] == "14"


def test_gain_saved_for_another_receiver_is_not_a_crash():
    saved = {"lna": 40, "mixer": 15, "vga": 62}          # e.g. moved from a HackRF
    assert sdr.Airspy(_cfg_gain("airspy", saved)).gain == {"lna": 14, "mixer": 15, "vga": 15}
    assert sdr.RtlSdr(_cfg_gain("rtlsdr", saved)).gain == {"gain": 49.6}


def test_gain_set_for_one_receiver_stays_with_it():
    saved = {"lna": 14, "mixer": 15, "vga": 5, "for": "hydrasdr"}
    assert sdr.HydraSDR(_cfg_gain("hydrasdr", saved)).gain["vga"] == 5
    assert sdr.HackRF(_cfg_gain("hackrf", saved)).gain == sdr.HackRF.preset


def test_rtl_bias_tee_is_switched_off_as_well_as_on(monkeypatch):
    """rtl_biast sets a pin that stays as it is until the dongle is unplugged: with the
    bias tee off in Settings it must be told so, not left as the last run had it."""
    ran = []
    monkeypatch.setattr(sdr, "which", lambda tool: "/x/" + tool)
    monkeypatch.setattr(sdr.subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    for bias, flag in ((True, "1"), (False, "0")):
        cfg = _cfg_gain("rtlsdr", "preset")
        cfg["advanced"]["bias_tee"] = bias
        sdr.RtlSdr(cfg).command(1000)
        assert ran[-1] == ["/x/rtl_biast", "-b", flag]


def test_bias_tee_is_off_on_an_rtl_until_asked_for():
    """Unset in the config, the bias tee follows the receiver: an RTL-SDR stays off, the
    others power their LNA. A value from Settings wins on every receiver."""
    from aquila import config
    assert config.DEFAULTS["advanced"]["bias_tee"] is None
    for name, default in (("rtlsdr", False), ("airspy", True), ("hydrasdr", True),
                          ("hackrf", True)):
        cls = sdr.SOURCES[name]
        assert cls.bias_on({}) is default
        assert cls.bias_on({"bias_tee": None}) is default
        assert cls.bias_on({"bias_tee": True}) is True
        assert cls.bias_on({"bias_tee": False}) is False
    c = _cfg_gain("rtlsdr", "preset")
    c["advanced"]["bias_tee"] = None
    assert sdr.RtlSdr(c).bias is False


def test_unset_bias_tee_is_left_out_of_the_saved_file(tmp_path):
    """An older version reads a missing bias tee as on, and "null" as off: a config saved
    here and opened there must not switch off the LNA of an Airspy."""
    import json

    from aquila import config
    p = str(tmp_path / "config.json")
    c = config.load(p)
    config.save(c, p)
    assert "bias_tee" not in json.load(open(p))["advanced"]
    assert config.load(p)["advanced"]["bias_tee"] is None
    for want in (True, False):
        c["advanced"]["bias_tee"] = want
        config.save(c, p)
        assert json.load(open(p))["advanced"]["bias_tee"] is want
        assert config.load(p)["advanced"]["bias_tee"] is want
