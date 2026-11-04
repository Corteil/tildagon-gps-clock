# GPS Clock

A retro multi-face watch for the [EMF Tildagon badge](https://tildagon.badge.emfcamp.org/),
driven by the GPS Hexpansion and designed for the round display.

## Faces

Cycle with **Left / Right** (it remembers the last clock face you used):

1. **Analog** — classic round dial with hour/minute/second hands and a date window.
2. **Digital** — retro Indiglo-style 7-segment LCD: `HH:MM`, seconds, day + date.
3. **Sun/Moon** — sunrise & sunset (local), day length, and the moon phase drawn
   with the correct illuminated fraction.
4. **Tide** — a **graphical tide wave** showing the water level, rising/falling and
   time to the next high/low. Offline by default; in the UK it shows **real water
   levels** from the Environment Agency when WiFi is available.
5. **Sky** — the satellites in view. With the 2026 front board's compass the
   map turns to match the real sky (tilt-compensated; North in red, a yellow
   mark for the way you point, and the heading), with a bubble level while the
   badge lies flat. Without a compass it stays North-up.
6. **Setup** — status (time source, WiFi, GPS, offset, location) and editable
   settings (timezone region, tide source, north trim, status ring on/off).

A thin **status ring** round the edge of the display shows the time source:
**green** = GPS, **cyan** = NTP, **grey** = badge RTC only. Press **A** (Up)
on any face to hide or show it (or use **Ring** in Setup).

Stand the badge on any edge and every face turns so its bottom stays level
(the LED clock turns with it).

The 12 edge **LEDs** show the time as a clock: **purple = hour**, **blue = minute**,
**white = second**.

## How it works

* **Time:** taken from GPS (`$..RMC`, via the Hexpansion firmware's `sentences`
  property) when there's a fix; otherwise from **NTP** when WiFi is up; otherwise
  the badge RTC. Whichever is available disciplines the RTC so the clock keeps
  running. Local time comes from the selected **timezone region** with automatic
  **UK/EU and US daylight-saving**.
* **Location:** taken from the GPS fix and cached, so sun/moon/tide keep working
  offline at your last known position.
* **Tide (offline):** a classic tide-clock (12 h 25 m cycle) you calibrate once by
  pressing **OK at high tide**; spring/neap is hinted from the moon phase.
* **Tide (online, UK):** when WiFi is up, the nearest Environment Agency tide gauge
  is queried for live water level + trend, which also auto-calibrates the offline
  clock. Uses the EA real-time data API (Beta) — Open Government Licence; no key.
  *"this uses Environment Agency tide gauge data from the real-time data API (Beta)"*
* **Settings** persist across power-off.

## Controls

* **Left / Right** — change face.
* **F / Cancel** — exit.
* **A / Up** — hide or show the status ring.
* **OK / Confirm** — on the **Tide** face, "set high tide now" (calibrate); on the
  **Setup** tile, enter/leave edit mode; on the **Sky** face, calibrate the
  compass (tumble the badge slowly in every direction for 20 s, away from
  computers). GPS Info shares the calibration and the north trim.
* **In the Setup menu:** **Up/Down** pick a setting, **Left/Right** change its value,
  **OK** done.

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
  + fs cp orient.mpy :/apps/gps_clock/orient.mpy ^
  + fs cp tildagon.toml :/apps/gps_clock/tildagon.toml ^
  + fs cp metadata.json :/apps/gps_clock/metadata.json
```

Rebuild from source: `pip install mpy-cross` then
`python -m mpy_cross app.py && python -m mpy_cross astro.py && python -m mpy_cross orient.py`.

## Credits

* [emfcamp/badge-2024-software](https://github.com/emfcamp/badge-2024-software) — Tildagon OS.
* [Environment Agency flood-monitoring API](https://environment.data.gov.uk/flood-monitoring/doc/tidegauge) — UK tide-gauge data (Open Government Licence).
* Sunrise/sunset via the standard NOAA solar equations; moon phase via a synodic-month calculation.

## License

MIT
