"""
zerodha_tradebook.py
---------------------
Parses the Tradebook report you export from Zerodha Console
(console.zerodha.com -> Reports -> Tradebook -> pick date range -> Download
as CSV or XLSX). This is the actual source of dated buy/sell history - the
Kite API only exposes current holdings, not historical trades.

Console's .xlsx export is NOT a plain table starting at row 1 - it has
~14 rows of preamble (client ID, the "Tradebook for Equity from X to Y"
title, blank spacing) before the real header row (Symbol, ISIN, Trade
Date, ...). This parser scans for that header row rather than assuming a
fixed position, so it's robust to Zerodha nudging the preamble layout.
Console also caps each export at roughly a year, so this accepts multiple
files and de-duplicates by Trade ID in case exported ranges overlap.
"""

from __future__ import annotations

from io import BytesIO

import pandas as pd

REQUIRED = ["Symbol", "ISIN", "Trade Date", "Trade Type", "Quantity", "Price", "Trade ID"]


def _read_bytes(file) -> bytes:
    if hasattr(file, "getvalue"):
        return file.getvalue()
    if hasattr(file, "read"):
        file.seek(0)
        return file.read()
    raise TypeError(f"Don't know how to read {file!r}")


def _find_header_row(raw: pd.DataFrame):
    for idx in range(len(raw)):
        row_vals = {str(v) for v in raw.iloc[idx] if pd.notna(v)}
        if {"Symbol", "ISIN", "Trade Type"}.issubset(row_vals):
            return idx
    return None


def looks_like_tradebook(file_bytes: bytes, filename: str = "") -> bool:
    """Cheap sniff used by the unified file-drop router - doesn't fully parse."""
    try:
        if filename.lower().endswith(".csv"):
            raw = pd.read_csv(BytesIO(file_bytes), header=None, dtype=object, nrows=25)
        else:
            raw = pd.read_excel(BytesIO(file_bytes), sheet_name=0, header=None, nrows=25)
    except Exception:
        return False
    return _find_header_row(raw) is not None


def parse_tradebook(files) -> pd.DataFrame:
    """
    Accepts one or more uploaded files (Streamlit UploadedFile objects, file
    paths, or open file handles - anything pandas.read_excel/read_csv can
    take, or with .getvalue()/.read()). Returns the app's standard
    transaction schema: Date, AssetClass, Identifier, Description, Amount.
    Amount convention: negative = bought (money out), positive = sold
    (money in) - matching xirr.py's expectation.
    """
    all_rows = []
    seen_trade_ids = set()

    for f in files:
        fname = getattr(f, "name", "") or ""
        fbytes = _read_bytes(f)

        if fname.lower().endswith(".csv"):
            raw = pd.read_csv(BytesIO(fbytes), header=None, dtype=object)
        else:
            raw = pd.read_excel(BytesIO(fbytes), sheet_name=0, header=None)

        header_idx = _find_header_row(raw)
        if header_idx is None:
            raise ValueError(
                f"'{fname or 'file'}' doesn't look like a Tradebook export - couldn't find the "
                f"Symbol/ISIN/Trade Type header row. Make sure this is Console -> Reports -> "
                f"Tradebook (not a Holdings or P&L report)."
            )

        header = [str(c).strip() if pd.notna(c) else "" for c in raw.iloc[header_idx]]
        missing = [c for c in REQUIRED if c not in header]
        if missing:
            raise ValueError(f"'{fname or 'file'}' is missing expected column(s): {', '.join(missing)}")
        col = {name_: header.index(name_) for name_ in REQUIRED}

        for idx in range(header_idx + 1, len(raw)):
            row = raw.iloc[idx]
            trade_id = row.iloc[col["Trade ID"]]
            if pd.isna(trade_id):
                continue
            trade_id = str(trade_id)
            if trade_id in seen_trade_ids:
                continue  # de-dupe in case two exports' date ranges overlap
            seen_trade_ids.add(trade_id)

            isin = row.iloc[col["ISIN"]]
            trade_date = row.iloc[col["Trade Date"]]
            if pd.isna(isin) or pd.isna(trade_date):
                continue

            symbol = row.iloc[col["Symbol"]]
            trade_type = str(row.iloc[col["Trade Type"]]).strip().lower()
            qty = float(row.iloc[col["Quantity"]])
            price = float(row.iloc[col["Price"]])

            amount = qty * price
            signed_amount = -amount if trade_type == "buy" else amount

            all_rows.append(
                {
                    "Date": pd.to_datetime(trade_date).date(),
                    "AssetClass": "Equity",
                    "Identifier": str(isin).strip(),
                    "Description": f"{symbol} - {trade_type}",
                    "Amount": round(signed_amount, 2),
                }
            )

    out = pd.DataFrame(all_rows, columns=["Date", "AssetClass", "Identifier", "Description", "Amount"])
    return out.sort_values("Date").reset_index(drop=True) if not out.empty else out
