# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# MIT licence: free for anyone to use and modify.
"""Where the dish points on the sky. Pure numpy maths (no astropy): horizon to
equatorial, equatorial to galactic, the galactic plane track, and an optional
all-sky background image fetched once from the CDS hips2fits service into the
user's cache (Mellinger optical panorama, credit shown on the map). Without the
image the map still draws, just on a plain background."""
import math
import os
import urllib.request

import numpy as np

HIPS_URL = ("https://alasky.cds.unistra.fr/hips-image-services/hips2fits"
            "?hips=CDS%2FP%2FMellinger%2Fcolor&width=1800&height=900&projection=CAR"
            "&coordsys=icrs&ra=0&dec=0&fov=360&format=png")
CREDIT = "Background: A. Mellinger optical panorama via CDS hips2fits"

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
    return ra, math.degrees(dec)


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


def cache_path():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    new = os.path.join(base, "aquila-radio-astronomy", "skymap_mellinger.png")
    old = os.path.join(base, "aquila-seti", "skymap_mellinger.png")
    return old if os.path.exists(old) and not os.path.exists(new) else new


def background(fetch=True, timeout=30):
    """Path to the cached all-sky PNG, downloading it once if allowed. None if unavailable."""
    p = cache_path()
    if os.path.exists(p) and os.path.getsize(p) > 10000:
        return p
    if not fetch:
        return None
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with urllib.request.urlopen(HIPS_URL, timeout=timeout) as r, open(p + ".tmp", "wb") as f:
            f.write(r.read())
        os.replace(p + ".tmp", p)
        return p
    except Exception:
        return None


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
