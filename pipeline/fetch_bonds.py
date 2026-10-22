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
BATCH = int(os.environ.get("BONDS_BATCH", "60" if KEY else "15"))
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


def figi_search(query):
    out, nxt = [], None
    for _ in range(4):
        body = {"query": query, "marketSecDes": "Corp"}
        if nxt:
            body["start"] = nxt
        h = {"Content-Type": "application/json"}
        if KEY:
            h["X-OPENFIGI-APIKEY"] = KEY
        doc = json.loads(http("https://api.openfigi.com/v3/search", data=json.dumps(body).encode(), headers=h, timeout=60).decode())
        out += doc.get("data") or []
        nxt = doc.get("next")
        time.sleep(PAUSE)
        if not nxt:
            break
    return out


def bonds_for(ticker, info, by_norm):
    rows = figi_search(info["name"])
    today = date.today().isoformat()
    seen, bonds = set(), []
    roots = {ticker, ticker.split("-")[0], {"GOOGL": "GOOG", "GOOG": "GOOGL", "BRK-B": "BRK"}.get(ticker, ticker)}
    for r in rows:
        if match_entity(r.get("name") or "", by_norm) != ticker:
            continue                                   # another issuer that merely shares a word
        if str(r.get("ticker") or "").split(" ")[0] not in roots:
            continue                                   # Bloomberg's bond ticker root must be the company's own
        cpn, mat = parse_desc(r.get("ticker"))
        if not mat or mat < today:
            continue
        key = (cpn, mat)
        if key in seen:
            continue
        seen.add(key)
        bonds.append({"figi": r.get("figi"), "desc": r.get("ticker"), "issuer": r.get("name"), "coupon": cpn, "maturity": mat,
                      "type": r.get("securityType") or r.get("securityType2")})
    bonds.sort(key=lambda b: b["maturity"])
    return bonds


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
    ok = 0
    for t in todo:
        try:
            b = bonds_for(t, iss[t], by_norm)
            write(OUT / "issuers" / f"{t}.json", {"ticker": t, "name": iss[t]["name"], "bonds": b, "source": "OpenFIGI", "updatedAt": now_iso()})
            ok += 1
            if b:
                log("bonds", f"{t}: {len(b)} bonds ({b[0]['desc']} … {b[-1]['desc']})")
        except Exception as e:
            log("bonds", f"{t}: {e}")
            if "429" in str(e):
                log("bonds", "rate-limited - the rest wait for the next run")
                break
    try:
        finra_probe()
    except Exception as e:
        log("trace", f"probe failed: {e}")
    log("bonds", f"refreshed {ok}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
