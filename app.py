"""
Portfolio Summary & XIRR Tracker
--------------------------------
Upload a CDSL Consolidated Account Statement (CAS) PDF and get a clean,
tabular summary of your holdings by asset class - plus, if you supply your
transaction history, real XIRR per asset class.

Run locally:    streamlit run app.py
Deploy:         see README.md for GitHub + Streamlit Community Cloud steps
"""

from datetime import date, datetime
from io import BytesIO
from collections import defaultdict
import os

import pandas as pd
import pdfplumber
import plotly.express as px
import streamlit as st

from cas_parser import parse_cas, CASData, instrument_type_breakdown, isin_to_instrument_type, combine_family
from xirr import CashFlow, xirr
from zerodha_tradebook import parse_tradebook
import zerodha_tradebook
import kuvera_import
import mfcentral_parser
import glc_parser
import zerodha_connector
import report_export
import instrument_classifier

st.set_page_config(page_title="Portfolio Summary & XIRR Tracker", page_icon="📊", layout="wide")

TXN_SCHEMA = ["Date", "AssetClass", "Identifier", "Description", "Amount", "Holder"]
if "txn_sources" not in st.session_state:
    st.session_state.txn_sources = {}  # source_key -> DataFrame in TXN_SCHEMA
if "zerodha_session" not in st.session_state:
    st.session_state.zerodha_session = None
if "zerodha_holdings" not in st.session_state:
    st.session_state.zerodha_holdings = None
if "holder_to_data" not in st.session_state:
    st.session_state.holder_to_data = {}
if "prev_holder_to_data" not in st.session_state:
    st.session_state.prev_holder_to_data = {}
    
# Bumping these forces the matching file_uploader to remount as a brand-new
# widget, which is the only way to make an uploader forget a previously
# selected file - clearing session_state alone doesn't touch it.
if "uploader_nonce" not in st.session_state:
    st.session_state.uploader_nonce = {"zt": 0, "kv": 0, "glc": 0, "manual": 0, "dropzone": 0, "resume": 0}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def fmt_inr(x: float) -> str:
    """Format a number in Indian comma style, rounded to the nearest rupee - e.g. 3,12,57,114"""
    if x is None or pd.isna(x):
        return "-"
    try:
        x = float(x)
    except (ValueError, TypeError):
        return "-"
    neg = x < 0
    x = round(abs(x))
    int_part = str(x)
    if len(int_part) > 3:
        last3 = int_part[-3:]
        rest = int_part[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        int_part = ",".join(groups + [last3])
    out = f"₹{int_part}"
    return f"-{out}" if neg else out


def fmt_inr_short(x: float) -> str:
    """Abbreviated Indian units for chart axes/labels - e.g. ₹50L, ₹1.24Cr, ₹8,400"""
    if x is None or pd.isna(x):
        return "-"
    try:
        x = float(x)
    except (ValueError, TypeError):
        return "-"
    neg = x < 0
    x = abs(x)
    if x >= 1_00_00_000:
        out = f"₹{x/1_00_00_000:.2f}Cr"
    elif x >= 1_00_000:
        out = f"₹{x/1_00_000:.2f}L"
    elif x >= 1_000:
        out = f"₹{x/1_000:.1f}K"
    else:
        out = f"₹{round(x)}"
    return f"-{out}" if neg else out


def _with_total_row(display_df: pd.DataFrame, raw_df: pd.DataFrame, sum_specs: dict, label: str = "Total") -> pd.DataFrame:
    """
    Appends a Total row to an already-formatted display DataFrame. sum_specs
    maps {column name: formatter function} - the column's sum is computed
    from raw_df (unformatted numbers) and passed through the same formatter
    used for the rest of that column, so e.g. a rupee column's total reads
    "₹12,34,567" just like every other cell in it. Every other column is
    left blank in that row except the first, which gets the label. No-op
    if raw_df is empty/None.
    """
    if raw_df is None or raw_df.empty:
        return display_df
    total_row = {col: "" for col in display_df.columns}
    for col, formatter in sum_specs.items():
        if col in raw_df.columns:
            total_row[col] = formatter(raw_df[col].sum())
    total_row[display_df.columns[0]] = label
    return pd.concat([display_df, pd.DataFrame([total_row])], ignore_index=True)


@st.cache_data(show_spinner=False)
def load_cas(file_bytes: bytes) -> CASData:
    """
    Auto-detects which of the two supported statement formats this is - a
    CDSL Consolidated Account Statement, or an MF Central Consolidated
    Account Summary (CAMS+KFintech) - and routes to the matching parser.
    Both produce the same CASData shape, so nothing downstream needs to
    know or care which one a given person's file was.
    """
    with pdfplumber.open(BytesIO(file_bytes)) as pdf:
        is_mfcentral = mfcentral_parser.looks_like_mfcentral(pdf)
    if is_mfcentral:
        return mfcentral_parser.parse_mfcentral_summary(BytesIO(file_bytes))
    return parse_cas(BytesIO(file_bytes))


# --------------------------------------------------------------------------
# Sidebar - upload
# --------------------------------------------------------------------------

st.sidebar.title("📊 Portfolio Tracker")
st.sidebar.caption(
    "Nothing you upload leaves this session - PDFs are parsed in memory only. Accepts "
    "either a CDSL CAS or an MF Central Consolidated Account Summary - auto-detected, "
    "mix and match freely across family members."
)
uploaded_files = st.sidebar.file_uploader(
    "Upload CAS PDF(s) - one per family member for a combined household view, or just "
    "one for yourself",
    type=["pdf"], accept_multiple_files=True,
)

with st.sidebar.expander("📅 Compare to last month (optional)"):
    st.caption(
        "Your CAS only tracks *total* portfolio value month-to-month, not a breakdown by "
        "asset class or instrument type. Upload last month's CAS(es) here to get real "
        "variance columns broken down that way - doesn't need to match the current set "
        "one-for-one, everyone's previous CAS just gets combined the same way."
    )
    prev_uploaded_files = st.sidebar.file_uploader(
        "Upload previous month's CAS PDF(s)", type=["pdf"], accept_multiple_files=True, key="prev_cas_upload"
    )

st.sidebar.markdown("---")

# --------------------------------------------------------------------------
# Sidebar - Save / Resume Session JSON
# --------------------------------------------------------------------------

def _cas_to_dict(cas: CASData) -> dict:
    """Serializes a CASData object to dicts for JSON export"""
    return {
        "holder_name": cas.holder_name,
        "total_value": cas.total_value,
        "asset_summary": cas.asset_summary.to_dict(orient="records") if not cas.asset_summary.empty else [],
        "equity_holdings": cas.equity_holdings.to_dict(orient="records") if not cas.equity_holdings.empty else [],
        "mf_in_demat_holdings": cas.mf_in_demat_holdings.to_dict(orient="records") if not cas.mf_in_demat_holdings.empty else [],
        "other_holdings": cas.other_holdings.to_dict(orient="records") if not cas.other_holdings.empty else [],
        "mf_folio_holdings": cas.mf_folio_holdings.to_dict(orient="records") if not cas.mf_folio_holdings.empty else [],
        "valuation_trend": cas.valuation_trend.to_dict(orient="records") if not cas.valuation_trend.empty else [],
    }

def _dict_to_cas(d: dict) -> CASData:
    """Deserializes dicts back to a CASData object, preserving columns even if empty"""
    cas = CASData()
    cas.holder_name = d.get("holder_name", "")
    cas.total_value = d.get("total_value", 0.0)
    
    def _restore_df(data_list, empty_cols):
        if not data_list:
            return pd.DataFrame(columns=empty_cols)
        return pd.DataFrame(data_list)
        
    cas.asset_summary = _restore_df(d.get("asset_summary", []), ["Asset Class", "Value (₹)", "% of Portfolio"])
    cas.equity_holdings = _restore_df(d.get("equity_holdings", []), ["ISIN", "Security", "Instrument Type", "Value (₹)"])
    cas.mf_in_demat_holdings = _restore_df(d.get("mf_in_demat_holdings", []), ["ISIN", "Security", "Instrument Type", "Value (₹)"])
    cas.other_holdings = _restore_df(d.get("other_holdings", []), ["ISIN", "Security", "Instrument Type", "Value (₹)", "Category"])
    cas.mf_folio_holdings = _restore_df(d.get("mf_folio_holdings", []), ["Scheme", "ISIN", "Instrument Type", "Folio No.", "Invested (₹)", "Valuation (₹)", "Unrealised P/L (₹)", "Unrealised P/L (%)"])
    cas.valuation_trend = _restore_df(d.get("valuation_trend", []), ["Month", "Portfolio Value (₹)", "Change (₹)", "Change (%)"])
    
    return cas

with st.sidebar.expander("💾 Save or resume session data"):
    st.caption("Download your loaded CAS data and transactions as a JSON, or upload a previous JSON here to restore the entire dashboard without your PDFs.")
    
    # Save Button
    if st.session_state.holder_to_data or st.session_state.txn_sources:
        import json as _json
        def _session_to_json() -> bytes:
            fh = st.session_state.get('last_family_holders', [])
            payload = {
                "version": 2, # Bumped version for full CAS state saving
                "exported_at": datetime.now().isoformat(),
                "family_holders": fh,
                "txn_sources": {
                    key: df.assign(Date=df["Date"].astype(str)).to_dict(orient="records")
                    for key, df in st.session_state.txn_sources.items()
                },
                "holder_to_data": {
                    k: _cas_to_dict(v) for k, v in st.session_state.holder_to_data.items()
                },
                "prev_holder_to_data": {
                    k: _cas_to_dict(v) for k, v in st.session_state.prev_holder_to_data.items()
                }
            }
            return _json.dumps(payload, indent=2, default=str).encode("utf-8")

        st.download_button(
            "⬇️ Download session (JSON)",
            data=_session_to_json(),
            file_name=f"portfolio_session_{date.today().isoformat()}.json",
            mime="application/json",
        )
    else:
        st.caption("Nothing loaded yet to save.")
        
    st.markdown("---")
    
    # Display visual success message if a file was just processed
    if st.session_state.get("show_json_success"):
        st.success("✅ Session data restored successfully!")
        st.session_state["show_json_success"] = False
    
    # Resume Uploader
    resume_file = st.file_uploader(
        "Upload a saved session JSON", type=["json"],
        key=f"resume_upload_{st.session_state.uploader_nonce.get('resume', 0)}",
    )
    
    if resume_file is not None:
        import json as _json
        try:
            payload = _json.loads(resume_file.getvalue().decode("utf-8"))
            
            # Verify it's actually an app-generated session file
            if not isinstance(payload, dict) or ("txn_sources" not in payload and "holder_to_data" not in payload):
                st.error("Invalid format: Please upload a JSON session file generated by this app.")
            else:
                restored_items = 0
                
                # Restore Transactions
                for key, records in payload.get("txn_sources", {}).items():
                    if not records:
                        continue
                    df = pd.DataFrame(records)
                    df["Date"] = pd.to_datetime(df["Date"]).dt.date
                    st.session_state.txn_sources[key] = df
                    restored_items += 1
                
                # Restore CAS state across all tabs
                if "holder_to_data" in payload and payload["holder_to_data"]:
                    st.session_state.holder_to_data = {
                        k: _dict_to_cas(v) for k, v in payload["holder_to_data"].items()
                    }
                    restored_items += 1
                    
                if "prev_holder_to_data" in payload and payload["prev_holder_to_data"]:
                    st.session_state.prev_holder_to_data = {
                        k: _dict_to_cas(v) for k, v in payload["prev_holder_to_data"].items()
                    }
                    
                if restored_items > 0:
                    # Set success flag
                    st.session_state["show_json_success"] = True
                    
                    # Safely clear the uploader widget by updating the nonce
                    new_nonces = st.session_state.uploader_nonce.copy()
                    new_nonces["resume"] = new_nonces.get("resume", 0) + 1
                    st.session_state.uploader_nonce = new_nonces
                    
                    st.rerun()
                else:
                    st.warning("That file didn't have any parseable portfolio or transaction sources in it.")
        except Exception as e:
            st.error(f"Couldn't read that session file: {e}")
            
    if st.session_state.holder_to_data or st.session_state.txn_sources:
        if st.button("🗑️ Clear all loaded data", use_container_width=True):
            st.session_state.holder_to_data = {}
            st.session_state.prev_holder_to_data = {}
            st.session_state.txn_sources = {}
            st.session_state.zerodha_holdings = None
            for k in st.session_state.uploader_nonce:
                st.session_state.uploader_nonce[k] += 1
            st.rerun()

st.sidebar.markdown("---")
st.sidebar.caption(
    "Get your CDSL CAS from **cdslindia.com** → \"Register for easi/easiest\" → e-CAS, "
    "or get an MF Central summary from **mfcentral.com** (mutual funds only, no demat "
    "equity - useful if that's all you or a family member holds)."
)

def _assign_holder_names(files) -> dict[str, CASData]:
    """
    Parses each uploaded file and picks a holder label for it: the name
    CDSL prints on the statement itself if we could extract it, else the
    filename. Shows an editable confirmation list in the sidebar so a
    misread name or an unwanted duplicate can be fixed before anything
    downstream uses it as a grouping key.
    """
    result = {}
    seen = set()
    st.sidebar.markdown("**Confirm who's who:**")
    for i, f in enumerate(files):
        with st.spinner(f"Parsing {f.name}..."):
            parsed = load_cas(f.getvalue())
        if parsed.total_value == 0:
            st.sidebar.error(f"Couldn't parse '{f.name}' as a CDSL CAS - skipped.")
            continue
        default_name = parsed.holder_name or os.path.splitext(f.name)[0]
        label = st.sidebar.text_input(
            f"{f.name}", value=default_name, key=f"holder_label_{i}",
            help="Editable - fix this if the name was misread or you'd rather use a nickname.",
        )
        label = label.strip() or default_name
        if label in seen:
            label = f"{label} ({i+1})"
        seen.add(label)
        result[label] = parsed
    return result

# Integrate PDF uploads into session state
if uploaded_files:
    st.session_state.holder_to_data = _assign_holder_names(uploaded_files)

# Fetch latest state
holder_to_data = st.session_state.holder_to_data

# Bypass the "Stop" wall if session data is loaded
if not holder_to_data:
    st.title("Portfolio Summary & XIRR Tracker")
    st.write(
        "Upload one or more CDSL Consolidated Account Statement (CAS) PDFs in the sidebar "
        "to get a tabular breakdown of your holdings by asset class - OR upload a saved JSON "
        "session file to restore your portfolio dashboard directly."
    )
    st.info(
        "**A quick note on XIRR:** a monthly CAS shows your *current* holdings and, for "
        "mutual funds, the cumulative amount invested - but not the dates of each individual "
        "purchase or redemption. True XIRR needs those dates. This app will show you the "
        "absolute return already computed for each mutual fund, and lets you optionally "
        "upload a transaction history (see the XIRR tab) to get real, dated XIRR by "
        "holding and by asset class."
    )
    st.stop()

# Continue building dashboard based on Restored OR Uploaded data
data = combine_family(holder_to_data)
family_holders = list(holder_to_data.keys())
st.session_state['last_family_holders'] = family_holders
is_family = len(family_holders) > 1

if data.total_value == 0:
    st.error(
        "Couldn't find holdings in this PDF. Make sure it's a CDSL CAS "
        "(Consolidated Account Statement) - the file CDSL emails monthly."
    )
    st.stop()

prev_data = None
if prev_uploaded_files:
    prev_h_to_d = {}
    for i, f in enumerate(prev_uploaded_files):
        with st.spinner(f"Parsing previous CAS {f.name}..."):
            parsed = load_cas(f.getvalue())
        if parsed.total_value > 0:
            prev_h_to_d[parsed.holder_name or f"{f.name}_{i}"] = parsed
        else:
            st.sidebar.error(f"Couldn't parse previous-month file '{f.name}' - skipped.")
    st.session_state.prev_holder_to_data = prev_h_to_d

prev_holder_to_data = st.session_state.prev_holder_to_data
if prev_holder_to_data:
    prev_data = combine_family(prev_holder_to_data)

def _with_variance(current: pd.DataFrame, previous: pd.DataFrame | None, key_cols: list[str], value_col: str) -> pd.DataFrame:
    """Left-joins current onto previous by key_cols and adds Δ₹/Δ% columns. Passthrough if no previous data."""
    if previous is None or previous.empty:
        return current
    prev_slim = previous.groupby(key_cols, as_index=False)[value_col].sum().rename(columns={value_col: "_prev_value"})
    merged = current.merge(prev_slim, on=key_cols, how="left")
    merged["Change vs Last Month (₹)"] = merged[value_col] - merged["_prev_value"]
    merged["Change vs Last Month (%)"] = (
        (merged[value_col] / merged["_prev_value"] - 1) * 100
    ).where(merged["_prev_value"].notna() & (merged["_prev_value"] != 0))
    return merged.drop(columns="_prev_value")


# Instrument-type breakdown across all holdings tables, with month-over-month
# variance if a previous CAS was supplied.
instrument_breakdown = instrument_type_breakdown(data)
instrument_breakdown_prev = instrument_type_breakdown(prev_data) if prev_data is not None else None
instrument_breakdown = _with_variance(
    instrument_breakdown, instrument_breakdown_prev, ["Asset Class", "Instrument Type"], "Value (₹)"
)

# Asset-class summary with the same variance treatment.
asset_summary_with_variance = _with_variance(
    data.asset_summary.copy(), prev_data.asset_summary if prev_data is not None else None,
    ["Asset Class"], "Value (₹)",
)


# --------------------------------------------------------------------------
# Header metrics
# --------------------------------------------------------------------------

st.title("Portfolio Summary & XIRR Tracker")
col1, col2, col3 = st.columns(3)
col1.metric("Total Portfolio Value", fmt_inr(data.total_value))
if not data.valuation_trend.empty and len(data.valuation_trend) >= 2:
    prev = data.valuation_trend.iloc[-2]["Portfolio Value (₹)"]
    curr = data.valuation_trend.iloc[-1]["Portfolio Value (₹)"]
    col2.metric("Change vs Last Month", fmt_inr(curr - prev), f"{(curr/prev - 1)*100:.2f}%")
mf_total_invested = data.mf_folio_holdings["Invested (₹)"].sum() if not data.mf_folio_holdings.empty else 0
mf_total_val = data.mf_folio_holdings["Valuation (₹)"].sum() if not data.mf_folio_holdings.empty else 0
if mf_total_invested:
    col3.metric(
        "Mutual Funds Return (absolute)",
        f"{(mf_total_val/mf_total_invested - 1)*100:.2f}%",
        help="Weighted absolute return across mutual fund folios (invested vs current valuation). Not annualised.",
    )

# --------------------------------------------------------------------------
# Holder view filter - only shown in family mode. Affects the Asset Class
# Summary and Holdings Detail tabs (and the exports, so what you download
# matches what you're looking at) by swapping in that one person's own
# already-parsed CASData instead of the family-combined one. XIRR stays
# family-wide regardless - it has its own per-holder breakdown instead,
# since blending it into this filter would mean re-deriving XIRR from a
# filtered transaction set rather than reusing what's already computed.
# --------------------------------------------------------------------------

view_data, view_instrument_breakdown, view_asset_summary = data, instrument_breakdown, asset_summary_with_variance
holder_view = "👪 All Family"
if is_family:
    holder_view = st.radio(
        "Viewing", ["👪 All Family"] + family_holders, horizontal=True, key="holder_view_filter",
    )
    if holder_view != "👪 All Family":
        view_data = holder_to_data[holder_view]
        view_instrument_breakdown = instrument_type_breakdown(view_data)
        prev_match = prev_holder_to_data.get(holder_view)
        view_instrument_breakdown = _with_variance(
            view_instrument_breakdown,
            instrument_type_breakdown(prev_match) if prev_match is not None else None,
            ["Asset Class", "Instrument Type"], "Value (₹)",
        )
        view_asset_summary = _with_variance(
            view_data.asset_summary.copy(),
            prev_match.asset_summary if prev_match is not None else None,
            ["Asset Class"], "Value (₹)",
        )
        if prev_uploaded_files and prev_match is None:
            st.caption(
                f"💡 No previous-month file matched to '{holder_view}' by name - upload one "
                "labelled the same way for variance columns here too."
            )

tab_summary, tab_holdings, tab_connect, tab_xirr, tab_trend = st.tabs(
    ["📋 Asset Class Summary", "🔍 Holdings Detail", "🔗 Connect & Import", "📈 XIRR", "🕒 12-Month Trend"]
)


# --------------------------------------------------------------------------
# XIRR computation - done once here (not inside the XIRR tab) so the
# Excel/PDF export section below can include it regardless of which tab is
# open. Streamlit runs every `with tab:` block on every rerun anyway - tabs
# only control what's *displayed*, not what code executes - but keeping the
# actual numbers in one place avoids computing them twice.
# --------------------------------------------------------------------------

txns = pd.concat(st.session_state.txn_sources.values(), ignore_index=True) if st.session_state.txn_sources else None
value_lookup = {}
xirr_by_holding_df = pd.DataFrame()
xirr_by_asset_class_df = pd.DataFrame()
xirr_by_instrument_type_df = pd.DataFrame()
xirr_by_holder_df = pd.DataFrame()

if txns is not None and not txns.empty:
    # Older txn_sources entries (saved before Holder-tagging existed) won't
    # have this column - backfill with the only sensible default so a
    # returning session doesn't hard-crash on the very people this feature
    # is for.
    if "Holder" not in txns.columns:
        txns["Holder"] = family_holders[0]
    txns["Holder"] = txns["Holder"].fillna(family_holders[0])

    # Defend against non-numeric or missing Amount values (stray commas,
    # currency symbols, blank cells, text like "N/A") from any source -
    # most likely the manual CSV path, since that one's hand-edited -
    # rather than crashing deep inside the XIRR solver with an opaque
    # TypeError. Rows that can't be used are dropped with a visible warning
    # instead of silently disappearing from every XIRR calculation on the
    # page. Note pandas' own CSV reader already treats things like "N/A"
    # and blank cells as missing before this code ever sees them, so the
    # "did sanitising break it" check can't rely on comparing before/after -
    # it has to flag anything left over Amount-less, from any source.
    _original_row_count = len(txns)
    _bad_row_examples = (
        txns.loc[txns["Amount"].isna(), "Description"].astype(str).head(3).tolist()
        if txns["Amount"].isna().any() else []
    )
    txns["Amount"] = pd.to_numeric(
        txns["Amount"].astype(str).str.replace(r"[₹,\s]", "", regex=True), errors="coerce"
    )
    _bad_rows = txns[txns["Amount"].isna()]
    if not _bad_rows.empty:
        _examples = _bad_row_examples or _bad_rows["Description"].astype(str).head(3).tolist()
        st.warning(
            f"⚠️ {len(_bad_rows)} of {_original_row_count} transaction row(s) have an Amount "
            f"that isn't a plain number - e.g. {', '.join(repr(e) for e in _examples)}. "
            "These were excluded from every XIRR calculation below rather than causing an "
            "error. Check the Connect & Import tab - this is usually a manually-edited CSV "
            "with a stray currency symbol, comma, blank cell, or text value."
        )
    txns = txns.dropna(subset=["Amount"])

    # Transactions for a security/scheme that couldn't be matched to any
    # current CAS holding (usually something fully sold/redeemed before the
    # CAS's date) are tagged "UNMATCHED::<name>" rather than a real ISIN, by
    # both the Kuvera and GLC importers - exclude these from XIRR entirely
    # rather than computing a return CDSL/CAMS/GLC never actually confirmed
    # the identity of.
    _unmatched_mask = txns["Identifier"].astype(str).str.startswith("UNMATCHED::")
    if _unmatched_mask.any():
        st.info(
            f"ℹ️ Excluded {_unmatched_mask.sum()} transaction(s) for securities/schemes that "
            "couldn't be matched to a current CAS holding (usually something fully sold or "
            "redeemed before the CAS's date) from every XIRR calculation below. See the "
            "match-summary table in Connect & Import → Kuvera or → GLC (PMS) for details."
        )
    txns = txns[~_unmatched_mask]

if txns is not None and not txns.empty:
    as_of = date.today()
    itype_lookup = isin_to_instrument_type(data)

    # Keyed by (Holder, ISIN), never just ISIN - the same stock or fund can
    # legitimately be held by more than one family member, each with their
    # own cost basis, and an ISIN-only lookup would silently merge or
    # misattribute their positions. Within one holder, the same ISIN can
    # still legitimately appear in more than one CAS table (a fund held
    # partly as a folio and partly in demat form, or split across two SIP
    # folios) - accumulate, never overwrite.
    _value_lookup = defaultdict(float)
    for hdf, col in [
        (data.mf_folio_holdings, "Valuation (₹)"),
        (data.equity_holdings, "Value (₹)"),
        (data.mf_in_demat_holdings, "Value (₹)"),
        (data.other_holdings, "Value (₹)"),
    ]:
        if not hdf.empty:
            for (holder, isin), val in hdf.groupby(["Holder", "ISIN"])[col].sum().items():
                _value_lookup[(holder, isin)] += val
    if st.session_state.zerodha_holdings is not None and not st.session_state.zerodha_holdings.empty:
        zh = st.session_state.zerodha_holdings
        zh_holder_col = zh["Holder"] if "Holder" in zh.columns else pd.Series([family_holders[0]] * len(zh))
        for (holder, isin), val in zh.assign(_h=zh_holder_col).groupby(["_h", "ISIN"])["Current Value (₹)"].sum().items():
            _value_lookup[(holder, isin)] = val
    value_lookup = dict(_value_lookup)

    def _lookup_value(holder, ident):
        return value_lookup.get((holder, str(ident).strip()))

    holding_rows = []
    for (holder, ident), grp in txns.groupby(["Holder", "Identifier"]):
        flows = [CashFlow(r["Date"], r["Amount"]) for _, r in grp.iterrows() if r["Amount"] != 0]
        current_val = _lookup_value(holder, ident)
        if current_val is not None:
            flows.append(CashFlow(as_of, current_val))
        result = xirr(flows) if len(flows) >= 2 else None
        desc = grp["Description"].iloc[0] if "Description" in grp.columns else ""
        asset_class = grp["AssetClass"].iloc[0] if "AssetClass" in grp.columns else ""
        holding_rows.append(
            {
                "Holder": holder,
                "Identifier": ident,
                "Description": desc,
                "Asset Class": asset_class,
                "Instrument Type": itype_lookup.get(str(ident).strip(), "Unknown"),
                "Current Value (₹)": current_val if current_val is not None else 0.0,
                "XIRR (%)": result * 100 if result is not None else None,
            }
        )
    xirr_by_holding_df = pd.DataFrame(holding_rows)
    if not is_family and not xirr_by_holding_df.empty:
        xirr_by_holding_df = xirr_by_holding_df.drop(columns="Holder")

    def _grouped_xirr(group_col: str) -> pd.DataFrame:
        rows = []
        merged = txns.merge(
            pd.DataFrame({"Identifier": list(itype_lookup.keys()), "Instrument Type": list(itype_lookup.values())}),
            on="Identifier", how="left",
        ) if group_col == "Instrument Type" else txns
        if group_col == "Instrument Type":
            merged["Instrument Type"] = merged["Instrument Type"].fillna("Unknown")
        for key, grp in merged.groupby(group_col):
            flows = [CashFlow(r["Date"], r["Amount"]) for _, r in grp.iterrows() if r["Amount"] != 0]
            pairs = grp[["Holder", "Identifier"]].drop_duplicates()
            total_current = sum(_lookup_value(h, i) or 0 for h, i in pairs.itertuples(index=False))
            if total_current:
                flows.append(CashFlow(as_of, total_current))
            result = xirr(flows) if len(flows) >= 2 else None
            rows.append({group_col: key, "Current Value (₹)": total_current, "XIRR (%)": result * 100 if result is not None else None})
        return pd.DataFrame(rows).sort_values("Current Value (₹)", ascending=False).reset_index(drop=True)

    xirr_by_asset_class_df = _grouped_xirr("AssetClass").rename(columns={"AssetClass": "Asset Class"})
    xirr_by_instrument_type_df = _grouped_xirr("Instrument Type")
    if is_family:
        xirr_by_holder_df = _grouped_xirr("Holder")


# --------------------------------------------------------------------------
# Pre-build Charts for UI and Export
# --------------------------------------------------------------------------
# Vibrant Color Palette
color_sequence = px.colors.qualitative.Vivid

# 1. Asset Class Pie Chart
pie_df = view_data.asset_summary.copy()
fig_pie = None
if not pie_df.empty:
    pie_df["Value (formatted)"] = pie_df["Value (₹)"].apply(fmt_inr)
    fig_pie = px.pie(
        pie_df, values="Value (₹)", names="Asset Class", hole=0.45, 
        custom_data=["Value (formatted)"], color_discrete_sequence=color_sequence
    )
    fig_pie.update_traces(textinfo="percent+label", hovertemplate="%{label}<br>%{customdata[0]}<br>%{percent}<extra></extra>")
    fig_pie.update_layout(showlegend=False, margin=dict(t=10, b=10, l=10, r=10), height=350)

# 2. Instrument Breakdown Bar Chart
fig_ib = None
if not view_instrument_breakdown.empty:
    ib_sorted = view_instrument_breakdown.sort_values("Value (₹)")
    fig_ib = px.bar(
        ib_sorted, x="Value (₹)", y="Instrument Type", color="Asset Class", orientation="h",
        text=ib_sorted["Value (₹)"].apply(fmt_inr_short), color_discrete_sequence=color_sequence
    )
    fig_ib.update_traces(textposition="outside", cliponaxis=False)
    # Removing tickvals to prevent overlapping axis text
    fig_ib.update_xaxes(title="Value (₹)")
    fig_ib.update_layout(
        margin=dict(t=40, b=10, l=10, r=10), height=100 + 32 * len(ib_sorted), 
        yaxis_title="", legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0)
    )

# 3. 12-Month Trend Line Chart
fig_trend = None
if not data.valuation_trend.empty:
    fig_trend = px.line(
        data.valuation_trend, x="Month", y="Portfolio Value (₹)", markers=True, 
        color_discrete_sequence=color_sequence
    )
    # Removing tickvals to prevent overlapping axis text
    fig_trend.update_yaxes(title="Portfolio Value (₹)")
    fig_trend.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=350)

# 4. Bubble Chart (Pre-generated for PDF export using Asset Class)
fig_bubble_export = None
if not xirr_by_asset_class_df.empty:
    chart_df_exp = xirr_by_asset_class_df.dropna(subset=["XIRR (%)"]).copy()
    if not chart_df_exp.empty:
        chart_df_exp["Current Value"] = chart_df_exp["Current Value (₹)"].apply(fmt_inr)
        fig_bubble_export = px.scatter(
            chart_df_exp, x="Asset Class", y="XIRR (%)", size="Current Value (₹)",
            color="Asset Class", size_max=70, color_discrete_sequence=color_sequence,
            title="Size vs. Return (by Asset Class)",
            hover_data={"Current Value (₹)": False, "Current Value": True, "Asset Class": False}
        )
        fig_bubble_export.add_hline(y=0, line_dash="dot", line_color="grey")

export_charts = [fig for fig in [fig_pie, fig_ib, fig_trend, fig_bubble_export] if fig is not None]

# --------------------------------------------------------------------------
# Download everything - Excel workbook + PDF report, from whatever's loaded
# --------------------------------------------------------------------------
def _append_raw_total(df: pd.DataFrame, cols_to_sum: list, label: str = "Total") -> pd.DataFrame:
    if df is None or df.empty:
        return df
    totals = {col: "" for col in df.columns}
    totals[df.columns[0]] = label
    for col in cols_to_sum:
        if col in df.columns:
            totals[col] = df[col].sum()
    return pd.concat([df, pd.DataFrame([totals])], ignore_index=True)

# Format the correct Equity dataframe for export (checking for Zerodha Live data)
export_equity = view_data.equity_holdings
zh_export = st.session_state.zerodha_holdings
if zh_export is not None and not zh_export.empty:
    zh_view = zh_export.copy()
    if is_family and holder_view != "👪 All Family":
        zh_view = zh_view[zh_view["Holder"] == holder_view]
    zh_view.insert(1, "Instrument Type", zh_view["Symbol"].apply(instrument_classifier.classify_equity))
    export_equity = zh_view

export_sections = {
    "Asset Class Summary": _append_raw_total(view_asset_summary, ["Value (₹)", "Change vs Last Month (₹)"]),
    "Instrument Type Breakdown": _append_raw_total(view_instrument_breakdown, ["Value (₹)", "Change vs Last Month (₹)"]),
    "Mutual Funds": _append_raw_total(view_data.mf_folio_holdings, ["Invested (₹)", "Valuation (₹)", "Unrealised P/L (₹)"]),
    "Equity": _append_raw_total(export_equity, ["Value (₹)", "Invested (₹)", "Current Value (₹)", "Unrealised P/L (₹)"]),
    "MF Held in Demat": _append_raw_total(view_data.mf_in_demat_holdings, ["Value (₹)"]),
    "Others (Govt Sec)": _append_raw_total(view_data.other_holdings, ["Value (₹)"]),
    "12M Trend": view_data.valuation_trend, # Trend totals don't make numerical sense
    "XIRR by Holding": _append_raw_total(xirr_by_holding_df, ["Current Value (₹)"]),
    "XIRR by Asset Class": _append_raw_total(xirr_by_asset_class_df, ["Current Value (₹)"]),
    "XIRR by Instrument Type": _append_raw_total(xirr_by_instrument_type_df, ["Current Value (₹)"]),
    "XIRR by Family Member": _append_raw_total(xirr_by_holder_df, ["Current Value (₹)"]),
}

with st.container(border=True):
    dl_col1, dl_col2, dl_note = st.columns([1, 1, 3])
    with dl_col1:
        st.download_button(
            "📊 Download all tables (Excel)",
            data=report_export.build_excel(export_sections),
            file_name=f"portfolio_report_{date.today().isoformat()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    with dl_col2:
        pdf_summary = {"Total Portfolio Value": fmt_inr(view_data.total_value)}
        if not xirr_by_asset_class_df.empty:
            for _, r in xirr_by_asset_class_df.iterrows():
                if pd.notna(r["XIRR (%)"]) and not isinstance(r["XIRR (%)"], str):
                    pdf_summary[f"XIRR - {r['Asset Class']}"] = f"{float(r['XIRR (%)']):.2f}%"
        pdf_bytes = report_export.build_pdf(
            title="Portfolio Summary Report" + (f" - {holder_view}" if is_family and holder_view != "👪 All Family" else ""),
            generated_note=f"Generated {date.today().isoformat()} from CAS data",
            summary_metrics=pdf_summary,
            sections=list(export_sections.items()),
            charts=export_charts
        )
        st.download_button(
            "📄 Download PDF report",
            data=pdf_bytes,
            file_name=f"portfolio_report_{date.today().isoformat()}.pdf",
            mime="application/pdf",
        )
    with dl_note:
        st.caption(
            "Both include every table currently loaded - asset class & instrument type "
            "breakdowns always, XIRR tables too once you've loaded transaction history in "
            "Connect & Import."
            + (" Matches your current **Viewing** selection above." if is_family else "")
        )


# --------------------------------------------------------------------------
# Tab 1: Asset class summary
# --------------------------------------------------------------------------

with tab_summary:
    st.subheader("Holdings by asset class")
    if fig_pie:
        st.plotly_chart(fig_pie, use_container_width=True)
    
    display_df = view_asset_summary.copy()
    display_df["Value (₹)"] = display_df["Value (₹)"].apply(fmt_inr)
    display_df["% of Portfolio"] = display_df["% of Portfolio"].apply(lambda x: f"{x:.2f}%")
    if "Change vs Last Month (₹)" in display_df.columns:
        display_df["Change vs Last Month (₹)"] = display_df["Change vs Last Month (₹)"].apply(fmt_inr)
        display_df["Change vs Last Month (%)"] = display_df["Change vs Last Month (%)"].apply(
            lambda x: f"{x:+.2f}%" if pd.notna(x) else "-"
        )
    total_specs = {"Value (₹)": fmt_inr, "% of Portfolio": lambda x: f"{x:.2f}%"}
    if "Change vs Last Month (₹)" in view_asset_summary.columns:
        total_specs["Change vs Last Month (₹)"] = fmt_inr
    display_df = _with_total_row(display_df, view_asset_summary, total_specs)
    st.dataframe(display_df, hide_index=True, width="stretch")
    if "Change vs Last Month (₹)" not in view_asset_summary.columns:
        st.caption("💡 Upload last month's CAS in the sidebar to see variance columns here.")
        
    st.markdown("---")
    st.subheader("Breakdown by instrument type")
    st.caption(
        "Splits each asset class further - e.g. Mutual Fund Folios into Equity Fund, "
        "Index Fund, and debt sub-types like Liquid Fund, Gilt/Treasury Fund, and Banking "
        "& PSU Fund. Inferred from scheme/security names, since the CAS itself doesn't "
        "label this."
    )
    if fig_ib:
        st.plotly_chart(fig_ib, use_container_width=True)
        
    ib_display = view_instrument_breakdown.copy()
    ib_display["Value (₹)"] = ib_display["Value (₹)"].apply(fmt_inr)
    ib_display["% of Portfolio"] = ib_display["% of Portfolio"].apply(lambda x: f"{x:.2f}%")
    if "Change vs Last Month (₹)" in ib_display.columns:
        ib_display["Change vs Last Month (₹)"] = ib_display["Change vs Last Month (₹)"].apply(fmt_inr)
        ib_display["Change vs Last Month (%)"] = ib_display["Change vs Last Month (%)"].apply(
            lambda x: f"{x:+.2f}%" if pd.notna(x) else "-"
        )
    ib_total_specs = {"Value (₹)": fmt_inr, "% of Portfolio": lambda x: f"{x:.2f}%"}
    if "Change vs Last Month (₹)" in view_instrument_breakdown.columns:
        ib_total_specs["Change vs Last Month (₹)"] = fmt_inr
    ib_display = _with_total_row(ib_display, view_instrument_breakdown, ib_total_specs)
    st.dataframe(ib_display, hide_index=True, width="stretch")


# --------------------------------------------------------------------------
# Tab 2: Holdings detail
# --------------------------------------------------------------------------

with tab_holdings:
    st.subheader("Mutual Fund Folios")
    if not view_data.mf_folio_holdings.empty:
        mf_display = view_data.mf_folio_holdings.copy()
        for c in ["Invested (₹)", "Valuation (₹)", "Unrealised P/L (₹)"]:
            mf_display[c] = mf_display[c].apply(fmt_inr)
        mf_display["Unrealised P/L (%)"] = mf_display["Unrealised P/L (%)"].apply(lambda x: f"{x:.2f}%")
        mf_display = _with_total_row(
            mf_display, view_data.mf_folio_holdings,
            {"Invested (₹)": fmt_inr, "Valuation (₹)": fmt_inr, "Unrealised P/L (₹)": fmt_inr},
        )
        st.dataframe(mf_display, hide_index=True, width="stretch")
    else:
        st.caption("No mutual fund folios found.")

    st.subheader("Equity Holdings")
    zh = st.session_state.zerodha_holdings
    if zh is not None and not zh.empty and "Holder" in zh.columns and is_family and holder_view != "👪 All Family":
        zh = zh[zh["Holder"] == holder_view]
    if zh is not None and not zh.empty:
        eq_display = zh.copy()
        eq_display.insert(1, "Instrument Type", eq_display["Symbol"].apply(instrument_classifier.classify_equity))
        for c in ["Avg. Buy Price (₹)", "Invested (₹)", "Last Price (₹)", "Current Value (₹)", "Unrealised P/L (₹)"]:
            eq_display[c] = eq_display[c].apply(fmt_inr)
        eq_display["Unrealised P/L (%)"] = eq_display["Unrealised P/L (%)"].apply(lambda x: f"{x:.2f}%")
        eq_display = _with_total_row(
            eq_display, zh,
            {"Invested (₹)": fmt_inr, "Current Value (₹)": fmt_inr, "Unrealised P/L (₹)": fmt_inr},
        )
        st.dataframe(eq_display, hide_index=True, width="stretch")
        st.caption(
            "Cost basis and return % from your connected Zerodha account (avg. buy price via "
            "Kite Connect) - the CAS alone doesn't carry this. Values may differ slightly from "
            "the CAS if it's not from today, since prices move."
        )
    elif not view_data.equity_holdings.empty:
        eq_display = view_data.equity_holdings.copy()
        eq_display["Value (₹)"] = eq_display["Value (₹)"].apply(fmt_inr)
        eq_display = _with_total_row(eq_display, view_data.equity_holdings, {"Value (₹)": fmt_inr})
        st.dataframe(eq_display, hide_index=True, width="stretch")
        st.caption(
            "Note: the CAS only reports current market value for equities, not your original "
            "purchase cost - so no return % is shown here. Connect Zerodha in the "
            "**Connect & Import** tab to get real cost basis, or upload a tradebook there for XIRR."
        )
    else:
        st.caption("No direct equity holdings found.")

    st.subheader("Mutual Funds Held in Demat Form")
    if not view_data.mf_in_demat_holdings.empty:
        d_display = view_data.mf_in_demat_holdings.copy()
        d_display["Value (₹)"] = d_display["Value (₹)"].apply(fmt_inr)
        d_display = _with_total_row(d_display, view_data.mf_in_demat_holdings, {"Value (₹)": fmt_inr})
        st.dataframe(d_display, hide_index=True, width="stretch")
    else:
        st.caption("None found.")

    st.subheader("Others (Government Securities / Sovereign Gold Bonds)")
    if not view_data.other_holdings.empty:
        o_display = view_data.other_holdings.copy()
        o_display["Value (₹)"] = o_display["Value (₹)"].apply(fmt_inr)
        o_display = _with_total_row(o_display, view_data.other_holdings, {"Value (₹)": fmt_inr})
        st.dataframe(o_display, hide_index=True, width="stretch")
    else:
        st.caption("None found.")


# --------------------------------------------------------------------------
# Tab 3: Connect & Import - the sources that feed real XIRR
# --------------------------------------------------------------------------

with tab_connect:
    st.write(
        "Real XIRR needs dated cash flows - your CAS alone doesn't have them (see the XIRR tab "
        "for why). Bring them in from any combination of the sources below; everything you load "
        "here gets pooled together for the XIRR tab."
    )
    if is_family:
        st.caption(
            f"👪 Family mode - {len(family_holders)} people loaded ({', '.join(family_holders)}). "
            "Each source below asks which family member it belongs to, since the same stock or "
            "fund can be held by more than one person with a different cost basis."
        )

    def _holder_picker(key: str) -> str:
        """Only shows a picker when there's more than one person loaded -
        no point asking in the common single-user case."""
        if not is_family:
            return family_holders[0]
        return st.selectbox("Which family member is this for?", family_holders, key=key)

    src_zerodha_live, src_zerodha_csv, src_kuvera, src_glc, src_manual = st.tabs(
        ["Zerodha (live)", "Zerodha (Tradebook CSV)", "Kuvera (statement)", "GLC (PMS)", "Manual CSV"]
    )

    # ---- Zerodha live connect (Kite Connect Personal API - free) ----
    with src_zerodha_live:
        st.markdown(
            "Pulls your **current holdings with average buy price** via Zerodha's official, "
            "free Kite Connect Personal API. This gives real cost basis for equities (which "
            "shows up in the Holdings Detail tab) - but *not* purchase dates, so it feeds cost "
            "basis, not XIRR. For dated history, use the Tradebook CSV tab instead."
        )
        st.caption(
            "Get a free API key + secret at [developers.kite.trade](https://developers.kite.trade) "
            "→ My Apps → Create New App → type **Personal**. Nothing you enter here is stored "
            "outside this browser session."
        )

        zl_holder = _holder_picker("zl_holder")
        api_key = st.text_input("Kite API key", key="kite_api_key")
        api_secret = st.text_input("Kite API secret", type="password", key="kite_api_secret")

        # Auto-capture request_token if Zerodha redirected back to this app's own URL
        qp = st.query_params
        auto_token = qp.get("request_token")

        if api_key:
            login_url = zerodha_connector.get_login_url(api_key)
            st.link_button("1. Log in to Zerodha", login_url)

        request_token = st.text_input(
            "2. Paste the request_token from the redirect URL after logging in "
            "(or it's auto-filled if your app's redirect URL points back here)",
            value=auto_token or "",
            key="kite_request_token",
        )

        if st.button("3. Fetch my holdings", disabled=not (api_key and api_secret and request_token)):
            try:
                session = zerodha_connector.generate_session(api_key, api_secret, request_token)
                st.session_state.zerodha_session = session
                holdings_df = zerodha_connector.fetch_holdings(session)
                holdings_df.insert(0, "Holder", zl_holder)
                st.session_state.zerodha_holdings = holdings_df
                st.success(f"Pulled {len(holdings_df)} holdings. Check the Holdings Detail tab.")
            except Exception as e:
                st.error(f"Couldn't connect: {e}")

        if st.session_state.zerodha_holdings is not None:
            st.caption(f"✅ {len(st.session_state.zerodha_holdings)} holdings loaded from Zerodha this session.")

    # ---- Zerodha Tradebook CSV/XLSX ----
    with src_zerodha_csv:
        st.markdown(
            "Console → Reports → **Tradebook** → pick a date range → Download. Console caps "
            "each export at about a year, so upload as many files as you need to cover your "
            "full holding period - they'll be combined."
        )
        zt_holder = _holder_picker("zt_holder")
        zt_files = st.file_uploader(
            "Upload Tradebook file(s)", type=["csv", "xlsx"], accept_multiple_files=True,
            key=f"zt_upload_{st.session_state.uploader_nonce['zt']}",
        )
        if zt_files and st.button("Add to XIRR data", key="zt_add"):
            try:
                parsed = parse_tradebook(zt_files)
                parsed.insert(0, "Holder", zt_holder)
                st.session_state.txn_sources["zerodha_tradebook"] = parsed
                st.rerun()
            except Exception as e:
                st.error(str(e))
        if "zerodha_tradebook" in st.session_state.txn_sources:
            st.caption(f"✅ {len(st.session_state.txn_sources['zerodha_tradebook'])} trades loaded.")
            st.dataframe(st.session_state.txn_sources["zerodha_tradebook"], hide_index=True, width="stretch")
            if st.button("🗑️ Clear Zerodha tradebook data", key="zt_clear"):
                del st.session_state.txn_sources["zerodha_tradebook"]
                st.session_state.uploader_nonce["zt"] += 1
                st.rerun()

    # ---- Kuvera statement ----
    with src_kuvera:
        st.markdown(
            "Kuvera app → **Reports** → transaction statement (.xlsx). Kuvera has no public "
            "API, so this is a file import. Kuvera doesn't include an ISIN in its export, so "
            "each scheme name is matched to the ISIN in your CAS by fund house + category - "
            "check the match table below before relying on the results. A scheme showing "
            "**no match** usually just means you've fully redeemed it, so it no longer "
            "appears in your current CAS - that's fine, its own buy/sell history is a "
            "complete cash-flow cycle on its own."
        )
        kv_holder = _holder_picker("kv_holder")
        kv_file = st.file_uploader(
            "Upload Kuvera statement", type=["xlsx", "csv"],
            key=f"kv_upload_{st.session_state.uploader_nonce['kv']}",
        )
        if kv_file is not None:
            try:
                kv_records = kuvera_import.load_statement(kv_file)
                # Match against *this* holder's own CAS holdings, not the
                # family-combined table - otherwise a scheme could falsely
                # match another family member's folio of the same fund.
                kv_txns, match_summary = kuvera_import.build_transactions(
                    kv_records, holder_to_data[kv_holder].mf_folio_holdings
                )
                kv_txns.insert(0, "Holder", kv_holder)
            except Exception as e:
                st.error(f"Couldn't read that file: {e}")
                kv_txns, match_summary = None, None

            if kv_txns is not None:
                st.caption(f"Parsed {len(kv_txns)} transactions across {len(match_summary)} schemes. Scheme matching:")
                st.dataframe(match_summary, hide_index=True, width="stretch")
                if st.button("Add to XIRR data", key="kv_add"):
                    st.session_state.txn_sources["kuvera_statement"] = kv_txns
                    st.rerun()
        if "kuvera_statement" in st.session_state.txn_sources:
            st.caption(f"✅ {len(st.session_state.txn_sources['kuvera_statement'])} transactions loaded.")
            if st.button("🗑️ Clear Kuvera data", key="kv_clear"):
                del st.session_state.txn_sources["kuvera_statement"]
                st.session_state.uploader_nonce["kv"] += 1
                st.rerun()

    # ---- GLC (PMS) statement ----
    with src_glc:
        st.markdown(
            "Green Lantern Capital's PMS **Transaction Statement** export (.xls). No ISIN "
            "either, so listed shares are matched against your CAS equity holdings and the "
            "PMS's cash-management fund (if any) against your CAS mutual fund holdings - "
            "both by name, same idea as Kuvera. Check the match table below; a security "
            "showing **no match** usually means it was bought and fully sold again before "
            "your CAS's date, so it never appears there at all."
        )
        glc_holder = _holder_picker("glc_holder")
        glc_file = st.file_uploader(
            "Upload GLC Transaction Statement", type=["xls", "xlsx"],
            key=f"glc_upload_{st.session_state.uploader_nonce['glc']}",
        )
        if glc_file is not None:
            try:
                glc_bytes = glc_file.getvalue() if hasattr(glc_file, "getvalue") else glc_file.read()
                holder_data = holder_to_data[glc_holder]
                glc_txns, glc_match_summary = glc_parser.parse_glc_statement(
                    glc_bytes, holder_data.equity_holdings, holder_data.mf_folio_holdings,
                    holder_data.mf_in_demat_holdings,
                )
                glc_txns.insert(0, "Holder", glc_holder)
            except Exception as e:
                st.error(f"Couldn't read that file: {e}")
                glc_txns, glc_match_summary = None, None

            if glc_txns is not None:
                st.caption(f"Parsed {len(glc_txns)} transactions across {len(glc_match_summary)} securities. Match summary:")
                st.dataframe(glc_match_summary, hide_index=True, width="stretch")
                if st.button("Add to XIRR data", key="glc_add"):
                    st.session_state.txn_sources["glc_pms"] = glc_txns
                    st.rerun()
        if "glc_pms" in st.session_state.txn_sources:
            st.caption(f"✅ {len(st.session_state.txn_sources['glc_pms'])} transactions loaded.")
            if st.button("🗑️ Clear GLC data", key="glc_clear"):
                del st.session_state.txn_sources["glc_pms"]
                st.session_state.uploader_nonce["glc"] += 1
                st.rerun()

    # ---- Manual CSV template ----
    with src_manual:
        st.markdown("For anything else - PMS accounts, funds outside Zerodha/Kuvera, manual entry.")
        template = pd.DataFrame(
            {
                "Date": ["2023-04-01", "2023-07-01", "2026-06-30"],
                "AssetClass": ["Mutual Fund Folios", "Mutual Fund Folios", "Mutual Fund Folios"],
                "Identifier": ["INF209K01YN0", "INF209K01YN0", "INF209K01YN0"],
                "Description": ["Aditya Birla Sun Life Banking & PSU Debt Fund", "same fund - SIP #2", "current value (auto-filled)"],
                "Amount": [-50000, -50000, 0],
                "Holder": [family_holders[0], family_holders[0], family_holders[0]],
            }
        )
        with st.expander("📋 CSV format reference (click to see an example - not your data)"):
            st.caption(
                "Amount convention: **negative** = invested (purchase/SIP), **positive** = received "
                "(redemption/dividend). Leave the final 'current value' row's Amount as 0 - the "
                "XIRR tab fills it in from your CAS."
                + (
                    f" **Holder** must match one of the names you assigned in the sidebar exactly "
                    f"({', '.join(family_holders)}) - different rows can name different people, so "
                    f"one file can cover the whole family if you'd rather not upload separately."
                    if is_family else ""
                )
            )
            st.dataframe(template, hide_index=True, width="stretch")
        st.download_button(
            "⬇️ Download template (CSV)",
            data=template.to_csv(index=False).encode("utf-8"),
            file_name="transactions_template.csv",
            mime="text/csv",
        )
        manual_file = st.file_uploader(
            "Upload completed CSV", type=["csv"],
            key=f"manual_upload_{st.session_state.uploader_nonce['manual']}",
        )
        if manual_file is not None:
            try:
                manual_df = pd.read_csv(manual_file)
                required = set(TXN_SCHEMA) - {"Holder"}
                missing = required - set(manual_df.columns)
                if missing:
                    st.error(f"Missing column(s): {', '.join(missing)}")
                else:
                    if "Holder" not in manual_df.columns:
                        fallback_holder = _holder_picker("manual_holder")
                        st.caption(f"No Holder column found - tagging every row as **{fallback_holder}**.")
                        manual_df["Holder"] = fallback_holder
                    else:
                        _unknown = set(manual_df["Holder"].dropna().unique()) - set(family_holders)
                        if _unknown:
                            st.warning(
                                f"⚠️ Holder value(s) {', '.join(repr(u) for u in _unknown)} don't match any "
                                f"name from the sidebar ({', '.join(family_holders)}) - those rows won't "
                                "find a current-value match for XIRR, but everything else still works."
                            )
                    manual_df["Date"] = pd.to_datetime(manual_df["Date"]).dt.date
                    if st.button("Add to XIRR data", key="manual_add"):
                        st.session_state.txn_sources["manual"] = manual_df[TXN_SCHEMA]
                        st.rerun()
            except Exception as e:
                st.error(str(e))
        if "manual" in st.session_state.txn_sources:
            st.caption(f"✅ {len(st.session_state.txn_sources['manual'])} rows loaded.")
            st.dataframe(st.session_state.txn_sources["manual"], hide_index=True, width="stretch")
            if st.button("🗑️ Clear manual CSV data", key="manual_clear"):
                del st.session_state.txn_sources["manual"]
                st.session_state.uploader_nonce["manual"] += 1
                st.rerun()

    if st.session_state.txn_sources:
        st.markdown("---")
        total_loaded = sum(len(df) for df in st.session_state.txn_sources.values())
        st.caption(f"**{total_loaded} transactions loaded across {len(st.session_state.txn_sources)} source(s).** See the XIRR tab for results.")


# --------------------------------------------------------------------------
# Tab 4: XIRR - computed from whatever's loaded in Connect & Import
# --------------------------------------------------------------------------

with tab_xirr:
    st.subheader("Real XIRR needs transaction-level cash flows")
    st.write(
        "A single CAS statement gives us your *current* holdings and, for mutual funds, the "
        "cumulative amount invested - enough to compute an **absolute return %**, which you can "
        "see in the Holdings Detail tab. It does **not** give us the date and amount of every "
        "individual purchase, SIP instalment, switch, or redemption - which is what XIRR "
        "(a money-weighted, annualised return) actually needs."
    )

    # ----------------------------------------------------------------
    # Unified drop zone - detect file type automatically and route to
    # the matching parser, instead of visiting each Connect & Import
    # sub-tab separately. Covers Zerodha Tradebook, Kuvera, and GLC (PMS)
    # exports, plus a manual CSV in the app's own TXN_SCHEMA format.
    # Doesn't cover CAS PDFs or the live Zerodha connect - those stay in
    # the sidebar / Connect & Import, since they need different UI
    # (per-file holder confirmation, or an OAuth flow) that doesn't fit
    # a drag-and-drop batch.
    # ----------------------------------------------------------------
    with st.expander("📥 Drop all your transaction files here (auto-detected)", expanded=not bool(st.session_state.txn_sources)):
        st.caption(
            "Drop any mix of Zerodha Tradebook, Kuvera statement, GLC (PMS) statement, or "
            "manual-CSV-template files at once - each is auto-detected and routed to the "
            "right parser. Doesn't cover CAS PDFs (upload those in the sidebar) or the live "
            "Zerodha connect (that needs its own login step, in Connect & Import)."
        )
        dropped_files = st.file_uploader(
            "Drop files", type=["csv", "xlsx", "xls"], accept_multiple_files=True,
            key=f"dropzone_{st.session_state.uploader_nonce.get('dropzone', 0)}",
            label_visibility="collapsed",
        )

        if dropped_files:
            detections = []
            for f in dropped_files:
                fbytes = f.getvalue()
                fname = f.name
                if glc_parser.looks_like_glc(fbytes):
                    dtype = "GLC (PMS)"
                elif zerodha_tradebook.looks_like_tradebook(fbytes, fname):
                    dtype = "Zerodha Tradebook"
                elif kuvera_import.looks_like_kuvera(fbytes, fname):
                    dtype = "Kuvera"
                else:
                    try:
                        _peek = pd.read_csv(BytesIO(fbytes), nrows=1) if fname.lower().endswith(".csv") else pd.read_excel(BytesIO(fbytes), nrows=1)
                        _peek_cols = set(c.strip() for c in _peek.columns)
                    except Exception:
                        _peek_cols = set()
                    dtype = "Manual CSV" if {"Date", "AssetClass", "Identifier", "Amount"}.issubset(_peek_cols) else "Unrecognized"
                detections.append({"file": f, "fname": fname, "fbytes": fbytes, "dtype": dtype})

            st.markdown("**Detected:**")
            holder_choices = {}
            for i, d in enumerate(detections):
                cols = st.columns([3, 2, 3]) if is_family else st.columns([3, 2])
                cols[0].write(d["fname"])
                if d["dtype"] == "Unrecognized":
                    cols[1].markdown("⚠️ *unrecognized*")
                else:
                    cols[1].write(d["dtype"])
                if is_family and d["dtype"] not in ("Unrecognized", "Manual CSV"):
                    holder_choices[i] = cols[2].selectbox(
                        "Holder", family_holders, key=f"dropzone_holder_{i}", label_visibility="collapsed",
                    )
                elif is_family:
                    holder_choices[i] = None  # Manual CSV carries its own Holder column per row

            n_ready = sum(1 for d in detections if d["dtype"] != "Unrecognized")
            if any(d["dtype"] == "Unrecognized" for d in detections):
                st.caption(
                    "⚠️ Unrecognized file(s) shown above won't be imported here - use the "
                    "matching tab in Connect & Import instead, or check it's a supported format."
                )

            if n_ready and st.button(f"Import {n_ready} file(s)", key="dropzone_import"):
                imported, errors = 0, []
                for i, d in enumerate(detections):
                    if d["dtype"] == "Unrecognized":
                        continue
                    holder = holder_choices.get(i) if is_family else family_holders[0]
                    key = f"dropzone_{d['fname']}"
                    try:
                        if d["dtype"] == "GLC (PMS)":
                            hd = holder_to_data[holder]
                            parsed, _ = glc_parser.parse_glc_statement(
                                d["fbytes"], hd.equity_holdings, hd.mf_folio_holdings, hd.mf_in_demat_holdings
                            )
                            parsed.insert(0, "Holder", holder)
                        elif d["dtype"] == "Zerodha Tradebook":
                            parsed = zerodha_tradebook.parse_tradebook([d["file"]])
                            parsed.insert(0, "Holder", holder)
                        elif d["dtype"] == "Kuvera":
                            hd = holder_to_data[holder]
                            records = kuvera_import.load_statement(d["file"])
                            parsed, _ = kuvera_import.build_transactions(records, hd.mf_folio_holdings)
                            parsed.insert(0, "Holder", holder)
                        else:  # Manual CSV
                            parsed = pd.read_csv(BytesIO(d["fbytes"]))
                            parsed = parsed.rename(columns=lambda c: str(c).strip())
                            parsed["Date"] = pd.to_datetime(parsed["Date"]).dt.date
                            if "Holder" not in parsed.columns:
                                parsed["Holder"] = family_holders[0]
                            parsed = parsed[TXN_SCHEMA]
                        st.session_state.txn_sources[key] = parsed
                        imported += 1
                    except Exception as e:
                        errors.append(f"{d['fname']}: {e}")
                if errors:
                    st.error("Some files failed:\n" + "\n".join(f"- {e}" for e in errors))
                if imported:
                    st.session_state.uploader_nonce["dropzone"] = st.session_state.uploader_nonce.get("dropzone", 0) + 1
                    st.rerun()

    if txns is None or txns.empty:
        st.info("Nothing loaded yet - head to the **Connect & Import** tab to bring in transaction history.")
    else:
        st.caption(f"Using {len(txns)} transactions from: {', '.join(st.session_state.txn_sources.keys())}.")

        # ---- Coverage: how much of the portfolio actually has transaction
        # history, vs just sitting in the CAS with none loaded. The XIRR
        # tables below can only total the covered portion - this is why
        # that total won't match the Asset Class Summary tab's total, and
        # says so explicitly rather than leaving it to be noticed.
        _covered_pairs = set(
            txns[["Holder", "Identifier"]].drop_duplicates().itertuples(index=False, name=None)
        )
        _holding_specs = [
            (data.mf_folio_holdings, "Valuation (₹)", "Scheme", "Mutual Fund Folios"),
            (data.equity_holdings, "Value (₹)", "Security", "Equity"),
            (data.mf_in_demat_holdings, "Value (₹)", "Security", "Mutual Funds Held in Demat Form"),
            (data.other_holdings, "Value (₹)", "Security", "Others"),
        ]
        _uncovered_rows = []
        _covered_value = 0.0
        for _tbl, _val_col, _name_col, _asset_class in _holding_specs:
            if _tbl.empty:
                continue
            _grouped = _tbl.groupby(["Holder", "ISIN"], as_index=False).agg({_val_col: "sum", _name_col: "first"})
            for _, r in _grouped.iterrows():
                pair = (r["Holder"], r["ISIN"])
                if pair in _covered_pairs:
                    _covered_value += r[_val_col]
                else:
                    _uncovered_rows.append(
                        {"Holder": r["Holder"], "Asset Class": _asset_class, "Description": r[_name_col], "Value (₹)": r[_val_col]}
                    )
        _uncovered_df = pd.DataFrame(_uncovered_rows).sort_values("Value (₹)", ascending=False).reset_index(drop=True) if _uncovered_rows else pd.DataFrame()
        _uncovered_total = _uncovered_df["Value (₹)"].sum() if not _uncovered_df.empty else 0.0
        _coverage_pct = (_covered_value / data.total_value * 100) if data.total_value else 0.0

        cov_col1, cov_col2 = st.columns([1, 3])
        with cov_col1:
            st.metric(
                "XIRR coverage", f"{_coverage_pct:.1f}%",
                help="Share of your total portfolio value (across all holders) that has transaction "
                     "history loaded and so gets a real XIRR below. This is why the Total in these "
                     "tables won't match the Asset Class Summary tab's total unless coverage is 100%.",
            )
        with cov_col2:
            if _uncovered_total > 0:
                st.caption(
                    f"**{fmt_inr(_uncovered_total)}** of your portfolio has no transaction history "
                    "loaded, so it isn't reflected in any table below. Usually either a holding type "
                    "no importer covers yet (e.g. Sovereign Gold Bonds), or shares/funds bought "
                    "outside the date range of whatever you've uploaded."
                )
                with st.expander(f"Show the {len(_uncovered_df)} uncovered holding(s)"):
                    _unc_display = _uncovered_df.copy()
                    _unc_display["Value (₹)"] = _unc_display["Value (₹)"].apply(fmt_inr)
                    _unc_display = _with_total_row(_unc_display, _uncovered_df, {"Value (₹)": fmt_inr})
                    st.dataframe(_unc_display, hide_index=True, width="stretch")
            else:
                st.caption("Every holding in your portfolio has transaction history loaded - full coverage.")

        st.markdown("---")
        
        # BUBBLE CHART MOVED HERE
        st.subheader("Size vs. return")
        st.caption(
            "Bubble size is current value, position on the vertical axis is XIRR - lets you "
            "see at a glance whether your biggest holdings are also your best performers, or "
            "not."
        )
        chart_options = ["Instrument Type", "Asset Class"] + (["Holder"] if is_family else [])
        chart_level = st.radio("Compare by", chart_options, horizontal=True, key="xirr_chart_level")
        chart_source = {
            "Instrument Type": xirr_by_instrument_type_df,
            "Asset Class": xirr_by_asset_class_df,
            "Holder": xirr_by_holder_df,
        }[chart_level]
        chart_df = chart_source.dropna(subset=["XIRR (%)"]).copy()
        if chart_df.empty:
            st.caption("No solvable XIRR values yet to plot.")
        else:
            chart_df["Current Value"] = chart_df["Current Value (₹)"].apply(fmt_inr)
            fig_bubble = px.scatter(
                chart_df, x=chart_level, y="XIRR (%)", size="Current Value (₹)",
                color=chart_level, size_max=70, color_discrete_sequence=color_sequence,
                hover_data={"Current Value (₹)": False, "Current Value": True, chart_level: False},
            )
            fig_bubble.update_layout(
                margin=dict(t=10, b=10, l=10, r=10), height=450, showlegend=False, xaxis_title="",
            )
            fig_bubble.add_hline(y=0, line_dash="dot", line_color="grey")
            st.plotly_chart(fig_bubble, use_container_width=True)
            
        st.markdown("---")

        def _fmt_xirr_table(df: pd.DataFrame) -> pd.DataFrame:
            out = df.copy()
            out["Current Value (₹)"] = out["Current Value (₹)"].apply(fmt_inr)
            out["XIRR (%)"] = out["XIRR (%)"].apply(lambda x: f"{x:.2f}%" if pd.notna(x) else "couldn't solve")
            out = _with_total_row(out, df, {"Current Value (₹)": fmt_inr})
            return out

        st.subheader("XIRR by holding")
        st.dataframe(_fmt_xirr_table(xirr_by_holding_df), hide_index=True, width="stretch")

        if is_family and not xirr_by_holder_df.empty:
            st.subheader("XIRR by family member")
            st.caption("Everyone's transactions pooled with their own current holdings - a per-person blended return.")
            st.dataframe(_fmt_xirr_table(xirr_by_holder_df), hide_index=True, width="stretch")

        st.subheader("XIRR by asset class")
        st.dataframe(_fmt_xirr_table(xirr_by_asset_class_df), hide_index=True, width="stretch")

        st.subheader("XIRR by instrument type")
        st.caption(
            "Same pooling, one level more granular - e.g. splits 'Mutual Fund Folios' into "
            "Equity Fund, Index Fund, Liquid Fund, Gilt/Treasury Fund, etc. Pools every cash "
            "flow within a group together with its total current value, which is the correct "
            "way to compute a blended XIRR for a group of holdings."
        )
        st.dataframe(_fmt_xirr_table(xirr_by_instrument_type_df), hide_index=True, width="stretch")


# --------------------------------------------------------------------------
# Tab 5: 12-month trend (context only - explicitly NOT presented as XIRR)
# --------------------------------------------------------------------------

with tab_trend:
    st.subheader("Portfolio value - last 12 months")
    st.caption(
        "From the CAS's own month-end valuation history. This mixes market movement with "
        "any money you added or withdrew, so it's shown for context only - it is not a "
        "return figure."
    )
    if not data.valuation_trend.empty:
        if fig_trend:
            st.plotly_chart(fig_trend, use_container_width=True)
        trend_display = data.valuation_trend.copy()
        trend_display["Portfolio Value (₹)"] = trend_display["Portfolio Value (₹)"].apply(fmt_inr)
        trend_display["Change (₹)"] = trend_display["Change (₹)"].apply(fmt_inr)
        trend_display["Change (%)"] = trend_display["Change (%)"].apply(lambda x: f"{x:.2f}%" if pd.notna(x) else "-")
        st.dataframe(trend_display, hide_index=True, width="stretch")
    else:
        st.caption("No trend data found in this CAS.")
