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
