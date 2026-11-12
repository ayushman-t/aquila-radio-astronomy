"""The rotator address a user types: host, host:port, or nothing."""
from aquila import rotator


def test_address_forms():
    assert rotator.parse("") is None and rotator.parse(None) is None
    assert rotator.parse("192.168.1.5") == ("192.168.1.5", 4533)
    assert rotator.parse(" dish.local:4030 ") == ("dish.local", 4030)
    assert rotator.parse("[::1]:4533") == ("::1", 4533)
    assert rotator.parse("dish:rot") is None


def test_port_out_of_range_is_not_tried(monkeypatch):
    def no_socket(*a, **k):
        raise AssertionError("opened a socket")
    monkeypatch.setattr(rotator.socket, "create_connection", no_socket)
    for addr in ("dish:0", "dish:-1", "dish:65536", "dish:99999999999999999999"):
        assert rotator.parse(addr) is None
        assert rotator.read(addr) is None
        assert rotator.pointing({"az": 10.0, "el": 20.0, "rotator": addr}) == {
            "az": 10.0, "el": 20.0, "rotator": addr, "pointing_unknown": True}
