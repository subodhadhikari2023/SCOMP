"""
Rich terminal live dashboard.
Renders a two-panel layout: stats table on top, recent log feed below.
Refreshes every 5 seconds when called in live mode.
"""

import time
from datetime import datetime
from typing import Optional

from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text

from config import settings
from db import database

console = Console()

VERSION = "v1.0"
TITLE   = f"S.C.O.M.P  —  Stealth Collection, Outreach & Messaging Pipeline  {VERSION}"


def _counts() -> dict:
    return database.count_leads_by_status()


def _sent_today() -> int:
    return database.count_sent_today()


def _recent_logs(n: int = 12) -> list:
    return database.get_recent_email_logs(n)


def _stats_table(counts: dict, sent_today: int) -> Table:
    tbl = Table(box=box.SIMPLE_HEAVY, show_header=False, padding=(0, 2))
    tbl.add_column("Metric", style="bold cyan", min_width=22)
    tbl.add_column("Value",  style="white", justify="right", min_width=10)

    def row(label, value, style="white"):
        tbl.add_row(f"[{style}]{label}[/{style}]", f"[{style}]{value}[/{style}]")

    row("Discovered",  counts.get("discovered", 0))
    row("Normalised",  counts.get("normalized", 0))
    row("Ready",       counts.get("ready", 0),    "green")
    row("Drafted",     counts.get("drafted", 0),  "yellow")
    row("Sent today",  f"{sent_today} / {settings.DAILY_EMAIL_CAP}", "bold green")
    tbl.add_row()
    row("Bounced",     counts.get("bounced", 0),  "red")
    row("Replied",     counts.get("replied", 0),  "bright_green")
    row("Flagged",     counts.get("flagged", 0),  "red")
    row("Manual",      counts.get("manual", 0),   "yellow")
    row("Skipped",     counts.get("skipped", 0))
    row("Error",       counts.get("error", 0),    "red")
    return tbl


def _progress_bar(sent_today: int) -> Progress:
    prog = Progress(
        TextColumn("[bold cyan]  Daily cap"),
        BarColumn(bar_width=40),
        TextColumn(f"[green]{sent_today}[/green] / [white]{settings.DAILY_EMAIL_CAP}[/white]"),
    )
    task = prog.add_task("", total=settings.DAILY_EMAIL_CAP)
    prog.update(task, completed=sent_today)
    return prog


def _log_panel(logs: list) -> Panel:
    lines = Text()
    for log in logs:
        ts = (log["sent_at"] or log.get("generated_at", ""))[:16].replace("T", " ")
        status = (log["status"] or "").lower()
        company = log.get("company") or ""
        recipient = log.get("recipient_email") or ""

        if status == "sent":
            color = "green"
            icon  = "✓"
        elif status == "flagged":
            color = "red"
            icon  = "✗"
        else:
            color = "yellow"
            icon  = "·"

        lines.append(f"  [{ts}]  ", style="dim")
        lines.append(f"{icon} {status.upper():<8}", style=f"bold {color}")
        lines.append(f" → {recipient}", style="white")
        if company:
            lines.append(f"  ({company})", style="dim")
        lines.append("\n")

    return Panel(lines, title="[bold]Recent Activity[/bold]", border_style="cyan", padding=(0, 1))


def render_snapshot() -> None:
    """Print a single static snapshot — used by --summary."""
    counts     = _counts()
    sent_today = _sent_today()
    logs       = _recent_logs()

    console.print()
    console.rule(f"[bold cyan]{TITLE}[/bold cyan]")
    console.print()
    console.print(_stats_table(counts, sent_today))
    console.print(_progress_bar(sent_today))
    console.print()
    console.print(_log_panel(logs))
    console.print()


def render_live(stop_event=None) -> None:
    """
    Live auto-refreshing dashboard. Runs until stop_event is set or Ctrl-C.
    """
    def _build():
        counts     = _counts()
        sent_today = _sent_today()
        logs       = _recent_logs()

        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )
        layout["body"].split_row(
            Layout(name="stats", ratio=1),
            Layout(name="logs",  ratio=2),
        )

        layout["header"].update(
            Panel(f"[bold cyan]{TITLE}[/bold cyan]", border_style="cyan")
        )
        layout["stats"].update(
            Panel(_stats_table(counts, sent_today), title="[bold]Stats[/bold]", border_style="blue")
        )
        layout["logs"].update(_log_panel(logs))
        layout["footer"].update(
            Panel(_progress_bar(sent_today), border_style="green")
        )
        return layout

    try:
        with Live(_build(), refresh_per_second=0.2, screen=True) as live:
            while True:
                if stop_event and stop_event.is_set():
                    break
                time.sleep(5)
                live.update(_build())
    except KeyboardInterrupt:
        pass
