import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── SMTP ──────────────────────────────────────────────────────────────────────
SMTP_ADDRESS: str  = os.getenv("SMTP_ADDRESS", "")
DAILY_EMAIL_CAP: int = int(os.getenv("DAILY_EMAIL_CAP", "80"))

# ── Auth credentials (populated at runtime via auth_bootstrap) ────────────────
LINKEDIN_EMAIL:    str = os.getenv("LINKEDIN_EMAIL", "")
LINKEDIN_PASSWORD: str = os.getenv("LINKEDIN_PASSWORD", "")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).resolve().parent.parent
DB_PATH       = str(BASE_DIR / os.getenv("DB_PATH", "db/scomp.sqlite"))
LOG_DIR       = str(BASE_DIR / "logs")
BROWSER_PROFILES_DIR = str(BASE_DIR / "browser_profiles")
TARGETS_YAML  = str(BASE_DIR / "config" / "targets.yaml")

# ── Auth bootstrap timeout ────────────────────────────────────────────────────
# Seconds to wait for user response before skipping an auth-required site
AUTH_PROMPT_TIMEOUT: int = int(os.getenv("AUTH_PROMPT_TIMEOUT", "60"))

# ── Pipeline passes ───────────────────────────────────────────────────────────
# S1 loops passes indefinitely until DAILY_EMAIL_CAP is reached.
# DISCOVERY_PASS_DELAY: seconds to wait between passes (lets Brave/Bing rate-limit
# cool down and gives downstream stages time to drain the URL queue).
DISCOVERY_PASS_DELAY: int = int(os.getenv("DISCOVERY_PASS_DELAY", "300"))

# ── Dispatch ──────────────────────────────────────────────────────────────────
DISPATCH_GAP_MIN: int = 4   # minutes
DISPATCH_GAP_MAX: int = 12  # minutes

# ── Search engine ─────────────────────────────────────────────────────────────
# Supported values: "brave" | "google" | "bing" (all Playwright stealth) | "duckduckgo" (httpx fallback)
SEARCH_ENGINE: str = os.getenv("SEARCH_ENGINE", "brave").lower()

# ── Browser engine ────────────────────────────────────────────────────────────
# Supported values: "firefox", "chromium", "webkit"
BROWSER_ENGINE: str = os.getenv("BROWSER_ENGINE", "firefox").lower()

# ── Scraper ───────────────────────────────────────────────────────────────────
# Flush discovered URLs to DB after this many new entries accumulate in the buffer.
# Lower = more crash-safe, slightly more DB writes. Higher = fewer writes, more RAM.
DISCOVERY_FLUSH_EVERY: int = int(os.getenv("DISCOVERY_FLUSH_EVERY", "50"))

HTTP_TIMEOUT: int   = 20    # seconds per request
MAX_RETRIES: int    = 2
USER_AGENT: str = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ── Portfolio / proof ─────────────────────────────────────────────────────────
# When set, a "Code samples available at <URL>" line is appended to every email's
# value proposition so recipients can verify the project claims independently.
PORTFOLIO_URL: str = os.getenv("PORTFOLIO_URL", "")

# ── Test mode ─────────────────────────────────────────────────────────────────
# When set, all outgoing emails are redirected to this address instead of the
# actual lead recipient. Everything else (scraping, copywriting, DB) runs normally.
TEST_RECIPIENT_EMAIL: str = os.getenv("TEST_RECIPIENT_EMAIL", "")

# ── Copywriter validation ─────────────────────────────────────────────────────
EMAIL_BODY_MAX_WORDS: int = 110
FORBIDDEN_PHRASES = [
    "passionate", "excited to", "hope this finds",
    "i wanted to reach out", "touch base", "circle back",
    "leverage", "synergy", "innovative",
]
