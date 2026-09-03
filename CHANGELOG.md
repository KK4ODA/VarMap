# Changelog

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
