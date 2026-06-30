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


async def _wait_new_mail_button(page: Page, timeout_ms: int = 20000) -> bool:
    """Waits for the inbox New mail button to be clickable."""
    try:
        btn = page.locator('[aria-label="New mail"]').first
        await btn.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


async def _compose_and_send(
    page: Page, context: BrowserContext, to: str, subject: str, body: str
) -> bool:
    """
    Composes and sends one email via Outlook Web.
    Handles both same-page compose pane and new-tab compose window,
    since Outlook opens compose in a new tab when in headless mode.
    """
    compose_page = page  # fallback; reassigned if a new tab opens
    try:
        # Ensure inbox is in a clean state
        if not await _wait_new_mail_button(page, timeout_ms=20000):
            logger.warning("New mail button not found — navigating back to inbox")
            await page.goto(_INBOX_URL, wait_until="domcontentloaded", timeout=30000)
            if not await _wait_new_mail_button(page, timeout_ms=20000):
                raise RuntimeError("New mail button still missing after reload")

        # Track existing tabs so we can detect a new one opening
        pages_before = {id(p) for p in context.pages}
        await page.click('[aria-label="New mail"]', timeout=15000)

        # Wait up to 5s for Outlook to open compose (same page or new tab)
        for _ in range(25):
            await asyncio.sleep(0.2)
            new_tabs = [p for p in context.pages if id(p) not in pages_before]
            if new_tabs:
                compose_page = new_tabs[-1]
                logger.debug("Compose opened in new tab: %s", compose_page.url)
                break

        # Wait for compose page to finish loading
        try:
            await compose_page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass

        # Give Outlook's React app time to mount the To field
        await asyncio.sleep(2.0)

        # To field — full-window compose uses contenteditable divs, not <input> elements
        to_input = None
        to_selectors = [
            # Full-window compose (new tab) — contenteditable / role=textbox
            'div[aria-label="To"]',
            'div[aria-label="To"] [role="textbox"]',
            'div[role="textbox"][aria-label*="To"]',
            # Some Outlook versions use a well/token input inside a labelled container
            '[data-testid="RecipientTo"] [role="textbox"]',
            '[data-testid="RecipientTo"] input',
            # Inline compose pane (same page) — classic input elements
            'input[aria-label="To"]',
            'input[aria-label="Add recipients"]',
            'div[aria-label="To"] input',
            # Broader fallbacks
            '[aria-label*="recipient" i]',
            'input[type="text"][aria-autocomplete]',
        ]
        for sel in to_selectors:
            loc = compose_page.locator(sel).first
            try:
                await loc.wait_for(state="visible", timeout=4000)
                to_input = loc
                logger.debug("To field matched selector: %s", sel)
                break
            except Exception:
                continue

        if to_input is None:
            # Dump page HTML for diagnosis (truncated)
            try:
                html_snippet = await compose_page.evaluate(
                    "document.documentElement.outerHTML.slice(0, 3000)"
                )
                logger.error("To field not found. Page HTML (first 3000 chars):\n%s", html_snippet)
            except Exception as dump_exc:
                logger.error("To field not found and HTML dump failed: %s", dump_exc)
            raise RuntimeError("Could not locate To field in compose pane")

        await to_input.click()
        await to_input.fill(to)
        await to_input.press("Tab")
        await asyncio.sleep(0.5)  # let autocomplete dismiss

        # Subject — selector differs between inline pane and full-window compose
        subj = None
        for subj_sel in (
            '[aria-label="Add a subject"]',
            'input[aria-label="Subject"]',
            '[aria-label="Subject"]',
            '[placeholder*="subject" i]',
            '[aria-placeholder*="subject" i]',
            'input[type="text"][aria-label*="subject" i]',
        ):
            loc = compose_page.locator(subj_sel).first
            try:
                await loc.wait_for(state="visible", timeout=4000)
                subj = loc
                logger.debug("Subject field matched selector: %s", subj_sel)
                break
            except Exception:
                continue

        if subj is None:
            try:
                html_snippet = await compose_page.evaluate(
                    "document.documentElement.outerHTML.slice(0, 3000)"
                )
                logger.error("Subject field not found. Page HTML:\n%s", html_snippet)
            except Exception:
                pass
            raise RuntimeError("Could not locate Subject field in compose pane")

        await subj.click()
        await subj.fill(subject)

        # Body (contenteditable div)
        body_sel = None
        for b_sel in (
            'div[aria-label="Message body, press Alt+F10 to exit"]',
            'div[role="textbox"][aria-label*="body" i]',
            '[aria-label*="Message body" i]',
            'div[contenteditable="true"][aria-multiline="true"]',
        ):
            loc = compose_page.locator(b_sel).first
            try:
                await loc.wait_for(state="visible", timeout=4000)
                body_sel = loc
                logger.debug("Body field matched selector: %s", b_sel)
                break
            except Exception:
                continue

        if body_sel is None:
            raise RuntimeError("Could not locate message body field in compose pane")

        body_el = body_sel
        await body_el.click()
        # execCommand preserves line breaks in Outlook's rich-text editor.
        # fill() sets raw textContent which collapses \n into spaces.
        await body_el.evaluate(
            "(el, text) => {"
            "  el.focus();"
            "  document.execCommand('selectAll', false, null);"
            "  document.execCommand('insertText', false, text);"
            "}",
            body,
        )

        # Send via keyboard shortcut
        await compose_page.keyboard.press("Control+Enter")

        # Wait for compose to close — tab closes if it was a popup, pane hides otherwise
        try:
            if compose_page is not page:
                await compose_page.wait_for_event("close", timeout=8000)
            else:
                await compose_page.locator(
                    'div[aria-label="Message body, press Alt+F10 to exit"]'
                ).first.wait_for(state="hidden", timeout=8000)
        except Exception:
            pass

        await page.wait_for_timeout(1500)
        return True

    except Exception as exc:
        logger.error("Compose/send failed for %s: %s", to, exc)
        try:
            if compose_page is not page:
                await compose_page.close()
            else:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(500)
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
        if not await _wait_inbox_any_page(context, timeout_ms=30000):
            logger.error("Outlook session expired — run: python main.py --setup-sender")
            stats["halted"] = True
            await browser.close()
            return stats

        logger.info("Outlook session valid. Dispatch starting — cap remaining: %d", remaining_cap)

        for i, email_row in enumerate(drafted):
            if stats["sent"] >= remaining_cap:
                logger.info("Daily cap reached mid-run. Stopping.")
                break

            row     = dict(email_row)
            send_to = settings.TEST_RECIPIENT_EMAIL or row["recipient_email"]
            if settings.TEST_RECIPIENT_EMAIL:
                logger.info(
                    "TEST MODE: redirecting %s → %s",
                    row["recipient_email"], send_to,
                )
            success = await _compose_and_send(
                page,
                context,
                send_to,
                row["subject"],
                row["body"],
            )

            if success:
                database.mark_email_sent(row["id"], row["lead_id"])
                stats["sent"] += 1
                if progress_callback:
                    progress_callback(
                        already_sent_today + stats["sent"],
                        settings.DAILY_EMAIL_CAP,
                        row["recipient_email"],
                        row.get("company", ""),
                    )
                logger.info("Sent → %s (%s)", row["recipient_email"], row.get("company", ""))

                if stats["sent"] < remaining_cap and i < len(drafted) - 1:
                    gap = random.randint(
                        settings.DISPATCH_GAP_MIN * 60,
                        settings.DISPATCH_GAP_MAX * 60,
                    )
                    logger.debug("Waiting %ds before next send...", gap)
                    await asyncio.sleep(gap)
                    # Navigate back to inbox after the gap — keeps page state clean
                    await page.goto(_INBOX_URL, wait_until="domcontentloaded", timeout=30000)
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
