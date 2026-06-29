"""
Dispatcher: drip-sends drafted emails via Outlook SMTP.
Random 4–12 minute gap between sends. Hard daily cap enforced.
Atomic DB update per send. Halts on SMTP auth failure.
"""

import asyncio
import logging
import random
import smtplib
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

from config import settings
from db import database

logger = logging.getLogger(__name__)


def _build_message(from_addr: str, to_addr: str, subject: str, body: str) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["From"]    = from_addr
    msg["To"]      = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    return msg


def _send_one(smtp: smtplib.SMTP, from_addr: str, email_row: dict) -> bool:
    to_addr = email_row["recipient_email"]
    subject = email_row["subject"]
    body    = email_row["body"]
    try:
        msg = _build_message(from_addr, to_addr, subject, body)
        smtp.sendmail(from_addr, to_addr, msg.as_string())
        logger.info("Sent → %s (%s)", to_addr, email_row.get("company", ""))
        return True
    except smtplib.SMTPRecipientsRefused:
        logger.warning("Recipient refused: %s", to_addr)
        return False
    except smtplib.SMTPException as exc:
        logger.error("SMTP error sending to %s: %s", to_addr, exc)
        return False


def run_dispatch(progress_callback=None) -> dict:
    """
    Connects to Outlook SMTP and drip-sends all drafted emails.
    progress_callback(sent, cap) is called after each successful send.
    Returns stats: {sent, skipped, halted}
    """
    stats = {"sent": 0, "skipped": 0, "halted": False}

    if not settings.SMTP_ADDRESS or not settings.SMTP_PASSWORD:
        logger.error("SMTP credentials not configured — aborting dispatch.")
        stats["halted"] = True
        return stats

    already_sent_today = database.count_sent_today()
    remaining_cap = settings.DAILY_EMAIL_CAP - already_sent_today
    if remaining_cap <= 0:
        logger.info("Daily email cap already reached (%d). Dispatch skipped.", settings.DAILY_EMAIL_CAP)
        return stats

    drafted = database.get_drafted_emails()
    if not drafted:
        logger.info("No drafted emails to dispatch.")
        return stats

    logger.info("Connecting to %s:%d ...", settings.SMTP_HOST, settings.SMTP_PORT)
    try:
        smtp = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT)
        smtp.ehlo()
        smtp.starttls()
        smtp.login(settings.SMTP_ADDRESS, settings.SMTP_PASSWORD)
    except smtplib.SMTPAuthenticationError as exc:
        logger.error("SMTP authentication failed: %s", exc)
        stats["halted"] = True
        return stats
    except smtplib.SMTPException as exc:
        logger.error("SMTP connection error: %s", exc)
        stats["halted"] = True
        return stats

    logger.info("Dispatch starting. Cap remaining today: %d", remaining_cap)

    try:
        for email_row in drafted:
            if stats["sent"] >= remaining_cap:
                logger.info("Daily cap reached. Halting dispatcher.")
                break

            row = dict(email_row)
            success = _send_one(smtp, settings.SMTP_ADDRESS, row)

            if success:
                database.mark_email_sent(row["id"], row["lead_id"])
                stats["sent"] += 1
                if progress_callback:
                    progress_callback(already_sent_today + stats["sent"], settings.DAILY_EMAIL_CAP)

                # Random gap: 4–12 minutes
                if stats["sent"] < remaining_cap and drafted.index(email_row) < len(drafted) - 1:
                    gap = random.randint(
                        settings.DISPATCH_GAP_MIN * 60,
                        settings.DISPATCH_GAP_MAX * 60,
                    )
                    logger.debug("Waiting %ds before next send...", gap)
                    time.sleep(gap)
            else:
                database.update_lead_status(
                    row["lead_id"], "flagged", "send failed — recipient refused"
                )
                stats["skipped"] += 1

    finally:
        try:
            smtp.quit()
        except Exception:
            pass

    logger.info("Dispatch complete: %s", stats)
    return stats
