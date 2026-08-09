"""
instrument_classifier.py
-------------------------
CDSL's CAS gives you an asset CLASS (Equity / Mutual Fund Folios / Mutual
Funds Held in Demat Form / Others) but not an instrument TYPE within that -
it doesn't say whether a mutual fund is a liquid fund, a gilt fund, or a
small-cap equity fund, or whether an equity holding is a share or an ETF.
That has to be inferred from the scheme/security name, since Indian fund
and security names are fairly standardised and encode this directly
(e.g. "ICICI Prudential Gilt Fund", "Kotak Small Cap Fund", "NIPPON INDIA
ETF NIFTY BEES").

This is a heuristic, not authoritative data - a fund with an unusual name
falls back to a generic bucket rather than a guess. Categories:

Equity / ETF:
    "Direct Equity (Shares)", "Equity ETF"

Mutual funds (by what they actually invest in, not the wrapper):
    "Equity Fund", "Equity Hybrid Fund", "Multi-Asset Fund", "Index Fund",
    "Debt - Liquid Fund", "Debt - Gilt/Treasury Fund",
    "Debt - Banking & PSU Fund", "Debt - Bond/Other Debt Fund",
    "Other Fund" (fallback)

Government paper:
    "Sovereign Gold Bond", "Government Security"
"""

from __future__ import annotations

import re


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().lower()


def classify_equity(security_name: str) -> str:
    n = _norm(security_name)
    if "etf" in n:
        return "Equity ETF"
    return "Direct Equity (Shares)"


def classify_mf(scheme_name: str) -> str:
    n = _norm(scheme_name)

    # Debt sub-types - check these before the generic equity keywords, since
    # a name like "Banking & PSU Debt Fund" would otherwise false-match
    # nothing, but "Equity Savings Fund" could wrongly hit "savings" logic
    # if checked in the wrong order, so debt keywords are deliberately
    # narrow and specific.
    if "liquid" in n or "overnight" in n or "money market" in n:
        return "Debt - Liquid Fund"
    if "gilt" in n or "g-sec" in n or "treasury" in n:
        return "Debt - Gilt/Treasury Fund"
    if "banking & psu" in n or "banking and psu" in n or "banking & psu debt" in n:
        return "Debt - Banking & PSU Fund"
    if any(k in n for k in [
        "bond fund", "all seasons bond", "dynamic bond", "corporate bond",
        "credit risk", "low duration", "short duration", "medium duration",
        "ultra short", "savings fund", "debt fund",
    ]):
        return "Debt - Bond/Other Debt Fund"

    # Equity-adjacent categories
    if "gold" in n or "silver" in n or "commodit" in n:
        return "Gold/Commodity Fund"
    if "multi asset" in n:
        return "Multi-Asset Fund"
    if "hybrid" in n:
        return "Equity Hybrid Fund"
    if any(k in n for k in ["index fund", "nifty", "sensex"]) and "hybrid" not in n:
        return "Index Fund"
    if any(k in n for k in [
        "small cap", "mid cap", "large cap", "multicap", "multi cap",
        "flexicap", "flexi cap", "elss", "focused", "value fund", "contra",
        "dividend yield",
    ]):
        return "Equity Fund"

    return "Other Fund"


def classify_other(security_name: str) -> str:
    n = _norm(security_name)
    if "sgb" in n or "sovereign gold" in n:
        return "Sovereign Gold Bond"
    return "Government Security"


def classify_holding(security_name: str, category: str) -> str:
    """category is one of 'equity', 'mf', 'other'."""
    if category == "equity":
        return classify_equity(security_name)
    if category == "mf":
        return classify_mf(security_name)
    if category == "other":
        return classify_other(security_name)
    return "Uncategorised"
