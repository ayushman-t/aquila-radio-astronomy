# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# MIT licence: free for anyone to use and modify.
"""Where the dish points on the sky. Pure numpy maths (no astropy): horizon to
equatorial, equatorial to galactic, the galactic plane track. The background is
the ESA Gaia EDR3 all-sky map (ESA/Gaia/DPAC, CC BY-SA 3.0 IGO), drawn from measured
star positions, reprojected once from galactic to equatorial coordinates and shipped
with the app, so nothing is downloaded."""
import math
import os

import numpy as np

BACKGROUND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "sky.jpg")
CREDIT = "Background: ESA/Gaia/DPAC all-sky map (CC BY-SA 3.0 IGO)"

RA_NGP, DEC_NGP, L_NCP = math.radians(192.85948), math.radians(27.12825), math.radians(122.93192)


def lst_hours(t_unix, lon_deg):
    d = t_unix / 86400.0 + 2440587.5 - 2451545.0
    return (18.697374558 + 24.06570982441908 * d + lon_deg / 15.0) % 24.0


def altaz_to_radec(az, el, lat, lon, t_unix):
    """Returns (ra_deg, dec_deg)."""
    az_r, el_r, lat_r = map(math.radians, (az, el, lat))
    sin_dec = math.sin(el_r) * math.sin(lat_r)
    sin_dec += math.cos(el_r) * math.cos(lat_r) * math.cos(az_r)
    dec = math.asin(max(-1.0, min(1.0, sin_dec)))
    cos_ha = (math.sin(el_r) - math.sin(dec) * math.sin(lat_r))
    cos_ha /= math.cos(dec) * math.cos(lat_r) + 1e-12
    ha = math.acos(max(-1.0, min(1.0, cos_ha)))
    if math.sin(az_r) > 0:
        ha = -ha
    ra = (lst_hours(t_unix, lon) * 15.0 - math.degrees(ha)) % 360.0
    return to_j2000(ra, math.degrees(dec), t_unix)


def to_j2000(ra_deg, dec_deg, t_unix):
    """Precess equinox-of-date coordinates back to J2000, so the dish marker sits on
    the same frame as the background image and the galactic plane (Meeus ch. 21)."""
    T = (t_unix / 86400.0 + 2440587.5 - 2451545.0) / 36525.0
    zeta = math.radians((2306.2181 * T + 0.30188 * T * T + 0.017998 * T ** 3) / 3600.0)
    z = math.radians((2306.2181 * T + 1.09468 * T * T + 0.018203 * T ** 3) / 3600.0)
    theta = math.radians((2004.3109 * T - 0.42665 * T * T - 0.041833 * T ** 3) / 3600.0)
    ra, dec = math.radians(ra_deg), math.radians(dec_deg)
    # inverse rotation: date -> J2000
    a = math.cos(dec) * math.sin(ra - z)
    b = math.cos(theta) * math.cos(dec) * math.cos(ra - z) + math.sin(theta) * math.sin(dec)
    c = -math.sin(theta) * math.cos(dec) * math.cos(ra - z) + math.cos(theta) * math.sin(dec)
    ra0 = (math.atan2(a, b) - zeta) % (2 * math.pi)
    dec0 = math.asin(max(-1.0, min(1.0, c)))
    return math.degrees(ra0), math.degrees(dec0)


def radec_to_galactic(ra_deg, dec_deg):
    ra, dec = math.radians(ra_deg), math.radians(dec_deg)
    sin_b = math.sin(dec) * math.sin(DEC_NGP)
    sin_b += math.cos(dec) * math.cos(DEC_NGP) * math.cos(ra - RA_NGP)
    b = math.asin(max(-1.0, min(1.0, sin_b)))
    y = math.cos(dec) * math.sin(ra - RA_NGP)
    x = math.sin(dec) * math.cos(DEC_NGP)
    x -= math.cos(dec) * math.sin(DEC_NGP) * math.cos(ra - RA_NGP)
    lon = (L_NCP - math.atan2(y, x)) % (2 * math.pi)
    return math.degrees(lon), math.degrees(b)


def galactic_to_radec(l_deg, b_deg):
    """Vectorised. Returns (ra_deg, dec_deg) arrays."""
    lon = np.radians(np.asarray(l_deg, float))
    b = np.radians(np.asarray(b_deg, float))
    sin_dec = np.sin(b) * math.sin(DEC_NGP) + np.cos(b) * math.cos(DEC_NGP) * np.cos(L_NCP - lon)
    dec = np.arcsin(np.clip(sin_dec, -1, 1))
    y = np.cos(b) * np.sin(L_NCP - lon)
    x = np.sin(b) * math.cos(DEC_NGP) - np.cos(b) * math.sin(DEC_NGP) * np.cos(L_NCP - lon)
    ra = (RA_NGP + np.arctan2(y, x)) % (2 * np.pi)
    return np.degrees(ra), np.degrees(dec)


def plane_track(b_deg=0.0, n=721):
    return galactic_to_radec(np.linspace(0, 360, n), np.full(n, b_deg))


def beam_deg(diameter_m, freq_hz=1420.4e6):
    if not diameter_m:
        return 0.0
    return 70.0 * (299792458.0 / freq_hz) / float(diameter_m)


def split_wrap(x, y, wrap=180.0):
    """Split a track into segments wherever it jumps across the RA seam."""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    cut = np.where(np.abs(np.diff(x)) > wrap)[0]
    return [(x[i], y[i]) for i in np.split(np.arange(len(x)), cut + 1) if len(i) > 1]


def background():
    """Path to the all-sky image, or None if the file is missing."""
    return BACKGROUND if os.path.exists(BACKGROUND) else None


def pointing(cfg, t_unix):
    """Everything the map needs for the dish right now, or None without a site."""
    site = cfg.get("site") or {}
    if site.get("lat") is None or site.get("lon") is None:
        return None
    az, el = float(cfg["dish"]["az"]), float(cfg["dish"]["el"])
    ra, dec = altaz_to_radec(az, el, float(site["lat"]), float(site["lon"]), t_unix)
    gl, gb = radec_to_galactic(ra, dec)
    return {"az": az, "el": el, "ra_deg": ra, "dec_deg": dec, "l": gl, "b": gb,
            "lst_h": lst_hours(t_unix, float(site["lon"])),
            "beam_deg": beam_deg(cfg["dish"].get("diameter_m"), cfg["center_mhz"] * 1e6)}
