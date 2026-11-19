#!/usr/bin/env python3
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-CREDIT
#  >>> FILE PATH:   pipeline/build_ratings.py
#  z194 - CREDIT RATINGS for everything the books hold  -> data/ratings.json
#    companies  : model-implied rating (rating_model.py): CDS-implied x distance-to-default x S&P-style
#                 fundamentals (banks / insurers: their own scorecards); an agency rating in
#                 config/agency_ratings.json replaces the model; bankruptcy (8-K item 1.03, a ...Q ticker) = D
#    funds      : look-through - the S&P 500 fund is its ~500 members' ratings by index weight, combined by
#                 Moody's WARF; sector funds their sector's members; leveraged / inverse products their
#                 underlying (the bot signs the exposure); single-stock products that stock; country funds and
#                 foreign indices their sovereign; Treasury funds / futures the US; bond funds their sleeve;
#                 ETNs no better than their issuing bank
#    futures    : equity-index futures = the index's look-through, Treasury futures = the US; commodities NR
#    FX         : each currency's sovereign (long the pair = long the base)
#    NR         : commodities, crypto, volatility, private companies, shells - no issuer to rate
#  Each record carries a credit TREND (CDS 1-month change, option-implied volatility vs the market, rating
#  migration) and the bot's 'credit' signal. Also writes data/ratings/history/<date>.json (notches, for
#  migration) and data/ratings/_vol.json (equity volatility cache - 1/5 of the histories re-read a day).
#  Run after build_credit_index.py (credit.yml). Inputs: the five books' master.json + price history,
#  TRAPP2-1 FX rates and FRED OAS by rating, TRAPP2-ANALYTICS index members + SEC 8-K index,
#  TRAPP2-OPTIONS implied-volatility surfaces, this repo's CDS tape and bond lists.
# ============================================================
import json
import math
import os
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from credit_common import DATA, RAW, ROOT, http, log, now_iso, read, write
import rating_model as rm

BOOKS = ("TRAPP2", "TRAPP2-1", "TRAPP2-2", "TRAPP2-3", "TRAPP2-4")
CFG = ROOT / "config"
OUT = DATA / "ratings.json"
RDIR = DATA / "ratings"
HIST = RDIR / "history"
VOLC = RDIR / "_vol.json"
SECC = RDIR / "_sec.json"
SEC_REFRESH_DAYS = int(os.environ.get("RATINGS_SEC_REFRESH_DAYS") or 20)
SEC_MAX_FETCH = int(os.environ.get("RATINGS_SEC_MAX_FETCH") or 3000)
VOL_REFRESH_DAYS = int(os.environ.get("RATINGS_VOL_REFRESH_DAYS") or 5)
VOL_MAX_FETCH = int(os.environ.get("RATINGS_VOL_MAX_FETCH") or 4000)
THREADS = int(os.environ.get("RATINGS_THREADS") or 12)
TODAY = (os.environ.get("RATINGS_TODAY") or datetime.now(timezone.utc).strftime("%Y-%m-%d"))[:10]
IV_STRESS = 1.25          # implied vol this far above realized replaces it in the structural model
# Registered here, operating elsewhere (shipping, cruise, reinsurance, US territories): no sovereign ceiling
NO_CEILING = {"Bermuda", "Cayman Islands", "British Virgin Islands", "Jersey", "Guernsey", "Isle of Man", "Marshall Islands",
              "Panama", "Puerto Rico", "Bahamas", "Curacao", "Gibraltar", "Liberia", "Monaco", "Cyprus", "Malta", ""}
_SUBUNIT = {"USX": ("USD", 0.01), "USd": ("USD", 0.01), "GBp": ("GBP", 0.01), "GBX": ("GBP", 0.01),
            "ZAc": ("ZAR", 0.01), "ZAC": ("ZAR", 0.01), "ILA": ("ILS", 0.01), "ILa": ("ILS", 0.01)}
NASDAQ = {"NMS", "NGM", "NCM", "NAS", "NIM"}


def fetch_json(url, timeout=60, tries=2):
    for _ in range(tries):
        try:
            return json.loads(http(url, timeout=timeout).decode())
        except Exception:
            continue
    return None


def f(v):
    return rm.finite(v)


def _filled(r):
    return sum(1 for v in r.values() if v not in (None, "", "None", "nan"))


# ------------------------------------------------------------------------------------ inputs ----
def load_books():
    rows = {}
    for b in BOOKS:
        d = fetch_json(f"{RAW}/{b}/main/data/master.json", timeout=120)
        rs = d if isinstance(d, list) else ((d or {}).get("rows") or [])
        n = 0
        for r in rs:
            t = str(r.get("ticker") or "").upper().strip()
            if not t:
                continue
            r = dict(r)
            r["_book"] = b
            old = rows.get(t)
            if old is None or _filled(r) > _filled(old):
                rows[t] = r
            n += 1
        log("ratings", f"{b}: {n} rows")
    return rows


def load_fx():
    d = fetch_json(f"{RAW}/TRAPP2-1/main/data/fx/rates.json") or {}
    out = {"USD": 1.0}
    for ccy, v in (d.get("rates") or {}).items():
        up = f(v.get("usdPer")) if isinstance(v, dict) else None
        if up and up > 0:
            out[str(ccy).upper()] = up
    return out


def main_ccy_rate(ccy, fx):
    """USD per one MAIN unit of the currency (market caps and statements are in main units; z187)."""
    c = str(ccy or "USD").strip() or "USD"
    return fx.get(_SUBUNIT.get(c, (c.upper(), 1.0))[0])


def load_oas():
    """Current OAS (bp) of each rating bucket + the IG / HY / EM indices' level and 1-month change."""
    now, mkt = {}, {}

    def series(sid):
        d = fetch_json(f"{RAW}/TRAPP2-1/main/data/macro/{sid}.json", timeout=30) or {}
        out = []
        for o in d.get("observations") or []:
            v = f(o.get("value"))
            if o.get("date") and v is not None:
                out.append((o["date"], v * 100))
        return sorted(out)
    for k, sid, _ in rm.BUCKET_SERIES:
        s = series(sid)
        if s:
            now[k] = round(s[-1][1], 1)
    for k, sid in (("ig", "BAMLC0A0CM"), ("hy", "BAMLH0A0HYM2"), ("em", "BAMLEMCBPIOAS"), ("ccc", "BAMLH0A3HYC")):
        s = series(sid)
        if len(s) < 25:
            continue
        lim = (date.fromisoformat(s[-1][0]) - timedelta(days=30)).isoformat()
        then = [v for d, v in s if d <= lim]
        then = then[-1] if then else None
        rel = (s[-1][1] / then - 1) if then else None
        mkt[k] = {"date": s[-1][0], "oas": round(s[-1][1]), "chg1m": None if then is None else round(s[-1][1] - then),
                  "tr": rm.trend_score(cds_rel_1m=rel, cds_liq=1.0) if rel is not None else None}
    return now, mkt


def load_members():
    u = fetch_json(f"{RAW}/TRAPP2-ANALYTICS/main/data/earnings/universe.json", timeout=60) or {}
    out = {}
    for k, v in (u.get("indices") or {}).items():
        out[k] = [(str(m.get("ticker") or "").upper(), f(m.get("weight")) or 0.0) for m in (v or {}).get("members") or [] if m.get("ticker")]
    return out


def load_sec():
    d = fetch_json(f"{RAW}/TRAPP2-ANALYTICS/main/data/sec/index.json", timeout=60) or {}
    return d.get("tickers") or {}


# --------------------------------------------------------------------- equity volatility ----
def realized_vol(closes):
    """Annualized volatility of daily log returns over the last year (and 3 months). A split day (a >= 1.9x
    move either way that the file didn't adjust) is dropped, not counted as volatility."""
    px = [c for c in closes if c and c > 0]
    if len(px) < 60:
        return None, None
    r = [math.log(b / a) for a, b in zip(px, px[1:])]
    cut = math.log(1.9)
    r = [x for x in r if abs(x) < cut]
    y, q = r[-252:], r[-63:]
    vy = statistics.pstdev(y) * math.sqrt(252) if len(y) >= 120 else None
    vq = statistics.pstdev(q) * math.sqrt(252) if len(q) >= 40 else None
    return vy, vq


def refresh_vols(rows, tickers, cache):
    """Re-read the price history of names whose cached volatility is missing or older than VOL_REFRESH_DAYS
    (oldest first, at most VOL_MAX_FETCH)."""
    def age(t):
        d = (cache.get(t) or {}).get("d")
        if not d:
            return 10 ** 6
        try:
            return (date.fromisoformat(TODAY) - date.fromisoformat(d)).days
        except ValueError:
            return 10 ** 6
    due = sorted([t for t in tickers if age(t) >= VOL_REFRESH_DAYS], key=lambda t: -age(t))[:VOL_MAX_FETCH]

    def one(t):
        b = rows[t].get("_book") or "TRAPP2"
        d = fetch_json(f"{RAW}/{b}/main/data/history/{quote(t)}.json", timeout=60)
        if not isinstance(d, list) or len(d) < 60:
            return t, None
        closes = [f(x.get("close")) for x in d[-300:] if isinstance(x, dict)]
        vy, vq = realized_vol(closes)
        last = d[-1].get("date") if isinstance(d[-1], dict) else None
        return t, {"v": None if vy is None else round(vy, 4), "v3m": None if vq is None else round(vq, 4), "d": TODAY, "last": last}
    ok = 0
    with ThreadPoolExecutor(THREADS) as ex:
        for t, v in ex.map(one, due):
            if v:
                cache[t] = v
                ok += 1
            elif t in cache:
                cache[t]["d"] = TODAY            # try again on the normal cycle, keep the last good value
    log("ratings", f"equity volatility: {ok} of {len(due)} histories re-read ({len(cache)} cached)")
    return cache


def load_iv(tickers):
    """Long-dated (365-day, else 180-day) at-the-money implied vol now and ~20 sessions ago, per optionable name."""
    man = fetch_json(f"{RAW}/TRAPP2-OPTIONS/main/data/options/manifest.json", timeout=60) or {}
    have = {str(t).upper() for t in man.get("tickers") or []}
    want = sorted((set(tickers) & have) | ({"SPY"} & have))

    def one(t):
        s = fetch_json(f"{RAW}/TRAPP2-OPTIONS/main/data/surfaces/{quote(t)}.json", timeout=45)
        if not isinstance(s, dict) or not s.get("snapshots"):
            return t, None
        try:
            mi = s["moneyness"].index(1.0)
        except (ValueError, KeyError, AttributeError):
            return t, None
        ten = s.get("tenorsDays") or []

        def atm(snap):
            iv = snap.get("iv") or []
            for days in (365, 180):
                if days in ten:
                    row = iv[ten.index(days)] if ten.index(days) < len(iv) else None
                    v = f(row[mi]) if row and mi < len(row) else None
                    if v and v > 0:
                        return v / 100.0, days
            return None, None
        snaps = s["snapshots"]
        now, days = atm(snaps[-1])
        then = None
        if len(snaps) >= 15:
            then, d2 = atm(snaps[max(0, len(snaps) - 21)])
            if d2 != days:
                then = None
        return t, {"iv": now, "iv20": then, "tenor": days, "date": snaps[-1].get("date")}
    out = {}
    with ThreadPoolExecutor(THREADS) as ex:
        for t, v in ex.map(one, want):
            if v and v.get("iv"):
                out[t] = v
    log("ratings", f"implied volatility: {len(out)} of {len(want)} optionable names")
    return out


# ---------------------------------------------------------------- SEC annual statements ----
def sec_metrics(doc):
    """From TRAPP2-ANALYTICS data/sec/<T>.json (XBRL company facts): the latest annual operating cash flow and
    capex (FOCF = CFO - capex, S&P's definition), long-term debt (the default point's split), and the volatility of
    operating income over up to ten years (year-on-year changes' spread, loss years)."""
    fa = (doc or {}).get("facts") or {}

    def series(k):
        by = {}
        for x in ((fa.get(k) or {}).get("annual") or []):
            e, v = x.get("end"), f(x.get("val"))
            if e and v is not None and (e not in by or (x.get("filed") or "") >= by[e][1]):
                by[e] = (v, x.get("filed") or "")
        return [(e, v) for e, (v, _) in sorted(by.items())]
    out = {}
    oi = series("operatingIncome")[-10:]
    if len(oi) >= 4:
        ch = [rm.clip((b - a) / abs(a), -2.0, 2.0) for (_, a), (_, b) in zip(oi, oi[1:]) if a]
        if len(ch) >= 3:
            out.update({"oiVol": round(statistics.pstdev(ch), 3), "loss": sum(1 for _, v in oi if v < 0), "n": len(oi)})
    cfo, cap, ltd = series("operatingCashFlow"), series("capex"), series("longTermDebt")
    if cfo:
        out.update({"cfo": cfo[-1][1], "cfoEnd": cfo[-1][0]})
        if cap and cap[-1][0] == cfo[-1][0]:
            out["capex"] = cap[-1][1]
    if ltd:
        out.update({"ltd": ltd[-1][1], "ltdEnd": ltd[-1][0]})
    return out


def refresh_sec(tickers, cache):
    """Re-read the SEC facts of names not refreshed in SEC_REFRESH_DAYS (annual figures move once a year)."""
    def age(t):
        d = (cache.get(t) or {}).get("d")
        try:
            return (date.fromisoformat(TODAY) - date.fromisoformat(d)).days if d else 10 ** 6
        except ValueError:
            return 10 ** 6
    due = sorted([t for t in tickers if age(t) >= SEC_REFRESH_DAYS], key=lambda t: -age(t))[:SEC_MAX_FETCH]

    def one(t):
        d = fetch_json(f"{RAW}/TRAPP2-ANALYTICS/main/data/sec/{quote(t)}.json", timeout=45, tries=2)
        return t, (dict(sec_metrics(d), d=TODAY) if isinstance(d, dict) else None)
    retry = (date.fromisoformat(TODAY) - timedelta(days=max(0, SEC_REFRESH_DAYS - 3))).isoformat()
    ok = 0
    with ThreadPoolExecutor(THREADS) as ex:
        for t, v in ex.map(one, due):
            if v is not None:
                cache[t] = v
                ok += 1
            elif t not in cache or cache[t].get("none"):
                cache[t] = {"d": retry, "none": 1}       # no file (or a failed read): ask again in 3 days
            # a failed read of a name already on file keeps its figures and is asked again tomorrow
    log("ratings", f"SEC statements: {ok} of {len(due)} read ({sum(1 for v in cache.values() if not v.get('none'))} on file)")
    return cache


def fresh_annual(end, months=20):
    try:
        return (date.fromisoformat(TODAY) - date.fromisoformat(str(end)[:10])).days <= months * 31
    except ValueError:
        return False


# --------------------------------------------------------------------------- CDS tape ----
def cds_view(t, latest):
    """Robust 5-year level now and a month ago from the issuer's trades - the median TRADE (a busy day counts for
    its trades, not as one point), units normalized (the tape mixes basis points and percentages) - with liquidity.
    None when the name has no CDS file."""
    li = (latest.get("issuers") or {}).get(t)
    if not li:
        return None
    doc = read(DATA / "cds" / "issuers" / f"{t}.json", {}) or {}
    pts = []
    for x in doc.get("trades") or []:
        b = rm.norm_bps(x.get("bps"))
        ten = f(x.get("tenor"))
        if b is None or ten is None or not (4.0 <= ten <= 6.0) or b >= 5000 or not x.get("date"):
            continue
        try:
            pts.append((date.fromisoformat(str(x["date"])[:10]), b))
        except ValueError:
            continue
    if not pts:
        return {"bp": None, "liq": 0.0, "n30": li.get("n30"), "last": li.get("lastDate"), "entity": li.get("entity")}
    lastd = max(d for d, _ in pts)
    recent = [b for d, b in pts if d >= lastd - timedelta(days=7)]
    month = [b for d, b in pts if lastd - timedelta(days=37) <= d <= lastd - timedelta(days=23)]
    now = statistics.median(recent)
    then = statistics.median(month) if month else None
    last = lastd.isoformat()
    return {"bp": round(now, 1), "bp1m": None if then is None else round(then, 1),
            "rel": None if not then else round(now / then - 1, 4), "last": last, "n30": li.get("n30"),
            "liq": rm.cds_liquidity(li.get("n30"), last, TODAY), "entity": li.get("entity")}


# ------------------------------------------------------------------------- companies ----
def sov_lookup(sov_cfg):
    rat = {k: rm.notch(v) for k, v in (sov_cfg.get("ratings") or {}).items()}
    alias = sov_cfg.get("aliases") or {}
    for comp, ws in (sov_cfg.get("composites") or {}).items():
        if isinstance(ws, dict) and not comp.startswith("_"):
            n, _ = rm.sovereign_blend({k: v for k, v in ws.items() if isinstance(v, (int, float))}, rat)
            rat[comp] = n

    def get(country):
        c = alias.get(str(country or "").strip(), str(country or "").strip())
        return rat.get(c)
    return get, rat


def kind_of(t, r):
    ac = str(r.get("asset_class") or "").strip()
    if ac == "Equity" and str(r.get("isetf")).lower() != "true" and str(r.get("isfund")).lower() != "true":
        return "co"
    if ac == "Equity":
        return "etf"
    return {"Index": "idx", "Future": "fut", "FX": "fx", "Crypto": "nr", "Private": "nr", "Option": "opt"}.get(ac, "nr")


FUND_KEYS = (("table", "t"), ("business", "b"), ("financial", "f"), ("netDebtToEbitda", "lev"), ("ffoToDebtPct", "ffo"),
             ("focfToDebtPct", "focf"), ("ebitdaToInterest", "cov"), ("runwayYears", "runway"), ("rule", "rule"),
             ("captive", "captive"), ("liquidity", "liq"), ("method", "m"), ("anchor", "anchor"),
             ("equityToAssets", "eqa"), ("roa", "roa"), ("assetsUSD", "assets"))


def compact_fund(det):
    """The scorecard detail, short keys (ratings.json is read by the app and the bot every pass)."""
    if not isinstance(det, dict):
        return {"why": det} if det else None
    out = {short: det[k] for k, short in FUND_KEYS if det.get(k) is not None}
    b = det.get("bus") or {}
    if b:
        out["bus"] = [b.get("industry"), b.get("position"), b.get("scale"), b.get("profit"), b.get("stability")]
    if det.get("adj"):
        out["adj"] = det["adj"]
    if out.get("assets"):
        out["assets"] = round(out["assets"] / 1e9, 1)
    return out


def rate_company(t, r, fx, ctx):
    """One company -> its rating record (the published notch is set later, against yesterday's)."""
    sec = ctx["sec"].get(t) or {}
    country = str(r.get("country") or "").strip()
    sovn = ctx["sov"](country) if country and country != "United States" else None
    qf = main_ccy_rate(r.get("currency"), fx)
    ff = main_ccy_rate(r.get("financialCurrency") or r.get("currency"), fx)

    def usd(v, k):
        v = f(v)
        return None if v is None or k is None else v * k
    sector, industry = r.get("sector") or "", r.get("industry") or ""
    rec = {"k": "co", "name": r.get("name") or t}
    flags = []
    # bankruptcy: an 8-K item 1.03 in 30 days, or a fifth-letter-Q (in bankruptcy) ticker
    if "1.03" in (sec.get("red30") or []) or re.fullmatch(r"[A-Z]{4}Q", t):
        rec.update({"x": 22.0, "src": "event", "fl": ["bankruptcy"], "why": "8-K item 1.03 (bankruptcy) or a ...Q ticker"})
        return rec
    if industry == "Shell Companies":
        rec.update({"x": None, "src": "nr", "why": "shell company - no operations to rate"})
        return rec
    mcap, debt = usd(r.get("marketcap"), qf), usd(r.get("totalDebt"), ff)
    rev, ebitda = usd(r.get("revenue"), ff), usd(r.get("ebitda"), ff)
    assets = usd(r.get("totalAssets"), ff)
    method = rm.method_of(sector, industry, assets, rev)
    # ---- pillar 1: CDS
    cv = cds_view(t, ctx["cds"])
    n_cds = rm.cds_notch(cv["bp"], ctx["anchors"]) if cv and cv.get("bp") else None
    liq = cv["liq"] if cv else 0.0
    sm = ctx["secm"].get(t) or {}
    cfo = usd(sm.get("cfo"), ff) if fresh_annual(sm.get("cfoEnd")) else None
    capex = usd(sm.get("capex"), ff) if cfo is not None else None
    ltd = usd(sm.get("ltd"), ff) if fresh_annual(sm.get("ltdEnd")) else None
    cap = rm.captive_debt_share(industry, debt, rev)
    debt_ind = debt * (1 - cap) if (debt and cap) else debt          # the industrial group's own debt
    # ---- pillar 2: distance to default (companies whose debt is their funding, not deposits / policies)
    n_eq = dd = sig_e = None
    q_eq = 0.0
    if method == "corporate" and mcap and mcap > 0 and debt_ind and debt_ind / mcap >= 0.01:
        vc = ctx["vol"].get(t) or {}
        sig_e, src = f(vc.get("v")), "realized 1y"
        iv = (ctx["iv"].get(t) or {}).get("iv")
        if sig_e and iv and iv > IV_STRESS * sig_e:
            sig_e, src = iv, "implied (stress)"
        elif not sig_e and iv:
            sig_e, src = iv, "implied"
        q_eq = 1.0 if debt_ind / mcap >= 0.05 else 0.5      # barely levered: the structural read says little
        if not sig_e:
            sig_e, src, q_eq = ctx["sector_vol"].get(sector) or 0.35, "sector median", 0.5
        lt = None if (ltd is None or cap) else min(ltd, debt_ind)
        dd = rm.merton_dd(mcap, rm.default_point(debt_ind, lt), sig_e)
        n_eq = rm.dd_notch(dd)
        rec["eqSrc"] = src
    # ---- pillar 3: fundamentals
    if method == "bank":
        n_f, det = rm.bank_scorecard({"assets": assets, "equity": usd(r.get("totalEquity"), ff),
                                      "roa": f(r.get("returnOnAssets"))}, sovn)
    elif method == "insurer":
        n_f, det = rm.insurer_scorecard({"assets": assets, "equity": usd(r.get("totalEquity"), ff),
                                         "roa": f(r.get("returnOnAssets")), "industry": industry}, sovn)
    else:
        cpn = ((ctx["bonds"].get(t) or {}).get("avgCoupon"))
        crisk = None if sovn is None or country in NO_CEILING else (6 if sovn >= 17 else 5 if sovn >= 14 else 4 if sovn >= 11 else None)
        n_f, det = rm.corporate_scorecard({"sector": sector, "industry": industry, "revenue": rev, "ebitda": ebitda, "debt": debt,
                                           "cash": usd(r.get("cash"), ff), "fcf": usd(r.get("freeCashFlow"), ff),
                                           "cfo": cfo, "capex": capex, "coupon": cpn, "currentRatio": f(r.get("currentRatio")),
                                           "stability": rm.stability_score(sm.get("oiVol"), sm.get("loss"), sm.get("n")),
                                           "countryRisk": crisk})
    q_f = 0.0
    if n_f is not None:
        q_f = 1.0 if ((det or {}).get("ratios", 0) >= 2 or (det or {}).get("method") or (det or {}).get("rule")) else 0.5
    # ---- agency override
    ag = (ctx["agency"].get(t) or {})
    ags = [rm.notch(ag.get(k)) for k in ("sp", "moodys", "fitch")]
    ags = [a for a in ags if a]
    n_ag = sum(ags) / len(ags) if ags else None
    model, w, fl = rm.composite(n_cds, liq, n_eq, q_eq, n_f, q_f)
    flags += fl
    m_nc, _, _ = rm.composite(None, 0.0, n_eq, q_eq, n_f, q_f)
    if n_cds is not None and m_nc is not None and liq >= 0.3:
        rec["gap"] = round(n_cds - m_nc, 1)               # + : the CDS market prices it worse than its fundamentals
    x = model
    if n_ag is not None:
        x = n_ag
        rec["src"] = "agency"
        rec["agency"] = {k: ag.get(k) for k in ("sp", "moodys", "fitch", "asOf") if ag.get(k)}
        rec["model"] = None if model is None else round(model, 2)
    else:
        rec["src"] = "model"
    if x is not None and sovn is not None and country not in NO_CEILING and n_ag is None and x < sovn - 1:
        x = float(sovn - 1)                              # a company rarely tops its sovereign by more than a notch
        flags.append("sovereign-ceiling")
    rec["x"] = None if x is None else round(x, 2)
    rec["w"] = w
    rec["p"] = {"cds": None if n_cds is None else [round(n_cds, 2), cv.get("bp"), liq],
                "eq": None if n_eq is None else [round(n_eq, 2), round(dd, 2) if dd is not None else None, round(sig_e, 3) if sig_e else None,
                                                 rec.pop("eqSrc", None)],
                "fund": None if n_f is None else [round(n_f, 2), method]}
    rec["fund"] = compact_fund(det)
    if cv:
        rec["cds"] = {k: cv.get(k) for k in ("bp", "bp1m", "rel", "last", "n30", "liq", "entity")}
    if flags:
        rec["fl"] = flags
    rec["_cds_rel"] = cv.get("rel") if cv else None
    rec["_liq"] = liq
    ivr = ctx["iv"].get(t) or {}
    if ivr.get("iv") and ivr.get("iv20") and ctx.get("iv_mkt") is not None:
        rec["_iv_rel"] = (ivr["iv"] / ivr["iv20"] - 1) - ctx["iv_mkt"]
    return rec


# ---------------------------------------------------------------------------- baskets ----
def build_baskets(rows, cos, fx, members):
    """Holdings lists [(ticker, weight)] for the look-through baskets."""
    def mcap_usd(t):
        r = rows.get(t) or {}
        v, k = f(r.get("marketcap")), main_ccy_rate(r.get("currency"), fx)
        return v * k if v and k else None
    us = [t for t, rec in cos.items() if (rows.get(t) or {}).get("country") in ("United States", "") and rec.get("x") is not None]
    B = {}
    spx = [(t, w) for t, w in members.get("SPY", []) if w > 0]
    B["SPX"] = spx
    B["SPX_TOP50"] = sorted(spx, key=lambda x: -x[1])[:50]
    B["SPX_EQ"] = [(t, 1.0) for t, _ in spx]
    pb = {t: f((rows.get(t) or {}).get("priceToBook")) for t, _ in spx}
    pbs = sorted(v for v in pb.values() if v and v > 0)
    med = pbs[len(pbs) // 2] if pbs else None
    if med:
        B["SPX_VALUE"] = [(t, w) for t, w in spx if pb.get(t) and 0 < pb[t] <= med]
        B["SPX_GROWTH"] = [(t, w) for t, w in spx if pb.get(t) and pb[t] > med]
    for t, w in spx:
        s = (rows.get(t) or {}).get("sector")
        if s:
            B.setdefault("SPX_SECTOR:" + s, []).append((t, w))
    B["DJIA"] = [(t, w) for t, w in members.get("DIA", []) if w > 0]
    nd = [(t, mcap_usd(t)) for t in cos if (rows.get(t) or {}).get("exchange") in NASDAQ
          and (rows.get(t) or {}).get("sector") != "Financial Services" and mcap_usd(t)]
    nd = sorted(nd, key=lambda x: -x[1])[:100]
    tot = sum(w for _, w in nd) or 1.0
    B["NDX"] = [(t, min(w / tot, 0.14)) for t, w in nd]       # modified cap weight: no name above 14 %
    B["US_TOTAL"] = [(t, mcap_usd(t)) for t in us if mcap_usd(t)]
    small = [(t, m) for t, m in B["US_TOTAL"] if 1.5e8 <= m <= 7e9]
    B["US_SMALL"] = small
    pbsm = sorted(f((rows.get(t) or {}).get("priceToBook")) or 0 for t, _ in small)
    pbsm = [v for v in pbsm if v > 0]
    if pbsm:
        m2 = pbsm[len(pbsm) // 2]
        B["US_SMALL_VALUE"] = [(t, m) for t, m in small if 0 < (f((rows.get(t) or {}).get("priceToBook")) or 0) <= m2]
    B["NYSE"] = [(t, mcap_usd(t)) for t in us if (rows.get(t) or {}).get("exchange") == "NYQ" and mcap_usd(t)]
    return B, mcap_usd


def industry_basket(spec, rows, cos, mcap_usd, equal=False, nasdaq=False, top=40):
    rx = re.compile(spec, re.I)
    xs = [(t, mcap_usd(t)) for t in cos if rx.search(str((rows.get(t) or {}).get("industry") or ""))
          and (not nasdaq or (rows.get(t) or {}).get("exchange") in NASDAQ) and mcap_usd(t)]
    xs = sorted(xs, key=lambda x: -x[1])[:top]
    return [(t, 1.0 if equal else w) for t, w in xs]


def bond_sleeve(kind, cos, rows, fx, sov_rat, mkt, mcap_usd):
    """A bond fund's sleeve -> (notch, trend, basis)."""
    us = sov_rat.get("United States")
    if kind in ("UST", "AGENCY_MBS"):
        return us, None, "US Treasury / agency (US sovereign)" if kind == "UST" else "agency MBS (US sovereign)"

    def debt_usd(t):
        r = rows.get(t) or {}
        v, k = f(r.get("totalDebt")), main_ccy_rate(r.get("financialCurrency") or r.get("currency"), fx)
        return v * k if v and k else None
    if kind in ("IG_CORP", "HY_CORP", "AGG"):
        ig = [(t, debt_usd(t)) for t, c in cos.items() if c.get("n") and c["n"] <= rm.IG_MAX and debt_usd(t)]
        hy = [(t, debt_usd(t)) for t, c in cos.items() if c.get("n") and rm.IG_MAX < c["n"] <= 19 and debt_usd(t)]
        lt_ig = rm.look_through(ig, cos)
        lt_hy = rm.look_through(hy, cos)
        if kind == "IG_CORP":
            return (lt_ig or {}).get("n"), (mkt.get("ig") or {}).get("tr"), f"rated investment-grade issuers by debt outstanding ({len(ig)})"
        if kind == "HY_CORP":
            return (lt_hy or {}).get("n"), (mkt.get("hy") or {}).get("tr"), f"rated high-yield issuers by debt outstanding ({len(hy)})"
        parts = [(us, 0.72), (1.0, 0.03)] + ([((lt_ig or {}).get("n"), 0.25)] if (lt_ig or {}).get("n") else [])
        warf = sum(rm.warf_of(n) * w for n, w in parts if n) / sum(w for n, w in parts if n)
        tr = (mkt.get("ig") or {}).get("tr")
        return rm.notch_of_warf(warf), None if tr is None else round(0.25 * tr, 3), "US aggregate: ~72 % Treasuries + agency MBS, ~25 % IG corporates, ~3 % AAA"
    if kind == "EM_SOV":
        w = {"Mexico": 4, "Saudi Arabia": 4, "Indonesia": 3.5, "United Arab Emirates": 3.5, "Qatar": 3, "Turkey": 3.5, "Brazil": 3,
             "Philippines": 3, "Colombia": 3, "Chile": 3, "Oman": 3, "South Africa": 3, "Peru": 2.5, "Poland": 2.5, "Panama": 2.5,
             "Dominican Republic": 3, "Egypt": 3, "Nigeria": 2.5, "Argentina": 3, "Romania": 3, "Hungary": 2.5, "Ecuador": 2,
             "Uruguay": 2, "Kazakhstan": 1.5, "Guatemala": 1.5, "Ivory Coast": 1.5, "Angola": 1.5, "Pakistan": 1.5, "Kenya": 1.5,
             "Bahrain": 2, "Sri Lanka": 1, "Jordan": 1, "Ukraine": 1, "El Salvador": 1}
        n, _ = rm.sovereign_blend(w, sov_rat)
        return n, (mkt.get("em") or {}).get("tr"), "USD emerging-market sovereigns (approximate EMBI weights)"
    if kind == "INTL_AGG":
        w = {"Japan": 13, "France": 10, "Germany": 9, "Italy": 8, "United Kingdom": 6, "Spain": 5, "Canada": 5, "European Union": 4,
             "China": 5, "Australia": 3, "Netherlands": 3, "Belgium": 3, "South Korea": 3, "Austria": 2, "Switzerland": 2,
             "Sweden": 1, "Denmark": 1, "Finland": 1, "Ireland": 1, "Portugal": 1, "Mexico": 1, "Indonesia": 1, "Poland": 1, "Singapore": 1}
        n, _ = rm.sovereign_blend(w, sov_rat)
        return n, None, "international government-heavy aggregate (approximate country weights)"
    return None, None, "unknown sleeve"


NAME_RULES = (
    (r"S&P 500|S&P500", {"basket": "SPX"}), (r"Nasdaq[- ]?100|\bQQQ\b", {"basket": "NDX"}), (r"Dow Jones Industrial", {"basket": "DJIA"}),
    (r"Russell 2000|Small[- ]Cap", {"basket": "US_SMALL"}), (r"Total (?:U\.?S\.? )?Stock Market", {"basket": "US_TOTAL"}),
    (r"Treasur|T-Bill|Government Bond", {"bond": "UST"}), (r"High Yield|Junk", {"bond": "HY_CORP"}),
    (r"Investment Grade|Corporate Bond", {"bond": "IG_CORP"}), (r"Aggregate|Total Bond", {"bond": "AGG"}),
    (r"\bMBS\b|Mortgage-Backed", {"bond": "AGENCY_MBS"}), (r"Emerging Markets? (?:USD )?Bond", {"bond": "EM_SOV"}),
    (r"Semiconductor", {"basket": "IND:Semiconductor"}), (r"Biotech", {"basket": "IND_EQ:Biotechnology"}),
    (r"Bitcoin|Ether(?:eum)?|Solana|XRP|Crypto", {"nr": "crypto - no issuer"}),
    (r"\bGold\b(?! Miners)|\bSilver\b(?! Miners)|Platinum|Palladium|Crude|\bOil\b|Natural Gas|Copper|Wheat|Corn|Soybean|Commodit|Freight",
     {"nr": "commodity - no issuer"}),
    (r"\bVIX\b|Volatility", {"vol": True}),
)


def rule_for_fund(t, r, lt_cfg, sov_names):
    if t in (lt_cfg.get("etfs") or {}):
        return dict(lt_cfg["etfs"][t]), "config"
    name = str(r.get("name") or "")
    m = re.search(r"MSCI ([A-Z][A-Za-z ]+?)(?: ETF| Index| Capped|$)", name)
    if m and m.group(1).strip() in sov_names:
        return {"sov": m.group(1).strip()}, "name"
    for rx, rule in NAME_RULES:
        if re.search(rx, name, re.I):
            return dict(rule), "name"
    return None, None


def future_rule(t, lt_cfg):
    root = re.sub(r"=F$", "", t)
    return (lt_cfg.get("futures") or {}).get(root)


# ------------------------------------------------------------------------------- main ----
def main():
    t0 = datetime.now(timezone.utc)
    lt_cfg = read(CFG / "lookthrough.json", {}) or {}
    sov_cfg = read(CFG / "sovereign_ratings.json", {}) or {}
    agency = (read(CFG / "agency_ratings.json", {}) or {}).get("ratings") or {}
    sov_get, sov_rat = sov_lookup(sov_cfg)
    rows = load_books()
    if not rows:
        log("ratings", "no book rows - nothing written")
        return 1
    fx = load_fx()
    oas, mkt = load_oas()
    anchors = rm.spread_anchors(oas)
    members = load_members()
    sec = load_sec()
    cds = read(DATA / "cds" / "latest.json", {}) or {}
    bonds = (read(DATA / "credit_index.json", {}) or {}).get("tickers") or {}
    prev = read(OUT, {}) or {}
    prev_r = prev.get("ratings") or {}
    kinds = {t: kind_of(t, r) for t, r in rows.items()}
    co_t = [t for t, k in kinds.items() if k == "co"]
    vol = read(VOLC, {}) or {}
    vol = refresh_vols(rows, co_t, vol)
    iv = load_iv(co_t)
    secm = refresh_sec(co_t, read(SECC, {}) or {})
    iv_mkt = None
    s = iv.get("SPY") or {}
    if s.get("iv") and s.get("iv20"):
        iv_mkt = s["iv"] / s["iv20"] - 1
    by_sector = {}
    for t in co_t:
        v = f((vol.get(t) or {}).get("v"))
        if v:
            by_sector.setdefault(rows[t].get("sector") or "", []).append(v)
    sector_vol = {k: statistics.median(v) for k, v in by_sector.items() if len(v) >= 5}
    ctx = {"sec": sec, "sov": sov_get, "cds": cds, "anchors": anchors, "vol": vol, "iv": iv, "iv_mkt": iv_mkt,
           "sector_vol": sector_vol, "bonds": bonds, "agency": agency, "secm": secm}
    # migration: the published notches ~30 days ago
    snap30 = None
    if HIST.exists():
        lim = (date.fromisoformat(TODAY) - timedelta(days=28)).isoformat()
        cands = sorted(p for p in HIST.glob("*.json") if p.stem <= lim)
        if cands:
            snap30 = (read(cands[-1], {}) or {}).get("n") or {}

    def finish(t, rec):
        """published notch (hysteresis vs yesterday), letter, trend, signal, outlook, change log."""
        p = prev_r.get(t) or {}
        x = rec.get("x")
        if x is not None:
            x = rec["x"] = round(float(x), 2)
        if isinstance(rec.get("lt"), dict) and rec["lt"].get("n") is not None:
            rec["lt"]["n"] = round(rec["lt"]["n"], 2)
        n = rm.publish(x, p.get("n")) if rec.get("src") not in ("event",) else (22 if x else None)
        rec["n"] = n
        rec["r"] = rm.letter(n) if n else "NR"
        mig = None
        if snap30 is not None and n and snap30.get(t):
            mig = n - int(snap30[t])
        if rec.get("k") == "co":
            tr = rm.trend_score(rec.pop("_cds_rel", None), rec.pop("_liq", 0.0), mig, rec.pop("_iv_rel", None))
            rec["tr"] = tr
            rec["sig"] = rm.credit_signal(n, tr) if n else None
        o = rm.outlook(rec.get("tr"), mig)
        if o != "stable":
            rec["o"] = o
        if mig:
            rec["d30"] = mig
        if p.get("n") and n and p["n"] != n:
            rec["lc"] = {"from": p.get("r"), "on": TODAY}
        elif p.get("lc"):
            rec["lc"] = p["lc"]
        for k in [k for k in rec if k.startswith("_")]:
            rec.pop(k)
        return rec

    # ---- companies
    cos = {}
    for t in co_t:
        try:
            cos[t] = finish(t, rate_company(t, rows[t], fx, ctx))
        except Exception as e:                      # one bad row never stops the run
            log("ratings", f"{t}: {type(e).__name__}: {e}")
    rated = {t: c for t, c in cos.items() if c.get("n")}
    # ---- baskets and everything that isn't a company
    B, mcap_usd = build_baskets(rows, rated, fx, members)
    lt_cache = {}

    def basket(key):
        if key in lt_cache:
            return lt_cache[key]
        if key in B:
            mem = B[key]
        elif key.startswith("IND_NASDAQ:"):
            mem = industry_basket(key.split(":", 1)[1], rows, rated, mcap_usd, nasdaq=True, top=80)
        elif key.startswith("IND_EQ:"):
            mem = industry_basket(key.split(":", 1)[1], rows, rated, mcap_usd, equal=True, top=80)
        elif key.startswith("IND:"):
            mem = industry_basket(key.split(":", 1)[1], rows, rated, mcap_usd)
        else:
            mem = []
        lt_cache[key] = (rm.look_through(mem, rated), mem)
        return lt_cache[key]
    sov_names = set(sov_rat)
    out = dict(cos)
    for t, r in rows.items():
        k = kinds[t]
        if k == "co":
            continue
        rec = {"k": k, "name": r.get("name") or t}
        rule = None
        if k == "etf":
            rule, how = rule_for_fund(t, r, lt_cfg, sov_names)
            rec["k"] = "etp" if (rule or {}).get("lev") not in (None, 1) else "etf"
            if rule is None:
                rec.update({"x": None, "src": "nr", "why": "fund holdings unknown (add it to config/lookthrough.json)"})
        elif k == "idx":
            rule = (lt_cfg.get("indices") or {}).get(t) or {"nr": "index - not mapped"}
        elif k == "fut":
            rule = future_rule(t, lt_cfg) or {"nr": "commodity future - no issuer"}
        elif k == "fx":
            legs = rm.fx_legs(t)
            cur = sov_cfg.get("currencies") or {}
            if legs:
                b, q = legs
                nb, nq = sov_get(cur.get(b)), sov_get(cur.get(q))
                rec.update({"x": nb, "src": "sovereign", "fx": {"base": b, "quote": q, "bR": rm.letter(nb), "qR": rm.letter(nq),
                                                                "diff": None if (nb is None or nq is None) else nq - nb},
                            "why": f"long {b} / short {q}: {cur.get(b) or b} vs {cur.get(q) or q}"})
            else:
                rec.update({"x": None, "src": "nr", "why": "currency pair not parsed"})
        elif k == "opt":
            continue                                    # after the funds (an option's underlying may be one)
        else:
            rec.update({"x": None, "src": "nr", "why": "crypto - no issuer" if k == "nr" and str(r.get("asset_class")) == "Crypto"
                        else "private company - no public financials" if str(r.get("asset_class")) == "Private" else "no issuer to rate"})
        if rule:
            if rule.get("lev") is not None:
                rec["lev"] = rule["lev"]
            if rule.get("proxy"):
                rec["proxy"] = rule["proxy"]
            if rule.get("nr"):
                rec.update({"x": None, "src": "nr", "why": rule["nr"]})
            elif rule.get("vol"):
                hy = (mkt.get("hy") or {}).get("tr")
                rec.update({"x": None, "src": "nr", "why": "volatility product - no issuer; credit stress lifts it",
                            "tr": None if hy is None else round(-hy, 3), "sig": None if hy is None else round(-hy, 3)})
            elif rule.get("basket"):
                lt, mem = basket(rule["basket"])
                rec.update({"src": "lookthrough", "basket": rule["basket"]})
                if lt and lt.get("n"):
                    rec.update({"x": round(lt["n"], 2), "lt": lt, "tr": lt.get("tr"), "sig": lt.get("tr")})
                else:
                    rec.update({"x": None, "why": f"look-through coverage too low ({(lt or {}).get('coverage', 0):.0%} of {len(mem)} holdings rated)"})
            elif rule.get("list"):
                mem = [(m.upper(), 1.0) for m in rule["list"]]
                lt = rm.look_through(mem, rated)
                rec.update({"src": "lookthrough", "basket": "list"})
                if lt and lt.get("n"):
                    rec.update({"x": round(lt["n"], 2), "lt": lt, "tr": lt.get("tr"), "sig": lt.get("tr")})
                else:
                    rec.update({"x": None, "why": f"look-through coverage too low ({(lt or {}).get('coverage', 0):.0%} of {len(mem)} named holdings rated)"})
            elif rule.get("stock") or rule.get("stockName"):
                st = rule.get("stock")
                if not st and rule.get("stockName"):
                    rx = re.compile(re.escape(rule["stockName"]), re.I)
                    st = next((u for u in rated if rx.search(str(rows[u].get("name") or ""))), None)
                c = rated.get(st or "")
                if c:
                    rec.update({"x": c.get("x") if c.get("x") is not None else c["n"], "src": "lookthrough", "basket": "stock", "of": st,
                                "tr": c.get("tr"), "sig": c.get("sig")})
                else:
                    rec.update({"x": None, "src": "nr", "why": f"underlying {st or rule.get('stockName')} not rated"})
            elif rule.get("sov") is not None:
                sv = rule["sov"]
                if isinstance(sv, dict):
                    n_s, cov = rm.sovereign_blend(sv, sov_rat)
                    rec.update({"x": n_s, "src": "sovereign", "sov": "blend", "cov": round(cov, 2)})
                else:
                    rec.update({"x": sov_get(sv), "src": "sovereign", "sov": sv})
                if rule.get("fx"):
                    rec["fxProxy"] = True
            elif rule.get("bond"):
                n_b, tr, basis = bond_sleeve(rule["bond"], rated, rows, fx, sov_rat, mkt, mcap_usd)
                rec.update({"x": n_b, "src": "bond", "sleeve": rule["bond"], "basis": basis, "tr": tr, "sig": tr})
            elif rule.get("fx"):
                b, q = rule["fx"]
                cur = sov_cfg.get("currencies") or {}
                nb, nq = sov_get(cur.get(b)), sov_get(cur.get(q))
                rec.update({"x": nb, "src": "sovereign", "fx": {"base": b, "quote": q, "bR": rm.letter(nb), "qR": rm.letter(nq)}})
            iss = rule.get("etnIssuer")
            if iss and rec.get("x") is not None:
                ci = rated.get(iss.get("ticker") or "")
                ni = ci["n"] if ci else rm.notch(iss.get("fallback"))
                rec["issuer"] = {"name": iss.get("name"), "r": rm.letter(ni), "src": "model" if ci else "fallback"}
                if ni and ni > rec["x"]:
                    rec["x"] = float(ni)                 # an ETN is the bank's unsecured debt: no better than the bank
                    rec.setdefault("fl", []).append("etn-issuer")
        out[t] = finish(t, rec)
    for t, r in rows.items():                           # options: their underlying's rating
        if kinds[t] != "opt":
            continue
        m = re.match(r"^([A-Z]{1,6})\d{6}[CP]\d{8}$", t)
        u = out.get(m.group(1)) if m else None
        rec = {"k": "opt", "name": r.get("name") or t}
        if u and u.get("n"):
            rec.update({"x": u.get("x") if u.get("x") is not None else u["n"], "src": "lookthrough", "basket": "underlying",
                        "of": m.group(1), "tr": u.get("tr"), "sig": u.get("sig")})
        else:
            rec.update({"x": None, "src": "nr", "why": "option - underlying not rated" if m else "option - underlying not parsed"})
        out[t] = finish(t, rec)
    # ---- calibration: the model pillars against liquid CDS (logged + stored - the DD map and buckets are judged here)
    cal = {"n": 0}
    pairs = [(c["p"]["cds"][0], c["p"].get("eq"), c["p"].get("fund")) for c in cos.values()
             if c.get("p") and c["p"].get("cds") and c["p"]["cds"][2] >= 0.5]
    if pairs:
        def stats(idx):
            d = [(p[idx][0] - p[0]) for p in pairs if p[idx]]
            return (round(statistics.mean(abs(x) for x in d), 2), round(statistics.median(d), 2), len(d)) if d else (None, None, 0)
        ea, eb, en = stats(1)
        fa, fb, fn = stats(2)
        cal = {"n": len(pairs), "eq": {"mae": ea, "bias": eb, "n": en}, "fund": {"mae": fa, "bias": fb, "n": fn},
               "note": "model pillars vs the CDS-implied (market) notch on names with liquid CDS - drift monitor, not accuracy (bias + = model rates worse than the market)"}
        dd_pts = [(math.log(max(0.05, c["p"]["eq"][1])), c["p"]["cds"][0]) for c in cos.values()
                  if c.get("p") and c["p"].get("cds") and c["p"]["cds"][2] >= 0.5 and c["p"].get("eq") and c["p"]["eq"][1] is not None]
        if len(dd_pts) >= 20:
            mx = statistics.mean(x for x, _ in dd_pts)
            my = statistics.mean(y for _, y in dd_pts)
            sxx = sum((x - mx) ** 2 for x, _ in dd_pts)
            b = sum((x - mx) * (y - my) for x, y in dd_pts) / sxx if sxx else 0.0
            cal["ddFit"] = {"n": len(dd_pts), "slope": round(b, 3), "intercept": round(my - b * mx, 3),
                            "note": "CDS notch ~ intercept + slope x ln(DD), for recalibrating DD_POINTS"}
    # ---- summary
    bk = {}
    for t, c in out.items():
        bk[rm.bucket(c.get("n"))] = bk.get(rm.bucket(c.get("n")), 0) + 1
    doc = {"asOf": now_iso(), "date": TODAY, "version": "z194",
           "method": "model-implied S&P-scale ratings: CDS-implied (vs ICE BofA OAS by rating) x distance-to-default x S&P "
                     "corporate-methodology fundamentals; funds by look-through (Moody's WARF); sovereigns from config",
           "oas": oas, "market": mkt, "anchors": {rm.SCALE[i]: a for i, a in enumerate(anchors)},
           "calibration": cal, "summary": {"total": len(out), "byBucket": bk,
                                           "companies": len(cos), "companiesRated": len(rated)},
           "sovereignAsOf": sov_cfg.get("asOf"), "ratings": out}
    write(OUT, doc)
    write(HIST / f"{TODAY}.json", {"asOf": now_iso(), "n": {t: c["n"] for t, c in out.items() if c.get("n")}})
    for p in sorted(HIST.glob("*.json"))[:-400]:
        p.unlink()
    write(VOLC, vol)
    write(SECC, secm)
    secs = (datetime.now(timezone.utc) - t0).total_seconds()
    log("ratings", f"{len(out)} instruments · {len(rated)} of {len(cos)} companies rated · buckets {json.dumps(bk, sort_keys=True)} · "
                   f"CDS calibration {json.dumps(cal.get('eq'))} / {json.dumps(cal.get('fund'))} · {secs:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
