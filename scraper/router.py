"""
Router: decides Fast Track → API probe → Heavy Track → skip.
Returns (html, track_used) or (None, 'skipped').
"""

import logging
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx

from config import settings
from db import database
from scraper import fast_track, heavy_track

logger = logging.getLogger(__name__)


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().lstrip("www.")


async def route(url: str, profile_dir: Optional[str] = None) -> Tuple[Optional[str], str]:
    """
    Returns (html, track) where track is one of:
      'fast', 'api', 'heavy', 'skipped', 'auth_required'
    Raises nothing — all errors are absorbed and logged.
    """
    domain = _domain(url)

    if database.is_site_skipped(domain):
        logger.debug("Skipping known-skipped domain: %s", domain)
        return None, "skipped"

    async with httpx.AsyncClient(follow_redirects=True, timeout=settings.HTTP_TIMEOUT) as client:
        # ── Attempt 1: Fast Track ──────────────────────────────────────────
        try:
            html = await fast_track.fetch_html(url, client)
            if html and len(html) > 500:
                logger.debug("Fast track success: %s", url)
                return html, "fast"
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                logger.info("Auth wall detected (fast track): %s", url)
                return None, "auth_required"

        # ── Attempt 2: API endpoint probe ──────────────────────────────────
        api_data = await fast_track.probe_api_endpoints(url, client)
        if api_data:
            return api_data, "api"

    # ── Attempt 3: Heavy Track (Playwright) ───────────────────────────────
    try:
        html = await heavy_track.fetch_html_playwright(url, profile_dir)
        if html and len(html) > 500:
            logger.debug("Heavy track success: %s", url)
            return html, "heavy"
    except PermissionError:
        logger.info("Auth wall detected (heavy track): %s", url)
        return None, "auth_required"

    # ── All tracks failed ─────────────────────────────────────────────────
    logger.info("All tracks failed for %s — marking manual", url)
    database.add_skipped_site(domain, "all tracks failed")
    return None, "skipped"
