# TRAPP2-CREDIT

Corporate bonds, single-name CDS and credit ratings for Valuatio. One workflow, `Credit (bonds + CDS)`, daily at 0730 ET.

| Step | Script | Writes |
|---|---|---|
| CDS | `pipeline/fetch_cds_dtcc.py` | `data/cds/latest.json`, `data/cds/issuers/<T>.json` (DTCC public swap tape) |
| Bonds | `pipeline/fetch_bonds.py` | `data/bonds/issuers/<T>.json` (OpenFIGI) |
| Index | `pipeline/build_credit_index.py` | `data/credit_index.json` |
| Ratings (z194) | `pipeline/build_ratings.py` + `pipeline/rating_model.py` | `data/ratings.json`, `data/ratings/history/<date>.json` |

## Ratings (z194)

No agency data is licensed here, so every company gets a **model-implied rating on the S&P scale** (AAA … D):

- **Fundamentals** (weight 0.40): S&P's corporate-methodology anchor — business risk (industry risk × competitive position: scale, profitability, 10-year profit stability) × financial risk (FFO/debt, debt/EBITDA, FOCF/debt, EBITDA/interest with interest = the bonds' average coupon × debt), on S&P's standard / medial / low-volatility tables. Banks and insurers use capital / earnings / scale scorecards; captive finance arms (Ford Credit, Deere Financial) are taken out of the industrial debt.
- **Distance to default** (0.35): Merton / Bharath–Shumway, equity value and volatility (1-year realized, raised to option-implied in stress) against the debt.
- **CDS** (0.10 × liquidity): the 5-year spread against the current spread of each rating bucket (ICE BofA OAS by rating). It carries little of the level and most of the **trend**.

Calibrated on 143 well-known companies (2026-11-18): mean gap to their agency ratings 1.03 notches, 62 % within one notch, 83 % within two. A rating moves only when the model passes 0.75 notch beyond the published one.

Everything else is rated by **look-through**: the S&P 500 fund is its ~500 members' ratings by index weight, combined by Moody's WARF (default-weighted); sector funds their sector; leveraged / inverse products their underlying; single-stock products that stock; country funds, foreign indices and FX their sovereign (`config/sovereign_ratings.json`); Treasury funds and futures the United States; bond funds their sleeve; ETNs no better than the issuing bank. Commodities, crypto and volatility products are NR (no issuer).

Config you can edit: `config/lookthrough.json` (how each fund / index / future is rated), `config/sovereign_ratings.json` (S&P sovereign ratings), `config/agency_ratings.json` (real agency ratings, if you have them — they replace the model for that company).
