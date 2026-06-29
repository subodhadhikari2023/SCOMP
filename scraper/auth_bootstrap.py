"""
Auth bootstrap: terminal prompting for credentials + Playwright session save.

Timeout behaviour (60s default):
  - First timeout  → site queued in pending_auth_sites, pipeline continues.
  - Next run start → pending sites are shown and re-prompted before discovery.
  - User declines  → site moved to permanent skipped_sites, never prompted again.
"""

import getpass
import logging
import os
import select
import sys
from pathlib import Path
from typing import Optional

from config import settings
from db import database

logger = logging.getLogger(__name__)


def _timed_input(prompt: str, timeout: int) -> Optional[str]:
    """Reads a line from stdin. Returns None if no input within timeout seconds."""
    print(prompt, end="", flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if ready:
        return sys.stdin.readline().strip()
    print("\n  [no response — skipping for now]")
    return None


def _derive_site_name(domain: str) -> str:
    """wellfound.com → wellfound, jobs.lever.co → lever"""
    parts = domain.rstrip(".").split(".")
    return parts[-2] if len(parts) >= 2 else parts[0]


# ── Core handler ──────────────────────────────────────────────────────────────

async def handle_auth_site(site_name: str, site_url: str, profile_dir: str) -> bool:
    """
    Interactively handles an auth-required site discovered during scraping.
    Returns True if a usable session exists or was just created.
    On timeout: queues the site in pending_auth_sites and returns False.
    On explicit decline: permanently skips the domain.
    """
    from scraper.heavy_track import fetch_html_visible
    from urllib.parse import urlparse

    domain     = urlparse(site_url).netloc.lower().lstrip("www.")
    state_file = Path(profile_dir) / "state.json"

    if state_file.exists():
        logger.info("Saved session found for %s — reusing.", site_name)
        return True

    print(f"\n{'─'*62}")
    print(f"  AUTH REQUIRED  —  {site_name}  ({domain})")
    print(f"{'─'*62}")
    print(f"  This site requires a login to scrape job listings.")
    print(f"  You have {settings.AUTH_PROMPT_TIMEOUT}s to respond. No response = queued for next run.\n")

    response = _timed_input(
        f"  Do you have an account on {site_name}? [y / n / skip]: ",
        settings.AUTH_PROMPT_TIMEOUT,
    )

    if response is None:
        # Timeout — queue for next run, do not permanently skip
        database.add_pending_auth_site(domain, site_url, site_name)
        logger.info("Auth timeout for %s — queued for next run.", site_name)
        return False

    if response.lower() in ("n", "no"):
        database.add_skipped_site(domain, "user has no account")
        database.remove_pending_auth_site(domain)
        logger.info("Permanently skipping %s — no account.", site_name)
        return False

    if response.lower() in ("skip", "s"):
        database.add_pending_auth_site(domain, site_url, site_name)
        logger.info("User skipped %s — queued for next run.", site_name)
        return False

    if response.lower() in ("y", "yes"):
        return await _collect_credentials_and_login(
            site_name, site_url, domain, profile_dir
        )

    # Unrecognised response — treat as skip
    database.add_pending_auth_site(domain, site_url, site_name)
    return False


async def _collect_credentials_and_login(
    site_name: str, site_url: str, domain: str, profile_dir: str
) -> bool:
    from scraper.heavy_track import fetch_html_visible

    env_key_email = f"{site_name.upper()}_EMAIL"
    env_key_pass  = f"{site_name.upper()}_PASSWORD"

    email = _timed_input(
        f"  Email for {site_name}: ",
        settings.AUTH_PROMPT_TIMEOUT,
    )
    if email is None:
        database.add_pending_auth_site(domain, site_url, site_name)
        return False

    print(f"  Password for {site_name} (hidden): ", end="", flush=True)
    password = getpass.getpass("")

    _append_env(env_key_email, email)
    _append_env(env_key_pass, password)

    print(f"\n  Opening browser for {site_name}.")
    print(f"  Complete the login (including MFA if required), then close the tab.")
    print(f"  Session will be saved automatically.\n")

    try:
        await fetch_html_visible(site_url, profile_dir)
        database.remove_pending_auth_site(domain)
        logger.info("Session saved for %s at %s", site_name, profile_dir)
        return True
    except Exception as exc:
        logger.error("Session save failed for %s: %s", site_name, exc)
        database.add_pending_auth_site(domain, site_url, site_name)
        return False


# ── Pending auth re-prompting (called at run start) ───────────────────────────

async def handle_pending_auth_sites() -> None:
    """
    Called at the beginning of every run. Shows all sites that timed out
    in previous runs and gives the user another chance to log in.
    Sites the user declines here are permanently added to skipped_sites.
    """
    pending = database.get_pending_auth_sites()
    if not pending:
        return

    print(f"\n{'═'*62}")
    print(f"  {len(pending)} site(s) are waiting for your login credentials.")
    print(f"  These were skipped in previous runs due to timeout.")
    print(f"{'═'*62}\n")

    for row in pending:
        domain    = row["domain"]
        site_url  = row["site_url"]
        site_name = row["site_name"] or _derive_site_name(domain)
        attempts  = row["attempts"]
        profile_dir = os.path.join(settings.BROWSER_PROFILES_DIR, site_name)

        print(f"  [{attempts} previous timeout(s)]  {site_name}  ({domain})")
        response = _timed_input(
            f"  Handle {site_name} now? [y / n / later]: ",
            settings.AUTH_PROMPT_TIMEOUT,
        )

        if response is None or response.lower() in ("later", "l", "skip", "s"):
            # Timeout or explicit "later" — keep in pending, try again next run
            database.add_pending_auth_site(domain, site_url, site_name)
            print(f"  → Kept in queue for next run.\n")
            continue

        if response.lower() in ("n", "no"):
            database.add_skipped_site(domain, "user declined after repeated prompts")
            database.remove_pending_auth_site(domain)
            print(f"  → Permanently skipped.\n")
            continue

        if response.lower() in ("y", "yes"):
            success = await _collect_credentials_and_login(
                site_name, site_url, domain, profile_dir
            )
            if success:
                print(f"  → Session saved. {site_name} will be scraped this run.\n")
            else:
                print(f"  → Login incomplete. Kept in queue.\n")


# ── Session expiry ─────────────────────────────────────────────────────────────

async def refresh_expired_session(site_name: str, site_url: str, profile_dir: str) -> bool:
    """Wipes stale state.json and re-prompts. Called by router on 401/403."""
    state_file = Path(profile_dir) / "state.json"
    if state_file.exists():
        state_file.unlink()
    print(f"\n  Session expired for {site_name}. Please log in again.")
    return await handle_auth_site(site_name, site_url, profile_dir)


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
