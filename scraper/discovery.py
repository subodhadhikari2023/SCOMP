"""
Search engine querying + URL classification.
Engine is selected via SEARCH_ENGINE env var (google | duckduckgo | bing).

Memory + persistence model:
  seen_domains  — Python set, grows throughout the run, never cleared.
                  Loaded from DB once at startup. All per-URL dedup is
                  O(1) against this set — zero DB reads during the loop.

  flush_buffer  — list[dict] of entries not yet written to DB.
                  Cleared to [] after every DISCOVERY_FLUSH_EVERY entries.
                  If the process crashes, only buffered (unflushed) entries
                  are lost; everything already flushed is safe in DB.
"""

import asyncio
import logging
import random
import re
from urllib.parse import urlparse, quote_plus

from pathlib import Path

import httpx
import yaml
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from config import settings
from db import database

logger = logging.getLogger(__name__)


class _BraveCaptchaError(Exception):
    """Raised when Brave returns a CAPTCHA / bot-verification page."""


def _load_queries() -> list[str]:
    """Load all search queries from config/queries.yaml and return as a flat shuffled list."""
    path = Path(settings.BASE_DIR) / "config" / "queries.yaml"
    data = yaml.safe_load(path.read_text())
    queries: list[str] = []
    for category in data.get("queries", {}).values():
        queries.extend(category)
    logger.info("Loaded %d queries from queries.yaml", len(queries))
    return queries


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

# ── Google Playwright stealth ─────────────────────────────────────────────────

_STEALTH_SCRIPT = """
() => {
    // Remove the primary automation fingerprint
    Object.defineProperty(navigator, 'webdriver', { get: () => false });

    // Realistic language list
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

    // Spoof enough plugins to look like a real browser
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const p = [
                { name: 'PDF Viewer',   filename: 'internal-pdf-viewer',     description: 'Portable Document Format' },
                { name: 'Chrome PDF Viewer',    filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                { name: 'Chromium PDF Viewer',  filename: 'internal-pdf-viewer',              description: '' },
                { name: 'Microsoft Edge PDF Viewer', filename: 'msedge-pdf-viewer',           description: '' },
                { name: 'WebKit built-in PDF',  filename: 'webkit-pdf-viewer',                description: '' },
            ];
            p.refresh = () => {};
            p.item    = (i) => p[i];
            p.namedItem = (n) => p.find(x => x.name === n) || null;
            Object.setPrototypeOf(p, PluginArray.prototype);
            return p;
        }
    });

    // Make permissions API behave normally
    if (navigator.permissions) {
        const _query = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = (p) =>
            p.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : _query(p);
    }

    // Hide the automation chrome flag used by some fingerprint scripts
    window.chrome = window.chrome || { runtime: {} };
}
"""


async def _launch_google_context(pw):
    """
    Creates a stealth Playwright Firefox browser + context for Google scraping.
    Returns (browser, context). Caller is responsible for closing the browser.
    Uses Firefox: less common in automation tooling → lower bot-detection signal.
    """
    browser = await pw.firefox.launch(headless=True)
    context = await browser.new_context(
        viewport={"width": 1366, "height": 768},
        locale="en-US",
        timezone_id="America/New_York",
        user_agent=random.choice(_USER_AGENTS),
        extra_http_headers={
            "Accept":                  "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language":         "en-US,en;q=0.5",
            "Accept-Encoding":         "gzip, deflate, br",
            "DNT":                     "1",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest":          "document",
            "Sec-Fetch-Mode":          "navigate",
            "Sec-Fetch-Site":          "none",
        },
    )
    await context.add_init_script(_STEALTH_SCRIPT)
    return browser, context


async def _accept_google_consent(page) -> None:
    """Clicks through Google's GDPR consent wall if present."""
    for selector in (
        'button:text("Accept all")',
        'button:text("I agree")',
        'button:text("Agree")',
        '#L2AGLb',                       # common consent button id
        'form[action*="consent"] button',
    ):
        try:
            btn = page.locator(selector).first
            if await btn.count() > 0:
                await btn.click()
                await page.wait_for_load_state("domcontentloaded", timeout=8000)
                return
        except Exception:
            pass


async def _fetch_google_page(ctx, query: str, page: int) -> list[str]:
    """
    Fetches one page of Google organic results in a reused stealth context.
    Handles consent walls, CAPTCHA detection, and JS-rendered result links.
    """
    start = page * 10
    url   = (
        f"https://www.google.com/search"
        f"?q={quote_plus(query)}&start={start}&num=10&hl=en&gl=us"
    )
    pw_page = await ctx.new_page()
    try:
        await pw_page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        await _accept_google_consent(pw_page)

        # Wait for the search-results container to exist in the DOM.
        # domcontentloaded fires before React/JS renders results — this bridges the gap.
        try:
            await pw_page.wait_for_selector(
                "#search, #rso, #rcnt, div[data-async-context]",
                state="attached", timeout=12_000,
            )
        except Exception:
            pass  # no results container — log below and return []

        # Simulate human reading time
        await asyncio.sleep(random.uniform(2.0, 4.5))

        title = await pw_page.title()
        logger.debug("Google page=%d title=%r query=%r", page, title, query)

        # Title is the URL → Google served a no-title block/consent page
        title_is_url = title.startswith("http")
        if title_is_url or any(kw in title.lower() for kw in ("captcha", "unusual traffic", "robot", "verify")):
            logger.warning("Google bot check hit (query=%r page=%d) title=%r", query, page, title)
            return []

        links: list[str] = await pw_page.evaluate("""
            () => {
                const seen = new Set();
                const out  = [];
                // Covers classic #search, newer #rso, and any direct body anchors
                const selectors = [
                    '#search a[href]',
                    '#rso a[href]',
                    '#rcnt a[href]',
                    'div[data-async-context] a[href]',
                ];
                for (const sel of selectors) {
                    document.querySelectorAll(sel).forEach(a => {
                        let h = a.href || '';
                        // Unwrap Google redirect URLs  /url?q=TARGET&...
                        if (h.includes('/url?')) {
                            try { h = new URL(h).searchParams.get('q') || h; }
                            catch (_) {}
                        }
                        if (h.startsWith('http') &&
                            !h.includes('google.com') &&
                            !h.includes('google.co') &&
                            !h.includes('googleapi') &&
                            !h.includes('youtube.com') &&
                            !seen.has(h)) {
                            seen.add(h);
                            out.push(h);
                        }
                    });
                }
                return out;
            }
        """)

        if not links:
            preview = await pw_page.evaluate(
                "document.body ? document.body.innerText.slice(0, 400).replace(/\\n+/g, ' ') : 'NO BODY'"
            )
            logger.debug("Google 0 links (page=%d query=%r) — body: %s", page, query, preview)
            # Catch block pages that slipped past the title check
            if any(kw in preview.lower() for kw in ("unusual traffic", "not a robot", "detected unusual")):
                logger.warning("Google IP block confirmed in body — stopping this query")
                return []

        logger.debug("Google page=%d for %r → %d links", page, query, len(links))
        return links
    except Exception as exc:
        logger.warning("Google fetch failed (query=%r page=%d): %s", query, page, exc)
        return []
    finally:
        await pw_page.close()


async def _fetch_bing_playwright_page(ctx, query: str, page: int) -> list[str]:
    """
    Fetches one page of Bing results using the same stealth Playwright context.
    Avoids the JS-challenge that httpx triggers on Bing.
    """
    offset = page * 10 + 1
    url = f"https://www.bing.com/search?q={quote_plus(query)}&first={offset}&count=10"
    pw_page = await ctx.new_page()
    try:
        await pw_page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        # Wait for Bing result list to render
        try:
            await pw_page.wait_for_selector(
                "#b_results, ol#b_results, li.b_algo",
                state="attached", timeout=12_000,
            )
        except Exception:
            pass

        await asyncio.sleep(random.uniform(1.5, 3.5))

        title = await pw_page.title()
        logger.debug("Bing page=%d title=%r query=%r", page, title, query)

        if any(kw in title.lower() for kw in ("captcha", "unusual activity", "verify you're human")):
            logger.warning("Bing bot check hit (query=%r page=%d) title=%r", query, page, title)
            return []

        links: list[str] = await pw_page.evaluate("""
            () => {
                const seen = new Set();
                const out  = [];
                const skipDomains = [
                    'bing.com', 'microsoft.com', 'youtube.com', 'msn.com',
                    'microsofttranslator.com', 'live.com', 'windows.com',
                ];

                // Bing wraps ALL result hrefs in its own redirect system, so a[href]
                // only ever contains bing.com URLs. The real result URLs appear as plain
                // text in the page body (e.g. "https://example.com › path"). Extract them
                // directly from innerText via regex.
                const text = document.body ? document.body.innerText : '';
                const matches = text.match(/https?:\\/\\/[^\\s›\\u203A\\u00BB]+/g) || [];
                for (let h of matches) {
                    // Strip trailing punctuation that may bleed in
                    h = h.replace(/[.,;:)>]+$/, '');
                    if (!h.startsWith('http')) continue;
                    if (skipDomains.some(d => h.includes(d))) continue;
                    if (seen.has(h)) continue;
                    seen.add(h);
                    out.push(h);
                }
                return out;
            }
        """)

        if not links:
            preview = await pw_page.evaluate(
                "document.body ? document.body.innerText.slice(0, 300).replace(/\\n+/g, ' ') : 'NO BODY'"
            )
            logger.debug("Bing 0 links (page=%d query=%r) — body: %s", page, query, preview)
            if any(kw in preview.lower() for kw in ("solve the challenge", "not a robot", "unusual activity")):
                logger.warning("Bing bot check confirmed in body — stopping this query")
                return []

        logger.debug("Bing page=%d for %r → %d links", page, query, len(links))
        return links
    except Exception as exc:
        logger.warning("Bing fetch failed (query=%r page=%d): %s", query, page, exc)
        return []
    finally:
        await pw_page.close()


async def _fetch_brave_page(ctx, query: str, page: int) -> list[str]:
    """
    Fetches one page of Brave Search results using stealth Playwright.
    Brave is less aggressive on bot detection than Google/Bing.
    """
    offset = page * 10
    url = f"https://search.brave.com/search?q={quote_plus(query)}&offset={offset}&source=web"
    pw_page = await ctx.new_page()
    try:
        await pw_page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        try:
            await pw_page.wait_for_selector(
                "#results, .snippet, [data-type='web']",
                state="attached", timeout=10_000,
            )
        except Exception:
            pass

        await asyncio.sleep(random.uniform(1.5, 3.0))

        title = await pw_page.title()
        logger.debug("Brave page=%d title=%r query=%r", page, title, query)

        if any(kw in title.lower() for kw in ("captcha", "unusual", "verify", "robot", "not a bot")):
            logger.warning("Brave CAPTCHA in title (query=%r) — rotating context", query)
            raise _BraveCaptchaError(query)

        links: list[str] = await pw_page.evaluate("""
            () => {
                const seen = new Set();
                const out  = [];
                const skipDomains = ['brave.com', 'microsoft.com', 'youtube.com'];

                // Primary: Brave result title links in #results
                const bySelector = document.querySelectorAll(
                    '#results .snippet-title a, #results a[href], .snippet a[href]'
                );
                bySelector.forEach(a => {
                    const h = a.href || '';
                    if (!h.startsWith('http')) return;
                    if (skipDomains.some(d => h.includes(d))) return;
                    if (seen.has(h)) return;
                    seen.add(h);
                    out.push(h);
                });

                // Fallback: regex-extract https:// URLs from innerText
                if (out.length === 0) {
                    const text = document.body ? document.body.innerText : '';
                    const matches = text.match(/https?:\\/\\/[^\\s›\\u203A\\u00BB]+/g) || [];
                    for (let h of matches) {
                        h = h.replace(/[.,;:)>]+$/, '');
                        if (!h.startsWith('http')) continue;
                        if (skipDomains.some(d => h.includes(d))) continue;
                        if (seen.has(h)) continue;
                        seen.add(h);
                        out.push(h);
                    }
                }
                return out;
            }
        """)

        if not links:
            preview = await pw_page.evaluate(
                "document.body ? document.body.innerText.slice(0, 300).replace(/\\n+/g, ' ') : 'NO BODY'"
            )
            logger.debug("Brave 0 links (page=%d query=%r) — body: %s", page, query, preview)
            captcha_kws = ("not a bot", "verifying", "captcha", "solve the challenge",
                           "not a robot", "unusual", "bot check")
            if any(kw in preview.lower() for kw in captcha_kws):
                logger.warning("Brave CAPTCHA confirmed in body (query=%r) — rotating context", query)
                raise _BraveCaptchaError(query)

        logger.debug("Brave page=%d for %r → %d links", page, query, len(links))
        return links
    except _BraveCaptchaError:
        raise   # let the consumer loop's except _BraveCaptchaError: handle rotation
    except Exception as exc:
        logger.warning("Brave fetch failed (query=%r page=%d): %s", query, page, exc)
        return []
    finally:
        await pw_page.close()


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
            headers={
                "User-Agent": random.choice(_USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "DNT": "1",
                "Upgrade-Insecure-Requests": "1",
                "Referer": "https://www.bing.com/",
            },
            timeout=settings.HTTP_TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Bing fetch failed (query=%r page=%d): %s", query, page, exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    links = [
        a["href"] for a in soup.select(engine["selectors"])
        if a.get("href", "").startswith("http")
    ]
    if not links:
        # Detect Bing bot challenge page
        text = soup.get_text(" ", strip=True)[:300]
        if any(kw in text.lower() for kw in ("unusual activity", "verify", "captcha", "robot")):
            logger.warning("Bing bot check detected (query=%r page=%d)", query, page)
        else:
            logger.debug("Bing 0 links (page=%d query=%r) — body: %s", page, query, text)
    return links


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


async def discover_urls(
    run_id: int,
    seen_domains: set,
    output_queue: asyncio.Queue,     # push {url, domain, url_type} dicts here as they're found
    upstream_done: asyncio.Event,    # set when all discovery is complete
    on_queries_ready=None,           # (queries: list[str]) → fires each time a new batch is ready
    on_query_start=None,             # (query: str, idx: int, total: int)
    on_page_done=None,               # (query: str, page: int, total_pages: int, total_found: int)
    on_query_done=None,              # (query: str, total_found: int)
    put_sentinel: bool = True,       # set False for multi-pass (caller manages sentinel)
) -> dict:
    """
    Streaming discovery: loads 520+ queries from queries.yaml (producer) while the
    search engine consumer fetches result pages and pushes each discovered URL to
    output_queue immediately — Stage 2-4 can start processing before Stage 1 finishes.

    Producer loop:
        Shuffles the full query pool, filters already-used queries, then feeds them
        into query_queue one at a time with backpressure (waits when queue depth > 5).
        Stops when max_urls_per_run is reached or the query pool is exhausted.

    Buffer behaviour:
        output_queue is bounded (maxsize set by caller) — backpressure from Stage 2-4
        naturally slows Stage 1 if downstream can't keep up.
        flush_buffer still checkpoints to DB every DISCOVERY_FLUSH_EVERY entries.

    # TODO: asyncio.Semaphore(2) parallel DDG workers — measure rate-limit ceiling first.
    """
    cfg         = _load_config()
    disc        = cfg["discovery"]
    classifier  = disc["url_classifier"]
    max_pages   = disc["max_pages_per_query"]
    max_total   = disc["max_urls_per_run"]
    engine_name = settings.SEARCH_ENGINE
    flush_every = settings.DISCOVERY_FLUSH_EVERY

    logger.info(
        "Discovery | engine: %s | seen: %d domains | flush every: %d",
        engine_name, len(seen_domains), flush_every,
    )

    total_found  = 0
    flush_buffer: list[dict] = []

    # ── Query producer → internal query queue ─────────────────────────────────
    # Loads all queries from queries.yaml, shuffles, filters already-used ones,
    # then feeds them into query_queue with backpressure when the consumer lags.
    stop_event:  asyncio.Event             = asyncio.Event()
    query_queue: asyncio.Queue[str | None] = asyncio.Queue()
    all_prev_queries: list[str]            = []
    total_queries_box                      = [0]

    async def _producer() -> None:
        all_queries = _load_queries()
        random.shuffle(all_queries)

        used  = {q.lower() for q in all_prev_queries}
        fresh = [q for q in all_queries if q.lower() not in used]

        total_queries_box[0] = len(fresh)
        if on_queries_ready:
            on_queries_ready(fresh)

        for q in fresh:
            if stop_event.is_set():
                break
            all_prev_queries.append(q)
            await query_queue.put(q)
            while query_queue.qsize() > 5 and not stop_event.is_set():
                await asyncio.sleep(1.0)

        await query_queue.put(None)
        logger.info("Producer done | queries queued: %d", total_queries_box[0])

    producer_task = asyncio.create_task(_producer())

    # ── Engine setup ─────────────────────────────────────────────────────────
    # google / bing / brave → stealth Playwright (bypasses JS challenges)
    # duckduckgo            → httpx (fallback only; IP is currently banned)
    _pw           = None
    _pw_browser   = None
    _pw_ctx       = None
    _http_client  = None
    _bing_max_pages = None  # set per-engine below when needed

    if engine_name in ("google", "bing", "brave"):
        _pw = await async_playwright().start()
        _pw_browser, _pw_ctx = await _launch_google_context(_pw)
        if engine_name == "google":
            async def _fetch_fn(q: str, p: int) -> list:
                return await _fetch_google_page(_pw_ctx, q, p)
            _page_delay  = (8.0, 15.0)
            _query_delay = (15.0, 30.0)
            logger.info("Google engine: stealth Playwright context ready")
        elif engine_name == "brave":
            async def _fetch_fn(q: str, p: int) -> list:
                return await _fetch_brave_page(_pw_ctx, q, p)
            _page_delay  = (0.0, 0.0)   # unused — Brave has results only on page 0
            _query_delay = (10.0, 18.0)
            _bing_max_pages = 1          # Brave index depth is 1 page per query
            logger.info("Brave Search engine: stealth Playwright context ready")
        else:
            # Bing triggers a per-session captcha after the first result page.
            # We rotate the browser context per query to reset session state.
            async def _fetch_fn(q: str, p: int) -> list:
                return await _fetch_bing_playwright_page(_pw_ctx, q, p)
            _page_delay  = (0.0, 0.0)   # unused — Bing uses only page 0
            _query_delay = (12.0, 20.0)
            _bing_max_pages = 1          # only scrape first 10 results per query
            logger.info("Bing engine: stealth Playwright context ready")
    else:
        _http_client = httpx.AsyncClient()
        async def _fetch_fn(q: str, p: int) -> list:
            return await _fetch_page(_http_client, engine_name, q, p)
        # DDG: IP banned — extreme delays as last-resort fallback
        _page_delay  = (10.0, 20.0)
        _query_delay = (20.0, 40.0)

    # ── Search consumer → output_queue (Stage 2-4) ───────────────────────────
    _bing_max_pages = locals().get("_bing_max_pages", None)
    effective_max_pages = _bing_max_pages if _bing_max_pages else max_pages
    _brave_consec_captcha = 0   # consecutive CAPTCHA hits; triggers context rotation
    try:
        qi = 0
        while True:
            try:
                query = await asyncio.wait_for(query_queue.get(), timeout=120.0)
            except asyncio.TimeoutError:
                logger.warning("Query queue empty for 120s — stopping discovery")
                break

            if query is None:
                break

            # Stop immediately if a fatal engine block was signalled (e.g. Brave CAPTCHA storm)
            if stop_event.is_set():
                break

            # Rotate Bing browser context per query to reset session / avoid captcha
            if engine_name == "bing" and _pw_browser:
                try:
                    await _pw_ctx.close()
                    _pw_ctx = await _pw_browser.new_context(
                        viewport={"width": 1366, "height": 768},
                        locale="en-US",
                        timezone_id="America/New_York",
                        user_agent=random.choice(_USER_AGENTS),
                        extra_http_headers={
                            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                            "Accept-Language": "en-US,en;q=0.9",
                        },
                    )
                    await _pw_ctx.add_init_script(_STEALTH_SCRIPT)

                    async def _fetch_fn(q: str, p: int) -> list:  # noqa: F811
                        return await _fetch_bing_playwright_page(_pw_ctx, q, p)
                except Exception as _e:
                    logger.debug("Bing context rotation failed: %s", _e)

            logger.info("Querying [%s]: %r", engine_name, query)
            if on_query_start:
                on_query_start(query, qi, total_queries_box[0])

            for page in range(effective_max_pages):
                if total_found >= max_total:
                    break

                try:
                    urls = await _fetch_fn(query, page)
                    _brave_consec_captcha = 0   # successful fetch — reset counter
                except _BraveCaptchaError:
                    _brave_consec_captcha += 1
                    logger.warning(
                        "Brave CAPTCHA #%d consecutive — %s",
                        _brave_consec_captcha,
                        "rotating context" if _brave_consec_captcha < 4 else "giving up this pass",
                    )
                    if _brave_consec_captcha < 4 and _pw_browser:
                        # Rotate the Playwright context to get a fresh browser fingerprint
                        try:
                            old_ctx = _pw_ctx
                            _pw_ctx = await _pw_browser.new_context(
                                viewport={"width": 1366, "height": 768},
                                locale="en-US",
                                timezone_id="America/New_York",
                                user_agent=random.choice(_USER_AGENTS),
                                extra_http_headers={
                                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                                    "Accept-Language": "en-US,en;q=0.9",
                                },
                            )
                            await _pw_ctx.add_init_script(_STEALTH_SCRIPT)
                            # Rebind _fetch_fn to the new context
                            async def _fetch_fn(q: str, p: int) -> list:  # noqa: F811
                                return await _fetch_brave_page(_pw_ctx, q, p)
                            await old_ctx.close()
                        except Exception as _rot_err:
                            logger.debug("Brave context rotation failed: %s", _rot_err)
                        # Back off before retrying with fresh context
                        backoff = 30 * _brave_consec_captcha
                        logger.info("Brave: waiting %ds after CAPTCHA before next query", backoff)
                        await asyncio.sleep(backoff)
                    else:
                        # Too many consecutive CAPTCHAs — stop querying for this pass
                        logger.warning("Brave: 4 consecutive CAPTCHAs — stopping discovery")
                        stop_event.set()
                    urls = []

                for url in urls:
                    domain = _extract_domain(url)
                    if domain in seen_domains:
                        continue
                    url_type = _classify_url(url, classifier)
                    if url_type == "discard":
                        continue

                    entry = {"url": url, "domain": domain, "url_type": url_type}
                    seen_domains.add(domain)
                    flush_buffer.append(entry)
                    total_found += 1

                    await output_queue.put(entry)

                    if len(flush_buffer) >= flush_every:
                        _flush(flush_buffer, run_id)
                        logger.info("Incremental flush | total: %d | seen: %d",
                                    total_found, len(seen_domains))

                if on_page_done:
                    on_page_done(query, page, effective_max_pages, total_found)

                if effective_max_pages > 1:
                    await asyncio.sleep(random.uniform(*_page_delay))

            if on_query_done:
                on_query_done(query, total_found)

            if total_found >= max_total:
                stop_event.set()
                break

            qi += 1
            await asyncio.sleep(random.uniform(*_query_delay))

    finally:
        if _pw_browser:
            await _pw_browser.close()
        if _pw:
            await _pw.stop()
        if _http_client:
            await _http_client.aclose()

    stop_event.set()
    await producer_task
    _flush(flush_buffer, run_id)

    upstream_done.set()
    if put_sentinel:
        await output_queue.put(None)   # sentinel — Stage 2-4 drains then stops

    logger.info("Discovery complete | found: %d | seen: %d", total_found, len(seen_domains))
    return {"found": total_found}
