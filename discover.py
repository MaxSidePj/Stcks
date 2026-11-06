"""
Market-wide screener: scores every US-listed company (Nasdaq/NYSE) using the SEC "frames" API,
which returns one value per company for a given metric and period - so the whole market costs
~60 requests instead of thousands.
"""
from __future__ import annotations

import json
import time
import urllib.error
from datetime import date, timedelta

import pulse as core

REVENUE = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
           "RevenueFromContractWithCustomerIncludingAssessedTax"]

FIELDS = ("revenue", "net_income", "gross_profit", "operating_income", "shares",
          "ocf", "capex", "cash", "debt", "equity", "fcf")


def frame(concept: str, unit: str, period: str) -> dict[int, dict]:
    url = f"https://data.sec.gov/api/xbrl/frames/us-gaap/{concept}/{unit}/{period}.json"
    try:
        data = json.loads(core.sec_get(url))["data"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise
    finally:
        time.sleep(0.15)  # SEC fair access
    return {row["cik"]: row for row in data}


def merged(concepts: list[str], unit: str, period: str) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for c in concepts:
        for cik, row in frame(c, unit, period).items():
            out.setdefault(cik, row)
    return out


def latest_annual_year(today: date) -> int:
    # 10-Ks are due 60-90 days after year end; by late April nearly all are in
    return today.year - 1 if (today.month, today.day) >= (4, 20) else today.year - 2


def latest_quarter(today: date) -> tuple[int, int]:
    # 10-Qs are due 40-45 days after quarter end; wait 60 days for good coverage
    cutoff = today - timedelta(days=60)
    # the quarter containing the cutoff date hasn't ended yet -> take the one before it
    return prev_quarter(cutoff.year, (cutoff.month - 1) // 3 + 1)


def prev_quarter(y: int, q: int) -> tuple[int, int]:
    return (y - 1, 4) if q == 1 else (y, q - 1)


def d(s):
    return date.fromisoformat(s)


def run(cfg: dict) -> list[dict]:
    today = date.today()
    Y = latest_annual_year(today)
    print(f"Discovery: annual frames CY{Y - 3}-CY{Y}", flush=True)

    rev = {y: merged(REVENUE, "USD", f"CY{y}") for y in range(Y - 3, Y + 1)}
    two = {}
    for key, concept, unit in (("net_income", "NetIncomeLoss", "USD"),
                               ("gross_profit", "GrossProfit", "USD"),
                               ("operating_income", "OperatingIncomeLoss", "USD"),
                               ("ocf", "NetCashProvidedByUsedInOperatingActivities", "USD"),
                               ("capex", "PaymentsToAcquirePropertyPlantAndEquipment", "USD"),
                               ("shares", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares")):
        two[key] = {y: frame(concept, unit, f"CY{y}") for y in (Y - 1, Y)}

    # Balance sheet: companies have different fiscal year ends, so fetch several instants
    instant_periods = [f"CY{Y}Q2I", f"CY{Y}Q3I", f"CY{Y}Q4I", f"CY{Y + 1}Q1I", f"CY{Y + 1}Q2I"]
    inst = {}
    for key, concepts in (("equity", ["StockholdersEquity"]),
                          ("cash", ["CashAndCashEquivalentsAtCarryingValue"]),
                          ("debt", ["LongTermDebt", "LongTermDebtNoncurrent"])):
        inst[key] = [merged(concepts, "USD", p) for p in instant_periods]

    qy, qq = latest_quarter(today)
    py, pq = prev_quarter(qy, qq)
    q_periods = [(qy - 1, qq), (py - 1, pq), (py, pq), (qy, qq)]
    print(f"Discovery: quarterly frames up to CY{qy}Q{qq}", flush=True)
    qrev = [merged(REVENUE, "USD", f"CY{y}Q{q}") for y, q in q_periods]

    exchanges = set(cfg.get("discovery_exchanges", ["Nasdaq", "NYSE"]))
    listing: dict[int, dict] = {}
    ex = json.loads(core.sec_get("https://www.sec.gov/files/company_tickers_exchange.json"))
    for cik, name, ticker, exch in ex["data"]:
        if exch in exchanges and cik not in listing:
            listing[cik] = {"ticker": ticker.upper(), "name": name, "exchange": exch}

    min_rev = cfg.get("discovery_min_revenue", 100_000_000)
    results = []
    for cik, row in rev[Y].items():
        if cik not in listing or row["val"] < min_rev or cik not in rev[Y - 1]:
            continue
        end_y = d(row["end"])

        annual = []
        for y in range(Y - 3, Y + 1):
            r = rev[y].get(cik)
            if not r:
                continue
            p = dict.fromkeys(FIELDS)
            p["end"], p["revenue"] = d(r["end"]), float(r["val"])
            if y in (Y - 1, Y):
                for key in two:
                    v = two[key][y].get(cik)
                    p[key] = float(v["val"]) if v else None
            annual.append(p)

        cur = annual[-1]
        for key, frames in inst.items():
            best = None
            for fr in frames:
                v = fr.get(cik)
                if v:
                    gap = abs((d(v["end"]) - end_y).days)
                    if gap <= 45 and (best is None or gap < best[0]):
                        best = (gap, float(v["val"]))
            cur[key] = best[1] if best else None
        for p in annual:
            p["fcf"] = p["ocf"] - abs(p["capex"] or 0) if p["ocf"] is not None else None
            if p["fcf"] is not None and p["revenue"] and abs(p["fcf"]) > p["revenue"]:
                p["ocf"] = p["fcf"] = None  # brokers/banks: customer money distorts cash flow

        quarterly = [{"end": d(fr[cik]["end"]), "revenue": float(fr[cik]["val"]), "net_income": None}
                     for fr in qrev if cik in fr]

        fin = {"currency": "USD", "annual": annual, "quarterly": sorted(quarterly, key=lambda x: x["end"]),
               "filing": None}
        an = core.analyze(fin, cfg)
        results.append({**listing[cik], "cik": cik, "fin": fin, "analysis": an})

    results.sort(key=lambda c: (-c["analysis"]["score"], -(c["analysis"]["metrics"].get("revenue_growth") or 0)))
    print(f"Discovery: scored {len(results)} companies", flush=True)
    return results


def lists(universe: list[dict], cfg: dict) -> dict[str, list[dict]]:
    """Curated views of the universe."""
    def m(c, k):
        return c["analysis"]["metrics"].get(k)

    def has(c, sid):
        return any(s["id"] == sid for s in c["analysis"]["signals"])

    min_score = cfg.get("discovery_min_score", 75)
    radar = [c for c in universe if c["analysis"]["score"] >= min_score]
    fast = sorted([c for c in universe if (m(c, "revenue_growth") or 0) >= 0.25
                   and c["fin"]["annual"][-2]["revenue"] >= 50e6  # skip "growth" from a near-zero base and (m(c, "fcf") or -1) > 0
                   and (c["fin"]["annual"][-1]["net_income"] or -1) > 0],
                  key=lambda c: -m(c, "revenue_growth"))
    accel = sorted([c for c in universe if has(c, "growth_accel") and c["analysis"]["score"] >= 60],
                   key=lambda c: -(m(c, "q_growth") or 0))
    turn = sorted([c for c in universe if has(c, "turned_profitable") and (m(c, "revenue_growth") or 0) > 0],
                  key=lambda c: -c["analysis"]["score"])
    cash = sorted([c for c in universe if (m(c, "fcf_margin") or 0) >= 0.25 and (m(c, "revenue_growth") or 0) >= 0.05],
                  key=lambda c: -m(c, "fcf_margin"))
    return {"radar": radar, "fast": fast, "accel": accel, "turn": turn, "cash": cash}
