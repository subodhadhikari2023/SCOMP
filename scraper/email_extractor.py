"""
Email extractor: mines contact emails from scraped HTML.
Checks mailto: links, crawls /contact /about /team subpages,
and regex-matches emails from raw text. Discards generic addresses.
"""

import asyncio
import logging
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from config import settings

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

_GENERIC_PREFIXES = {
    "info", "noreply", "no-reply", "support", "admin",
    "hello", "contact", "team", "hr", "jobs", "careers",
    "sales", "billing", "help", "webmaster", "postmaster",
}

_CRAWL_SUBPATHS = ["/contact", "/about", "/team", "/contact-us", "/about-us", "/hire"]


def _is_generic(email: str) -> bool:
    local = email.split("@")[0].lower()
    return local in _GENERIC_PREFIXES


def _extract_emails_from_html(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    found: set[str] = set()

    # mailto: anchors
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("mailto:"):
            addr = href[7:].split("?")[0].strip()
            if _EMAIL_RE.match(addr):
                found.add(addr.lower())

    # raw text regex scan
    text = soup.get_text(" ")
    for match in _EMAIL_RE.findall(text):
        found.add(match.lower())

    return [e for e in found if not _is_generic(e)]


async def _fetch_subpage(client: httpx.AsyncClient, url: str) -> Optional[str]:
    try:
        resp = await client.get(
            url,
            headers={"User-Agent": settings.USER_AGENT},
            timeout=settings.HTTP_TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.text
    except httpx.HTTPError:
        return None


async def extract_emails(base_url: str, page_html: str) -> list[str]:
    """
    Returns a deduplicated list of non-generic email addresses found
    on the given page and one level of contact/about/team subpages.
    """
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    all_emails: set[str] = set(_extract_emails_from_html(page_html))

    async with httpx.AsyncClient() as client:
        tasks = []
        for subpath in _CRAWL_SUBPATHS:
            sub_url = urljoin(origin, subpath)
            if sub_url != base_url:
                tasks.append(_fetch_subpage(client, sub_url))
        sub_htmls = await asyncio.gather(*tasks)

    for html in sub_htmls:
        if html:
            all_emails.update(_extract_emails_from_html(html))

    result = sorted(all_emails)
    if result:
        logger.debug("Emails found at %s: %s", base_url, result)
    return result
