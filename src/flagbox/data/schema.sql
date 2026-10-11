-- Flagbox snapshot schema (SPEC section 4.1). Rebuilt from the raw files by `flagbox ingest`.
-- Raw JSON is not stored. Supplier names that lack an incorporation marker are never stored.

CREATE TABLE source_files (
    file_id       INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    url           TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    downloaded_at TEXT NOT NULL,
    line_count    INTEGER NOT NULL
);

CREATE TABLE buyers (
    buyer_key TEXT PRIMARY KEY,
    raw_id    TEXT,
    name      TEXT NOT NULL,
    name_norm TEXT NOT NULL
);

CREATE TABLE processes (
    ocid           TEXT PRIMARY KEY,
    file_id        INTEGER NOT NULL REFERENCES source_files (file_id),
    line_no        INTEGER NOT NULL,
    buyer_key      TEXT NOT NULL REFERENCES buyers (buyer_key),
    title          TEXT,
    method         TEXT,
    method_details TEXT,
    category       TEXT,
    n_tenderers    INTEGER,
    period_start   TEXT,
    period_end     TEXT,
    status         TEXT,
    tender_value   REAL,
    currency       TEXT,
    portal_url     TEXT
);

CREATE TABLE awards (
    ocid     TEXT NOT NULL REFERENCES processes (ocid),
    award_id TEXT NOT NULL,
    date     TEXT,
    amount   REAL,
    currency TEXT,
    status   TEXT,
    PRIMARY KEY (ocid, award_id)
);

CREATE TABLE award_suppliers (
    ocid          TEXT NOT NULL,
    award_id      TEXT NOT NULL,
    supplier_key  TEXT NOT NULL,
    raw_id        TEXT,
    display_name  TEXT NOT NULL,
    name_withheld INTEGER NOT NULL CHECK (name_withheld IN (0, 1)),
    PRIMARY KEY (ocid, award_id, supplier_key),
    FOREIGN KEY (ocid, award_id) REFERENCES awards (ocid, award_id)
);

CREATE TABLE contracts (
    contract_id TEXT NOT NULL,
    ocid        TEXT NOT NULL REFERENCES processes (ocid),
    award_id    TEXT,
    date_signed TEXT,
    amount      REAL,
    PRIMARY KEY (ocid, contract_id)
);

CREATE INDEX idx_processes_buyer ON processes (buyer_key);
CREATE INDEX idx_award_suppliers_supplier ON award_suppliers (supplier_key);
CREATE INDEX idx_contracts_ocid ON contracts (ocid);
