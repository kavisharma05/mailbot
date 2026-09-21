"""Command-line interface: python -m imt_access <command>.

Commands:
  health        - credentials + login + catalog reachability
  login-auto    - force a fresh portal login (OTP via Gmail if required)
  sync-catalog  - scrape /resources into Supabase and show the diff
  analyze       - generate the LLM catalog briefing (resource_analysis)
  daily         - fetch publications and deliver the digest (Telegram/WhatsApp)
"""

from __future__ import annotations

import asyncio
import logging
import sys

import click
from rich.console import Console
from rich.table import Table

from imt_access.settings import get_imt_settings

console = Console()
logger = logging.getLogger(__name__)


def _configure_logging(verbose: bool) -> None:
    # Windows consoles default to cp1252 — digests contain emoji/unicode.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _make_bot():
    """A bare Telegram Bot for CLI delivery (no PTB application needed)."""
    from config import get_settings
    from telegram import Bot

    settings = get_settings()
    return Bot(token=settings.telegram_bot_token)


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Debug logging")
def cli(verbose: bool) -> None:
    _configure_logging(verbose)


@cli.command()
def health() -> None:
    """Check credentials, portal login, and catalog reachability."""
    from imt_access.portal import health_check

    settings = get_imt_settings()
    console.print(f"[bold]Portal:[/bold] {settings.mapmyaccess_base_url}")
    console.print(f"[bold]Account:[/bold] {settings.mapmyaccess_email or '(not set)'}")
    with console.status("Checking portal session..."):
        result = health_check()
    if result.get("ok"):
        console.print("[green][OK] Login OK[/green] - catalog page reachable "
                      f"({result.get('resources_page_bytes')} bytes)")
    else:
        console.print(f"[red][FAIL] Health check failed:[/red] {result.get('error')}")
        sys.exit(1)


@cli.command(name="login-auto")
def login_auto() -> None:
    """Force a fresh login (uses .env credentials; OTP auto-read from Gmail)."""
    from imt_access.portal import PortalClient

    with console.status("Logging in to MapMyAccess..."):
        try:
            with PortalClient() as portal:
                portal.login()
                name = portal.display_name
        except Exception as exc:
            console.print(f"[red][FAIL] Login failed:[/red] {exc}")
            sys.exit(1)
    console.print(f"[green][OK] Logged in[/green] as {name or 'unknown'} - session cached")


@cli.command(name="sync-catalog")
def sync_catalog_cmd() -> None:
    """Scrape the live /resources catalog into Supabase."""
    from imt_access.jobs import run_catalog_sync

    with console.status("Syncing catalog..."):
        result = asyncio.run(run_catalog_sync(alert_new=False))
    if not result.get("ok"):
        console.print(f"[red][FAIL] Sync failed:[/red] {result.get('error')}")
        sys.exit(1)

    console.print(f"[green][OK] {result['total']} resources[/green] "
                  f"({len(result['new'])} new, {len(result['removed'])} removed)")
    if result["new"]:
        table = Table(title="New resources")
        table.add_column("Name")
        for name in result["new"]:
            table.add_row(name)
        console.print(table)


@cli.command()
def analyze() -> None:
    """Generate the LLM catalog briefing and store it for the web app."""
    from imt_access.jobs import run_analysis

    with console.status("Analyzing catalog..."):
        result = asyncio.run(run_analysis())
    if not result.get("ok"):
        console.print(f"[red][FAIL] Analysis failed:[/red] {result.get('error') or 'no LLM available'}")
        sys.exit(1)
    console.print(f"[dim]({result['resources']} resources analyzed)[/dim]")
    console.print(result["report"])


@cli.command()
@click.option("--dry-run", is_flag=True, help="Fetch and preview without sending")
@click.option("--force", is_flag=True, help="Send even if already sent today")
def daily(dry_run: bool, force: bool) -> None:
    """Fetch EPW + FT + ET Prime and deliver the daily digest."""
    from imt_access.jobs import run_daily_job

    bot = None if dry_run else _make_bot()
    with console.status("Running daily job..."):
        result = asyncio.run(run_daily_job(bot, dry_run=dry_run, force=force))
    if result.get("skipped"):
        console.print(f"[yellow]Skipped:[/yellow] {result['skipped']}")
        return
    if not result.get("ok"):
        console.print(f"[red][FAIL] Daily job failed:[/red] {result.get('error')}")
        sys.exit(1)

    console.print(
        f"[green][OK] Daily job done[/green] - EPW {result.get('epw_articles')} articles, "
        f"FT {result.get('ft_headlines')}, ET Prime {result.get('et_prime_stories')}"
    )
    if dry_run and result.get("preview"):
        console.print("\n[bold]Preview:[/bold]\n")
        console.print(result["preview"])
    elif result.get("delivery"):
        console.print(f"Delivery: {result['delivery']}")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
