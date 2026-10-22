# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-CREDIT   (new repo, z139)
#  >>> FILE PATH:   pipeline/credit_common.py
#  Shared helpers: paths, safe JSON, the issuer list (the books' US stocks + S&P 500 members),
#  and name matching between tickers and credit reference entities ("ORACLE CORP" ↔ ORCL).
# ============================================================
import json
import math
import os
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = "https://raw.githubusercontent.com/TheMostLocal"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36 valuatio-credit"}
SUFFIX = re.compile(r"\b(THE|INCORPORATED|INC|CORPORATION|CORP|COMPANY|CO|LIMITED|LTD|PLC|P L C|LLC|LP|L P|N V|NV|S A|SA|AG|SE|HOLDINGS?|GROUP|"
                    r"CLASS [A-C]|CL [A-C]|COMMON STOCK|ORDINARY SHARES|ADR|ADS|DEL|NEW)\b")


def log(tag, *a):
    print(f"[{tag}]", *a, flush=True)


def num(v):
    try:
        f = float(str(v).replace(",", "").strip())
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def clean(o):
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    return o


def write(path, doc):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(clean(doc), separators=(",", ":")))
    os.replace(tmp, path)


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http(url, data=None, headers=None, timeout=60):
    h = dict(UA, **(headers or {}))
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def norm_name(s):
    """'Oracle Corporation' / 'ORACLE CORP' / 'Oracle Corp.' -> 'ORACLE'."""
    s = re.sub(r"[^A-Z0-9 ]", " ", str(s or "").upper().replace("&", " AND "))
    s = SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def issuers():
    """{TICKER: {name, norm, sector}} - the books' US-listed stocks and the S&P 500 members."""
    out = {}
    for b in ("TRAPP2", "TRAPP2-1", "TRAPP2-2", "TRAPP2-3"):
        try:
            rows = json.loads(http(f"{RAW}/{b}/main/data/master.json").decode())
            rows = rows if isinstance(rows, list) else (rows.get("rows") or [])
            for x in rows:
                t = str(x.get("ticker") or "").upper().strip()
                if not t or re.search(r"\.[A-Z]{1,3}$|^\^|=X$|=F$|-USD$", t) or str(x.get("asset_class") or "Equity") not in ("Equity", ""):
                    continue
                nm = x.get("name") or x.get("longName") or ""
                if nm and t not in out:
                    out[t] = {"name": nm, "norm": norm_name(nm), "sector": x.get("sector")}
        except Exception as e:
            log("issuers", f"{b}: {e}")
    try:
        uni = json.loads(http(f"{RAW}/TRAPP2-ANALYTICS/main/data/earnings/universe.json").decode())
        for m in ((uni.get("indices") or {}).get("SPY") or {}).get("members") or []:
            t = m.get("ticker")
            if t and t not in out and m.get("name"):
                out[t] = {"name": m["name"], "norm": norm_name(m["name"]), "sector": m.get("sector")}
    except Exception as e:
        log("issuers", f"S&P list: {e}")
    return out


def match_entity(entity, by_norm, min_len=4):
    """Reference-entity name -> ticker: exact normalized match, else the longest company name the
    entity starts with ('FORD MOTOR CREDIT' -> FORD MOTOR -> F), so finance subsidiaries land on
    their parent with the entity's own name kept."""
    n = norm_name(entity)
    if not n:
        return None
    if n in by_norm:
        return by_norm[n]
    best = None
    for k, t in by_norm.items():
        if len(k) >= min_len and n.startswith(k + " ") and (best is None or len(k) > len(best[0])):
            rest = n[len(k) + 1:].split()
            # only finance arms ("FORD MOTOR CREDIT", "GENERAL MOTORS FINANCIAL"), never another business
            if rest and all(w in FIN_SUB for w in rest) and (any(w in FIN_STRONG for w in rest) or rest == ["FINANCIAL"]):
                best = (k, t)
    return best[1] if best else None


FIN_STRONG = {"CREDIT", "FINANCE", "FUNDING", "CAPITAL", "TREASURY"}
FIN_SUB = {"CREDIT", "FINANCE", "FINANCIAL", "CAPITAL", "FUNDING", "SERVICES", "AMERICA", "USA", "US", "INTERNATIONAL", "HOLDING", "TREASURY", "NORTH",
           "MARKETS"}                                   # z146: "BP CAPITAL MARKETS" - BP's financing arm
