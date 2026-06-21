# GPS Clock

A retro multi-face watch for the [EMF Tildagon badge](https://tildagon.badge.emfcamp.org/),
driven by the GPS Hexpansion and designed for the round display.

## Faces

Cycle with **Left / Right**:

1. **Analog** — classic round dial with hour/minute/second hands and a date window.
2. **Digital** — retro Indiglo-style 7-segment LCD: `HH:MM`, seconds, day + date.
3. **Sun/Moon** — sunrise & sunset (local), day length, and the moon phase drawn
   with the correct illuminated fraction.
4. **Tide** — a tide-clock dial showing rising/falling and time to the next high/low.
   Offline by default; in the UK it shows **real water levels** from the
   Environment Agency when WiFi is available.

## How it works

* **Time:** parsed from the GPS `$..RMC` sentence (via the Hexpansion firmware's
  `sentences` property) and used to discipline the badge RTC, so the clock keeps
  good time even without a fix. Local time = a manual **UTC offset + DST toggle**
  (defaulting to a rough estimate from GPS longitude).
* **Location:** taken from the GPS fix and cached, so sun/moon/tide keep working
  offline at your last known position.
* **Tide (offline):** a classic tide-clock (12 h 25 m cycle) you calibrate once by
  pressing **OK at high tide**; spring/neap is hinted from the moon phase.
* **Tide (online, UK):** when WiFi is up, the nearest Environment Agency tide gauge
  is queried for live water level + trend, which also auto-calibrates the offline
  clock. Uses the EA real-time data API (Beta) — Open Government Licence; no key.
  *"this uses Environment Agency tide gauge data from the real-time data API (Beta)"*

## Controls

* **Left / Right** — change face.
* **Up / Down** — adjust UTC offset.
* **OK / Confirm** — on the Tide face, "set high tide now" (calibrate); on clock
  faces, toggle DST.
* **F / Cancel** — exit.

## Requirements

* **Tildagon OS v2.0.0+**.
* **GPS Hexpansion firmware v3+** (the `sentences` property) for GPS time/location.
  Without it the clock runs on the badge RTC only.

## Install / build

Pre-built `.mpy` is committed; flash without `mpy-cross`. Find the port with
`mpremote devs`:

```powershell
mpremote connect COM13 fs mkdir :/apps/gps_clock ^
  + fs cp app.mpy   :/apps/gps_clock/app.mpy ^
  + fs cp astro.mpy :/apps/gps_clock/astro.mpy ^
  + fs cp tildagon.toml :/apps/gps_clock/tildagon.toml ^
  + fs cp metadata.json :/apps/gps_clock/metadata.json
```

Rebuild from source: `pip install mpy-cross` then
`python -m mpy_cross app.py && python -m mpy_cross astro.py`.

## Credits

* [emfcamp/badge-2024-software](https://github.com/emfcamp/badge-2024-software) — Tildagon OS.
* [Environment Agency flood-monitoring API](https://environment.data.gov.uk/flood-monitoring/doc/tidegauge) — UK tide-gauge data (Open Government Licence).
* Sunrise/sunset via the standard NOAA solar equations; moon phase via a synodic-month calculation.

## License

MIT
