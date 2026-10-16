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
