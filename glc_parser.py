"""
glc_parser.py
-------------
Parses a Green Lantern Capital LLP (GLC) PMS "Transaction Statement" export
(.xls). PMS statements are unlike every other source this app reads: it's a
flattened multi-page report - the account header, title, and column header
row physically repeat every ~30 rows (one block per printed "page" of the
original statement), with instrument-category section labels ("Shares -
Listed", "Mutual Funds - Liquid") and settlement-status labels ("Current
Period Settled/Not Settled Transactions") interleaved as their own rows
rather than columns. Buy/Sell transaction rows continue seamlessly across
page boundaries without repeating the section label, so this parser tracks
"current category" and "current settlement status" as state while scanning
top to bottom, tagging each transaction row with whatever was most recently
seen above it.

No ISIN is printed anywhere. Since a PMS holds its securities in the same
underlying demat/folio structure as the account holder's own CAS (this GLC
account settles through Nuvama, which is also the NSDL DP on the CAS), this
matches each transaction's security/scheme name against that person's own
CAS holdings for the ISIN - listed shares against equity_holdings (using
scheme_matching.best_match_equity, tuned for company names rather than fund
scheme names), and anything else (e.g. the liquid fund used for PMS cash
management) against mf_folio_holdings + mf_in_demat_holdings using the
regular fund matcher. A security with no confident match keeps the same
"UNMATCHED::" convention Kuvera uses, so it's automatically excluded from
XIRR by the same filter, and flagged in the match summary as likely a
position closed before the CAS's date - a real possibility, not just an
unresolved match, since GLC actively churns individual stock positions.
"""

from __future__ import annotations

from io import BytesIO

import pandas as pd

import scheme_matching

_PAGE_HEADER_PREFIXES = (
    "TRANSACTION STATEMENT", "From ", "Account :", "Account:",
)
_STATUS_LABELS = {
    "Current Period Settled Transactions": "Settled",
    "Current Period Not Settled Transactions": "Not Settled",
}
_SKIP_LABELS = {"Current Period Transactions"}


def _to_float(v) -> float:
    if pd.isna(v):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def looks_like_glc(file_bytes: bytes) -> bool:
    """Cheap check used by the Connect & Import UI to sanity-check the upload."""
    try:
        df = pd.read_excel(BytesIO(file_bytes), sheet_name=0, header=None, nrows=10)
    except Exception:
        return False
    text = " ".join(str(v) for v in df.values.flatten() if pd.notna(v))
    return "TRANSACTION STATEMENT" in text.upper() and "GREEN LANTERN" in text.upper()


def _extract_raw_transactions(file_bytes: bytes) -> tuple[list[dict], str]:
    """
    Returns (transactions, fund_label). fund_label is whatever GLC printed
    as the fund/strategy name (e.g. "GREEN LANTERN CAPITAL LLP - GLC GROWTH
    FUND"), surfaced so the UI can show which PMS strategy this was, since
    an account holder could in principle have more than one.
    """
    df = pd.read_excel(BytesIO(file_bytes), sheet_name=0, header=None)

    fund_label = ""
    current_category = None
    current_status = "Settled"
    rows = []

    for i in range(len(df)):
        v = df.iloc[i, 0]
        if pd.isna(v):
            continue
        v = str(v).strip()

        if v in ("Buy", "Sell"):
            rows.append(
                {
                    "type": v,
                    "category": current_category,
                    "status": current_status,
                    "tran_date": df.iloc[i, 3],
                    "security": str(df.iloc[i, 8]).strip() if pd.notna(df.iloc[i, 8]) else "",
                    "quantity": _to_float(df.iloc[i, 15]),
                    "unit_price": _to_float(df.iloc[i, 17]),
                    "settlement_amount": _to_float(df.iloc[i, 27]),
                }
            )
            continue

        if v == "TRANSACTION STATEMENT SUMMARY":
            break  # everything after this is aggregate totals, not transaction rows
        if "GREEN LANTERN" in v.upper():
            # Repeats on every page of this multi-page report - capture it
            # once for display, but it must never fall through to the
            # "new category label" branch below, or it silently overwrites
            # current_category (e.g. "Shares - Listed") on every page break,
            # misclassifying every transaction after it until the next real
            # section label.
            if not fund_label:
                fund_label = v
            continue
        if any(v.startswith(p) for p in _PAGE_HEADER_PREFIXES) or v == "Transaction Description":
            continue
        if v in _STATUS_LABELS:
            current_status = _STATUS_LABELS[v]
            continue
        if v in _SKIP_LABELS:
            continue
        # Anything else non-null in the first column is a new instrument
        # category label (e.g. "Shares - Listed", "Mutual Funds - Liquid").
        current_category = v

    return rows, fund_label


def parse_glc_statement(
    file_bytes: bytes,
    cas_equity_holdings: pd.DataFrame,
    cas_mf_folio_holdings: pd.DataFrame,
    cas_mf_in_demat_holdings: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns (transactions_df, match_summary_df). transactions_df has columns
    [Date, AssetClass, Identifier, Description, Amount] (Holder is added by
    the caller, same as the other Connect & Import sources) - amount is
    negative for Buy, positive for Sell, using the statement's own
    Settlement Amount (already net of brokerage/STT, so no need to
    recompute it from quantity x price).

    Mutual fund candidates are taken from both mf_folio_holdings (column
    "Scheme") and mf_in_demat_holdings (column "Security") - a PMS's cash-
    management fund (e.g. a liquid fund) is often held in dematerialised
    form via the same NSDL account as its equities, not as a regular folio,
    so both tables need checking; they use different column names for the
    same concept, which is why this doesn't just take one combined frame.
    """
    raw_rows, fund_label = _extract_raw_transactions(file_bytes)

    equity_candidates = [(r.ISIN, r.Security) for r in cas_equity_holdings.itertuples()] if not cas_equity_holdings.empty else []
    mf_candidates = [(r.ISIN, r.Scheme) for r in cas_mf_folio_holdings.itertuples()] if not cas_mf_folio_holdings.empty else []
    if cas_mf_in_demat_holdings is not None and not cas_mf_in_demat_holdings.empty:
        mf_candidates += [(r.ISIN, r.Security) for r in cas_mf_in_demat_holdings.itertuples()]

    txn_rows = []
    match_cache: dict[tuple, tuple | None] = {}

    for r in raw_rows:
        security = r["security"]
        is_equity = "share" in (r["category"] or "").lower()
        cache_key = (is_equity, security)

        if cache_key not in match_cache:
            if is_equity:
                match_cache[cache_key] = scheme_matching.best_match_equity(security, equity_candidates)
            else:
                match_cache[cache_key] = scheme_matching.best_match(security, mf_candidates)
        match = match_cache[cache_key]

        identifier = match[0] if match else f"UNMATCHED::{security}"
        signed_amount = -r["settlement_amount"] if r["type"] == "Buy" else r["settlement_amount"]

        try:
            date_val = pd.to_datetime(r["tran_date"], dayfirst=True).date()
        except Exception:
            continue  # skip anything with an unparseable date rather than crash the whole import

        txn_rows.append(
            {
                "Date": date_val,
                "AssetClass": "Equity" if is_equity else "Mutual Fund Folios",
                "Identifier": identifier,
                "Description": f"{security} - {r['type'].lower()}" + (
                    "" if r["status"] == "Settled" else " (not yet settled)"
                ),
                "Amount": round(signed_amount, 2),
            }
        )

    txns = pd.DataFrame(txn_rows, columns=["Date", "AssetClass", "Identifier", "Description", "Amount"])
    if not txns.empty:
        txns = txns.sort_values("Date").reset_index(drop=True)

    summary_rows = []
    for (is_equity, security), match in match_cache.items():
        summary_rows.append(
            {
                "Security/Scheme": security,
                "Category": "Equity" if is_equity else "Mutual Fund",
                "Matched CAS Holding": match[1] if match else "(no match - likely closed before CAS date)",
                "ISIN": match[0] if match else "-",
                "Match Confidence": f"{match[2]*100:.0f}%" if match else "-",
            }
        )
    match_summary = pd.DataFrame(summary_rows)

    return txns, match_summary
