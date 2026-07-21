#!/usr/bin/env python3
"""
IDX Trading Assistant (CLI)
Monitors IDX tickers via YOUR logged-in Stockbit session in Edge/Chrome
(attached over CDP — no backend fetch) and labels each stock
WORTH TO BUY / NEUTRAL / NOT WORTH TO BUY from its fundamentals.

The table re-renders after EVERY single stock page finishes loading,
so you see results stream in one by one instead of waiting for the
whole watchlist.

Usage:
  python main.py BBCA BBRI TLKM ASII
  python main.py --trending --top 5
  python main.py BBCA --interval 120 --verbose
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich import box

from fetcher import StockbitBrowserFetcher, StockSnapshot
from scoring import evaluate, Verdict

console = Console()


class KeyPoller:
    """Non-blocking single-key reader. Windows: msvcrt; POSIX: termios+select.
    Silently disabled when stdin isn't a terminal (e.g. piped)."""

    def __init__(self):
        self.enabled = sys.stdin.isatty()
        self._win = sys.platform.startswith("win")
        self._old = None

    def __enter__(self):
        if self.enabled and not self._win:
            try:
                import termios, tty
                self._old = termios.tcgetattr(sys.stdin.fileno())
                tty.setcbreak(sys.stdin.fileno())
            except Exception:
                self.enabled = False
        return self

    def __exit__(self, *exc):
        if self._old is not None:
            try:
                import termios
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old)
            except Exception:
                pass

    def poll(self) -> str | None:
        if not self.enabled:
            return None
        try:
            if self._win:
                import msvcrt
                if msvcrt.kbhit():
                    return msvcrt.getwch().lower()
                return None
            import select
            r, _, _ = select.select([sys.stdin], [], [], 0)
            if r:
                return sys.stdin.read(1).lower()
        except Exception:
            self.enabled = False
        return None

LABEL_STYLE = {
    "WORTH TO BUY": "bold green",
    "NEUTRAL": "yellow",
    "NOT WORTH TO BUY": "bold red",
    "HOLD": "bold green",
    "SELL": "bold red",
    "INSUFFICIENT DATA": "dim",
}


def fmt(v, pct=False, digits=1):
    if v is None:
        return "-"
    return f"{v:,.{digits}f}{'%' if pct else ''}"


def build_table(order: list[str],
                cache: dict[str, tuple[StockSnapshot, Verdict]],
                verbose: bool,
                fetching: str | None = None,
                status: str = "",
                holdings: dict[str, dict] | None = None) -> Table:
    show_pf = holdings is not None
    holdings = holdings or {}
    t = Table(box=box.SIMPLE_HEAVY,
              title=f"IDX Trading Assistant — {datetime.now():%H:%M:%S}"
                    + (f"  [dim]{status}[/]" if status else ""),
              caption="Heuristic screen from Stockbit fundamentals — not financial advice."
                      + ("  Owned stocks are labeled HOLD / NEUTRAL / SELL." if show_pf else ""))
    t.add_column("Ticker", style="bold cyan")
    t.add_column("Price", justify="right")
    t.add_column("Chg%", justify="right")
    if show_pf:
        t.add_column("Avg", justify="right")
        t.add_column("P&L%", justify="right")
    t.add_column("RevGr", justify="right")
    t.add_column("NI Gr", justify="right")
    t.add_column("NPM", justify="right")
    t.add_column("EPS", justify="right")
    t.add_column("ROE", justify="right")
    t.add_column("PER", justify="right")
    t.add_column("PBV", justify="right")
    t.add_column("DER", justify="right")
    t.add_column("Score", justify="right")
    t.add_column("Verdict")
    t.add_column("Updated", justify="right", style="dim")
    ncols = len(t.columns)

    for tk in order:
        owned = tk in holdings
        marker = (" [magenta](In Portfolio)[/]" if owned else "") \
                 + (" [blink]⟳[/]" if tk == fetching else "")
        if tk not in cache:
            t.add_row(f"{tk}{marker}", *["[dim]…[/]"] * (ncols - 2),
                      "[dim]fetching[/]" if tk == fetching else "[dim]queued[/]")
            continue
        snap, v = cache[tk]
        m = snap.metrics
        chg = snap.change_pct
        chg_txt = f"[green]+{chg:.2f}%[/]" if (chg or 0) > 0 else (
                  f"[red]{chg:.2f}%[/]" if (chg or 0) < 0 else fmt(chg, pct=True, digits=2))
        verdict_txt = f"[{LABEL_STYLE.get(v.label, '')}]{v.label}[/]"
        if snap.error:
            verdict_txt = f"[dim red]{snap.error[:40]}[/]"

        row = [f"{tk}{marker}", fmt(snap.price, digits=0), chg_txt]
        if show_pf:
            h = holdings.get(tk, {})
            avg = h.get("avg_price")
            pnl = h.get("pnl_pct")
            if pnl is None and avg and snap.price:
                pnl = (snap.price - avg) / avg * 100
            pnl_txt = "-"
            if pnl is not None:
                pnl_txt = f"[green]+{pnl:.2f}%[/]" if pnl > 0 else (
                          f"[red]{pnl:.2f}%[/]" if pnl < 0 else "0.00%")
            row += [fmt(avg, digits=0) if owned else "-", pnl_txt if owned else "-"]
        row += [
            fmt(m.get("revenue_growth"), pct=True),
            fmt(m.get("net_income_growth"), pct=True),
            fmt(m.get("npm"), pct=True),
            fmt(m.get("eps")),
            fmt(m.get("roe"), pct=True),
            fmt(m.get("per"), digits=2),
            fmt(m.get("pbv"), digits=2),
            fmt(m.get("der"), digits=2),
            f"{v.score}/{v.max_possible}" if v.max_possible else "-",
            verdict_txt,
            f"{datetime.fromtimestamp(snap.fetched_at):%H:%M:%S}",
        ]
        t.add_row(*row)
        if verbose and (v.reasons_good or v.reasons_bad):
            notes = " · ".join([f"[green]+ {r}[/]" for r in v.reasons_good] +
                               [f"[red]- {r}[/]" for r in v.reasons_bad])
            t.add_row(*([""] * (ncols - 2)), notes, "")
    return t


def main() -> int:
    ap = argparse.ArgumentParser(description="IDX trading assistant fed by your Stockbit browser session")
    ap.add_argument("tickers", nargs="*", help="IDX symbols, e.g. BBCA BBRI TLKM (optional with --trending)")
    ap.add_argument("--trending", action="store_true",
                    help="monitor Stockbit's Trending stocks (list re-discovered every refresh)")
    ap.add_argument("--portfolio", action="store_true",
                    help="also monitor the stocks you own (read from your Stockbit portfolio)")
    ap.add_argument("--watchlist", action="store_true",
                    help="also monitor your Stockbit Watchlist")
    ap.add_argument("--movers", action="store_true",
                    help="also monitor Movers (Top Value/Volume/Frequency, IEP/IEV, "
                         "Net Foreign Buy/Sell; Top Gainer and Top Loser are excluded)")
    ap.add_argument("--top", type=int, default=10, help="how many trending/mover tickers to track (default 10)")
    ap.add_argument("--interval", type=int, default=300, help="seconds between full refresh cycles (default 300)")
    ap.add_argument("--once", action="store_true", help="single scan, then exit")
    ap.add_argument("--verbose", action="store_true", help="show the reasons behind each verdict")
    ap.add_argument("--cdp", default="http://localhost:9222", help="CDP endpoint of the running browser")
    ap.add_argument("--page-wait", type=float, default=8.0, help="seconds to let each symbol page load")
    args = ap.parse_args()

    if not args.tickers and not args.trending and not args.portfolio \
            and not args.movers and not args.watchlist:
        console.print("[bold red]Give tickers, or use "
                      "--portfolio / --watchlist / --trending / --movers.[/]")
        return 1

    fetcher = StockbitBrowserFetcher(cdp_url=args.cdp, page_wait_s=args.page_wait)
    console.print("[dim]Attaching to your browser…[/]")
    try:
        fetcher.connect()
    except ConnectionError as e:
        console.print(f"[bold red]{e}[/]")
        return 1

    cache: dict[str, tuple[StockSnapshot, Verdict]] = {}
    last_list: list[str] = []
    holdings: dict[str, dict] = {}

    def resolve_watchlist(live: Live | None = None) -> list[str]:
        nonlocal holdings
        pf_view = holdings if args.portfolio else None
        tickers: list[str] = []
        if args.portfolio:
            if live:
                live.update(build_table(last_list, cache, args.verbose,
                                        status="reading your portfolio…", holdings=pf_view))
            found = fetcher.fetch_portfolio()
            if found:
                holdings = found          # refresh avg/P&L details each cycle
            elif not holdings:
                console.print("[yellow]Couldn't read the portfolio page — "
                              "check you're logged in.[/]")
            tickers += list(holdings)     # owned stocks go first
        if args.watchlist:
            if live:
                live.update(build_table(last_list or tickers, cache, args.verbose,
                                        status="reading your watchlist…", holdings=pf_view))
            for t in fetcher.fetch_watchlist():
                if t not in tickers:
                    tickers.append(t)
        tickers += [t for t in (x.upper() for x in args.tickers) if t not in tickers]
        if args.trending:
            if live:
                live.update(build_table(last_list or tickers, cache, args.verbose,
                                        status="discovering trending stocks…", holdings=pf_view))
            trending = fetcher.fetch_trending(limit=args.top)
            if trending:
                tickers += [t for t in trending if t not in tickers]
            elif not tickers and last_list:
                return last_list  # keep previous list if discovery failed
        if args.movers:
            if live:
                live.update(build_table(last_list or tickers, cache, args.verbose,
                                        status="reading Movers (value/volume/frequency)…",
                                        holdings=pf_view))
            movers = fetcher.fetch_movers(limit=args.top)
            tickers += [t for t in movers if t not in tickers]
        return tickers

    try:
        with KeyPoller() as keys, Live(console=console, refresh_per_second=4) as live:
            while True:
                wl = resolve_watchlist(live) or last_list
                last_list = wl
                pf_view = holdings if args.portfolio else None
                # prune tickers that dropped off the watchlist
                for gone in [k for k in cache if k not in wl]:
                    del cache[gone]

                # >>> incremental refresh: table updates after EACH stock <<<
                for i, tk in enumerate(wl, 1):
                    live.update(build_table(wl, cache, args.verbose, fetching=tk,
                                            status=f"refreshing {i}/{len(wl)}", holdings=pf_view))
                    snap = fetcher.fetch(tk)
                    cache[tk] = (snap, evaluate(snap.metrics,
                                                owned=args.portfolio and tk in holdings))
                    live.update(build_table(wl, cache, args.verbose,
                                            status=f"refreshing {i}/{len(wl)}", holdings=pf_view))

                if args.once:
                    live.update(build_table(wl, cache, args.verbose, holdings=pf_view))
                    break

                # countdown — press P to pause the timer, R to refresh now
                hint = "P=pause · R=refresh" if keys.enabled else ""
                paused, remaining, tick = False, args.interval, 0
                while remaining > 0:
                    ch = keys.poll()
                    if ch == "r":
                        break
                    if ch == "p":
                        paused = not paused
                    if paused:
                        status = f"PAUSED at {remaining}s — P=resume · R=refresh"
                    else:
                        status = f"next refresh in {remaining}s" + (f" — {hint}" if hint else "")
                    live.update(build_table(wl, cache, args.verbose,
                                            status=status, holdings=pf_view))
                    time.sleep(0.2)
                    if not paused:
                        tick += 1
                        if tick >= 5:      # five 0.2s ticks = 1 second
                            tick = 0
                            remaining -= 1
        return 0
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/]")
        return 0
    finally:
        fetcher.close()


if __name__ == "__main__":
    sys.exit(main())
