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

FACES = ("analog", "digital", "sunmoon", "tide", "info")
DOW = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
MON = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
       "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

TIDE_PERIOD = 12 * 3600 + 25.2 * 60      # mean semidiurnal tide period (s)
EA_BASE = "https://environment.data.gov.uk/flood-monitoring"
EA_ATTRIB = "EA real-time data API (Beta)"

# Indiglo-ish palette (vivid blue-green backlight, dark LCD segments)
INDIGLO_BG = (0.10, 0.62, 0.54)
INDIGLO_ON = (0.00, 0.12, 0.12)
INDIGLO_OFF = (0.09, 0.52, 0.46)

# Edge-LED clock ring: hours purple, minutes blue, seconds white (values 0-255)
LED_HOUR = (95, 0, 135)
LED_MINUTE = (0, 25, 150)
LED_BOTH = (70, 10, 150)
LED_SECOND = (55, 55, 65)
LED_OFFSET = -1         # rotate ring so position 0 is 12 o'clock

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


_DIM = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _last_sunday(y, mo):
    """Day-of-month of the last Sunday in the given month."""
    dim = _DIM[mo - 1]
    if mo == 2 and (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)):
        dim = 29
    dow = astro.jdn(y, mo, dim) % 7          # Mon=0 .. Sun=6
    return dim - ((dow - 6) % 7)


def is_eu_dst(t):
    """EU/UK summer time: last Sun Mar 01:00 UTC .. last Sun Oct 01:00 UTC.
    t is a UTC time tuple (year, month, day, hour, ...)."""
    y, mo, d, h = t[0], t[1], t[2], t[3]
    if mo < 3 or mo > 10:
        return False
    if 3 < mo < 10:
        return True
    ls = _last_sunday(y, mo)
    if mo == 3:
        return d > ls or (d == ls and h >= 1)
    return d < ls or (d == ls and h < 1)


def _nth_sunday(y, mo, n):
    """Day-of-month of the nth Sunday of the month."""
    dow1 = astro.jdn(y, mo, 1) % 7               # Mon=0 .. Sun=6
    first = 1 + ((6 - dow1) % 7)
    return first + (n - 1) * 7


def is_us_dst(t):
    """US daylight time: 2nd Sun Mar .. 1st Sun Nov (date-based)."""
    y, mo, d = t[0], t[1], t[2]
    if mo < 3 or mo > 11:
        return False
    if 3 < mo < 11:
        return True
    if mo == 3:
        return d >= _nth_sunday(y, 3, 2)
    return d < _nth_sunday(y, 11, 1)


# Region presets: name -> (base UTC offset hours or None=auto-from-longitude, DST rule)
REGIONS = (
    ("UK / Eire", 0, "eu"),
    ("Auto (GPS)", None, "eu"),
    ("UTC", 0, None),
    ("C. Europe", 1, "eu"),
    ("E. Europe", 2, "eu"),
    ("US Eastern", -5, "us"),
    ("US Central", -6, "us"),
    ("US Mountain", -7, "us"),
    ("US Pacific", -8, "us"),
    ("India", 5.5, None),
    ("China", 8, None),
    ("Japan", 9, None),
    ("E. Australia", 10, None),
)
REGION_BY_NAME = {r[0]: (r[1], r[2]) for r in REGIONS}


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
        saved = _get("gpsclock_face", "analog")
        if saved not in FACES or saved == "info":   # never start on the Setup tile
            saved = "analog"
        self.face = FACES.index(saved)

        self.gps = None
        self._find_gps_module()

        # cached location
        self.lat = _get("gpsclock_lat")
        self.lon = _get("gpsclock_lon")
        self._last_sync = 0      # ticks since last RTC discipline
        self._time_src = None    # None / "gps" / "net"
        self._net_ms = 0         # 0 = never attempted (ticks_ms once attempted)
        self._dst_on = False
        self._tz_base = 0
        self._editing = False    # SETUP tile edit mode
        self._sel = 0            # selected menu item

        # tide online state
        self._tide = None        # dict: value, units, trend, ... or None
        self._tide_fetch_ms = -999999
        self._tide_station = _get("gpsclock_tide_station")

        # LEDs
        self._led_hm = None

        eventbus.on_async(RequestForegroundPushEvent, self._resume, self)
        eventbus.on_async(RequestForegroundPopEvent, self._pause, self)
        eventbus.on_async(HexpansionMountedEvent, self._mounted, self)
        try:
            from system.hexpansion.events import HexpansionUnmountedEvent
            eventbus.on_async(HexpansionUnmountedEvent, self._unmounted, self)
        except ImportError:
            from system.hexpansion.events import HexpansionRemovalEvent
            eventbus.on_async(HexpansionRemovalEvent, self._unmounted, self)
        eventbus.emit(PatternDisable())

    def _find_gps_module(self):
        self.gps = get_app_by_vid_pid_shim(GPS_VID, GPS_PID)

    # --- lifecycle --------------------------------------------------------

    async def _resume(self, _):
        eventbus.emit(PatternDisable())
        self._led_hm = None

    async def _pause(self, _):
        self._leds_off()
        eventbus.emit(PatternEnable())

    async def _mounted(self, _):
        if not self.gps:
            self._find_gps_module()

    async def _unmounted(self, e):
        if self.gps and getattr(e, "port", None) == self.gps.config.port:
            self.gps = None
            if self._time_src == "gps":
                self._time_src = None      # GPS gone -> let NTP take over

    # --- settings helpers -------------------------------------------------

    def _offset_seconds(self):
        base, rule = REGION_BY_NAME.get(_get("gpsclock_region", REGIONS[0][0]),
                                        (None, "eu"))
        if base is None:
            base = round(self.lon / 15.0) if self.lon is not None else 0
        dst = 0
        if rule == "eu" and is_eu_dst(self._now_utc()):
            dst = 1
        elif rule == "us" and is_us_dst(self._now_utc()):
            dst = 1
        self._dst_on = bool(dst)
        self._tz_base = base
        return int((base + dst) * 3600)

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
            self._time_src = "gps"
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
        b = self.button_states
        if b.get(BUTTON_TYPES["CANCEL"]):
            b.clear()
            if self._editing:
                self._editing = False            # leave the settings menu
            else:
                self._leds_off()
                self.minimise()
                return

        if self._editing:
            # Settings menu: Up/Down pick item, Left/Right change value, OK done
            n = len(self._menu_items())
            if b.get(BUTTON_TYPES["UP"]):
                self._sel = (self._sel - 1) % n; b.clear()
            if b.get(BUTTON_TYPES["DOWN"]):
                self._sel = (self._sel + 1) % n; b.clear()
            if b.get(BUTTON_TYPES["LEFT"]):
                self._menu_change(-1); b.clear()
            if b.get(BUTTON_TYPES["RIGHT"]):
                self._menu_change(1); b.clear()
            if b.get(BUTTON_TYPES["CONFIRM"]):
                self._editing = False; b.clear()
        else:
            # Browsing: Left/Right change face
            if b.get(BUTTON_TYPES["RIGHT"]):
                self.face = (self.face + 1) % len(FACES)
                self._save_face(); b.clear()
            if b.get(BUTTON_TYPES["LEFT"]):
                self.face = (self.face - 1) % len(FACES)
                self._save_face(); b.clear()
            if b.get(BUTTON_TYPES["CONFIRM"]):
                if FACES[self.face] == "info":
                    self._editing = True; self._sel = 0
                elif FACES[self.face] == "tide":
                    _set("gpsclock_tide_anchor", int(time.time()))
                b.clear()

        self._read_gps()
        self._update_leds()

    def _menu_items(self):
        """Editable settings: (key, label, options, current_value)."""
        return [
            ("gpsclock_region", "Zone", [r[0] for r in REGIONS],
             _get("gpsclock_region", REGIONS[0][0])),
            ("gpsclock_tide_provider", "Tide", ["auto", "offline"],
             _get("gpsclock_tide_provider", "auto")),
        ]

    def _menu_change(self, delta):
        key, label, opts, cur = self._menu_items()[self._sel]
        i = opts.index(cur) if cur in opts else 0
        _set(key, opts[(i + delta) % len(opts)])   # apply immediately and persist

    def _save_face(self):
        if FACES[self.face] != "info":      # never persist the Setup tile
            _set("gpsclock_face", FACES[self.face])

    def _update_leds(self):
        lt = self._now_local()
        hpos = lt[3] % 12                   # hour hand position (0-11)
        mpos = (lt[4] // 5) % 12            # minute hand position (5-min steps)
        spos = (lt[5] // 5) % 12            # second hand position (5-sec steps)
        if (hpos, mpos, spos) == self._led_hm:
            return
        self._led_hm = (hpos, mpos, spos)
        try:
            for p in range(12):
                if p == hpos and p == mpos:
                    c = LED_BOTH
                elif p == hpos:
                    c = LED_HOUR
                elif p == mpos:
                    c = LED_MINUTE
                elif p == spos:
                    c = LED_SECOND
                else:
                    c = (0, 0, 0)
                tildagonos.leds[((p + LED_OFFSET) % 12) + 1] = c
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
        self._led_hm = None

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
        elif f == "tide":
            self._draw_tide(ctx)
        else:
            self._draw_info(ctx)
        self._draw_gps_pip(ctx)

    def _draw_gps_pip(self, ctx):
        ctx.save()
        if self._time_src == "gps":
            col = (0.0, 1.0, 0.25)        # bright green = GPS time lock
        elif self._time_src == "net":
            col = (0.1, 0.75, 1.0)        # cyan = NTP network time
        else:
            col = (0.45, 0.45, 0.45)      # grey = RTC only
        ctx.rgba(col[0], col[1], col[2], 0.35)
        ctx.begin_path(); ctx.arc(0, -108, 9, 0, 2 * math.pi, False); ctx.fill()
        ctx.rgb(*col)
        ctx.begin_path(); ctx.arc(0, -108, 5.5, 0, 2 * math.pi, False); ctx.fill()
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
        if online and online.get("last_high"):
            anchor = online["last_high"]      # auto-calibrate from EA data

        # sky
        ctx.rgb(0.05, 0.09, 0.17)
        ctx.begin_path(); ctx.arc(0, 0, 118, 0, 2 * math.pi, False); ctx.fill()

        if anchor is None and online is None:
            ctx.rgb(0.9, 0.9, 0.7); ctx.font_size = 17
            ctx.move_to(0, -10).text("Press OK")
            ctx.move_to(0, 14).text("at high tide")
            ctx.restore(); return

        now = time.time()
        if anchor is None:
            anchor = now
        T = TIDE_PERIOD
        elapsed = (now - anchor) % T
        phase = elapsed / T
        y0, amp = 22, 44

        def hgt(dt):
            return math.cos(2 * math.pi * (elapsed + dt) / T)

        # tide curve as a wave, one full cycle across the screen
        pts = []
        x = -112
        while x <= 112:
            dt = (x / 112.0) * (T / 2.0)
            pts.append((x, y0 - amp * hgt(dt)))
            x += 4
        # water fill below the surface
        ctx.rgb(0.10, 0.40, 0.62)
        ctx.begin_path(); ctx.move_to(pts[0][0], pts[0][1])
        for px, py in pts[1:]:
            ctx.line_to(px, py)
        ctx.line_to(120, 120); ctx.line_to(-120, 120); ctx.close_path(); ctx.fill()
        # surface line
        ctx.rgb(0.62, 0.86, 1.0); ctx.line_width = 2
        ctx.begin_path(); ctx.move_to(pts[0][0], pts[0][1])
        for px, py in pts[1:]:
            ctx.line_to(px, py)
        ctx.stroke()
        # "now" marker
        ny = y0 - amp * hgt(0)
        ctx.rgba(1, 1, 1, 0.5); ctx.line_width = 1
        ctx.begin_path(); ctx.move_to(0, ny); ctx.line_to(0, 116); ctx.stroke()
        ctx.rgb(1.0, 0.85, 0.2)
        ctx.begin_path(); ctx.arc(0, ny, 5, 0, 2 * math.pi, False); ctx.fill()

        rising = phase > 0.5
        to_high = (1 - phase) % 1 * T
        to_low = (0.5 - phase) % 1 * T
        nxt = to_high if rising else to_low
        ctx.rgb(0.95, 1.0, 1.0); ctx.font_size = 20
        ctx.move_to(0, -92).text("Rising" if rising else "Falling")
        ctx.font_size = 14; ctx.rgb(0.75, 0.85, 0.95)
        ctx.move_to(0, -72).text("%s in %dh%02dm" % ("HIGH" if rising else "LOW",
                                                     int(nxt // 3600), int((nxt % 3600) // 60)))
        if online and online.get("value") is not None:
            ctx.font_size = 16; ctx.rgb(0.5, 1.0, 0.75)
            ctx.move_to(0, 96).text("%.2f%s" % (online["value"], online.get("units", "m")))
            ctx.font_size = 9; ctx.rgb(0.45, 0.55, 0.65)
            ctx.move_to(0, 110).text(EA_ATTRIB)
        ctx.restore()


    # --- setup / info tile ------------------------------------------------

    def _draw_info(self, ctx):
        ctx.save()
        ctx.text_align = ctx.CENTER; ctx.text_baseline = ctx.MIDDLE
        ctx.rgb(0.8, 0.85, 1.0); ctx.font_size = 18
        ctx.move_to(0, -96).text("SETUP")

        oh = self._offset_seconds() / 3600.0
        src = {"gps": "GPS", "net": "NTP"}.get(self._time_src, "RTC")
        srccol = {"gps": (0.2, 1.0, 0.4), "net": (0.3, 0.8, 1.0)}.get(
            self._time_src, (0.65, 0.65, 0.65))
        if self._dst_on:
            tz = "UTC%+g BST" % oh if oh == 1 else "UTC%+g DST" % oh
        else:
            tz = "UTC%+g" % oh
        loc = "%.2f,%.2f" % (self.lat, self.lon) if self.lat is not None else "no fix"

        wifi_up = self._wifi_connected()
        wifi = "up" if wifi_up else "off"
        wcol = (0.2, 1.0, 0.4) if wifi_up else (0.6, 0.6, 0.6)
        if self.gps is None:
            gstat, gcol = "none", (0.6, 0.6, 0.6)
        elif getattr(self.gps, "position", None):
            gstat, gcol = "fix", (0.2, 1.0, 0.4)
        else:
            gstat, gcol = "no fix", (1.0, 0.7, 0.0)

        # read-only status
        ctx.font_size = 14
        y = -78
        for label, val, col in (("Time", src, srccol),
                                ("WiFi", wifi, wcol),
                                ("GPS", gstat, gcol),
                                ("Offset", tz, (0.9, 0.9, 0.9)),
                                ("Loc", loc, (0.82, 0.82, 0.86))):
            ctx.text_align = ctx.LEFT
            ctx.rgb(0.5, 0.6, 0.8).move_to(-82, y).text(label)
            ctx.text_align = ctx.RIGHT
            ctx.rgb(*col).move_to(82, y).text(val)
            y += 18

        # editable settings (cursor while editing)
        items = self._menu_items()
        y = 16
        for idx, (key, label, opts, cur) in enumerate(items):
            sel = self._editing and idx == self._sel
            if sel:
                ctx.rgb(0.16, 0.26, 0.42)
                ctx.rectangle(-94, y - 13, 188, 26).fill()
            ctx.text_align = ctx.LEFT
            ctx.rgb(0.65, 0.8, 1.0).move_to(-78, y).text(label)
            ctx.text_align = ctx.RIGHT
            ctx.rgb(*((1.0, 0.95, 0.4) if sel else (0.92, 0.92, 0.92)))
            ctx.move_to(78, y).text(str(cur))
            if sel:
                ctx.text_align = ctx.LEFT; ctx.rgb(1.0, 0.95, 0.4)
                ctx.move_to(-92, y).text("<")
                ctx.text_align = ctx.RIGHT
                ctx.move_to(92, y).text(">")
            y += 28

        ctx.text_align = ctx.CENTER; ctx.font_size = 11; ctx.rgb(0.55, 0.6, 0.7)
        if self._editing:
            ctx.move_to(0, 94).text("Up/Dn pick   L/R change   OK done")
        else:
            ctx.move_to(0, 94).text("OK to edit")
        ctx.restore()

    # --- online tide (EA flood-monitoring, UK; best-effort) ---------------

    async def background_task(self):
        # The scheduler runs this for every app. Pulls NTP time (when there's no
        # GPS) and UK tide data.
        import asyncio
        await asyncio.sleep_ms(400)
        while True:
            try:
                await self._maybe_net_time()
            except Exception:
                pass
            try:
                await self._maybe_fetch_tide()
            except Exception:
                pass
            await asyncio.sleep(5)

    @staticmethod
    def _wifi_connected():
        # Cheap, non-blocking check so we never stall on DNS/connect when offline.
        try:
            import network
            return network.WLAN(network.STA_IF).isconnected()
        except Exception:
            return False

    async def _maybe_net_time(self):
        # Use NTP when GPS time isn't available and WiFi is already up. GPS, if
        # present, always wins (it keeps disciplining the RTC in _read_gps).
        if self._time_src == "gps":
            return
        if not self._wifi_connected():
            return
        # retry ~every 60 s until we have time, then re-sync hourly
        if self._net_ms != 0:
            period = 60 * 60 * 1000 if self._time_src == "net" else 60 * 1000
            if time.ticks_diff(time.ticks_ms(), self._net_ms) < period:
                return
        self._net_ms = time.ticks_ms()
        try:
            import ntptime
            import async_helpers
            await async_helpers.unblock(ntptime.settime)   # off the UI loop
            if self._time_src != "gps":
                self._time_src = "net"
        except Exception:
            try:
                import ntptime
                ntptime.settime()                          # brief, WiFi is up
                if self._time_src != "gps":
                    self._time_src = "net"
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
        if not self._wifi_connected():
            return
        self._tide_fetch_ms = time.ticks_ms()
        try:
            import async_helpers
            data = await async_helpers.unblock(self._fetch_tide_blocking)
        except Exception:
            data = None
        if data:
            self._tide = data

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
