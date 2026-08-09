# Portfolio Summary & XIRR Tracker

A small Streamlit app that turns your CDSL Consolidated Account Statement
(CAS) PDF into a clean, tabular summary of your holdings by asset class —
Equity, Mutual Fund Folios, Mutual Funds Held in Demat Form, and Others
(government securities / Sovereign Gold Bonds) — with charts, a downloadable
Excel breakdown, and an XIRR calculator.

## Two supported statement formats

The sidebar accepts either of two document types, auto-detected per file -
mix freely across family members:

- **CDSL Consolidated Account Statement (CAS)** - the original target,
  covering equity, mutual funds, government securities/SGBs, all in one
  statement, plus 12 months of value history.
- **MF Central Consolidated Account Summary** (CAMS + KFintech, from
  mfcentral.com) - mutual funds only, no equity or demat holdings, no
  ISIN column, no value history. Useful for a family member who only
  holds mutual funds and doesn't have a demat account at all.

Since MF Central doesn't print an ISIN, `mfcentral_parser.py` backfills one
by fetching AMFI's free public scheme master
(amfiindia.com/spages/NAVAll.txt) and fuzzy-matching scheme names against
it (`amfi_lookup.py`, reusing the same fund-house-gated matcher as the
Kuvera importer - now shared in `scheme_matching.py`). This is a genuine
network call, unlike everything else in this app - if it fails for any
reason (offline, AMFI down, a firewalled deployment), holdings just keep a
synthetic identifier instead of crashing; they still display and total
correctly, just without ISIN-based cross-referencing against Zerodha/Kuvera
transactions for that specific holding.

## Family / household mode

Upload more than one CAS PDF in the sidebar - one per family member - and
the app combines them into a household-wide view instead of just one
person's. Each holdings table, the asset-class summary, and the 12-month
trend all become true aggregates (not just one file relabelled), and a
"Holder" column tags every row throughout, including in both exports.

A few things worth knowing:
- Each person's name is read directly off their own CAS (the "...in the
  single name of X ( PAN..." line CDSL prints), with an editable text box
  in the sidebar in case it's misread or you'd rather use a nickname.
- The **Viewing** selector above the tabs lets you drill from "All Family"
  down to one person for the Asset Class Summary, Holdings Detail, and
  both exports. XIRR stays family-wide regardless, since it has its own
  "XIRR by family member" breakdown instead.
- In Connect & Import, every source (Zerodha live, Zerodha Tradebook,
  Kuvera, Manual CSV) asks which family member it belongs to (only shown
  once there's more than one person loaded). This matters for correctness,
  not just labelling: `value_lookup` in `app.py` is keyed by
  `(Holder, ISIN)`, not just `ISIN`, specifically so two people holding the
  same stock or fund never get their positions merged or misattributed to
  the wrong person's cost basis. Kuvera scheme-matching also matches
  against each holder's *own* CAS holdings, not the pooled family table,
  for the same reason.
- The month-over-month comparison (see below) also accepts multiple
  previous-month files and matches them to current holders by name.

## Unified drop zone + save/resume

The XIRR tab has two extra tools beyond the per-source flow in Connect &
Import:

- **Drop zone** — drag in any mix of Zerodha Tradebook, Kuvera, GLC (PMS),
  or manual-CSV-template files at once; each is auto-detected (by content,
  not filename) and routed to the matching parser. Doesn't cover CAS PDFs
  (sidebar) or the live Zerodha connect (needs its own login step) - those
  stay where they are. In family mode, each detected file gets its own
  holder picker before you commit to importing.
- **Save / resume** — download everything currently loaded as one JSON
  file, and load it back in a later session instead of re-uploading and
  re-matching your Kuvera/GLC/Zerodha files from scratch. This only
  persists the transaction side (already-matched identifiers included) -
  you still bring your CAS PDF(s) fresh each session, since that's also
  where current values get looked up from for XIRR. Verified round-trip:
  exporting then re-importing produces byte-identical transactions and
  identical XIRR results.

## Why XIRR needs a second source

Your monthly CAS tells you what you hold *today* and, for mutual funds, the
cumulative amount you've invested — enough to compute an absolute return %.
It does **not** list the date and amount of every individual purchase, SIP
instalment, or redemption, which is what a true XIRR (money-weighted,
annualised return) requires.

So the app does two things:
1. **Always works, from the CAS alone:** the asset-class summary, holdings
   tables, and each mutual fund's absolute return % (which CAMS/KFIN already
   compute and print on the statement).
2. **Optional, for real XIRR:** bring in transaction history from the
   **Connect & Import** tab, from any combination of:
   - **Zerodha (live)** — current holdings + average buy price via Zerodha's
     free Kite Connect Personal API (`zerodha_connector.py`). Gives real
     equity cost basis instantly, but not purchase dates.
   - **Zerodha (Tradebook CSV)** — full dated buy/sell history, exported
     free from Console → Reports → Tradebook (`zerodha_tradebook.py`).
     This is what actually feeds equity XIRR.
   - **Kuvera (statement)** — Kuvera has no public API, so this parses
     whatever you export from Kuvera's own Reports section
     (`kuvera_import.py`), auto-detecting between two known export shapes:
     a newer clean CSV/XLSX with real column headers (Date, Folio Number,
     Name of the Fund, Order, Units, NAV, Current Nav, Amount), and an
     older `.xlsx` shape with every field flattened one-per-row down a
     single column and an inconsistent field count - parsed by classifying
     each value's *type* (a date starts a record; text fields are
     scheme/buy-sell; numbers are units/price/amount) rather than assuming
     a fixed position. Neither includes an ISIN, so each scheme name is
     matched to your CAS by fund house + category words (gated so "HDFC
     Large Cap" can never match "SBI Large Cap" just because both contain
     "large cap") — the app shows you the match table before you rely on
     it. A scheme with no match usually means you've fully redeemed it -
     these are excluded from every XIRR calculation entirely (with a note
     showing how many), rather than computing a return for a holding the
     app was never actually able to confirm the identity of.
   - **GLC (PMS)** — parses a Green Lantern Capital PMS "Transaction
     Statement" export (`.xls`) (`glc_parser.py`). This one's a flattened
     multi-page report - the account header, title, and column header row
     physically repeat every ~30 rows (one block per printed page of the
     original), with instrument-category labels ("Shares - Listed",
     "Mutual Funds - Liquid") and settlement-status labels interleaved as
     their own rows; the parser tracks both as state while scanning top to
     bottom, tagging each Buy/Sell row with whatever was most recently seen
     above it. No ISIN here either - since a PMS holds its securities
     through the same underlying demat/folio structure as your own CAS,
     each listed share is matched against your CAS *equity* holdings
     (using a company-name-tuned matcher in `scheme_matching.py`, separate
     from the fund-name one, since Indian corporate demergers can leave
     several similarly-named listed entities - e.g. Vedanta Limited vs
     Vedanta Power Limited vs Vedanta Aluminium Metal Limited - that need
     exact rather than loose matching to tell apart), and anything else
     (e.g. a liquid fund used for cash management) against your CAS mutual
     fund holdings, same convention as Kuvera for no-match cases.
   - **Manual CSV** — the original template, for anything else (other
     PMS providers, other brokers, funds outside Zerodha/Kuvera/GLC).

   Everything loaded gets pooled together and the XIRR tab computes real,
   dated XIRR per holding and per asset class.

### A deliberate omission: no Kuvera API integration

Kuvera doesn't publish a developer API. There's a community-maintained
*unofficial* spec reverse-engineering some of Kuvera's endpoints — but
inspecting it shows it only covers public market data (fund NAVs, AMC
lists, gold/crypto prices) plus login/profile — it does **not** document
any endpoint for your actual folios, holdings, or transactions. Going
further than that published spec would mean probing Kuvera's private,
undocumented endpoints directly, which isn't something this project does —
the risk (to your account, and just generally scraping a fintech platform's
internal API without any sanctioned path) isn't worth it when the Reports
export does the job safely. If Kuvera ever ships a real API, swapping it in
would be a contained change to `kuvera_import.py`.

### A note on Zerodha's live connect

Kite Connect access tokens expire daily by Zerodha's design — there's no
official way to get a long-lived personal token. So "Zerodha (live)" is a
"click connect each time you check in" flow, not a background sync. Nothing
you enter (API key, secret, or the resulting token) is written to disk —
it lives only in Streamlit's session state for that browser session.

## Instrument-type breakdown

The CAS gives you an asset *class* (Equity / Mutual Fund Folios / Mutual
Funds Held in Demat Form / Others) but not an instrument *type* within
that — it doesn't say whether a fund is a liquid fund or a small-cap
equity fund. `instrument_classifier.py` infers this from scheme/security
names (which are fairly standardised in India), splitting things out into
buckets like Equity Fund, Index Fund, Equity Hybrid Fund, Multi-Asset
Fund, and debt sub-types (Liquid Fund, Gilt/Treasury Fund, Banking & PSU
Fund, Bond/Other Debt Fund), plus Direct Equity (Shares) vs Equity ETF.
This shows up throughout the app: the Asset Class Summary tab's breakdown
table and chart, the XIRR tab's third grouping level, and every export.

## Month-over-month variance

A single CAS only tracks *total* portfolio value over time (see the
12-Month Trend tab) — it doesn't give you a historical breakdown by asset
class or instrument type. To get real variance columns broken down that
way, upload last month's CAS too, in the sidebar's "Compare to last
month" section. With both loaded, the Asset Class Summary and Instrument
Type Breakdown tables both grow "Change vs Last Month (₹/%)" columns.

## Excel & PDF export

The section right under the header metrics has two buttons that pull
together *everything* currently loaded - asset class & instrument type
breakdowns always, holdings tables, the 12-month trend, and XIRR tables
too once you've imported transaction history:
- **Excel** (`report_export.build_excel`) - one workbook, one sheet per table.
- **PDF** (`report_export.build_pdf`) - a print-friendly report, tables
  only (no charts, to keep this dependency-light and reliable rather than
  pulling in a chart-rasterising toolchain). Uses a bundled DejaVu Sans
  font (`assets/`) rather than reportlab's default, since the default
  can't render ₹ at all - it shows as a black box otherwise. This is
  portable across any deployment (local, Streamlit Cloud) since the font
  ships with the repo rather than relying on system fonts being present.

  **If the PDF button errors, or ₹ shows as "Rs." in the PDF instead of
  the symbol:** the two `.ttf` files under `assets/` didn't make it into
  your deployment. Unlike every other file in this repo, those are
  *binary*, not text - if you've been copying files over by pasting code
  into GitHub's web editor, that only works for text files; a pasted
  `.ttf` is corrupted or empty. The app won't crash either way (it falls
  back to "Rs." automatically instead), but to get the ₹ symbol back:
  - **Easiest:** on your repo's GitHub page, go into the `assets` folder
    (create it if it's missing) and use **Add file → Upload files**, then
    drag `assets/DejaVuSans.ttf` and `assets/DejaVuSans-Bold.ttf` in from
    your local copy of this zip. That's a real binary upload, not a paste.
  - **With git installed:** `git add assets/*.ttf && git commit -m "Add fonts" && git push`
    copies the binary files correctly since git doesn't care about content type.

## Project structure

```
.
├── app.py                       # Streamlit app (UI + orchestration)
├── cas_parser.py                 # Parses the CDSL CAS PDF into structured data
├── mfcentral_parser.py            # Parses the MF Central Consolidated Account Summary PDF
├── amfi_lookup.py                 # AMFI scheme-master fetch + ISIN backfill for MF Central
├── scheme_matching.py             # Shared fund-house-gated fuzzy scheme matcher
├── instrument_classifier.py      # Infers instrument type from scheme/security names
├── zerodha_connector.py          # Kite Connect Personal API (live holdings + cost basis)
├── zerodha_tradebook.py          # Parses Zerodha Console Tradebook CSV/XLSX exports
├── kuvera_import.py              # Parses both known Kuvera export shapes
├── glc_parser.py                  # Parses Green Lantern Capital PMS transaction statements
├── report_export.py              # Excel workbook + PDF report builders
├── xirr.py                       # XIRR (money-weighted return) calculation
├── transactions_template.csv     # Template for the manual-entry XIRR path
├── assets/                       # Bundled fonts (DejaVu Sans, for ₹ in the PDF)
├── requirements.txt
└── .streamlit/config.toml        # Streamlit server config
```

## Run it locally

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

It'll open at `http://localhost:8501`. Upload your CAS PDF in the sidebar.

Nothing is written to disk or sent anywhere outside the running app — the
PDF and any transaction CSV you upload are parsed in memory for that
session only.

## Put it on GitHub

```bash
git init
git add .
git commit -m "Initial commit: portfolio summary & XIRR tracker"
```

Then create a new (empty) repository on GitHub — **don't** initialise it
with a README, since you already have one — and push:

```bash
git remote add origin https://github.com/<your-username>/<repo-name>.git
git branch -M main
git push -u origin main
```

⚠️ **Before you push anything**, double-check `git status` doesn't show
your actual CAS PDF or a filled-in transaction CSV — `.gitignore` is
already set up to exclude `*.pdf` and `*.csv` (other than the template),
but it's worth a glance since this is financial data.

## Deploy to Streamlit Community Cloud (free)

1. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with
   GitHub.
2. Click **"New app"**, pick the repo you just pushed, branch `main`, and
   set the main file path to `app.py`.
3. Click **Deploy**. It'll install `requirements.txt` and give you a
   public URL like `https://<something>.streamlit.app`.

That URL is public by default — anyone with the link can open the app and
upload *their own* CAS (nothing of yours is stored on the server between
visits). If you'd rather keep it private, Streamlit Community Cloud lets
you restrict access to specific viewers under the app's settings, or you
can run it privately with `streamlit run app.py` on your own machine
whenever you need it.

## Extending this

- The parser (`cas_parser.py`) classifies holdings using India's standard
  ISIN prefix convention (`INE` = equity, `INF` = mutual fund, `IN0` =
  government security), so it should work on any CDSL CAS, not just this
  one — NSDL-issued CAS PDFs use a similar layout and would need light
  adjustments if the table headers differ.
- `xirr.py` is a standalone, dependency-light XIRR solver (Newton-Raphson
  with a bisection fallback) — it's reusable outside Streamlit if you want
  to fold this into a bigger net-worth tracker later.
