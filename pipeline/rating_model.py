# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-CREDIT
#  >>> FILE PATH:   pipeline/rating_model.py
#  z194 - CREDIT RATINGS, the model (pure functions, no I/O; build_ratings.py feeds it).
#
#  Agency ratings (S&P, Moody's, Fitch) are licensed data - none is on file, so every company gets a
#  transparent MODEL-IMPLIED rating on the S&P scale (AAA ... D, 22 notches), from three pillars:
#    1. market    - the 5-year CDS spread (DTCC public tape) against the CURRENT spread of each rating
#                   bucket (ICE BofA OAS by rating, FRED): Moody's market-implied-rating idea. Weighted by
#                   how liquid / fresh the CDS print is.
#    2. structural- Merton distance-to-default (Bharath-Shumway naive form): equity value and volatility
#                   against the debt it must cover. Not used for banks / insurers (deposits, policy
#                   liabilities are not in "total debt").
#    3. fundamental- S&P's corporate methodology anchor: business risk (industry risk, scale,
#                   profitability) x financial risk (debt/EBITDA, FOCF/debt, EBITDA/interest with interest
#                   = average bond coupon x debt), on S&P's standard / medial / low-volatility ratio tables.
#                   Banks and insurers get their own capital / earnings / size scorecards.
#  A user-supplied agency rating (config/agency_ratings.json) replaces the model when present.
#  Funds, ETPs, futures, FX and indices are rated by LOOK-THROUGH (their holdings' ratings combined by
#  Moody's WARF - default-risk weighted, so a few junk names weigh more than a linear average), by
#  their sovereign (Treasuries, country funds, FX), or NR when there is no issuer (commodities, crypto).
#  Notches are continuous inside the model (no cliff when a ratio crosses a bucket edge) and rounded
#  once, with a 0.75-notch dead band against the last published rating (ratings are sticky, like an
#  agency's - a weight shift between pillars must not flip them day to day).
# ============================================================
import math
import re

SCALE = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-",
         "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "D"]          # notch 1 .. 22
IG_MAX = 10           # BBB- : investment grade at or better
DISTRESS = 17         # CCC+ : distressed at or worse
MOODYS = {"Aaa": 1, "Aa1": 2, "Aa2": 3, "Aa3": 4, "A1": 5, "A2": 6, "A3": 7, "Baa1": 8, "Baa2": 9, "Baa3": 10,
          "Ba1": 11, "Ba2": 12, "Ba3": 13, "B1": 14, "B2": 15, "B3": 16, "Caa1": 17, "Caa2": 18, "Caa3": 19,
          "Ca": 20, "C": 21}
# Moody's rating factors (idealized 10-year expected loss, the CLO / fund WARF convention) by notch
WARF = {1: 1, 2: 10, 3: 20, 4: 40, 5: 70, 6: 120, 7: 180, 8: 260, 9: 360, 10: 610, 11: 940, 12: 1350,
        13: 1766, 14: 2220, 15: 2720, 16: 3490, 17: 4770, 18: 6500, 19: 8070, 20: 10000, 21: 10000, 22: 10000}
HYSTERESIS = 0.75     # notches the model must move past the published rating before it changes


def clip(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def finite(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


# ----------------------------------------------------------------------------------- the scale ----
def notch(r):
    """'AA-' / 'aa-' / 'Aa3' / 'BBB (high)' / 'SD' -> 1..22 (None if not a rating)."""
    if r is None:
        return None
    if isinstance(r, (int, float)) and not isinstance(r, bool):
        return clip(int(round(r)), 1, 22) if math.isfinite(r) else None
    s = str(r).strip()
    if not s:
        return None
    if s in MOODYS:
        return MOODYS[s]
    s = re.sub(r"\s*\((?:high|low)\)|\s+(?:positive|negative|stable|developing)\b.*$|\s*\*.*$", "", s, flags=re.I).strip().upper()
    if s in ("SD", "RD", "D", "DD", "DDD"):
        return 22
    return SCALE.index(s) + 1 if s in SCALE else None


def letter(n):
    """Continuous or integer notch -> letter (rounded, 1..22). None -> 'NR'."""
    if n is None:
        return "NR"
    return SCALE[int(clip(int(math.floor(n + 0.5)), 1, 22)) - 1]


def bucket(n):
    """Broad bucket of a notch: AAA / AA / A / BBB / BB / B / CCC / D."""
    if n is None:
        return "NR"
    n = int(clip(int(math.floor(n + 0.5)), 1, 22))
    return "AAA" if n == 1 else "AA" if n <= 4 else "A" if n <= 7 else "BBB" if n <= 10 else "BB" if n <= 13 \
        else "B" if n <= 16 else "CCC" if n <= 21 else "D"


def warf_of(n):
    """Moody's rating factor of a (continuous) notch - log-interpolated between notches."""
    n = clip(float(n), 1.0, 22.0)
    lo = int(math.floor(n))
    hi = min(22, lo + 1)
    f = n - lo
    a, b = WARF[lo], WARF[hi]
    return math.exp(math.log(a) * (1 - f) + math.log(b) * f)


def notch_of_warf(w):
    """Inverse of warf_of on 1..20 (WARF 10000 = Ca/CC; C and D share that factor)."""
    w = clip(float(w), 1.0, 10000.0)
    for k in range(1, 20):
        a, b = WARF[k], WARF[k + 1]
        if w <= b:
            return k + (math.log(w) - math.log(a)) / (math.log(b) - math.log(a)) if b > a else float(k)
    return 20.0


def interp(x, pts):
    """Piecewise-linear y(x) through pts [(x, y), ...] sorted by x, flat beyond the ends."""
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * ((x - x0) / (x1 - x0) if x1 != x0 else 0.0)
    return pts[-1][1]


# --------------------------------------------------------------------- pillar 1: CDS-implied ----
# The bucket spreads are index OAS (all maturities, ~7-10 years for IG); the CDS tape is 5-year. The 5-year point
# sits below the index OAS on an upward-sloping IG curve - TENOR scales each bucket to a 5-year equivalent. Set so the
# CDS-implied notch is unbiased against agency ratings of 73 names with liquid CDS on 2026-11-18 (IG 0.50, HY 0.78);
# its scatter around them is wide (~3 notches: thin prints, AI-capex hedging in the megacaps), so the market pillar
# carries little of the LEVEL and most of the TREND.
BUCKET_SERIES = (("AAA", "BAMLC0A1CAAA", 1), ("AA", "BAMLC0A2CAA", 3), ("A", "BAMLC0A3CA", 6), ("BBB", "BAMLC0A4CBBB", 9),
                 ("BB", "BAMLH0A1HYBB", 12), ("B", "BAMLH0A2HYB", 15), ("CCC", "BAMLH0A3HYC", 18))
TENOR = {"AAA": 0.50, "AA": 0.50, "A": 0.50, "BBB": 0.50, "BB": 0.78, "B": 0.78, "CCC": 0.78}
FALLBACK_OAS = {"AAA": 40, "AA": 55, "A": 75, "BBB": 110, "BB": 200, "B": 340, "CCC": 950}   # bp, if FRED is missing


def spread_anchors(oas_bp=None):
    """5-year CDS spread (bp) typical of each notch 1..21, from the rating buckets' current OAS. Flat notches
    (AAA, AA, A, BBB, BB, B, CCC) sit on their bucket; the +/- notches are log-interpolated between buckets;
    CCC-, CC, C extend the B->CCC slope. Strictly increasing (>= 4 % a notch)."""
    oas = dict(FALLBACK_OAS)
    for k, v in (oas_bp or {}).items():
        v = finite(v)
        if v and v > 0:
            oas[k] = v
    centers = [(n, math.log(oas[k] * TENOR[k])) for k, _, n in BUCKET_SERIES]
    a = {}
    for (n0, l0), (n1, l1) in zip(centers, centers[1:]):
        for n in range(n0, n1 + 1):
            a[n] = l0 + (l1 - l0) * (n - n0) / (n1 - n0)
    slope = (centers[-1][1] - centers[-2][1]) / (centers[-1][0] - centers[-2][0])
    for n in range(19, 22):
        a[n] = centers[-1][1] + slope * (n - 18)
    out = []
    prev = None
    for n in range(1, 22):
        v = math.exp(a[n])
        if prev is not None and v < prev * 1.04:
            v = prev * 1.04
        out.append(round(v, 2))
        prev = v
    return out


def cds_notch(bps, anchors):
    """Continuous notch of a 5-year spread against the notch anchors (log-interpolated)."""
    s = finite(bps)
    if s is None or s <= 0:
        return None
    if s <= anchors[0]:
        return 1.0
    for k in range(len(anchors) - 1):
        lo, hi = anchors[k], anchors[k + 1]
        if s <= hi:
            return k + 1 + math.log(s / lo) / math.log(hi / lo)
    return float(len(anchors))


def norm_bps(x):
    """DTCC disseminates a 5-year spread in basis points - or as a percentage (0.60 = 60 bp). No real 5-year
    corporate / sovereign spread is under 5 bp, so a value < 5 is a percentage."""
    x = finite(x)
    if x is None or x <= 0:
        return None
    return round(x * 100, 2) if x < 5 else x


def cds_liquidity(n30, last_date, today):
    """0..1 trust in the CDS level: trades in 30 days (8+ = full) x freshness (1 within 7 days, 0 at 45)."""
    n30 = finite(n30) or 0
    if not last_date:
        return 0.0
    try:
        from datetime import date
        age = (date.fromisoformat(str(today)[:10]) - date.fromisoformat(str(last_date)[:10])).days
    except ValueError:
        return 0.0
    fresh = 1.0 if age <= 7 else 0.0 if age >= 45 else 1.0 - (age - 7) / 38.0
    return round(clip(n30 / 8.0, 0.0, 1.0) * fresh, 3)


# -------------------------------------------------------------- pillar 2: distance to default ----
MERTON_MU = 0.05      # asset drift (a long-run assumption, not last year's return - ratings are not momentum)


def merton_dd(E, F, sigma_e, mu=MERTON_MU, T=1.0):
    """Bharath & Shumway (2008) naive distance to default. E equity value, F default point, sigma_e annual
    equity volatility. sigma_D = 0.05 + 0.25 sigma_E; sigma_V the value-weighted mix."""
    E, F, s = finite(E), finite(F), finite(sigma_e)
    if not E or not F or not s or E <= 0 or F <= 0 or s <= 0:
        return None
    sd = 0.05 + 0.25 * s
    V = E + F
    sv = (E / V) * s + (F / V) * sd
    return (math.log(V / F) + (mu - 0.5 * sv * sv) * T) / (sv * math.sqrt(T))


# DD -> notch: notch = 13.8 - 3.4 ln(DD), the least-squares fit of agency ratings on log distance-to-default over 132
# well-known companies (2026-11-18; DD 1 ~ BB-, 4 ~ BBB, 10 ~ A, 20 ~ AA-). Never better than AA from leverage alone -
# Merton cannot tell an AAA from a lightly levered AA.
DD_FIT = (13.8, 3.4)
DD_BEST = 3.0


def dd_notch(dd):
    d = finite(dd)
    if d is None:
        return None
    if d <= 0:
        return 21.0
    return clip(DD_FIT[0] - DD_FIT[1] * math.log(d), DD_BEST, 21.0)


def default_point(total_debt, lt_debt=None):
    """KMV / Bharath-Shumway default point: short-term debt + half of long-term debt. Without the split, 60 %
    of total debt (the typical large-cap mix: most debt long-term)."""
    D = finite(total_debt)
    if not D or D <= 0:
        return None
    L = finite(lt_debt)
    if L is not None and 0 <= L <= D:
        return (D - L) + 0.5 * L
    return 0.6 * D


# ------------------------------------------------------------------- pillar 3: fundamentals ----
# Industry risk 1 (very low) .. 6 (very high), from S&P's industry risk assessments, by Yahoo industry.
SECTOR_RISK = {"Utilities": 2, "Consumer Defensive": 2, "Healthcare": 2, "Technology": 3, "Communication Services": 3,
               "Consumer Cyclical": 4, "Industrials": 3, "Energy": 5, "Basic Materials": 4, "Real Estate": 3,
               "Financial Services": 3}
INDUSTRY_RISK = {
    "Utilities - Regulated Electric": 1, "Utilities - Regulated Gas": 1, "Utilities - Regulated Water": 1,
    "Utilities - Diversified": 1.5, "Utilities - Independent Power Producers": 4, "Utilities - Renewable": 4,
    "Education & Training Services": 4, "Farm Products": 4, "Food Distribution": 3, "Grocery Stores": 3,
    "Biotechnology": 5, "Diagnostics & Research": 3, "Drug Manufacturers - Specialty & Generic": 3,
    "Health Information Services": 3, "Medical Care Facilities": 3, "Medical Distribution": 3,
    "Semiconductors": 4, "Semiconductor Equipment & Materials": 4, "Computer Hardware": 4, "Consumer Electronics": 4,
    "Communication Equipment": 4, "Electronic Components": 4, "Electronics & Computer Distribution": 4, "Solar": 5,
    "Telecom Services": 2, "Entertainment": 4, "Advertising Agencies": 4, "Broadcasting": 4, "Publishing": 4,
    "Electronic Gaming & Multimedia": 4,
    "Restaurants": 3, "Internet Retail": 3, "Home Improvement Retail": 3, "Packaging & Containers": 3, "Luxury Goods": 3,
    "Residential Construction": 5, "Department Stores": 5, "Recreational Vehicles": 5, "Textile Manufacturing": 5,
    "Railroads": 2, "Waste Management": 2, "Airlines": 5, "Marine Shipping": 5, "Trucking": 4, "Building Products & Equipment": 4,
    "Engineering & Construction": 4, "Farm & Heavy Construction Machinery": 4, "Rental & Leasing Services": 4,
    "Staffing & Employment Services": 4, "Metal Fabrication": 4, "Business Equipment & Supplies": 4,
    "Oil & Gas Midstream": 3, "Oil & Gas Integrated": 4, "Oil & Gas Drilling": 6,
    "Specialty Chemicals": 3, "Building Materials": 3, "Paper & Paper Products": 4, "Chemicals": 4, "Agricultural Inputs": 4,
    "REIT - Office": 5, "REIT - Hotel & Motel": 4, "Real Estate - Development": 5, "Real Estate Services": 4,
    "Financial Data & Stock Exchanges": 2, "Insurance Brokers": 2, "Asset Management": 3, "Capital Markets": 3,
    "Credit Services": 3, "Financial Conglomerates": 3,
}
MEDIAL = {"Telecom Services", "Oil & Gas Midstream", "Waste Management", "Railroads", "Utilities - Diversified",
          "Utilities - Independent Power Producers", "Utilities - Renewable"}
LOW_VOL = {"Utilities - Regulated Electric", "Utilities - Regulated Gas", "Utilities - Regulated Water"}
# ratio -> financial-risk score 1 (minimal) .. 6 (highly leveraged): S&P's bucket edges sit at the half points.
# FFO/debt is S&P's primary ratio (FFO = EBITDA - interest - cash tax; here proxied from EBITDA, the bond coupons and
# the SEC operating cash flow); debt/EBITDA the second core ratio; FOCF/debt and EBITDA/interest supplementary.
FFO_PTS = {"standard": [60, 45, 30, 20, 12], "medial": [50, 35, 23, 13, 9], "low": [35, 23, 13, 9, 6]}
LEV_PTS = {"standard": [1.5, 2, 3, 4, 5], "medial": [1.75, 2.5, 3.5, 4.5, 5.5], "low": [2, 3, 4, 5, 6],
           "reit": [4.5, 6, 7.5, 9, 10.5]}
FOCF_PTS = {"standard": [40, 25, 15, 10, 5]}
COV_PTS = {"standard": [15, 9, 6, 4, 2], "medial": [10.5, 7, 4, 2.5, 1.5], "low": [8, 5.5, 3.5, 2.2, 1.4],
           "reit": [5, 3.5, 2.6, 1.9, 1.4]}
# which ratios count, by table (capex-funded, regulated or contracted cash flows: FOCF is negative by design)
RATIO_W = {"standard": {"ffo": 0.35, "lev": 0.30, "cov": 0.15, "focf": 0.20},
           "medial": {"ffo": 0.45, "lev": 0.30, "cov": 0.25},
           "low": {"ffo": 0.45, "lev": 0.30, "cov": 0.25},
           "reit": {"lev": 0.60, "cov": 0.40}}
# S&P business risk = competitive position (rows: excellent .. vulnerable) x industry / country risk (cols: very low ..
# very high) - S&P corporate methodology, Table 1
BUSINESS = [[1, 1, 1, 2, 3, 5],
            [1, 2, 2, 3, 4, 5],
            [2, 3, 3, 3, 4, 6],
            [3, 4, 4, 4, 5, 6],
            [4, 5, 5, 5, 5, 6],
            [5, 6, 6, 6, 6, 6]]
# S&P anchor: business risk (rows: excellent .. vulnerable) x financial risk (cols: minimal .. highly leveraged).
# A "a+/a" pair is its midpoint (5.5): the scores are continuous.
ANCHOR = [[1.5, 3, 5.5, 7, 9, 10.5],
          [3.5, 5.5, 7.5, 9, 11, 12],
          [6.5, 8, 9.5, 10.5, 12, 14],
          [9.5, 10, 11, 12, 13, 15],
          [11, 11, 12, 13, 14, 15.5],
          [13, 13, 13.5, 14, 15, 16]]
# finance arms inside industrial groups (Ford Credit, John Deere Financial, Cat Financial, GM Financial): S&P rates
# the industrial business on its own debt - the arm's receivable-backed debt is taken out
CAPTIVE = {"Auto Manufacturers", "Farm & Heavy Construction Machinery", "Recreational Vehicles"}
CAPTIVE_SHARE = 0.75


def _score_up(v, edges, worst_at=1.5):
    """A ratio where HIGHER is worse (debt/EBITDA): edges b1..b5 -> scores 1.5..5.5; 1 at zero, 6 at b5 x worst_at."""
    pts = [(0.0, 1.0)] + [(b, 1.5 + k) for k, b in enumerate(edges)] + [(edges[-1] * worst_at, 6.0)]
    return interp(v, pts)


def _score_down(v, edges):
    """A ratio where HIGHER is better (FFO/debt %, EBITDA/interest x): edges b1 > .. > b5 -> scores 1.5..5.5."""
    pts = [(edges[-1] - (edges[-2] - edges[-1]), 6.0)] + [(b, 5.5 - k) for k, b in enumerate(reversed(edges))] \
        + [(edges[0] * 2, 1.0)]
    return interp(v, pts)


def bilinear(grid, r, c):
    """grid[row][col] at continuous 1-based (r, c)."""
    r, c = clip(r, 1, 6) - 1, clip(c, 1, 6) - 1
    r0, c0 = int(math.floor(r)), int(math.floor(c))
    r1, c1 = min(5, r0 + 1), min(5, c0 + 1)
    fr, fc = r - r0, c - c0
    top = grid[r0][c0] * (1 - fc) + grid[r0][c1] * fc
    bot = grid[r1][c0] * (1 - fc) + grid[r1][c1] * fc
    return top * (1 - fr) + bot * fr


def method_of(sector, industry, total_assets_usd=None, revenue_usd=None):
    """corporate | bank | insurer | shell - which scorecard. Lenders inside 'Credit Services' / 'Capital Markets' /
    'Mortgage Finance' / mortgage REITs carry assets many times their revenue: those are judged as banks."""
    ind = str(industry or "")
    sec = str(sector or "")
    if ind == "Shell Companies":
        return "shell"
    if ind.startswith("Insurance") and ind != "Insurance Brokers":
        return "insurer"
    if ind.startswith("Banks"):
        return "bank"
    if sec in ("Financial Services", "Real Estate") and ind in ("Credit Services", "Capital Markets", "Mortgage Finance",
                                                               "REIT - Mortgage", "Financial Conglomerates"):
        a, r = finite(total_assets_usd), finite(revenue_usd)
        if ind in ("Mortgage Finance", "REIT - Mortgage") or (a and r and r > 0 and a / r > 4.0):
            return "bank"
    return "corporate"


def volatility_table(sector, industry):
    ind = str(industry or "")
    if ind in LOW_VOL:
        return "low"
    if str(sector or "") == "Real Estate" and ind.startswith("REIT"):
        return "reit"
    if ind in MEDIAL:
        return "medial"
    return "standard"


def stability_score(oi_vol=None, loss_years=None, n_years=None):
    """Volatility of profitability (S&P adjusts the competitive position for it): 1 steady .. 6 erratic, from the
    spread of year-on-year operating-income changes and the loss years in up to ten annual reports."""
    v = finite(oi_vol)
    if v is None or not n_years or n_years < 4:
        return None
    s = interp(v, [(0.08, 1.0), (0.15, 2.0), (0.25, 3.0), (0.40, 4.0), (0.65, 5.0), (1.0, 6.0)])
    return clip(s + 0.6 * (loss_years or 0) * 10.0 / max(n_years, 1), 1.0, 6.0)


def business_risk(sector, industry, revenue_usd, ebitda_margin, stability=None, country_risk=None):
    """1 (excellent) .. 6 (vulnerable) via S&P's matrix: competitive position (scale 55 %, profitability 30 %,
    stability 15 %) x industry risk (raised to the country's risk outside strong sovereigns)."""
    ind = INDUSTRY_RISK.get(str(industry or ""), SECTOR_RISK.get(str(sector or ""), 3.5))
    cicra = max(ind, country_risk) if country_risk else ind
    rev = finite(revenue_usd)
    size = None
    if rev and rev > 0:
        size = interp(math.log10(rev), [(math.log10(3e8), 6.0), (9.0, 5.0), (math.log10(3e9), 4.0), (10.0, 3.0),
                                         (math.log10(3e10), 2.0), (11.0, 1.0)])
    m = finite(ebitda_margin)
    prof = interp(m, [(0.0, 6.0), (0.06, 5.0), (0.12, 4.0), (0.20, 3.0), (0.30, 2.0), (0.40, 1.0)]) if m is not None else None
    stab = finite(stability)
    parts = ([(size, 0.55)] if size is not None else []) + ([(prof, 0.30)] if prof is not None else []) \
        + ([(stab, 0.15)] if stab is not None else [])
    cp = sum(v * k for v, k in parts) / sum(k for _, k in parts) if parts else 4.0
    return bilinear(BUSINESS, cp, cicra), {"industry": ind, "cicra": round(cicra, 2), "position": round(cp, 2),
                                           "scale": None if size is None else round(size, 2),
                                           "profit": None if prof is None else round(prof, 2),
                                           "stability": None if stab is None else round(stab, 2)}


def captive_debt_share(industry, debt, revenue):
    """The share of total debt that belongs to a captive finance arm (0 when the group has none to speak of)."""
    d, r = finite(debt), finite(revenue)
    if str(industry or "") in CAPTIVE and d and r and r > 0 and d / r > 0.5:
        return CAPTIVE_SHARE
    return 0.0


def corporate_scorecard(f):
    """f: {sector, industry, revenue, ebitda, debt, cash, fcf, cfo, capex, coupon (avg %), currentRatio, stability,
    countryRisk} - USD where money. -> (continuous notch, detail) or (None, reason)."""
    rev, ebitda, debt = finite(f.get("revenue")), finite(f.get("ebitda")), finite(f.get("debt"))
    cash, cpn = finite(f.get("cash")) or 0.0, finite(f.get("coupon"))
    cfo, capex = finite(f.get("cfo")), finite(f.get("capex"))
    fcf = (cfo - abs(capex)) if (cfo is not None and capex is not None) else finite(f.get("fcf"))
    if rev is None and ebitda is None:
        return None, "no revenue / EBITDA"
    table = volatility_table(f.get("sector"), f.get("industry"))
    margin = (ebitda / rev) if (ebitda is not None and rev and rev > 0) else None
    bus, bus_d = business_risk(f.get("sector"), f.get("industry"), rev, margin, f.get("stability"), f.get("countryRisk"))
    det = {"table": table, "business": round(bus, 2), "bus": bus_d}
    cap = captive_debt_share(f.get("industry"), debt, rev)
    if cap and debt:
        debt = debt * (1 - cap)
        det["captive"] = f"{cap:.0%} of debt treated as the finance arm's (receivable-backed)"
    # the cash-burn case: no EBITDA and no free cash flow - rated on its runway (S&P's approach for pre-profit biotech)
    if (ebitda is None or ebitda <= 0) and (fcf is not None and fcf < 0):
        runway = cash / -fcf
        n = 18.0 if runway < 1 else 17.0 if runway < 2 else 16.0 if runway < 3 else 15.0
        det.update({"financial": 6.0, "runwayYears": round(runway, 1), "rule": "cash burn"})
        return n, det
    W = RATIO_W[table]
    scores = []
    if debt is not None and debt >= 0:
        net = max(0.0, debt - cash)
        interest = (cpn / 100.0 if (cpn and cpn > 0) else 0.05) * debt
        if ebitda is not None:
            if ebitda > 0:
                lev = net / ebitda
                det["netDebtToEbitda"] = round(lev, 2)
                scores.append((_score_up(lev, LEV_PTS[table]), W["lev"]))
            elif net > 0:
                scores.append((6.0, W["lev"]))
            else:
                scores.append((3.5, W["lev"] * 0.5))        # no net debt, but no EBITDA either: middling
        if "ffo" in W and ebitda is not None:
            ffo_e = max(0.0, ebitda - interest) * 0.85 if ebitda > 0 else ebitda
            ffo = (ffo_e + cfo) / 2.0 if cfo is not None else ffo_e
            if net > 0:
                ffo_d = ffo / net * 100
                det["ffoToDebtPct"] = round(ffo_d, 1)
                scores.append((_score_down(ffo_d, FFO_PTS[table]) if ffo_d > 0 else 6.0, W["ffo"]))
            else:
                scores.append((1.0 if ffo > 0 else 4.0, W["ffo"]))
        if "focf" in W and fcf is not None:
            if debt > 0:
                focf = fcf / debt * 100
                det["focfToDebtPct"] = round(focf, 1)
                scores.append((_score_down(focf, FOCF_PTS["standard"]) if focf > -50 else 6.0, W["focf"]))
            else:
                scores.append((1.0 if fcf > 0 else 4.0, W["focf"] * 0.5))
        if "cov" in W and cpn is not None and cpn > 0 and debt > 0 and ebitda is not None:
            cov = ebitda / interest if interest > 0 else None
            if cov is not None:
                det["ebitdaToInterest"] = round(cov, 1)
                scores.append((_score_down(cov, COV_PTS[table]) if cov > 0 else 6.0, W["cov"]))
    if not scores:
        return None, "no debt / cash-flow figures"
    fin = sum(s * w for s, w in scores) / sum(w for _, w in scores)
    det["financial"] = round(fin, 2)
    det["ratios"] = len(scores)
    n = bilinear(ANCHOR, bus, fin)
    cr = finite(f.get("currentRatio"))
    if table == "standard" and cr is not None and cr < 0.8 and fcf is not None and fcf < 0:
        n += 1.0                                         # thin liquidity while burning cash
        det["liquidity"] = "current ratio < 0.8 and negative FCF: -1 notch"
    if rev is not None and rev < 1e8:
        n = max(n, 14.0)                                 # sub-$100M revenue: scale caps it at B+
    elif rev is not None and rev < 5e8:
        n = max(n, 12.0)
    return clip(n, 1.0, 21.0), det


def bank_scorecard(f, sov_n=None):
    """Banks / lenders: S&P's bank anchor (US 'bbb+') adjusted for capital (equity / assets), earnings (ROA) and
    scale (assets). Outside the US the sovereign bounds the anchor."""
    A, Eq, roa = finite(f.get("assets")), finite(f.get("equity")), finite(f.get("roa"))
    if not A or A <= 0 or Eq is None:
        return None, "no assets / equity"
    anchor = 8.0 if (sov_n is None or sov_n <= 4) else float(sov_n)
    cap = Eq / A
    adj_cap = interp(cap, [(0.03, 2.0), (0.05, 1.0), (0.08, 0.0), (0.12, 0.0), (0.16, -1.0)])
    adj_roa = 0.0 if roa is None else interp(roa, [(-0.01, 2.0), (0.0, 1.0), (0.006, 0.0), (0.012, 0.0), (0.02, -1.0)])
    adj_size = interp(math.log10(A), [(10.0, 2.0), (10.7, 1.0), (11.7, 0.0), (12.4, -1.0), (12.7, -2.0)])
    n = anchor + adj_cap + adj_roa + adj_size
    if sov_n is not None:
        n = max(n, sov_n - 1.0)
    return clip(n, 1.0, 21.0), {"method": "bank", "anchor": anchor, "equityToAssets": round(cap, 4),
                                "roa": None if roa is None else round(roa, 4), "assetsUSD": round(A),
                                "adj": {"capital": round(adj_cap, 2), "earnings": round(adj_roa, 2), "scale": round(adj_size, 2)}}


def insurer_scorecard(f, sov_n=None):
    """Insurers: anchor 'a-' (S&P's US insurance sector), capital (equity / assets - life and P&C apart), ROA, scale."""
    A, Eq, roa = finite(f.get("assets")), finite(f.get("equity")), finite(f.get("roa"))
    if not A or A <= 0 or Eq is None:
        return None, "no assets / equity"
    life = "Life" in str(f.get("industry") or "")
    anchor = 7.0 if (sov_n is None or sov_n <= 4) else float(sov_n)
    cap = Eq / A
    adj_cap = interp(cap, [(0.02, 2.0), (0.04, 1.0), (0.06, 0.0), (0.10, 0.0), (0.14, -1.0)]) if life else \
        interp(cap, [(0.08, 2.0), (0.14, 1.0), (0.20, 0.0), (0.30, 0.0), (0.40, -1.0)])
    adj_roa = 0.0 if roa is None else (interp(roa, [(-0.005, 2.0), (0.0, 1.0), (0.003, 0.0), (0.008, 0.0), (0.015, -1.0)]) if life else
                                       interp(roa, [(-0.01, 2.0), (0.0, 1.0), (0.01, 0.0), (0.03, 0.0), (0.05, -1.0)]))
    adj_size = interp(math.log10(A), [(9.5, 2.0), (10.3, 1.0), (11.0, 0.0), (11.7, -1.0)])
    n = anchor + adj_cap + adj_roa + adj_size
    if sov_n is not None:
        n = max(n, sov_n - 1.0)
    return clip(n, 1.0, 21.0), {"method": "insurer", "anchor": anchor, "equityToAssets": round(cap, 4),
                                "roa": None if roa is None else round(roa, 4), "assetsUSD": round(A),
                                "adj": {"capital": round(adj_cap, 2), "earnings": round(adj_roa, 2), "scale": round(adj_size, 2)}}


# ------------------------------------------------------------------------------- composite ----
# Pillar weights: fitted on the same 143 names (mean absolute gap to the agencies 1.04 notches, no bias). CDS adds
# noise to the level (the fit put it near zero), so it weighs 0.10 x its liquidity - a market cross-check for the
# names it prices far out (ORCL, KSS, AAL) - and drives the credit TREND instead.
W_CDS, W_EQ, W_FUND = 0.10, 0.35, 0.40


def composite(cds=None, cds_liq=0.0, eq=None, eq_quality=1.0, fund=None, fund_quality=1.0, agency=None):
    """Weighted continuous notch. cds / eq / fund are continuous notches (None = missing). Returns
    (notch, weights, flags). An agency rating, when given, IS the rating (the model stays alongside)."""
    flags = []
    if agency is not None:
        return float(agency), {"agency": 1.0}, ["agency"]
    w = {}
    if cds is not None and cds_liq > 0:
        w["cds"] = W_CDS * cds_liq
    if eq is not None and eq_quality > 0:
        w["eq"] = W_EQ * eq_quality
    if fund is not None and fund_quality > 0:
        w["fund"] = W_FUND * fund_quality
    vals = {"cds": cds, "eq": eq, "fund": fund}
    if "cds" in w:
        model = [(vals[k], w[k]) for k in ("eq", "fund") if k in w]
        if model:
            m = sum(v * k for v, k in model) / sum(k for _, k in model)
            if abs(cds - m) > 5 and cds_liq < 0.6:
                w["cds"] *= 0.25                         # a thin print far from everything else: trust it less
                flags.append("cds-outlier")
    tot = sum(w.values())
    if tot < 0.12:
        return None, {}, flags + ["insufficient-data"]
    n = sum(vals[k] * w[k] for k in w) / tot
    return clip(n, 1.0, 22.0), {k: round(v / tot, 3) for k, v in w.items()}, flags


def publish(cont, prev):
    """The published integer notch: the model's rounded notch, unless it is within HYSTERESIS of the last one."""
    if cont is None:
        return None
    if prev is not None and abs(cont - prev) < HYSTERESIS:
        return int(prev)
    return int(clip(int(math.floor(cont + 0.5)), 1, 22))


# --------------------------------------------------------------------------- trend & signal ----
def trend_score(cds_rel_1m=None, cds_liq=0.0, mig30=None, iv_rel=None):
    """-1 (deteriorating fast) .. +1 (improving fast), or None. cds_rel_1m: the 5-year spread's 1-month change
    relative to its level a month ago (+0.3 = 30 % wider); mig30: published notch change over ~30 days (+ =
    downgrade); iv_rel: the long-dated implied volatility's 20-session change relative to it, LESS the market's
    (an idiosyncratic rise in option-implied risk leads credit - Cremers et al. 2008)."""
    parts = []
    r = finite(cds_rel_1m)
    if r is not None and cds_liq > 0:
        parts.append((clip(-r / 0.30, -1, 1), 0.5 * cds_liq))
    m = finite(mig30)
    if m is not None:
        parts.append((clip(-m / 3.0, -1, 1), 0.3))
    v = finite(iv_rel)
    if v is not None:
        parts.append((clip(-v / 0.40, -1, 1), 0.2))
    w = sum(k for _, k in parts)
    if w <= 0:
        return None
    return round(sum(s * k for s, k in parts) / w, 3)


def level_score(n):
    """+1 at AAA, 0 at BBB, -1 at CCC and worse."""
    return None if n is None else round(clip((9.0 - float(n)) / 8.0, -1.0, 1.0), 3)


def credit_signal(n, trend):
    """The bot's 'credit' component for a company: 30 % rating level (alone it can never reach the 0.33 trigger
    bar - a rating is not a timing signal) + 70 % credit trend (CDS / implied-vol / migration lead equity)."""
    lv = level_score(n)
    if lv is None and trend is None:
        return None
    return round(clip(0.30 * (lv or 0.0) + 0.70 * (trend or 0.0), -1.0, 1.0), 3)


def outlook(trend, mig30=None):
    if mig30 is not None and abs(mig30) >= 2:
        return "watch-neg" if mig30 > 0 else "watch-pos"
    if trend is None:
        return "stable"
    return "negative" if trend <= -0.25 else "positive" if trend >= 0.25 else "stable"


# -------------------------------------------------------------------------- look-through ----
def look_through(members, ratings):
    """members [(ticker, weight)], ratings {T: {n, tr}} -> the basket's WARF-implied notch, linear average, coverage
    (weight share rated), investment-grade share, weight-averaged trend. None if < 30 % of the weight is rated."""
    tot = sum(max(0.0, w or 0.0) for _, w in members)
    if tot <= 0:
        return None
    rated = [(t, w / tot, ratings[t]) for t, w in members if (w or 0) > 0 and t in ratings and ratings[t].get("n") is not None]
    cov = sum(w for _, w, _ in rated)
    if cov < 0.30:
        return {"coverage": round(cov, 3), "n": None}
    warf = sum(w * warf_of(r["n"]) for _, w, r in rated) / cov
    avg = sum(w * r["n"] for _, w, r in rated) / cov
    ig = sum(w for _, w, r in rated if r["n"] <= IG_MAX) / cov
    tw = [(w, r["tr"]) for _, w, r in rated if r.get("tr") is not None]
    trw = sum(w for w, _ in tw)
    top = sorted(rated, key=lambda x: -x[1])[:5]
    return {"n": notch_of_warf(warf), "warf": round(warf), "avg": round(avg, 2), "coverage": round(cov, 3),
            "ig": round(ig, 3), "members": len(members), "rated": len(rated),
            "tr": round(sum(w * t for w, t in tw) / trw, 3) if trw >= 0.2 * cov else None,
            "top": [[t, round(w, 4), letter(r["n"])] for t, w, r in top]}


def sovereign_blend(weights, sov):
    """{country: weight} -> WARF-implied notch over the countries that have a rating (None if < 50 % rated)."""
    tot = sum(max(0.0, w) for w in weights.values())
    rated = [(c, w / tot) for c, w in weights.items() if tot > 0 and w > 0 and sov.get(c) is not None]
    cov = sum(w for _, w in rated)
    if cov < 0.5:
        return None, cov
    warf = sum(w * warf_of(sov[c]) for c, w in rated) / cov
    return notch_of_warf(warf), cov


_FX_RE = re.compile(r"^([A-Z]{3})([A-Z]{3})?=X$")


def fx_legs(ticker):
    """Yahoo FX tickers -> (base, quote): 'EURUSD=X' -> (EUR, USD); 'JPY=X' quotes yen per dollar -> (USD, JPY).
    Long the pair = long the base currency, short the quote."""
    m = _FX_RE.match(str(ticker or "").upper())
    if not m:
        return None
    a, b = m.group(1), m.group(2)
    return (a, b) if b else ("USD", a)
