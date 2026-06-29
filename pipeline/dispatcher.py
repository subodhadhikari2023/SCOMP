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


async def _wait_inbox_any_page(context: BrowserContext, timeout_ms: int = 8000) -> bool:
    """
    Polls every open tab in the context for the inbox button.
    Handles both same-tab redirects and logins that open in a new tab.
    """
    import time
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for p in context.pages:
            try:
                if await p.locator('[aria-label="New mail"]').count() > 0:
                    return True
            except Exception:
                pass
        await asyncio.sleep(1.5)
    return False


def _has_display() -> bool:
    """Returns True if a graphical display is available (Linux/Mac)."""
    import os
    return bool(
        os.getenv("DISPLAY")
        or os.getenv("WAYLAND_DISPLAY")
        or os.getenv("TERM_PROGRAM")   # macOS terminal apps
    )


async def ensure_session() -> None:
    """
    Opens a headed browser and waits for the user to log into Outlook.
    Polls all tabs — handles Outlook's sign-in redirect opening in a new tab.
    Session saves automatically once the inbox is detected on any tab.
    Called automatically by run_dispatch on first run; also via --setup-sender.
    """
    from rich.console import Console
    console = Console()

    _SESSION_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser_type = getattr(pw, settings.BROWSER_ENGINE, pw.firefox)
        browser = await browser_type.launch(headless=False)
        context = await browser.new_context()
        page    = await context.new_page()

        await page.goto(_INBOX_URL)

        console.print(
            f"\n  [cyan]Log into Outlook as[/cyan] [bold]{settings.SMTP_ADDRESS}[/bold]\n"
            "  [dim]A sign-in tab may open — complete login there. "
            "Session saves automatically once your inbox loads.[/dim]\n"
        )

        # Poll all tabs — works whether login redirects in same tab or opens a new one
        if not await _wait_inbox_any_page(context, timeout_ms=180_000):
            await browser.close()
            raise RuntimeError("Timed out waiting for Outlook inbox. Run --setup-sender to retry.")

        await context.storage_state(path=str(_SESSION_FILE))
        await browser.close()

    console.print("  [green]Session saved — continuing...[/green]\n")


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
        if not _has_display():
            logger.error(
                "No Outlook session and no display available. "
                "Run interactively first: python main.py --setup-sender"
            )
            stats["halted"] = True
            return stats
        logger.info("No session — opening browser for first-time login...")
        try:
            await ensure_session()
        except Exception as exc:
            logger.error("Session setup failed: %s", exc)
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

        await page.goto(_INBOX_URL, wait_until="domcontentloaded", timeout=30000)
        if not await _wait_inbox_any_page(context):
            logger.error("Outlook session expired — run: python main.py --setup-sender")
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
