# Changelog

## 0.4.0 (2026-09-06)

- Welfare check-ins from Emcomm BBS on the map. VarMap reads the `welfare_*.csv` that
  [Emcomm BBS](https://github.com/KK4ODA/emcomm-bbs) writes into VarAC's BBS folder and marks
  matching stations with a shoulder badge (green SAFE, red NEED ASSISTANCE, orange TRAFFIC), a
  chip in the list and a Welfare row in the station panel; a new Filters → Welfare check-in
  select narrows the map to a status, the status bar shows the board's totals, and Settings →
  Welfare lists everyone on the board including name-only check-ins. Entirely read-only and
  inert when Emcomm BBS is absent: no folder or file means no badges, a half-written file keeps
  the previous board, and a board older than a configurable age is flagged stale.
- `/api/welfare` and a `welfare` field on `/api/stations` for other tools (Emcomm BBS's
  "Stations heard" bulletin reads VarMap's station list the same way).

## 0.3.13 (2026-09-04)

- Fix the unit-bearing labels on the Position TX tab, which rendered as three stacked lines in 0.3.12.

## 0.3.12 (2026-09-04)

- Units setting now covers everything: imperial (miles, feet, mph) or metric (km, m, km/h). Movement thresholds and speeds on the Position TX tab are shown and edited in the chosen unit (stored in metric); accuracy shows in ft/mi or m/km. Changing the setting re-labels the open settings page immediately.

## 0.3.11 (2026-09-04)

- Removed the "Hide positions older than" filter; "Show stations heard within" is the only time filter and marker colour shows position age.

## 0.3.10 (2026-09-04)

- Keepalive removed. "Only if moved" (now in both timing modes, on by default) means a parked station is silent; switch it off and the plain schedule applies (interval / slow rate). Old max_interval settings are ignored.

## 0.3.9 (2026-09-04)

- Keepalive can be switched off (0 = never): a stationary station with "only if moved" then stays silent.
- Preferred bands: with the scanner hopping, sends start only in the first seconds of a stop on a preferred band, and the band is re-checked right before the final click (an abort is not counted as a failure). The frequency VarAC actually transmitted on is read back from VarAC.log into the TX log.
- Position TX no longer retries a failing VarAC hand-over every 2 minutes: backoff doubles per failure up to 30 min, and while the Windows session is locked no attempt is made at all.
- Every change of "why Position TX is holding" is now written to varmap.log, and the UI warns if the Position TX thread stops ticking.
- All cross-process window calls into VarAC use timeouts so a wedged VarAC UI can never freeze VarMap's TX thread.

## 0.3.8 (2026-09-04)

- Softer, muted red for stale dots and cluster badges so they no longer dominate the map.

## 0.3.7 (2026-09-03)

- Cluster badges now use the same age colours as the dots (yellow = freshest member is recent, red = stale). They were still blue/orange.

## 0.3.6 (2026-09-03)

- New position-age colours: fresh green, recent light yellow, stale half red with a light border, old open circle with a grey border.

## 0.3.5 (2026-09-03)

- Clarify in the Position TX tab that Preferred bands applies to every VarAC transmission, including APRS-to-VarAC relays.

## 0.3.4 (2026-09-03)

- Preferred bands for Position TX: automatic broadcasts hold until VarAC's scanner is on a chosen band; manual sends (position and relay) are queued for it, with a maximum wait and a Cancel button. Status shows VarAC's current band.

## 0.3.3 (2026-09-03)

- Manual sends (Send position once now, Relay to VarAC) need only 60 s since the previous VarAC transmission; the 5-minute floor now applies to the automatic scheduler only. APRS object transmissions no longer count toward VarAC spacing. Hourly and daily limits unchanged.

## 0.3.2 (2026-09-03)

- Fix: the start-up integrity check could rename the live database when a second VarMap process (for example `--status`) opened it while VarMap was running. The check now runs only in the main server after the port check proves it is the sole instance, is skipped when another process holds the database, and `--status` never checks integrity.
- Updater: proper batch self-delete (no more 'batch file cannot be found'), leftover installers are removed.

## 0.3.1 (2026-09-03)

- Fix: a relayed position broadcast ('APRS KN4PLO <GPS:..> via KK4ODA') was attributed to the relaying station; it now belongs to the named station (source 'relayed') and existing databases are repaired at start-up.
- Start-up integrity check of VarMap's own database; a damaged file is quarantined and rebuilt from VarAC's history.
- Clearer position-source labels in the station panel.

## 0.3.0 (2026-09-03)

- Built-in update check (GitHub releases, every 12 h) with an Update pill; one-click self-update for the installed Windows build with SHA-256 verification; releases now ship SHA256SUMS.txt.

## 0.2.0 (2026-09-03)

APRS integration via Graywolf, Position TX safeguards, UI polish.

- APRS via Graywolf: stations heard by Graywolf on the map (diamond markers), own position from Graywolf's GPS, APRS settings tab with connection test.
- APRS transmit through Graywolf (off, dry run by default): mirror own position broadcasts as an APRS beacon; relay consenting VarAC stations (APRS:Y token) as APRS objects with ambiguity, rate limits, consent expiry and retirement; relay an APRS station's position to VarAC from the station panel.
- Consent token APRS:Y / APRS:N parsed from VarAC broadcasts and shown as a chip; 'Allow others to relay my position to APRS' switch in Position TX.
- Position TX: 6/hour hard cap (default 2), no smart timing on VarAC calling frequencies, HF/VHF timing profiles, DCD guard, QSO hold-off, frequency logged per transmission, bands heard per station.

## 0.1.0 (2026-09-03)

First public release.

- Maps every position VarAC hears: advanced beacons and CQs (grid squares), broadcasts
  (`<GPS:…>`, `<LOC:…>` tags and bare grids), VMail `<GPS:…>` tags.
- Live map with age-coded markers, clustering options, legend, filters (heard-within,
  band, distance, SNR, source, tags), station panel with 7-day tracks, broadcasts tab.
- Own position from VarAC's GPS log, an NMEA GPS, ManualGPSData or MyLocator.
- Position TX: broadcasts your GPS position through VarAC's Broadcast window, fixed
  interval or smart timing (HF and VHF profiles), with anti-spam limits, DCD guard,
  QSO hold-off, dry run and a dialog rehearsal.
- Offline maps: tile cache plus region downloads (MBTiles).
- VarAC database is opened strictly read-only.
