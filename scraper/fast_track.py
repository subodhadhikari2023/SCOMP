"""
Fast Track scraper: httpx async GET + BeautifulSoup parse.
Returns raw page HTML or None on failure.
Also attempts lightweight API endpoint discovery.
"""

import asyncio
import logging
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from config import settings

logger = logging.getLogger(__name__)

_API_PROBES = ["/_next/data/", "/api/jobs", "/api/careers", "/graphql"]


async def fetch_html(url: str, client: Optional[httpx.AsyncClient] = None) -> Optional[str]:
    owned = client is None
    if owned:
        client = httpx.AsyncClient(follow_redirects=True, timeout=settings.HTTP_TIMEOUT)
    try:
        resp = await client.get(url, headers={"User-Agent": settings.USER_AGENT})
        resp.raise_for_status()
        return resp.text
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            raise  # let router detect auth wall
        logger.debug("HTTP error fetching %s: %s", url, exc)
        return None
    except httpx.HTTPError as exc:
        logger.debug("Network error fetching %s: %s", url, exc)
        return None
    finally:
        if owned:
            await client.aclose()


async def probe_api_endpoints(base_url: str, client: httpx.AsyncClient) -> Optional[str]:
    from urllib.parse import urlparse
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    for path in _API_PROBES:
        probe_url = origin + path
        try:
            resp = await client.get(
                probe_url,
                headers={"User-Agent": settings.USER_AGENT},
                timeout=10,
            )
            if resp.status_code == 200 and "json" in resp.headers.get("content-type", ""):
                logger.debug("API endpoint found: %s", probe_url)
                return resp.text
        except httpx.HTTPError:
            pass
    return None


def extract_page_data(html: str, selectors: dict) -> dict:
    """Extract fields from HTML using CSS selectors from targets.yaml."""
    soup = BeautifulSoup(html, "html.parser")
    data = {}
    for field, selector in selectors.items():
        el = soup.select_one(selector)
        if el:
            data[field] = el.get_text(strip=True)
    return data
