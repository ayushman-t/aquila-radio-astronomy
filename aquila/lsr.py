# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# GPL-3.0-or-later: free to use and modify, derived works must stay open.
"""Velocity with respect to the local standard of rest. A fixed dish measures the line
from a moving place: the Earth goes round the Sun at 30 km/s, turns at up to 0.5 km/s,
and the Sun itself moves at 20 km/s among its neighbours. The part of that motion that
lies along the line of sight is added to the measured velocity. Pure numpy maths (no
astropy), good to about 0.05 km/s."""
import math

import numpy as np

from . import skymap

AU_KM = 149597870.7
EPS = math.radians(23.4392911)                 # obliquity of the ecliptic, J2000
# The standard solar motion (LSRK): 20 km/s toward RA 18 h, Dec +30 deg of 1900, which
# is this direction in J2000.
APEX_RA, APEX_DEC, V_SUN = math.radians(270.9595), math.radians(30.0047), 20.0
V_SPIN = 0.4651                                # km/s at the equator


def _earth_xyz(t_unix):
    """The Earth as seen from the Sun, J2000 equatorial, in km (Meeus ch. 25, low
    precision: 0.01 deg, which is 0.005 km/s on the velocity)."""
    d = np.asarray(t_unix, float) / 86400.0 + 2440587.5 - 2451545.0
    T = d / 36525.0
    L0 = 280.46646 + 36000.76983 * T + 0.0003032 * T * T
    M = np.radians(357.52911 + 35999.05029 * T - 0.0001537 * T * T)
    e = 0.016708634 - 0.000042037 * T
    C = ((1.914602 - 0.004817 * T) * np.sin(M) + (0.019993 - 0.000101 * T) * np.sin(2 * M)
         + 0.000289 * np.sin(3 * M))
    R = 1.000001018 * (1 - e * e) / (1 + e * np.cos(M + np.radians(C)))
    # the Sun's longitude from the equinox of the date, taken back to J2000
    lam = np.radians(L0 + C - 1.396971 * T)
    x, y = -R * np.cos(lam), -R * np.sin(lam)
    return AU_KM * np.array([x, y * math.cos(EPS), y * math.sin(EPS)])


def earth_velocity(t_unix):
    """km/s, J2000 equatorial: array of shape (3,) or (3, n)."""
    h = 1800.0
    return (_earth_xyz(np.asarray(t_unix, float) + h)
            - _earth_xyz(np.asarray(t_unix, float) - h)) / (2 * h)


def correction(ra_deg, dec_deg, t_unix, lat_deg, lon_deg):
    """km/s to add to a measured velocity (positive = receding) to get the velocity with
    respect to the local standard of rest. Arrays or single values."""
    ra, dec = np.radians(np.asarray(ra_deg, float)), np.radians(np.asarray(dec_deg, float))
    t = np.asarray(t_unix, float)
    n = np.array([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)])
    orbit = (earth_velocity(t) * n).sum(0)
    lst = np.radians(skymap.lst_hours(t, lon_deg) * 15.0)
    spin = V_SPIN * math.cos(math.radians(lat_deg)) * np.cos(dec) * np.sin(ra - lst)
    sun = V_SUN * (np.sin(dec) * math.sin(APEX_DEC)
                   + np.cos(dec) * math.cos(APEX_DEC) * np.cos(ra - APEX_RA))
    return orbit + spin + sun


def of_dish(view, t_unix):
    """The correction for each time, for a dish that stays where it points now. view holds
    'dish' (az, el) and 'site' (lat, lon). None without a site or a pointing."""
    site, dish = view.get("site") or {}, view.get("dish") or {}
    try:
        az, el = float(dish["az"]), float(dish["el"])
        lat, lon = float(site["lat"]), float(site["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(x) for x in (az, el, lat, lon)):
        return None
    t = np.atleast_1d(np.asarray(t_unix, float))
    if not t.size:
        return np.zeros(0)
    radec = np.array([skymap.altaz_to_radec(az, el, lat, lon, float(x)) for x in t])
    return correction(radec[:, 0], radec[:, 1], t, lat, lon)


def shifted(rows, corr):
    """History rows with each spectrum moved onto the LSR velocity axis. The rows keep
    their grid: what a row measured at velocity v is shown at v + its correction."""
    from . import map3d
    out = rows.copy()
    v = map3d.V_MID
    for i in range(rows.size):
        out["spec"][i] = np.interp(v - corr[i], v, rows["spec"][i], left=np.nan, right=np.nan)
    return out
