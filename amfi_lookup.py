"""
amfi_lookup.py
---------------
MF Central's Consolidated Account Summary (see mfcentral_parser.py) doesn't
print an ISIN for any holding - unlike CDSL's CAS, or Kuvera's export (which
at least has a CAS to match against). Without an ISIN, a holding from this
source can't be cross-referenced against Zerodha/Kuvera transactions, or
even correctly de-duplicated against a holding of the same fund reported by
a different source for the same person.

AMFI (the Association of Mutual Funds in India) publishes a free, public,
no-auth master list of every scheme with its ISIN at
https://www.amfiindia.com/spages/NAVAll.txt - this module fetches and
caches that (scheme name -> ISIN) and matches MF Central's scheme names
against it using the same fund-house-gated fuzzy matcher used for Kuvera.

This is a genuine network dependency, unlike everything else in this app
(which only ever touches whatever the user directly uploads). It's wrapped
to fail gracefully: if the fetch doesn't work for any reason - offline,
AMFI's endpoint down, a firewalled deployment - holdings just keep a
synthetic identifier instead of a real ISIN, and everything downstream
(the app already treats "ISIN" as an opaque string key almost everywhere)
keeps working, just without cross-source matching for that holding.
"""

from __future__ import annotations

import re
import urllib.request

import pandas as pd

import scheme_matching

AMFI_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
_TIMEOUT_SECONDS = 15


def fetch_amfi_master() -> pd.DataFrame | None:
    """
    Returns a DataFrame with columns [ISIN, Scheme Name], one row per
    (scheme, plan, option) AMFI lists - typically 15,000+ rows, including
    every dividend/IDCW variant alongside Growth for the same fund. Returns
    None (not an exception) if the fetch fails, so callers can degrade
    gracefully rather than needing a try/except at every call site.
    """
    try:
        req = urllib.request.Request(AMFI_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or ";" not in line:
            continue
        parts = line.split(";")
        if len(parts) < 4:
            continue
        scheme_code, isin_growth, isin_reinvest, scheme_name = parts[0], parts[1], parts[2], parts[3]
        if not re.match(r"^\d+$", scheme_code):
            continue  # header/category/AMC separator lines, not a scheme row
        isin = isin_growth if isin_growth and isin_growth != "-" else isin_reinvest
        if not isin or isin == "-":
            continue
        rows.append({"ISIN": isin.strip(), "Scheme Name": scheme_name.strip()})

    if not rows:
        return None
    return pd.DataFrame(rows)


def match_against_amfi(scheme_name: str, amfi_master: pd.DataFrame, threshold: float = 0.45) -> tuple[str, str, float] | None:
    """
    Returns (ISIN, matched AMFI scheme name, score) or None. Biases toward
    "Growth" plan variants when the query doesn't specify a dividend/IDCW
    option itself, since AMFI lists every option under near-identical names
    and most retail holdings in these summary documents are Growth plans.
    """
    if amfi_master is None or amfi_master.empty:
        return None
    candidates = list(amfi_master[["ISIN", "Scheme Name"]].itertuples(index=False, name=None))
    prefer = "growth" if "idcw" not in scheme_name.lower() and "dividend" not in scheme_name.lower() else None
    return scheme_matching.best_match(scheme_name, candidates, threshold=threshold, prefer_substring=prefer)
