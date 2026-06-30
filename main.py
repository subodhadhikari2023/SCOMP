"""
S.C.O.M.P  —  Stealth Collection, Outreach & Messaging Pipeline
Entry point and pipeline orchestrator.

Usage:
  python main.py --run            Full pipeline end to end
  python main.py --discover       Discovery + scraping only
  python main.py --write          Copywriting only
  python main.py --send           Dispatch only
  python main.py --dashboard      Live terminal dashboard
  python main.py --summary        Print today's run summary
  python main.py --setup          First-time setup wizard
  python main.py --setup-sender   One-time Outlook Web login (run before first --send)
"""

import argparse
import asyncio
import logging
import os
import platform
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import yaml
from bs4 import BeautifulSoup
from rich.console import Console
from rich.prompt import Confirm

sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from db import database
from pipeline import copywriter, dispatcher, normalizer
from scraper import auth_bootstrap, discovery, email_extractor, router
from ui import dashboard
from ui.dashboard import PipelineState, PipelineUI, RUNNING, DONE, ERROR

console = Console()


# ── Logging ───────────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    os.makedirs(settings.LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")

    # Combined log — everything goes here
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    combined = logging.FileHandler(os.path.join(settings.LOG_DIR, "scomp.log"))
    combined.setFormatter(fmt)
    root.addHandler(combined)

    # Per-stage log files — each stage logger also writes to its own file
    _stage_logs = {
        "s1_discovery.log":  ["scraper.discovery"],
        "s24_scrape.log":    ["scraper.email_extractor", "scraper.router", "pipeline.normalizer"],
        "s5_storage.log":    ["db.database"],
        "s6_copywriter.log": ["pipeline.copywriter"],
        "s7_dispatch.log":   ["pipeline.dispatcher"],
        "main.log":          ["scomp.main"],
    }
    for filename, names in _stage_logs.items():
        fh = logging.FileHandler(os.path.join(settings.LOG_DIR, filename))
        fh.setFormatter(fmt)
        for name in names:
            lg = logging.getLogger(name)
            lg.addHandler(fh)

    for lib in ("httpx", "httpcore", "playwright", "asyncio"):
        logging.getLogger(lib).setLevel(logging.WARNING)

logger = logging.getLogger("scomp.main")


# ── First-boot autostart ───────────────────────────────────────────────────────

def _check_first_boot() -> None:
    lock = Path(settings.BASE_DIR) / ".autostart_asked"
    if lock.exists():
        return
    lock.touch()

    if not sys.stdin.isatty():
        return

    console.print()
    console.rule("[bold cyan]S.C.O.M.P  —  First-Boot Setup[/bold cyan]")
    console.print(
        "\n  SCOMP can register itself to start automatically when your machine boots.\n"
    )
    try:
        if Confirm.ask("  Enable auto-start on boot?", default=False):
            _register_autostart()
        else:
            console.print(
                "  [dim]Skipped. Run [bold]python main.py --setup[/bold] anytime to change this.[/dim]"
            )
    except (EOFError, KeyboardInterrupt):
        pass
    console.print()


def _register_autostart() -> None:
    system = platform.system()
    base   = str(Path(__file__).parent)
    python = sys.executable

    if system == "Linux":
        service = (
            f"[Unit]\nDescription=S.C.O.M.P Outreach Pipeline\nAfter=network.target\n\n"
            f"[Service]\nType=simple\nWorkingDirectory={base}\n"
            f"ExecStart={python} {base}/main.py --run\nRestart=no\n"
            f"StandardOutput=append:{settings.LOG_DIR}/scomp.log\n"
            f"StandardError=append:{settings.LOG_DIR}/scomp.log\n\n"
            f"[Install]\nWantedBy=default.target\n"
        )
        svc = Path.home() / ".config/systemd/user/scomp.service"
        svc.parent.mkdir(parents=True, exist_ok=True)
        svc.write_text(service)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "scomp.service"], check=False)
        console.print(f"  [green]Systemd user service installed:[/green] {svc}")

    elif system == "Darwin":
        plist = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"\n'
            '  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            f'  <key>Label</key><string>com.scomp.pipeline</string>\n'
            f'  <key>ProgramArguments</key><array>'
            f'<string>{python}</string><string>{base}/main.py</string>'
            f'<string>--run</string></array>\n'
            f'  <key>WorkingDirectory</key><string>{base}</string>\n'
            f'  <key>RunAtLoad</key><true/>\n'
            f'  <key>StandardOutPath</key><string>{settings.LOG_DIR}/scomp.log</string>\n'
            f'  <key>StandardErrorPath</key><string>{settings.LOG_DIR}/scomp.log</string>\n'
            '</dict></plist>\n'
        )
        plist_path = Path.home() / "Library/LaunchAgents/com.scomp.pipeline.plist"
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(plist)
        subprocess.run(["launchctl", "load", str(plist_path)], check=False)
        console.print(f"  [green]LaunchAgent installed:[/green] {plist_path}")

    elif system == "Windows":
        cmd = (
            f'schtasks /create /tn "SCOMP" /tr "{python} {base}\\main.py --run" '
            f'/sc ONLOGON /rl HIGHEST /f'
        )
        subprocess.run(cmd, shell=True, check=False)
        console.print("  [green]Windows Task Scheduler entry created.[/green]")

    else:
        console.print("  [yellow]Unsupported OS — add SCOMP to startup manually.[/yellow]")


# ── HTML helpers ──────────────────────────────────────────────────────────────

def _get_known_site_config(domain: str, known: dict) -> dict | None:
    for cfg in known.values():
        site_domain = urlparse(cfg.get("base_url", "")).netloc.lstrip("www.")
        if site_domain and site_domain in domain:
            return cfg
    return None


def _get_site_name(domain: str, known: dict) -> str:
    for name, cfg in known.items():
        site_domain = urlparse(cfg.get("base_url", "")).netloc.lstrip("www.")
        if site_domain and site_domain in domain:
            return name
    parts = domain.split(".")
    return parts[-2] if len(parts) >= 2 else parts[0]


def _extract_company_name(html: str, url: str, site_cfg: dict | None) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if site_cfg:
        sel = (site_cfg.get("selectors") or {}).get("company_name")
        if sel:
            el = soup.select_one(sel)
            if el:
                return el.get_text(strip=True)
    title = soup.find("title")
    if title:
        name = title.get_text(strip=True).split("|")[0].split("–")[0].strip()
        if name:
            return name
    return urlparse(url).netloc.lstrip("www.").split(".")[0].capitalize()


def _extract_company_desc(html: str, site_cfg: dict | None) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if site_cfg:
        sel = (site_cfg.get("selectors") or {}).get("company_desc")
        if sel:
            el = soup.select_one(sel)
            if el:
                return el.get_text(strip=True)[:300]
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return meta["content"][:300]
    return ""


def _infer_niche(html: str) -> str:
    text = BeautifulSoup(html, "html.parser").get_text(" ").lower()
    niches = {
        "fintech":    ["fintech", "payment", "banking", "financial"],
        "healthtech": ["health", "medical", "clinic", "hospital", "pharma"],
        "edtech":     ["education", "learning", "school", "university", "edtech"],
        "saas":       ["saas", "software as a service", "cloud platform"],
        "ecommerce":  ["ecommerce", "e-commerce", "shop", "marketplace", "retail"],
        "logistics":  ["logistics", "supply chain", "shipping", "delivery"],
        "devtools":   ["developer tools", "devtools", "api", "sdk", "open source"],
    }
    for niche, signals in niches.items():
        if any(s in text for s in signals):
            return niche
    return "technology"


# ── Queue helpers ─────────────────────────────────────────────────────────────

_MIN_URL_BUFFER  = 5
_MIN_LEAD_BUFFER = 3
_NORMALISE_BATCH = 5


async def _buffer_get(
    queue: asyncio.Queue,
    min_buffer: int,
    upstream_done: asyncio.Event,
) -> dict | None:
    while True:
        if upstream_done.is_set():
            if queue.qsize() > 0:
                return queue.get_nowait()
            return None
        if queue.qsize() > min_buffer:
            return queue.get_nowait()
        await asyncio.sleep(0.5)


async def _wait_queue_nonempty(queue: asyncio.Queue, upstream_done: asyncio.Event) -> None:
    """Returns once the queue has ≥ 1 item, or upstream has finished."""
    while queue.qsize() == 0 and not upstream_done.is_set():
        await asyncio.sleep(0.2)


async def _wait_event_or_done(event: asyncio.Event, done: asyncio.Event) -> None:
    """Returns once event fires, or done is set (upstream finished with nothing)."""
    while not event.is_set() and not done.is_set():
        await asyncio.sleep(0.2)


def _add_stats(total: dict, delta: dict) -> None:
    for k, v in delta.items():
        total[k] = total.get(k, 0) + v


# ── Stage 1: Discovery (multi-pass) ──────────────────────────────────────────
#
# Runs settings.MAX_PASSES rounds of discovery. Each pass starts immediately
# after the previous one finishes — while Stages 2-7 are still draining the
# URL/lead queues from the previous pass. s1_done is only set after ALL passes
# complete so Stage 2-4 keeps consuming without interruption.

async def _stage1(
    state: PipelineState,
    ui: PipelineUI,
    run_id: int,
    seen_domains: set,
    url_queue: asyncio.Queue,
    s1_done: asyncio.Event,
    disc_cfg: dict,
) -> None:
    s          = state.s1
    total_urls = 0
    pass_num   = 0

    # Loop passes until the daily email cap is reached — no fixed pass limit.
    # Between passes, DISCOVERY_PASS_DELAY seconds of cooldown let the search
    # engine recover from rate-limits and give downstream stages time to drain.
    while True:
        pass_num += 1

        if database.count_sent_today() >= settings.DAILY_EMAIL_CAP:
            logger.info("Daily cap reached — discovery stopping before pass %d.", pass_num)
            break

        state.pass_num = pass_num
        s.status  = RUNNING
        s.note    = f"engine: {settings.SEARCH_ENGINE.upper()} · Pass {pass_num}"
        s.current = ""
        ui.refresh()

        _prev_pass_total = total_urls

        def on_queries_ready(queries, _p=pass_num):
            s.stats["queries"] = s.stats.get("queries", 0) + len(queries)
            s.current = f"{len(queries)} queries loaded"
            ui.refresh()

        def on_query_start(query, idx, total, _p=pass_num):
            nonlocal _prev_pass_total
            _prev_pass_total = s.stats.get("urls", 0)
            s.current = query
            ui.refresh()

        def on_page_done(query, page, total_pages, total_found, _p=pass_num):
            s.stats["urls"] = total_urls + total_found
            ui.refresh()

        def on_query_done(query, total_found, _p=pass_num):
            nonlocal total_urls
            delta = s.stats.get("urls", 0) - _prev_pass_total
            s.add_item(f"[P{_p}] {query}", f"+{delta}")
            state.log_event("S1", f"[P{_p}] {query}", f"+{delta}")
            s.stats["urls"] = total_urls + total_found
            s.current = ""
            ui.refresh()

        await discovery.discover_urls(
            run_id, seen_domains, url_queue,
            asyncio.Event(),
            on_queries_ready=on_queries_ready,
            on_query_start=on_query_start,
            on_page_done=on_page_done,
            on_query_done=on_query_done,
            put_sentinel=False,
        )

        total_urls = s.stats.get("urls", total_urls)
        logger.info("Discovery pass %d done. URLs so far: %d", pass_num, total_urls)

        if database.count_sent_today() >= settings.DAILY_EMAIL_CAP:
            logger.info("Daily cap reached after pass %d — stopping discovery.", pass_num)
            break

        # Cool-down between passes: let rate-limits recover + downstream drain
        delay = settings.DISCOVERY_PASS_DELAY
        s.note    = f"Pass {pass_num} done · cooling down {delay}s…"
        s.current = ""
        ui.refresh()
        await asyncio.sleep(delay)

    s.status  = DONE
    s.current = ""
    s.note    = f"{pass_num} pass{'es' if pass_num > 1 else ''} done · {total_urls} URLs total"
    ui.refresh()
    s1_done.set()
    await url_queue.put(None)   # sentinel: Stage 2-4 drains then stops


# ── Stages 2–4: Scraping + Email Extraction ───────────────────────────────────

async def _stage24(
    state: PipelineState,
    ui: PipelineUI,
    url_queue: asyncio.Queue,
    s1_done: asyncio.Event,
    lead_queue: asyncio.Queue,
    s4_done: asyncio.Event,
    known: dict,
) -> None:
    s        = state.s24
    s.status = RUNNING
    count    = 0

    while True:
        item = await _buffer_get(url_queue, _MIN_URL_BUFFER, s1_done)
        if item is None:
            break

        url      = item["url"]
        domain   = item["domain"]
        site_cfg = _get_known_site_config(domain, known)
        count   += 1

        s.current           = domain
        s.stats["scraped"]  = count
        ui.refresh()

        html, track = await router.route(url)

        if track == "auth_required":
            s.note = f"⚠ auth: {domain}"
            ui.refresh()
            site_name   = _get_site_name(domain, known)
            profile_dir = os.path.join(settings.BROWSER_PROFILES_DIR, site_name)
            resolved    = await auth_bootstrap.handle_auth_site(site_name, url, profile_dir, ui)
            s.note = ""
            if resolved:
                html, track = await router.route(url, profile_dir)
            else:
                database.update_discovered_url_status(domain, "skipped")
                continue

        if not html:
            s.add_item(domain, "no content")
            state.log_event("S2-4", domain, "no content")
            database.update_discovered_url_status(domain, "failed")
            ui.refresh()
            continue

        emails = await email_extractor.extract_emails(url, html)
        if not emails:
            s.add_item(domain, "no emails")
            state.log_event("S2-4", domain, "no emails")
            database.update_discovered_url_status(domain, "scraped")
            ui.refresh()
            continue

        company_name = _extract_company_name(html, url, site_cfg)
        company_desc = _extract_company_desc(html, site_cfg)
        niche        = _infer_niche(html)
        n            = len(emails)

        s.add_item(company_name, f"{n} email{'s' if n > 1 else ''}")
        state.log_event("S2-4", company_name, f"{n} email{'s' if n > 1 else ''}")
        s.stats["emails"] = s.stats.get("emails", 0) + n
        database.update_discovered_url_status(domain, "scraped")
        ui.refresh()

        for email in emails:
            await lead_queue.put({
                "company":      company_name,
                "email":        email,
                "source_url":   url,
                "company_desc": company_desc,
                "niche":        niche,
            })

    s.status  = DONE
    s.current = ""
    ui.refresh()
    s4_done.set()
    lead_queue.put_nowait(None)


# ── Stage 5: Normalisation ────────────────────────────────────────────────────

async def _stage5(
    state: PipelineState,
    ui: PipelineUI,
    lead_queue: asyncio.Queue,
    s4_done: asyncio.Event,
    s5_done: asyncio.Event,
    s5_first_stored: asyncio.Event | None = None,
) -> None:
    s        = state.s5
    s.status = RUNNING
    batch: list[dict] = []
    total: dict       = {}

    while True:
        item = await _buffer_get(lead_queue, _MIN_LEAD_BUFFER, s4_done)
        if item is None:
            if batch:
                _add_stats(total, await asyncio.to_thread(normalizer.normalize_and_store, batch))
            break

        batch.append(item)
        s.current = item.get("company", "")
        s.stats["queued"] = s.stats.get("queued", 0) + 1
        ui.refresh()

        if len(batch) >= _NORMALISE_BATCH:
            res = await asyncio.to_thread(normalizer.normalize_and_store, batch.copy())
            _add_stats(total, res)
            n_stored = res.get("stored", 0)
            if n_stored:
                state.log_event("S5", "batch stored", f"+{n_stored}")
                if s5_first_stored and not s5_first_stored.is_set():
                    s5_first_stored.set()
            batch.clear()
            s.stats["stored"] = total.get("stored", 0)
            s.stats["dups"]   = total.get("skipped_duplicate", 0)
            ui.refresh()

    s.status  = DONE
    s.current = ""
    s.stats   = {
        "stored": total.get("stored", 0),
        "dups":   total.get("skipped_duplicate", 0),
    }
    ui.refresh()
    s5_done.set()


# ── Stage 6: Copywriting ──────────────────────────────────────────────────────

async def _stage6(
    state: PipelineState,
    ui: PipelineUI,
    s5_done: asyncio.Event,
    s6_done: asyncio.Event,
    s6_first_draft: asyncio.Event | None = None,
) -> None:
    s             = state.s6
    s.status      = RUNNING
    drafted_total = 0
    flagged_total = 0

    def on_draft(company: str, subject: str) -> None:
        nonlocal drafted_total
        state.log_event("S6", company, "drafted")
        state.s6.current = company
        if s6_first_draft and not s6_first_draft.is_set():
            s6_first_draft.set()
        ui.refresh()

    while True:
        stats = await asyncio.to_thread(copywriter.run_copywriting, on_draft)
        if stats["drafted"] > 0:
            drafted_total      += stats["drafted"]
            s.stats["drafted"]  = drafted_total
        if stats.get("flagged", 0) > 0:
            flagged_total      += stats["flagged"]
            s.stats["flagged"]  = flagged_total

        s.current = f"{drafted_total} drafted"
        ui.refresh()

        if s5_done.is_set():
            remaining = await asyncio.to_thread(database.get_leads_by_status, "ready")
            if not remaining:
                break
        await asyncio.sleep(3.0)

    s.status  = DONE
    s.current = ""
    ui.refresh()
    s6_done.set()


# ── Stage 7: Dispatch ─────────────────────────────────────────────────────────

async def _stage7(
    state: PipelineState,
    ui: PipelineUI,
    s6_done: asyncio.Event,
) -> None:
    """
    Polls for drafted emails and dispatches them in batches.
    Runs concurrently with S6 — starts as soon as the first email is drafted,
    continues until the daily cap is reached or the pipeline drains completely.
    """
    s = state.s7
    s.status  = RUNNING
    s.current = "waiting for drafts…"
    ui.refresh()

    total_skipped = 0

    def on_progress(sent: int, cap: int,
                    recipient: str = "", company: str = "") -> None:
        s.stats["sent"] = sent
        s.stats["cap"]  = cap
        s.current       = f"{sent} / {cap} sent"
        if recipient:
            s.add_item(recipient, "sent ✓")
            state.log_event("S7", recipient, f"sent ({company[:18]})" if company else "sent ✓")
        ui.refresh()

    while True:
        drafted = await asyncio.to_thread(database.get_drafted_emails)

        if drafted:
            s.current = f"dispatching {len(drafted)} email(s)…"
            ui.refresh()
            stats = await dispatcher.run_dispatch(progress_callback=on_progress)
            total_skipped += stats.get("skipped", 0)
            s.stats = {
                "sent":    database.count_sent_today(),
                "skipped": total_skipped,
            }
            if stats.get("halted"):
                s.note   = "⚠ session expired — run --setup-sender"
                s.status = ERROR
                ui.refresh()
                break
            if database.count_sent_today() >= settings.DAILY_EMAIL_CAP:
                s.note = "daily cap reached ✓"
                break

        # Stop when S6 is fully done and nothing left to send
        if s6_done.is_set():
            remaining = await asyncio.to_thread(database.get_drafted_emails)
            if not remaining:
                break

        await asyncio.sleep(30.0)

    s.status  = DONE
    s.current = ""
    ui.refresh()


# ── Run modes ──────────────────────────────────────────────────────────────────

async def cmd_run() -> None:
    await auth_bootstrap.handle_pending_auth_sites()

    run_id       = database.start_run()
    seen_domains = database.load_seen_domains()
    cfg          = yaml.safe_load(Path(settings.TARGETS_YAML).read_text())
    known        = cfg.get("known_sites", {})
    disc_cfg     = cfg.get("discovery", {})

    url_queue  = asyncio.Queue(maxsize=50)
    lead_queue = asyncio.Queue(maxsize=20)
    s1_done    = asyncio.Event()
    s4_done    = asyncio.Event()
    s5_done    = asyncio.Event()
    s6_done    = asyncio.Event()

    # Cascade trigger events — each fires when a stage produces its first output
    s5_first_stored = asyncio.Event()
    s6_first_draft  = asyncio.Event()

    state = PipelineState(pass_num=1, run_id=run_id)
    state.url_queue      = url_queue
    state.lead_queue     = lead_queue
    state.url_queue_cap  = 50
    state.lead_queue_cap = 20

    async with PipelineUI(state) as ui:
        # ── Cascade startup ───────────────────────────────────────────────────
        # Each stage is created only after its predecessor puts data in the
        # shared buffer. Once started, all active stages run concurrently.

        # S1: start immediately
        t1 = asyncio.create_task(
            _stage1(state, ui, run_id, seen_domains, url_queue, s1_done, disc_cfg)
        )

        # S2-4: start when S1 puts first URL in url_queue
        await _wait_queue_nonempty(url_queue, s1_done)
        t24 = asyncio.create_task(
            _stage24(state, ui, url_queue, s1_done, lead_queue, s4_done, known)
        )

        # S5: start when S2-4 puts first lead in lead_queue
        await _wait_queue_nonempty(lead_queue, s4_done)
        t5 = asyncio.create_task(
            _stage5(state, ui, lead_queue, s4_done, s5_done, s5_first_stored)
        )

        # S6: start when S5 stores first lead to DB (ready table)
        await _wait_event_or_done(s5_first_stored, s5_done)
        t6 = asyncio.create_task(
            _stage6(state, ui, s5_done, s6_done, s6_first_draft)
        )

        # S7: start when S6 drafts first email (drafted table)
        await _wait_event_or_done(s6_first_draft, s6_done)
        t7 = asyncio.create_task(
            _stage7(state, ui, s6_done)
        )

        await asyncio.gather(t1, t24, t5, t6, t7)

    database.finish_run(run_id)
    console.rule("[bold green]Run Complete[/bold green]")

    if database.count_sent_today() >= settings.DAILY_EMAIL_CAP:
        console.print("\n  [bold yellow]Daily cap reached. Shutting down.[/bold yellow]")
        sys.exit(0)


async def cmd_discover() -> None:
    await auth_bootstrap.handle_pending_auth_sites()

    run_id       = database.start_run()
    seen_domains = database.load_seen_domains()
    cfg          = yaml.safe_load(Path(settings.TARGETS_YAML).read_text())
    known        = cfg.get("known_sites", {})
    disc_cfg     = cfg.get("discovery", {})

    url_queue  = asyncio.Queue(maxsize=50)
    lead_queue = asyncio.Queue(maxsize=20)
    s1_done    = asyncio.Event()
    s4_done    = asyncio.Event()
    s5_done    = asyncio.Event()

    state = PipelineState(pass_num=1, run_id=run_id)
    state.url_queue      = url_queue
    state.lead_queue     = lead_queue
    state.url_queue_cap  = 50
    state.lead_queue_cap = 20

    async with PipelineUI(state) as ui:
        t1 = asyncio.create_task(
            _stage1(state, ui, run_id, seen_domains, url_queue, s1_done, disc_cfg)
        )
        await _wait_queue_nonempty(url_queue, s1_done)
        t24 = asyncio.create_task(
            _stage24(state, ui, url_queue, s1_done, lead_queue, s4_done, known)
        )
        await _wait_queue_nonempty(lead_queue, s4_done)
        t5 = asyncio.create_task(
            _stage5(state, ui, lead_queue, s4_done, s5_done)
        )
        await asyncio.gather(t1, t24, t5)

    database.finish_run(run_id)
    console.rule("[bold green]Discovery Complete[/bold green]")


def cmd_write() -> None:
    console.rule("[cyan]Copywriting[/cyan]")
    stats = copywriter.run_copywriting()
    console.print(f"  Drafted: {stats['drafted']}  Flagged: {stats['flagged']}")


async def cmd_send() -> None:
    console.rule("[cyan]Dispatch[/cyan]")
    stats = await dispatcher.run_dispatch()
    console.print(f"  Sent: {stats['sent']}  Skipped: {stats['skipped']}")
    if stats.get("halted"):
        console.print("  [red]Dispatcher halted — run: python main.py --setup-sender[/red]")


def cmd_summary() -> None:
    dashboard.render_snapshot()


def cmd_dashboard() -> None:
    dashboard.render_live()


async def cmd_setup_sender() -> None:
    console.rule("[cyan]Outlook Sender Setup[/cyan]")
    try:
        await dispatcher.ensure_session()
        console.print("  Run [bold]python main.py --run[/bold] to start the pipeline.\n")
    except Exception as exc:
        console.print(f"  [red]Setup failed:[/red] {exc}\n")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    _setup_logging()
    database.init_db(settings.DB_PATH)
    _check_first_boot()

    parser = argparse.ArgumentParser(
        prog="scomp",
        description="S.C.O.M.P  —  Stealth Collection, Outreach & Messaging Pipeline",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run",       action="store_true", help="Full pipeline end to end")
    group.add_argument("--discover",  action="store_true", help="Discovery + scraping + normalisation")
    group.add_argument("--write",     action="store_true", help="Copywriting for ready leads")
    group.add_argument("--send",      action="store_true", help="Dispatch drafted emails")
    group.add_argument("--dashboard", action="store_true", help="Live terminal dashboard")
    group.add_argument("--summary",   action="store_true", help="Today's run snapshot")
    group.add_argument("--setup",         action="store_true", help="Re-run first-boot setup wizard")
    group.add_argument("--setup-sender",  action="store_true",
                       help="One-time Outlook Web login — saves session for silent dispatch")
    args = parser.parse_args()

    if args.run:
        asyncio.run(cmd_run())
    elif args.discover:
        asyncio.run(cmd_discover())
    elif args.write:
        cmd_write()
    elif args.send:
        asyncio.run(cmd_send())
    elif args.dashboard:
        cmd_dashboard()
    elif args.summary:
        cmd_summary()
    elif args.setup:
        lock = Path(settings.BASE_DIR) / ".autostart_asked"
        if lock.exists():
            lock.unlink()
        _check_first_boot()
    elif args.setup_sender:
        asyncio.run(cmd_setup_sender())


if __name__ == "__main__":
    main()
