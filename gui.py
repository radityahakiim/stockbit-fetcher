#!/usr/bin/env python3
"""
IDX Trading Assistant — Tkinter GUI (classic style)

First screen: a setup checklist (what to monitor) + timing fields.
Second screen: live monitor table that updates after EVERY stock,
same engine as the CLI (fetcher.py + scoring.py).

Run:  python gui.py
"""

from __future__ import annotations

import queue
import threading
import time
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox

from fetcher import StockbitBrowserFetcher
from scoring import evaluate

COLUMNS = [
    ("ticker",  "Ticker",   130, "w"),
    ("price",   "Price",     70, "e"),
    ("chg",     "Chg%",      60, "e"),
    ("avg",     "Avg",       70, "e"),
    ("pnl",     "P&L%",      65, "e"),
    ("revgr",   "RevGr",     60, "e"),
    ("nigr",    "NI Gr",     60, "e"),
    ("npm",     "NPM",       55, "e"),
    ("eps",     "EPS",       65, "e"),
    ("roe",     "ROE",       55, "e"),
    ("per",     "PER",       60, "e"),
    ("pbv",     "PBV",       50, "e"),
    ("der",     "DER",       50, "e"),
    ("score",   "Score",     55, "e"),
    ("verdict", "Verdict",  130, "w"),
    ("updated", "Updated",   65, "e"),
]

GOOD_LABELS = {"WORTH TO BUY", "HOLD"}
BAD_LABELS = {"NOT WORTH TO BUY", "SELL"}


def fmt(v, pct=False, digits=1):
    if v is None:
        return "-"
    return f"{v:,.{digits}f}{'%' if pct else ''}"


# ==========================================================================
# Background worker: owns the fetcher (Playwright must stay on one thread)
# ==========================================================================

class Worker(threading.Thread):
    def __init__(self, cfg: dict, out: queue.Queue, stop_evt: threading.Event,
                 pause_evt: threading.Event, refresh_evt: threading.Event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.out = out
        self.stop_evt = stop_evt
        self.pause_evt = pause_evt
        self.refresh_evt = refresh_evt

    def emit(self, kind, **payload):
        self.out.put((kind, payload))

    def run(self):
        cfg = self.cfg
        fetcher = StockbitBrowserFetcher(cdp_url=cfg["cdp"], page_wait_s=cfg["page_wait"])
        self.emit("status", text="Attaching to your browser…")
        try:
            fetcher.connect()
        except ConnectionError as e:
            self.emit("fatal", text=str(e))
            return

        holdings: dict[str, dict] = {}
        last_list: list[str] = []
        try:
            while not self.stop_evt.is_set():
                # ---- resolve watchlist -------------------------------
                tickers: list[str] = []
                if cfg["portfolio"]:
                    self.emit("status", text="Reading your portfolio…")
                    found = fetcher.fetch_portfolio()
                    if found:
                        holdings = found
                    tickers += list(holdings)
                tickers += [t for t in cfg["tickers"] if t not in tickers]
                if cfg["trending"]:
                    self.emit("status", text="Discovering trending stocks…")
                    trending = fetcher.fetch_trending(limit=cfg["top"])
                    tickers += [t for t in trending if t not in tickers]
                if cfg["movers"]:
                    self.emit("status", text="Reading Movers (value/volume/frequency)…")
                    movers = fetcher.fetch_movers(limit=cfg["top"])
                    tickers += [t for t in movers if t not in tickers]
                if not tickers:
                    tickers = last_list
                if not tickers:
                    self.emit("fatal", text="Nothing to monitor — no tickers found.")
                    return
                last_list = tickers
                self.emit("watchlist", tickers=tickers, holdings=dict(holdings))

                # ---- fetch each stock, table updates per stock -------
                for i, tk_ in enumerate(tickers, 1):
                    if self.stop_evt.is_set():
                        return
                    self.emit("status", text=f"Refreshing {tk_}  ({i}/{len(tickers)})…")
                    self.emit("fetching", ticker=tk_)
                    snap = fetcher.fetch(tk_)
                    owned = cfg["portfolio"] and tk_ in holdings
                    verdict = evaluate(snap.metrics, owned=owned)
                    self.emit("row", ticker=tk_, snap=snap, verdict=verdict,
                              holding=holdings.get(tk_, {}), owned=owned)

                if cfg["once"]:
                    self.emit("status", text="Done (single scan).")
                    return

                # ---- countdown (Pause freezes it, Refresh skips it) --
                self.refresh_evt.clear()
                remaining = cfg["interval"]
                while remaining > 0:
                    if self.stop_evt.is_set():
                        return
                    if self.refresh_evt.is_set():
                        self.refresh_evt.clear()
                        break
                    if self.pause_evt.is_set():
                        self.emit("status", text=f"Paused — countdown frozen at {remaining}s")
                        time.sleep(0.25)
                        continue
                    self.emit("status", text=f"Next refresh in {remaining}s")
                    time.sleep(1)
                    remaining -= 1
        finally:
            fetcher.close()


# ==========================================================================
# GUI
# ==========================================================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("IDX Trading Assistant")
        self.geometry("1180x560")
        self.queue: queue.Queue = queue.Queue()
        self.stop_evt = threading.Event()
        self.pause_evt = threading.Event()
        self.refresh_evt = threading.Event()
        self.worker: Worker | None = None
        self.reasons: dict[str, str] = {}
        self.holdings: dict[str, dict] = {}

        self.setup_frame = self._build_setup()
        self.monitor_frame = self._build_monitor()
        self.setup_frame.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- setup screen (checklist + timing) ----------------
    def _build_setup(self) -> tk.Frame:
        f = tk.Frame(self, padx=16, pady=12)
        tk.Label(f, text="IDX Trading Assistant — Setup",
                 font=("TkDefaultFont", 13, "bold")).grid(row=0, column=0,
                 columnspan=2, sticky="w", pady=(0, 10))

        # checklist
        box = tk.LabelFrame(f, text=" What to monitor ", padx=10, pady=8)
        box.grid(row=1, column=0, sticky="nwe", padx=(0, 12))
        self.var_portfolio = tk.BooleanVar(value=True)
        self.var_trending = tk.BooleanVar(value=True)
        self.var_movers = tk.BooleanVar(value=False)
        self.var_once = tk.BooleanVar(value=False)
        tk.Checkbutton(box, text="My portfolio (owned stocks — labeled HOLD / NEUTRAL / SELL)",
                       variable=self.var_portfolio, anchor="w").pack(fill="x")
        tk.Checkbutton(box, text="Trending Stocks strip on stockbit.com/stream",
                       variable=self.var_trending, anchor="w").pack(fill="x")
        tk.Checkbutton(box, text="Movers — Value / Volume / Frequency / IEP-IEV / Net Foreign Buy & Sell (no Gainer/Loser)",
                       variable=self.var_movers, anchor="w").pack(fill="x")
        tk.Checkbutton(box, text="Single scan only (no auto-refresh)",
                       variable=self.var_once, anchor="w").pack(fill="x")
        tk.Label(box, text="Extra tickers (space separated):", anchor="w").pack(fill="x", pady=(8, 0))
        self.ent_tickers = tk.Entry(box, width=40)
        self.ent_tickers.pack(fill="x")
        self.ent_tickers.insert(0, "")

        # timing / connection
        tbox = tk.LabelFrame(f, text=" Timing & connection ", padx=10, pady=8)
        tbox.grid(row=1, column=1, sticky="nwe")
        self.ent_interval = self._labeled_entry(tbox, 0, "Refresh interval (seconds):", "300")
        self.ent_pagewait = self._labeled_entry(tbox, 1, "Page load wait (seconds):", "8")
        self.ent_top = self._labeled_entry(tbox, 2, "Trending tickers to track:", "10")
        self.ent_cdp = self._labeled_entry(tbox, 3, "Browser CDP endpoint:", "http://localhost:9222", width=24)

        tk.Label(f, justify="left", fg="#444", text=(
            "Before starting: launch Edge/Chrome with  --remote-debugging-port=9222\n"
            "and log in to stockbit.com in that window. No backend fetch is used —\n"
            "everything is read through your own browser session.")).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(12, 8))

        tk.Button(f, text="Start Monitoring", width=20,
                  command=self._start).grid(row=3, column=0, sticky="w")
        tk.Label(f, fg="#666",
                 text="Heuristic screen — not financial advice.").grid(
                 row=3, column=1, sticky="e")
        return f

    @staticmethod
    def _labeled_entry(parent, row, label, default, width=10) -> tk.Entry:
        tk.Label(parent, text=label, anchor="w").grid(row=row, column=0, sticky="w", pady=2)
        e = tk.Entry(parent, width=width)
        e.grid(row=row, column=1, sticky="w", padx=(8, 0), pady=2)
        e.insert(0, default)
        return e

    # ---------------- monitor screen -----------------------------------
    def _build_monitor(self) -> tk.Frame:
        f = tk.Frame(self, padx=8, pady=6)
        top = tk.Frame(f)
        top.pack(fill="x")
        self.lbl_status = tk.Label(top, text="", anchor="w")
        self.lbl_status.pack(side="left")
        tk.Button(top, text="Stop & Back to Setup", command=self._stop).pack(side="right")
        tk.Button(top, text="Refresh", command=self._refresh_now).pack(side="right", padx=(0, 6))
        self.btn_pause = tk.Button(top, text="Pause Timer", width=12, command=self._toggle_pause)
        self.btn_pause.pack(side="right", padx=(0, 6))

        cols = [c[0] for c in COLUMNS]
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=16)
        for key, title, width, anchor in COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key in ("ticker", "verdict")))
        ysb = ttk.Scrollbar(f, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="top", fill="both", expand=True)
        ysb.place(relx=1.0, rely=0.08, relheight=0.75, anchor="ne")

        # classic row colors via tags
        self.tree.tag_configure("good", foreground="#0a7d00")
        self.tree.tag_configure("bad", foreground="#b00000")
        self.tree.tag_configure("neutral", foreground="#8a6d00")
        self.tree.tag_configure("pending", foreground="#888888")
        self.tree.bind("<<TreeviewSelect>>", self._show_reasons)

        tk.Label(f, text="Why (select a row):", anchor="w").pack(fill="x", pady=(6, 0))
        self.txt_reasons = tk.Text(f, height=3, wrap="word", state="disabled",
                                   bg=self.cget("bg"), relief="sunken")
        self.txt_reasons.pack(fill="x")
        tk.Label(f, fg="#666", anchor="w",
                 text="Heuristic screen from Stockbit fundamentals — not financial advice."
                 ).pack(fill="x")
        return f

    # ---------------- start / stop --------------------------------------
    def _start(self):
        try:
            cfg = {
                "portfolio": self.var_portfolio.get(),
                "trending": self.var_trending.get(),
                "movers": self.var_movers.get(),
                "once": self.var_once.get(),
                "tickers": [t.upper() for t in self.ent_tickers.get().split()],
                "interval": max(5, int(self.ent_interval.get())),
                "page_wait": max(2.0, float(self.ent_pagewait.get())),
                "top": max(1, int(self.ent_top.get())),
                "cdp": self.ent_cdp.get().strip(),
            }
        except ValueError:
            messagebox.showerror("Invalid input", "Interval, page wait and trending count must be numbers.")
            return
        if not (cfg["portfolio"] or cfg["trending"] or cfg["movers"] or cfg["tickers"]):
            messagebox.showerror("Nothing selected",
                                 "Tick portfolio, trending or movers, or type at least one ticker.")
            return

        self.setup_frame.pack_forget()
        self.monitor_frame.pack(fill="both", expand=True)
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.reasons.clear()
        self.stop_evt = threading.Event()
        self.pause_evt = threading.Event()
        self.refresh_evt = threading.Event()
        self.btn_pause.config(text="Pause Timer")
        self.worker = Worker(cfg, self.queue, self.stop_evt, self.pause_evt, self.refresh_evt)
        self.worker.start()
        self.after(150, self._poll)

    def _toggle_pause(self):
        if self.pause_evt.is_set():
            self.pause_evt.clear()
            self.btn_pause.config(text="Pause Timer")
        else:
            self.pause_evt.set()
            self.btn_pause.config(text="Resume Timer")

    def _refresh_now(self):
        # refreshing also un-pauses, so the new cycle starts immediately
        self.pause_evt.clear()
        self.btn_pause.config(text="Pause Timer")
        self.refresh_evt.set()

    def _stop(self):
        self.stop_evt.set()
        self.monitor_frame.pack_forget()
        self.setup_frame.pack(fill="both", expand=True)

    def _on_close(self):
        self.stop_evt.set()
        self.destroy()

    # ---------------- queue -> GUI ---------------------------------------
    def _poll(self):
        try:
            while True:
                kind, p = self.queue.get_nowait()
                if kind == "status":
                    self.lbl_status.config(text=f"{datetime.now():%H:%M:%S}   {p['text']}")
                elif kind == "fatal":
                    messagebox.showerror("Cannot continue", p["text"])
                    self._stop()
                elif kind == "watchlist":
                    self.holdings = p["holdings"]
                    self._sync_rows(p["tickers"])
                elif kind == "fetching":
                    self._mark_fetching(p["ticker"])
                elif kind == "row":
                    self._update_row(p)
        except queue.Empty:
            pass
        if not self.stop_evt.is_set():
            self.after(150, self._poll)

    # ---------------- table helpers --------------------------------------
    def _display_name(self, ticker: str) -> str:
        return f"{ticker}  (In Portfolio)" if ticker in self.holdings else ticker

    def _sync_rows(self, tickers: list[str]):
        existing = set(self.tree.get_children())
        for tk_ in tickers:
            if tk_ not in existing:
                self.tree.insert("", "end", iid=tk_,
                                 values=[self._display_name(tk_)] + ["…"] * 14 + ["queued"],
                                 tags=("pending",))
        for iid in existing:
            if iid not in tickers:
                self.tree.delete(iid)
                self.reasons.pop(iid, None)

    def _mark_fetching(self, ticker: str):
        if self.tree.exists(ticker):
            vals = list(self.tree.item(ticker, "values"))
            vals[0] = self._display_name(ticker) + "  ⟳"
            self.tree.item(ticker, values=vals)

    def _update_row(self, p: dict):
        snap, v, h = p["snap"], p["verdict"], p["holding"]
        tk_ = p["ticker"]
        m = snap.metrics
        avg = h.get("avg_price")
        pnl = h.get("pnl_pct")
        if pnl is None and avg and snap.price:
            pnl = (snap.price - avg) / avg * 100
        chg = snap.change_pct
        verdict = snap.error[:38] if snap.error else v.label
        values = [
            self._display_name(tk_),
            fmt(snap.price, digits=0),
            (f"+{chg:.2f}%" if (chg or 0) > 0 else fmt(chg, pct=True, digits=2)),
            fmt(avg, digits=0) if p["owned"] else "-",
            (f"+{pnl:.2f}%" if (pnl or 0) > 0 else fmt(pnl, pct=True, digits=2)) if p["owned"] else "-",
            fmt(m.get("revenue_growth"), pct=True),
            fmt(m.get("net_income_growth"), pct=True),
            fmt(m.get("npm"), pct=True),
            fmt(m.get("eps")),
            fmt(m.get("roe"), pct=True),
            fmt(m.get("per"), digits=2),
            fmt(m.get("pbv"), digits=2),
            fmt(m.get("der"), digits=2),
            f"{v.score}/{v.max_possible}" if v.max_possible else "-",
            verdict,
            f"{datetime.fromtimestamp(snap.fetched_at):%H:%M:%S}",
        ]
        tag = ("bad" if snap.error else
               "good" if v.label in GOOD_LABELS else
               "bad" if v.label in BAD_LABELS else
               "pending" if v.label == "INSUFFICIENT DATA" else "neutral")
        if self.tree.exists(tk_):
            self.tree.item(tk_, values=values, tags=(tag,))
        else:
            self.tree.insert("", "end", iid=tk_, values=values, tags=(tag,))
        self.reasons[tk_] = " · ".join(
            [f"+ {r}" for r in v.reasons_good] + [f"- {r}" for r in v.reasons_bad]
        ) or (snap.error or "no scored metrics")

    def _show_reasons(self, _evt=None):
        sel = self.tree.selection()
        text = self.reasons.get(sel[0], "") if sel else ""
        self.txt_reasons.config(state="normal")
        self.txt_reasons.delete("1.0", "end")
        self.txt_reasons.insert("1.0", text)
        self.txt_reasons.config(state="disabled")


if __name__ == "__main__":
    App().mainloop()
