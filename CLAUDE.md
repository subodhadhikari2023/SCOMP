# CLAUDE.md — S.C.O.M.P Handoff Notes

This file is for future Claude Code sessions working on this project.

---

## What this project is

S.C.O.M.P (Stealth Collection, Outreach & Messaging Pipeline) is a single-operator,
zero-budget Python CLI that automates cold email outreach for freelance/job opportunities.
It uses only free-tier services (Bing search, Gemini free tier, Outlook SMTP via OAuth2).

---

## Key architecture decisions

- **No cron inside the container.** The pipeline runs sequentially top-to-bottom when
  `main.py --run` is called. The container exits when the daily cap is reached (`sys.exit(0)`).
  Autostart on boot is handled by the OS (systemd / launchd / Task Scheduler) via `--setup`.

- **Auth bootstrap timeout.** If a site requires login and the user doesn't respond within
  `AUTH_PROMPT_TIMEOUT` seconds (default 120), the domain is added to `skipped_sites` and
  the pipeline continues. Same site will prompt again on the next fresh run unless the user
  explicitly skipped with "n".

- **Selector isolation.** All CSS selectors for known sites are in `config/targets.yaml`,
  not in Python code. When a site redesigns its HTML, update only the YAML.

- **DB is the single source of truth.** Lead status transitions happen atomically via
  `db/database.py`. No status is set in-memory and then synced — every transition is a
  direct `UPDATE leads SET status=?` call.

- **Two Gemini calls per lead.** Body and subject line are separate calls to allow independent
  retries. Each is validated post-generation; two failures → lead flagged.

---

## Build order (for incremental work)

See section 18 of the original project brief (`SOE_Project_Brief.md` in Downloads).
Files are built in dependency order: db → config → scraper → pipeline → ui → main.

---

## What is NOT in v1

- Web frontend
- Multi-account SMTP rotation
- Reply detection / inbox monitoring
- Proxy rotation
- Cloud deployment
- Any non-public data sources

---

## Email dispatch — Outlook Web via Playwright

Microsoft permanently disabled basic SMTP auth for personal Outlook.com accounts (error 5.7.139),
and OAuth2 app registration is not accessible to personal accounts without an Azure subscription.

The dispatcher uses Playwright to automate Outlook Web (outlook.live.com) instead of SMTP:
- Sends from the real subodhadhikari2023@outlook.com address
- No SMTP, no OAuth2, no Azure app required
- Session saved to browser_profiles/outlook_sender/state.json (gitignored)

First-time setup:
  `python main.py --setup-sender`
  Opens a headed browser — user logs in once, handles any MFA.
  Session persists for weeks. Re-run when dispatcher reports session expired.

Key selectors (stable in Outlook Web as of 2026):
  New mail:  [aria-label="New mail"]
  To field:  input[aria-label="To"]
  Subject:   [aria-label="Add a subject"]
  Body:      div[aria-label="Message body, press Alt+F10 to exit"]
  Send:      Ctrl+Return keyboard shortcut

## Known rough edges to address in v2

- `scraper/discovery.py` does not rotate User-Agent or add random delays between queries,
  which may trigger Bing's bot detection on long runs.
- `pipeline/dispatcher.py` uses a blocking `time.sleep()` for the inter-send gap.
  If the gap is long (up to 12 min) this blocks the process. Consider asyncio.sleep in v2.
- `scraper/email_extractor.py` crawls subpages sequentially via asyncio.gather but does not
  respect `robots.txt`. Add a robots check in v2.
- `ui/dashboard.py` does not show per-stage progress during a live `--run`. Dashboard only
  reflects DB state, not in-flight pipeline stage.
