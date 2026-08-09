"""
mfcentral_parser.py
--------------------
Parses an MF Central "Consolidated Account Summary" PDF - a different
document from a CDSL CAS, produced jointly by CAMS and KFintech rather than
a depository. Structurally quite different:

  - Mutual funds only - no equity shares, bonds, or SGBs at all. It covers
    both regular ("SoA" - Statement of Account) folios and mutual fund
    units held in dematerialised form, but nothing outside mutual funds.
  - No ISIN column anywhere. Backfilled via amfi_lookup.py's fuzzy match
    against AMFI's public scheme master where possible; falls back to a
    synthetic identifier (never crashes, just can't cross-reference against
    other sources for that one holding) when no confident match is found.
  - No 12-month portfolio valuation history like CDSL's CAS provides.
  - The holder's name and PAN are printed in a single top-left textbox
    rather than embedded in a sentence like CDSL's CAS.
  - Folios with zero invested/market value (fully redeemed, but MF Central
    still lists them) are dropped rather than shown as zero rows.

Produces the same CASData structure cas_parser.py does, so everything
downstream (family combining, instrument-type breakdown, exports, XIRR
value lookups) works identically regardless of which parser a given
person's file came from.
"""

from __future__ import annotations

import re

import pandas as pd
import pdfplumber

import amfi_lookup
from cas_parser import CASData
from instrument_classifier import classify_mf

_SOA_HEADER_MARKERS = {"Folio No.", "Scheme Details", "Invested Value"}
_DEMAT_HEADER_MARKERS = {"Client Id", "Scheme Details", "Invested Value"}


def _clean(cell) -> str:
    return (cell or "").replace("\n", " ").strip()


def _to_float(cell) -> float:
    s = _clean(cell).replace(",", "").replace("₹", "").replace("INR", "").strip()
    if s in ("", "--", "-"):
        return 0.0
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return 0.0
    return -v if neg else v


def _split_gain_loss(cell: str) -> tuple[float, float]:
    """'12,91,358.64 (+128.20%)' -> (1291358.64, 128.20); '(5,986.87) (-6.00%)' -> (-5986.87, -6.00)"""
    text = _clean(cell)
    m = re.match(r"\(?([\d,]+\.?\d*)\)?\s*\(([+-]?[\d.]+)%\)", text)
    if not m:
        return 0.0, 0.0
    magnitude = float(m.group(1).replace(",", ""))
    if text.strip().startswith("("):
        magnitude = -magnitude
    pct = float(m.group(2))
    return magnitude, pct


def looks_like_mfcentral(pdf) -> bool:
    """Cheap check used by the caller to decide which parser to try."""
    try:
        first_page_text = pdf.pages[0].extract_text() or ""
    except Exception:
        return False
    return "mfcentral" in first_page_text.lower().replace(" ", "") or (
        "cams" in first_page_text.lower() and "kfintech" in first_page_text.lower()
    )


def _extract_holder(pdf) -> tuple[str, str]:
    """Returns (holder_name, PAN) from the top-left textbox on page 1."""
    try:
        table = pdf.pages[0].extract_tables()[0]
        raw = table[0][0] or ""
    except Exception:
        return "", ""
    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    pan = ""
    name = ""
    for i, line in enumerate(lines):
        m = re.search(r"PAN\s*:?\s*([A-Z]{5}\d{4}[A-Z])", line, re.IGNORECASE)
        if m:
            pan = m.group(1).upper()
            if i + 1 < len(lines):
                name = lines[i + 1]
            break
    return name, pan


def _parse_holdings_table(pdf, header_markers: set[str]) -> list[dict]:
    rows = []
    for page in pdf.pages:
        for table in page.extract_tables():
            if not table:
                continue
            header = [_clean(c) for c in table[0]]
            header_text = " ".join(header)
            if not all(marker in header_text for marker in header_markers):
                continue
            col = {name: header.index(name) for name in header if name}
            for row in table[1:]:
                if not row or len(row) < len(header):
                    continue
                folio = _clean(row[0])
                if folio in ("--", "") and "No MF holdings" in _clean(row[1] if len(row) > 1 else ""):
                    continue
                scheme = _clean(row[1]) if len(row) > 1 else ""
                invested = _to_float(row[2]) if len(row) > 2 else 0.0
                market_value = _to_float(row[6]) if len(row) > 6 else 0.0
                pl_abs, pl_pct = _split_gain_loss(row[7]) if len(row) > 7 else (0.0, 0.0)
                if invested == 0.0 and market_value == 0.0:
                    continue  # fully redeemed folio MF Central still lists - not a current holding
                rows.append(
                    {
                        "Folio No.": folio,
                        "Scheme": scheme,
                        "Invested (₹)": invested,
                        "Valuation (₹)": market_value,
                        "Unrealised P/L (₹)": pl_abs,
                        "Unrealised P/L (%)": pl_pct,
                    }
                )
    return rows


def parse_mfcentral_summary(pdf_path_or_bytes, resolve_isins: bool = True) -> CASData:
    data = CASData()

    with pdfplumber.open(pdf_path_or_bytes) as pdf:
        holder_name, pan = _extract_holder(pdf)
        data.holder_name = holder_name

        soa_rows = _parse_holdings_table(pdf, _SOA_HEADER_MARKERS)
        demat_rows = _parse_holdings_table(pdf, _DEMAT_HEADER_MARKERS)

    amfi_master = amfi_lookup.fetch_amfi_master() if resolve_isins and (soa_rows or demat_rows) else None

    def _finish(rows: list[dict]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame(columns=["Scheme", "ISIN", "Instrument Type", "Folio No.", "Invested (₹)",
                                          "Valuation (₹)", "Unrealised P/L (₹)", "Unrealised P/L (%)"])
        for r in rows:
            match = amfi_lookup.match_against_amfi(r["Scheme"], amfi_master) if amfi_master is not None else None
            r["ISIN"] = match[0] if match else f"MFCENTRAL::{r['Folio No.']}::{r['Scheme']}"
            r["Instrument Type"] = classify_mf(r["Scheme"])
        df = pd.DataFrame(rows)
        return df[["Scheme", "ISIN", "Instrument Type", "Folio No.", "Invested (₹)",
                   "Valuation (₹)", "Unrealised P/L (₹)", "Unrealised P/L (%)"]].sort_values(
            "Valuation (₹)", ascending=False
        ).reset_index(drop=True)

    data.mf_folio_holdings = _finish(soa_rows)
    data.mf_in_demat_holdings = _finish(demat_rows)
    data.equity_holdings = pd.DataFrame(columns=["ISIN", "Security", "Instrument Type", "Value (₹)"])
    data.other_holdings = pd.DataFrame(columns=["ISIN", "Security", "Instrument Type", "Value (₹)", "Category"])
    data.valuation_trend = pd.DataFrame()  # MF Central's summary doesn't include a value history

    folio_total = data.mf_folio_holdings["Valuation (₹)"].sum() if not data.mf_folio_holdings.empty else 0.0
    demat_total = data.mf_in_demat_holdings["Valuation (₹)"].sum() if not data.mf_in_demat_holdings.empty else 0.0
    data.total_value = folio_total + demat_total

    summary_rows = []
    if folio_total:
        summary_rows.append({"Asset Class": "Mutual Fund Folios", "Value (₹)": folio_total})
    if demat_total:
        summary_rows.append({"Asset Class": "Mutual Funds Held in Demat Form", "Value (₹)": demat_total})
    summary_df = pd.DataFrame(summary_rows)
    if not summary_df.empty and data.total_value:
        summary_df["% of Portfolio"] = summary_df["Value (₹)"] / data.total_value * 100
    data.asset_summary = summary_df

    return data
