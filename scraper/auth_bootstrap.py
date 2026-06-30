"""
Auth bootstrap: terminal prompting for credentials + Playwright session save.

Skip logic
──────────
  Timeout or 's' → site queued in pending_auth_sites (attempts +1).
                   When attempts reaches 5, auto-permanently-skip (stop prompting).
  'n' / 'never'  → immediately permanently skip (user has no account there).
  'y'            → collect credentials, open browser, save session.
"""

import getpass
import logging
import os
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from config import settings
from db import database

if TYPE_CHECKING:
    from ui.dashboard import PipelineUI

logger  = logging.getLogger(__name__)
console = Console()

PERM_SKIP_AFTER = 5   # automatically permanent-skip after this many queued skips


# ── Internal helpers ──────────────────────────────────────────────────────────

def _derive_site_name(domain: str) -> str:
    parts = domain.rstrip(".").split(".")
    return parts[-2] if len(parts) >= 2 else parts[0]


def _prompt_panel(title: str, lines: list[str]) -> Panel:
    body = Text()
    for ln in lines:
        body.append(ln + "\n")
    return Panel(body, title=f"[bold yellow]{title}[/bold yellow]", border_style="yellow", padding=(0, 2))


def _plain_ask(prompt_lines: list[str], input_label: str = "  → ") -> str:
    """Show a Rich-styled prompt and read from stdin (blocking). No timeout."""
    console.print()
    console.print(_prompt_panel("⚡  Auth Required", prompt_lines))
    return input(input_label).strip().lower()


async def _async_ask(
    ui: "PipelineUI",
    prompt_lines: list[str],
    input_label: str = "  → ",
) -> str:
    """Route through PipelineUI.ask so the live dashboard pauses properly."""
    return await ui.ask(prompt_lines, input_label)


# ── Core handler ──────────────────────────────────────────────────────────────

async def handle_auth_site(
    site_name: str,
    site_url: str,
    profile_dir: str,
    ui: Optional["PipelineUI"] = None,
) -> bool:
    """
    Interactively handles an auth-required site discovered during scraping.
    Returns True if a usable session exists or was just created.
    On skip / timeout: queues in pending_auth_sites, returns False.
    On 'n': permanently skips the domain.
    """
    from urllib.parse import urlparse

    domain     = urlparse(site_url).netloc.lower().lstrip("www.")
    state_file = Path(profile_dir) / "state.json"

    if state_file.exists():
        logger.info("Saved session found for %s — reusing.", site_name)
        return True

    prompt_lines = [
        f"[bold]{site_name}[/bold]  ({domain})",
        "",
        "  This site requires a login to scrape job listings.",
        "",
        "  [bold white][ y ][/bold white]  Login now",
        "  [bold white][ s ][/bold white]  Skip this run",
        "  [bold white][ n ][/bold white]  Never ask again",
    ]

    if ui is not None:
        response = await _async_ask(ui, prompt_lines)
    else:
        response = _plain_ask(prompt_lines)

    if response in ("n", "no", "never"):
        database.add_skipped_site(domain, "user has no account")
        database.remove_pending_auth_site(domain)
        logger.info("Permanently skipping %s — user declined.", site_name)
        return False

    if response in ("y", "yes"):
        return await _collect_credentials_and_login(site_name, site_url, domain, profile_dir, ui)

    # 's' / 'skip' / timeout / anything else → queue for next run
    database.add_pending_auth_site(domain, site_url, site_name)
    new_attempts = database.get_pending_auth_site_attempts(domain)
    if new_attempts >= PERM_SKIP_AFTER:
        database.add_skipped_site(domain, f"auto-skipped after {new_attempts} skips")
        database.remove_pending_auth_site(domain)
        logger.info("Auto-permanently-skipping %s after %d skips.", site_name, new_attempts)
    else:
        logger.info("Skipping %s (%d/%d) — queued for next run.", site_name, new_attempts, PERM_SKIP_AFTER)
    return False


async def _collect_credentials_and_login(
    site_name: str,
    site_url: str,
    domain: str,
    profile_dir: str,
    ui: Optional["PipelineUI"] = None,
) -> bool:
    from scraper.heavy_track import fetch_html_visible

    env_key_email = f"{site_name.upper()}_EMAIL"
    env_key_pass  = f"{site_name.upper()}_PASSWORD"

    if ui is not None:
        email = await _async_ask(ui, [f"[bold]{site_name}[/bold] — enter your email address:"], "  Email → ")
        if not email:
            database.add_pending_auth_site(domain, site_url, site_name)
            return False
        # getpass doesn't work well through async; use sync input with echo-off note
        console.print("  [dim]Enter password (input hidden — type and press Enter):[/dim]")
        password = await __import__("asyncio").to_thread(getpass.getpass, "  Password → ")
    else:
        console.print()
        email = input(f"  Email for {site_name}: ").strip()
        if not email:
            database.add_pending_auth_site(domain, site_url, site_name)
            return False
        password = getpass.getpass(f"  Password for {site_name} (hidden): ")

    _append_env(env_key_email, email)
    _append_env(env_key_pass, password)

    console.print(f"\n  [cyan]Opening browser for {site_name}.[/cyan]")
    console.print("  Complete the login (including MFA if required), then close the tab.")
    console.print("  Session will be saved automatically.\n")

    try:
        await fetch_html_visible(site_url, profile_dir)
        database.remove_pending_auth_site(domain)
        logger.info("Session saved for %s at %s", site_name, profile_dir)
        return True
    except Exception as exc:
        logger.error("Session save failed for %s: %s", site_name, exc)
        database.add_pending_auth_site(domain, site_url, site_name)
        return False


# ── Pending auth re-prompting (called at run start, before pipeline UI) ────────

async def handle_pending_auth_sites() -> None:
    """
    Called at the beginning of every run, before the live dashboard starts.
    Uses plain console output (no PipelineUI needed).
    Sites skipped >= PERM_SKIP_AFTER total times are auto-permanently-skipped.
    """
    pending = database.get_pending_auth_sites()
    if not pending:
        return

    console.print()
    console.rule("[bold cyan]Pending Auth Sites[/bold cyan]")
    console.print(
        f"\n  [bold]{len(pending)}[/bold] site(s) need attention before this run starts.\n"
        "  These sites require login and were skipped in previous runs.\n"
    )

    for row in pending:
        domain    = row["domain"]
        site_url  = row["site_url"]
        site_name = row["site_name"] or _derive_site_name(domain)
        attempts  = row["attempts"]
        profile_dir = os.path.join(settings.BROWSER_PROFILES_DIR, site_name)

        remaining = PERM_SKIP_AFTER - attempts - 1  # skips left before auto-permanent

        prompt_lines = [
            f"[bold]{site_name}[/bold]  ({domain})",
            f"  Skipped [bold yellow]{attempts}[/bold yellow] time(s) · "
            + (
                f"[dim]auto-skip after [bold]{remaining}[/bold] more[/dim]"
                if remaining > 0 else
                "[bold red]last chance before permanent skip[/bold red]"
            ),
            "",
            "  [bold white][ y ][/bold white]  Login now",
            "  [bold white][ l ][/bold white]  Later (keep in queue)",
            "  [bold white][ n ][/bold white]  Never ask again (permanent skip)",
        ]

        response = _plain_ask(prompt_lines)

        if response in ("n", "no", "never"):
            database.add_skipped_site(domain, "user explicitly declined after repeated prompts")
            database.remove_pending_auth_site(domain)
            console.print("  [dim]→ Permanently skipped.[/dim]\n")
            continue

        if response in ("y", "yes"):
            success = await _collect_credentials_and_login(site_name, site_url, domain, profile_dir)
            if success:
                console.print(f"  [green]→ Session saved. {site_name} will be scraped this run.[/green]\n")
            else:
                console.print("  [dim]→ Login incomplete. Kept in queue.[/dim]\n")
            continue

        # 'l' / 'later' / timeout / skip → re-queue with incremented attempt
        database.add_pending_auth_site(domain, site_url, site_name)
        new_attempts = database.get_pending_auth_site_attempts(domain)

        if new_attempts >= PERM_SKIP_AFTER:
            database.add_skipped_site(domain, f"auto-skipped after {new_attempts} skips")
            database.remove_pending_auth_site(domain)
            console.print(
                f"  [yellow]→ Auto-permanently-skipped after {new_attempts} skips.[/yellow]\n"
            )
        else:
            console.print(
                f"  [dim]→ Kept in queue ({new_attempts}/{PERM_SKIP_AFTER}).[/dim]\n"
            )

    console.print()


# ── Session expiry ─────────────────────────────────────────────────────────────

async def refresh_expired_session(
    site_name: str,
    site_url: str,
    profile_dir: str,
    ui: Optional["PipelineUI"] = None,
) -> bool:
    """Wipes stale state.json and re-prompts. Called by router on 401/403."""
    state_file = Path(profile_dir) / "state.json"
    if state_file.exists():
        state_file.unlink()
    console.print(f"\n  [yellow]Session expired for {site_name}. Please log in again.[/yellow]")
    return await handle_auth_site(site_name, site_url, profile_dir, ui)


# ── .env helper ───────────────────────────────────────────────────────────────

def _append_env(key: str, value: str) -> None:
    env_path = Path(settings.BASE_DIR) / ".env"
    lines    = env_path.read_text().splitlines() if env_path.exists() else []
    updated  = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            updated = True
            break
    if not updated:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n")
