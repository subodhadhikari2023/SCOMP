# CLAUDE.md — S.C.O.M.P Handoff Notes

## What this project is

S.C.O.M.P (Stealth Collection, Outreach & Messaging Pipeline) is a single-operator,
zero-budget Python CLI that automates cold email outreach for freelance/job opportunities.
Fully offline — no third-party LLM or paid API. Uses only: Bing/Brave/DDG search,
YAML-based query bank + template engine, Outlook Web via Playwright.

---

## Key architecture decisions

- **No cron inside the container.** The pipeline runs sequentially when `main.py --run`
  is called. Exits via `sys.exit(0)` when the daily cap is reached. Autostart on boot
  is handled by the OS (systemd / launchd / Task Scheduler) via `--setup`.

- **Producer-consumer queues.** S1 (discovery) feeds a bounded `url_queue` (cap 50).
  S2-4 (scrape/extract/normalise) drains it and feeds a bounded `lead_queue` (cap 20).
  S5 (DB store) drains the lead queue. S6 (copywriting) and S7 (dispatch) are sequential
  gates that run after S5 signals completion via `asyncio.Event`.

- **YAML query bank.** `config/queries.yaml` holds 520 search queries across 10 niches.
  Loaded and shuffled at runtime; deduplicated against previous-pass queries before each
  new pass. To add queries: append to the relevant category block and bump `metadata.total`.

- **Template email engine.** `config/email_templates.yaml` — openings/value_props/CTAs/
  subjects per niche. `pipeline/copywriter.py` assembles body + subject from these parts,
  validates word count (≤ `EMAIL_BODY_MAX_WORDS`) and `FORBIDDEN_PHRASES`, then retries
  with `value_props.short` on first failure. Two failures → lead flagged. No LLM involved.

- **Selector isolation.** All CSS selectors for known sites are in `config/targets.yaml`,
  not in Python code. When a site redesigns its HTML, update only the YAML.

- **DB is the single source of truth.** Lead status transitions happen atomically via
  `db/database.py`. No status is set in-memory and then synced — every transition is a
  direct `UPDATE leads SET status=?` call.

- **Auth bootstrap timeout.** If a site requires login and the user doesn't respond within
  `AUTH_PROMPT_TIMEOUT` seconds (default 120), the domain is added to `skipped_sites` and
  the pipeline continues. Same site will prompt again on the next run unless the user
  explicitly skipped with "n". Auto-permanent-skip after 5 timeouts (`PERM_SKIP_AFTER`).

---

## Build order (for incremental work)

Files are built in dependency order: `db` → `config` → `scraper` → `pipeline` → `ui` → `main`.

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

Microsoft permanently disabled basic SMTP auth for personal Outlook.com accounts (error 5.7.139).
OAuth2 app registration is not accessible to personal accounts without an Azure subscription.

The dispatcher automates Outlook Web (outlook.live.com) via Playwright instead:
- Sends from the real subodhadhikari2023@outlook.com address
- No SMTP, no OAuth2, no Azure app required
- Session saved to `browser_profiles/outlook_sender/state.json` (gitignored)
- Inter-send gap is `asyncio.sleep` (non-blocking), range: `DISPATCH_GAP_MIN`–`DISPATCH_GAP_MAX` minutes

First-time setup:
  `python main.py --setup-sender`
  Opens a headed browser — user logs in once, handles any MFA.
  Session persists for weeks. Re-run when dispatcher reports session expired.

Key selectors (stable in Outlook Web as of 2026):
  New mail:  `[aria-label="New mail"]`
  To field:  `input[aria-label="To"]`
  Subject:   `[aria-label="Add a subject"]`
  Body:      `div[aria-label="Message body, press Alt+F10 to exit"]`
  Send:      `Ctrl+Return` keyboard shortcut

---

## Terminal UI

Full-screen live dashboard via `ui/dashboard.py`:
- `PipelineState` / `StageState` — shared mutable state updated by all stage coroutines.
  Holds live `asyncio.Queue` refs (`url_queue`, `lead_queue`) read on every render tick.
- `PipelineUI` — async context manager owning the `Live` display; pauses cleanly for
  interactive prompts via `await ui.ask(prompt_lines, input_label)`.
- `build_layout(state)` — pure function → Rich `Layout` with 5 sections:
  header · queue buffer bars · 5-column stage cards (S1 · S2-4 · S5 · S6 · S7) ·
  live event feed · optional prompt zone.
- `state.log_event(stage_id, text, badge)` — appends to rolling event log (cap 80);
  all stage coroutines in `main.py` call this per significant event.
- `--dashboard` and `--summary` CLI modes work standalone via `render_live()` / `render_snapshot()`.

---

## Auth skip logic

`scraper/auth_bootstrap.py` — `PERM_SKIP_AFTER = 5`:
- Timeout or `s` → `add_pending_auth_site` (increments `attempts`); ≥ 5 → auto-permanent-skip.
- `n` / `never` → immediately permanent-skip (user has no account on that site).
- During pipeline (S2-4): `handle_auth_site(..., ui=ui)` pauses the live dashboard for input.
- Before pipeline start: `handle_pending_auth_sites()` uses plain Rich console output.

---

## Known rough edges

- `scraper/discovery.py` does not rotate User-Agent or add random delays between queries —
  may trigger bot detection on long runs. Mitigation: add jitter + UA rotation in `_search_worker`.

- `scraper/email_extractor.py` crawls subpages via `asyncio.gather` but does not respect
  `robots.txt`. Add a `robotparser` check before crawling each domain.
