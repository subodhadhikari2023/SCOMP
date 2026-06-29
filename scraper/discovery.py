"""
Search engine querying + URL classification.
Engine is selected via SEARCH_ENGINE env var (bing | duckduckgo).

Deduplication strategy:
  - Caller loads all previously seen domains from DB into a Python set (one DB read).
  - Every per-URL check hits that in-memory set — O(1), zero DB calls during the run.
  - New URLs are batch-written to DB in one INSERT at the end.
"""

import asyncio
import logging
from urllib.parse import urlparse

import httpx
import yaml
from bs4 import BeautifulSoup

from config import settings
from db import database

logger = logging.getLogger(__name__)

_ENGINES = {
    "bing": {
        "url":         "https://www.bing.com/search",
        "page_param":  "first",
        "count_param": "count",
        "selectors":   "li.b_algo h2 a, li.b_algo a.tilk",
    },
    "duckduckgo": {
        "url":         "https://html.duckduckgo.com/html/",
        "page_param":  None,
        "count_param": None,
        "selectors":   "a.result__a",
    },
}


def _load_config() -> dict:
    with open(settings.TARGETS_YAML) as f:
        return yaml.safe_load(f)


def _extract_domain(url: str) -> str:
    return urlparse(url).netloc.lower().lstrip("www.")


def _classify_url(url: str, classifier: dict) -> str:
    """Returns 'job_board', 'company', or 'discard'."""
    lower = url.lower()
    for sig in classifier.get("discard_signals", []):
        if sig in lower:
            return "discard"
    for sig in classifier.get("job_board_signals", []):
        if sig in lower:
            return "job_board"
    for sig in classifier.get("company_signals", []):
        if sig in lower:
            return "company"
    return "company"


async def _fetch_bing_page(client: httpx.AsyncClient, engine: dict, query: str, page: int) -> list:
    params = {"q": query, engine["page_param"]: page * 10 + 1, engine["count_param"]: 10}
    try:
        resp = await client.get(
            engine["url"],
            params=params,
            headers={"User-Agent": settings.USER_AGENT},
            timeout=settings.HTTP_TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Bing fetch failed (query=%r page=%d): %s", query, page, exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    return [
        a["href"] for a in soup.select(engine["selectors"])
        if a.get("href", "").startswith("http")
    ]


async def _fetch_ddg_page(client: httpx.AsyncClient, engine: dict, query: str, page: int) -> list:
    offset = page * 10
    data = {"q": query, "b": "", "kl": "wt-wt"}
    if offset > 0:
        data["s"] = str(offset)
        data["dc"] = str(offset + 1)
    try:
        resp = await client.post(
            engine["url"],
            data=data,
            headers={
                "User-Agent": settings.USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=settings.HTTP_TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("DDG fetch failed (query=%r page=%d): %s", query, page, exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    return [
        a.get("href", "") for a in soup.select(engine["selectors"])
        if a.get("href", "").startswith("http") and "duckduckgo.com" not in a.get("href", "")
    ]


async def _fetch_page(client: httpx.AsyncClient, engine_name: str, query: str, page: int) -> list:
    engine = _ENGINES.get(engine_name)
    if not engine:
        logger.error("Unknown search engine %r — falling back to bing", engine_name)
        engine = _ENGINES["bing"]
        engine_name = "bing"
    if engine_name == "duckduckgo":
        return await _fetch_ddg_page(client, engine, query, page)
    return await _fetch_bing_page(client, engine, query, page)


async def discover_urls(run_id: int, seen_domains: set) -> list:
    """
    Queries the configured search engine and returns new URLs to process.

    Args:
        run_id:       Current run ID — URLs are associated with it in DB.
        seen_domains: In-memory set pre-loaded from DB. Updated in-place
                      as new domains are found. Zero DB reads during the loop.

    Returns:
        List of {url, domain, url_type} dicts — only domains not in seen_domains.
    """
    cfg = _load_config()
    disc        = cfg["discovery"]
    classifier  = disc["url_classifier"]
    queries     = disc["search_queries"]
    max_pages   = disc["max_pages_per_query"]
    max_total   = disc["max_urls_per_run"]
    engine_name = settings.SEARCH_ENGINE

    logger.info("Discovery using engine: %s  |  seen domains loaded: %d", engine_name, len(seen_domains))

    new_entries: list[dict] = []

    async with httpx.AsyncClient() as client:
        for query in queries:
            logger.info("Querying [%s]: %r", engine_name, query)
            for page in range(max_pages):
                if len(new_entries) >= max_total:
                    break
                urls = await _fetch_page(client, engine_name, query, page)
                for url in urls:
                    domain = _extract_domain(url)
                    if domain in seen_domains:          # O(1) — no DB hit
                        continue
                    url_type = _classify_url(url, classifier)
                    if url_type == "discard":
                        continue
                    seen_domains.add(domain)            # update set in-place
                    new_entries.append({"url": url, "domain": domain, "url_type": url_type})
                await asyncio.sleep(1.5)
            if len(new_entries) >= max_total:
                break

    # One batch write at the end — not inside the loop
    database.batch_insert_discovered_urls(new_entries, run_id)
    logger.info("Discovery complete: %d new URLs  |  total seen: %d", len(new_entries), len(seen_domains))
    return new_entries
