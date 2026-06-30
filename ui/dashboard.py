"""
S.C.O.M.P — Terminal dashboard (v2).

Screen layout (full-terminal, refreshes every 250 ms)
──────────────────────────────────────────────────────
  ┌ header (4 rows) ──────────────────────────────────────────────────────────────┐
  │  title · sub · version  /  run# · pass · elapsed · clock · cap bar           │
  ├ flow row 1 (9 rows) ──────────────────────────────────────────────────────────┤
  │  S1 Discovery  │  ████████░░░░ URL Queue 18/50  │  S2-4 Scraping              │
  ├ flow row 2 (9 rows) ──────────────────────────────────────────────────────────┤
  │  S2-4 Scraping │  ████░░░░░░░ Lead Queue 4/20  │  S5 Normalise               │
  ├ flow row 3 (9 rows) ──────────────────────────────────────────────────────────┤
  │  S5 Normalise  │           →  sequential       │  S6 Copywrite               │
  ├ flow row 4 (9 rows) ──────────────────────────────────────────────────────────┤
  │  S6 Copywrite  │           →  sequential       │  S7 Dispatch                │
  ├ live events (remaining rows, min 4) ──────────────────────────────────────────┤
  │  HH:MM:SS  STAGE  text…                                           badge       │
  └ prompt zone (only when a question is pending) ────────────────────────────────┘

State model
───────────
  StageState    mutable per-stage (status, note, current, items, stats)
  PipelineState shared across all coroutines; also holds queue references and
                a chronological event_log
  PipelineUI    async context manager: owns the Live display + interactive prompts
  build_layout  pure function → Rich Layout from PipelineState
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime

from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from config import settings
from db import database

console = Console()

VERSION   = "v2.0"
APP_TITLE = "S.C.O.M.P"
APP_SUB   = "Stealth Collection, Outreach & Messaging Pipeline"

# ── Stage status tokens ────────────────────────────────────────────────────────

WAITING = "waiting"
RUNNING = "running"
DONE    = "done"
ERROR   = "error"

_ICON = {
    WAITING: ("○", "dim white"),
    RUNNING: ("●", "bold green"),
    DONE:    ("✓", "bold cyan"),
    ERROR:   ("✗", "bold red"),
}
_BORDER = {
    WAITING: "dim",
    RUNNING: "cyan",
    DONE:    "green",
    ERROR:   "red",
}
_STAGE_COLOR = {
    "S1":   "blue",
    "S2-4": "magenta",
    "S5":   "cyan",
    "S6":   "yellow",
    "S7":   "green",
}


# ── State objects ──────────────────────────────────────────────────────────────

@dataclass
class StageState:
    number:  str
    label:   str
    status:  str  = WAITING
    note:    str  = ""
    current: str  = ""
    items:   list = field(default_factory=list)   # [(text, badge), ...]
    stats:   dict = field(default_factory=dict)

    def add_item(self, text: str, right: str = "", *, maxkeep: int = 5):
        self.items.append((text, right))
        if len(self.items) > maxkeep:
            self.items = self.items[-maxkeep:]


@dataclass
class PipelineState:
    pass_num:   int   = 1
    run_id:     int   = 0
    started_at: float = field(default_factory=time.time)

    s1:  StageState = field(default_factory=lambda: StageState("1",   "Discovery"))
    s24: StageState = field(default_factory=lambda: StageState("2–4", "Scraping"))
    s5:  StageState = field(default_factory=lambda: StageState("5",   "Normalise"))
    s6:  StageState = field(default_factory=lambda: StageState("6",   "Copywrite"))
    s7:  StageState = field(default_factory=lambda: StageState("7",   "Dispatch"))

    # Live queue references — set by cmd_run() after queue creation.
    # .qsize() is called on every render to show actual buffer depth.
    url_queue:      object = None   # asyncio.Queue
    lead_queue:     object = None   # asyncio.Queue
    url_queue_cap:  int    = 50
    lead_queue_cap: int    = 20

    # Chronological event stream — (time_str, stage_id, text, badge)
    event_log:    list = field(default_factory=list)

    # Prompt zone — non-empty while awaiting user input
    prompt_lines: list = field(default_factory=list)

    def log_event(self, stage_id: str, text: str, badge: str = "") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.event_log.append((ts, stage_id, text, badge))
        if len(self.event_log) > 80:
            self.event_log = self.event_log[-80:]


# ── Bar helper ─────────────────────────────────────────────────────────────────

def _bar_markup(filled: int, total: int, width: int) -> str:
    """Return Rich markup for a colored fill-bar."""
    pct   = filled / total if total else 0.0
    n     = int(pct * width)
    color = "green" if pct < 0.5 else ("yellow" if pct < 0.8 else "red")
    return (
        f"[bold {color}]{'█' * n}[/bold {color}]"
        f"[dim]{'░' * (width - n)}[/dim]"
    )


# ── Panel renderers ────────────────────────────────────────────────────────────

def _buf_panel(label: str, filled: int, cap: int, per_row: int) -> Panel:
    """Queue buffer panel: one █/░ per slot, wrapped at per_row, centered."""
    pct   = filled / cap if cap else 0.0
    color = "green" if pct < 0.5 else ("yellow" if pct < 0.8 else "red")

    body = Text(justify="center")
    for slot in range(cap):
        if slot > 0 and slot % per_row == 0:
            body.append("\n")
        body.append(
            "█" if slot < filled else "░",
            style=f"bold {color}" if slot < filled else "dim",
        )
    body.append(f"\n{filled} / {cap}", style="dim")

    return Panel(body, title=f"[dim]{label}[/dim]",
                 border_style="dim", padding=(0, 1))


def _arrow_panel(label: str) -> Panel:
    """Simple sequential-gate connector panel (no queue)."""
    body = Text(justify="center")
    body.append("\n\n")
    body.append("→  ", style="dim")
    body.append(label, style="dim italic")
    return Panel(body, border_style="dim", padding=(0, 1))


def _stage_card(st: StageState) -> Panel:
    icon, istyle = _ICON.get(st.status, ("?", "white"))
    border       = _BORDER.get(st.status, "dim")

    tbl = Table.grid(padding=(0, 1))
    tbl.add_column(width=2,  no_wrap=True)
    tbl.add_column(ratio=1,  no_wrap=True, overflow="ellipsis")
    tbl.add_column(width=7,  no_wrap=True, justify="right")

    # Status
    tbl.add_row(
        f"[{istyle}]{icon}[/{istyle}]",
        f"[{istyle}]{st.status.upper()}[/{istyle}]",
        "",
    )

    # Note (engine name, warnings, etc.)
    if st.note:
        note = (st.note[:20] + "…") if len(st.note) > 21 else st.note
        tbl.add_row("", f"[dim]{note}[/dim]", "")

    # Current activity (only when running)
    if st.current and st.status == RUNNING:
        cur = (st.current[:20] + "…") if len(st.current) > 21 else st.current
        tbl.add_row("[yellow]▶[/yellow]", f"[white]{cur}[/white]", "")

    tbl.add_row("", "", "")  # spacer

    # Recent items (all kept, up to maxkeep)
    for text, badge in st.items[-5:]:
        t = (text[:16] + "…") if len(text) > 17 else text
        b = badge[:7]
        tbl.add_row("[dim]·[/dim]", f"[dim]{t}[/dim]", f"[dim]{b}[/dim]")

    tbl.add_row("", "", "")  # spacer

    # Stats footer — abbreviated keys to fit narrow columns
    if st.stats:
        parts = []
        _abbrev = {
            "queries": "q", "urls": "u", "scraped": "sc",
            "emails":  "em", "queued": "q", "stored": "st",
            "dups": "dup", "drafted": "dr", "flagged": "fl",
            "sent": "s", "cap": "/", "skipped": "sk",
        }
        for k, v in list(st.stats.items())[:4]:
            short = _abbrev.get(k, k[:3])
            parts.append(f"[dim]{short}[/dim][bold white]{v}[/bold white]")
        tbl.add_row("", " ".join(parts), "")

    title = (
        f"[{istyle}]{icon}[/{istyle}] "
        f"[bold]S{st.number}[/bold]"
        f"[dim]·{st.label}[/dim]"
    )
    return Panel(tbl, title=title, border_style=border, padding=(0, 0))


def _events_panel(state: PipelineState) -> Panel:
    # Pull from event_log (pipeline events) and DB email log, merge newest-first
    db_logs = database.get_recent_email_logs(10)

    tbl = Table.grid(padding=(0, 1))
    tbl.add_column(width=8,  no_wrap=True)   # time
    tbl.add_column(width=5,  no_wrap=True)   # stage id
    tbl.add_column(ratio=1,  no_wrap=True, overflow="ellipsis")   # text
    tbl.add_column(width=12, justify="right", no_wrap=True)  # badge

    # Pipeline events (last 12 from event_log)
    for ts, stage_id, text, badge in state.event_log[-12:]:
        color = _STAGE_COLOR.get(stage_id, "white")
        txt   = (text[:36] + "…") if len(text) > 37 else text
        tbl.add_row(
            f"[dim]{ts}[/dim]",
            f"[{color}]{stage_id:<5}[/{color}]",
            f"[white]{txt}[/white]",
            f"[dim]{badge}[/dim]",
        )

    # Separator between pipeline events and DB email log
    if state.event_log and db_logs:
        tbl.add_row("[dim]─[/dim]", "[dim]─────[/dim]",
                    "[dim]─── email log ──────────────────────[/dim]", "")

    # DB email log (sent / drafted / flagged)
    for row in db_logs[:5]:
        ts     = (row["sent_at"] or "")[:16].replace("T", " ")[11:]  # HH:MM only
        status = (row["status"] or "").lower()
        recip  = row["recipient_email"] or ""
        co     = row["company"] or ""
        icon, color = {
            "sent":    ("✓", "green"),
            "flagged": ("✗", "red"),
            "drafted": ("·", "yellow"),
        }.get(status, ("·", "dim"))
        label = f"{icon} {status.upper()}"
        tbl.add_row(
            f"[dim]{ts}[/dim]",
            f"[{color}]{label:<5}[/{color}]",
            f"[dim]{recip[:36]}[/dim]",
            f"[dim]{co[:12]}[/dim]",
        )

    if not state.event_log and not db_logs:
        tbl.add_row("", "", "[dim]Pipeline starting…[/dim]", "")

    return Panel(tbl, title="[bold]Live Events[/bold]",
                 border_style="dim", padding=(0, 1))


def _prompt_panel(lines: list) -> Panel:
    body = Text()
    for ln in lines:
        body.append(ln + "\n")
    return Panel(
        body,
        title="[bold yellow]⚡  Action Required[/bold yellow]",
        border_style="yellow",
        padding=(0, 2),
    )


# ── Layout builder ─────────────────────────────────────────────────────────────

def build_layout(state: PipelineState) -> Layout:
    sent    = database.count_sent_today()
    cap     = settings.DAILY_EMAIL_CAP
    now     = datetime.now().strftime("%H:%M:%S")
    elapsed = _elapsed(state.started_at)
    cap_bar = _bar_markup(sent, cap, 24)

    header = Panel(
        Align.center(
            Text.from_markup(
                f"[bold cyan]{APP_TITLE}[/bold cyan]  [dim]·  {APP_SUB}  ·  {VERSION}[/dim]\n"
                f"[dim]Run #{state.run_id}  ·  Pass {state.pass_num}  ·  {elapsed}  ·  {now}[/dim]"
                f"   [{cap_bar}]  [bold]{sent}[/bold][dim]/{cap}[/dim]"
            )
        ),
        border_style="cyan",
        padding=(0, 2),
    )

    url_sz  = state.url_queue.qsize()  if state.url_queue  else 0
    lead_sz = state.lead_queue.qsize() if state.lead_queue else 0

    # Each entry: (left StageState, middle panel, right StageState)
    flow_rows = [
        (state.s1,  _buf_panel("URL Queue",  url_sz,  state.url_queue_cap,  25), state.s24),
        (state.s24, _buf_panel("Lead Queue", lead_sz, state.lead_queue_cap, 20), state.s5),
        (state.s5,  _arrow_panel("sequential"),                                   state.s6),
        (state.s6,  _arrow_panel("sequential"),                                   state.s7),
    ]

    sections = [Layout(header, name="header", size=4)]
    for i, (left, mid, right) in enumerate(flow_rows):
        row = Layout(name=f"flow{i}")
        row.split_row(
            Layout(_stage_card(left),  name=f"f{i}_l", ratio=2),
            Layout(mid,                name=f"f{i}_m", ratio=3),
            Layout(_stage_card(right), name=f"f{i}_r", ratio=2),
        )
        sections.append(Layout(row, name=f"flow{i}", size=9))

    sections.append(Layout(_events_panel(state), name="events", minimum_size=4))

    if state.prompt_lines:
        height = min(len(state.prompt_lines) + 4, 10)
        sections.append(Layout(_prompt_panel(state.prompt_lines),
                                name="prompt", size=height))

    layout = Layout()
    layout.split_column(*sections)
    return layout


def _elapsed(started_at: float) -> str:
    secs = int(time.time() - started_at)
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m:02d}m" if h else f"{m:02d}m {s:02d}s"


# ── PipelineUI context manager ─────────────────────────────────────────────────

class PipelineUI:
    """
    Async context manager: owns the Live display for the pipeline run.

    ui.refresh()              — immediate re-render (call after any state mutation)
    await ui.ask(prompt, lbl) — pause live, read user input, resume; returns str.
                                prompt may be list[str] or any Rich renderable.
    """

    def __init__(self, state: PipelineState):
        self.state = state
        self._live = None
        self._stop = asyncio.Event()
        self._task = None
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        self._live = Live(
            build_layout(self.state),
            console=console,
            screen=True,
            refresh_per_second=1,
        )
        self._live.__enter__()
        self._task = asyncio.create_task(self._refresh_loop())
        return self

    async def __aexit__(self, *_):
        self._stop.set()
        if self._task:
            await self._task
        if self._live:
            self._live.__exit__(None, None, None)

    async def _refresh_loop(self):
        while not self._stop.is_set():
            async with self._lock:
                if self._live and not self._stop.is_set():
                    self._live.update(build_layout(self.state))
            await asyncio.sleep(0.25)

    def refresh(self):
        if self._live:
            self._live.update(build_layout(self.state))

    async def ask(self, prompt, input_label: str = "  → ") -> str:
        """
        Pause the live display, collect user input, then resume.
        prompt: list[str] for simple text lines, or any Rich renderable (Panel etc).
        """
        self.state.prompt_lines = prompt if isinstance(prompt, list) else ["  Awaiting response…"]
        self.refresh()

        async with self._lock:
            self._live.stop()
            console.print()
            if isinstance(prompt, list):
                console.print(_prompt_panel(prompt))
            else:
                console.print(prompt)
            response = await asyncio.to_thread(input, input_label)
            console.print()
            self._live.start(refresh=True)

        self.state.prompt_lines = []
        self.refresh()
        return response.strip().lower()


# ── Standalone modes (--dashboard, --summary) ──────────────────────────────────

def render_snapshot() -> None:
    """Static one-shot snapshot — used by --summary."""
    state  = PipelineState()
    counts = database.count_leads_by_status()
    sent   = database.count_sent_today()

    state.s1.stats  = {"urls":    counts.get("discovered", 0)}
    state.s5.stats  = {"stored":  counts.get("normalized", 0),
                       "ready":   counts.get("ready", 0)}
    state.s6.stats  = {"drafted": counts.get("drafted", 0),
                       "flagged": counts.get("flagged", 0)}
    state.s7.stats  = {"sent":    sent,
                       "cap":     settings.DAILY_EMAIL_CAP}
    for st in (state.s1, state.s24, state.s5, state.s6, state.s7):
        st.status = DONE

    console.print()
    console.print(build_layout(state))
    console.print()


def render_live(stop_event=None) -> None:
    """Auto-refreshing live view — used by --dashboard (no pipeline running)."""
    state = PipelineState()
    try:
        with Live(build_layout(state), console=console, screen=True,
                  refresh_per_second=0.2) as live:
            while True:
                if stop_event and stop_event.is_set():
                    break
                counts = database.count_leads_by_status()
                sent   = database.count_sent_today()
                state.s1.stats  = {"urls":    counts.get("discovered", 0)}
                state.s5.stats  = {"stored":  counts.get("normalized", 0)}
                state.s6.stats  = {"drafted": counts.get("drafted", 0)}
                state.s7.stats  = {"sent":    sent}
                live.update(build_layout(state))
                time.sleep(5)
    except KeyboardInterrupt:
        pass
