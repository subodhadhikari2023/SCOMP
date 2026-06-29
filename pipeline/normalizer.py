"""
Normalizer: validates raw lead dicts, deduplicates against DB,
and promotes clean records to status='ready'.
"""

import logging
import re
from typing import Optional

from db import database

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


def _is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email.strip()))


def normalize_and_store(raw_leads: list[dict]) -> dict:
    """
    Takes a list of raw lead dicts (company, email, niche, contact_name,
    source_url, company_desc) and stores valid ones.

    Returns counts: {stored, skipped_duplicate, skipped_invalid, flagged_manual}
    """
    stats = {"stored": 0, "skipped_duplicate": 0, "skipped_invalid": 0, "flagged_manual": 0}

    for lead in raw_leads:
        email   = (lead.get("email") or "").strip().lower()
        company = (lead.get("company") or "").strip()

        # Hard requirements
        if not email or not company:
            stats["skipped_invalid"] += 1
            continue

        if not _is_valid_email(email):
            logger.debug("Invalid email format: %s", email)
            stats["skipped_invalid"] += 1
            continue

        status = "ready"
        failure_reason: Optional[str] = None

        # Flag records missing useful context so they can be reviewed
        if not lead.get("company_desc") and not lead.get("niche"):
            status = "manual"
            failure_reason = "missing company_desc and niche"

        lead_id = database.insert_lead(
            company=company,
            email=email,
            niche=lead.get("niche"),
            contact_name=lead.get("contact_name"),
            source_url=lead.get("source_url"),
            company_desc=lead.get("company_desc"),
            status=status,
            failure_reason=failure_reason,
        )

        if lead_id is None:
            stats["skipped_duplicate"] += 1
        elif status == "manual":
            stats["flagged_manual"] += 1
            logger.debug("Flagged manual: %s (%s)", company, email)
        else:
            stats["stored"] += 1
            logger.debug("Lead stored: %s (%s)", company, email)

    logger.info(
        "Normalizer: stored=%d dup=%d invalid=%d manual=%d",
        stats["stored"], stats["skipped_duplicate"],
        stats["skipped_invalid"], stats["flagged_manual"],
    )
    return stats
