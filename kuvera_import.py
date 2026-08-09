"""
kuvera_import.py
-----------------
Parses a Kuvera transaction/statement export (.xlsx).

Kuvera has no public API for retail users (see README), so this is a file
importer. Their export is unlike a normal spreadsheet: it's not a table of
rows and columns at all - it's every transaction's fields (date, scheme,
buy/sell, units, price, amount) flattened one value per row down a single
column, and Kuvera sometimes inserts a blank filler cell between fields,
so a fixed "every 6th row is a new record" assumption breaks partway
through. This parser instead classifies each value by *type* (a date
starts a new record; the two text values in between are the scheme name
and buy/sell; the three numeric values are units, price, and amount),
which is robust to that inconsistency - verified against a real ~15-year,
450+ transaction export.

Kuvera also doesn't give you an ISIN at all, and its scheme names don't
match CAMS/CDSL's naming for the same fund (e.g. Kuvera's "Bandhan Small
Cap Growth Direct Plan" vs. the CAS's "D340 - Bandhan Small Cap Fund-
Direct Plan-Growth"). So this module matches scheme names to ISINs from
your already-parsed CAS by fund house + category words, gated so a fund
never matches a different fund house's scheme just because they share a
generic word like "large cap". A scheme with no confident match is not an
error - it usually means you've fully redeemed it and it's no longer in
your current CAS, in which case its buy/sell transactions alone already
form a complete, closed cash-flow cycle and don't need a current-value
cash flow for XIRR to be correct.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from io import BytesIO

import pandas as pd

import scheme_matching


# --------------------------------------------------------------------------
# Parsing - two known Kuvera export shapes
# --------------------------------------------------------------------------

def _read_bytes(file) -> bytes:
    if hasattr(file, "getvalue"):
        return file.getvalue()
    if hasattr(file, "read"):
        file.seek(0)
        return file.read()
    raise TypeError(f"Don't know how to read {file!r}")


_CLEAN_CSV_COLUMNS = {"Date", "Folio Number", "Name of the Fund", "Order", "Units", "Amount (INR)"}


def _load_clean_tabular(df: pd.DataFrame) -> pd.DataFrame:
    """
    The newer Kuvera export: a normal table with real column headers (Date,
    Folio Number, Name of the Fund, Order, Units, NAV, Current Nav,
    Amount (INR)) - column names sometimes carry stray leading/trailing
    whitespace (Kuvera's CSV header has ", " after each comma), so those
    get stripped before matching.
    """
    df = df.rename(columns=lambda c: str(c).strip())
    records = pd.DataFrame(
        {
            "Date": pd.to_datetime(df["Date"].astype(str).str.strip()).dt.date,
            "Scheme": df["Name of the Fund"].astype(str).str.strip(),
            "Type": df["Order"].astype(str).str.strip().str.lower(),
            "Units": pd.to_numeric(df["Units"], errors="coerce"),
            "Price": pd.to_numeric(df["NAV"], errors="coerce") if "NAV" in df.columns else float("nan"),
            "Amount": pd.to_numeric(df["Amount (INR)"], errors="coerce"),
        }
    )
    return records.dropna(subset=["Date", "Scheme", "Amount"]).reset_index(drop=True)


def _load_flattened_xlsx(raw: pd.DataFrame) -> pd.DataFrame:
    """
    The older Kuvera export: every transaction's fields flattened one value
    per row down a single column, no real headers at all (see module
    docstring). raw is the sheet read with header=None.
    """
    vals = raw[0].tolist()
    n = len(vals)
    date_idxs = [i for i in range(n) if isinstance(vals[i], datetime)]
    if not date_idxs:
        raise ValueError(
            "Couldn't find any transaction dates in this file - make sure it's a Kuvera "
            "transaction/statement export."
        )
    date_idxs.append(n)  # sentinel

    records = []
    for k in range(len(date_idxs) - 1):
        start, end = date_idxs[k], date_idxs[k + 1]
        dt = vals[start]
        chunk = vals[start + 1 : end]
        strs = [v for v in chunk if isinstance(v, str)]
        nums = [
            v for v in chunk
            if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v))
        ]
        if len(strs) != 2 or len(nums) != 3:
            continue  # skip anything that doesn't fit the expected shape (e.g. a stray note row)
        scheme, ttype = strs
        units, price, amount = nums
        records.append(
            {"Date": dt.date(), "Scheme": scheme, "Type": ttype.strip().lower(),
             "Units": units, "Price": price, "Amount": amount}
        )
    return pd.DataFrame(records)


def looks_like_kuvera(file_bytes: bytes, filename: str = "") -> bool:
    """Cheap sniff used by the unified file-drop router - doesn't fully parse."""
    try:
        if filename.lower().endswith(".csv"):
            df = pd.read_csv(BytesIO(file_bytes), nrows=5)
            df = df.rename(columns=lambda c: str(c).strip())
            return _CLEAN_CSV_COLUMNS.issubset(df.columns)
        raw = pd.read_excel(BytesIO(file_bytes), sheet_name=0, header=None, nrows=5)
        if raw.shape[1] == 0:
            return False
        if _CLEAN_CSV_COLUMNS.issubset({str(c).strip() for c in raw.iloc[0]}):
            return True
        # Old flattened shape: a date sitting in the very first cell is the
        # only reliable signature without doing a full parse.
        return isinstance(raw.iloc[0, 0], datetime)
    except Exception:
        return False


def load_statement(file) -> pd.DataFrame:
    """
    Returns one row per transaction: Date, Scheme, Type, Units, Price,
    Amount. Auto-detects which of the two known Kuvera export shapes this
    is (see module docstring) - a plain CSV is always the newer clean-
    tabular shape; an .xlsx could be either, so it's read once and
    dispatched by whether its first row looks like real column headers.
    Raises ValueError if the file doesn't look like a Kuvera export at all.
    """
    fbytes = _read_bytes(file)
    fname = getattr(file, "name", "") or ""

    if fname.lower().endswith(".csv"):
        df = pd.read_csv(BytesIO(fbytes))
        df = df.rename(columns=lambda c: str(c).strip())
        if not _CLEAN_CSV_COLUMNS.issubset(df.columns):
            raise ValueError(
                f"This CSV doesn't have the expected Kuvera columns "
                f"({', '.join(sorted(_CLEAN_CSV_COLUMNS))})."
            )
        records = _load_clean_tabular(df)
    else:
        raw = pd.read_excel(BytesIO(fbytes), sheet_name=0, header=None)
        if raw.shape[1] == 0:
            raise ValueError("Empty file.")
        if _CLEAN_CSV_COLUMNS.issubset({str(c).strip() for c in raw.iloc[0]}):
            df = pd.read_excel(BytesIO(fbytes), sheet_name=0)
            records = _load_clean_tabular(df)
        else:
            records = _load_flattened_xlsx(raw)

    if records is None or records.empty:
        raise ValueError(
            "Found the file but couldn't parse any complete transactions from it. "
            "Kuvera may have changed their export format - check the raw file."
        )
    return records


# --------------------------------------------------------------------------
# Scheme-name -> ISIN matching against the parsed CAS
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Scheme-name -> ISIN matching against the parsed CAS
# --------------------------------------------------------------------------

def match_scheme_to_isin(scheme_name: str, cas_mf_holdings: pd.DataFrame, threshold: float = 0.45, min_intersect: int = 1):
    """
    Returns (ISIN, CAS scheme name, score) or None. Thin wrapper around the
    shared matcher in scheme_matching.py (also used by mfcentral_parser.py
    for ISIN backfill against AMFI's scheme master).
    """
    if cas_mf_holdings is None or cas_mf_holdings.empty:
        return None
    candidates = [
        (row.ISIN, row.Scheme)
        for row in cas_mf_holdings[["ISIN", "Scheme"]].drop_duplicates().itertuples()
    ]
    return scheme_matching.best_match(scheme_name, candidates, threshold=threshold, min_intersect=min_intersect)


def build_transactions(kuvera_records: pd.DataFrame, cas_mf_holdings: pd.DataFrame):
    """
    Converts load_statement()'s output into the app's standard transaction
    schema, matching each scheme to a CAS ISIN. Returns (transactions_df,
    match_summary) where match_summary is a DataFrame of one row per unique
    scheme showing what it matched to (or "no match"), so the app can show
    the user what happened before they rely on the numbers.
    """
    rows = []
    summary = {}
    match_cache = {}

    for _, r in kuvera_records.iterrows():
        scheme = r["Scheme"]
        if scheme not in match_cache:
            match_cache[scheme] = match_scheme_to_isin(scheme, cas_mf_holdings)
        match = match_cache[scheme]

        identifier = match[0] if match else f"UNMATCHED::{scheme}"
        summary.setdefault(
            scheme,
            {
                "Kuvera Scheme": scheme,
                "Matched CAS Scheme": match[1] if match else "(no current CAS holding - likely fully redeemed)",
                "ISIN": match[0] if match else "-",
                "Match Confidence": f"{match[2]*100:.0f}%" if match else "-",
            },
        )

        signed_amount = -r["Amount"] if r["Type"] == "buy" else r["Amount"]
        rows.append(
            {
                "Date": r["Date"],
                "AssetClass": "Mutual Fund Folios",
                "Identifier": identifier,
                "Description": f"{scheme} - {r['Type']}",
                "Amount": round(float(signed_amount), 2),
            }
        )

    txns = pd.DataFrame(rows).sort_values("Date").reset_index(drop=True)
    match_summary = pd.DataFrame(summary.values())
    return txns, match_summary
