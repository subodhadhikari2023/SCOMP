"""
Heavy Track scraper: Playwright headless fallback.
Browser engine is selected via BROWSER_ENGINE env var (firefox | chromium | webkit).
Used when Fast Track returns nothing or hits a JS-rendered page.
"""

import logging
import os
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, BrowserContext, Page

from config import settings

logger = logging.getLogger(__name__)

_SUPPORTED_ENGINES = {"firefox", "chromium", "webkit"}


def _launch_browser(playwright, headless: bool):
    engine = settings.BROWSER_ENGINE
    if engine not in _SUPPORTED_ENGINES:
        logger.warning("Unknown BROWSER_ENGINE=%r — falling back to firefox", engine)
        engine = "firefox"
    launcher = getattr(playwright, engine)
    return launcher.launch(headless=headless)


async def _get_context(playwright, profile_dir: Optional[str] = None) -> BrowserContext:
    browser = await _launch_browser(playwright, headless=True)
    state_file = Path(profile_dir, "state.json") if profile_dir else None
    if state_file and state_file.exists():
        ctx = await browser.new_context(storage_state=str(state_file))
        logger.debug("Loaded saved session from %s", profile_dir)
    else:
        ctx = await browser.new_context(user_agent=settings.USER_AGENT)
    return ctx


async def fetch_html_playwright(url: str, profile_dir: Optional[str] = None) -> Optional[str]:
    async with async_playwright() as pw:
        ctx = await _get_context(pw, profile_dir)
        page: Page = await ctx.new_page()
        try:
            resp = await page.goto(url, timeout=30_000, wait_until="domcontentloaded")
            if resp and resp.status in (401, 403):
                raise PermissionError(f"Auth wall at {url}")
            await page.wait_for_load_state("networkidle", timeout=15_000)
            return await page.content()
        except PermissionError:
            raise
        except Exception as exc:
            logger.warning("Playwright [%s] failed on %s: %s", settings.BROWSER_ENGINE, url, exc)
            return None
        finally:
            await ctx.close()


async def fetch_html_visible(url: str, profile_dir: str) -> Optional[str]:
    """Visible (non-headless) browser — used for manual auth flows."""
    async with async_playwright() as pw:
        browser = await _launch_browser(pw, headless=False)
        ctx = await browser.new_context(user_agent=settings.USER_AGENT)
        page: Page = await ctx.new_page()
        try:
            await page.goto(url, timeout=60_000)
            return await page.content()
        finally:
            os.makedirs(profile_dir, exist_ok=True)
            await ctx.storage_state(path=str(Path(profile_dir, "state.json")))
            await ctx.close()
            await browser.close()
