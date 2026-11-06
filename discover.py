"""
Market-wide screener.

1. US companies (and Chinese ADRs reporting in CNY): SEC "frames" API - one request returns a metric for
   every company, so thousands of companies cost ~60 requests (cached for a day).
2. Large foreign companies listed in the US (Europe, Asia, Canada, Latin America) that report under IFRS:
   their full SEC filings are parsed one by one (cached for a day).
"""
from __future__ import annotations

import json
import time
import urllib.error
from datetime import date, timedelta

import cache
import market
import pulse as core

REVENUE = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
           "RevenueFromContractWithCustomerIncludingAssessedTax"]

FIELDS = ("revenue", "net_income", "gross_profit", "operating_income", "shares",
          "ocf", "capex", "cash", "debt", "equity", "fcf", "dividends", "buybacks")

LISTING: dict[int, dict] = {}   # cik -> {ticker, name, exchange}


def frame(concept: str, unit: str, period: str) -> dict[int, dict]:
    url = f"https://data.sec.gov/api/xbrl/frames/us-gaap/{concept}/{unit}/{period}.json"

    def fetch():
        try:
            data = json.loads(core.sec_get(url))["data"]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            raise
        finally:
            time.sleep(0.12)  # SEC fair access
        return [{k: r.get(k) for k in ("cik", "start", "end", "val")} for r in data]
    rows = cache.get(f"frame:{concept}:{unit}:{period}", 20, fetch)
    return {r["cik"]: r for r in rows}


def merged(concepts: list[str], unit: str, period: str) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for c in concepts:
        for cik, row in frame(c, unit, period).items():
            out.setdefault(cik, row)
    return out


def latest_annual_year(today: date) -> int:
    # 10-Ks are due 60-90 days after year end; by late April nearly all are in
    return today.year - 1 if (today.month, today.day) >= (4, 20) else today.year - 2


def prev_quarter(y: int, q: int) -> tuple[int, int]:
    return (y - 1, 4) if q == 1 else (y, q - 1)


def latest_quarter(today: date) -> tuple[int, int]:
    # 10-Qs are due 40-45 days after quarter end; wait 60 days for good coverage
    cutoff = today - timedelta(days=60)
    return prev_quarter(cutoff.year, (cutoff.month - 1) // 3 + 1)


def d(s):
    return date.fromisoformat(s)


def load_listing(cfg: dict) -> None:
    exchanges = set(cfg.get("discovery_exchanges", ["Nasdaq", "NYSE"]))
    ex = cache.get("listing", 20, lambda: json.loads(core.sec_get("https://www.sec.gov/files/company_tickers_exchange.json")))
    for cik, name, ticker, exch in ex["data"]:
        if exch in exchanges and cik not in LISTING:
            LISTING[cik] = {"ticker": ticker.upper(), "name": name, "exchange": exch}


def _frames_universe(cfg: dict, cur: str, skip: set[int]) -> list[dict]:
    today = date.today()
    Y = latest_annual_year(today)
    rev = {y: merged(REVENUE, cur, f"CY{y}") for y in range(Y - 3, Y + 1)}
    if not rev[Y]:
        return []
    two = {}
    for key, concept, unit in (("net_income", "NetIncomeLoss", cur),
                               ("gross_profit", "GrossProfit", cur),
                               ("operating_income", "OperatingIncomeLoss", cur),
                               ("ocf", "NetCashProvidedByUsedInOperatingActivities", cur),
                               ("capex", "PaymentsToAcquirePropertyPlantAndEquipment", cur),
                               ("shares", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares")):
        two[key] = {y: frame(concept, unit, f"CY{y}") for y in (Y - 1, Y)}
    returns = {"dividends": merged(["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"], cur, f"CY{Y}"),
               "buybacks": merged(["PaymentsForRepurchaseOfCommonStock"], cur, f"CY{Y}")}
    instant_periods = [f"CY{Y}Q2I", f"CY{Y}Q3I", f"CY{Y}Q4I", f"CY{Y + 1}Q1I", f"CY{Y + 1}Q2I"]
    inst = {key: [merged(concepts, cur, p) for p in instant_periods]
            for key, concepts in (("equity", ["StockholdersEquity"]),
                                  ("cash", ["CashAndCashEquivalentsAtCarryingValue"]),
                                  ("debt", ["LongTermDebt", "LongTermDebtNoncurrent"]))}
    qy, qq = latest_quarter(today)
    py, pq = prev_quarter(qy, qq)
    qrev = [merged(REVENUE, cur, f"CY{y}Q{q}") for y, q in [(qy - 1, qq), (py - 1, pq), (py, pq), (qy, qq)]] \
        if cur == "USD" else []

    rate = market.fx("USD", cur) or 1.0
    min_rev = cfg.get("discovery_min_revenue", 100_000_000) * rate
    results = []
    for cik, row in rev[Y].items():
        if cik in skip or cik not in LISTING or row["val"] < min_rev or cik not in rev[Y - 1]:
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
        last = annual[-1]
        for key, fr in returns.items():
            last[key] = float(fr[cik]["val"]) if cik in fr else None
        for key, frames in inst.items():
            best = None
            for fr in frames:
                v = fr.get(cik)
                if v:
                    gap = abs((d(v["end"]) - end_y).days)
                    if gap <= 45 and (best is None or gap < best[0]):
                        best = (gap, float(v["val"]))
            last[key] = best[1] if best else None
        for p in annual:
            p["fcf"] = p["ocf"] - abs(p["capex"] or 0) if p["ocf"] is not None else None
            if p["fcf"] is not None and p["revenue"] and abs(p["fcf"]) > p["revenue"]:
                p["ocf"] = p["fcf"] = None  # brokers/banks: customer money distorts cash flow
        quarterly = sorted([{"end": d(fr[cik]["end"]), "revenue": float(fr[cik]["val"]), "net_income": None}
                            for fr in qrev if cik in fr], key=lambda x: x["end"])
        fin = {"currency": cur, "annual": annual, "quarterly": quarterly, "filing": None}
        results.append({**LISTING[cik], "cik": cik, "fin": fin, "analysis": core.analyze(fin, cfg)})
    return results


# ---- foreign filers (IFRS): parse full company filings, cached as JSON

def _fin_to_json(f: dict) -> dict:
    def conv(p):
        return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in p.items()}
    return {**f, "annual": [conv(p) for p in f["annual"]], "quarterly": [conv(p) for p in f["quarterly"]]}


def _fin_from_json(f: dict) -> dict:
    def conv(p):
        return {k: (d(v) if k in ("start", "end") and isinstance(v, str) else v) for k, v in p.items()}
    return {**f, "annual": [conv(p) for p in f["annual"]], "quarterly": [conv(p) for p in f["quarterly"]]}


def company_fin(cik: int) -> dict:
    def fetch():
        raw = json.loads(core.sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"))
        time.sleep(0.12)
        return _fin_to_json(core.parse(raw))
    return _fin_from_json(cache.get(f"fin2:{cik}", 20, fetch))


def _foreign_universe(cfg: dict, screener: dict, skip: set[int]) -> list[dict]:
    by_ticker = {market.norm(v["ticker"]): (cik, v) for cik, v in LISTING.items()}
    min_mcap = cfg.get("foreign_min_mcap", 5e9)
    out = []
    picks = [(sym, row) for sym, row in screener.items()
             if row.get("country") not in ("", "United States") and (row.get("mcap") or 0) >= min_mcap
             and sym in by_ticker and by_ticker[sym][0] not in skip]
    print(f"Discovery: {len(picks)} large foreign companies to check", flush=True)
    for sym, row in picks:
        cik, lst = by_ticker[sym]
        try:
            fin = company_fin(cik)
        except Exception:
            continue
        if not fin["annual"] or not fin["annual"][-1].get("revenue") or len(fin["annual"]) < 2:
            continue
        if (date.today() - fin["annual"][-1]["end"]).days > cfg.get("max_data_age_days", 700):
            continue  # stale (stopped filing XBRL)
        skip.add(cik)
        out.append({**lst, "cik": cik, "fin": fin, "analysis": core.analyze(fin, cfg)})
    return out


def run(cfg: dict, screener: dict) -> list[dict]:
    load_listing(cfg)
    results: list[dict] = []
    seen: set[int] = set()
    for cur in cfg.get("discovery_currencies", ["USD", "CNY"]):
        print(f"Discovery: US-GAAP filers reporting in {cur}", flush=True)
        part = _frames_universe(cfg, cur, seen)
        seen |= {c["cik"] for c in part}
        results += part
    if screener and cfg.get("foreign_discovery", True):
        results += _foreign_universe(cfg, screener, seen)
    print(f"Discovery: scored {len(results)} companies", flush=True)
    return results


def attach_market(universe: list[dict], screener: dict[str, dict]) -> dict[str, float]:
    """Price, market cap, sector, region and valuation (incl. P/E vs sector median) for every company."""
    for c in universe:
        mk = screener.get(market.norm(c["ticker"])) or {}
        c["market"] = mk
        c["sector"] = mk.get("sector") or "Other"
        c["country"] = mk.get("country") or ""
        c["region"] = market.region(c["country"])
        mcap = mk.get("mcap")
        if mcap and c["fin"]["currency"] != "USD":
            rate = market.fx("USD", c["fin"]["currency"])
            mcap = mcap * rate if rate else None
        c["mcap_local"] = mcap
    pes = []
    for c in universe:
        ni = market.ttm_net_income(c["fin"])
        if c["mcap_local"] and (c["market"].get("mcap") or 0) >= 3e8 and ni and ni > 0:
            pes.append((c["sector"], c["mcap_local"] / ni))
    medians = market.sector_medians(pes)
    for c in universe:
        c["val"] = market.valuation(c["fin"], c["analysis"]["metrics"], c["mcap_local"],
                                    medians.get(c["sector"]), c["sector"])
    return medians


def lists(universe: list[dict], cfg: dict) -> dict[str, list[dict]]:
    """Curated views of the universe."""
    def m(c, k):
        return c["analysis"]["metrics"].get(k)

    def has(c, sid):
        return any(s["id"] == sid for s in c["analysis"]["signals"])

    min_score = cfg.get("discovery_min_score", 75)
    radar = [c for c in universe if c["analysis"]["score"] >= min_score]
    fast = sorted([c for c in universe if (m(c, "revenue_growth") or 0) >= 0.25
                   and (c["fin"]["annual"][-2]["revenue"] or 0) >= 50e6   # skip "growth" from a near-zero base
                   and (m(c, "fcf") or -1) > 0
                   and (c["fin"]["annual"][-1]["net_income"] or -1) > 0],
                  key=lambda c: -m(c, "revenue_growth"))
    accel = sorted([c for c in universe if has(c, "growth_accel") and c["analysis"]["score"] >= 60],
                   key=lambda c: -(m(c, "q_growth") or 0))
    turn = sorted([c for c in universe if has(c, "turned_profitable") and (m(c, "revenue_growth") or 0) > 0],
                  key=lambda c: -c["analysis"]["score"])
    cash = sorted([c for c in universe if (m(c, "fcf_margin") or 0) >= 0.25 and (m(c, "revenue_growth") or 0) >= 0.05],
                  key=lambda c: -m(c, "fcf_margin"))
    value = sorted([c for c in universe if c["analysis"]["score"] >= cfg.get("value_min_score", 70)
                    and (c.get("val") or {}).get("label") == "Attractive"
                    and (c["market"].get("mcap") or 0) >= cfg.get("value_min_mcap", 1e9)],
                   key=lambda c: (-c["val"]["value_score"], -(c["val"].get("fcf_yield") or 0)))
    return {"value": value, "radar": radar, "fast": fast, "accel": accel, "turn": turn, "cash": cash}
