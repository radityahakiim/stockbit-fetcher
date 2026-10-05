# IDX Trading Assistant (CLI)

Monitors Indonesian (IDX) stocks in near-real-time and labels each one **WORTH TO BUY / NEUTRAL / NOT WORTH TO BUY** based on fundamentals (revenue growth, net income growth, margins, EPS, ROE, ROA, PER, PBV, DER, liquidity, Piotroski F-Score, Altman Z-Score, and more).

Data comes from **Stockbit through your own Chromium-based browser** (Edge or Chrome). The script attaches to a browser you already have open and logged in, navigates the symbol pages, and reads the JSON the page itself loads — with a fallback that parses the rendered **Key Stats** panels directly. **No backend/direct HTTP fetch from Python** — everything rides on your real browser session and cookies.

## Setup

```bash
pip install playwright rich
```

(No `playwright install` needed — it attaches to your existing browser.)

### 1. Launch Edge with remote debugging

Close all Edge windows first, then run (Windows):

```
"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\EdgeCDP"
```

Chrome works too:

```
chrome --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\ChromeCDP"
```

### 2. Log in to Stockbit

In that Edge window, go to https://stockbit.com and sign in.

### 3. Run the assistant

```bash
python main.py BBCA BBRI TLKM ASII            # live monitor, refresh every 5 min
python main.py --trending --top 5             # monitor Stockbit's Trending Stocks strip
python main.py --portfolio                    # monitor & score the stocks you own
python main.py --watchlist                    # monitor your Stockbit watchlist
python main.py --movers                       # monitor Movers: Top Value/Volume/Frequency
python main.py BBCA --trending                # your picks + whatever is trending
python main.py BBCA BBRI --once --verbose     # one scan with reasons per verdict
python main.py BBCA --interval 60 --cdp http://localhost:9333
python main.py BBCA --page-wait 30              # optional: raise the max wait cap
```

## GUI version (Tkinter)

```bash
python gui.py
```

A classic-style desktop GUI with two screens:

1. **Setup screen (shown on open):** a checklist of what to monitor — My portfolio, My Watchlist, Trending Stocks, Movers (6 categories), Single scan only — plus a field for extra tickers, and timing/connection inputs (refresh interval, page-load wait, trending count, CDP endpoint). Defaults match the CLI.
2. **Monitor screen:** the same live table, updated after every stock — color-coded verdicts (green HOLD/WORTH TO BUY, red SELL/NOT WORTH TO BUY, amber NEUTRAL), owned stocks tagged "(In Portfolio)" with Avg and P&L%, a status line with countdown, **Pause Timer** and **Refresh** buttons (pause freezes the countdown; refresh starts a new cycle immediately), and a "Why" panel that shows the scoring reasons when you select a row. "Stop & Back to Setup" returns to the checklist.

The GUI runs the same `fetcher.py` + `scoring.py` engine on a background thread, so the window stays responsive during page loads. Tkinter ships with Python on Windows; on Linux install `python3-tk`.

## How long each fetch takes

Each stock is fetched **as soon as its real data arrives** — the tool polls the page every ~350ms and stops the moment enough fundamentals (or at least a price) have been captured, rather than sleeping a fixed duration. A fast-loading page returns in a couple of seconds; a slow one keeps polling.

`--page-wait` (CLI) / "Page load wait cap" (GUI) is **optional** and only matters as a safety ceiling — the longest the tool will wait before giving up on a stock. Leave it unset and a built-in ~20s cap protects against a page that never responds; set it explicitly only if you want a shorter or longer cap than that.

## Live table

The table **re-renders after every single stock page finishes loading** — you don't wait for the whole watchlist:

- the row being fetched shows a ⟳ marker; not-yet-fetched tickers show as `queued`
- previous results stay visible while a ticker re-fetches; each row's **Updated** column shows when *that* row was last refreshed
- the title bar shows live status: `refreshing 3/10`, `discovering trending stocks…`, and a `next refresh in Ns` countdown between cycles — during the countdown press **P** to pause/resume the timer and **R** to refresh immediately (shown in the status line; needs a real terminal, not piped output)
- `--verbose` adds a reasons line under each row (e.g. `+ high ROE · − revenue declining`)

## Trending mode

With `--trending`, each cycle starts by opening `stockbit.com/stream` and reading the **Trending Stocks** carousel at the top. Tickers come from the trending JSON the page loads; if that fails, the tool reads the strip in the DOM and **pages through the carousel** (arrow click, else horizontal scroll) until it has `--top` tickers. The list is re-discovered every cycle, so stocks that cool off drop out and new movers appear automatically. If discovery fails mid-session, the previous list is kept.

Heads-up on pacing: each ticker is a real page load (~8–10 s), so `--top 10` means a full pass takes ~1.5 minutes — set `--interval` comfortably above that.

## Portfolio mode

```bash
python main.py --portfolio                    # score the stocks you own
python main.py --portfolio --trending         # owned + trending together
python main.py --portfolio BBRI               # owned + manual picks
```

With `--portfolio`, each cycle starts by opening your Stockbit **Portfolio** page and reading your holdings from the JSON it loads (avg price, lots, unrealized P&L when available; plain ticker list as fallback). Owned stocks are listed **first**, tagged **(In Portfolio)**, and get two extra columns: **Avg** (your average price) and **P&L%** (Stockbit's unrealized % if provided, otherwise computed from the live price vs your average). Holdings are re-read every cycle, so buys/sells reflect automatically. Owned stocks use owner-framed labels on the same thresholds: **HOLD / NEUTRAL / SELL** instead of WORTH TO BUY / NEUTRAL / NOT WORTH TO BUY, and are tagged **(In Portfolio)** beside the ticker. A SELL label means the fundamentals score poorly right now — treat it as a cue to re-examine the position (your cost basis, thesis, and taxes are yours to weigh), not an automatic order.

## Watchlist mode

```bash
python main.py --watchlist                    # score your Stockbit watchlist
python main.py --watchlist --portfolio        # watchlist + owned, merged & deduped
```

With `--watchlist` (or the Watchlist checkbox in the GUI), each cycle opens `stockbit.com/watchlist` and reads the Symbol column of your watchlist table, in display order.

### One row per stock across all sources

Portfolio, watchlist, manual tickers, trending, and movers are merged into a single de-duplicated list — **a stock that appears in more than one source is shown once**, at its earliest source position. The precedence is:

1. **Portfolio** (owned — so it keeps its HOLD/NEUTRAL/SELL label and (In Portfolio) tag)
2. **Watchlist**
3. **Manual tickers**
4. **Trending**
5. **Movers**

So if BBCA is both on your watchlist and trending, it's fetched and scored once, listed in the watchlist position; if you also own it, it stays in the portfolio position with owned framing.

## Movers mode

```bash
python main.py --movers                       # Value/Volume/Frequency + IEP/IEV + Net Foreign Buy/Sell
python main.py --portfolio --trending --movers --top 5
```

With `--movers` (or the Movers checkbox in the GUI), each cycle also opens the **Movers panel** on `stockbit.com/stream` — the fire icon in the right rail — selecting each category from the panel's ≡ dropdown — **Top Value, Top Volume, Top Frequency, IEP/IEV, Net Foreign Buy, and Net Foreign Sell** — and collecting up to `--top` tickers from each (deduped across categories). **Top Gainer and Top Loser are intentionally skipped.** If the panel isn't already open, the tool tries to click the fire icon; if Stockbit shows the panel collapsed and it can't be opened, movers are skipped for that cycle without breaking the rest of the watchlist.

## How the verdict works

Each captured metric earns points on a threshold ladder (see `scoring.py` — every number is editable):

| Group | Metric | Best case | Worst case |
|---|---|---|---|
| Growth | Revenue growth (Quarter YoY) | > 15% → +2 | negative → −2 |
| Growth | Net income growth (Quarter YoY) | > 20% → +2 | negative → −2 |
| Growth | EPS growth | > 15% → +1 | negative → −1 |
| Profitability | Net profit margin | > 15% → +2 | negative → −2 |
| Profitability | ROE | > 15% → +2 | negative → −2 |
| Profitability | ROA | > 10% → +1 | negative → −1 |
| Valuation | PER (TTM) | 0–10 → +2 | negative earnings → −2 |
| Valuation | PBV | < 1 → +1 | > 4 → −1 |
| Safety | DER | < 0.8 → +1 | > 2 → −1 |
| Safety | Current ratio | ≥ 1.5 → +1 | < 1 → −1 |
| Safety | Interest coverage | ≥ 3 → +1 | < 1.5 → −1 |
| Safety | Altman Z-Score | ≥ 3 → +1 | < 1.8 → −1 |
| Quality | Piotroski F-Score | ≥ 7 → +2 | < 4 → −1 |
| Bonus | Dividend yield | ≥ 4% → +1 | — |
| Veto input | EPS | positive → 0 | negative → −2 |

The score is **normalized by the max possible for the metrics actually found** (shown as e.g. `6/19`), so missing data doesn't skew the verdict. Then:

- ratio ≥ **0.45** → WORTH TO BUY
- ratio ≤ **−0.15** → NOT WORTH TO BUY
- otherwise → NEUTRAL
- fewer than 3 metrics → INSUFFICIENT DATA
- **Veto:** negative EPS *and* shrinking net income is never labeled a buy, regardless of score (classic value trap).

## How metrics are extracted

Field matching uses the **exact labels on Stockbit's Key Stats page** with priority tiers — e.g. `Current PE Ratio (TTM)` is preferred over `(Annualised)`, growth comes from `Revenue (Quarter YoY Growth)`, quality from `Piotroski F-Score` and `Altman Z-Score (Modified)`. Values like `4,331 B`, `(107 B)`, and `28.77%` are parsed correctly. If the JSON capture yields fewer than 6 metrics, the tool falls back to parsing the rendered Key Stats panels as label/value pairs.

## Notes & troubleshooting

- **"Could not attach"** — the browser isn't running with `--remote-debugging-port=9222`, or another port is in use. Verify by opening http://localhost:9222/json in that browser.
- **"login page" error** — sign in to Stockbit inside the attached window; the script never handles credentials.
- **Missing metrics** — Stockbit renames fields occasionally. Add new label fragments to `METRIC_KEYWORDS` in `fetcher.py` (primary list = exact label, fallback list = loose match).
- **Trending list empty** — make sure the desktop layout is showing (the Trending Stocks strip only appears on the desktop view of `/stream`).
- **Rate/pacing** — each ticker takes ~8–10 s (a real page load). Keep watchlists modest and intervals reasonable; you're using your own account session.
- Respect Stockbit's Terms of Service regarding automated access of your account.

## Disclaimer

The labels are transparent heuristics on point-in-time fundamentals — a screening aid, not investment advice or a prediction. Fundamentals ignore news, sentiment, sector cycles, and price action. Always do your own research before trading.
