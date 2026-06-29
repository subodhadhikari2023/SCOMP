"""
Search engine querying + URL classification.
Engine is selected via SEARCH_ENGINE env var (bing | duckduckgo).

Memory + persistence model:
  seen_domains  — Python set, grows throughout the run, never cleared.
                  Loaded from DB once at startup. All per-URL dedup is
                  O(1) against this set — zero DB reads during the loop.

  flush_buffer  — list[dict] of entries not yet written to DB.
                  Cleared to [] after every DISCOVERY_FLUSH_EVERY entries.
                  If the process crashes, only buffered (unflushed) entries
                  are lost; everything already flushed is safe in DB.

  all_entries   — list[dict] accumulating every new URL this run.
                  Returned to the caller unchanged; never cleared.
"""

import asyncio
import logging
import random
from urllib.parse import urlparse

import httpx
import yaml
from bs4 import BeautifulSoup

from config import settings
from db import database

logger = logging.getLogger(__name__)

_USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

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


def _flush(buffer: list, run_id: int) -> None:
    """Writes the buffer to DB and clears it in-place."""
    if buffer:
        database.batch_insert_discovered_urls(buffer, run_id)
        logger.debug("Flushed %d URLs to DB.", len(buffer))
        buffer.clear()


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
        engine      = _ENGINES["bing"]
        engine_name = "bing"
    if engine_name == "duckduckgo":
        return await _fetch_ddg_page(client, engine, query, page)
    return await _fetch_bing_page(client, engine, query, page)


async def discover_urls(run_id: int, seen_domains: set) -> list:
    """
    Queries the configured search engine and returns new URLs to process.

    Args:
        run_id:       Current run ID — URLs are tagged with it in DB.
        seen_domains: In-memory set pre-loaded from DB. Updated in-place
                      as new domains are found. Never cleared mid-run.

    Returns:
        List of all {url, domain, url_type} dicts discovered this run.

    Flush behaviour:
        Every DISCOVERY_FLUSH_EVERY new entries, the flush_buffer is
        written to DB and cleared. seen_domains is unaffected — it keeps
        growing so in-run dedup stays correct after every flush.
        A final flush handles any remainder at the end.
    """
    cfg         = _load_config()
    disc        = cfg["discovery"]
    classifier  = disc["url_classifier"]
    queries     = disc["search_queries"]
    max_pages   = disc["max_pages_per_query"]
    max_total   = disc["max_urls_per_run"]
    engine_name = settings.SEARCH_ENGINE
    flush_every = settings.DISCOVERY_FLUSH_EVERY

    logger.info(
        "Discovery | engine: %s | seen: %d domains | flush every: %d",
        engine_name, len(seen_domains), flush_every,
    )

    all_entries:   list[dict] = []   # full result — returned to caller, never cleared
    flush_buffer:  list[dict] = []   # pending DB write — cleared after each flush

    async with httpx.AsyncClient() as client:
        for query in queries:
            logger.info("Querying [%s]: %r", engine_name, query)
            for page in range(max_pages):
                if len(all_entries) >= max_total:
                    break

                # Rotate User-Agent per request to reduce rate-limit risk
                client.headers.update({"User-Agent": random.choice(_USER_AGENTS)})
                urls = await _fetch_page(client, engine_name, query, page)

                for url in urls:
                    domain = _extract_domain(url)

                    if domain in seen_domains:       # O(1) — pure RAM, no DB
                        continue

                    url_type = _classify_url(url, classifier)
                    if url_type == "discard":
                        continue

                    entry = {"url": url, "domain": domain, "url_type": url_type}

                    seen_domains.add(domain)         # grow set in-place
                    all_entries.append(entry)        # accumulate full result
                    flush_buffer.append(entry)       # stage for next flush

                    # Incremental flush — crash-safe checkpoint
                    if len(flush_buffer) >= flush_every:
                        _flush(flush_buffer, run_id)
                        logger.info(
                            "Incremental flush | flushed at %d total | seen: %d",
                            len(all_entries), len(seen_domains),
                        )

                await asyncio.sleep(random.uniform(1.5, 3.0))

            if len(all_entries) >= max_total:
                break

            # Longer pause between queries to avoid DDG rate-limiting
            await asyncio.sleep(random.uniform(3.0, 6.0))

    # Final flush — clears any remainder that didn't hit the threshold
    _flush(flush_buffer, run_id)

    logger.info(
        "Discovery complete | new: %d URLs | total seen: %d | DB flush size: %d",
        len(all_entries), len(seen_domains), flush_every,
    )
    return all_entries
