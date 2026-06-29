"""
S.C.O.M.P  —  Stealth Collection, Outreach & Messaging Pipeline
Entry point and pipeline orchestrator.

Usage:
  python main.py --run          Full pipeline end to end
  python main.py --discover     Discovery + scraping only
  python main.py --write        Copywriting only
  python main.py --send         Dispatch only
  python main.py --dashboard    Live terminal dashboard
  python main.py --summary      Print today's run summary
  python main.py --setup        First-time setup wizard
"""

import argparse
import asyncio
import logging
import os
import platform
import subprocess
import sys
import threading
from pathlib import Path

import yaml
from bs4 import BeautifulSoup
from rich.console import Console
from rich.prompt import Confirm, Prompt

# ── Bootstrap path so sub-packages can find each other ───────────────────────
sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from db import database
from pipeline import copywriter, dispatcher, normalizer
from scraper import auth_bootstrap, discovery, email_extractor, router
from ui import dashboard

console = Console()

# ── Logging setup ─────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    os.makedirs(settings.LOG_DIR, exist_ok=True)
    log_file = os.path.join(settings.LOG_DIR, "scomp.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )

logger = logging.getLogger("scomp.main")


# ── First-boot autostart ───────────────────────────────────────────────────────

def _check_first_boot() -> None:
    """
    On first run after machine reboot (detected via absence of a lock file),
    ask the user whether to register SCOMP as a startup service.
    """
    lock = Path(settings.BASE_DIR) / ".autostart_asked"
    if lock.exists():
        return

    lock.touch()
    console.print()
    console.rule("[bold cyan]S.C.O.M.P First-Boot Setup[/bold cyan]")
    console.print()
    console.print(
        "  SCOMP detected this is the first run after initial setup.\n"
        "  It can be registered to start automatically when your machine boots."
    )
    console.print()

    if Confirm.ask("  Register SCOMP to auto-start on system boot?", default=False):
        _register_autostart()
    else:
        console.print("  [dim]Skipped. Run [bold]python main.py --setup[/bold] anytime to configure this.[/dim]")
    console.print()


def _register_autostart() -> None:
    system = platform.system()
    base   = str(Path(__file__).parent)
    python = sys.executable

    if system == "Linux":
        service = f"""[Unit]
Description=S.C.O.M.P Outreach Pipeline
After=network.target

[Service]
Type=simple
WorkingDirectory={base}
ExecStart={python} {base}/main.py --run
Restart=no
StandardOutput=append:{settings.LOG_DIR}/scomp.log
StandardError=append:{settings.LOG_DIR}/scomp.log

[Install]
WantedBy=default.target
"""
        service_path = Path.home() / ".config/systemd/user/scomp.service"
        service_path.parent.mkdir(parents=True, exist_ok=True)
        service_path.write_text(service)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "scomp.service"], check=False)
        console.print("  [green]Systemd user service installed.[/green]")
        console.print(f"  [dim]File: {service_path}[/dim]")

    elif system == "Darwin":
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>          <string>com.scomp.pipeline</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>{base}/main.py</string>
    <string>--run</string>
  </array>
  <key>WorkingDirectory</key> <string>{base}</string>
  <key>RunAtLoad</key>        <true/>
  <key>StandardOutPath</key>  <string>{settings.LOG_DIR}/scomp.log</string>
  <key>StandardErrorPath</key><string>{settings.LOG_DIR}/scomp.log</string>
</dict>
</plist>"""
        plist_path = Path.home() / "Library/LaunchAgents/com.scomp.pipeline.plist"
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(plist)
        subprocess.run(["launchctl", "load", str(plist_path)], check=False)
        console.print("  [green]LaunchAgent plist installed.[/green]")

    elif system == "Windows":
        task_cmd = (
            f'schtasks /create /tn "SCOMP" /tr "{python} {base}\\main.py --run" '
            f'/sc ONLOGON /rl HIGHEST /f'
        )
        subprocess.run(task_cmd, shell=True, check=False)
        console.print("  [green]Windows Task Scheduler entry created.[/green]")

    else:
        console.print("  [yellow]Unsupported OS — add SCOMP to your startup manually.[/yellow]")


# ── Pipeline stages ────────────────────────────────────────────────────────────

async def _run_discovery_and_scraping() -> list[dict]:
    """Stage 1–4: discover URLs, scrape, extract emails. Returns raw lead dicts."""
    console.rule("[cyan]Stage 1 — Discovery[/cyan]")
    urls = await discovery.discover_urls()
    console.print(f"  [green]{len(urls)} URLs discovered.[/green]")

    raw_leads: list[dict] = []
    cfg = yaml.safe_load(Path(settings.TARGETS_YAML).read_text())
    known = cfg.get("known_sites", {})

    console.rule("[cyan]Stage 2–4 — Scraping + Email Extraction[/cyan]")
    total = len(urls)

    for i, entry in enumerate(urls, 1):
        url       = entry["url"]
        domain    = entry["domain"]
        url_type  = entry["url_type"]

        console.print(f"  [{i}/{total}] {domain}", end="\r")

        # Check if this is a known site that requires auth
        site_config = _match_known_site(domain, known)
        profile_dir: str | None = None
        if site_config and site_config.get("requires_auth"):
            site_name  = _find_site_name(domain, known)
            profile_dir = os.path.join(settings.BROWSER_PROFILES_DIR, site_name)
            resolved = await auth_bootstrap.handle_auth_site(site_name, url, profile_dir)
            if not resolved:
                continue

        html, track = await router.route(url, profile_dir)

        if track == "auth_required":
            # Discover which site name this belongs to
            site_name  = _find_site_name(domain, known) or domain.split(".")[0]
            profile_dir = os.path.join(settings.BROWSER_PROFILES_DIR, site_name)
            resolved = await auth_bootstrap.handle_auth_site(site_name, url, profile_dir)
            if resolved:
                html, track = await router.route(url, profile_dir)

        if not html:
            continue

        emails = await email_extractor.extract_emails(url, html)
        if not emails:
            continue

        # Extract company name from HTML
        company_name = _extract_company_name(html, url, site_config)
        company_desc = _extract_company_desc(html, site_config)
        niche        = _infer_niche(html)

        for email in emails:
            raw_leads.append({
                "company":      company_name,
                "email":        email,
                "source_url":   url,
                "company_desc": company_desc,
                "niche":        niche,
            })

    console.print()
    console.print(f"  [green]{len(raw_leads)} raw lead records extracted.[/green]")
    return raw_leads


def _match_known_site(domain: str, known: dict) -> dict | None:
    for site_cfg in known.values():
        from urllib.parse import urlparse
        site_domain = urlparse(site_cfg.get("base_url", "")).netloc.lstrip("www.")
        if site_domain and site_domain in domain:
            return site_cfg
    return None


def _find_site_name(domain: str, known: dict) -> str:
    from urllib.parse import urlparse
    for name, site_cfg in known.items():
        site_domain = urlparse(site_cfg.get("base_url", "")).netloc.lstrip("www.")
        if site_domain and site_domain in domain:
            return name
    return domain.split(".")[0]


def _extract_company_name(html: str, url: str, site_cfg: dict | None) -> str:
    soup = BeautifulSoup(html, "html.parser")
    # Try site-specific selector first
    if site_cfg:
        sel = (site_cfg.get("selectors") or {}).get("company_name")
        if sel:
            el = soup.select_one(sel)
            if el:
                return el.get_text(strip=True)
    # Fallback: <title> tag
    title = soup.find("title")
    if title:
        name = title.get_text(strip=True).split("|")[0].split("–")[0].strip()
        if name:
            return name
    from urllib.parse import urlparse
    return urlparse(url).netloc.lstrip("www.").split(".")[0].capitalize()


def _extract_company_desc(html: str, site_cfg: dict | None) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if site_cfg:
        sel = (site_cfg.get("selectors") or {}).get("company_desc")
        if sel:
            el = soup.select_one(sel)
            if el:
                return el.get_text(strip=True)[:300]
    # Fallback: meta description
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return meta["content"][:300]
    return ""


def _infer_niche(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ").lower()
    niches = {
        "fintech":   ["fintech", "payment", "banking", "financial"],
        "healthtech":["health", "medical", "clinic", "hospital", "pharma"],
        "edtech":    ["education", "learning", "school", "university", "edtech"],
        "saas":      ["saas", "software as a service", "cloud platform"],
        "ecommerce": ["ecommerce", "e-commerce", "shop", "marketplace", "retail"],
        "logistics": ["logistics", "supply chain", "shipping", "delivery"],
        "devtools":  ["developer tools", "devtools", "api", "sdk", "open source"],
    }
    for niche, signals in niches.items():
        if any(s in text for s in signals):
            return niche
    return "technology"


# ── Run modes ──────────────────────────────────────────────────────────────────

async def cmd_run() -> None:
    run_id = database.start_run()
    stats  = {"leads_attempted": 0, "emails_sent": 0, "emails_skipped": 0, "emails_flagged": 0}

    # Stage 1–4
    raw_leads = await _run_discovery_and_scraping()
    stats["leads_attempted"] = len(raw_leads)

    # Stage 5 — Normalise
    console.rule("[cyan]Stage 5 — Normalisation[/cyan]")
    norm_stats = normalizer.normalize_and_store(raw_leads)
    console.print(f"  Stored: {norm_stats['stored']}  Duplicates: {norm_stats['skipped_duplicate']}  "
                  f"Invalid: {norm_stats['skipped_invalid']}  Manual: {norm_stats['flagged_manual']}")

    # Stage 6 — Copywriting
    console.rule("[cyan]Stage 6 — Copywriting[/cyan]")
    copy_stats = copywriter.run_copywriting()
    stats["emails_flagged"] = copy_stats["flagged"]
    console.print(f"  Drafted: {copy_stats['drafted']}  Flagged: {copy_stats['flagged']}")

    # Stage 7 — Dispatch
    console.rule("[cyan]Stage 7 — Dispatch[/cyan]")
    dispatch_stats = dispatcher.run_dispatch(
        progress_callback=lambda sent, cap: console.print(
            f"  Sent {sent}/{cap}", end="\r"
        )
    )
    stats["emails_sent"]    = dispatch_stats["sent"]
    stats["emails_skipped"] = dispatch_stats["skipped"]
    console.print()

    database.finish_run(run_id, **stats)

    console.rule("[bold green]Run Complete[/bold green]")
    console.print(f"  Sent: {stats['emails_sent']}  Skipped: {stats['emails_skipped']}  "
                  f"Flagged: {stats['emails_flagged']}")

    # If daily cap is reached, signal container exit
    if database.count_sent_today() >= settings.DAILY_EMAIL_CAP:
        console.print("\n  [bold yellow]Daily cap reached. Shutting down.[/bold yellow]")
        sys.exit(0)


async def cmd_discover() -> None:
    raw_leads = await _run_discovery_and_scraping()
    console.rule("[cyan]Stage 5 — Normalisation[/cyan]")
    stats = normalizer.normalize_and_store(raw_leads)
    console.print(f"  Stored: {stats['stored']}  Duplicates: {stats['skipped_duplicate']}  "
                  f"Invalid: {stats['skipped_invalid']}")


def cmd_write() -> None:
    console.rule("[cyan]Copywriting[/cyan]")
    stats = copywriter.run_copywriting()
    console.print(f"  Drafted: {stats['drafted']}  Flagged: {stats['flagged']}")


def cmd_send() -> None:
    console.rule("[cyan]Dispatch[/cyan]")
    stats = dispatcher.run_dispatch()
    console.print(f"  Sent: {stats['sent']}  Skipped: {stats['skipped']}")
    if stats.get("halted"):
        console.print("  [red]Dispatcher halted — check SMTP credentials.[/red]")


def cmd_summary() -> None:
    dashboard.render_snapshot()


def cmd_dashboard() -> None:
    dashboard.render_live()


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
    group.add_argument("--discover",  action="store_true", help="Discovery + scraping only")
    group.add_argument("--write",     action="store_true", help="Copywriting only")
    group.add_argument("--send",      action="store_true", help="Dispatch only")
    group.add_argument("--dashboard", action="store_true", help="Live terminal dashboard")
    group.add_argument("--summary",   action="store_true", help="Print today's run summary")
    group.add_argument("--setup",     action="store_true", help="Run first-time setup wizard")
    args = parser.parse_args()

    if args.run:
        asyncio.run(cmd_run())
    elif args.discover:
        asyncio.run(cmd_discover())
    elif args.write:
        cmd_write()
    elif args.send:
        cmd_send()
    elif args.dashboard:
        cmd_dashboard()
    elif args.summary:
        cmd_summary()
    elif args.setup:
        # Reset the autostart lock to re-run the wizard
        lock = Path(settings.BASE_DIR) / ".autostart_asked"
        if lock.exists():
            lock.unlink()
        _check_first_boot()


if __name__ == "__main__":
    main()
