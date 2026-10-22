#!/usr/bin/env python3
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-CREDIT
#  >>> FILE PATH:   pipeline/build_credit_index.py
#  z139 - one small file the app reads first: which companies have bonds and/or CDS, with the
#  headline numbers (bond count, nearest / last maturity, average coupon, maturity wall by year,
#  CDS 5Y spread and its 1-week / 1-month change).  -> data/credit_index.json
# ============================================================
import sys
from datetime import date

from credit_common import DATA, log, now_iso, read, write


def main():
    cds = (read(DATA / "cds" / "latest.json", {}) or {})
    ci = cds.get("issuers") or {}
    out = {}
    for p in sorted((DATA / "bonds" / "issuers").glob("*.json")):
        d = read(p) or {}
        bs = d.get("bonds") or []
        if not bs:
            continue
        cp = [b["coupon"] for b in bs if b.get("coupon") is not None]
        wall = {}
        for b in bs:
            y = b["maturity"][:4]
            wall[y] = wall.get(y, 0) + 1
        out[p.stem] = {"name": d.get("name"), "bonds": len(bs), "nextMaturity": bs[0]["maturity"], "lastMaturity": bs[-1]["maturity"],
                       "avgCoupon": round(sum(cp) / len(cp), 3) if cp else None, "wall": wall}
    for t, c in ci.items():
        out.setdefault(t, {"name": c.get("name"), "bonds": 0})["cds"] = {k: c.get(k) for k in ("entity", "bps5y", "bps5yDate", "chg1w", "chg1m", "n30", "lastDate")}
    write(DATA / "credit_index.json", {"asOf": now_iso(), "cdsAsOf": cds.get("asOf"), "tickers": out})
    log("index", f"{len(out)} companies · {sum(1 for v in out.values() if v.get('bonds'))} with bonds · {sum(1 for v in out.values() if v.get('cds'))} with CDS trades")
    return 0


if __name__ == "__main__":
    sys.exit(main())
