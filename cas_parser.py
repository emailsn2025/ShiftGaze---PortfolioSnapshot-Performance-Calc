"""
cas_parser.py
-------------
Parses a CDSL Consolidated Account Statement (CAS) PDF and extracts:
  - Portfolio-level asset class summary (Equity / Mutual Fund Folios /
    Mutual Funds Held in Demat Form / Others)
  - Individual equity & bond holdings (from CDSL + NSDL demat tables)
  - Individual mutual fund folio holdings (with invested amount, valuation,
    and unrealised P&L, which CAMS/KFIN pre-compute for us)
  - The 12-month portfolio valuation trend table (for context, not XIRR)

CDSL CAS PDFs interleave English and Hindi text at the character level,
which makes plain page.extract_text() unusable. This parser instead relies
on pdfplumber's table extraction, which comes through clean, and classifies
each row using India's standard ISIN prefix convention:
    INE -> Equity share
    INF -> Mutual fund unit
    IN0 -> Government security / Sovereign Gold Bond
This makes the parser reusable for any CDSL CAS, not just one specific file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import pdfplumber

from instrument_classifier import classify_holding

ISIN_RE = re.compile(r"^IN[A-Z0-9]{10}")

ISIN_PREFIX_TO_CLASS = {
    "INE": "Equity",
    "INF": "Mutual Funds Held in Demat Form",
    "IN0": "Others (Govt Securities/SGB)",
}


def _clean(cell: Optional[str]) -> str:
    return (cell or "").replace("\n", " ").strip()


def _to_float(cell: Optional[str]) -> float:
    s = _clean(cell).replace(",", "").replace("`", "").replace("₹", "")
    if s in ("", "--", "-", "None"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


@dataclass
class CASData:
    holder_name: str = ""
    total_value: float = 0.0
    asset_summary: pd.DataFrame = field(default_factory=pd.DataFrame)
    equity_holdings: pd.DataFrame = field(default_factory=pd.DataFrame)
    mf_in_demat_holdings: pd.DataFrame = field(default_factory=pd.DataFrame)
    other_holdings: pd.DataFrame = field(default_factory=pd.DataFrame)
    mf_folio_holdings: pd.DataFrame = field(default_factory=pd.DataFrame)
    valuation_trend: pd.DataFrame = field(default_factory=pd.DataFrame)


def parse_cas(pdf_path_or_bytes) -> CASData:
    demat_rows: list[list[str]] = []
    mf_rows: list[list[str]] = []
    asset_summary_rows: list[list[str]] = []
    trend_rows: list[list[str]] = []
    holder_name = ""

    with pdfplumber.open(pdf_path_or_bytes) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table:
                    continue
                if not holder_name:
                    holder_name = _extract_holder_name(table)
                header_idx, header_type = _identify_header(table)
                if header_idx is None:
                    continue
                data_rows = table[header_idx + 1 :]

                if header_type == "demat":
                    for row in data_rows:
                        if row and row[0] and ISIN_RE.match(_clean(row[0])):
                            demat_rows.append(row)
                elif header_type == "mf":
                    for row in data_rows:
                        if len(row) > 1 and row[1] and ISIN_RE.match(_clean(row[1])):
                            mf_rows.append(row)
                elif header_type == "asset_summary" and not asset_summary_rows:
                    asset_summary_rows = table[header_idx:]
                elif header_type == "trend" and not trend_rows:
                    trend_rows = table[header_idx:]

    data = _build_cas_data(demat_rows, mf_rows, asset_summary_rows, trend_rows)
    data.holder_name = holder_name
    return data


def _extract_holder_name(table: list[list[str]]) -> str:
    """
    The holder's name appears once, near the top of the CAS, in a sentence
    like "...In the single name of SANDEEP NARANG ( PAN :ACPPN6801M )".
    Joint accounts say "In the joint names of X AND Y" instead - this
    grabs everything between "name(s) of" and the opening PAN parenthesis,
    which covers both. Returns "" if the pattern isn't found on this table
    (most tables won't have it - the caller keeps scanning until one does).
    """
    for row in table:
        for cell in row:
            if not cell or "PAN" not in cell:
                continue
            text = _clean(cell)
            match = re.search(r"names?\s+of\s+(.+?)\s*\(\s*PAN", text, re.IGNORECASE)
            if match:
                return match.group(1).strip()
    return ""


def _identify_header(table: list[list[str]]):
    for idx, row in enumerate(table):
        row_text = " ".join(_clean(c) for c in row if c)
        # Skip "STATEMENT OF TRANSACTIONS" tables (period activity, not
        # current holdings) - they also contain "ISIN"/"Security" so must
        # be excluded explicitly before the holdings check below.
        is_transaction_table = any(
            marker in row_text for marker in ("Transaction", "Op. Bal", "Cl. Bal", "Credit", "Debit")
        )
        if "Scheme Name" in row_text and "ISIN" in row_text:
            return idx, "mf"
        if "ISIN" in row_text and ("Security" in row_text or "Current" in row_text) and not is_transaction_table:
            return idx, "demat"
        if "Asset Class" in row_text:
            return idx, "asset_summary"
        if "Month-Year" in row_text or ("Portfolio Valuation" in row_text and "Changes" in row_text):
            return idx, "trend"
    return None, None


def _classify_isin(isin: str) -> str:
    prefix = isin[:3]
    return ISIN_PREFIX_TO_CLASS.get(prefix, f"Other ({prefix})")


def _build_cas_data(demat_rows, mf_rows, asset_summary_rows, trend_rows) -> CASData:
    data = CASData()

    # ---- Demat holdings (equity / govt securities / MF-in-demat) ----
    equity, mf_in_demat, others = [], [], []
    for row in demat_rows:
        isin = _clean(row[0])
        security = _clean(row[1]) if len(row) > 1 else ""
        value = _to_float(row[-1])
        cls = _classify_isin(isin)
        if cls == "Equity":
            rec = {"ISIN": isin, "Security": security, "Instrument Type": classify_holding(security, "equity"), "Value (₹)": value}
            equity.append(rec)
        elif cls == "Mutual Funds Held in Demat Form":
            rec = {"ISIN": isin, "Security": security, "Instrument Type": classify_holding(security, "mf"), "Value (₹)": value}
            mf_in_demat.append(rec)
        else:
            rec = {"ISIN": isin, "Security": security, "Instrument Type": classify_holding(security, "other"), "Value (₹)": value, "Category": cls}
            others.append(rec)

    data.equity_holdings = (
        pd.DataFrame(equity).sort_values("Value (₹)", ascending=False).reset_index(drop=True)
        if equity else pd.DataFrame(columns=["ISIN", "Security", "Instrument Type", "Value (₹)"])
    )
    data.mf_in_demat_holdings = (
        pd.DataFrame(mf_in_demat).sort_values("Value (₹)", ascending=False).reset_index(drop=True)
        if mf_in_demat else pd.DataFrame(columns=["ISIN", "Security", "Instrument Type", "Value (₹)"])
    )
    data.other_holdings = (
        pd.DataFrame(others).sort_values("Value (₹)", ascending=False).reset_index(drop=True)
        if others else pd.DataFrame(columns=["ISIN", "Security", "Instrument Type", "Value (₹)", "Category"])
    )

    # ---- Mutual fund folio holdings ----
    mf_records = []
    for row in mf_rows:
        scheme = _clean(row[0])
        isin = _clean(row[1])
        folio = _clean(row[2]) if len(row) > 2 else ""
        invested = _to_float(row[5]) if len(row) > 5 else 0.0
        valuation = _to_float(row[6]) if len(row) > 6 else 0.0
        pl_abs = _to_float(row[7]) if len(row) > 7 else (valuation - invested)
        pl_pct = _to_float(row[8]) if len(row) > 8 else (
            (pl_abs / invested * 100) if invested else 0.0
        )
        mf_records.append(
            {
                "Scheme": scheme,
                "ISIN": isin,
                "Instrument Type": classify_holding(scheme, "mf"),
                "Folio No.": folio,
                "Invested (₹)": invested,
                "Valuation (₹)": valuation,
                "Unrealised P/L (₹)": pl_abs,
                "Unrealised P/L (%)": pl_pct,
            }
        )
    data.mf_folio_holdings = (
        pd.DataFrame(mf_records).sort_values("Valuation (₹)", ascending=False).reset_index(drop=True)
        if mf_records else pd.DataFrame(
            columns=["Scheme", "ISIN", "Instrument Type", "Folio No.", "Invested (₹)", "Valuation (₹)",
                     "Unrealised P/L (₹)", "Unrealised P/L (%)"]
        )
    )

    # ---- Asset class summary ----
    summary_records = []
    for row in asset_summary_rows[1:] if asset_summary_rows else []:
        label = _clean(row[0])
        if not label or label.lower() == "total":
            if label.lower() == "total":
                data.total_value = _to_float(row[1])
            continue
        summary_records.append({"Asset Class": label, "Value (₹)": _to_float(row[1]),
                                 "% of Portfolio": _to_float(row[2]) if len(row) > 2 else None})
    data.asset_summary = pd.DataFrame(summary_records)
    if not data.total_value and not data.asset_summary.empty:
        data.total_value = data.asset_summary["Value (₹)"].sum()

    # ---- 12-month valuation trend (context only, NOT used for XIRR) ----
    trend_records = []
    for row in trend_rows[1:] if trend_rows else []:
        month = _clean(row[0])
        if not month:
            continue
        trend_records.append(
            {
                "Month": month,
                "Portfolio Value (₹)": _to_float(row[1]),
                "Change (₹)": _to_float(row[2]) if len(row) > 2 else None,
                "Change (%)": _to_float(row[3]) if len(row) > 3 else None,
            }
        )
    data.valuation_trend = pd.DataFrame(trend_records)

    return data


# --------------------------------------------------------------------------
# Cross-table helpers - built from an already-parsed CASData
# --------------------------------------------------------------------------

_ASSET_CLASS_BY_TABLE = {
    "equity_holdings": "Equity",
    "mf_folio_holdings": "Mutual Fund Folios",
    "mf_in_demat_holdings": "Mutual Funds Held in Demat Form",
    "other_holdings": "Others",
}
_VALUE_COL_BY_TABLE = {
    "equity_holdings": "Value (₹)",
    "mf_folio_holdings": "Valuation (₹)",
    "mf_in_demat_holdings": "Value (₹)",
    "other_holdings": "Value (₹)",
}


def instrument_type_breakdown(data: "CASData") -> pd.DataFrame:
    """
    One row per (Asset Class, Instrument Type) combination across all four
    holdings tables, e.g. splitting "Mutual Fund Folios" into "Equity Fund",
    "Debt - Liquid Fund", "Index Fund", etc. Value and % of total portfolio.
    """
    rows = []
    for table_name, asset_class in _ASSET_CLASS_BY_TABLE.items():
        df = getattr(data, table_name)
        if df is None or df.empty:
            continue
        value_col = _VALUE_COL_BY_TABLE[table_name]
        for itype, val in df.groupby("Instrument Type")[value_col].sum().items():
            rows.append({"Asset Class": asset_class, "Instrument Type": itype, "Value (₹)": val})
    out = pd.DataFrame(rows, columns=["Asset Class", "Instrument Type", "Value (₹)"])
    if not out.empty and data.total_value:
        out["% of Portfolio"] = out["Value (₹)"] / data.total_value * 100
    return out.sort_values("Value (₹)", ascending=False).reset_index(drop=True) if not out.empty else out


def isin_to_instrument_type(data: "CASData") -> dict:
    """ISIN -> Instrument Type, pooled across all four holdings tables."""
    mapping = {}
    for table_name in _ASSET_CLASS_BY_TABLE:
        df = getattr(data, table_name)
        if df is None or df.empty:
            continue
        for isin, itype in df[["ISIN", "Instrument Type"]].drop_duplicates("ISIN").itertuples(index=False):
            mapping[isin] = itype
    return mapping


_HOLDINGS_TABLES = ["equity_holdings", "mf_folio_holdings", "mf_in_demat_holdings", "other_holdings"]


def combine_family(holder_to_data: dict[str, "CASData"]) -> "CASData":
    """
    Merges multiple holders' parsed CAS data into one family-wide CASData.
    Every holdings table gains a leading "Holder" column (so per-person
    detail is never lost - the Holdings Detail tab and every export show
    it naturally), while total_value, asset_summary, and valuation_trend
    are recomputed as true family-wide aggregates rather than just one
    holder's numbers relabelled.
    """
    combined = CASData()
    combined.holder_name = ", ".join(holder_to_data.keys())
    combined.total_value = sum(d.total_value for d in holder_to_data.values())

    for attr in _HOLDINGS_TABLES:
        parts = []
        for holder, d in holder_to_data.items():
            df = getattr(d, attr)
            if df is not None and not df.empty:
                df = df.copy()
                df.insert(0, "Holder", holder)
                parts.append(df)
        setattr(combined, attr, pd.concat(parts, ignore_index=True) if parts else getattr(CASData(), attr))

    summary_parts = [d.asset_summary for d in holder_to_data.values() if d.asset_summary is not None and not d.asset_summary.empty]
    if summary_parts:
        grouped = pd.concat(summary_parts, ignore_index=True).groupby("Asset Class", as_index=False)["Value (₹)"].sum()
        grouped["% of Portfolio"] = grouped["Value (₹)"] / combined.total_value * 100 if combined.total_value else 0.0
        combined.asset_summary = grouped.sort_values("Value (₹)", ascending=False).reset_index(drop=True)
    else:
        combined.asset_summary = pd.DataFrame()

    trend_parts = [
        d.valuation_trend[["Month", "Portfolio Value (₹)"]]
        for d in holder_to_data.values() if d.valuation_trend is not None and not d.valuation_trend.empty
    ]
    if trend_parts:
        all_trend = pd.concat(trend_parts, ignore_index=True)
        grouped = all_trend.groupby("Month", as_index=False)["Portfolio Value (₹)"].sum()
        # Preserve chronological order using whichever holder has the longest
        # trend history, since CDSL always lists months oldest-first.
        longest = max(
            (d.valuation_trend["Month"].tolist() for d in holder_to_data.values() if d.valuation_trend is not None and not d.valuation_trend.empty),
            key=len, default=[],
        )
        order = {m: i for i, m in enumerate(longest)}
        grouped["_order"] = grouped["Month"].map(order).fillna(len(order))
        grouped = grouped.sort_values("_order").drop(columns="_order").reset_index(drop=True)
        grouped["Change (₹)"] = grouped["Portfolio Value (₹)"].diff()
        grouped["Change (%)"] = grouped["Portfolio Value (₹)"].pct_change() * 100
        combined.valuation_trend = grouped
    else:
        combined.valuation_trend = pd.DataFrame()

    return combined
