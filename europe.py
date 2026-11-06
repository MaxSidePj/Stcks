"""
European fundamentals from ESEF annual reports (the machine-readable annual reports every EU-listed
company must publish), served free by filings.xbrl.org. Annual data only.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

BASE = "https://filings.xbrl.org"

# Ticker suffixes on Yahoo Finance for EU/EEA exchanges that publish ESEF reports
EU_SUFFIXES = {"BR", "AS", "PA", "DE", "F", "MI", "MC", "LS", "IR", "VI", "HE", "CO", "ST", "OL", "WA", "AT", "PR", "BD", "LU", "SW"}

CONCEPTS = {
    "revenue": ["Revenue", "RevenueFromContractsWithCustomers"],
    "net_income": ["ProfitLossAttributableToOwnersOfParent", "ProfitLoss"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["ProfitLossFromOperatingActivities"],
    "ocf": ["CashFlowsFromUsedInOperatingActivities"],
    "capex": ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "PurchaseOfPropertyPlantAndEquipment",
              "PurchaseOfPropertyPlantAndEquipmentIntangibleAssetsOtherThanGoodwillInvestmentPropertyAndOtherNoncurrentAssets"],
    "eps": ["DilutedEarningsLossPerShare", "BasicEarningsLossPerShare"],
    "shares": ["AdjustedWeightedAverageShares", "WeightedAverageShares"],
    "equity": ["EquityAttributableToOwnersOfParent", "Equity"],
    "cash": ["CashAndCashEquivalents"],
    "debt": ["NoncurrentPortionOfNoncurrentBorrowings", "LongtermBorrowings", "Borrowings"],
    "dividends": ["DividendsPaidClassifiedAsFinancingActivities", "DividendsPaid"],
    "buybacks": ["PaymentsToAcquireOrRedeemEntitysShares"],
}
INSTANT = {"equity", "cash", "debt"}


def is_eu(ticker: str) -> bool:
    return (ticker.startswith("EU:") and len(ticker[3:]) == 20 and ticker[3:].isalnum()) or (
        "." in ticker and ticker.rsplit(".", 1)[1].upper() in EU_SUFFIXES)


def _get(path: str, budget=None) -> dict:
    url = path if path.startswith("http") else BASE + path
    contact = os.environ.get("SEC_CONTACT_EMAIL") or os.environ.get("GITHUB_REPOSITORY", "personal stock screener")
    req = urllib.request.Request(url, headers={"User-Agent": f"CompanyPulse/1.0 ({contact})", "Accept": "application/json"})
    for attempt in range(3):
        try:
            timeout = budget.request() if budget else 90
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception:
            if budget and (budget.left <= 0 or time.monotonic() >= budget.end - 1):
                raise
            if attempt == 2:
                raise
            time.sleep(3)
    raise RuntimeError("unreachable")


def search(name: str) -> list[dict]:
    f = json.dumps([{"name": "name", "op": "ilike", "val": f"%{name}%"}])
    return [{"lei": e["attributes"]["identifier"], "name": e["attributes"]["name"]}
            for e in _get("/api/entities?filter=" + urllib.parse.quote(f) + "&page%5Bsize%5D=20")["data"]]


STOP = {"sa", "nv", "se", "ag", "the", "group", "holding", "holdings", "plc", "spa", "s.a.", "n.v.", "sa/nv", "groep", "company"}


def resolve_lei(long_name: str | None) -> str | None:
    """Look up the LEI (company ID) from a name like 'Anheuser-Busch InBev', or pass an LEI through."""
    if long_name and len(long_name) == 20 and long_name.isalnum():
        return long_name.upper()
    if not long_name:
        return None
    from eu_discovery import canonical_name
    wanted = canonical_name(long_name)
    words = [w.strip(",.()") for w in long_name.replace("-", " ").split()]
    words = [w for w in words if w and w.lower() not in STOP]
    queries = [long_name]
    if len(words) == 1 and words[0].isalpha() and len(words[0]) <= 6:
        queries.append(" ".join(words[0].upper()))
    for n in (2, 1):
        if len(words) < n:
            continue
        queries.append(" ".join(words[:n]))
    matches = {}
    for query in dict.fromkeys(queries):
        for hit in search(query):
            if canonical_name(hit["name"]) == wanted:
                matches[hit["lei"]] = hit
    return next(iter(matches)) if len(matches) == 1 else None


def _period(p: str) -> tuple[date | None, date]:
    """xBRL-JSON periods use exclusive end dates: '2025-01-01T00:00:00/2026-01-01T00:00:00'."""
    def day(s):
        return date.fromisoformat(s[:10])
    if "/" in p:
        a, b = p.split("/")
        return day(a), day(b) - timedelta(days=1)
    return None, day(p) - timedelta(days=1)


def fetch(lei: str, filings=None, budget=None, report_limit=3) -> dict:
    if filings is None:
        filings = _get(f"/api/entities/{lei}/filings?page%5Bsize%5D=100", budget)["data"]
    by_period: dict[str, dict] = {}
    for f in filings:
        a = f["attributes"]
        if not a.get("json_url") or not a.get("period_end"):
            continue
        cur = by_period.get(a["period_end"])
        # one report per year; prefer the English version and the latest upload
        score = (a["json_url"].endswith("-en.json"), a.get("date_added") or "")
        if cur is None or score > cur["_score"]:
            by_period[a["period_end"]] = {**a, "_score": score}
    reports = [by_period[k] for k in sorted(by_period)][-report_limit:]
    if not reports:
        raise ValueError("no machine-readable annual reports found")

    values: dict[str, dict[date, float]] = {k: {} for k in CONCEPTS}
    starts: dict[date, date] = {}
    currency = None
    for rep in reports:  # oldest -> newest so newer reports overwrite restated numbers
        facts = _get(rep["json_url"], budget)["facts"]
        for f in facts.values():
            dims = f.get("dimensions", {})
            if set(dims) - {"concept", "entity", "period", "unit", "language"}:
                continue  # skip segment / breakdown facts
            concept = dims.get("concept", "")
            if not concept.startswith("ifrs-full:"):
                continue
            name = concept.split(":", 1)[1]
            try:
                val = float(f["value"])
            except (TypeError, ValueError):
                continue
            start, end = _period(dims.get("period", ""))
            for key, names in CONCEPTS.items():
                if name not in names:
                    continue
                if key in INSTANT:
                    if start is not None:
                        continue
                else:
                    if start is None or not 340 <= (end - start).days <= 380:
                        continue
                    starts[end] = start
                if key == "revenue" and currency is None and str(dims.get("unit", "")).startswith("iso4217:"):
                    currency = dims["unit"].split(":")[1].split("/")[0]
                prio = names.index(name)
                prev = values[key].get(end)
                if prev is None or prio <= prev[1]:
                    values[key][end] = (val, prio)
        time.sleep(0.3)

    ends = sorted(values["revenue"] or values["net_income"])
    annual = []
    for end in ends[-4:]:
        p = {"start": starts.get(end), "end": end}
        for key in CONCEPTS:
            hit = values[key].get(end)
            p[key] = hit[0] if hit else None
        if p["shares"] is None and p["eps"] and p["net_income"]:
            p["shares"] = p["net_income"] / p["eps"]
        # without capex we can't compute free cash flow honestly
        p["fcf"] = p["ocf"] - abs(p["capex"]) if p["ocf"] is not None and p["capex"] is not None else None
        annual.append(p)
    if not annual:
        raise ValueError("annual report has no revenue or profit data")

    last = reports[-1]
    return {
        "currency": currency or "EUR",
        "annual": annual,
        "quarterly": [],
        "filing": {"accn": last.get("fxo_id") or last["json_url"], "form": "Annual report (ESEF)",
                   "filed": (last.get("date_added") or "")[:10], "period_end": last["period_end"]},
        "lei": lei,
    }
