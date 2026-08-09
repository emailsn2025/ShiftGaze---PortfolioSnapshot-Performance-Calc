"""
zerodha_connector.py
---------------------
Thin wrapper around the official Kite Connect SDK (kiteconnect), using the
free "Personal" API tier Zerodha introduced in 2025 - free for pulling your
own holdings, positions, and funds. No live/historical *market* data on the
free tier, which is fine here since we only need YOUR holdings.

Get a free API key + secret at https://developers.kite.trade (My Apps ->
Create New App -> choose "Personal"). You'll need a redirect URL - if you're
running this app locally, http://localhost:8501 works fine; if deployed,
use your Streamlit app's public URL.

IMPORTANT: this module never stores your API secret or access token to
disk - everything lives only in Streamlit's session state for the current
browser session.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

try:
    from kiteconnect import KiteConnect
except ImportError:  # pragma: no cover
    KiteConnect = None


@dataclass
class ZerodhaSession:
    api_key: str
    access_token: str


def get_login_url(api_key: str) -> str:
    """The URL to send the user to for Zerodha's own login + 2FA."""
    kite = KiteConnect(api_key=api_key)
    return kite.login_url()


def generate_session(api_key: str, api_secret: str, request_token: str) -> ZerodhaSession:
    """
    Exchange the one-time request_token (from the redirect after login) for
    an access_token. The access_token is valid until ~6am IST the next day -
    Zerodha does not support long-lived personal tokens, so this has to be
    repeated each session; there's no way around that on their end.
    """
    kite = KiteConnect(api_key=api_key)
    data = kite.generate_session(request_token, api_secret=api_secret)
    return ZerodhaSession(api_key=api_key, access_token=data["access_token"])


def fetch_holdings(session: ZerodhaSession) -> pd.DataFrame:
    """
    Current equity holdings, including average buy price - i.e. real cost
    basis, which the CDSL CAS does not give us. Does NOT include purchase
    dates (Kite's API doesn't expose those - see zerodha_tradebook.py for
    the dated history import).
    """
    kite = KiteConnect(api_key=session.api_key, access_token=session.access_token)
    holdings = kite.holdings()
    if not holdings:
        return pd.DataFrame(
            columns=["Symbol", "ISIN", "Quantity", "Avg. Buy Price (₹)",
                     "Invested (₹)", "Last Price (₹)", "Current Value (₹)",
                     "Unrealised P/L (₹)", "Unrealised P/L (%)"]
        )

    rows = []
    for h in holdings:
        qty = h.get("quantity", 0) + h.get("t1_quantity", 0)
        avg_price = h.get("average_price", 0.0)
        last_price = h.get("last_price", 0.0)
        invested = qty * avg_price
        current = qty * last_price
        pl = current - invested
        rows.append(
            {
                "Symbol": h.get("tradingsymbol", ""),
                "ISIN": h.get("isin", ""),
                "Quantity": qty,
                "Avg. Buy Price (₹)": avg_price,
                "Invested (₹)": invested,
                "Last Price (₹)": last_price,
                "Current Value (₹)": current,
                "Unrealised P/L (₹)": pl,
                "Unrealised P/L (%)": (pl / invested * 100) if invested else 0.0,
            }
        )
    return pd.DataFrame(rows).sort_values("Current Value (₹)", ascending=False).reset_index(drop=True)
