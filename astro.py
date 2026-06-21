"""
Pure-math astronomy helpers for the GPS Clock app (offline, no network).

Sunrise/sunset use the standard "sunrise equation"; moon phase uses a synodic-
month calculation. Everything is kept in small "days since J2000" magnitudes
using an *integer* Julian Day Number, because the badge's MicroPython floats are
reduced precision and cannot represent a full Julian Date (~2.45e6) to fractional-
day accuracy. Accurate enough for a watch face (sunrise within a minute or two;
moon illumination within a few percent).

License: MIT
"""
import math

_RAD = math.pi / 180.0
_DEG = 180.0 / math.pi
_SYNODIC = 29.530588853          # mean synodic month (days)
_J2000 = 2451545                 # JDN of 2000-01-01 12:00 UTC
_NEW_MOON_OFF = 5.1              # 2451550.1 - 2451545  (a known new moon)


def jdn(y, mo, d):
    """Integer Julian Day Number (exact; arbitrary-precision int)."""
    a = (14 - mo) // 12
    yy = y + 4800 - a
    mm = mo + 12 * a - 3
    return (d + (153 * mm + 2) // 5 + 365 * yy
            + yy // 4 - yy // 100 + yy // 400 - 32045)


def days_since_2000(y, mo, d):
    """Whole days from 2000-01-01 (small integer; exact)."""
    return jdn(y, mo, d) - _J2000


def sun_times(lat, lon, y, mo, d):
    """Sunrise, sunset and solar noon as fractional UTC hours (lon east-positive).

    Returns (sunrise, sunset, solar_noon); sunrise/sunset are None during polar
    day/night (solar_noon is still returned). All intermediate values are small,
    so this stays accurate on reduced-precision floats.
    """
    n = days_since_2000(y, mo, d)
    j_star = n - lon / 360.0
    m = (357.5291 + 0.98560028 * j_star) % 360.0
    mr = m * _RAD
    c = (1.9148 * math.sin(mr) + 0.0200 * math.sin(2 * mr)
         + 0.0003 * math.sin(3 * mr))
    lam = (m + c + 180.0 + 102.9372) % 360.0
    lamr = lam * _RAD
    # transit time as a small fraction of a day either side of local noon
    transit_frac = -lon / 360.0 + 0.0053 * math.sin(mr) - 0.0069 * math.sin(2 * lamr)
    noon = (12.0 + transit_frac * 24.0) % 24.0
    delta = math.asin(math.sin(lamr) * math.sin(23.44 * _RAD))
    latr = lat * _RAD
    denom = math.cos(latr) * math.cos(delta)
    if denom == 0:
        return (None, None, noon)
    cos_w = (math.sin(-0.833 * _RAD) - math.sin(latr) * math.sin(delta)) / denom
    if cos_w > 1.0 or cos_w < -1.0:
        return (None, None, noon)                # polar night / day
    w = math.acos(cos_w) * _DEG / 360.0          # hour angle in days
    rise = (12.0 + (transit_frac - w) * 24.0) % 24.0
    sett = (12.0 + (transit_frac + w) * 24.0) % 24.0
    return (rise, sett, noon)


def moon_phase(y, mo, d, hour=12.0):
    """Return (age_days, illuminated_fraction, name, waxing)."""
    age = (days_since_2000(y, mo, d) + (hour - 12.0) / 24.0 - _NEW_MOON_OFF) % _SYNODIC
    if age < 0:
        age += _SYNODIC
    illum = (1.0 - math.cos(2 * math.pi * age / _SYNODIC)) / 2.0
    waxing = age < _SYNODIC / 2.0
    return (age, illum, _phase_name(age), waxing)


def _phase_name(age):
    if age < 1.0 or age > _SYNODIC - 1.0:
        return "New"
    if age < 6.4:
        return "Waxing Crescent"
    if age < 8.4:
        return "First Quarter"
    if age < 13.8:
        return "Waxing Gibbous"
    if age < 15.8:
        return "Full"
    if age < 21.1:
        return "Waning Gibbous"
    if age < 23.1:
        return "Last Quarter"
    return "Waning Crescent"


def is_spring_tide(age_days):
    """Spring tides occur near new and full moon."""
    return (age_days < 2.0 or abs(age_days - _SYNODIC / 2.0) < 2.0
            or age_days > _SYNODIC - 2.0)
