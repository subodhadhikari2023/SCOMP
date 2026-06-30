# S.C.O.M.P
### Stealth Collection, Outreach & Messaging Pipeline

A self-contained Python CLI automation framework for personalised cold email outreach targeting freelance and job opportunities. Handles the complete pipeline — lead discovery, web scraping, email extraction, template-based copywriting, and regulated drip dispatch — all tracked in a local SQLite database with a Rich terminal dashboard. Fully offline; no third-party LLM or paid API required.

---

## Architecture

```
Discovery (Brave/Bing)  →  Scraping (httpx / Playwright)  →  Email Extraction
          ↓
Normalisation  →  Copywriting (YAML template engine)  →  Drip Dispatch (Outlook Web)
          ↓
      SQLite DB  ←→  Rich Terminal Dashboard
```

## Directory Structure

```
SCOMP/
├── config/          # Env loading, YAML query bank, email templates, per-site CSS selectors
├── scraper/         # Search engine querying, httpx fast scraper, Playwright heavy scraper,
│                    #   routing logic, auth bootstrap, email extractor
├── pipeline/        # Lead normalizer, template-based copywriter, Playwright dispatcher
├── db/              # SQLite schema + query/transaction layer
├── ui/              # Rich live terminal dashboard
├── browser_profiles/# Playwright session state (gitignored)
└── logs/            # Rotating run logs (gitignored)
```

---

## Quick Start

### 1. Clone and configure

```bash
cd SCOMP
cp .env.example .env
# Fill in SMTP_ADDRESS (your Outlook address)
```

### 2. Run with Docker (recommended)

```bash
docker-compose up --build
```

The container runs `--run` (full pipeline) by default and exits when the daily cap is reached.

### 3. Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install firefox

python main.py --run
```

---

## CLI Commands

| Command | Description |
|---|---|
| `python main.py --run` | Full pipeline end to end |
| `python main.py --discover` | Discovery + scraping + normalisation only |
| `python main.py --write` | Copywriting for all ready leads |
| `python main.py --send` | Dispatch drafted emails |
| `python main.py --dashboard` | Live auto-refreshing terminal dashboard |
| `python main.py --summary` | Print today's run snapshot |
| `python main.py --setup` | Re-run first-boot setup wizard |
| `python main.py --setup-sender` | Log into Outlook Web once to save session |

---

## Configuration

All search queries live in `config/queries.yaml` (520 queries across 10 niches).  
Email copy templates live in `config/email_templates.yaml`.  
Per-site CSS selectors live in `config/targets.yaml`.  
All secrets and limits live in `.env` (see `.env.example`).  
No code changes needed when a site redesigns its HTML — update the YAML selector only.

---

## Lead Status Flow

```
discovered → normalized → ready → drafted → sent
                                            ↓
                                  [bounced | replied | unsubscribed]

Any stage → [retry | flagged | error | skipped | manual]
```

---

## Daily Volume

| Parameter | Value |
|---|---|
| Search passes per run | Unlimited (until daily cap) |
| Total URLs per run | 400–500 |
| Expected usable leads | 80–120 |
| Emails sent per day | 50–80 (hard cap: 80) |
| Gap between sends | 4–12 minutes (randomised) |

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SMTP_ADDRESS` | Yes | Your Outlook address — used as the sender identity for Outlook Web dispatch |
| `DAILY_EMAIL_CAP` | No | Max emails per day. Default: 80 |
| `SEARCH_ENGINE` | No | `brave`, `google`, `bing`, or `duckduckgo`. Default: brave |
| `BROWSER_ENGINE` | No | `firefox`, `chromium`, or `webkit`. Default: firefox |
| `AUTH_PROMPT_TIMEOUT` | No | Seconds before skipping an auth-required site. Default: 60 |
| `DISCOVERY_FLUSH_EVERY` | No | URLs buffered before DB flush. Default: 50 |
| `DISCOVERY_PASS_DELAY` | No | Seconds to cool down between discovery passes. Default: 300 |
| `PORTFOLIO_URL` | No | URL appended to every email body as a code-sample link |
| `TEST_RECIPIENT_EMAIL` | No | When set, all outgoing emails are redirected here instead of the real lead |

---

## Email Dispatch — Outlook Web

Microsoft permanently disabled basic SMTP auth for personal Outlook.com accounts. The dispatcher automates Outlook Web via Playwright instead — no SMTP password, no OAuth2, no Azure app required.

**First-time setup (run once before first `--send`):**

```bash
python main.py --setup-sender
```

Opens a headed browser — log in once and handle any MFA. Session persists for weeks in `browser_profiles/outlook_sender/state.json`. Re-run when the dispatcher reports session expired.

---

## Tech Stack

Python 3.14 · httpx · BeautifulSoup4 · Playwright (Firefox) · SQLite · Rich · pyyaml · python-dotenv · Docker
