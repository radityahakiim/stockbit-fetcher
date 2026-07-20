"""
scoring.py — Turns raw fundamentals into a label:
WORTH TO BUY / NEUTRAL / NOT WORTH TO BUY.

The rules are simple, transparent heuristics on classic value/quality
metrics. Tune THRESHOLDS to your own strategy — this is a screening aid,
not financial advice.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

# Each rule: metric -> list of (lower_bound, points, note)
# Bounds are checked top-down; first match wins. Percent metrics are in %.
RULES: dict[str, list[tuple[float, int, str]]] = {
    # growth
    "revenue_growth": [
        (15,   +2, "strong revenue growth"),
        (5,    +1, "decent revenue growth"),
        (0,     0, "flat revenue"),
        (-1e9, -2, "revenue declining"),
    ],
    "net_income_growth": [
        (20,   +2, "strong profit growth"),
        (5,    +1, "decent profit growth"),
        (0,     0, "flat profit"),
        (-1e9, -2, "profit declining"),
    ],
    "eps_growth": [
        (15,   +1, "EPS growing"),
        (0,     0, "EPS flat"),
        (-1e9, -1, "EPS shrinking"),
    ],
    # profitability
    "npm": [
        (15,   +2, "fat net margin"),
        (8,    +1, "healthy net margin"),
        (3,     0, "thin net margin"),
        (0,    -1, "very thin margin"),
        (-1e9, -2, "losing money"),
    ],
    "roe": [
        (15,   +2, "high ROE"),
        (8,    +1, "decent ROE"),
        (0,     0, "low ROE"),
        (-1e9, -2, "negative ROE"),
    ],
    # valuation
    "per": [
        (30,   -1, "expensive (PER > 30)"),
        (18,    0, "fair-ish PER"),
        (10,   +1, "reasonable PER"),
        (0.01, +2, "cheap PER"),
        (-1e9, -2, "negative earnings"),
    ],
    "pbv": [
        (4,    -1, "pricey vs book"),
        (1,     0, "normal PBV"),
        (0.01, +1, "below/near book value"),
        (-1e9, -1, "negative book value"),
    ],
    # leverage / safety
    "der": [
        (2.0,  -1, "heavy leverage"),
        (0.8,   0, "moderate leverage"),
        (0.0,  +1, "low leverage"),
        (-1e9, -2, "negative equity"),
    ],
    "eps": [
        (0.01,  0, "positive EPS"),
        (-1e9, -2, "negative EPS"),
    ],
    "roa": [
        (10,   +1, "high ROA"),
        (0,     0, "modest ROA"),
        (-1e9, -1, "negative ROA"),
    ],
    "current_ratio": [
        (1.5,  +1, "strong liquidity"),
        (1.0,   0, "adequate liquidity"),
        (-1e9, -1, "current ratio < 1"),
    ],
    "interest_coverage": [
        (3.0,  +1, "comfortable interest coverage"),
        (1.5,   0, "adequate interest coverage"),
        (-1e9, -1, "weak interest coverage"),
    ],
    "piotroski": [
        (7,    +2, "strong Piotroski F-Score"),
        (5,    +1, "decent Piotroski F-Score"),
        (4,     0, "average Piotroski F-Score"),
        (-1e9, -1, "weak Piotroski F-Score"),
    ],
    "altman_z": [
        (3.0,  +1, "safe Altman Z-Score"),
        (1.8,   0, "grey-zone Altman Z-Score"),
        (-1e9, -1, "distress-zone Altman Z-Score"),
    ],
    "dividend_yield": [
        (4.0,  +1, "juicy dividend yield"),
        (-1e9,  0, ""),
    ],
}

# PER/PBV/DER are "smaller is better": bounds above are descending, meaning
# the first bound the value EXCEEDS wins. For ascending metrics the same
# logic works because bounds are also listed high→low.

# Max positive points obtainable per metric, derived straight from RULES so
# normalization can never drift out of sync when you tune thresholds.
WEIGHT_MAX: dict[str, int] = {
    metric: max(0, max(points for _, points, _ in ladder))
    for metric, ladder in RULES.items()
}

BUY_CUTOFF = 0.45      # score / max_possible  >= this  -> worth to buy
SELL_CUTOFF = -0.15    # score / max_possible  <= this  -> not worth
MIN_METRICS = 3        # need at least this many metrics to judge

# For stocks you already own, the question isn't "should I buy?" but
# "should I keep?" — same thresholds, different framing.
OWNED_LABELS = {
    "WORTH TO BUY": "HOLD",
    "NOT WORTH TO BUY": "SELL",
    "NEUTRAL": "NEUTRAL",
}


@dataclass
class Verdict:
    label: str
    score: int
    max_possible: int
    ratio: float
    reasons_good: list[str] = field(default_factory=list)
    reasons_bad: list[str] = field(default_factory=list)
    metrics_used: int = 0


def evaluate(metrics: dict[str, float], owned: bool = False) -> Verdict:
    score, max_possible, used = 0, 0, 0
    good, bad = [], []

    for metric, rules in RULES.items():
        val = metrics.get(metric)
        if val is None:
            continue
        used += 1
        max_possible += WEIGHT_MAX[metric]
        for bound, points, note in rules:
            if val >= bound:
                score += points
                if points > 0 and note:
                    good.append(note)
                elif points < 0 and note:
                    bad.append(note)
                break

    if used < MIN_METRICS or max_possible == 0:
        return Verdict("INSUFFICIENT DATA", score, max_possible, 0.0, good, bad, used)

    ratio = score / max_possible
    hard_red_flag = metrics.get("eps", 1) < 0 and metrics.get("net_income_growth", 1) < 0

    if ratio >= BUY_CUTOFF and not hard_red_flag:
        label = "WORTH TO BUY"
    elif ratio <= SELL_CUTOFF or hard_red_flag:
        label = "NOT WORTH TO BUY"
    else:
        label = "NEUTRAL"
    if owned:
        label = OWNED_LABELS.get(label, label)
    return Verdict(label, score, max_possible, ratio, good, bad, used)
