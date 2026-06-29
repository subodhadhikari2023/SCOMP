CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company         TEXT NOT NULL,
    niche           TEXT,
    contact_name    TEXT,
    email           TEXT UNIQUE NOT NULL,
    source_url      TEXT,
    company_desc    TEXT,
    status          TEXT DEFAULT 'discovered',
    failure_reason  TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS emails (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         INTEGER REFERENCES leads(id),
    subject         TEXT,
    body            TEXT,
    word_count      INTEGER,
    generated_at    TEXT DEFAULT (datetime('now')),
    sent_at         TEXT,
    status          TEXT DEFAULT 'drafted'
);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT DEFAULT (datetime('now')),
    finished_at     TEXT,
    leads_attempted INTEGER DEFAULT 0,
    emails_sent     INTEGER DEFAULT 0,
    emails_skipped  INTEGER DEFAULT 0,
    emails_flagged  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS skipped_sites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    domain      TEXT UNIQUE NOT NULL,
    reason      TEXT,
    skipped_at  TEXT DEFAULT (datetime('now'))
);

-- Stores every URL found during discovery so the scraper can resume
-- mid-run and avoid re-discovering already-processed domains next run.
-- Dedup during a run uses an in-memory set; this table is the durable record.
CREATE TABLE IF NOT EXISTS discovered_urls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    domain        TEXT NOT NULL,
    url           TEXT NOT NULL,
    url_type      TEXT,                          -- job_board | company
    run_id        INTEGER REFERENCES runs(id),
    discovered_at TEXT DEFAULT (datetime('now')),
    status        TEXT DEFAULT 'pending'         -- pending | scraped | failed | skipped
);

-- Sites that hit an auth wall and were skipped due to timeout.
-- Displayed and re-prompted at the start of every subsequent run
-- until the user either logs in or explicitly declines.
CREATE TABLE IF NOT EXISTS pending_auth_sites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    domain      TEXT UNIQUE NOT NULL,
    site_url    TEXT NOT NULL,
    site_name   TEXT,
    skipped_at  TEXT DEFAULT (datetime('now')),
    attempts    INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_leads_status       ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_email        ON leads(email);
CREATE INDEX IF NOT EXISTS idx_emails_lead_id     ON emails(lead_id);
CREATE INDEX IF NOT EXISTS idx_emails_status      ON emails(status);
CREATE INDEX IF NOT EXISTS idx_discovered_domain  ON discovered_urls(domain);
CREATE INDEX IF NOT EXISTS idx_discovered_run     ON discovered_urls(run_id);
CREATE INDEX IF NOT EXISTS idx_discovered_status  ON discovered_urls(status);
