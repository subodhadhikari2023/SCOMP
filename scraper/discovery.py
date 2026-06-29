"""
Search engine querying + URL classification.
Engine is selected via SEARCH_ENGINE env var (bing | duckduckgo).
Paginates up to max_pages_per_query, scores each result URL, and
returns a deduplicated approved list.
"""

import asyncio
import logging
from urllib.parse import urlparse

import httpx
import yaml
from bs4 import BeautifulSoup

from config import settings

logger = logging.getLogger(__name__)

_ENGINES = {
    "bing": {
        "url":         "https://www.bing.com/search",
        "page_param":  "first",       # value: page * 10 + 1
        "count_param": "count",
        "selectors":   "li.b_algo h2 a, li.b_algo a.tilk",
    },
    "duckduckgo": {
        "url":         "https://html.duckduckgo.com/html/",
        "page_param":  None,          # DDG HTML does not support offset pagination cleanly
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


async def _fetch_bing_page(client: httpx.AsyncClient, engine: dict, query: str, page: int) -> list[str]:
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


async def _fetch_ddg_page(client: httpx.AsyncClient, engine: dict, query: str, page: int) -> list[str]:
    # DuckDuckGo HTML interface uses POST with 's' (start offset) for pagination
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
    urls = []
    for a in soup.select(engine["selectors"]):
        href = a.get("href", "")
        if href.startswith("http") and "duckduckgo.com" not in href:
            urls.append(href)
    return urls


async def _fetch_page(client: httpx.AsyncClient, engine_name: str, query: str, page: int) -> list[str]:
    engine = _ENGINES.get(engine_name)
    if not engine:
        logger.error("Unknown search engine: %r — falling back to bing", engine_name)
        engine = _ENGINES["bing"]
        engine_name = "bing"

    if engine_name == "duckduckgo":
        return await _fetch_ddg_page(client, engine, query, page)
    return await _fetch_bing_page(client, engine, query, page)


async def discover_urls() -> list[dict]:
    """
    Returns a list of dicts: {url, domain, url_type}.
    Uses SEARCH_ENGINE env var to pick the engine (default: bing).
    """
    cfg = _load_config()
    disc = cfg["discovery"]
    classifier = disc["url_classifier"]
    queries: list[str]  = disc["search_queries"]
    max_pages: int       = disc["max_pages_per_query"]
    max_total: int       = disc["max_urls_per_run"]
    engine_name: str     = settings.SEARCH_ENGINE

    logger.info("Discovery using search engine: %s", engine_name)

    seen_domains: set[str] = set()
    results: list[dict]    = []

    async with httpx.AsyncClient() as client:
        for query in queries:
            logger.info("Querying [%s]: %r", engine_name, query)
            for page in range(max_pages):
                if len(results) >= max_total:
                    break
                urls = await _fetch_page(client, engine_name, query, page)
                for url in urls:
                    domain = _extract_domain(url)
                    if domain in seen_domains:
                        continue
                    url_type = _classify_url(url, classifier)
                    if url_type == "discard":
                        continue
                    seen_domains.add(domain)
                    results.append({"url": url, "domain": domain, "url_type": url_type})
                await asyncio.sleep(1.5)
            if len(results) >= max_total:
                break

    logger.info("Discovery complete: %d unique URLs", len(results))
    return results
