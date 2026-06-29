"""
Dispatcher: drip-sends drafted emails via Outlook Web (Playwright).

Auth model:
  Run `python main.py --setup-sender` once to log into Outlook in a headed browser.
  Session state is saved to browser_profiles/outlook_sender/state.json.
  All subsequent dispatch runs restore that session silently (no password needed).
  If the session expires (weeks later), re-run --setup-sender.
"""

import asyncio
import json
import logging
import random
from pathlib import Path

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from config import settings
from db import database

logger = logging.getLogger(__name__)

_SESSION_DIR  = Path(settings.BROWSER_PROFILES_DIR) / "outlook_sender"
_SESSION_FILE = _SESSION_DIR / "state.json"
_INBOX_URL    = "https://outlook.live.com/mail/0/"


async def _is_logged_in(page: Page) -> bool:
    await page.goto(_INBOX_URL, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_selector('[aria-label="New mail"]', timeout=8000)
        return True
    except Exception:
        return False


async def _compose_and_send(page: Page, to: str, subject: str, body: str) -> bool:
    try:
        # Open compose window
        await page.click('[aria-label="New mail"]', timeout=10000)

        # To field — focus is here on compose open
        to_input = page.locator('input[aria-label="To"]').first
        await to_input.wait_for(state="visible", timeout=8000)
        await to_input.fill(to)
        await to_input.press("Tab")

        # Subject
        subj = page.locator('[aria-label="Add a subject"]').first
        await subj.wait_for(state="visible", timeout=8000)
        await subj.fill(subject)

        # Body (contenteditable div)
        body_el = page.locator('div[aria-label="Message body, press Alt+F10 to exit"]').first
        await body_el.wait_for(state="visible", timeout=8000)
        await body_el.click()
        await body_el.fill(body)

        # Send via keyboard shortcut — more reliable than clicking the button
        await page.keyboard.press("Control+Return")
        await page.wait_for_timeout(2000)
        return True

    except Exception as exc:
        logger.error("Compose/send failed for %s: %s", to, exc)
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return False


async def run_dispatch(progress_callback=None) -> dict:
    """
    Restores saved Outlook session and drip-sends all drafted emails.
    Returns stats: {sent, skipped, halted}
    """
    stats = {"sent": 0, "skipped": 0, "halted": False}

    if not settings.SMTP_ADDRESS:
        logger.error("SMTP_ADDRESS not set in .env — aborting dispatch.")
        stats["halted"] = True
        return stats

    if not _SESSION_FILE.exists():
        logger.error(
            "No Outlook session found. Run: python main.py --setup-sender"
        )
        stats["halted"] = True
        return stats

    already_sent_today = database.count_sent_today()
    remaining_cap = settings.DAILY_EMAIL_CAP - already_sent_today
    if remaining_cap <= 0:
        logger.info("Daily cap reached (%d). Dispatch skipped.", settings.DAILY_EMAIL_CAP)
        return stats

    drafted = database.get_drafted_emails()
    if not drafted:
        logger.info("No drafted emails to dispatch.")
        return stats

    async with async_playwright() as pw:
        browser_type = getattr(pw, settings.BROWSER_ENGINE, pw.firefox)
        browser: Browser = await browser_type.launch(headless=True)
        context: BrowserContext = await browser.new_context(
            storage_state=str(_SESSION_FILE)
        )
        page: Page = await context.new_page()

        if not await _is_logged_in(page):
            logger.error(
                "Outlook session has expired. Run: python main.py --setup-sender"
            )
            stats["halted"] = True
            await browser.close()
            return stats

        logger.info("Outlook session valid. Dispatch starting — cap remaining: %d", remaining_cap)

        for i, email_row in enumerate(drafted):
            if stats["sent"] >= remaining_cap:
                logger.info("Daily cap reached mid-run. Stopping.")
                break

            row = dict(email_row)
            success = await _compose_and_send(
                page,
                row["recipient_email"],
                row["subject"],
                row["body"],
            )

            if success:
                database.mark_email_sent(row["id"], row["lead_id"])
                stats["sent"] += 1
                if progress_callback:
                    progress_callback(already_sent_today + stats["sent"], settings.DAILY_EMAIL_CAP)
                logger.info("Sent → %s (%s)", row["recipient_email"], row.get("company", ""))

                if stats["sent"] < remaining_cap and i < len(drafted) - 1:
                    gap = random.randint(
                        settings.DISPATCH_GAP_MIN * 60,
                        settings.DISPATCH_GAP_MAX * 60,
                    )
                    logger.debug("Waiting %ds before next send...", gap)
                    await asyncio.sleep(gap)
            else:
                database.update_lead_status(row["lead_id"], "flagged", "web send failed")
                stats["skipped"] += 1

        # Persist refreshed session state
        try:
            await context.storage_state(path=str(_SESSION_FILE))
        except Exception:
            pass

        await browser.close()

    logger.info("Dispatch complete: %s", stats)
    return stats
