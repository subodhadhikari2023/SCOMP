# S.C.O.M.P
### Stealth Collection, Outreach & Messaging Pipeline

A self-contained Python CLI automation framework for personalised cold email outreach targeting freelance and job opportunities. Handles the complete pipeline — lead discovery, web scraping, email extraction, AI-generated copy, and regulated drip dispatch — all tracked in a local SQLite database with a Rich terminal dashboard.

---

## Architecture

```
Discovery (Bing)  →  Scraping (httpx / Playwright)  →  Email Extraction
       ↓
Normalisation  →  Copywriting (Gemini)  →  Drip Dispatch (Outlook SMTP)
       ↓
   SQLite DB  ←→  Rich Terminal Dashboard
```

## Directory Structure

```
SCOMP/
├── config/          # Env loading, YAML discovery config, per-site CSS selectors
├── scraper/         # Bing querying, httpx fast scraper, Playwright heavy scraper,
│                    #   routing logic, auth bootstrap, email extractor
├── pipeline/        # Lead normalizer, Gemini copywriter, SMTP dispatcher
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
# Fill in GEMINI_API_KEY and SMTP_ADDRESS
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
| `python main.py --setup-sender` | Refresh Outlook Web session (if expired) |

---

## Configuration

All discovery settings, search queries, and per-site CSS selectors live in `config/targets.yaml`.  
All secrets and limits live in `.env` (see `.env.example`).  
No logic changes needed when a site redesigns its HTML — update the YAML selector only.

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
| Search queries per run | 8 |
| Total URLs per run | 400–500 |
| Expected usable leads | 80–120 |
| Emails sent per day | 50–80 (hard cap: 80) |
| Gap between sends | 4–12 minutes (randomised) |

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Yes | Google AI Studio free tier key |
| `SMTP_ADDRESS` | Yes | Your Outlook address (emails sent from here via Outlook Web) |
| `DAILY_EMAIL_CAP` | No | Max emails per day. Default: 80 |
| `SEARCH_ENGINE` | No | `bing` or `duckduckgo`. Default: bing |
| `BROWSER_ENGINE` | No | `firefox`, `chromium`, or `webkit`. Default: firefox |
| `AUTH_PROMPT_TIMEOUT` | No | Seconds before skipping an auth-required site. Default: 60 |
| `DISCOVERY_FLUSH_EVERY` | No | URLs buffered before DB flush. Default: 50 |

---

## Tech Stack

Python 3.11 · httpx · BeautifulSoup4 · Playwright (Firefox) · SQLite · google-generativeai · Rich · pyyaml · python-dotenv · Docker
