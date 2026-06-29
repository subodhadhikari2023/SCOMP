"""
Auth bootstrap: terminal prompting for credentials + Playwright session save.
If the user doesn't respond within AUTH_PROMPT_TIMEOUT seconds, the site is
skipped and queued for next run.
"""

import asyncio
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
    """Read a line from stdin with a timeout. Returns None on timeout."""
    print(prompt, end="", flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if ready:
        return sys.stdin.readline().strip()
    print("\n[timeout — skipping]")
    return None


async def handle_auth_site(site_name: str, site_url: str, profile_dir: str) -> bool:
    """
    Interactively handles an auth-required site.
    Returns True if session was successfully saved, False if skipped.
    """
    from scraper.heavy_track import fetch_html_visible
    from urllib.parse import urlparse

    domain = urlparse(site_url).netloc.lower().lstrip("www.")
    state_file = Path(profile_dir) / "state.json"

    # Already have a saved session
    if state_file.exists():
        logger.info("Saved session found for %s, reusing.", site_name)
        return True

    print(f"\n{'─'*60}")
    print(f"  AUTH REQUIRED: {site_name} ({domain})")
    print(f"{'─'*60}")
    response = _timed_input(
        f"  Do you have an account on {site_name}? [y/n/skip] (timeout {settings.AUTH_PROMPT_TIMEOUT}s): ",
        settings.AUTH_PROMPT_TIMEOUT,
    )

    if response is None or response.lower() in ("n", "no", "skip", "s"):
        reason = "no account" if (response and response.lower() in ("n", "no")) else "timeout/skip"
        database.add_skipped_site(domain, reason)
        logger.info("Skipping %s (%s)", site_name, reason)
        return False

    if response.lower() in ("y", "yes"):
        env_key_email = f"{site_name.upper()}_EMAIL"
        env_key_pass  = f"{site_name.upper()}_PASSWORD"

        # Collect credentials
        email = _timed_input(
            f"  Email for {site_name}: ",
            settings.AUTH_PROMPT_TIMEOUT,
        )
        if email is None:
            database.add_skipped_site(domain, "timeout during credential entry")
            return False

        print(f"  Password for {site_name}: ", end="", flush=True)
        password = getpass.getpass("")

        # Persist to .env
        _append_env(env_key_email, email)
        _append_env(env_key_pass, password)

        print(f"\n  Opening Firefox for {site_name}.")
        print(f"  Log in manually (including MFA if needed), then close the browser.")
        print(f"  Your session will be saved automatically.\n")

        try:
            await fetch_html_visible(site_url, profile_dir)
            logger.info("Session saved for %s at %s", site_name, profile_dir)
            return True
        except Exception as exc:
            logger.error("Failed to save session for %s: %s", site_name, exc)
            database.add_skipped_site(domain, f"auth save failed: {exc}")
            return False

    database.add_skipped_site(domain, "invalid response")
    return False


async def refresh_expired_session(site_name: str, site_url: str, profile_dir: str) -> bool:
    """
    Called when a saved session has expired. Prompts re-auth.
    Halts the current queue item until resolved or skipped.
    """
    state_file = Path(profile_dir) / "state.json"
    if state_file.exists():
        state_file.unlink()

    print(f"\n  Session expired for {site_name}. Re-authentication needed.")
    return await handle_auth_site(site_name, site_url, profile_dir)


def _append_env(key: str, value: str) -> None:
    env_path = Path(settings.BASE_DIR) / ".env"
    lines = env_path.read_text().splitlines() if env_path.exists() else []

    updated = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            updated = True
            break
    if not updated:
        lines.append(f"{key}={value}")

    env_path.write_text("\n".join(lines) + "\n")
