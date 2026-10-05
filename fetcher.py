"""
fetcher.py — Pulls IDX stock data from Stockbit *through the user's own
Chromium-based browser* (Edge/Chrome) instead of any backend HTTP call.

How it works
------------
1. You launch Edge with a remote-debugging port open (see README).
2. This module attaches to that running browser via CDP
   (Playwright's connect_over_cdp), so it reuses YOUR logged-in
   Stockbit session and cookies.
3. It navigates a tab to stockbit.com/symbol/<TICKER>/keystats and simply
   *listens* to the JSON responses the page itself loads (keystats,
   fundamentals, price). No requests are forged from Python.
4. Metrics are extracted heuristically by name-matching, so minor
   Stockbit API changes usually don't break it.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from playwright.sync_api import sync_playwright, Page, Browser

CDP_URL_DEFAULT = "http://localhost:9222"

# Substrings that identify Stockbit's data endpoints (their internal API
# host is exodus.stockbit.com; we match loosely on purpose).
INTERESTING_URL_PARTS = (
    "stockbit.com",  # keep broad; we filter by content later
)

# metric-key -> (primary fragments, fallback fragments), all lowercase.
# Primary fragments are the EXACT labels on Stockbit's Key Stats page, so
# e.g. "Current PE Ratio (TTM)" is preferred over "(Annualised)".
METRIC_KEYWORDS: dict[str, tuple[list[str], list[str]]] = {
    "revenue_growth":    (["revenue (quarter yoy", "revenue (annual yoy"],
                          ["revenue growth", "sales growth"]),
    "net_income_growth": (["net income (quarter yoy", "net income (annual yoy"],
                          ["net income growth", "profit growth", "earning growth"]),
    "npm":               (["net profit margin"], []),
    "gpm":               (["gross profit margin"], []),
    "opm":               (["operating profit margin"], ["operating margin"]),
    "eps":               (["current eps (ttm)"],
                          ["current eps", "earnings per share", "basic eps"]),
    "eps_growth":        (["eps growth"], []),
    "roe":               (["return on equity (ttm)"], ["return on equity", " roe "]),
    "roa":               (["return on assets (ttm)"], ["return on assets"]),
    "per":               (["current pe ratio (ttm)"],
                          ["current pe ratio", "pe ratio", "price earnings ratio", "p/e"]),
    "pbv":               (["current price to book value"], ["price to book", "pbv"]),
    "der":               (["debt to equity ratio (quarter)"], ["debt to equity"]),
    "current_ratio":     (["current ratio (quarter)"], ["current ratio"]),
    "interest_coverage": (["interest coverage"], []),
    "piotroski":         (["piotroski f-score"], ["piotroski"]),
    "altman_z":          (["altman z-score"], ["altman z"]),
    "dividend_yield":    (["dividend yield"], ["div yield"]),
    "market_cap":        (["market cap"], []),
    "net_income":        (["net income (ttm)"], []),
    "revenue":           (["revenue (ttm)"], []),
}


def extract_metrics(pairs: list[tuple[str, Any]]) -> dict[str, float]:
    """Map (name, value) pairs to metrics; primary labels beat fallbacks."""
    norm = [(" " + n.lower().strip() + " ", v) for n, v in pairs if isinstance(n, str)]
    out: dict[str, float] = {}
    for key, (primary, fallback) in METRIC_KEYWORDS.items():
        val: Optional[float] = None
        for frags in (primary, fallback):
            for frag in frags:
                for name, raw in norm:
                    if frag in name:
                        num = parse_number(raw)
                        if num is not None:
                            val = num
                            break
                if val is not None:
                    break
            if val is not None:
                break
        if val is not None:
            out[key] = val
    return out

PRICE_KEYS = ("lastprice", "last_price", "last", "close", "price")


@dataclass
class StockSnapshot:
    ticker: str
    price: Optional[float] = None
    change_pct: Optional[float] = None
    metrics: dict[str, float] = field(default_factory=dict)
    raw_names_seen: int = 0
    error: Optional[str] = None
    fetched_at: float = field(default_factory=time.time)


# --------------------------------------------------------------------------
# value parsing helpers
# --------------------------------------------------------------------------

_SUFFIX = {"t": 1e12, "b": 1e9, "m": 1e6, "k": 1e3}


def parse_number(raw: Any) -> Optional[float]:
    """Parse Stockbit-style values: '12.34%', '1,234', '(5.6)', '2.1T', 4.2"""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip().lower()
    if not s or s in {"-", "--", "n/a", "na", "null"}:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("%", "").replace("rp", "").replace(",", "").strip()
    mult = 1.0
    if s and s[-1] in _SUFFIX:
        mult = _SUFFIX[s[-1]]
        s = s[:-1]
    try:
        val = float(s) * mult
    except ValueError:
        return None
    return -val if neg else val


# --------------------------------------------------------------------------
# recursive JSON walkers
# --------------------------------------------------------------------------

def _walk_name_value_pairs(node: Any, out: list[tuple[str, Any]]) -> None:
    """Collect anything shaped like {'name'/'title'/'label': X, 'value'/'result': Y}."""
    if isinstance(node, dict):
        name = None
        for nk in ("name", "title", "label", "display_name", "item"):
            if isinstance(node.get(nk), str):
                name = node[nk]
                break
        if name is not None:
            for vk in ("value", "result", "display", "raw", "amount", "values"):
                if vk in node:
                    v = node[vk]
                    if isinstance(v, list) and v:
                        v = v[0] if not isinstance(v[0], dict) else v[0].get("value")
                    out.append((name, v))
                    break
        for v in node.values():
            _walk_name_value_pairs(v, out)
    elif isinstance(node, list):
        for item in node:
            _walk_name_value_pairs(item, out)


def _walk_flat_keys(node: Any, out: list[tuple[str, Any]], depth: int = 0) -> None:
    """Also collect plain key: scalar pairs (e.g. {'roe': 12.3})."""
    if depth > 8:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (int, float, str)) and isinstance(k, str):
                out.append((k.replace("_", " "), v))
            else:
                _walk_flat_keys(v, out, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _walk_flat_keys(item, out, depth + 1)


def _find_price(node: Any, ticker: str, depth: int = 0) -> tuple[Optional[float], Optional[float]]:
    """Look for a last-price / change-percent pair in captured JSON."""
    if depth > 8:
        return None, None
    if isinstance(node, dict):
        keys_lower = {k.lower(): k for k in node.keys() if isinstance(k, str)}
        # prefer dicts that mention the symbol or look like a quote
        price = None
        for pk in PRICE_KEYS:
            if pk in keys_lower:
                price = parse_number(node[keys_lower[pk]])
                if price:
                    break
        chg = None
        for ck in ("percentage_change", "change_pct", "percent", "change_percentage", "percentagechange"):
            if ck in keys_lower:
                chg = parse_number(node[keys_lower[ck]])
                break
        if price and price > 1:  # IDX prices are integers >= 50 mostly
            return price, chg
        for v in node.values():
            p, c = _find_price(v, ticker, depth + 1)
            if p:
                return p, c
    elif isinstance(node, list):
        for item in node:
            p, c = _find_price(item, ticker, depth + 1)
            if p:
                return p, c
    return None, None


# --------------------------------------------------------------------------
# main fetcher
# --------------------------------------------------------------------------

class StockbitBrowserFetcher:
    # Data-readiness polling: fetch() waits for real data instead of a fixed
    # sleep. `page_wait_s` (constructor arg / --page-wait / GUI field) is now
    # OPTIONAL — when None, DEFAULT_MAX_WAIT_S is used purely as a safety
    # ceiling so a dead page can't hang forever; it does not slow down a
    # page that responds quickly.
    DEFAULT_MAX_WAIT_S = 20.0
    POLL_INTERVAL_S = 0.35
    MIN_METRICS_FOR_READY = 3

    def __init__(self, cdp_url: str = CDP_URL_DEFAULT, page_wait_s: Optional[float] = None):
        self.cdp_url = cdp_url
        self.page_wait_s = page_wait_s  # None = use DEFAULT_MAX_WAIT_S as the cap
        self._pw = None
        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None
        self._captured: list[Any] = []

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> None:
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
        except Exception as e:
            raise ConnectionError(
                f"Could not attach to a Chromium browser at {self.cdp_url}.\n"
                "Start Edge with remote debugging first, e.g.:\n"
                '  msedge --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\\EdgeCDP"\n'
                "then log in to stockbit.com in that window."
            ) from e
        ctx = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
        self._page = ctx.new_page()
        self._page.on("response", self._on_response)

    def close(self) -> None:
        try:
            if self._page:
                self._page.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    # -- capture -----------------------------------------------------------
    def _on_response(self, response) -> None:
        url = response.url
        if not any(part in url for part in INTERESTING_URL_PARTS):
            return
        ctype = (response.headers or {}).get("content-type", "")
        if "json" not in ctype:
            return
        try:
            self._captured.append(response.json())
        except Exception:
            pass

    # -- public API ---------------------------------------------------------
    def fetch(self, ticker: str) -> StockSnapshot:
        snap = StockSnapshot(ticker=ticker.upper())
        if not self._page:
            snap.error = "not connected"
            return snap

        self._captured.clear()
        try:
            self._page.goto(f"https://stockbit.com/symbol/{snap.ticker}/keystats",
                            wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            snap.error = f"navigation failed: {e}"
            return snap

        # Brief settle so the page's first XHRs can fire
        try:
            self._page.wait_for_timeout(700)
        except Exception:
            pass

        if "login" in self._page.url or "/#/login" in self._page.url:
            snap.error = "Stockbit shows the login page — sign in inside the Edge window."
            return snap

        # Poll until actual stock data has arrived, instead of sleeping a
        # fixed duration. `page_wait_s` is now an OPTIONAL safety cap — if
        # set, it's the longest we'll wait before giving up; if left unset,
        # a sensible built-in ceiling protects against a page that never
        # responds. Either way, we stop as soon as real data shows up.
        ceiling = self.page_wait_s if self.page_wait_s is not None else self.DEFAULT_MAX_WAIT_S
        deadline = time.time() + max(ceiling, 1.0)
        pairs: list[tuple[str, Any]] = []
        while True:
            pairs = self._collect_pairs()
            metrics = extract_metrics(pairs)
            price, change_pct = self._price_from_captured(snap.ticker)
            ready = len(metrics) >= self.MIN_METRICS_FOR_READY or (price is not None and metrics)
            if ready or time.time() >= deadline:
                snap.metrics, snap.price, snap.change_pct = metrics, price, change_pct
                break
            self._page.wait_for_timeout(int(self.POLL_INTERVAL_S * 1000))

        snap.raw_names_seen = len(pairs)

        # If JSON capture came up thin, read the Key Stats panels straight
        # off the page (label/value lines like "Return on Equity (TTM) 54.98%").
        if len(snap.metrics) < 6:
            dom_pairs = self._pairs_from_keystats_dom()
            for key, val in extract_metrics(dom_pairs).items():
                snap.metrics.setdefault(key, val)

        # DOM fallback for price if the JSON hunt failed.
        if snap.price is None:
            snap.price = self._price_from_dom()

        if not snap.metrics and snap.price is None:
            snap.error = ("no data captured — is the symbol valid and are you "
                          "logged in to Stockbit in the attached browser?")
        return snap

    def _collect_pairs(self) -> list[tuple[str, Any]]:
        """Re-walk everything captured so far into (name, value) pairs."""
        pairs: list[tuple[str, Any]] = []
        for payload in self._captured:
            _walk_name_value_pairs(payload, pairs)
            _walk_flat_keys(payload, pairs)
        return pairs

    def _price_from_captured(self, ticker: str) -> tuple[Optional[float], Optional[float]]:
        for payload in self._captured:
            p, c = _find_price(payload, ticker)
            if p:
                return p, c
        return None, None


    # -- trending discovery ---------------------------------------------
    # The "Trending Stocks" strip lives at the top of /stream (desktop view).
    TRENDING_URLS = (
        "https://stockbit.com/stream",
        "https://stockbit.com/trending",
    )
    _TICKER_RE = re.compile(r"^[A-Z]{4}$")
    _CASHTAG_RE = re.compile(r"\$([A-Z]{4})\b")
    _PLAIN_TICKER_RE = re.compile(r"\b([A-Z]{4})\b")

    def fetch_trending(self, limit: int = 10) -> list[str]:
        """Open Stockbit's stream page in the attached browser and read the
        'Trending Stocks' strip — from the JSON it loads, else from the DOM."""
        if not self._page:
            return []
        found: list[str] = []

        for url in self.TRENDING_URLS:
            self._captured.clear()
            try:
                self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                self._page.wait_for_timeout(4000)
            except Exception:
                continue

            # 1) hunt symbols inside captured JSON payloads that look like
            #    a trending/movers feed (keeps stream chatter out)
            for payload in self._captured:
                if self._looks_like_trending(payload):
                    self._walk_symbols(payload, found)

            # 2) DOM: scope to the section headed "Trending Stocks" so we
            #    don't pick up tickers mentioned in stream posts below it.
            if len(found) < limit:
                found += [t for t in self._trending_from_dom(limit) if t not in found]

            # 3) last resort: $XXXX cashtags anywhere on the page
            if len(found) < 3:
                try:
                    body = self._page.inner_text("body", timeout=3000)
                    for m in self._CASHTAG_RE.finditer(body):
                        if m.group(1) not in found:
                            found.append(m.group(1))
                except Exception:
                    pass
            if found:
                break

        return found[:limit]

    # -- watchlist discovery --------------------------------------------
    WATCHLIST_URLS = (
        "https://stockbit.com/watchlist",
        "https://stockbit.com/#/watchlist",
    )

    def fetch_watchlist(self, limit: int = 50) -> list[str]:
        """Open your Stockbit Watchlist page and return the symbols in the
        leftmost column, in display order (JSON first, DOM table fallback)."""
        if not self._page:
            return []
        found: list[str] = []
        for url in self.WATCHLIST_URLS:
            self._captured.clear()
            try:
                self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                self._page.wait_for_timeout(4000)
            except Exception:
                continue
            if "login" in self._page.url:
                break

            # 1) JSON: watchlist feeds carry symbol lists
            for payload in self._captured:
                if self._looks_like_watchlist(payload):
                    self._walk_symbols(payload, found)

            # 2) DOM fallback: read the Symbol column of the table. Each row
            #    starts with the ticker, so take the first ticker per line.
            if len(found) < 3:
                found += [t for t in self._watchlist_from_dom(limit) if t not in found]
            if found:
                break
        return found[:limit]

    @staticmethod
    def _looks_like_watchlist(payload: Any) -> bool:
        try:
            return "watchlist" in json.dumps(payload)[:4000].lower()
        except Exception:
            return False

    def _watchlist_from_dom(self, limit: int) -> list[str]:
        out: list[str] = []
        try:
            body = self._page.inner_text("body", timeout=3000)
        except Exception:
            return out
        # Column headers / chrome that also match a 4-letter pattern -> skip
        skip = {"PREV", "OPEN", "HIGH", "SORT"}
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            m = self._PLAIN_TICKER_RE.match(line)  # anchored at line start
            if m:
                tk = m.group(1)
                if tk not in skip and tk not in out:
                    out.append(tk)
            if len(out) >= limit:
                break
        return out

    # -- portfolio discovery --------------------------------------------
    # Real location (desktop): stockbit.com/securities/portfolio
    PORTFOLIO_URLS = (
        "https://stockbit.com/securities/portfolio",
        "https://stockbit.com/portfolio",
    )
    # Portfolio symbols can carry suffixes: PADI-R (rights), XXXX-W (warrants)
    _PF_TICKER_RE = re.compile(r"^[A-Z]{4}(?:-[A-Z]{1,2})?$")
    _AVG_KEYS = ("average_price", "avg_price", "averageprice", "avg", "average")
    _QTY_KEYS = ("lot", "lots", "balance_lot", "shares", "share", "balance", "quantity", "qty")
    _PNL_KEYS = ("unrealized_percent", "gain_percent", "profit_loss_percent",
                 "percentage_gain", "pl_percent", "unrealized_pct", "percentage")

    def fetch_portfolio(self) -> dict[str, dict]:
        """Open your Stockbit Portfolio page and return owned stocks as
        {ticker: {avg_price, qty, pnl_pct, cur_price}} (details best-effort)."""
        if not self._page:
            return {}
        holdings: dict[str, dict] = {}

        for url in self.PORTFOLIO_URLS:
            self._captured.clear()
            try:
                self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                self._page.wait_for_timeout(4000)
            except Exception:
                continue
            if "login" in self._page.url:
                break

            # 1) JSON: dicts that carry a symbol AND holding-ish fields
            for payload in self._captured:
                self._walk_holdings(payload, holdings)

            # 2) structured DOM: the holdings table row layout is
            #    Symbol | Balance Lot | Available Lot | Average Price |
            #    Current Price | Invested | Market Value | P/L | Percentage
            if not holdings:
                holdings = self._portfolio_from_dom()

            # 3) last resort: any tickers rendered on the page
            if not holdings:
                try:
                    body = self._page.inner_text("body", timeout=3000)
                    for m in self._PLAIN_TICKER_RE.finditer(body):
                        holdings.setdefault(m.group(1), {})
                except Exception:
                    pass
            if holdings:
                break
        return holdings

    def _portfolio_from_dom(self) -> dict[str, dict]:
        holdings: dict[str, dict] = {}
        try:
            body = self._page.inner_text("body", timeout=3000)
        except Exception:
            return holdings
        tokens = [tok for line in body.splitlines() for tok in line.split() if tok]
        sym, nums = None, []

        def flush():
            if sym and len(nums) >= 4:
                # [balance_lot, available_lot, avg, cur_price, invested, mkt_value, pl, pct]
                holdings[sym] = {"qty": nums[0], "avg_price": nums[2],
                                 "cur_price": nums[3]}
                if len(nums) >= 8:
                    holdings[sym]["pnl_pct"] = nums[7]

        for tok in tokens:
            if self._PF_TICKER_RE.match(tok) and not tok.isdigit():
                flush()
                sym, nums = tok, []
            elif sym is not None:
                if len(tok) == 1 and tok.isalpha():
                    continue  # the [C]/[R] badge next to the symbol
                num = parse_number(tok)
                if num is not None:
                    nums.append(num)
                elif tok[0].isalpha() and len(tok) > 1:
                    flush()   # hit the next column header / Action area
                    sym, nums = None, []
        flush()
        return holdings

    def _walk_holdings(self, node: Any, out: dict[str, dict], depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(node, dict):
            lower = {k.lower(): k for k in node.keys() if isinstance(k, str)}
            sym = None
            for sk in ("symbol", "ticker", "code", "company_symbol"):
                v = node.get(lower.get(sk, ""), None)
                if isinstance(v, str):
                    v = v.upper().lstrip("$")
                    if self._PF_TICKER_RE.match(v):
                        sym = v
                        break
            if sym is None:
                # symbol sometimes nests one level down, e.g. {"company": {"ticker": ...}, "avg": ...}
                for child in node.values():
                    if isinstance(child, dict):
                        for sk in ("symbol", "ticker", "code", "company_symbol"):
                            v = child.get(sk) or child.get(sk.upper())
                            if isinstance(v, str):
                                v = v.upper().lstrip("$")
                                if self._PF_TICKER_RE.match(v):
                                    sym = v
                                    break
                        if sym:
                            break
            if sym:
                entry: dict = {}
                for keys, field in ((self._AVG_KEYS, "avg_price"),
                                    (self._QTY_KEYS, "qty"),
                                    (self._PNL_KEYS, "pnl_pct")):
                    for k in keys:
                        if k in lower:
                            num = parse_number(node[lower[k]])
                            if num is not None:
                                entry[field] = num
                                break
                if entry:  # only count as a holding if it has real details
                    cur = out.setdefault(sym, {})
                    for k, v in entry.items():
                        cur.setdefault(k, v)
            for v in node.values():
                self._walk_holdings(v, out, depth + 1)
        elif isinstance(node, list):
            for item in node:
                self._walk_holdings(item, out, depth + 1)

    # -- movers discovery (right-rail fire-icon panel) -------------------
    # Categories live in a dropdown (the ≡ icon in the Movers header).
    # Top Gainer and Top Loser are skipped on purpose.
    MOVERS_TABS = ("Top Value", "Top Volume", "Top Frequency",
                   "IEP/IEV", "Net Foreign Buy", "Net Foreign Sell")

    def fetch_movers(self, limit: int = 10) -> list[str]:
        """Open the Movers panel on stockbit.com/stream (the fire icon in the
        right rail) and collect tickers from every category except Top
        Gainer / Top Loser. Returns up to `limit` per category, deduped."""
        if not self._page:
            return []
        try:
            if "stockbit.com/stream" not in self._page.url:
                self._page.goto("https://stockbit.com/stream",
                                wait_until="domcontentloaded", timeout=30_000)
            self._page.wait_for_timeout(3000)
        except Exception:
            return []

        # Make sure the Movers panel is open; click the fire icon if not.
        if not self._panel_open("Movers"):
            for sel in ("[aria-label*='mover' i]", "[data-cy*='mover' i]",
                        "[class*='mover' i]", "[aria-label*='fire' i]"):
                try:
                    btns = self._page.locator(sel)
                    if btns.count():
                        btns.first.click(timeout=1500)
                        self._page.wait_for_timeout(1200)
                        break
                except Exception:
                    continue
        if not self._panel_open("Movers"):
            return []

        found: list[str] = []
        for tab in self.MOVERS_TABS:
            if not self._click_movers_tab(tab):
                continue
            self._page.wait_for_timeout(1200)
            tickers = self._section_tickers("Movers")
            found += [t for t in tickers[:limit] if t not in found]
        return found

    def _click_movers_tab(self, tab: str) -> bool:
        """Select a movers category. Categories may be inline tabs OR hidden
        behind the ≡ dropdown in the panel header — try direct click first,
        then open the dropdown and retry."""
        for _attempt in range(2):
            try:
                loc = self._page.get_by_text(tab, exact=False)
                if loc.count():
                    loc.first.click(timeout=1500)
                    return True
            except Exception:
                pass
            self._open_movers_menu()
        return False

    def _open_movers_menu(self) -> None:
        """Click the category-list (≡) control in the Movers header."""
        try:
            heading = self._page.get_by_text("Movers", exact=False)
            if not heading.count():
                return
            node = heading.first
            for _ in range(4):
                node = node.locator("xpath=..")
                try:
                    btns = node.locator("button, [role='button'], svg")
                    if btns.count():
                        btns.last.click(timeout=1200)
                        self._page.wait_for_timeout(600)
                        return
                except Exception:
                    continue
        except Exception:
            pass

    def _panel_open(self, heading: str) -> bool:
        try:
            return bool(self._page.get_by_text(heading, exact=False).count())
        except Exception:
            return False

    def _section_tickers(self, heading_text: str) -> list[str]:
        """Climb ancestors from a heading until the container holds several
        plain 4-letter tickers, then return them in display order."""
        try:
            heading = self._page.get_by_text(heading_text, exact=False)
            if not heading.count():
                return []
            node = heading.first
            for _ in range(6):
                node = node.locator("xpath=..")
                tickers = self._tickers_in(node)
                if len(tickers) >= 3:
                    return tickers
        except Exception:
            pass
        return []

    @staticmethod
    def _looks_like_trending(payload: Any) -> bool:
        try:
            return "trending" in json.dumps(payload)[:4000].lower()
        except Exception:
            return False

    def _trending_from_dom(self, limit: int = 10) -> list[str]:
        """Read the 'Trending Stocks' carousel. Only ~5 cards are visible at
        a time, so after collecting what's on screen we advance the strip
        (arrow click, else horizontal scroll) and collect again."""
        out: list[str] = []
        try:
            heading = self._page.get_by_text("Trending Stocks", exact=False)
            if not heading.count():
                return out

            # climb ancestors until the container's text holds several tickers
            container = heading.first
            for _ in range(6):
                container = container.locator("xpath=..")
                if len(self._tickers_in(container)) >= 3:
                    break
            else:
                return out

            seen_pages_without_new = 0
            for _ in range(12):  # hard cap on pagination steps
                new = [t for t in self._tickers_in(container) if t not in out]
                out += new
                if len(out) >= limit:
                    break
                seen_pages_without_new = 0 if new else seen_pages_without_new + 1
                if seen_pages_without_new >= 2:  # end of carousel
                    break
                if not self._advance_carousel(container):
                    break
        except Exception:
            pass
        return out[:limit]

    def _tickers_in(self, locator) -> list[str]:
        tickers: list[str] = []
        try:
            text = locator.inner_text(timeout=2000)
        except Exception:
            return tickers
        for m in self._PLAIN_TICKER_RE.finditer(text):
            tk = m.group(1)
            if tk not in tickers:
                tickers.append(tk)
        return tickers

    def _advance_carousel(self, container) -> bool:
        """Click the strip's 'next' arrow; if none found, horizontally
        scroll whatever descendant actually overflows."""
        try:
            old_text = container.inner_text(timeout=2000)
        except Exception:
            old_text = ""

        advanced = False
        
        # Hover the container to reveal the navigation buttons if they are hidden
        try:
            container.hover(timeout=1000)
            self._page.wait_for_timeout(300)
        except Exception:
            pass

        # a) an explicit next/right arrow button
        try:
            # The next arrow (>) and prev arrow (<) are often stacked on the right.
            # We want the 'next' arrow, which is typically the topmost one on the right edge.
            clicked = container.evaluate(
                """(root) => {
                    const sel = '[aria-label*="next" i], [aria-label*="right" i], [class*="next" i], button, svg';
                    const els = Array.from(root.querySelectorAll(sel));
                    const rootRect = root.getBoundingClientRect();
                    
                    // Filter for visible elements in the rightmost 20% of the container
                    const rightEdge = els.filter(e => {
                        const r = e.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 && r.right > (rootRect.left + rootRect.width * 0.8);
                    });
                    
                    if (rightEdge.length === 0) return false;
                    
                    // Sort by rightmost, then topmost
                    rightEdge.sort((a, b) => {
                        const ra = a.getBoundingClientRect();
                        const rb = b.getBoundingClientRect();
                        if (Math.abs(ra.right - rb.right) > 5) {
                            return rb.right - ra.right;
                        }
                        return ra.top - rb.top;
                    });
                    
                    // Click the best candidate
                    let target = rightEdge[0];
                    const btn = target.closest('button, [role="button"]');
                    if (btn) target = btn;
                    
                    // If the button is disabled or visually indicates it's at the end, do not click
                    if (target.disabled || target.getAttribute('aria-disabled') === 'true' || 
                        (target.className && typeof target.className === 'string' && target.className.toLowerCase().includes('disabled'))) {
                        return false;
                    }
                    
                    target.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                    return true;
                }"""
            )
            if clicked:
                advanced = True
        except Exception:
            pass

        if not advanced:
            # b) generic: scroll the overflowing child one viewport to the right
            try:
                advanced = container.evaluate(
                    """(root) => {
                        const nodes = [root, ...root.querySelectorAll('*')];
                        for (const el of nodes) {
                            if (el.scrollWidth > el.clientWidth + 20) {
                                const before = el.scrollLeft;
                                el.scrollLeft += el.clientWidth;
                                if (el.scrollLeft !== before) return true;
                            }
                        }
                        return false;
                    }"""
                )
            except Exception:
                pass

        if not advanced:
            return False

        # Wait for the DOM text to actually change to avoid race conditions
        for _ in range(15):
            self._page.wait_for_timeout(200)
            try:
                new_text = container.inner_text(timeout=1000)
                if new_text != old_text:
                    return True
            except Exception:
                pass

        return False

    def _walk_symbols(self, node: Any, out: list[str], depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(node, dict):
            for k in ("symbol", "ticker", "code", "symbol_2", "company_symbol"):
                v = node.get(k)
                if isinstance(v, str):
                    v = v.upper().lstrip("$")
                    if self._TICKER_RE.match(v) and v not in out:
                        out.append(v)
            for v in node.values():
                self._walk_symbols(v, out, depth + 1)
        elif isinstance(node, list):
            for item in node:
                self._walk_symbols(item, out, depth + 1)

    _VALUE_RE = re.compile(r"^\(?-?[\d.,]+\s?[%btmk]?\)?$", re.I)
    _INLINE_RE = re.compile(r"^(.*[A-Za-z)])\s+(\(?-?[\d.,]+\s?[%BTMK]?\)?)$")

    def _pairs_from_keystats_dom(self) -> list[tuple[str, Any]]:
        """Turn the rendered Key Stats page into (label, value) pairs.
        Handles both 'Label  Value' on one line and label/value on
        consecutive lines (how inner_text usually flattens the panels)."""
        pairs: list[tuple[str, Any]] = []
        try:
            body = self._page.inner_text("body", timeout=3000)
        except Exception:
            return pairs
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        i = 0
        while i < len(lines):
            line = lines[i]
            if not self._VALUE_RE.match(line):
                m = self._INLINE_RE.match(line)
                if m:
                    pairs.append((m.group(1).strip(), m.group(2)))
                    i += 1
                    continue
                if i + 1 < len(lines) and self._VALUE_RE.match(lines[i + 1]):
                    pairs.append((line, lines[i + 1]))
                    i += 2
                    continue
            i += 1
        return pairs

    def _price_from_dom(self) -> Optional[float]:
        try:
            body = self._page.inner_text("body", timeout=3000)
            # first plausible IDX price near the top of the page
            m = re.search(r"\b(\d{1,3}(?:,\d{3})+|\d{2,6})(?:\.\d+)?\b", body[:2000])
            if m:
                return parse_number(m.group(0))
        except Exception:
            pass
        return None
