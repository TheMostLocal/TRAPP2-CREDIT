#!/usr/bin/env python3
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-CREDIT
#  >>> FILE PATH:   pipeline/fetch_cds_dtcc.py
#
#  z139 - SINGLE-NAME CDS from the official public tape: DTCC's SEC security-based swap data
#  repository (SBSDR) public dissemination - every single-name CDS trade reported by US persons,
#  with execution time, reference entity, maturity, (capped) notional and price. Free, public,
#  one cumulative file per UTC day (SEC_CUMULATIVE_CREDITS_YYYY_MM_DD.zip).
#    -> data/cds/issuers/<T>.json  { ticker, entities, trades[-400], daily[{date,n,notional,bps5y,bpsAll}] }
#       data/cds/latest.json       { asOf, days, issuers{T:{entity,lastDate,bps5y,chg1w,chg1m,n30}}, unmatched[] }
#  Caveats, shown in the app: corporate single names trade thinly (days without a trade are
#  normal); the disseminated price is sometimes blank or quoted as upfront points rather than a
#  spread - only rows with a usable spread feed the spread series, every trade still counts.
#  CDS_BACKFILL_DAYS (default 5; 60 on the first run) = how many UTC days to (re)read.
# ============================================================
import csv
import io
import os
import statistics
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone

from credit_common import DATA, http, issuers, log, match_entity, norm_name, now_iso, num, read, write

OUT = DATA / "cds"
DAYS = int(os.environ.get("CDS_BACKFILL_DAYS", "5"))
PATTERNS = [
    "https://pddata.dtcc.com/ppd/api/report/cumulative/sec/SEC_CUMULATIVE_CREDITS_{y}_{m}_{d}.zip",
    "https://kgc0418-tdw-data-0.s3.amazonaws.com/sec/eod/SEC_CUMULATIVE_CREDITS_{y}_{m}_{d}.zip",
]


def col(headers, *needles, avoid=()):
    """The first header containing every needle (case-insensitive) and none of `avoid`."""
    for h in headers:
        hl = h.lower()
        if all(n in hl for n in needles) and not any(a in hl for a in avoid):
            return h
    return None


def fetch_day(d):
    last = None
    for p in PATTERNS:
        url = p.format(y=d.year, m=f"{d.month:02d}", d=f"{d.day:02d}")
        try:
            raw = http(url, timeout=90)
            z = zipfile.ZipFile(io.BytesIO(raw))
            rows = []
            for nm in z.namelist():
                if nm.lower().endswith(".csv"):
                    rows += list(csv.DictReader(io.TextIOWrapper(z.open(nm), encoding="utf-8", errors="replace")))
            log("cds", f"{d}: {len(rows)} rows from {url.split('/')[2]}")
            return rows
        except Exception as e:
            last = e
    log("cds", f"{d}: no file ({last})")
    return None


def parse(rows):
    """CSV rows -> trades {entity, entityId, ts, tenor, bps, upfront, notional, ccy, id, action}."""
    if not rows:
        return []
    H = list(rows[0].keys())
    c = {"entity": col(H, "underl", "name") or col(H, "reference", "entity") or col(H, "underlying asset name"),
         "eid": col(H, "underl", "id") or col(H, "underlying asset id") or col(H, "reference", "id"),
         "ts": col(H, "execution", "timestamp") or col(H, "execution"),
         "exp": col(H, "expiration") or col(H, "maturity") or col(H, "end date"),
         "price": col(H, "price", avoid=("notation", "unit", "currency", "type")) or col(H, "spread", avoid=("notation", "unit", "currency")),
         "pnote": col(H, "price", "notation") or col(H, "price", "unit") or col(H, "price", "type"),
         "spread": col(H, "spread", avoid=("notation", "unit", "currency", "type")),
         "notional": col(H, "notional", "amount") or col(H, "notional", avoid=("currency", "type")),
         "ccy": col(H, "notional", "currency"),
         "id": col(H, "dissemination", "id", avoid=("original",)),
         "orig": col(H, "original", "dissemination"),
         "action": col(H, "action")}
    out = []
    for r in rows:
        ent = (r.get(c["entity"]) or "").strip() if c["entity"] else ""
        if not ent:
            continue
        act = (r.get(c["action"]) or "").upper() if c["action"] else ""
        ts = (r.get(c["ts"]) or "")[:19] if c["ts"] else ""
        try:
            tsd = datetime.fromisoformat(ts.replace("Z", "")) if ts else None
        except ValueError:
            tsd = None
        tenor = None
        if c["exp"] and tsd:
            try:
                tenor = round((date.fromisoformat((r.get(c["exp"]) or "")[:10]) - tsd.date()).days / 365.25, 2)
            except ValueError:
                pass
        pv, note = num(r.get(c["price"])) if c["price"] else None, (r.get(c["pnote"]) or "").upper() if c["pnote"] else ""
        sv = num(r.get(c["spread"])) if c["spread"] and c["spread"] != c["price"] else None
        bps = upf = None
        if sv is not None and 0 < sv < 5000:
            bps = sv if sv >= 1 else sv * 10000            # spread given as a decimal fraction
        elif pv is not None:
            if "BASIS" in note or "BPS" in note:
                bps = pv
            elif "PERCENT" in note:
                bps = pv * 100 if abs(pv) < 50 else None     # 1.25 % = 125 bp
            elif "POINT" in note or "UPFRONT" in note or "MONETARY" in note:
                upf = pv
            elif 1 <= pv <= 3000:
                bps = pv                                     # an unlabeled price in the spread range
        out.append({"entity": ent, "entityId": (r.get(c["eid"]) or "").strip() if c["eid"] else "", "ts": ts, "date": ts[:10],
                    "tenor": tenor, "bps": round(bps, 2) if bps is not None and 0 < bps < 5000 else None, "upfront": upf,
                    "notional": num(r.get(c["notional"])) if c["notional"] else None,
                    "ccy": (r.get(c["ccy"]) or "").strip() if c["ccy"] else "",
                    "id": (r.get(c["id"]) or "").strip() if c["id"] else "", "orig": (r.get(c["orig"]) or "").strip() if c["orig"] else "",
                    "action": act})
    # cancellations remove what they cancel
    cancelled = {t["orig"] or t["id"] for t in out if t["action"].startswith("CANC") or t["action"] == "TERM"}
    return [t for t in out if not (t["action"].startswith("CANC") or t["action"] == "TERM") and t["id"] not in cancelled]


def med(v):
    v = [x for x in v if x is not None]
    return round(statistics.median(v), 2) if v else None


def main():
    seen = read(OUT / "_days.json", {}) or {}
    today = datetime.now(timezone.utc).date()
    days = DAYS if seen else max(DAYS, int(os.environ.get("CDS_FIRST_RUN_DAYS", "60")))
    iss = issuers()
    by_norm = {}
    for t, v in iss.items():
        by_norm.setdefault(v["norm"], t)
    log("cds", f"{len(iss)} issuers to match · reading {days} UTC day(s)")
    new_by_t, unmatched = {}, {}
    for k in range(1, days + 1):
        d = today - timedelta(days=k)
        if seen.get(d.isoformat()) == "done" and k > 2:
            continue
        rows = fetch_day(d)
        if rows is None:
            continue
        for t in parse(rows):
            tk = match_entity(t["entity"], by_norm)
            if not tk:
                unmatched[t["entity"]] = unmatched.get(t["entity"], 0) + 1
                continue
            new_by_t.setdefault(tk, []).append(t)
        seen[d.isoformat()] = "done"
    latest = {"asOf": now_iso(), "source": "DTCC SEC SBSDR public dissemination (single-name CDS)", "issuers": {}, "unmatched": []}
    old_latest = read(OUT / "latest.json", {}) or {}
    for tk in set(new_by_t) | set((old_latest.get("issuers") or {}).keys()):
        doc = read(OUT / "issuers" / f"{tk}.json", {"ticker": tk, "entities": [], "trades": [], "daily": []})
        have = {(x.get("id"), x.get("ts")) for x in doc["trades"]}
        for t in new_by_t.get(tk, []):
            if (t["id"], t["ts"]) not in have:
                doc["trades"].append({k: t[k] for k in ("date", "ts", "entity", "tenor", "bps", "upfront", "notional", "ccy", "id")})
                if t["entity"] not in doc["entities"]:
                    doc["entities"].append(t["entity"])
        doc["trades"].sort(key=lambda x: x.get("ts") or "")
        doc["trades"] = doc["trades"][-400:]
        days_ = {}
        for x in doc["trades"]:
            days_.setdefault(x["date"], []).append(x)
        daily = {d["date"]: d for d in doc.get("daily") or []}
        for dd, xs in days_.items():
            five = [x["bps"] for x in xs if x.get("bps") is not None and x.get("tenor") is not None and 4.0 <= x["tenor"] <= 6.0]
            daily[dd] = {"date": dd, "n": len(xs), "notional": round(sum((x.get("notional") or 0) for x in xs), 0),
                         "bps5y": med(five), "bpsAll": med([x.get("bps") for x in xs])}
        cut = (today - timedelta(days=760)).isoformat()
        doc["daily"] = sorted([v for k, v in daily.items() if k >= cut], key=lambda v: v["date"])
        doc["updatedAt"] = now_iso()
        doc["name"] = (iss.get(tk) or {}).get("name")
        write(OUT / "issuers" / f"{tk}.json", doc)
        s5 = [(d["date"], d["bps5y"]) for d in doc["daily"] if d.get("bps5y") is not None]

        def at_or_before(days_back):
            lim = (today - timedelta(days=days_back)).isoformat()
            xs = [v for d, v in s5 if d <= lim]
            return xs[-1] if xs else None
        last5 = s5[-1][1] if s5 else None
        n30 = sum(d["n"] for d in doc["daily"] if d["date"] >= (today - timedelta(days=30)).isoformat())
        w, m = at_or_before(7), at_or_before(30)
        latest["issuers"][tk] = {"name": doc["name"], "entity": doc["entities"][0] if doc["entities"] else None,
                                 "lastDate": doc["daily"][-1]["date"] if doc["daily"] else None, "bps5y": last5,
                                 "bps5yDate": s5[-1][0] if s5 else None,
                                 "chg1w": round(last5 - w, 2) if (last5 is not None and w is not None) else None,
                                 "chg1m": round(last5 - m, 2) if (last5 is not None and m is not None) else None, "n30": n30}
    latest["unmatched"] = sorted(({"entity": k, "n": v} for k, v in unmatched.items()), key=lambda x: -x["n"])[:60]
    latest["days"] = sorted(seen)[-90:]
    write(OUT / "latest.json", latest)
    write(OUT / "_days.json", {k: v for k, v in seen.items() if k >= (today - timedelta(days=400)).isoformat()})
    log("cds", f"{len(new_by_t)} issuers with new trades · {len(latest['issuers'])} tracked · {len(unmatched)} unmatched entities (top: "
        + ", ".join(f"{u['entity']} ×{u['n']}" for u in latest["unmatched"][:5]) + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
