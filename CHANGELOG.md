# Changelog

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
