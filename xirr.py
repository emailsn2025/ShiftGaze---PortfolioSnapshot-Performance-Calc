"""
xirr.py
-------
Standard XIRR (money-weighted rate of return) calculation.

XIRR needs actual dated cash flows: every purchase (negative, money leaving
your pocket), every redemption/dividend (positive, money coming back), and
the current value treated as a final positive cash flow "as if sold today".

A single-month CAS statement (like the one CDSL emails you) does not contain
this: it shows current holdings and, for mutual funds, a *cumulative*
invested amount, but not the individual transaction dates. So this module is
built to be fed a proper transaction history (see transactions_template.csv)
- either exported from CAMS/KFinKart ("Transaction Statement" / MF Central)
for mutual funds, or your broker's tradebook for equities.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Sequence


@dataclass
class CashFlow:
    when: date
    amount: float  # negative = money out (investment), positive = money in (redemption / current value)


def xirr(cashflows: Sequence[CashFlow], guess: float = 0.1) -> float | None:
    """
    Returns the annualised XIRR as a decimal (0.12 = 12%), or None if it
    couldn't be solved (e.g. all cash flows same sign, or no convergence).
    Non-numeric amounts return None rather than raising, since a single bad
    row in a larger transaction set shouldn't take down every XIRR result
    on the page - callers that need to know about bad data should validate
    before this point (the app does, in its Amount-sanitising step).
    """
    try:
        flows = [cf for cf in cashflows if float(cf.amount) != 0]
    except (TypeError, ValueError):
        return None
    if len(flows) < 2:
        return None
    if all(cf.amount > 0 for cf in flows) or all(cf.amount < 0 for cf in flows):
        return None

    t0 = min(cf.when for cf in flows)

    def npv(rate: float) -> float:
        if rate <= -1:
            return float("inf")
        total = 0.0
        for cf in flows:
            days = (cf.when - t0).days
            total += cf.amount / ((1 + rate) ** (days / 365.0))
        return total

    def dnpv(rate: float) -> float:
        if rate <= -1:
            return float("inf")
        total = 0.0
        for cf in flows:
            days = (cf.when - t0).days
            years = days / 365.0
            if years == 0:
                continue
            total += -years * cf.amount / ((1 + rate) ** (years + 1))
        return total

    # Newton-Raphson with a bisection fallback for robustness.
    rate = guess
    for _ in range(100):
        try:
            f = npv(rate)
            fp = dnpv(rate)
        except OverflowError:
            break  # rate has wandered somewhere absurd - fall through to bisection
        if fp == 0:
            break
        new_rate = rate - f / fp
        if abs(new_rate - rate) < 1e-7:
            return new_rate
        rate = new_rate
        if rate <= -0.999:
            rate = -0.999
        if rate > 1000:
            break  # clearly diverging - let bisection take over

    # Fallback: bisection over a wide, sane range.
    lo, hi = -0.999, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-6:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def xirr_from_transactions(
    transactions: Sequence[tuple[date, float]],
    current_value: float,
    as_of: date,
) -> float | None:
    """
    Convenience wrapper: pass historical (date, signed_amount) transactions
    plus the current valuation, and this appends the current value as the
    final "sell everything today" cash flow before solving for XIRR.
    """
    flows = [CashFlow(d, amt) for d, amt in transactions]
    flows.append(CashFlow(as_of, current_value))
    return xirr(flows)
