import sqlite3
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DB_PATH: str = os.getenv("DB_PATH", "db/scomp.sqlite")
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_db_path() -> str:
    return _DB_PATH


def init_db(db_path: Optional[str] = None) -> None:
    path = db_path or _DB_PATH
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(_SCHEMA_PATH.read_text())
        conn.commit()
    logger.info("Database initialised at %s", path)


@contextmanager
def get_conn(db_path: Optional[str] = None):
    path = db_path or _DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Leads ─────────────────────────────────────────────────────────────────────

def insert_lead(company: str, email: str, **kwargs) -> Optional[int]:
    fields = {"company": company, "email": email, **kwargs}
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    values = list(fields.values())
    with get_conn() as conn:
        try:
            cur = conn.execute(
                f"INSERT INTO leads ({cols}) VALUES ({placeholders})", values
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None  # duplicate email


def get_leads_by_status(status: str) -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM leads WHERE status = ?", (status,)
        ).fetchall()


def update_lead_status(lead_id: int, status: str, failure_reason: Optional[str] = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE leads
               SET status = ?, failure_reason = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (status, failure_reason, lead_id),
        )


def count_leads_by_status() -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM leads GROUP BY status"
        ).fetchall()
    return {row["status"]: row["n"] for row in rows}


# ── Emails ────────────────────────────────────────────────────────────────────

def insert_email(lead_id: int, subject: str, body: str, word_count: int) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO emails (lead_id, subject, body, word_count)
               VALUES (?, ?, ?, ?)""",
            (lead_id, subject, body, word_count),
        )
        return cur.lastrowid


def get_drafted_emails() -> list:
    with get_conn() as conn:
        return conn.execute(
            """SELECT e.*, l.email as recipient_email, l.company, l.contact_name
               FROM emails e
               JOIN leads l ON l.id = e.lead_id
               WHERE e.status = 'drafted'
               ORDER BY e.generated_at ASC"""
        ).fetchall()


def mark_email_sent(email_id: int, lead_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE emails SET status='sent', sent_at=datetime('now') WHERE id=?",
            (email_id,),
        )
        conn.execute(
            "UPDATE leads SET status='sent', updated_at=datetime('now') WHERE id=?",
            (lead_id,),
        )


def count_sent_today() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM emails WHERE status='sent' AND date(sent_at)=date('now')"
        ).fetchone()
    return row[0]


def get_recent_email_logs(limit: int = 15) -> list:
    with get_conn() as conn:
        return conn.execute(
            """SELECT e.sent_at, e.status, l.email as recipient_email, l.company,
                      e.subject, l.failure_reason
               FROM emails e
               JOIN leads l ON l.id = e.lead_id
               ORDER BY e.generated_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()


# ── Runs ──────────────────────────────────────────────────────────────────────

def start_run() -> int:
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO runs DEFAULT VALUES")
        return cur.lastrowid


def finish_run(run_id: int, leads_attempted: int, emails_sent: int,
               emails_skipped: int, emails_flagged: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE runs
               SET finished_at=datetime('now'), leads_attempted=?,
                   emails_sent=?, emails_skipped=?, emails_flagged=?
               WHERE id=?""",
            (leads_attempted, emails_sent, emails_skipped, emails_flagged, run_id),
        )


def get_today_run_summary() -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM runs
               WHERE date(started_at)=date('now')
               ORDER BY started_at DESC LIMIT 1"""
        ).fetchone()


# ── Skipped Sites ─────────────────────────────────────────────────────────────

def add_skipped_site(domain: str, reason: str) -> None:
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO skipped_sites (domain, reason) VALUES (?, ?)",
                (domain, reason),
            )
        except sqlite3.IntegrityError:
            pass  # already skipped


def is_site_skipped(domain: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM skipped_sites WHERE domain=?", (domain,)
        ).fetchone()
    return row is not None


def get_skipped_sites() -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM skipped_sites ORDER BY skipped_at DESC"
        ).fetchall()
