"""
GPS Clock for the Tildagon badge.

A retro multi-face watch driven by the GPS Hexpansion:
  * Analog   - classic round dial with hands
  * Digital  - retro Indiglo-style 7-segment LCD
  * Sun/Moon - sunrise/sunset, day length and moon phase
  * Tide     - tide-clock dial; offline by default, real UK levels from the
               Environment Agency flood-monitoring API when WiFi is available.

GPS time (from $..RMC) disciplines the badge RTC, so the clock keeps running
without a fix; the last location is cached so sun/moon/tide still work offline.

Requires Tildagon OS v2.0.0+ and the GPS Hexpansion firmware v3+ (the `sentences`
property). Without them the clock falls back to the badge RTC only.

License: MIT
"""
import app
import math
import time

from events.input import Buttons, BUTTON_TYPES
from tildagonos import tildagonos
from system.eventbus import eventbus
from system.hexpansion.events import HexpansionMountedEvent
from system.patterndisplay.events import PatternDisable, PatternEnable
from system.scheduler.events import RequestForegroundPushEvent, RequestForegroundPopEvent

from . import astro

GPS_VID = 0x7CAB
GPS_PID = 0xBEAC

FACES = ("analog", "digital", "sunmoon", "tide")
DOW = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
MON = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
       "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

TIDE_PERIOD = 12 * 3600 + 25.2 * 60      # mean semidiurnal tide period (s)
EA_BASE = "https://environment.data.gov.uk/flood-monitoring"
EA_ATTRIB = "EA real-time data API (Beta)"

# Indiglo-ish palette
INDIGLO_BG = (0.10, 0.42, 0.40)
INDIGLO_ON = (0.02, 0.10, 0.10)
INDIGLO_OFF = (0.09, 0.36, 0.35)

# 7-segment lookup
SEG = {
    "0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg",
    "5": "acdfg", "6": "acdefg", "7": "abc", "8": "abcdefg", "9": "abcdfg",
    " ": "",
}


def get_app_by_vid_pid_shim(vid, pid):
    try:
        from system.hexpansion.util import get_app_by_vid_pid
        return get_app_by_vid_pid(vid, pid)
    except (ImportError, AttributeError):
        return None


def _get(key, default=None):
    try:
        import settings
        v = settings.get(key)
        return default if v is None else v
    except Exception:
        return default


def _set(key, value):
    try:
        import settings
        settings.set(key, value)
        settings.save()
    except Exception:
        pass


def _hhmm(h):
    """Fractional hours -> 'HH:MM' (or '--:--' for None)."""
    if h is None:
        return "--:--"
    h = h % 24
    return "%02d:%02d" % (int(h), int((h % 1) * 60))


def draw_7seg(ctx, x, y, w, h, ch, on_col, off_col, lw):
    """Draw one 7-segment character with top-left at (x, y)."""
    segs = SEG.get(ch, "")
    pts = {
        "a": ((x, y), (x + w, y)),
        "b": ((x + w, y), (x + w, y + h / 2)),
        "c": ((x + w, y + h / 2), (x + w, y + h)),
        "d": ((x, y + h), (x + w, y + h)),
        "e": ((x, y + h / 2), (x, y + h)),
        "f": ((x, y), (x, y + h / 2)),
        "g": ((x, y + h / 2), (x + w, y + h / 2)),
    }
    ctx.line_width = lw
    for name, (p0, p1) in pts.items():
        ctx.rgb(*(on_col if name in segs else off_col))
        ctx.begin_path()
        ctx.move_to(*p0).line_to(*p1)
        ctx.stroke()


def parse_ea_readings(items):
    """From EA readings [{dateTime, value}, ...] (any order) return
    (current_value, trend, last_high_epoch, last_low_epoch). trend is +1 rising,
    -1 falling, 0 flat/unknown. Epochs are seconds (MicroPython epoch)."""
    rows = []
    for it in items:
        dt = it.get("dateTime")
        val = it.get("value")
        if dt is None or val is None:
            continue
        try:
            ep = _iso_to_epoch(dt)
            rows.append((ep, float(val)))
        except (ValueError, TypeError):
            continue
    if not rows:
        return (None, 0, None, None)
    rows.sort()                                  # oldest -> newest
    current = rows[-1][1]
    trend = 0
    if len(rows) >= 2:
        d = rows[-1][1] - rows[-2][1]
        trend = 1 if d > 0.02 else (-1 if d < -0.02 else 0)
    # last local maximum / minimum in the series
    last_high = last_low = None
    for i in range(1, len(rows) - 1):
        if rows[i][1] >= rows[i - 1][1] and rows[i][1] > rows[i + 1][1]:
            last_high = rows[i][0]
        if rows[i][1] <= rows[i - 1][1] and rows[i][1] < rows[i + 1][1]:
            last_low = rows[i][0]
    return (current, trend, last_high, last_low)


def _iso_to_epoch(s):
    """'2026-06-21T17:00:00Z' -> MicroPython epoch seconds (UTC)."""
    y = int(s[0:4]); mo = int(s[5:7]); d = int(s[8:10])
    h = int(s[11:13]); mi = int(s[14:16]); se = int(s[17:19])
    return time.mktime((y, mo, d, h, mi, se, 0, 0))


class GPSClock(app.App):

    def __init__(self):
        self.button_states = Buttons(self)
        self.face = FACES.index(_get("gpsclock_face", "analog")) if _get("gpsclock_face") in FACES else 0

        self.gps = None
        self._find_gps_module()

        # cached location
        self.lat = _get("gpsclock_lat")
        self.lon = _get("gpsclock_lon")
        self._last_sync = 0      # ticks since last RTC discipline
        self._have_gps_time = False

        # tide online state
        self._tide = None        # dict: value, units, trend, ... or None
        self._tide_fetch_ms = -999999
        self._tide_station = _get("gpsclock_tide_station")

        # LEDs
        self._led_sec = -1

        eventbus.on_async(RequestForegroundPushEvent, self._resume, self)
        eventbus.on_async(RequestForegroundPopEvent, self._pause, self)
        eventbus.on_async(HexpansionMountedEvent, self._mounted, self)
        eventbus.emit(PatternDisable())

    def _find_gps_module(self):
        self.gps = get_app_by_vid_pid_shim(GPS_VID, GPS_PID)

    # --- lifecycle --------------------------------------------------------

    async def _resume(self, _):
        eventbus.emit(PatternDisable())
        self._led_sec = -1

    async def _pause(self, _):
        self._leds_off()
        eventbus.emit(PatternEnable())

    async def _mounted(self, _):
        if not self.gps:
            self._find_gps_module()

    # --- settings helpers -------------------------------------------------

    def _offset_seconds(self):
        off = _get("gpsclock_utc_offset")
        if off is None:
            off = round(self.lon / 15.0) if self.lon is not None else 0
        dst = 1 if _get("gpsclock_dst", False) else 0
        return int((off + dst) * 3600)

    def _now_utc(self):
        return time.localtime(time.time())        # RTC held in UTC

    def _now_local(self):
        return time.localtime(time.time() + self._offset_seconds())

    # --- GPS time / location ---------------------------------------------

    def _read_gps(self):
        if not self.gps:
            return
        pos = getattr(self.gps, "position", None)
        if pos:
            if self.lat is None or abs(pos[0] - self.lat) > 0.02 or abs(pos[1] - self.lon) > 0.02:
                self.lat, self.lon = pos[0], pos[1]
                _set("gpsclock_lat", self.lat)
                _set("gpsclock_lon", self.lon)
            else:
                self.lat, self.lon = pos[0], pos[1]

        sentences = getattr(self.gps, "sentences", None)
        if not sentences:
            return
        t = self._parse_rmc_time(sentences)
        if t:
            self._have_gps_time = True
            self._discipline_rtc(t)

    def _parse_rmc_time(self, sentences):
        for line in reversed(sentences):
            p = line.split(",")
            if len(p[0]) >= 6 and p[0][3:6] == "RMC" and len(p) > 9 and p[2] == "A":
                tm, dt = p[1], p[9]
                if len(tm) >= 6 and len(dt) == 6:
                    try:
                        return (2000 + int(dt[4:6]), int(dt[2:4]), int(dt[0:2]),
                                int(tm[0:2]), int(tm[2:4]), int(tm[4:6]))
                    except ValueError:
                        return None
        return None

    def _discipline_rtc(self, t):
        y, mo, d, h, mi, s = t
        try:
            gps_epoch = time.mktime((y, mo, d, h, mi, s, 0, 0))
        except (ValueError, OverflowError):
            return
        if abs(gps_epoch - time.time()) > 2:
            try:
                from machine import RTC
                RTC().datetime((y, mo, d, 0, h, mi, s, 0))
            except Exception:
                pass

    # --- update -----------------------------------------------------------

    def update(self, delta):
        if self.button_states.get(BUTTON_TYPES["CANCEL"]):
            self.button_states.clear()
            self._leds_off()
            self.minimise()
            return
        if self.button_states.get(BUTTON_TYPES["RIGHT"]):
            self.face = (self.face + 1) % len(FACES)
            _set("gpsclock_face", FACES[self.face])
            self.button_states.clear()
        if self.button_states.get(BUTTON_TYPES["LEFT"]):
            self.face = (self.face - 1) % len(FACES)
            _set("gpsclock_face", FACES[self.face])
            self.button_states.clear()
        if self.button_states.get(BUTTON_TYPES["UP"]):
            self._adjust_offset(1)
            self.button_states.clear()
        if self.button_states.get(BUTTON_TYPES["DOWN"]):
            self._adjust_offset(-1)
            self.button_states.clear()
        if self.button_states.get(BUTTON_TYPES["CONFIRM"]):
            self._confirm_action()
            self.button_states.clear()

        self._read_gps()
        self._update_leds()

    def _adjust_offset(self, d):
        off = _get("gpsclock_utc_offset")
        if off is None:
            off = round(self.lon / 15.0) if self.lon is not None else 0
        _set("gpsclock_utc_offset", max(-12, min(14, off + d)))

    def _confirm_action(self):
        if FACES[self.face] == "tide":
            # Calibrate the offline tide-clock: "it is high tide now"
            _set("gpsclock_tide_anchor", int(time.time()))
        else:
            _set("gpsclock_dst", not _get("gpsclock_dst", False))

    def _update_leds(self):
        lt = self._now_local()
        sec = lt[5]
        led = int(sec / 5) % 12
        if led == self._led_sec:
            return
        self._led_sec = led
        try:
            for i in range(12):
                tildagonos.leds[i + 1] = (40, 40, 50) if i == led else (0, 0, 0)
            tildagonos.leds.write()
        except Exception:
            pass

    def _leds_off(self):
        try:
            for i in range(12):
                tildagonos.leds[i + 1] = (0, 0, 0)
            tildagonos.leds.write()
        except Exception:
            pass
        self._led_sec = -1

    # --- draw -------------------------------------------------------------

    def draw(self, ctx):
        ctx.rgb(0.04, 0.05, 0.08).rectangle(-120, -120, 240, 240).fill()
        f = FACES[self.face]
        if f == "analog":
            self._draw_analog(ctx)
        elif f == "digital":
            self._draw_digital(ctx)
        elif f == "sunmoon":
            self._draw_sunmoon(ctx)
        else:
            self._draw_tide(ctx)
        self._draw_gps_pip(ctx)

    def _draw_gps_pip(self, ctx):
        ctx.save()
        ok = self._have_gps_time
        ctx.rgb(*(0.0, 0.8, 0.3) if ok else (0.5, 0.5, 0.5))
        ctx.begin_path()
        ctx.arc(0, 112, 3, 0, 2 * math.pi, False)
        ctx.fill()
        ctx.restore()

    # --- analog face ------------------------------------------------------

    def _draw_analog(self, ctx):
        lt = self._now_local()
        h, m, s = lt[3], lt[4], lt[5]
        ctx.save()
        # dial
        ctx.rgb(0.93, 0.90, 0.82)
        ctx.begin_path(); ctx.arc(0, 0, 112, 0, 2 * math.pi, False); ctx.fill()
        ctx.rgb(0.12, 0.12, 0.12)
        ctx.line_width = 3
        ctx.begin_path(); ctx.arc(0, 0, 112, 0, 2 * math.pi, False); ctx.stroke()
        # ticks
        for i in range(60):
            a = i / 60 * 2 * math.pi
            r0 = 104 if i % 5 else 96
            ctx.line_width = 3 if i % 5 == 0 else 1
            ctx.begin_path()
            ctx.move_to(math.sin(a) * r0, -math.cos(a) * r0)
            ctx.line_to(math.sin(a) * 110, -math.cos(a) * 110)
            ctx.stroke()
        # date window
        ctx.text_align = ctx.CENTER; ctx.text_baseline = ctx.MIDDLE
        ctx.font_size = 16
        ctx.rgb(0.2, 0.2, 0.2).move_to(55, 0).text("%02d" % lt[2])
        # hands
        self._hand(ctx, (h % 12 + m / 60.0) / 12.0, 58, 5, (0.1, 0.1, 0.1))
        self._hand(ctx, (m + s / 60.0) / 60.0, 86, 4, (0.1, 0.1, 0.1))
        self._hand(ctx, s / 60.0, 92, 1.5, (0.85, 0.1, 0.1))
        ctx.rgb(0.1, 0.1, 0.1)
        ctx.begin_path(); ctx.arc(0, 0, 5, 0, 2 * math.pi, False); ctx.fill()
        ctx.restore()

    def _hand(self, ctx, frac, length, width, col):
        a = frac * 2 * math.pi
        ctx.rgb(*col)
        ctx.line_width = width
        ctx.begin_path()
        ctx.move_to(0, 0)
        ctx.line_to(math.sin(a) * length, -math.cos(a) * length)
        ctx.stroke()

    # --- digital face -----------------------------------------------------

    def _draw_digital(self, ctx):
        lt = self._now_local()
        ctx.save()
        # backlit panel
        ctx.rgb(*INDIGLO_BG)
        ctx.begin_path(); ctx.arc(0, 0, 116, 0, 2 * math.pi, False); ctx.fill()
        # HH:MM big
        w, h, gap, lw = 28, 46, 10, 6
        s = "%02d%02d" % (lt[3], lt[4])
        total = 4 * w + 3 * gap + 14
        x = -total / 2
        y = -h / 2 - 6
        for i, ch in enumerate(s):
            draw_7seg(ctx, x, y, w, h, ch, INDIGLO_ON, INDIGLO_OFF, lw)
            x += w + gap
            if i == 1:
                ctx.rgb(*INDIGLO_ON)
                for dy in (y + h * 0.32, y + h * 0.68):
                    ctx.begin_path(); ctx.arc(x - gap / 2, dy, 2.6, 0, 2 * math.pi, False); ctx.fill()
                x += 14
        # seconds + day/date
        ctx.text_align = ctx.CENTER; ctx.text_baseline = ctx.MIDDLE
        ctx.rgb(*INDIGLO_ON)
        ctx.font_size = 20
        ctx.move_to(0, y + h + 18).text(":%02d" % lt[5])
        ctx.font_size = 18
        ctx.move_to(0, y - 18).text("%s %02d %s" % (DOW[lt[6]], lt[2], MON[lt[1] - 1]))
        ctx.restore()

    # --- sun / moon face --------------------------------------------------

    def _draw_sunmoon(self, ctx):
        ctx.save()
        ctx.text_align = ctx.CENTER; ctx.text_baseline = ctx.MIDDLE
        if self.lat is None:
            ctx.rgb(0.9, 0.9, 0.9); ctx.font_size = 20
            ctx.move_to(0, 0).text("No location yet")
            ctx.restore(); return
        ut = self._now_utc()
        rise, sett, noon = astro.sun_times(self.lat, self.lon, ut[0], ut[1], ut[2])
        off_h = self._offset_seconds() / 3600.0

        def loc(hh):
            return None if hh is None else (hh + off_h) % 24

        ctx.rgb(1.0, 0.8, 0.1); ctx.font_size = 18
        ctx.move_to(0, -88).text("Sun")
        ctx.rgb(0.95, 0.95, 0.95); ctx.font_size = 22
        ctx.move_to(-44, -55).text(_hhmm(loc(rise)))
        ctx.move_to(44, -55).text(_hhmm(loc(sett)))
        ctx.font_size = 13; ctx.rgb(0.7, 0.7, 0.7)
        ctx.move_to(-44, -38).text("rise")
        ctx.move_to(44, -38).text("set")
        if rise is not None and sett is not None:
            dl = (sett - rise) % 24
            ctx.font_size = 14; ctx.rgb(0.8, 0.8, 0.6)
            ctx.move_to(0, -20).text("day %dh%02dm" % (int(dl), int((dl % 1) * 60)))

        age, illum, name, waxing = astro.moon_phase(ut[0], ut[1], ut[2])
        self._draw_moon(ctx, 0, 40, 30, illum, waxing)
        ctx.text_align = ctx.CENTER; ctx.text_baseline = ctx.MIDDLE
        ctx.rgb(0.85, 0.85, 0.95); ctx.font_size = 14
        ctx.move_to(0, 84).text("%s  %d%%" % (name, int(illum * 100)))
        ctx.restore()

    def _draw_moon(self, ctx, cx, cy, r, illum, waxing):
        ctx.save()
        # dark (unlit) disc
        ctx.rgb(0.10, 0.10, 0.16)
        ctx.begin_path(); ctx.arc(cx, cy, r, 0, 2 * math.pi, False); ctx.fill()
        # lit region: limb on the lit side + terminator ellipse (x-radius r*(1-2*illum))
        side = 1 if waxing else -1
        tw = (1.0 - 2.0 * illum)
        n = 30
        ctx.rgb(0.93, 0.93, 0.85)
        ctx.begin_path()
        for i in range(n + 1):
            a = -math.pi / 2 + (i / n) * math.pi
            x = cx + side * r * math.cos(a)
            y = cy + r * math.sin(a)
            ctx.move_to(x, y) if i == 0 else ctx.line_to(x, y)
        for i in range(n + 1):
            a = math.pi / 2 - (i / n) * math.pi
            x = cx + side * r * tw * math.cos(a)
            y = cy + r * math.sin(a)
            ctx.line_to(x, y)
        ctx.close_path(); ctx.fill()
        # rim
        ctx.rgb(0.4, 0.4, 0.5); ctx.line_width = 1
        ctx.begin_path(); ctx.arc(cx, cy, r, 0, 2 * math.pi, False); ctx.stroke()
        ctx.restore()

    # --- tide face --------------------------------------------------------

    def _draw_tide(self, ctx):
        ctx.save()
        ctx.text_align = ctx.CENTER; ctx.text_baseline = ctx.MIDDLE
        anchor = _get("gpsclock_tide_anchor")
        online = self._tide

        # dial
        ctx.rgb(0.10, 0.20, 0.30)
        ctx.begin_path(); ctx.arc(0, 0, 96, 0, 2 * math.pi, False); ctx.fill()
        ctx.rgb(0.5, 0.7, 0.9); ctx.line_width = 2
        ctx.begin_path(); ctx.arc(0, 0, 96, 0, 2 * math.pi, False); ctx.stroke()
        ctx.rgb(0.8, 0.9, 1.0); ctx.font_size = 14
        ctx.move_to(0, -80).text("HIGH")
        ctx.move_to(0, 80).text("LOW")

        if anchor is None and online is None:
            ctx.rgb(0.9, 0.9, 0.7); ctx.font_size = 15
            ctx.move_to(0, -20).text("Press OK at")
            ctx.move_to(0, 2).text("high tide to")
            ctx.move_to(0, 24).text("calibrate")
            ctx.restore(); return

        now = time.time()
        # auto-calibrate from online last-high if available
        if online and online.get("last_high"):
            anchor = online["last_high"]
        if anchor is None:
            anchor = now            # not calibrated; assume high now

        phase = ((now - anchor) % TIDE_PERIOD) / TIDE_PERIOD   # 0=high .. .5=low
        ang = phase * 2 * math.pi
        # pointer
        ctx.rgb(0.95, 0.85, 0.2); ctx.line_width = 4
        ctx.begin_path(); ctx.move_to(0, 0)
        ctx.line_to(math.sin(ang) * 80, -math.cos(ang) * 80); ctx.stroke()
        ctx.rgb(0.95, 0.85, 0.2)
        ctx.begin_path(); ctx.arc(0, 0, 5, 0, 2 * math.pi, False); ctx.fill()

        rising = phase > 0.5
        to_high = (1 - phase) % 1 * TIDE_PERIOD
        to_low = (0.5 - phase) % 1 * TIDE_PERIOD
        ctx.font_size = 16; ctx.rgb(0.9, 0.95, 1.0)
        ctx.move_to(0, 30).text("Rising" if rising else "Falling")
        nxt = to_high if rising else to_low
        ctx.font_size = 14; ctx.rgb(0.75, 0.85, 0.95)
        ctx.move_to(0, 50).text("%s in %dh%02dm" % ("HIGH" if rising else "LOW",
                                                     int(nxt // 3600), int((nxt % 3600) // 60)))
        if online and online.get("value") is not None:
            ctx.font_size = 15; ctx.rgb(0.6, 1.0, 0.8)
            ctx.move_to(0, -40).text("%.2f%s" % (online["value"], online.get("units", "m")))
            ctx.font_size = 10; ctx.rgb(0.5, 0.6, 0.7)
            ctx.move_to(0, 92).text(EA_ATTRIB)
        ctx.restore()


    # --- online tide (EA flood-monitoring, UK; best-effort) ---------------

    async def background_task(self):
        import asyncio
        while True:
            await asyncio.sleep(5)
            try:
                await self._maybe_fetch_tide()
            except Exception:
                pass

    async def _maybe_fetch_tide(self):
        if _get("gpsclock_tide_provider", "auto") == "offline" or self.lat is None:
            return
        if self._tide and time.ticks_diff(time.ticks_ms(), self._tide_fetch_ms) < 15 * 60 * 1000:
            return
        # EA coverage is UK only
        if not (49.0 < self.lat < 61.0 and -11.0 < self.lon < 2.5):
            return
        try:
            import async_helpers
            data = await async_helpers.unblock(self._fetch_tide_blocking)
        except Exception:
            data = self._fetch_tide_blocking()
        if data:
            self._tide = data
        self._tide_fetch_ms = time.ticks_ms()

    def _fetch_tide_blocking(self):
        try:
            import requests
        except ImportError:
            try:
                import urequests as requests
            except ImportError:
                return None
        try:
            measure = self._tide_station
            if not measure:
                url = ("%s/id/stations?type=TideGauge&lat=%.4f&long=%.4f&dist=30&_limit=5"
                       % (EA_BASE, self.lat, self.lon))
                r = requests.get(url); j = r.json(); r.close()
                measure = self._pick_measure(j.get("items", []))
                if not measure:
                    return None
                self._tide_station = measure
                _set("gpsclock_tide_station", measure)
            url = "%s/id/measures/%s/readings?_sorted&_limit=24" % (EA_BASE, measure)
            r = requests.get(url); j = r.json(); r.close()
            cur, trend, hi, lo = parse_ea_readings(j.get("items", []))
            if cur is None:
                return None
            units = "mAOD" if measure.endswith("mAOD") else "m"
            return {"value": cur, "trend": trend, "last_high": hi, "last_low": lo, "units": units}
        except Exception:
            return None

    @staticmethod
    def _pick_measure(stations):
        for s in stations:
            ms = s.get("measures")
            if isinstance(ms, dict):
                ms = [ms]
            for m in (ms or []):
                mid = m.get("@id", "").split("/")[-1] if isinstance(m, dict) else str(m)
                if "tidal_level" in mid:
                    return mid
        return None


__app_export__ = GPSClock # pylint: disable=invalid-name
