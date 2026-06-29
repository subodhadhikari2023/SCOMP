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
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from db import database
from pipeline import copywriter, dispatcher, normalizer
from scraper import auth_bootstrap, discovery, email_extractor, router
from ui import dashboard

console = Console()


# ── Logging ───────────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    os.makedirs(settings.LOG_DIR, exist_ok=True)
    # All detail goes to the log file; terminal output is handled by Rich UI only
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        handlers=[
            logging.FileHandler(os.path.join(settings.LOG_DIR, "scomp.log")),
        ],
    )
    for lib in ("httpx", "httpcore", "playwright", "google_genai", "asyncio"):
        logging.getLogger(lib).setLevel(logging.WARNING)

logger = logging.getLogger("scomp.main")


# ── First-boot autostart ───────────────────────────────────────────────────────

def _check_first_boot() -> None:
    lock = Path(settings.BASE_DIR) / ".autostart_asked"
    if lock.exists():
        return
    lock.touch()

    if not sys.stdin.isatty():
        return  # non-interactive context (piped, CI, Docker) — skip prompt silently

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
    """Returns the YAML config block for a domain if it is a known site."""
    for cfg in known.values():
        site_domain = urlparse(cfg.get("base_url", "")).netloc.lstrip("www.")
        if site_domain and site_domain in domain:
            return cfg
    return None


def _get_site_name(domain: str, known: dict) -> str:
    """Returns the known site name or derives it from the domain."""
    for name, cfg in known.items():
        site_domain = urlparse(cfg.get("base_url", "")).netloc.lstrip("www.")
        if site_domain and site_domain in domain:
            return name
    # Auto-derive: wellfound.com → wellfound, jobs.lever.co → lever
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


# ── Pipeline stages ────────────────────────────────────────────────────────────

def _discovery_panel(
    completed: list,
    current_query: str,
    current_page: int,
    total_pages: int,
    total_queries: int,
    total_urls: int,
    engine: str,
) -> Panel:
    """Builds the live Stage 1 panel renderable."""
    rows = Table.grid(padding=(0, 1))
    rows.add_column(width=2)
    rows.add_column(min_width=56, max_width=56)
    rows.add_column(width=12, justify="right")

    for q, count in completed[-12:]:
        label = (q[:54] + "..") if len(q) > 56 else q
        rows.add_row(
            "[green]✓[/green]",
            f"[dim]{label}[/dim]",
            f"[green]{count}[/green] [dim]urls[/dim]",
        )

    if current_query:
        label = (current_query[:54] + "..") if len(current_query) > 56 else current_query
        filled = current_page
        empty  = total_pages - current_page
        bar    = f"[cyan]{'█' * filled}[/cyan][dim]{'░' * empty}[/dim]"
        rows.add_row("[yellow]▶[/yellow]", f"[white]{label}[/white]", bar)

    return Panel(
        rows,
        title="[bold white]  Stage 1  ·  Discovery  [/bold white]",
        subtitle=(
            f"[dim]engine[/dim] [bold cyan]{engine.upper()}[/bold cyan]"
            f"[dim]  ·  {len(completed)}/{total_queries} queries  ·  [/dim]"
            f"[bold green]{total_urls}[/bold green][dim] urls found[/dim]"
        ),
        border_style="cyan",
        padding=(1, 2),
    )


async def _run_discovery_and_scraping(run_id: int) -> list[dict]:
    seen_domains: set = database.load_seen_domains()
    cfg        = yaml.safe_load(Path(settings.TARGETS_YAML).read_text())
    known      = cfg.get("known_sites", {})
    disc_cfg   = cfg.get("discovery", {})
    total_q    = len(disc_cfg.get("search_queries", []))
    max_pages  = disc_cfg.get("max_pages_per_query", 5)

    # ── Stage 1: Discovery ────────────────────────────────────────────────────
    state = dict(completed=[], current="", page=0, prev_total=0, total=0)

    def _panel():
        return _discovery_panel(
            state["completed"], state["current"], state["page"],
            max_pages, total_q, state["total"], settings.SEARCH_ENGINE,
        )

    def on_query_start(query, idx, total):
        state["current"]    = query
        state["page"]       = 0
        state["prev_total"] = state["total"]
        live.update(_panel())

    def on_page_done(query, page, total_pages, total_urls):
        state["page"]  = page + 1
        state["total"] = total_urls
        live.update(_panel())

    def on_query_done(query, total_urls):
        state["completed"].append((query, total_urls - state["prev_total"]))
        state["current"] = ""
        state["total"]   = total_urls
        live.update(_panel())

    console.print()
    with Live(_panel(), console=console, refresh_per_second=8) as live:
        urls = await discovery.discover_urls(
            run_id, seen_domains,
            on_query_start=on_query_start,
            on_page_done=on_page_done,
            on_query_done=on_query_done,
        )
        state["current"] = ""
        live.update(_panel())
    console.print()

    if not urls:
        console.print("  [yellow]No new URLs discovered.[/yellow]\n")
        return []

    # ── Stages 2–4: Scraping + Email Extraction ───────────────────────────────
    console.print(Panel(
        f"  [dim]Processing[/dim] [bold white]{len(urls)}[/bold white] [dim]URLs[/dim]  ",
        title="[bold white]  Stages 2–4  ·  Scraping  ·  Extraction  [/bold white]",
        border_style="cyan",
        padding=(0, 2),
    ))
    console.print()

    raw_leads: list[dict] = []
    dom_w = 44

    for i, entry in enumerate(urls, 1):
        url      = entry["url"]
        domain   = entry["domain"]
        site_cfg = _get_known_site_config(domain, known)
        prefix   = f"  [dim]{i:>3}/{len(urls)}[/dim]  [white]{domain[:dom_w]:<{dom_w}}[/white]"

        html, track = await router.route(url)

        if track == "auth_required":
            console.print(f"{prefix}  [yellow]⚠  auth required[/yellow]")
            site_name   = _get_site_name(domain, known)
            profile_dir = os.path.join(settings.BROWSER_PROFILES_DIR, site_name)
            resolved    = await auth_bootstrap.handle_auth_site(site_name, url, profile_dir)
            if resolved:
                html, track = await router.route(url, profile_dir)
            else:
                database.update_discovered_url_status(domain, "skipped")
                continue

        if not html:
            console.print(f"{prefix}  [dim]✗  no content[/dim]")
            database.update_discovered_url_status(domain, "failed")
            continue

        emails = await email_extractor.extract_emails(url, html)
        if not emails:
            console.print(f"{prefix}  [dim]·  no emails[/dim]")
            database.update_discovered_url_status(domain, "scraped")
            continue

        company_name = _extract_company_name(html, url, site_cfg)
        company_desc = _extract_company_desc(html, site_cfg)
        niche        = _infer_niche(html)
        n            = len(emails)

        console.print(
            f"{prefix}  [green]✓  {n} email{'s' if n > 1 else ''}[/green]"
            f"  [dim]·  {company_name[:28]}[/dim]"
        )

        for email in emails:
            raw_leads.append({
                "company":      company_name,
                "email":        email,
                "source_url":   url,
                "company_desc": company_desc,
                "niche":        niche,
            })
        database.update_discovered_url_status(domain, "scraped")

    console.print()
    console.print(
        f"  [bold green]{len(raw_leads)}[/bold green] [dim]raw lead{'s' if len(raw_leads) != 1 else ''} extracted[/dim]\n"
    )
    return raw_leads


# ── Run modes ──────────────────────────────────────────────────────────────────

async def cmd_run() -> None:
    # Re-prompt any sites that timed out in previous runs
    await auth_bootstrap.handle_pending_auth_sites()

    run_id = database.start_run()
    stats  = {"leads_attempted": 0, "emails_sent": 0, "emails_skipped": 0, "emails_flagged": 0}

    raw_leads = await _run_discovery_and_scraping(run_id)
    stats["leads_attempted"] = len(raw_leads)

    console.rule("[cyan]Stage 5 — Normalisation[/cyan]")
    norm = normalizer.normalize_and_store(raw_leads)
    console.print(
        f"  Stored: {norm['stored']}  "
        f"Duplicates: {norm['skipped_duplicate']}  "
        f"Invalid: {norm['skipped_invalid']}  "
        f"Manual: {norm['flagged_manual']}"
    )

    console.rule("[cyan]Stage 6 — Copywriting[/cyan]")
    copy = copywriter.run_copywriting()
    stats["emails_flagged"] = copy["flagged"]
    console.print(f"  Drafted: {copy['drafted']}  Flagged: {copy['flagged']}")

    console.rule("[cyan]Stage 7 — Dispatch[/cyan]")
    dispatch = await dispatcher.run_dispatch(
        progress_callback=lambda sent, cap: console.print(f"  Sent {sent}/{cap}", end="\r")
    )
    stats["emails_sent"]    = dispatch["sent"]
    stats["emails_skipped"] = dispatch["skipped"]
    console.print()

    database.finish_run(run_id, **stats)
    console.rule("[bold green]Run Complete[/bold green]")
    console.print(
        f"  Sent: {stats['emails_sent']}  "
        f"Skipped: {stats['emails_skipped']}  "
        f"Flagged: {stats['emails_flagged']}"
    )

    if database.count_sent_today() >= settings.DAILY_EMAIL_CAP:
        console.print("\n  [bold yellow]Daily cap reached. Shutting down.[/bold yellow]")
        sys.exit(0)


async def cmd_discover() -> None:
    await auth_bootstrap.handle_pending_auth_sites()
    run_id    = database.start_run()
    raw_leads = await _run_discovery_and_scraping(run_id)
    console.rule("[cyan]Stage 5 — Normalisation[/cyan]")
    norm = normalizer.normalize_and_store(raw_leads)
    console.print(
        f"  Stored: {norm['stored']}  "
        f"Duplicates: {norm['skipped_duplicate']}  "
        f"Invalid: {norm['skipped_invalid']}"
    )


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
    """
    Explicit Outlook Web login — useful for refreshing an expired session.
    On first --run or --send this is triggered automatically; no need to call it manually.
    """
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
    group.add_argument("--setup-sender",  action="store_true", help="One-time Outlook Web login — saves session for silent dispatch")
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
