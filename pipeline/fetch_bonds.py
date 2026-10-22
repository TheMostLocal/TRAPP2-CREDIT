#!/usr/bin/env python3
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-CREDIT
#  >>> FILE PATH:   pipeline/fetch_bonds.py
#
#  z139 - CORPORATE BONDS per company.
#   1. The bond list: OpenFIGI (Bloomberg's open identifier service, free) - every corporate debt
#      security filed under the issuer's name, with coupon and maturity ("ORCL 5.55 02/06/53").
#      Matured bonds dropped, one row per (coupon, maturity). OPENFIGI_API_KEY (free) raises the
#      rate limit from 5 to 20 searches a minute; issuers refresh every 14 days in batches.
#   2. Prices / yields: FINRA TRACE - the official record of every US corporate bond trade.
#      With FINRA_CLIENT_ID / FINRA_CLIENT_SECRET set, each run probes the FINRA Query API's
#      fixed-income datasets (keyless metadata) and logs which security-level data the account
#      can read and its fields; trades are matched to the list by coupon + maturity. Until that
#      is confirmed the bonds show their terms only - never invented prices.
#    -> data/bonds/issuers/<T>.json { ticker, name, bonds[{figi, desc, coupon, maturity, type}], updatedAt }
#       data/bonds/finra_probe.json  (what FINRA's API exposes to this account)
# ============================================================
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone

from credit_common import DATA, http, issuers, log, match_entity, norm_name, now_iso, read, write

OUT = DATA / "bonds"
KEY = os.environ.get("OPENFIGI_API_KEY", "").strip()
BATCH = int(os.environ.get("BONDS_BATCH") or ("150" if KEY else "20"))   # z146: 150 a day with a key; a manual run can do all
PAUSE = 3.2 if KEY else 12.5                 # 20 / min with a key, 5 / min without
REFRESH_D = 14
TICK = re.compile(r"\s(\d+(?:\.\d+)?|\d+\s\d/\d|FLT|V\d)\s+(\d{2})/(\d{2})/(\d{2})\b")


def parse_desc(desc):
    """'ORCL 5.55 02/06/53' -> (5.55, '2053-02-06'); floaters -> (None, date)."""
    m = TICK.search(" " + str(desc or ""))
    if not m:
        return None, None
    cpn = m.group(1)
    coupon = None
    try:
        if " " in cpn and "/" in cpn:
            w, f = cpn.split(" ")
            a, b = f.split("/")
            coupon = float(w) + float(a) / float(b)
        else:
            coupon = float(cpn)
    except ValueError:
        coupon = None
    mm, dd, yy = int(m.group(2)), int(m.group(3)), int(m.group(4))
    year = 2000 + yy if yy < 70 else 1900 + yy
    try:
        return coupon, date(year, mm, dd).isoformat()
    except ValueError:
        return coupon, None


MAX_PAGES = int(os.environ.get("BONDS_MAX_PAGES", "6"))


def _figi(endpoint, body):
    h = {"Content-Type": "application/json"}
    if KEY:
        h["X-OPENFIGI-APIKEY"] = KEY
    doc = json.loads(http(f"https://api.openfigi.com/v3/{endpoint}", data=json.dumps(body).encode(), headers=h, timeout=60).decode())
    if isinstance(doc, dict) and doc.get("error"):
        raise RuntimeError(f"OpenFIGI: {doc['error']}")
    return doc


def figi_pages(query, live_only=True):
    """z146: OpenFIGI's FILTER endpoint (every match, not a relevance top-list) for corporate bonds,
    asking for maturities from today on when the API accepts it - big issuers have thousands of matured
    notes that used to fill the 4 pages before a live bond appeared (Bank of America, Boeing, CSX came
    back with 0). Falls back to SEARCH without the maturity filter if FILTER refuses the request."""
    body = {"query": query, "marketSecDes": "Corp"}
    if live_only:
        body.update({"securityType2": "Corp", "maturity": [date.today().isoformat(), "2199-12-31"]})
    out, nxt, endpoint = [], None, ("filter" if live_only else "search")
    for _ in range(MAX_PAGES):
        b = dict(body, **({"start": nxt} if nxt else {}))
        try:
            doc = _figi(endpoint, b)
        except Exception as e:
            if live_only and not out and ("400" in str(e) or "maturity" in str(e).lower() or "securityType2" in str(e)):
                return figi_pages(query, live_only=False)
            raise
        out += doc.get("data") or []
        nxt = doc.get("next")
        time.sleep(PAUSE)
        if not nxt:
            break
    return out


def bonds_for(ticker, info):
    """The company's live bonds. A result is the company's when Bloomberg's bond-ticker root is the
    company's ticker (e.g. "BA 5.15 05/01/30") OR its issuer name is the company's own or its finance
    arm's - judged against THIS company only (no clash with another listing of a similar name).
    Query by ticker first (precise), by name only if that found nothing."""
    today = date.today().isoformat()
    root0 = ticker.split("-")[0].split(".")[0]
    roots = {ticker, root0, {"GOOGL": "GOOG", "GOOG": "GOOGL", "BRK-B": "BRK"}.get(ticker, ticker)}
    own = {info["norm"]: ticker}
    seen, bonds = set(), []
    stat = {"results": 0, "other": 0, "matured": 0}
    for q in (root0, info["name"]):
        rows = figi_pages(q)
        stat["results"] += len(rows)
        for r in rows:
            root = str(r.get("ticker") or "").split(" ")[0]
            if root not in roots and match_entity(r.get("name") or "", own, min_len=2) != ticker:   # one company only: short names (BP) are safe
                stat["other"] += 1
                continue
            cpn, mat = parse_desc(r.get("ticker"))
            if not mat or mat < today:
                stat["matured"] += 1
                continue
            key = (cpn, mat)
            if key in seen:
                continue
            seen.add(key)
            bonds.append({"figi": r.get("figi"), "desc": r.get("ticker"), "issuer": r.get("name"), "coupon": cpn, "maturity": mat,
                          "type": r.get("securityType") or r.get("securityType2")})
        if bonds:
            break
    bonds.sort(key=lambda b: b["maturity"])
    return bonds, stat


# ---------------------------------------------------------------- FINRA TRACE (probe, then prices)
CANDIDATES = ["trace", "corporateBond", "corporateBonds", "corporateAndAgencyBond", "corporateAndAgencyBonds",
              "corporateBondTrade", "corporateBondTrades", "corporateAndAgencyDebtTrade", "corporatesAndAgenciesCappedVolume",
              "corporateDebtMarketBreadth", "corporateDebtMarketSentiment"]


def finra_probe():
    found = {}
    for nm in CANDIDATES:
        try:
            meta = json.loads(http(f"https://api.finra.org/metadata/group/FixedIncomeMarket/name/{nm}", headers={"Accept": "application/json"}, timeout=30).decode())
            fields = [f.get("name") for f in (meta.get("fields") or meta.get("datasetFields") or []) if isinstance(f, dict)]
            found[nm] = {"exists": True, "fields": fields[:60]}
        except Exception as e:
            code = getattr(e, "code", None)
            if code == 400:
                found[nm] = {"exists": True, "fields": []}
            elif code != 404:
                found[nm] = {"exists": None, "error": str(e)[:120]}
    sec = [k for k, v in found.items() if v.get("exists") and any(f and f.lower() in ("cusip", "cusipid", "symbol", "bondsymbol") for f in v.get("fields") or [])]
    has_creds = bool(os.environ.get("FINRA_CLIENT_ID") and os.environ.get("FINRA_CLIENT_SECRET"))
    write(OUT / "finra_probe.json", {"at": now_iso(), "credentials": has_creds, "datasets": found, "securityLevel": sec})
    log("trace", f"FINRA fixed-income datasets: {', '.join(k for k, v in found.items() if v.get('exists')) or 'none reachable'}"
        f" · security-level: {', '.join(sec) or 'none found'} · credentials {'set' if has_creds else 'NOT set (FINRA_CLIENT_ID / FINRA_CLIENT_SECRET)'}")
    return sec, has_creds


def main():
    iss = issuers()
    by_norm = {}
    for t, v in iss.items():
        by_norm.setdefault(v["norm"], t)
    cds = (read(DATA / "cds" / "latest.json", {}) or {}).get("issuers") or {}
    now = datetime.now(timezone.utc)

    def stale(t):
        doc = read(OUT / "issuers" / f"{t}.json")
        if not doc:
            return True
        try:
            return (now - datetime.fromisoformat(doc["updatedAt"].replace("Z", "+00:00"))).days >= REFRESH_D
        except Exception:
            return True
    order = sorted(iss, key=lambda t: (t not in cds, t))         # names with CDS trading first
    todo = [t for t in order if stale(t)][:BATCH]
    log("bonds", f"{len(iss)} issuers · {len(todo)} to refresh this run (batch {BATCH}, OpenFIGI key {'set' if KEY else 'not set'})")
    ok = nb = 0
    for t in todo:
        try:
            b, st = bonds_for(t, iss[t])
            write(OUT / "issuers" / f"{t}.json", {"ticker": t, "name": iss[t]["name"], "bonds": b, "source": "OpenFIGI", "updatedAt": now_iso(),
                                                  "searched": st})
            ok += 1
            nb += 1 if b else 0
            if b:
                log("bonds", f"{t}: {len(b)} bonds ({b[0]['desc']} … {b[-1]['desc']})")
            else:
                log("bonds", f"{t}: none outstanding · {st['results']} results ({st['other']} another issuer, {st['matured']} matured)")
        except Exception as e:
            log("bonds", f"{t}: {e}")
            if "429" in str(e):
                log("bonds", "rate-limited - the rest wait for the next run")
                break
    try:
        finra_probe()
    except Exception as e:
        log("trace", f"probe failed: {e}")
    files = list((OUT / "issuers").glob("*.json"))
    have = sum(1 for p in files if (read(p) or {}).get("bonds"))
    write(OUT / "_progress.json", {"updatedAt": now_iso(), "issuers": len(iss), "checked": len(files), "withBonds": have,
                                   "thisRun": {"refreshed": ok, "withBonds": nb, "batch": BATCH}})
    log("bonds", f"refreshed {ok} ({nb} with bonds) · checked {len(files)} of {len(iss)} companies so far · {have} with bonds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
