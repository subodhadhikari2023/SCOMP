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

CREATE INDEX IF NOT EXISTS idx_leads_status   ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_email    ON leads(email);
CREATE INDEX IF NOT EXISTS idx_emails_lead_id ON emails(lead_id);
CREATE INDEX IF NOT EXISTS idx_emails_status  ON emails(status);
