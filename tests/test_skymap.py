"""The dish marker's place on the sky. A far node's status supplies the site and the
pointing, and it can hold anything."""
from aquila import skymap

SITE = {"lat": 24.67, "lon": 81.87, "alt": 300}
T = 1_794_325_595.0


def _cfg(dish, site=SITE, mhz=1420.4):
    return {"site": site, "dish": dish, "center_mhz": mhz}


def test_pointing_of_a_set_up_node():
    pt = skymap.pointing(_cfg({"az": 255.0, "el": 73.0, "diameter_m": 2.4}), T)
    assert 0 <= pt["ra_deg"] < 360 and abs(pt["dec_deg"] - 19.3) < 1.0
    assert 5.0 < pt["beam_deg"] < 7.0


def test_no_site_gives_no_pointing():
    assert skymap.pointing(_cfg({"az": 1, "el": 2}, site={"lat": None, "lon": None}), T) is None
    assert skymap.pointing(_cfg({"az": 1, "el": 2}, site=None), T) is None


def test_a_pointing_that_is_no_number_gives_none_and_no_error():
    for dish in ({"az": "west", "el": 2}, {"az": 1}, {}, None, "up", {"az": [1], "el": 2},
                 {"az": float("nan"), "el": 2}, {"az": 1, "el": float("inf")},
                 {"az": 1, "el": 2, "diameter_m": "big"}, {"az": 1, "el": 2, "diameter_m": [3]}):
        assert skymap.pointing(_cfg(dish), T) is None, dish
    for site in ({"lat": "north", "lon": 2}, {"lat": 1, "lon": [2]},
                 {"lat": float("nan"), "lon": 2}):
        assert skymap.pointing(_cfg({"az": 1, "el": 2}, site=site), T) is None, site
    assert skymap.pointing(_cfg({"az": 1, "el": 2, "diameter_m": 1}, mhz=0), T) is None
    assert skymap.pointing(_cfg({"az": 1, "el": 2, "diameter_m": 1}, mhz="L band"), T) is None
