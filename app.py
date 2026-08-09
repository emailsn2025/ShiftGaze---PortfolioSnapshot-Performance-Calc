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
import base64

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

# Custom CSS to double the font size of the Tab Headers
st.markdown("""
<style>
.stTabs [data-baseweb="tab-list"] button [data-testid="stMarkdownContainer"] p {
    font-size: 1.6rem !important; 
    font-weight: 600 !important;
}
</style>
""", unsafe_allow_html=True)

# --------------------------------------------------------------------------
# State Initialization
# --------------------------------------------------------------------------
TXN_SCHEMA = ["Date", "AssetClass", "Identifier", "Description", "Amount", "Holder"]
if "txn_sources" not in st.session_state:
    st.session_state.txn_sources = {}
if "zerodha_session" not in st.session_state:
    st.session_state.zerodha_session = None
if "zerodha_holdings" not in st.session_state:
    st.session_state.zerodha_holdings = None
if "holder_to_data" not in st.session_state:
    st.session_state.holder_to_data = {}
if "prev_holder_to_data" not in st.session_state:
    st.session_state.prev_holder_to_data = {}
if "uploader_nonce" not in st.session_state:
    st.session_state.uploader_nonce = {"zt": 0, "kv": 0, "glc": 0, "manual": 0, "dropzone": 0, "resume": 0, "cas": 0, "prev_cas": 0}

# --------------------------------------------------------------------------
# UI Helpers & Formatting
# --------------------------------------------------------------------------
def fmt_inr(x: float) -> str:
    """Format a number in Indian comma style, rounded to the nearest rupee"""
    if x is None or pd.isna(x): return "-"
    try: x = float(x)
    except (ValueError, TypeError): return "-"
    neg = x < 0
    x = round(abs(x))
    int_part = str(x)
    if len(int_part) > 3:
        last3, rest = int_part[-3:], int_part[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest: groups.insert(0, rest)
        int_part = ",".join(groups + [last3])
    out = f"₹{int_part}"
    return f"-{out}" if neg else out

def fmt_inr_short(x: float) -> str:
    """Abbreviated Indian units for chart axes/labels"""
    if x is None or pd.isna(x): return "-"
    try: x = float(x)
    except (ValueError, TypeError): return "-"
    neg = x < 0
    x = abs(x)
    if x >= 1_00_00_000: out = f"₹{x/1_00_00_000:.2f}Cr"
    elif x >= 1_00_000: out = f"₹{x/1_00_000:.2f}L"
    elif x >= 1_000: out = f"₹{x/1_000:.1f}K"
    else: out = f"₹{round(x)}"
    return f"-{out}" if neg else out

def get_axis_ticks(min_v, max_v, n=6, force_zero=True):
    """Generate evenly spaced ticks for Plotly charts to prevent overlaps"""
    if force_zero and min_v > 0: min_v = 0
    if max_v == min_v: return [min_v]
    step = (max_v - min_v) / (n - 1)
    return [min_v + i * step for i in range(n)]

def _with_total_row(display_df: pd.DataFrame, raw_df: pd.DataFrame, sum_specs: dict, label: str = "Total") -> pd.DataFrame:
    """Appends a Total row to an already-formatted display DataFrame."""
    if raw_df is None or raw_df.empty: return display_df
    total_row = {col: "" for col in display_df.columns}
    for col, formatter in sum_specs.items():
        if col in raw_df.columns:
            total_row[col] = formatter(raw_df[col].sum())
    total_row[display_df.columns[0]] = label
    return pd.concat([display_df, pd.DataFrame([total_row])], ignore_index=True)

# Consistent Colors for Charts
ASSET_COLORS = {
    "Equity": "#ef4444", 
    "Mutual Fund Folios": "#3b82f6", 
    "Mutual Funds Held in Demat Form": "#10b981", 
    "Others": "#f59e0b",
    "Others (Govt Securities/SGB)": "#f59e0b"
}

@st.cache_data(show_spinner=False)
def load_cas(file_bytes: bytes) -> CASData:
    with pdfplumber.open(BytesIO(file_bytes)) as pdf:
        if mfcentral_parser.looks_like_mfcentral(pdf):
            return mfcentral_parser.parse_mfcentral_summary(BytesIO(file_bytes))
    return parse_cas(BytesIO(file_bytes))

def _cas_to_dict(cas: CASData) -> dict:
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
    cas = CASData()
    cas.holder_name = d.get("holder_name", "")
    cas.total_value = d.get("total_value", 0.0)
    def _restore_df(data_list, empty_cols):
        return pd.DataFrame(data_list) if data_list else pd.DataFrame(columns=empty_cols)
    cas.asset_summary = _restore_df(d.get("asset_summary", []), ["Asset Class", "Value (₹)", "% of Portfolio"])
    cas.equity_holdings = _restore_df(d.get("equity_holdings", []), ["ISIN", "Security", "Instrument Type", "Value (₹)"])
    cas.mf_in_demat_holdings = _restore_df(d.get("mf_in_demat_holdings", []), ["ISIN", "Security", "Instrument Type", "Value (₹)"])
    cas.other_holdings = _restore_df(d.get("other_holdings", []), ["ISIN", "Security", "Instrument Type", "Value (₹)", "Category"])
    cas.mf_folio_holdings = _restore_df(d.get("mf_folio_holdings", []), ["Scheme", "ISIN", "Instrument Type", "Folio No.", "Invested (₹)", "Valuation (₹)", "Unrealised P/L (₹)", "Unrealised P/L (%)"])
    cas.valuation_trend = _restore_df(d.get("valuation_trend", []), ["Month", "Portfolio Value (₹)", "Change (₹)", "Change (%)"])
    return cas

# --------------------------------------------------------------------------
# UI Header & Banner
# --------------------------------------------------------------------------
def get_logo_b64():
    logo_path = os.path.join(os.path.dirname(__file__), "shiftgaze_logo.jpg")
    if os.path.exists(logo_path):
        with open(logo_path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    return None

logo_b64 = get_logo_b64()
logo_html = (
    f'<img src="data:image/jpeg;base64,{logo_b64}" '
    f'style="height:70px; object-fit:contain; border-radius:8px;"/>'
    if logo_b64 else ""
)

st.markdown(f"""
<div style="background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 100%); padding: 16px 28px; border-radius: 12px; margin-bottom: 16px; box-shadow: 0 4px 12px rgba(0,0,0,0.3);">
<div style="display: flex; align-items: center; justify-content: space-between;">
<div>
<div style="color:#ffffff; font-size:26px; font-weight:700; letter-spacing:-0.5px; white-space:nowrap;">📊 Portfolio Summary & XIRR Tracker</div>
<div style="color:#94a3b8; font-size:13px; margin-top:3px; white-space:nowrap;">Upload CAS · Track XIRR · Analyze Portfolio</div>
</div>
<div style="text-align:center; color:#94a3b8; font-size:18px; font-style:italic;">Developed by Sandeep Narang</div>
<div>{logo_html}</div>
</div>
<div style="margin-top:14px; padding-top:12px; border-top:1px solid rgba(148,163,184,0.2); font-size:11px; line-height:1.6;">
<span style="color:#f59e0b;">⚠️ Disclaimer:</span> <span style="color:#94a3b8;">This application is for personal tracking and informational purposes only. It parses your data locally. Projections and XIRR are estimates.</span><br/>
<span style="color:#34d399;">🔒 Privacy:</span> <span style="color:#94a3b8;">Your financial data — CAS PDFs and transaction histories — never leaves your browser session. It is never stored, transmitted, or retained anywhere; it's lost when you close the tab unless you securely download your session using the JSON Save tool.</span>
</div>
</div>
""", unsafe_allow_html=True)


# --------------------------------------------------------------------------
# Tabs Setup
# --------------------------------------------------------------------------
tab_settings, tab_summary, tab_holdings, tab_xirr, tab_trend, tab_connect = st.tabs([
    "1. ⚙️ Settings",
    "2. 📋 Asset Class Summary",
    "3. 🔍 Holdings Detail",
    "4. 📈 XIRR",
    "5. 🕒 12-Month Trend",
    "6. 🔗 Connections"
])

# --------------------------------------------------------------------------
# 1. ⚙️ Settings (Uploads & Downloads Management)
# --------------------------------------------------------------------------
with tab_settings:
    st.markdown("""
<div style="background: linear-gradient(135deg, #1e3a5f 0%, #0f172a 100%); border-left: 4px solid #2563eb; border-radius: 8px; padding: 14px 20px; margin-bottom: 20px;">
<details>
<summary style="color:#93c5fd; font-size:14px; font-weight:600; cursor:pointer; list-style:none;">
ℹ️ How to use this planner &nbsp;·&nbsp;
<span style="color:#64748b; font-weight:400; font-size:12px;">Click to expand</span>
</summary>
<div style="margin-top:12px; color:#cbd5e1; font-size:13px; line-height:1.7;">
<b style="color:#93c5fd;">Step 1 — Upload your CAS PDF</b><br/>
Upload your CDSL CAS (Consolidated Account Statement) or MF Central summary directly below. You can upload multiple PDFs (e.g., one for each family member) to generate a combined household view. This populates your current portfolio holdings, asset breakdowns, and 12-month trend.
<br/><br/><b style="color:#93c5fd;">Step 2 — Review your Holdings</b><br/>
Navigate to the <b>Asset Class Summary</b> and <b>Holdings Detail</b> tabs to see cleanly organized, tabular breakdowns of everything you currently own.
<br/><br/><b style="color:#93c5fd;">Step 3 — Import Transactions for Real XIRR</b><br/>
Your CAS alone only shows current values. To calculate an accurate, annualized <b>XIRR</b>, the app needs your purchase and redemption history. Use the Transaction Dropzone below (or the Connections tab) to securely import your tradebook from Zerodha, Kuvera, GLC, or a manual CSV.
<br/><br/><b style="color:#93c5fd;">Step 4 — Save your Session & Export Reports</b><br/>
Don't want to re-upload everything tomorrow? Use the <b>Save Session Data</b> feature to download a secure, offline JSON file. Next time, just drop that JSON right back into the uploader to instantly restore your entire dashboard! You can also download clean PDF and Excel summaries of your portfolio.
</div>
</details>
</div>
""", unsafe_allow_html=True)

    # ------------------ UPLOADS SECTION ------------------
    st.header("📤 Uploads")
    col_up1, col_up2 = st.columns(2)
    
    with col_up1:
        st.subheader("📄 Upload CAS PDFs")
        st.caption("Upload one per family member for a combined view.")
        uploaded_files = st.file_uploader(
            "Upload Current CAS PDF(s)", type=["pdf"], accept_multiple_files=True,
            key=f"cas_upload_{st.session_state.uploader_nonce['cas']}"
        )
        
        # Confirm names immediately if files uploaded
        if uploaded_files:
            result = {}
            seen = set(st.session_state.holder_to_data.keys()) 
            st.markdown("**Confirm who's who:**")
            for i, f in enumerate(uploaded_files):
                with st.spinner(f"Parsing {f.name}..."):
                    parsed = load_cas(f.getvalue())
                if parsed.total_value == 0:
                    st.error(f"Couldn't parse '{f.name}' as a CDSL CAS - skipped.")
                    continue
                default_name = parsed.holder_name or os.path.splitext(f.name)[0]
                label = st.text_input(
                    f"{f.name}", value=default_name, key=f"holder_label_{i}",
                    help="Editable - fix this if the name was misread.",
                )
                label = label.strip() or default_name
                if label in seen:
                    label = f"{label} ({i+1})"
                seen.add(label)
                result[label] = parsed
            
            if st.button("Apply CAS Data"):
                st.session_state.holder_to_data.update(result) 
                st.session_state.uploader_nonce["cas"] += 1
                st.rerun()

        with st.expander("📅 Compare to last month (optional)"):
            st.caption("Upload last month's CAS to get variance columns in tables.")
            prev_uploaded_files = st.file_uploader(
                "Upload previous month's CAS PDF(s)", type=["pdf"], accept_multiple_files=True,
                key=f"prev_cas_upload_{st.session_state.uploader_nonce['prev_cas']}"
            )
            if prev_uploaded_files and st.button("Apply Previous CAS"):
                prev_h_to_d = {}
                for i, f in enumerate(prev_uploaded_files):
                    with st.spinner(f"Parsing previous CAS {f.name}..."):
                        parsed = load_cas(f.getvalue())
                    if parsed.total_value > 0:
                        prev_h_to_d[parsed.holder_name or f"{f.name}_{i}"] = parsed
                st.session_state.prev_holder_to_data.update(prev_h_to_d) 
                st.session_state.uploader_nonce["prev_cas"] += 1
                st.rerun()

    with col_up2:
        st.subheader("📥 Transaction Dropzone")
        st.caption("Drop any mix of Zerodha Tradebook, Kuvera, GLC, or Manual CSV files.")
        
        if not st.session_state.holder_to_data:
            st.info("Please upload a CAS PDF first to map your transactions.")
        else:
            dropzone_family_holders = list(st.session_state.holder_to_data.keys())
            dropzone_is_family = len(dropzone_family_holders) > 1
            
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
                    if glc_parser.looks_like_glc(fbytes): dtype = "GLC (PMS)"
                    elif zerodha_tradebook.looks_like_tradebook(fbytes, fname): dtype = "Zerodha Tradebook"
                    elif kuvera_import.looks_like_kuvera(fbytes, fname): dtype = "Kuvera"
                    else:
                        try:
                            _peek = pd.read_csv(BytesIO(fbytes), nrows=1) if fname.lower().endswith(".csv") else pd.read_excel(BytesIO(fbytes), nrows=1)
                            _peek_cols = set(c.strip() for c in _peek.columns)
                        except: _peek_cols = set()
                        dtype = "Manual CSV" if {"Date", "AssetClass", "Identifier", "Amount"}.issubset(_peek_cols) else "Unrecognized"
                    detections.append({"file": f, "fname": fname, "fbytes": fbytes, "dtype": dtype})

                st.markdown("**Detected:**")
                holder_choices = {}
                for i, d in enumerate(detections):
                    cols = st.columns([3, 2, 3]) if dropzone_is_family else st.columns([3, 2])
                    cols[0].write(d["fname"])
                    if d["dtype"] == "Unrecognized": cols[1].markdown("⚠️ *unrecognized*")
                    else: cols[1].write(d["dtype"])
                    if dropzone_is_family and d["dtype"] not in ("Unrecognized", "Manual CSV"): 
                        holder_choices[i] = cols[2].selectbox("Holder", dropzone_family_holders, key=f"dropzone_holder_{i}", label_visibility="collapsed")
                    elif dropzone_is_family: holder_choices[i] = None

                n_ready = sum(1 for d in detections if d["dtype"] != "Unrecognized")
                if any(d["dtype"] == "Unrecognized" for d in detections):
                    st.caption("⚠️ Unrecognized file(s) shown above won't be imported here - check Connections tab.")

                if n_ready and st.button(f"Import {n_ready} file(s)", key="dropzone_import"):
                    imported, errors = 0, []
                    for i, d in enumerate(detections):
                        if d["dtype"] == "Unrecognized": continue
                        holder = holder_choices.get(i) if dropzone_is_family else dropzone_family_holders[0]
                        key = f"dropzone_{d['fname']}"
                        try:
                            if d["dtype"] == "GLC (PMS)":
                                hd = st.session_state.holder_to_data[holder]
                                parsed, _ = glc_parser.parse_glc_statement(d["fbytes"], hd.equity_holdings, hd.mf_folio_holdings, hd.mf_in_demat_holdings)
                                parsed.insert(0, "Holder", holder)
                            elif d["dtype"] == "Zerodha Tradebook":
                                parsed = zerodha_tradebook.parse_tradebook([d["file"]])
                                parsed.insert(0, "Holder", holder)
                            elif d["dtype"] == "Kuvera":
                                hd = st.session_state.holder_to_data[holder]
                                records = kuvera_import.load_statement(d["file"])
                                parsed, _ = kuvera_import.build_transactions(records, hd.mf_folio_holdings)
                                parsed.insert(0, "Holder", holder)
                            else:
                                parsed = pd.read_csv(BytesIO(d["fbytes"])).rename(columns=lambda c: str(c).strip())
                                parsed["Date"] = pd.to_datetime(parsed["Date"]).dt.date
                                if "Holder" not in parsed.columns: parsed["Holder"] = dropzone_family_holders[0]
                                parsed = parsed[TXN_SCHEMA]
                            st.session_state.txn_sources[key] = parsed
                            imported += 1
                        except Exception as e: errors.append(f"{d['fname']}: {e}")
                    if errors: st.error("Some files failed:\n" + "\n".join(f"- {e}" for e in errors))
                    if imported:
                        st.session_state.uploader_nonce["dropzone"] += 1
                        st.rerun()

    st.markdown("---")
    
    # ------------------ DOWNLOADS SECTION ------------------
    st.header("📥 Downloads & Session Management")
    col_dl1, col_dl2 = st.columns(2)

    with col_dl1:
        st.subheader("💾 Save / Resume Session JSON")
        st.caption("Download your loaded CAS data and transactions, or upload a JSON to instantly restore your dashboard.")
        
        if st.session_state.holder_to_data or st.session_state.txn_sources:
            import json as _json
            def _session_to_json() -> bytes:
                fh = st.session_state.get('last_family_holders', [])
                payload = {
                    "version": 2,
                    "exported_at": datetime.now().isoformat(),
                    "family_holders": fh,
                    "txn_sources": {
                        key: df.assign(Date=df["Date"].astype(str)).to_dict(orient="records")
                        for key, df in st.session_state.txn_sources.items()
                    },
                    "holder_to_data": {k: _cas_to_dict(v) for k, v in st.session_state.holder_to_data.items()},
                    "prev_holder_to_data": {k: _cas_to_dict(v) for k, v in st.session_state.prev_holder_to_data.items()}
                }
                return _json.dumps(payload, indent=2, default=str).encode("utf-8")

            st.download_button(
                "⬇️ Download session (JSON)",
                data=_session_to_json(),
                file_name=f"portfolio_session_{date.today().isoformat()}.json",
                mime="application/json",
                use_container_width=True
            )
        else:
            st.info("No data loaded yet to save.")
            
        if st.session_state.get("show_json_success"):
            st.success("✅ Session data restored successfully!")
            st.session_state["show_json_success"] = False
        
        resume_file = st.file_uploader(
            "Upload a saved session JSON", type=["json"],
            key=f"resume_upload_{st.session_state.uploader_nonce.get('resume', 0)}",
            label_visibility="collapsed"
        )
        
        if resume_file is not None:
            import json as _json
            try:
                payload = _json.loads(resume_file.getvalue().decode("utf-8"))
                if not isinstance(payload, dict) or ("txn_sources" not in payload and "holder_to_data" not in payload):
                    st.error("Invalid format: Please upload a JSON session file generated by this app.")
                else:
                    restored_items = 0
                    for key, records in payload.get("txn_sources", {}).items():
                        if records:
                            df = pd.DataFrame(records)
                            df["Date"] = pd.to_datetime(df["Date"]).dt.date
                            st.session_state.txn_sources[key] = df
                            restored_items += 1
                            
                    if "holder_to_data" in payload and payload["holder_to_data"]:
                        st.session_state.holder_to_data = {k: _dict_to_cas(v) for k, v in payload["holder_to_data"].items()}
                        restored_items += 1
                        
                    if "prev_holder_to_data" in payload and payload["prev_holder_to_data"]:
                        st.session_state.prev_holder_to_data = {k: _dict_to_cas(v) for k, v in payload["prev_holder_to_data"].items()}
                        
                    if restored_items > 0:
                        st.session_state["show_json_success"] = True
                        new_nonces = st.session_state.uploader_nonce.copy()
                        new_nonces["resume"] = new_nonces.get("resume", 0) + 1
                        st.session_state.uploader_nonce = new_nonces
                        st.rerun()
                    else:
                        st.warning("That file didn't have any parseable portfolio or transaction sources in it.")
            except Exception as e:
                st.error(f"Couldn't read that session file: {e}")
                
    with col_dl2:
        st.subheader("📄 Export Reports")
        st.caption("Download clean PDF and Excel summaries of your currently viewed portfolio and XIRR data.")
        # We create a placeholder here. The buttons will be injected below once the data is processed.
        report_downloads_container = st.container()

    st.markdown("<br>", unsafe_allow_html=True)
    if st.session_state.holder_to_data or st.session_state.txn_sources:
        if st.button("🗑️ Clear all loaded data", use_container_width=True):
            st.session_state.holder_to_data = {}
            st.session_state.prev_holder_to_data = {}
            st.session_state.txn_sources = {}
            st.session_state.zerodha_holdings = None
            for k in st.session_state.uploader_nonce:
                st.session_state.uploader_nonce[k] += 1
            st.rerun()


# --------------------------------------------------------------------------
# Check if data exists before populating downstream tabs
# --------------------------------------------------------------------------
holder_to_data = st.session_state.holder_to_data
if not holder_to_data:
    with tab_summary: st.info("👈 Please upload your CAS PDF or Session JSON in the Settings tab to view this section.")
    with tab_holdings: st.info("👈 Please upload your CAS PDF or Session JSON in the Settings tab to view this section.")
    with tab_xirr: st.info("👈 Please upload your CAS PDF or Session JSON in the Settings tab to view this section.")
    with tab_trend: st.info("👈 Please upload your CAS PDF or Session JSON in the Settings tab to view this section.")
    with tab_connect: st.info("👈 Please upload your CAS PDF or Session JSON in the Settings tab to view this section.")
    st.stop()


# --------------------------------------------------------------------------
# Data Aggregation
# --------------------------------------------------------------------------
data = combine_family(holder_to_data)
family_holders = list(holder_to_data.keys())
st.session_state['last_family_holders'] = family_holders
is_family = len(family_holders) > 1

prev_data = None
prev_holder_to_data = st.session_state.prev_holder_to_data
if prev_holder_to_data:
    prev_data = combine_family(prev_holder_to_data)

def _with_variance(current: pd.DataFrame, previous: pd.DataFrame | None, key_cols: list[str], value_col: str) -> pd.DataFrame:
    """Left-joins current onto previous by key_cols and adds Δ₹/Δ% columns."""
    if previous is None or previous.empty: return current
    prev_slim = previous.groupby(key_cols, as_index=False)[value_col].sum().rename(columns={value_col: "_prev_value"})
    merged = current.merge(prev_slim, on=key_cols, how="left")
    merged["Change vs Last Month (₹)"] = merged[value_col] - merged["_prev_value"]
    merged["Change vs Last Month (%)"] = ((merged[value_col] / merged["_prev_value"] - 1) * 100).where(merged["_prev_value"].notna() & (merged["_prev_value"] != 0))
    return merged.drop(columns="_prev_value")

instrument_breakdown = instrument_type_breakdown(data)
instrument_breakdown_prev = instrument_type_breakdown(prev_data) if prev_data is not None else None
instrument_breakdown = _with_variance(instrument_breakdown, instrument_breakdown_prev, ["Asset Class", "Instrument Type"], "Value (₹)")

asset_summary_with_variance = _with_variance(data.asset_summary.copy(), prev_data.asset_summary if prev_data is not None else None, ["Asset Class"], "Value (₹)")

# Header Metrics (Drawn above tabs)
col1, col2, col3 = st.columns(3)
col1.metric("Total Portfolio Value", fmt_inr(data.total_value))
if not data.valuation_trend.empty and len(data.valuation_trend) >= 2:
    prev = data.valuation_trend.iloc[-2]["Portfolio Value (₹)"]
    curr = data.valuation_trend.iloc[-1]["Portfolio Value (₹)"]
    col2.metric("Change vs Last Month", fmt_inr(curr - prev), f"{(curr/prev - 1)*100:.2f}%")
mf_total_invested = data.mf_folio_holdings["Invested (₹)"].sum() if not data.mf_folio_holdings.empty else 0
mf_total_val = data.mf_folio_holdings["Valuation (₹)"].sum() if not data.mf_folio_holdings.empty else 0
if mf_total_invested:
    col3.metric("Mutual Funds Return (absolute)", f"{(mf_total_val/mf_total_invested - 1)*100:.2f}%")

st.markdown("---")
view_data, view_instrument_breakdown, view_asset_summary = data, instrument_breakdown, asset_summary_with_variance
holder_view = "👪 All Family"

# Holder Filter
if is_family:
    holder_view = st.radio("Viewing", ["👪 All Family"] + family_holders, horizontal=True, key="holder_view_filter")
    if holder_view != "👪 All Family":
        view_data = holder_to_data[holder_view]
        view_instrument_breakdown = instrument_type_breakdown(view_data)
        prev_match = prev_holder_to_data.get(holder_view)
        view_instrument_breakdown = _with_variance(view_instrument_breakdown, instrument_type_breakdown(prev_match) if prev_match is not None else None, ["Asset Class", "Instrument Type"], "Value (₹)")
        view_asset_summary = _with_variance(view_data.asset_summary.copy(), prev_match.asset_summary if prev_match is not None else None, ["Asset Class"], "Value (₹)")


# --------------------------------------------------------------------------
# XIRR Processing
# --------------------------------------------------------------------------
txns = pd.concat(st.session_state.txn_sources.values(), ignore_index=True) if st.session_state.txn_sources else None
xirr_by_holding_df = pd.DataFrame()
xirr_by_asset_class_df = pd.DataFrame()
xirr_by_instrument_type_df = pd.DataFrame()
xirr_by_holder_df = pd.DataFrame()

if txns is not None and not txns.empty:
    if "Holder" not in txns.columns: txns["Holder"] = family_holders[0]
    txns["Holder"] = txns["Holder"].fillna(family_holders[0])
    txns["Amount"] = pd.to_numeric(txns["Amount"].astype(str).str.replace(r"[₹,\s]", "", regex=True), errors="coerce")
    txns = txns.dropna(subset=["Amount"])
    _unmatched_mask = txns["Identifier"].astype(str).str.startswith("UNMATCHED::")
    txns = txns[~_unmatched_mask]

if txns is not None and not txns.empty:
    as_of = date.today()
    itype_lookup = isin_to_instrument_type(data)
    _value_lookup = defaultdict(float)
    for hdf, col in [(data.mf_folio_holdings, "Valuation (₹)"), (data.equity_holdings, "Value (₹)"), (data.mf_in_demat_holdings, "Value (₹)"), (data.other_holdings, "Value (₹)")]:
        if not hdf.empty:
            for (holder, isin), val in hdf.groupby(["Holder", "ISIN"])[col].sum().items():
                _value_lookup[(holder, isin)] += val
    if st.session_state.zerodha_holdings is not None and not st.session_state.zerodha_holdings.empty:
        zh = st.session_state.zerodha_holdings
        zh_holder_col = zh["Holder"] if "Holder" in zh.columns else pd.Series([family_holders[0]] * len(zh))
        for (holder, isin), val in zh.assign(_h=zh_holder_col).groupby(["_h", "ISIN"])["Current Value (₹)"].sum().items():
            _value_lookup[(holder, isin)] = val
    value_lookup = dict(_value_lookup)

    def _lookup_value(holder, ident): return value_lookup.get((holder, str(ident).strip()))

    holding_rows = []
    for (holder, ident), grp in txns.groupby(["Holder", "Identifier"]):
        flows = [CashFlow(r["Date"], r["Amount"]) for _, r in grp.iterrows() if r["Amount"] != 0]
        current_val = _lookup_value(holder, ident)
        if current_val is not None: flows.append(CashFlow(as_of, current_val))
        result = xirr(flows) if len(flows) >= 2 else None
        desc = grp["Description"].iloc[0] if "Description" in grp.columns else ""
        asset_class = grp["AssetClass"].iloc[0] if "AssetClass" in grp.columns else ""
        holding_rows.append({"Holder": holder, "Identifier": ident, "Description": desc, "Asset Class": asset_class, "Instrument Type": itype_lookup.get(str(ident).strip(), "Unknown"), "Current Value (₹)": current_val if current_val is not None else 0.0, "XIRR (%)": result * 100 if result is not None else None})
    xirr_by_holding_df = pd.DataFrame(holding_rows)
    if not is_family and not xirr_by_holding_df.empty: xirr_by_holding_df = xirr_by_holding_df.drop(columns="Holder")

    def _grouped_xirr(group_col: str) -> pd.DataFrame:
        rows = []
        merged = txns.merge(pd.DataFrame({"Identifier": list(itype_lookup.keys()), "Instrument Type": list(itype_lookup.values())}), on="Identifier", how="left") if group_col == "Instrument Type" else txns
        if group_col == "Instrument Type": merged["Instrument Type"] = merged["Instrument Type"].fillna("Unknown")
        for key, grp in merged.groupby(group_col):
            flows = [CashFlow(r["Date"], r["Amount"]) for _, r in grp.iterrows() if r["Amount"] != 0]
            pairs = grp[["Holder", "Identifier"]].drop_duplicates()
            total_current = sum(_lookup_value(h, i) or 0 for h, i in pairs.itertuples(index=False))
            if total_current: flows.append(CashFlow(as_of, total_current))
            result = xirr(flows) if len(flows) >= 2 else None
            rows.append({group_col: key, "Current Value (₹)": total_current, "XIRR (%)": result * 100 if result is not None else None})
        return pd.DataFrame(rows).sort_values("Current Value (₹)", ascending=False).reset_index(drop=True)

    xirr_by_asset_class_df = _grouped_xirr("AssetClass").rename(columns={"AssetClass": "Asset Class"})
    xirr_by_instrument_type_df = _grouped_xirr("Instrument Type")
    if is_family: xirr_by_holder_df = _grouped_xirr("Holder")


# --------------------------------------------------------------------------
# Pre-build Charts
# --------------------------------------------------------------------------
# 1. Pie Chart
pie_df = view_data.asset_summary.copy()
fig_pie = None
if not pie_df.empty:
    pie_df["Value (formatted)"] = pie_df["Value (₹)"].apply(fmt_inr)
    fig_pie = px.pie(pie_df, values="Value (₹)", names="Asset Class", color="Asset Class", hole=0.45, custom_data=["Value (formatted)"], color_discrete_map=ASSET_COLORS)
    fig_pie.update_traces(textinfo="percent+label", hovertemplate="%{label}<br>%{customdata[0]}<br>%{percent}<extra></extra>")
    fig_pie.update_layout(showlegend=False, margin=dict(t=10, b=10, l=10, r=10), height=350)

# 2. Bar Chart (Descending Visually)
fig_ib = None
if not view_instrument_breakdown.empty:
    ib_sorted = view_instrument_breakdown.sort_values("Value (₹)", ascending=True) 
    fig_ib = px.bar(ib_sorted, x="Value (₹)", y="Instrument Type", color="Asset Class", orientation="h", text=ib_sorted["Value (₹)"].apply(fmt_inr_short), color_discrete_map=ASSET_COLORS)
    fig_ib.update_traces(textposition="outside", cliponaxis=False)
    fig_ib.update_yaxes(categoryorder="total ascending")
    
    max_val = ib_sorted["Value (₹)"].max() if not ib_sorted.empty else 0
    ticks = get_axis_ticks(0, max_val, n=6)
    fig_ib.update_xaxes(tickvals=ticks, ticktext=[fmt_inr_short(v) for v in ticks], title="Value (₹)")
    fig_ib.update_layout(margin=dict(t=40, b=10, l=10, r=10), height=100 + 32 * len(ib_sorted), yaxis_title="", legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0))

# 3. Line Chart
fig_trend = None
if not data.valuation_trend.empty:
    fig_trend = px.line(data.valuation_trend, x="Month", y="Portfolio Value (₹)", markers=True, color_discrete_sequence=[ASSET_COLORS["Equity"]])
    min_v = data.valuation_trend["Portfolio Value (₹)"].min()
    max_v = data.valuation_trend["Portfolio Value (₹)"].max()
    ticks = get_axis_ticks(min_v * 0.95, max_v * 1.05, n=6, force_zero=False)
    fig_trend.update_yaxes(tickvals=ticks, ticktext=[fmt_inr_short(v) for v in ticks], title="Portfolio Value (₹)")
    fig_trend.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=350)

# 4. Bubble Chart
fig_bubble_export = None
if not xirr_by_asset_class_df.empty and "XIRR (%)" in xirr_by_asset_class_df.columns:
    chart_df_exp = xirr_by_asset_class_df.dropna(subset=["XIRR (%)"]).copy()
    if not chart_df_exp.empty:
        chart_df_exp["Current Value"] = chart_df_exp["Current Value (₹)"].apply(fmt_inr)
        fig_bubble_export = px.scatter(
            chart_df_exp, x="Asset Class", y="XIRR (%)", size="Current Value (₹)",
            color="Asset Class", size_max=70, color_discrete_map=ASSET_COLORS,
            title="Size vs. Return (by Asset Class)",
            hover_data={"Current Value (₹)": False, "Current Value": True, "Asset Class": False}
        )
        fig_bubble_export.add_hline(y=0, line_dash="dot", line_color="grey")

export_charts = [fig for fig in [fig_pie, fig_ib, fig_trend, fig_bubble_export] if fig is not None]

# --------------------------------------------------------------------------
# PDF/Excel Exports (Injected back into Settings Tab placeholder)
# --------------------------------------------------------------------------
def _append_raw_total(df: pd.DataFrame, cols_to_sum: list, label: str = "Total") -> pd.DataFrame:
    if df is None or df.empty: return df
    totals = {col: "" for col in df.columns}
    totals[df.columns[0]] = label
    for col in cols_to_sum:
        if col in df.columns: totals[col] = df[col].sum()
    return pd.concat([df, pd.DataFrame([totals])], ignore_index=True)

export_equity = view_data.equity_holdings
zh_export = st.session_state.zerodha_holdings
if zh_export is not None and not zh_export.empty:
    zh_view = zh_export.copy()
    if is_family and holder_view != "👪 All Family": zh_view = zh_view[zh_view["Holder"] == holder_view]
    zh_view.insert(1, "Instrument Type", zh_view["Symbol"].apply(instrument_classifier.classify_equity))
    export_equity = zh_view

export_sections = {
    "Asset Class Summary": _append_raw_total(view_asset_summary, ["Value (₹)", "Change vs Last Month (₹)"]),
    "Instrument Type Breakdown": _append_raw_total(view_instrument_breakdown, ["Value (₹)", "Change vs Last Month (₹)"]),
    "Mutual Funds": _append_raw_total(view_data.mf_folio_holdings, ["Invested (₹)", "Valuation (₹)", "Unrealised P/L (₹)"]),
    "Equity": _append_raw_total(export_equity, ["Value (₹)", "Invested (₹)", "Current Value (₹)", "Unrealised P/L (₹)"]),
    "MF Held in Demat": _append_raw_total(view_data.mf_in_demat_holdings, ["Value (₹)"]),
    "Others (Govt Sec)": _append_raw_total(view_data.other_holdings, ["Value (₹)"]),
    "12M Trend": view_data.valuation_trend,
    "XIRR by Holding": _append_raw_total(xirr_by_holding_df, ["Current Value (₹)"]),
    "XIRR by Asset Class": _append_raw_total(xirr_by_asset_class_df, ["Current Value (₹)"]),
    "XIRR by Instrument Type": _append_raw_total(xirr_by_instrument_type_df, ["Current Value (₹)"]),
    "XIRR by Family Member": _append_raw_total(xirr_by_holder_df, ["Current Value (₹)"]),
}

with report_downloads_container:
    dl_colA, dl_colB = st.columns(2)
    with dl_colA:
        st.download_button(
            "📊 Download all tables (Excel)", 
            data=report_export.build_excel(export_sections), 
            file_name=f"portfolio_report_{date.today().isoformat()}.xlsx", 
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )
    with dl_colB:
        pdf_summary = {"Total Portfolio Value": fmt_inr(view_data.total_value)}
        if not xirr_by_asset_class_df.empty and "XIRR (%)" in xirr_by_asset_class_df.columns:
            for _, r in xirr_by_asset_class_df.iterrows():
                if pd.notna(r["XIRR (%)"]) and not isinstance(r["XIRR (%)"], str): pdf_summary[f"XIRR - {r['Asset Class']}"] = f"{float(r['XIRR (%)']):.2f}%"
        pdf_bytes = report_export.build_pdf(
            title="Portfolio Summary Report" + (f" - {holder_view}" if is_family and holder_view != "👪 All Family" else ""),
            generated_note=f"Generated {date.today().isoformat()} from CAS data",
            summary_metrics=pdf_summary, sections=list(export_sections.items()), charts=export_charts
        )
        st.download_button(
            "📄 Download PDF report", 
            data=pdf_bytes, 
            file_name=f"portfolio_report_{date.today().isoformat()}.pdf", 
            mime="application/pdf",
            use_container_width=True
        )

# --------------------------------------------------------------------------
# Tab 2: Asset Class Summary
# --------------------------------------------------------------------------
with tab_summary:
    st.subheader("Holdings by asset class")
    if fig_pie: st.plotly_chart(fig_pie, use_container_width=True)
    
    display_df = view_asset_summary.copy()
    display_df["Value (₹)"] = display_df["Value (₹)"].apply(fmt_inr)
    display_df["% of Portfolio"] = display_df["% of Portfolio"].apply(lambda x: f"{x:.2f}%")
    if "Change vs Last Month (₹)" in display_df.columns:
        display_df["Change vs Last Month (₹)"] = display_df["Change vs Last Month (₹)"].apply(fmt_inr)
        display_df["Change vs Last Month (%)"] = display_df["Change vs Last Month (%)"].apply(lambda x: f"{x:+.2f}%" if pd.notna(x) else "-")
    total_specs = {"Value (₹)": fmt_inr, "% of Portfolio": lambda x: f"{x:.2f}%"}
    if "Change vs Last Month (₹)" in view_asset_summary.columns: total_specs["Change vs Last Month (₹)"] = fmt_inr
    display_df = _with_total_row(display_df, view_asset_summary, total_specs)
    st.dataframe(display_df, hide_index=True, width="stretch")
    if "Change vs Last Month (₹)" not in view_asset_summary.columns:
        st.caption("💡 Upload last month's CAS in the Settings tab to see variance columns here.")
        
    st.markdown("---")
    st.subheader("Breakdown by instrument type")
    st.caption("Splits each asset class further based on instrument logic.")
    
    if fig_ib: st.plotly_chart(fig_ib, use_container_width=True)
        
    ib_display = view_instrument_breakdown.copy()
    ib_display["Value (₹)"] = ib_display["Value (₹)"].apply(fmt_inr)
    ib_display["% of Portfolio"] = ib_display["% of Portfolio"].apply(lambda x: f"{x:.2f}%")
    if "Change vs Last Month (₹)" in ib_display.columns:
        ib_display["Change vs Last Month (₹)"] = ib_display["Change vs Last Month (₹)"].apply(fmt_inr)
        ib_display["Change vs Last Month (%)"] = ib_display["Change vs Last Month (%)"].apply(lambda x: f"{x:+.2f}%" if pd.notna(x) else "-")
    ib_total_specs = {"Value (₹)": fmt_inr, "% of Portfolio": lambda x: f"{x:.2f}%"}
    if "Change vs Last Month (₹)" in view_instrument_breakdown.columns: ib_total_specs["Change vs Last Month (₹)"] = fmt_inr
    ib_display = _with_total_row(ib_display, view_instrument_breakdown, ib_total_specs)
    st.dataframe(ib_display, hide_index=True, width="stretch")


# --------------------------------------------------------------------------
# Tab 3: Holdings Detail
# --------------------------------------------------------------------------
with tab_holdings:
    st.subheader("Mutual Fund Folios")
    if not view_data.mf_folio_holdings.empty:
        mf_display = view_data.mf_folio_holdings.copy()
        for c in ["Invested (₹)", "Valuation (₹)", "Unrealised P/L (₹)"]: mf_display[c] = mf_display[c].apply(fmt_inr)
        mf_display["Unrealised P/L (%)"] = mf_display["Unrealised P/L (%)"].apply(lambda x: f"{x:.2f}%")
        mf_display = _with_total_row(mf_display, view_data.mf_folio_holdings, {"Invested (₹)": fmt_inr, "Valuation (₹)": fmt_inr, "Unrealised P/L (₹)": fmt_inr})
        st.dataframe(mf_display, hide_index=True, width="stretch")
    else: st.caption("No mutual fund folios found.")

    st.subheader("Equity Holdings")
    zh = st.session_state.zerodha_holdings
    if zh is not None and not zh.empty and "Holder" in zh.columns and is_family and holder_view != "👪 All Family":
        zh = zh[zh["Holder"] == holder_view]
    if zh is not None and not zh.empty:
        eq_display = zh.copy()
        eq_display.insert(1, "Instrument Type", eq_display["Symbol"].apply(instrument_classifier.classify_equity))
        for c in ["Avg. Buy Price (₹)", "Invested (₹)", "Last Price (₹)", "Current Value (₹)", "Unrealised P/L (₹)"]: eq_display[c] = eq_display[c].apply(fmt_inr)
        eq_display["Unrealised P/L (%)"] = eq_display["Unrealised P/L (%)"].apply(lambda x: f"{x:.2f}%")
        eq_display = _with_total_row(eq_display, zh, {"Invested (₹)": fmt_inr, "Current Value (₹)": fmt_inr, "Unrealised P/L (₹)": fmt_inr})
        st.dataframe(eq_display, hide_index=True, width="stretch")
        st.caption("Cost basis and return % from your connected Zerodha account.")
    elif not view_data.equity_holdings.empty:
        eq_display = view_data.equity_holdings.copy()
        eq_display["Value (₹)"] = eq_display["Value (₹)"].apply(fmt_inr)
        eq_display = _with_total_row(eq_display, view_data.equity_holdings, {"Value (₹)": fmt_inr})
        st.dataframe(eq_display, hide_index=True, width="stretch")
        st.caption("Note: the CAS only reports current market value for equities, not your original purchase cost.")
    else: st.caption("No direct equity holdings found.")

    st.subheader("Mutual Funds Held in Demat Form")
    if not view_data.mf_in_demat_holdings.empty:
        d_display = view_data.mf_in_demat_holdings.copy()
        d_display["Value (₹)"] = d_display["Value (₹)"].apply(fmt_inr)
        d_display = _with_total_row(d_display, view_data.mf_in_demat_holdings, {"Value (₹)": fmt_inr})
        st.dataframe(d_display, hide_index=True, width="stretch")
    else: st.caption("None found.")

    st.subheader("Others (Government Securities / Sovereign Gold Bonds)")
    if not view_data.other_holdings.empty:
        o_display = view_data.other_holdings.copy()
        o_display["Value (₹)"] = o_display["Value (₹)"].apply(fmt_inr)
        o_display = _with_total_row(o_display, view_data.other_holdings, {"Value (₹)": fmt_inr})
        st.dataframe(o_display, hide_index=True, width="stretch")
    else: st.caption("None found.")


# --------------------------------------------------------------------------
# Tab 4: XIRR (Bubble Chart First)
# --------------------------------------------------------------------------
with tab_xirr:
    st.subheader("Size vs. return")
    st.caption("Bubble size is current value, position on the vertical axis is XIRR.")
    chart_options = ["Instrument Type", "Asset Class"] + (["Holder"] if is_family else [])
    chart_level = st.radio("Compare by", chart_options, horizontal=True, key="xirr_chart_level")
    
    chart_source = {
        "Instrument Type": xirr_by_instrument_type_df,
        "Asset Class": xirr_by_asset_class_df,
        "Holder": xirr_by_holder_df,
    }[chart_level]
    
    if chart_source.empty or "XIRR (%)" not in chart_source.columns:
        st.info("No solvable XIRR values yet to plot. Please upload transactions in the Settings or Connections tab.")
    else:
        chart_df = chart_source.dropna(subset=["XIRR (%)"]).copy()
        if chart_df.empty:
            st.info("No solvable XIRR values yet to plot. Please upload transactions in the Settings or Connections tab.")
        else:
            chart_df["Current Value"] = chart_df["Current Value (₹)"].apply(fmt_inr)
            use_color_map = ASSET_COLORS if chart_level == "Asset Class" else None
            
            fig_bubble = px.scatter(
                chart_df, x=chart_level, y="XIRR (%)", size="Current Value (₹)",
                color=chart_level, size_max=70, color_discrete_map=use_color_map,
                hover_data={"Current Value (₹)": False, "Current Value": True, chart_level: False},
            )
            fig_bubble.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=450, showlegend=False, xaxis_title="")
            fig_bubble.add_hline(y=0, line_dash="dot", line_color="grey")
            st.plotly_chart(fig_bubble, use_container_width=True)
            
    st.markdown("---")

    def _fmt_xirr_table(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["Current Value (₹)"] = out["Current Value (₹)"].apply(fmt_inr)
        out["XIRR (%)"] = out["XIRR (%)"].apply(lambda x: f"{x:.2f}%" if pd.notna(x) else "couldn't solve")
        out = _with_total_row(out, df, {"Current Value (₹)": fmt_inr})
        return out

    if not xirr_by_holding_df.empty:
        st.subheader("XIRR by holding")
        st.dataframe(_fmt_xirr_table(xirr_by_holding_df), hide_index=True, width="stretch")

        if is_family and not xirr_by_holder_df.empty:
            st.subheader("XIRR by family member")
            st.dataframe(_fmt_xirr_table(xirr_by_holder_df), hide_index=True, width="stretch")

        st.subheader("XIRR by asset class")
        st.dataframe(_fmt_xirr_table(xirr_by_asset_class_df), hide_index=True, width="stretch")

        st.subheader("XIRR by instrument type")
        st.dataframe(_fmt_xirr_table(xirr_by_instrument_type_df), hide_index=True, width="stretch")


# --------------------------------------------------------------------------
# Tab 5: 12-month trend 
# --------------------------------------------------------------------------
with tab_trend:
    st.subheader("Portfolio value - last 12 months")
    st.caption("From the CAS's own month-end valuation history. Shown for context only - it is not a return figure.")
    if not data.valuation_trend.empty:
        if fig_trend: st.plotly_chart(fig_trend, use_container_width=True)
        trend_display = data.valuation_trend.copy()
        trend_display["Portfolio Value (₹)"] = trend_display["Portfolio Value (₹)"].apply(fmt_inr)
        trend_display["Change (₹)"] = trend_display["Change (₹)"].apply(fmt_inr)
        trend_display["Change (%)"] = trend_display["Change (%)"].apply(lambda x: f"{x:.2f}%" if pd.notna(x) else "-")
        st.dataframe(trend_display, hide_index=True, width="stretch")
    else: st.caption("No trend data found in this CAS.")


# --------------------------------------------------------------------------
# Tab 6: Connections 
# --------------------------------------------------------------------------
with tab_connect:
    st.write("Connect live accounts or upload specific transaction files below. *(Note: You can also drag and drop transaction files directly in the Settings tab!)*")
    if is_family: st.caption(f"👪 Family mode - Each source asks which family member it belongs to.")

    def _holder_picker(key: str) -> str:
        if not is_family: return family_holders[0]
        return st.selectbox("Which family member is this for?", family_holders, key=key)

    src_zerodha_live, src_zerodha_csv, src_kuvera, src_glc, src_manual = st.tabs(["Zerodha (live)", "Zerodha (Tradebook CSV)", "Kuvera (statement)", "GLC (PMS)", "Manual CSV"])

    with src_zerodha_live:
        zl_holder = _holder_picker("zl_holder")
        api_key = st.text_input("Kite API key", key="kite_api_key")
        api_secret = st.text_input("Kite API secret", type="password", key="kite_api_secret")
        qp = st.query_params
        auto_token = qp.get("request_token")
        if api_key: st.link_button("1. Log in to Zerodha", zerodha_connector.get_login_url(api_key))
        request_token = st.text_input("2. Paste request_token", value=auto_token or "", key="kite_request_token")
        if st.button("3. Fetch my holdings", disabled=not (api_key and api_secret and request_token)):
            try:
                session = zerodha_connector.generate_session(api_key, api_secret, request_token)
                st.session_state.zerodha_session = session
                holdings_df = zerodha_connector.fetch_holdings(session)
                holdings_df.insert(0, "Holder", zl_holder)
                st.session_state.zerodha_holdings = holdings_df
                st.success(f"Pulled {len(holdings_df)} holdings.")
            except Exception as e: st.error(f"Couldn't connect: {e}")

    with src_zerodha_csv:
        zt_holder = _holder_picker("zt_holder")
        zt_files = st.file_uploader("Upload Tradebook file(s)", type=["csv", "xlsx"], accept_multiple_files=True, key=f"zt_upload_{st.session_state.uploader_nonce['zt']}")
        if zt_files and st.button("Add to XIRR data", key="zt_add"):
            try:
                parsed = parse_tradebook(zt_files)
                parsed.insert(0, "Holder", zt_holder)
                st.session_state.txn_sources["zerodha_tradebook"] = parsed
                st.rerun()
            except Exception as e: st.error(str(e))

    with src_kuvera:
        kv_holder = _holder_picker("kv_holder")
        kv_file = st.file_uploader("Upload Kuvera statement", type=["xlsx", "csv"], key=f"kv_upload_{st.session_state.uploader_nonce['kv']}")
        if kv_file is not None:
            try:
                kv_records = kuvera_import.load_statement(kv_file)
                kv_txns, match_summary = kuvera_import.build_transactions(kv_records, holder_to_data[kv_holder].mf_folio_holdings)
                kv_txns.insert(0, "Holder", kv_holder)
                st.dataframe(match_summary, hide_index=True)
                if st.button("Add to XIRR data", key="kv_add"):
                    st.session_state.txn_sources["kuvera_statement"] = kv_txns
                    st.rerun()
            except Exception as e: st.error(f"Couldn't read that file: {e}")

    with src_glc:
        glc_holder = _holder_picker("glc_holder")
        glc_file = st.file_uploader("Upload GLC Statement", type=["xls", "xlsx"], key=f"glc_upload_{st.session_state.uploader_nonce['glc']}")
        if glc_file is not None:
            try:
                glc_bytes = glc_file.getvalue()
                hd = holder_to_data[glc_holder]
                glc_txns, glc_match_summary = glc_parser.parse_glc_statement(glc_bytes, hd.equity_holdings, hd.mf_folio_holdings, hd.mf_in_demat_holdings)
                glc_txns.insert(0, "Holder", glc_holder)
                st.dataframe(glc_match_summary, hide_index=True)
                if st.button("Add to XIRR data", key="glc_add"):
                    st.session_state.txn_sources["glc_pms"] = glc_txns
                    st.rerun()
            except Exception as e: st.error(f"Couldn't read that file: {e}")

    with src_manual:
        st.download_button("⬇️ Download template (CSV)", data=pd.DataFrame(columns=TXN_SCHEMA).to_csv(index=False).encode("utf-8"), file_name="transactions_template.csv", mime="text/csv")
        manual_file = st.file_uploader("Upload completed CSV", type=["csv"], key=f"manual_upload_{st.session_state.uploader_nonce['manual']}")
        if manual_file is not None:
            try:
                manual_df = pd.read_csv(manual_file)
                manual_df["Date"] = pd.to_datetime(manual_df["Date"]).dt.date
                if "Holder" not in manual_df.columns: manual_df["Holder"] = _holder_picker("manual_holder")
                if st.button("Add to XIRR data", key="manual_add"):
                    st.session_state.txn_sources["manual"] = manual_df[TXN_SCHEMA]
                    st.rerun()
            except Exception as e: st.error(str(e))
