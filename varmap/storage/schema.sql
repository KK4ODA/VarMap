-- VarMap's own database.  VarAC's database is a read-only upstream; this is
-- the system of record for everything we derive.  Observations are append-only;
-- `station` is a materialised view over `station_position` + `station_heard`.

CREATE TABLE IF NOT EXISTS station (
    callsign            TEXT PRIMARY KEY,
    base_callsign       TEXT NOT NULL,
    is_own              INTEGER NOT NULL DEFAULT 0,

    lat                 REAL,
    lon                 REAL,
    grid                TEXT,
    accuracy_m          REAL,
    position_source     TEXT,
    position_time       TEXT,           -- when the station transmitted it (receiver clock)
    position_received   TEXT,           -- when WE ingested it
    position_ref        TEXT,
    position_suspect    INTEGER NOT NULL DEFAULT 0,

    first_heard         TEXT NOT NULL,
    last_heard          TEXT NOT NULL,
    heard_count         INTEGER NOT NULL DEFAULT 0,
    position_count      INTEGER NOT NULL DEFAULT 0,

    last_snr_db         INTEGER,
    last_frequency_hz   INTEGER,
    last_band           TEXT,
    last_bandwidth      TEXT,
    last_frame_kind     TEXT,
    last_text           TEXT,

    is_away             INTEGER NOT NULL DEFAULT 0,
    is_emcomm           INTEGER NOT NULL DEFAULT 0,
    is_email_gateway    INTEGER NOT NULL DEFAULT 0,
    is_bbs              INTEGER NOT NULL DEFAULT 0,
    is_ai_gateway       INTEGER NOT NULL DEFAULT 0,
    has_diploma         INTEGER NOT NULL DEFAULT 0,
    last_cq_tag         TEXT,

    op_name             TEXT,
    qth                 TEXT,
    notes               TEXT,
    is_favorite         INTEGER NOT NULL DEFAULT 0,
    is_hidden           INTEGER NOT NULL DEFAULT 0,

    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS station_last_heard_idx    ON station(last_heard DESC);
CREATE INDEX IF NOT EXISTS station_position_time_idx ON station(position_time DESC);
CREATE INDEX IF NOT EXISTS station_updated_idx       ON station(updated_at DESC);
CREATE INDEX IF NOT EXISTS station_base_idx          ON station(base_callsign);

CREATE TABLE IF NOT EXISTS station_position (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    callsign        TEXT NOT NULL,
    heard_at        TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    lat             REAL NOT NULL,
    lon             REAL NOT NULL,
    grid            TEXT,
    accuracy_m      REAL,
    source          TEXT NOT NULL,
    source_ref      TEXT NOT NULL UNIQUE,
    snr_db          INTEGER,
    frequency_hz    INTEGER,
    band            TEXT,
    is_away         INTEGER NOT NULL DEFAULT 0,
    suspect         INTEGER NOT NULL DEFAULT 0,
    raw_json        TEXT
);
CREATE INDEX IF NOT EXISTS sp_callsign_time_idx ON station_position(callsign, heard_at DESC);
CREATE INDEX IF NOT EXISTS sp_heard_at_idx      ON station_position(heard_at DESC);

CREATE TABLE IF NOT EXISTS station_heard (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    callsign        TEXT NOT NULL,
    heard_at        TEXT NOT NULL,
    source_ref      TEXT NOT NULL UNIQUE,
    frame_kind      TEXT NOT NULL,          -- beacon | cq | broadcast | vmail
    had_position    INTEGER NOT NULL,
    snr_db          INTEGER,
    band            TEXT,
    frequency_hz    INTEGER,
    text            TEXT
);
CREATE INDEX IF NOT EXISTS sh_callsign_time_idx ON station_heard(callsign, heard_at DESC);
CREATE INDEX IF NOT EXISTS sh_heard_at_idx      ON station_heard(heard_at DESC);

CREATE TABLE IF NOT EXISTS own_position (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    fix_time    TEXT NOT NULL,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    grid        TEXT,
    source      TEXT NOT NULL,
    speed_kmh   REAL,
    course_deg  REAL,
    altitude_m  REAL,
    accuracy_m  REAL
);
CREATE INDEX IF NOT EXISTS own_position_time_idx ON own_position(recorded_at DESC);

CREATE TABLE IF NOT EXISTS beacon_tx (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_at TEXT NOT NULL,
    sent_at      TEXT,
    lat          REAL, lon REAL, grid TEXT,
    trigger      TEXT NOT NULL,
    method       TEXT NOT NULL,
    message      TEXT,
    dry_run      INTEGER NOT NULL DEFAULT 0,
    ok           INTEGER,
    error        TEXT,
    frequency_hz INTEGER                  -- VarAC's frequency at hand-over (from VarAC.log)
);
CREATE INDEX IF NOT EXISTS beacon_tx_time_idx ON beacon_tx(requested_at DESC);

CREATE TABLE IF NOT EXISTS source_cursor (
    source_id    TEXT PRIMARY KEY,
    cursor       TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    last_ok_at   TEXT,
    last_error   TEXT,
    error_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
