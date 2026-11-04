"""
Badge orientation for the round display.

  * Auto-level: when the badge stands on an edge, `rotation` turns the face so
    its bottom stays level (apply with ctx.rotate(rotation)).
  * Level bubble: `level` is the bubble's offset (-1..1, screen x/y) while the
    badge lies near flat.
  * Compass: `heading` is the tilt-compensated direction the top of the face
    points to (flat) or the direction you face looking at the screen (upright),
    in degrees from magnetic north plus the north trim. Needs the 2026 front board (QMC6309); on a
    2024 board `has_compass` is False and `heading` stays None.

Axes measured on a Tildagon (BMI270 + QMC6309, 2026 front board): z points out
of the screen, the screen's "up" is the IMU's +x, and the firmware already maps
the compass onto the accelerometer's axes. The badge has a point at the top and
bottom, so standing on one of its lower edges tilts the screen 30 degrees, and
flat edges left and right.

The compass calibration and the north trim (a fixed correction in degrees, for
what calibration cannot remove) are shared settings, so GPS Info and GPS Clock
use the same ones.

License: MIT
"""
import math

try:
    import imu
except ImportError:          # simulator / desktop
    imu = None

UP = 0.0                     # the screen's up, as an angle in the IMU's x/y plane
UPRIGHT_ON = 50              # degrees from flat: start levelling the face
UPRIGHT_OFF = 35             # ... and stop again (hysteresis)
CAL_KEY = "compass_cal"      # [ox, oy, oz, sx, sy, sz], shared between apps (sphere fit: s = 1)
CAL_MS = 20000               # calibration: tumble the badge for this long
CAL_MIN_SPAN = 0.3           # each axis must swing at least this far (field ~0.5)
TRIM_KEY = "compass_trim"    # degrees added to the heading, shared between apps
TRIM_MAX = 30


def _wrap(a):
    """Angle in radians to -pi..pi."""
    return (a + math.pi) % (2 * math.pi) - math.pi


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(v):
    n = math.sqrt(_dot(v, v))
    return None if n < 1e-6 else (v[0] / n, v[1] / n, v[2] / n)


def _solve(m, b):
    """Solve the n x n system m x = b (Gaussian elimination, partial pivoting)."""
    n = len(b)
    for i in range(n):
        p = max(range(i, n), key=lambda r: abs(m[r][i]))
        m[i], m[p] = m[p], m[i]
        b[i], b[p] = b[p], b[i]
        if abs(m[i][i]) < 1e-12:
            return None
        for r in range(i + 1, n):
            f = m[r][i] / m[i][i]
            for c in range(i, n):
                m[r][c] -= f * m[i][c]
            b[r] -= f * b[i]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (b[i] - sum(m[i][c] * x[c] for c in range(i + 1, n))) / m[i][i]
    return x


def _load_cal():
    try:
        import settings
        c = settings.get(CAL_KEY)
        if c and len(c) == 6:
            return [float(x) for x in c]
    except Exception:
        pass
    return None


def _save(key, value):
    try:
        import settings
        settings.set(key, value)
        settings.save()
    except Exception:
        pass


def _load_trim():
    try:
        import settings
        return max(-TRIM_MAX, min(TRIM_MAX, int(settings.get(TRIM_KEY) or 0)))
    except Exception:
        return 0


class Orientation:

    def __init__(self):
        self.acc = (0.0, 0.0, 9.81)
        self.tilt = 0.0              # degrees from lying flat, screen up
        self.upright = False
        self.rotation = 0.0          # radians, for ctx.rotate()
        self.level = (0.0, 0.0)      # bubble offset, screen coordinates
        self.has_compass = None      # None until known
        self.heading = None          # degrees, magnetic
        self.cal = _load_cal()
        self.trim = _load_trim()     # degrees
        self._zero_reads = 0
        self._cal_ms = None          # time left while calibrating
        self._cal_lo = None
        self._cal_hi = None
        self.cal_result = None       # "ok" / "failed" after a calibration

    @property
    def calibrated(self):
        return self.cal is not None

    @property
    def compass_ready(self):
        return bool(self.has_compass) and self.calibrated and self.heading is not None

    def set_trim(self, degrees):
        self.trim = max(-TRIM_MAX, min(TRIM_MAX, int(degrees)))
        _save(TRIM_KEY, self.trim)

    # --- calibration ------------------------------------------------------

    @property
    def calibrating(self):
        return self._cal_ms is not None

    @property
    def cal_progress(self):
        return 0.0 if self._cal_ms is None else 1.0 - self._cal_ms / CAL_MS

    def start_calibration(self):
        self._cal_ms = CAL_MS
        self._cal_lo = None
        self._cal_hi = None
        # Least-squares sphere fit |m|^2 = 2 c.m + k, accumulated as normal
        # equations so no samples need storing: unknowns (cx, cy, cz, k)
        self._cal_ata = [[0.0] * 4 for _ in range(4)]
        self._cal_atb = [0.0] * 4
        self.cal_result = None

    def cancel_calibration(self):
        self._cal_ms = None

    def _finish_calibration(self):
        self._cal_ms = None
        lo, hi = self._cal_lo, self._cal_hi
        # Each axis must have swung far enough, or the fit is meaningless
        fit = None
        if lo is not None and min(hi[i] - lo[i] for i in range(3)) >= CAL_MIN_SPAN:
            fit = _solve(self._cal_ata, self._cal_atb)
        if fit is None:
            self.cal_result = "failed"
            return
        self.cal = [fit[0], fit[1], fit[2], 1.0, 1.0, 1.0]
        _save(CAL_KEY, self.cal)
        self.cal_result = "ok"

    # --- update -----------------------------------------------------------

    def update(self, delta):
        if imu is None:
            return
        try:
            a = imu.acc_read()
        except Exception:
            return
        k = min(1.0, delta / 120.0)                 # low-pass, ~120 ms
        s = self.acc
        self.acc = (s[0] + (a[0] - s[0]) * k, s[1] + (a[1] - s[1]) * k, s[2] + (a[2] - s[2]) * k)
        ax, ay, az = self.acc
        g = math.sqrt(ax * ax + ay * ay + az * az)
        if g < 2:                                   # free fall or no data
            return

        self.tilt = math.degrees(math.acos(max(-1.0, min(1.0, az / g))))
        if self.tilt > UPRIGHT_ON:
            self.upright = True
        elif self.tilt < UPRIGHT_OFF:
            self.upright = False

        # Rotate the face so its bottom points down: standing on an edge the
        # "up" direction in the IMU plane is atan2(ay, ax); the face is upright
        # when that equals UP.
        target = _wrap(UP - math.atan2(ay, ax)) if self.upright else 0.0
        self.rotation = _wrap(self.rotation + _wrap(target - self.rotation) * min(1.0, delta / 180.0))

        # Level bubble: the raised side, in screen coordinates (x right, y down)
        u = (ax * math.cos(UP) + ay * math.sin(UP)) / g
        r = (ax * math.sin(UP) - ay * math.cos(UP)) / g
        self.level = (r, -u)

        self._update_compass(delta, (ax / g, ay / g, az / g))

    def _update_compass(self, delta, up):
        if self.has_compass is False:
            return
        try:
            m = imu.mag_read()
        except Exception:
            self.has_compass = False
            return
        # Without a compass the firmware never updates its (0, 0, 0) reading
        if m[0] == 0 and m[1] == 0 and m[2] == 0:
            self._zero_reads += 1
            if self._zero_reads > 20 and not self.has_compass:
                self.has_compass = False
            return
        self.has_compass = True

        if self._cal_ms is not None:
            if self._cal_lo is None:
                self._cal_lo, self._cal_hi = list(m), list(m)
            for i in range(3):
                self._cal_lo[i] = min(self._cal_lo[i], m[i])
                self._cal_hi[i] = max(self._cal_hi[i], m[i])
            row = (2 * m[0], 2 * m[1], 2 * m[2], 1.0)
            b = m[0] * m[0] + m[1] * m[1] + m[2] * m[2]
            for i in range(4):
                for j in range(4):
                    self._cal_ata[i][j] += row[i] * row[j]
                self._cal_atb[i] += row[i] * b
            self._cal_ms -= delta
            if self._cal_ms <= 0:
                self._finish_calibration()
            return

        if self.cal is None:
            return
        c = self.cal
        m = ((m[0] - c[0]) * c[3], (m[1] - c[1]) * c[4], (m[2] - c[2]) * c[5])
        east = _norm(_cross(m, up))
        if east is None:
            return
        north = _cross(up, east)
        # Direction to report: the face's top (as displayed, after levelling)
        # plus "into the screen"; whichever lies flatter dominates, so this is
        # the top of the map when flat and the way you face when upright.
        t = UP - self.rotation
        v = (math.cos(t), math.sin(t), -1.0)
        h = math.atan2(_dot(v, east), _dot(v, north)) + math.radians(self.trim)
        if self.heading is None:
            self.heading = math.degrees(h) % 360
        else:
            cur = math.radians(self.heading)
            cur = cur + _wrap(h - cur) * min(1.0, delta / 250.0)
            self.heading = math.degrees(cur) % 360
